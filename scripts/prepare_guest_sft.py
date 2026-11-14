"""Prepare reviewed extraction-only chat data; never contacts or trains a model.

    python -m scripts.prepare_guest_sft --input reviewed.jsonl --out outputs/sft

Rows require id, group_id, source_dataset, record, context, target and annotation.
The annotation requires reviewed: true, reviewer: str, reviewer_kind:
human|assistant|model and method: manual|teacher_reviewed, with optional string
notes. An annotation is an operator assertion, not a proof.
Use a single source_dataset (quickstart, dev or synthetic) per export. Benchmark
messages and their connected groups are excluded; organizer Holdout is forbidden.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import json
import math
from pathlib import Path
import re
import shutil
import subprocess
import unicodedata

from common.contracts.schemas import GuestIntentV1
from ingestion.guest_generation import generation_schema, generation_schema_sha256
from ingestion.guest_parser import (
    PARSER_VERSION, PROMPT_VERSION, _canonical, _known_context, _system_prompt, _utc,
)

ROOT = Path(__file__).resolve().parents[1]
EVALUATION_FIXTURES = (
    ROOT / "tests/fixtures/parser_benchmark_cases.jsonl",
    ROOT / "tests/fixtures/parser_local_acceptance.jsonl",
)
ROW_KEYS = {"id", "group_id", "source_dataset", "record", "context", "target", "annotation"}
TARGET_KEYS = {
    "intent", "requested_time_utc", "requested_time_text", "evidence_quote", "details", "needs_review",
}
SOURCE_DATASETS = {"quickstart", "dev", "synthetic"}
RECORD_KEYS = {
    "request_id", "timestamp_utc", "room_id", "booking_id", "channel", "language",
    "request_type", "text", "priority", "response_due_utc", "requires_guest_consent",
}
SPLITS = ("train", "valid", "test")


def _sha256(value: bytes | str) -> str:
    return hashlib.sha256(value.encode("utf-8") if isinstance(value, str) else value).hexdigest()


def normalize_text(text: str) -> str:
    """Exact-text guard only; this deliberately makes no paraphrase guarantee."""
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", text).casefold()).strip()


def _nonempty(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _pairs(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON object key")
        result[key] = value
    return result


def _invalid_constant(value: str) -> None:
    raise ValueError("Non-finite JSON number")


def _finite_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError("Non-finite JSON number")
    return parsed


def _json_rows(content: bytes) -> list[dict]:
    rows = []
    for number, line in enumerate(content.decode("utf-8-sig").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line, object_pairs_hook=_pairs, parse_constant=_invalid_constant,
                             parse_float=_finite_float)
        except (ValueError, TypeError) as exc:
            raise ValueError(f"Invalid JSON at line {number}") from exc
        if not isinstance(row, dict):
            raise ValueError(f"Expected an object at line {number}")
        rows.append(row)
    return rows


def _validate_row(row: dict, number: int) -> None:
    prefix = f"Row {number}: "
    if set(row) != ROW_KEYS:
        raise ValueError(prefix + "expected exactly the documented row keys")
    if not all(_nonempty(row[key]) for key in ("id", "group_id", "source_dataset")):
        raise ValueError(prefix + "id, group_id and source_dataset must be nonempty strings")
    if row["source_dataset"] not in SOURCE_DATASETS:
        raise ValueError(prefix + "source_dataset must be quickstart, dev or synthetic; Holdout is forbidden")
    annotation = row["annotation"]
    if (not isinstance(annotation, dict)
            or not {"reviewed", "reviewer", "reviewer_kind", "method"} <= set(annotation)
            or not set(annotation) <= {"reviewed", "reviewer", "reviewer_kind", "method", "notes"}
            or annotation.get("reviewed") is not True
            or not _nonempty(annotation.get("reviewer"))
            or annotation.get("reviewer_kind") not in ("human", "assistant", "model")
            or annotation.get("method") not in ("manual", "teacher_reviewed")
            or ("notes" in annotation and not isinstance(annotation["notes"], str))):
        raise ValueError(prefix + "an explicit reviewed annotation with reviewer and method is required")
    record, context, target = row["record"], row["context"], row["target"]
    if not isinstance(record, dict) or not isinstance(context, dict):
        raise ValueError(prefix + "record and context must be objects")
    if not set(record) <= RECORD_KEYS:
        raise ValueError(prefix + "record may contain only the documented guest_requests.csv columns")
    if any(value is not None and not isinstance(value, str) for value in record.values()):
        raise ValueError(prefix + "source record columns must be strings or null")
    required = ("request_id", "timestamp_utc", "language", "request_type", "text")
    if not all(_nonempty(record.get(key)) for key in required):
        raise ValueError(prefix + "record requires nonempty request_id, timestamp_utc, language, request_type and text")
    if not record["timestamp_utc"].endswith("Z") or _utc(record["timestamp_utc"]) is None:
        raise ValueError(prefix + "record timestamp_utc must be a valid UTC timestamp ending in Z")
    if "as_of_utc" in context and context["as_of_utc"] is not None:
        if not isinstance(context["as_of_utc"], str) or _utc(context["as_of_utc"]) is None:
            raise ValueError(prefix + "context as_of_utc must be an aware timestamp or null")
    if "use_llm" in context and type(context["use_llm"]) is not bool:
        raise ValueError(prefix + "context use_llm must be boolean")
    if not isinstance(target, dict) or set(target) != {"intents"}:
        raise ValueError(prefix + "target must contain exactly intents")
    intents = target["intents"]
    if not isinstance(intents, list) or not 1 <= len(intents) <= 32:
        raise ValueError(prefix + "target must contain 1..32 intents")
    for item in intents:
        if not isinstance(item, dict) or set(item) != TARGET_KEYS:
            raise ValueError(prefix + "each target intent must contain exactly the extraction fields")
        if (item["requested_time_utc"] is not None or not isinstance(item["details"], dict)
                or item["details"] or type(item["needs_review"]) is not bool):
            raise ValueError(prefix + "extraction targets require null UTC, empty details and boolean needs_review")
        evidence, quoted_time = item["evidence_quote"], item["requested_time_text"]
        if not _nonempty(evidence) or evidence not in record["text"]:
            raise ValueError(prefix + "evidence_quote must be an exact nonempty source substring")
        if quoted_time is not None and (not _nonempty(quoted_time) or quoted_time not in record["text"]):
            raise ValueError(prefix + "requested_time_text must be null or an exact nonempty source substring")
        try:
            GuestIntentV1.model_validate(item, strict=True)
        except (ValueError, TypeError) as exc:
            raise ValueError(prefix + "target violates the frozen intent contract") from exc


def _code_manifest() -> dict:
    files = (
        "scripts/prepare_guest_sft.py", "ingestion/guest_parser.py", "ingestion/guest_generation.py",
        "ingestion/request_polarity.py", "ingestion/llm_transport.py", "common/contracts/schemas.py",
    )
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, check=True, capture_output=True, text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        commit = None
    return {"git_commit": commit, "files": {name: _sha256((ROOT / name).read_bytes()) for name in files}}


def prepare_dataset(input_path: Path, output_dir: Path, *, seed: int = 17,
                    valid_fraction: float = 0.1, test_fraction: float = 0.1) -> dict:
    """Validate everything, group before splitting, then create a new export."""
    input_path, output_dir = Path(input_path), Path(output_dir)
    if output_dir.exists() or output_dir.is_symlink():
        raise FileExistsError("Output directory must not already exist")
    if type(seed) is not int:
        raise ValueError("seed must be an integer")
    for value in (valid_fraction, test_fraction):
        if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value < 1:
            raise ValueError("Split fractions must be finite numbers in [0, 1)")
    if valid_fraction + test_fraction >= 1:
        raise ValueError("Split fractions must leave a positive training fraction")
    input_bytes = input_path.read_bytes()
    rows = _json_rows(input_bytes)
    if not rows:
        raise ValueError("Input has no annotated rows")
    for number, row in enumerate(rows, 1):
        _validate_row(row, number)
    datasets = {row["source_dataset"] for row in rows}
    if len(datasets) != 1:
        raise ValueError("Use one source_dataset per export; never merge Quickstart and Dev")

    protected_texts, protected_identities, fixtures = set(), set(), []
    for fixture in EVALUATION_FIXTURES:
        content = fixture.read_bytes()
        cases = _json_rows(content)
        texts = {normalize_text(case["record"]["text"]) for case in cases}
        protected_texts.update(texts)
        for case in cases:
            record = case["record"]
            for kind, column in (("request", "request_id"), ("booking", "booking_id")):
                if _nonempty(record.get(column)):
                    protected_identities.add((kind, record[column]))
        fixtures.append({"path": str(fixture.relative_to(ROOT)), "sha256": _sha256(content),
                         "normalized_texts": len(texts)})

    system = _system_prompt("extraction") + "\nSchema: " + _canonical(generation_schema("extraction"))
    parents = list(range(len(rows)))

    def find(index):
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    identities, inputs, source_records, row_ids, prepared = {}, {}, {}, {}, []
    for index, row in enumerate(rows):
        record = row["record"]
        safe, _ = _known_context(record, row["context"])
        user = _canonical({"guest_record": dict(record), "known_context": safe})
        assistant = _canonical(row["target"])
        input_hash, target_hash = _sha256(user), _sha256(assistant)
        if input_hash in inputs and inputs[input_hash] != target_hash:
            raise ValueError("Conflicting target labels for identical production input")
        inputs[input_hash] = target_hash
        row_identity = (input_hash, target_hash)
        if row["id"] in row_ids and row_ids[row["id"]] != row_identity:
            raise ValueError("A row id is reused for conflicting inputs or labels")
        row_ids[row["id"]] = row_identity
        source_id = record["request_id"]
        source_record = _canonical(record)
        if source_id in source_records and source_records[source_id] != source_record:
            raise ValueError("Conflicting source records for the same request_id")
        source_records[source_id] = source_record
        text = normalize_text(record["text"])
        keys = {("group", row["group_id"]), ("id", row["id"]), ("request", source_id),
                ("text", text), ("input", input_hash)}
        if _nonempty(record.get("booking_id")):
            keys.add(("booking", record["booking_id"]))
        for key in keys:
            if key in identities:
                parents[find(index)] = find(identities[key])
            else:
                identities[key] = index
        prepared.append({"row": row, "user": user, "assistant": assistant,
                         "input_sha256": input_hash, "target_sha256": target_hash,
                         "keys": keys, "protected": text in protected_texts or bool(keys & protected_identities),
                         "protected_identity": bool(keys & protected_identities)})

    groups = defaultdict(list)
    for index, item in enumerate(prepared):
        groups[find(index)].append(item)
    retained, excluded_evaluation, excluded_related, excluded_groups = [], 0, 0, 0
    group_counts = Counter({split: 0 for split in SPLITS})
    group_sizes = []
    for members in groups.values():
        direct = sum(item["protected"] for item in members)
        if direct:
            excluded_evaluation += direct
            excluded_related += len(members) - direct
            excluded_groups += 1
            continue
        keys = sorted({key for item in members for key in item["keys"]})
        group_hash = _sha256(_canonical(keys))
        score = int(_sha256(f"{seed}:{group_hash}"), 16) / (1 << 256)
        split = "test" if score < test_fraction else "valid" if score < test_fraction + valid_fraction else "train"
        group_counts[split] += 1
        unique = defaultdict(list)
        for item in members:
            unique[item["input_sha256"]].append(item)
        group_sizes.append(len(unique))
        for duplicates in unique.values():
            item = duplicates[0]
            retained.append({"split": split, "group_sha256": group_hash, "item": item,
                             "duplicates": duplicates})
    if not retained:
        raise ValueError("No rows remain after excluding evaluation messages and related groups")

    lines = {split: [] for split in SPLITS}
    examples = []
    for example in sorted(retained, key=lambda value: value["item"]["input_sha256"]):
        split, item, duplicates = example["split"], example["item"], example["duplicates"]
        messages = [{"role": "system", "content": system}, {"role": "user", "content": item["user"]},
                    {"role": "assistant", "content": item["assistant"]}]
        lines[split].append(_canonical({"messages": messages}) + "\n")
        examples.append({
            "ids": sorted({part["row"]["id"] for part in duplicates}),
            "group_ids": sorted({part["row"]["group_id"] for part in duplicates}),
            "source_dataset": item["row"]["source_dataset"], "split": split,
            "output_line": len(lines[split]), "input_sha256": item["input_sha256"],
            "target_sha256": item["target_sha256"], "group_sha256": example["group_sha256"],
            "source_rows": sorted([
                {"id": part["row"]["id"], "sha256": _sha256(_canonical(part["row"])),
                 "reviewer_kind": part["row"]["annotation"]["reviewer_kind"],
                 "annotation_sha256": _sha256(_canonical(part["row"]["annotation"]))}
                for part in duplicates
            ], key=lambda part: (part["id"], part["sha256"])),
        })
    output_bytes = {f"{split}.jsonl": "".join(lines[split]).encode("utf-8") for split in SPLITS}
    output_counts = {split: len(lines[split]) for split in SPLITS}
    fractions = {"train": 1 - valid_fraction - test_fraction, "valid": valid_fraction, "test": test_fraction}
    empty_splits = [split for split in SPLITS if fractions[split] > 0 and not output_counts[split]]
    warnings = []
    if empty_splits:
        warnings.append("Requested splits are empty: " + ", ".join(empty_splits)
                        + ". Add independent groups; related rows were not split to fill them.")
    if max(group_sizes) > len(retained) / 2:
        warnings.append(f"The largest connected group contains {max(group_sizes)}/{len(retained)} unique inputs; split balance is limited.")
    manifest = {
        "schema_version": "guest-sft-v1", "generation_profile": "extraction",
        "source_dataset": next(iter(datasets)), "parser_version": PARSER_VERSION, "prompt_version": PROMPT_VERSION,
        "input": {"path": str(input_path.resolve()), "sha256": _sha256(input_bytes), "rows": len(rows)},
        "code": _code_manifest(),
        "prompt": {"system_sha256": _sha256(system),
                   "generation_schema_sha256": generation_schema_sha256("extraction"),
                   "user_template": "canonical_json({guest_record:record,known_context:_known_context(record,context)[0]})"},
        "outputs": {name: {"sha256": _sha256(content), "rows": output_counts[Path(name).stem]}
                    for name, content in output_bytes.items()},
        "split": {"method": "sha256_connected_groups_v1", "seed": seed,
                  "valid_fraction": valid_fraction, "test_fraction": test_fraction,
                  "rows": output_counts, "groups": dict(group_counts),
                  "largest_group_rows": max(group_sizes), "empty_splits": empty_splits},
        "exclusion": {"fixtures": fixtures, "normalization": "NFKC + casefold + whitespace collapse",
                      "identity_guard": "exact declared evaluation request_id/booking_id and connected training groups",
                      "identity_matched_rows": sum(item["protected_identity"] for item in prepared),
                      "excluded_evaluation_rows": excluded_evaluation, "excluded_related_rows": excluded_related,
                      "excluded_groups": excluded_groups,
                      "duplicates_removed": len(rows) - excluded_evaluation - excluded_related - len(retained)},
        "annotation": {"reviewer_kind_counts": dict(Counter(row["annotation"]["reviewer_kind"] for row in rows)),
                       "verification": "operator_asserted_review_not_independent_quality_verification"},
        "examples": examples,
        "warnings": warnings,
        "limits": [
            "Reviewed annotations and source_dataset are operator assertions; this utility does not independently verify truth or origin.",
            "Target validation checks schema and exact quotations, not semantic completeness or correct intent/uncertainty labels.",
            "Evaluation exclusion detects normalized exact text, exact declared evaluation request/booking IDs and connected groups, not undeclared paraphrases or shared templates.",
            "Grouped hash splits are deterministic, not stratified; small or connected datasets may have empty or imbalanced splits.",
            "The production context filter is reused unchanged; callers remain responsible for historically justified source metadata.",
            "No organizer Holdout is an accepted source. Source datasets remain separate; no model was called or trained.",
            "The Git commit identifies the checkout base; exact code hashes also cover uncommitted exporter code.",
        ],
    }
    output_bytes["manifest.json"] = (json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir()  # Exclusive creation: recheck immediately before any writes.
    try:
        for name, content in output_bytes.items():
            with (output_dir / name).open("xb") as handle:
                handle.write(content)
    except BaseException:
        shutil.rmtree(output_dir)
        raise
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path, dest="input_path")
    parser.add_argument("--out", required=True, type=Path, dest="output_dir")
    parser.add_argument("--seed", type=int, default=17)
    parser.add_argument("--valid-fraction", type=float, default=0.1)
    parser.add_argument("--test-fraction", type=float, default=0.1)
    manifest = prepare_dataset(**vars(parser.parse_args(argv)))
    print(json.dumps({"status": "prepared_not_trained", "rows": manifest["split"]["rows"],
                      "excluded": manifest["exclusion"]["excluded_evaluation_rows"]
                      + manifest["exclusion"]["excluded_related_rows"],
                      "warnings": manifest["warnings"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
