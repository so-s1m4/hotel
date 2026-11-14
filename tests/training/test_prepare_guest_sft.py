"""Independent bad-input and data-boundary checks for SFT preparation."""
from __future__ import annotations

import copy
import importlib
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
SPLITS = ("train", "valid", "test")


@pytest.fixture
def prepare():
    return importlib.import_module("scripts.prepare_guest_sft").prepare_dataset


def example(number: int, *, text: str | None = None) -> dict:
    text = text or f"Please bring fresh towels for synthetic visit {number}."
    return {
        "id": f"row-{number}",
        "group_id": f"group-{number}",
        "source_dataset": "synthetic",
        "record": {
            "request_id": f"request-{number}",
            "timestamp_utc": "2026-01-06T09:00:00Z",
            "language": "en",
            "request_type": "extra_towels",
            "text": text,
        },
        "context": {"as_of_utc": "2026-01-06T09:00:00Z", "timezone": "Europe/Vienna"},
        "target": {"intents": [{
            "intent": "extra_towels",
            "requested_time_utc": None,
            "requested_time_text": None,
            "evidence_quote": text,
            "details": {},
            "needs_review": False,
        }]},
        "annotation": {"reviewed": True, "reviewer": "offline-test-reviewer",
                       "reviewer_kind": "human", "method": "manual"},
    }


def write_rows(path: Path, rows: list[dict]) -> Path:
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
    return path


def output_rows(path: Path) -> list[dict]:
    return [json.loads(line) for split in SPLITS
            for line in (path / f"{split}.jsonl").read_text(encoding="utf-8").splitlines()]


def input_payload(output_row: dict) -> dict:
    return json.loads(next(message["content"] for message in output_row["messages"]
                           if message["role"] == "user"))


def test_connected_source_groups_never_cross_splits(prepare, tmp_path):
    rows = [example(number) for number in range(40)]
    # Distinct texts are linked transitively by an explicit group and booking.
    rows[0]["group_id"] = rows[1]["group_id"] = "related-guests"
    rows[1]["record"]["booking_id"] = rows[2]["record"]["booking_id"] = "linked-booking"
    # A formatting variant of row 2 must remain in that same component as well.
    rows[3]["record"]["text"] = "  " + rows[2]["record"]["text"].upper().replace(" ", "\t") + "  "
    rows[3]["target"]["intents"][0]["evidence_quote"] = rows[3]["record"]["text"]
    source = write_rows(tmp_path / "source.jsonl", rows)
    out = tmp_path / "prepared"

    manifest = prepare(source, out, seed=17, valid_fraction=0.2, test_fraction=0.2)

    by_id = {row_id: item["split"] for item in manifest["examples"] for row_id in item["ids"]}
    assert {f"row-{number}" for number in range(40)} == set(by_id)
    assert len({by_id[f"row-{number}"] for number in range(4)}) == 1
    grouped = {}
    for item in manifest["examples"]:
        grouped.setdefault(item["group_sha256"], set()).add(item["split"])
    assert all(len(splits) == 1 for splits in grouped.values())
    assert all(set(row) == {"messages"} for row in output_rows(out))


def test_normalized_evaluation_duplicate_excludes_its_related_group(prepare, tmp_path):
    evaluation = json.loads((ROOT / "tests/fixtures/parser_benchmark_cases.jsonl").read_text(
        encoding="utf-8").splitlines()[0])
    # NFKC compatibility characters plus case and whitespace changes evade byte comparison.
    upper = evaluation["record"]["text"].upper()
    disguised = " \n" + "\t".join(upper.split()) + "  "
    disguised = "".join(chr(ord(char) + 0xFEE0) if "A" <= char <= "Z" else char
                        for char in disguised)
    duplicate = example(1, text=disguised)
    related = example(2)
    related["group_id"] = duplicate["group_id"]
    independent = example(3)
    out = tmp_path / "prepared"

    manifest = prepare(write_rows(tmp_path / "source.jsonl", [duplicate, related, independent]), out)

    assert manifest["exclusion"]["excluded_evaluation_rows"] == 1
    assert manifest["exclusion"]["excluded_related_rows"] == 1
    assert [item["ids"] for item in manifest["examples"]] == [["row-3"]]
    assert len(output_rows(out)) == 1


@pytest.mark.parametrize('column', ['request_id', 'booking_id'])
def test_declared_evaluation_identity_is_excluded_even_with_changed_text(prepare, tmp_path, column):
    evaluation = json.loads((ROOT / 'tests/fixtures/parser_benchmark_cases.jsonl').read_text().splitlines()[0])
    overlap, related, unknown = example(201), example(202), example(203)
    overlap['record'][column] = evaluation['record'][column]
    overlap['group_id'] = related['group_id'] = 'same-declared-source-group'
    source = write_rows(tmp_path / 'source.jsonl', [overlap, related, unknown])
    out = tmp_path / 'prepared'
    manifest = prepare(source, out)
    assert manifest['exclusion']['identity_matched_rows'] == 1
    assert manifest['exclusion']['excluded_evaluation_rows'] == 1
    assert manifest['exclusion']['excluded_related_rows'] == 1
    assert [row['ids'] for row in manifest['examples']] == [['row-203']]
    assert input_payload(output_rows(out)[0])['guest_record']['text'] == unknown['record']['text']


def test_future_booking_and_actual_outcomes_never_enter_training_prompt(prepare, tmp_path):
    future, known = example(1), example(2)
    future["record"]["booking_id"] = "future-booking"
    future["context"]["booking"] = {
        "booking_id": "future-booking", "created_utc": "2026-01-07T09:00:00Z",
        "arrival_utc": "2026-01-08T14:00:00Z", "status": "FUTURE_CONFIRMED_MARKER",
    }
    known["record"]["booking_id"] = "known-booking"
    known["context"]["booking"] = {
        "booking_id": "known-booking", "created_utc": "2026-01-05T09:00:00Z",
        "arrival_utc": "2026-01-08T14:00:00Z", "departure_utc": "2026-01-10T10:00:00Z",
        "status": "FUTURE_CHECKED_OUT_MARKER", "completed_utc": "2026-01-10T10:10:00Z",
    }
    known["context"]["observed_later"] = "LATER_ROOM_RELEASE_MARKER"
    out = tmp_path / "prepared"
    prepare(write_rows(tmp_path / "source.jsonl", [future, known]), out)

    payloads = {payload["guest_record"]["request_id"]: payload
                for payload in map(input_payload, output_rows(out))}
    assert "booking" not in payloads["request-1"]["known_context"]
    known_booking = payloads["request-2"]["known_context"]["booking"]
    assert known_booking["arrival_utc"] == "2026-01-08T14:00:00Z"
    rendered = json.dumps(output_rows(out))
    assert "FUTURE_CONFIRMED_MARKER" not in rendered
    assert "FUTURE_CHECKED_OUT_MARKER" not in rendered
    assert "LATER_ROOM_RELEASE_MARKER" not in rendered
    assert "completed_utc" not in known_booking


@pytest.mark.parametrize("bad_field,bad_value", [
    ("evidence_quote", "The guest never wrote this sentence."),
    ("evidence_quote", ""),
    ("requested_time_text", "at 09:17 tomorrow"),
    ("requested_time_utc", "2026-01-07T08:17:00Z"),
    ("details", {"guest_consent_obtained": True}),
])
def test_unsupported_or_invented_target_fields_are_rejected(prepare, tmp_path, bad_field, bad_value):
    row = example(1)
    row["target"]["intents"][0][bad_field] = bad_value
    out = tmp_path / "prepared"
    with pytest.raises(ValueError):
        prepare(write_rows(tmp_path / "source.jsonl", [row]), out)
    assert not out.exists()


@pytest.mark.parametrize("field,value", [
    ("reviewed", False), ("reviewed", "true"), ("reviewer", ""),
    ("method", "source_label"), ("method", "unreviewed_teacher"), ("reviewer_kind", "unknown"),
])
def test_unreviewed_or_unattributed_annotations_are_rejected(prepare, tmp_path, field, value):
    row = example(1)
    row["annotation"][field] = value
    out = tmp_path / "prepared"
    with pytest.raises(ValueError):
        prepare(write_rows(tmp_path / "source.jsonl", [row]), out)
    assert not out.exists()


def test_conflicting_labels_for_one_immutable_source_record_are_rejected(prepare, tmp_path):
    first = example(1)
    conflicting = copy.deepcopy(first)
    conflicting["id"] = "second-annotation"
    conflicting["record"]["request_type"] = "wifi_help"
    with pytest.raises(ValueError):
        prepare(write_rows(tmp_path / "source.jsonl", [first, conflicting]), tmp_path / "prepared")


@pytest.mark.parametrize("reviewer_kind", ["assistant", "model"])
def test_explicit_nonhuman_review_is_allowed_and_not_mislabeled(prepare, tmp_path, reviewer_kind):
    row = example(1)
    row["annotation"]["reviewer_kind"] = reviewer_kind
    source = write_rows(tmp_path / "source.jsonl", [row])
    manifest = prepare(source, tmp_path / "prepared")
    assert len(manifest["examples"]) == 1
    assert json.loads(source.read_text(encoding="utf-8"))["annotation"]["reviewer_kind"] == reviewer_kind


@pytest.mark.parametrize("injected_key", ["gold", "target", "completed_utc", "booking_status"])
def test_unexpected_record_fields_cannot_carry_labels_or_future_outcomes(prepare, tmp_path, injected_key):
    row = example(1)
    row["record"][injected_key] = "unsupported-source-field"
    with pytest.raises(ValueError):
        prepare(write_rows(tmp_path / "source.jsonl", [row]), tmp_path / "prepared")


def test_identical_input_with_conflicting_targets_is_rejected(prepare, tmp_path):
    first = example(1)
    conflicting = copy.deepcopy(first)
    conflicting["id"] = "second-annotation"
    conflicting["target"]["intents"][0]["needs_review"] = True
    with pytest.raises(ValueError):
        prepare(write_rows(tmp_path / "source.jsonl", [first, conflicting]), tmp_path / "prepared")


def test_quickstart_and_dev_cannot_be_merged_in_one_export(prepare, tmp_path):
    first, second = example(1), example(2)
    first["source_dataset"], second["source_dataset"] = "quickstart", "dev"
    with pytest.raises(ValueError):
        prepare(write_rows(tmp_path / "source.jsonl", [first, second]), tmp_path / "prepared")


def test_existing_output_directory_is_not_modified(prepare, tmp_path):
    source = write_rows(tmp_path / "source.jsonl", [example(1)])
    out = tmp_path / "prepared"
    out.mkdir()
    marker = out / "unfinished-checkpoint.txt"
    marker.write_text("Do not overwrite an existing run.", encoding="utf-8")
    with pytest.raises((ValueError, FileExistsError)):
        prepare(source, out)
    assert {path.name for path in out.iterdir()} == {marker.name}
    assert marker.read_text(encoding="utf-8") == "Do not overwrite an existing run."


def test_reviewed_semantic_disagreement_preserves_source_label(prepare, tmp_path):
    row = example(1)
    row["record"]["request_type"] = "wifi_help"
    row["target"]["intents"][0]["needs_review"] = True
    out = tmp_path / "prepared"
    prepare(write_rows(tmp_path / "source.jsonl", [row]), out)
    generated = output_rows(out)[0]
    assert input_payload(generated)["guest_record"]["request_type"] == "wifi_help"
    target = json.loads(generated["messages"][-1]["content"])
    assert target["intents"][0]["intent"] == "extra_towels"
