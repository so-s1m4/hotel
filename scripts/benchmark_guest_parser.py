"""Bounded guest-parser benchmark; planning is the default, --execute permits calls.

Use one standalone process per run: instrumentation and environment changes are
scoped but process-global. Do not run unrelated parser calls in that process.
Transport counts mean calls to the original _call_llm, not server receipts.
Gold review flags describe post-host output; raw model review flags are not gold.
"""
from __future__ import annotations

import argparse
import copy
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime
import hashlib
import importlib
import json
import math
import os
from pathlib import Path
import re
import sys
from threading import Lock
import time
from typing import Any, get_args
from unittest.mock import patch
from urllib.parse import parse_qsl, unquote, urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from common.contracts.schemas import GuestIntentV1, SCHEMA_VERSION

_INTENTS = set(get_args(GuestIntentV1.model_fields["intent"].annotation))
_TRACE: ContextVar[dict | None] = ContextVar("parser_benchmark_trace", default=None)
_RUN_LOCK = Lock()
_MARKER = "benchmark_manifest.json"


@dataclass(frozen=True)
class BenchmarkConfig:
    input_path: str | Path
    limit: int
    candidate: str
    revision: str
    run_id: str
    cache_root: str | Path
    out: str | Path
    base_url: str
    model: str
    workers: int = 1
    execute: bool = False
    warm: bool = False
    timeout_seconds: float = 15.0


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value: str | bytes) -> str:
    return hashlib.sha256(value.encode("utf-8") if isinstance(value, str) else value).hexdigest()


def _utc(value: Any) -> bool:
    if not isinstance(value, str) or not value.endswith("Z"):
        return False
    try:
        return datetime.fromisoformat(value[:-1] + "+00:00").utcoffset().total_seconds() == 0
    except (ValueError, AttributeError):
        return False


def _invalid_constant(value: str) -> None:
    raise ValueError("Non-finite JSON number")


def load_cases(path: str | Path) -> list[dict]:
    """Validate the entire JSONL before selection; never invokes the parser."""
    cases, ids, records = [], set(), set()
    with Path(path).open(encoding="utf-8-sig") as handle:
        for line in handle:
            if not line.strip():
                continue
            case = json.loads(line, parse_constant=_invalid_constant)
            if not isinstance(case, dict) or not {"case_id", "record", "context", "gold"} <= case.keys():
                raise ValueError("Each case needs case_id, record, context and gold")
            case_id, record, context, gold = (case[k] for k in ("case_id", "record", "context", "gold"))
            if not isinstance(case_id, str) or not case_id.strip() or case_id in ids:
                raise ValueError("Case IDs must be nonempty and unique")
            if not isinstance(record, dict) or not isinstance(context, dict) or not isinstance(gold, dict):
                raise ValueError("record, context and gold must be objects")
            for field in ("request_id", "timestamp_utc", "language", "request_type", "text"):
                if not isinstance(record.get(field), str) or not record[field].strip():
                    raise ValueError("Record has an absent or invalid required string")
            if not _utc(record["timestamp_utc"]):
                raise ValueError("Record timestamp must be UTC with Z")
            if (not isinstance(gold.get("intents"), list) or len(gold["intents"]) != 1
                    or not isinstance(gold["intents"][0], str) or gold["intents"][0] not in _INTENTS):
                raise ValueError("This benchmark requires one valid gold intent label")
            if "requested_time_utc" not in gold or (gold["requested_time_utc"] is not None and not _utc(gold["requested_time_utc"])):
                raise ValueError("Gold requested time must be UTC with Z or null")
            if type(gold.get("needs_review")) is not bool:
                raise ValueError("Gold needs_review must be boolean")
            for field in ("rationale", "evidence_notes", "adjudication"):
                if field in gold and not isinstance(gold[field], str):
                    raise ValueError("Gold notes must be strings")
            required = gold.get("required_uncertain_fields", [])
            if not isinstance(required, list) or not all(isinstance(x, str) for x in required):
                raise ValueError("Required uncertainty fields must be a string list")
            if "use_llm" in context and type(context["use_llm"]) is not bool:
                raise ValueError("Context use_llm must be boolean")
            encoded_record = _canonical(record)
            # Conservative rejection also covers records whose differing contexts
            # reduce to the same production cache key. No duplicate-call race.
            if encoded_record in records:
                raise ValueError("Duplicate source records require a separate benchmark")
            _canonical(case)
            ids.add(case_id)
            records.add(encoded_record)
            cases.append(case)
    if not cases:
        raise ValueError("Input must contain at least one case")
    return cases


def _validate_config(config: BenchmarkConfig) -> None:
    if type(config.execute) is not bool or type(config.warm) is not bool:
        raise ValueError("Execute and warm flags must be booleans")
    if type(config.limit) is not int or config.limit < 1:
        raise ValueError("Limit must be a positive integer")
    if type(config.workers) is not int or config.workers not in (1, 6):
        raise ValueError("Workers must be 1 or 6")
    for value in (config.candidate, config.run_id):
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", value):
            raise ValueError("Candidate and run ID must be safe single path components")
    if not isinstance(config.revision, str) or not re.fullmatch(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64}|sha256:[0-9a-fA-F]{64})", config.revision):
        raise ValueError("Revision must be a full commit or SHA-256 weights identifier")
    endpoint = urlsplit(config.base_url)
    if endpoint.scheme not in {"http", "https"} or not endpoint.hostname:
        raise ValueError("Explicit HTTP(S) base URL required")
    endpoint.port  # Validate the configured port before any work.
    if not isinstance(config.model, str) or not config.model.strip():
        raise ValueError("Explicit model identifier required")
    if not math.isfinite(config.timeout_seconds) or not 0.1 <= config.timeout_seconds <= 60:
        raise ValueError("Timeout must be between 0.1 and 60 seconds")
    if Path(config.input_path).resolve() == Path(config.out).resolve():
        raise ValueError("Output cannot replace input")


def _public_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        if ":" in host:
            host = "[" + host + "]"
        if parsed.port is not None:
            host += ":" + str(parsed.port)
        return urlunsplit((parsed.scheme, host, parsed.path, "", ""))
    except ValueError:
        return "[REDACTED_URL]"


def _secrets(base_url: str) -> set[str]:
    values = {value for name, value in os.environ.items()
              if name.startswith(("LLM_", "OPENAI_", "OPENROUTER_", "CEREBRAS_"))
              and any(word in name for word in ("KEY", "TOKEN", "SECRET", "PASSWORD")) and value}
    parsed = urlsplit(base_url)
    values.update(x for x in (parsed.username, parsed.password) if x)
    values.update(unquote(x) for x in tuple(values))
    values.update(x.strip() for x in tuple(values) if x.strip())
    values.update(value for name, value in parse_qsl(parsed.query)
                  if value and re.search(r"key|token|secret|password", name, re.I))
    return values


def _scrub(value: Any, secrets: set[str]) -> Any:
    if isinstance(value, str):
        for secret in sorted(secrets, key=len, reverse=True):
            value = value.replace(secret, "[REDACTED]")
        return re.sub(r"https?://[^\s\"<>]+", lambda m: _public_url(m.group(0)), value)
    if isinstance(value, list):
        return [_scrub(x, secrets) for x in value]
    if isinstance(value, dict):
        return {key: _scrub(item, secrets) for key, item in value.items()}
    return value


def _grounded_details(details: Any, text: str) -> bool:
    if not isinstance(details, dict) or set(details) - {"approximate_time", "assistance_requested"}:
        return False
    if "approximate_time" in details and type(details["approximate_time"]) is not bool:
        return False
    if "assistance_requested" in details:
        help_text = details["assistance_requested"]
        quotes = help_text if isinstance(help_text, list) else [help_text]
        if not quotes or not all(isinstance(x, str) and bool(x) and x in text for x in quotes):
            return False
    return True


def _checks(intents: list[dict], gold: dict, text: str, meta: dict | None = None) -> dict:
    try:
        for item in intents:
            GuestIntentV1.model_validate(item, strict=True)
        schema_valid = bool(intents)
    except (ValueError, TypeError):
        schema_valid = False
    check = {
        "schema_valid": schema_valid,
        "intents_match": [x.get("intent") for x in intents] == gold["intents"],
        "requested_time_utc_match": len(intents) == 1 and intents[0].get("requested_time_utc") == gold["requested_time_utc"],
        "exact_evidence": bool(intents) and all(isinstance(x.get("evidence_quote"), str)
                            and bool(x["evidence_quote"]) and x["evidence_quote"] in text for x in intents),
        "exact_time_evidence": all(
            (x.get("requested_time_text") is None and x.get("requested_time_utc") is None)
            or (isinstance(x.get("requested_time_text"), str) and bool(x["requested_time_text"])
                and x["requested_time_text"] in text) for x in intents),
        "allowed_details": all(_grounded_details(x.get("details", {}), text) for x in intents),
    }
    if meta is not None:
        check["needs_review_match"] = (meta.get("needs_review") is gold["needs_review"]
                                       and all(x.get("needs_review", False) is gold["needs_review"] for x in intents))
        check["required_uncertainty_present"] = set(gold.get("required_uncertain_fields", [])) <= set(meta.get("uncertain_fields", []))
    check["passed"] = all(check.values())
    return check


def _raw_checks(raw: str | None, case: dict) -> dict | None:
    if raw is None:
        return None
    try:
        value = json.loads(raw, parse_constant=_invalid_constant)
        items = value["intents"]
        if set(value) != {"intents"} or not isinstance(items, list) or not all(isinstance(x, dict) for x in items):
            raise ValueError("Unexpected raw response shape")
        return _checks(items, case["gold"], case["record"]["text"])
    except (ValueError, KeyError, TypeError):
        return {"schema_valid": False, "passed": False}


def _run_cases(config: BenchmarkConfig, cases: list[dict], parser: Any) -> list[dict]:
    original_call, original_read = parser._call_llm, parser._read_cache

    def call(messages, settings):
        trace = _TRACE.get()
        if trace is None:
            raise RuntimeError("Unattributed parser call in benchmark process")
        trace["counts"]["transport_calls"] += 1
        start = time.perf_counter()
        try:
            reply = original_call(messages, settings)
            trace["raw_response"] = reply if isinstance(reply, str) else None
            trace["raw_response_sha256"] = _digest(reply) if isinstance(reply, str) else None
            return reply
        except Exception as exc:
            trace["error_types"].append(type(exc).__name__)
            raise
        finally:
            trace["timings"]["transport_seconds"] += time.perf_counter() - start

    def read(path):
        trace = _TRACE.get()
        if trace is None:
            raise RuntimeError("Unattributed cache read in benchmark process")
        trace["counts"]["cache_lookups"] += 1
        start = time.perf_counter()
        try:
            cached = original_read(path)
            trace["counts"]["cache_hits"] += int(cached is not None)
            return cached
        except Exception as exc:
            trace["error_types"].append(type(exc).__name__)
            raise
        finally:
            trace["timings"]["cache_seconds"] += time.perf_counter() - start

    def one(case):
        trace = {"case_id": case["case_id"], "raw_response": None, "raw_response_sha256": None,
                 "counts": {"transport_calls": 0, "cache_lookups": 0, "cache_hits": 0},
                 "timings": {"total_seconds": 0.0, "transport_seconds": 0.0, "cache_seconds": 0.0},
                 "error_types": []}
        token = _TRACE.set(trace)
        start = time.perf_counter()
        try:
            items, meta = parser.parse_request_full(copy.deepcopy(case["record"]), copy.deepcopy(case["context"]))
            meta = meta.model_dump(mode="json") if hasattr(meta, "model_dump") else dict(meta)
            items = [x.model_dump(mode="json") if hasattr(x, "model_dump") else x for x in items]
            method = meta.get("method")
            trace["path"] = ("live_llm" if trace["counts"]["transport_calls"] else "cache_llm") if method == "llm" else method
            trace["normalized"] = {"intents": items, "extraction": meta}
            trace["gold"] = copy.deepcopy(case["gold"])
            trace["checks"] = {"raw": _raw_checks(trace["raw_response"], case),
                               "normalized": _checks(items, case["gold"], case["record"]["text"], meta)}
            return trace
        except Exception as exc:
            trace["path"] = "error"
            trace["error_types"].append(type(exc).__name__)
            trace["normalized"] = {"intents": [], "extraction": None}
            trace["gold"] = copy.deepcopy(case["gold"])
            trace["checks"] = {"raw": _raw_checks(trace["raw_response"], case), "normalized": {"passed": False}}
            return trace
        finally:
            trace["timings"]["total_seconds"] = time.perf_counter() - start
            _TRACE.reset(token)

    with patch.object(parser, "_call_llm", call), patch.object(parser, "_read_cache", read):
        with ThreadPoolExecutor(max_workers=config.workers) as pool:
            return list(pool.map(one, cases))


def run_benchmark(config: BenchmarkConfig, *, parser_module: Any = None) -> dict:
    """Plan or run a validated bounded cohort; write one exclusively new report.

    LLM_API_KEY is read from the environment only. An injected parser module is
    supported for offline tests and is identified as such in the manifest.
    """
    _validate_config(config)
    cases = load_cases(config.input_path)
    selected = cases[:config.limit]  # All validation precedes every parser call.
    parser = parser_module or importlib.import_module("ingestion.guest_parser")
    cache_dir = (Path(config.cache_root) / config.candidate / config.run_id).resolve()
    out = Path(config.out)
    if out.resolve().is_relative_to(cache_dir):
        raise ValueError("Report must be outside the managed cache directory")
    secrets = _secrets(config.base_url)
    identity = {
        "candidate": config.candidate, "revision": config.revision, "run_id": config.run_id,
        "workers": config.workers, "model": config.model, "endpoint_sha256": _digest(config.base_url.rstrip("/")),
        "input_sha256": _digest(Path(config.input_path).read_bytes()), "selected_sha256": _digest(_canonical(selected)),
        "parser_version": parser.PARSER_VERSION, "parser_sha256": _digest(Path(parser.__file__).read_bytes()),
        "prompt_sha256": _digest(parser.SYSTEM_PROMPT), "schema_version": SCHEMA_VERSION,
        "schema_sha256": _digest(_canonical(GuestIntentV1.model_json_schema())),
        "timeout_seconds": config.timeout_seconds,
    }
    marker = cache_dir / _MARKER
    cache_identity = {"identity_sha256": _digest(_canonical(identity))}
    if config.warm:
        if not marker.is_file() or json.loads(marker.read_text(encoding="utf-8")) != cache_identity:
            raise ValueError("Warm cache identity does not match this run")
    elif cache_dir.exists() and (not cache_dir.is_dir() or any(cache_dir.iterdir())):
        raise ValueError("Fresh mode requires an absent or empty candidate/run cache directory")
    manifest = dict(identity, endpoint=_public_url(config.base_url), input_count=len(cases),
                    selected_count=len(selected), limit=config.limit, case_ids=[x["case_id"] for x in selected],
                    cache_dir=str(cache_dir), warm=config.warm,
                    transport_kind="injected_parser" if parser_module is not None else "configured_parser",
                    measurement="Original _call_llm invocations; not server-confirmed HTTP requests")
    summary = {"selected_count": len(selected), "completed_count": 0, "transport_calls": 0,
               "cache_lookups": 0, "cache_hits": 0, "live_llm": 0, "cache_llm": 0,
               "source_label": 0, "fallback": 0, "error": 0, "total_seconds": 0.0,
               "raw_predictions_count": 0, "raw_gold_pass_count": None, "host_gold_pass_count": None}
    # Until the whole cohort finishes, zero would falsely imply no work or cost.
    unknown_summary = {key: value if key == "selected_count" else None
                       for key, value in summary.items()}
    report = {"schema_version": "1.1", "mode": "execute" if config.execute else "plan",
              "status": "running" if config.execute else "planned",
              "manifest": manifest, "results": [],
              "summary": unknown_summary if config.execute else summary}
    api_key = os.environ.get("LLM_API_KEY", "").strip()
    if config.execute and not api_key:
        raise ValueError("Execution requires explicit LLM_API_KEY configuration")
    if not _RUN_LOCK.acquire(blocking=False):
        raise RuntimeError("Only one benchmark may run in this process")
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        # Exclusive creation rejects existing files, symlinks and hardlink aliases
        # before cache mutation or model calls. Keep the same open handle to write.
        handle = out.open("x", encoding="utf-8")
        original_error = None
        try:
            json.dump(_scrub(report, secrets), handle, ensure_ascii=False, indent=2)
            handle.flush()
            if config.execute:
                if not config.warm:
                    cache_dir.mkdir(parents=True, exist_ok=True)
                    with marker.open("x", encoding="utf-8") as metadata:
                        metadata.write(_canonical(cache_identity))
                environment = {"LLM_API_KEY": api_key, "LLM_BASE_URL": config.base_url,
                               "LLM_MODEL": config.model, "LLM_TIMEOUT_SECONDS": str(config.timeout_seconds),
                               "GUEST_PARSER_CACHE_DIR": str(cache_dir)}
                started = time.perf_counter()
                with patch.dict(os.environ, environment):
                    report["results"] = _run_cases(config, selected, parser)
                summary["total_seconds"] = time.perf_counter() - started
                summary["completed_count"] = len(report["results"])
                for result in report["results"]:
                    for key, count in result["counts"].items():
                        summary[key] += count
                    if result["path"] in {"live_llm", "cache_llm", "source_label", "fallback", "error"}:
                        summary[result["path"]] += 1
                predictions = [x for x in report["results"] if x["raw_response"] is not None]
                summary["raw_predictions_count"] = len(predictions)
                if predictions:
                    summary["raw_gold_pass_count"] = sum(x["checks"]["raw"]["passed"] for x in predictions)
                    summary["host_gold_pass_count"] = sum(x["path"] == "live_llm" and x["checks"]["normalized"]["passed"] for x in predictions)
                report["summary"] = summary
                report["status"] = "complete"
            sanitized = _scrub(report, secrets)
            for original, result in zip(report["results"], sanitized["results"]):
                result["raw_response_redacted"] = result["raw_response"] != original["raw_response"]
                result["normalized_redacted"] = result["normalized"] != original["normalized"]
            report = sanitized
            handle.seek(0)
            json.dump(report, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.truncate()
            handle.flush()
            return report
        except BaseException as exc:
            original_error = exc
            report["status"] = "failed" if isinstance(exc, Exception) else "interrupted"
            report["error_type"] = type(exc).__name__
            report["summary"] = unknown_summary
            report["results"] = []
            # Reporting must never replace the original interrupt/failure. The
            # initial running report already records unknown measurements if
            # this best-effort update cannot be written.
            try:
                handle.seek(0)
                json.dump(_scrub(report, secrets), handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.truncate()
                handle.flush()
            except BaseException:
                pass
            raise
        finally:
            try:
                handle.close()
            except BaseException:
                if original_error is None:
                    raise
    finally:
        _RUN_LOCK.release()


def main(argv: list[str] | None = None) -> int:
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--input", dest="input_path", required=True, type=Path)
    cli.add_argument("--limit", required=True, type=int)
    for flag in ("candidate", "revision", "run-id", "base-url", "model"):
        cli.add_argument("--" + flag, required=True)
    for flag in ("cache-root", "out"):
        cli.add_argument("--" + flag, required=True, type=Path)
    cli.add_argument("--workers", required=True, choices=(1, 6), type=int)
    cli.add_argument("--timeout-seconds", type=float, default=15.0)
    cli.add_argument("--execute", action="store_true", help="permit configured parser transport calls")
    cli.add_argument("--warm", action="store_true", help="require matching cache metadata from a prior execution")
    args = cli.parse_args(argv)
    try:
        report = run_benchmark(BenchmarkConfig(**vars(args)))
    except Exception as exc:
        print(f"Benchmark failed ({type(exc).__name__}).", file=sys.stderr)
        return 2
    print(f"{report['mode']}: {report['manifest']['selected_count']} selected cases; "
          f"{report['summary']['transport_calls']} parser transport calls.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
