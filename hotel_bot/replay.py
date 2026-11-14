"""Causal Quickstart/Dev replay jobs for the local Hotel presentation.

Only supplied, explicitly named datasets are accepted. Historical events are
constructed without eager inference; each guest message is parsed immediately
before the real DecisionEngine processes it. All non-guest events, decisions,
approval requirements and executor statuses retain their production meaning.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from itertools import groupby
import json
from pathlib import Path
import re
from threading import Event, RLock, Thread
from time import monotonic
from typing import Any
from uuid import uuid4

from common.contracts.schemas import DecisionRecordV2, ExtractionMetaV1, GuestIntentV1, NormalizedEventV1
from engine.core import instant, utc
from engine.decisions import write_decisions
from engine.output_artifacts import preflight_outputs, publish_artifacts
from engine.planner import DecisionEngine
from engine.validation import validate_outputs
from ingestion.events import source_label_intents
from ingestion.guest_parser import parse_request_full
from ingestion.loader import inspect_dataset, REQUIRED_FILES
from ingestion.replay import ReplayData

from .datasets import DatasetRegistry, IMPORTED_ID

DATASETS = {"quickstart": "PLATO40_v2_quickstart", "dev": "PLATO40_v2_dev"}
ARTIFACTS = {
    "decisions": "decisions.jsonl", "audit": "audit.jsonl", "journal": "journal.jsonl",
    "summary": "summary.json", "validation": "validation.json",
    "extractions": "extractions.jsonl", "rejections": "rejections.jsonl",
}
_ACTIVE = {"queued", "running", "finalizing"}
_TERMINAL = {"completed", "degraded", "failed", "cancelled"}
_JOB_ID = re.compile(r"replay-[0-9a-f]{32}\Z")
_MAX_ARTIFACT_BYTES = 256 * 1024 * 1024


class ReplayValidationError(ValueError):
    status_code = 400


class ReplayNotFoundError(ValueError):
    status_code = 404


class ReplayConflictError(ValueError):
    status_code = 409


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True)


def _source_hashes(root: Path) -> dict[str, str]:
    result = {}
    for name in REQUIRED_FILES:
        digest = sha256()
        with (root / name).open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        result[name] = digest.hexdigest()
    return result


def _strict_json(raw: bytes):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate artifact field")
            result[key] = value
        return result

    def invalid(value):
        raise ValueError("Non-finite artifact value")

    return json.loads(raw.decode("utf-8"), object_pairs_hook=pairs, parse_constant=invalid)


def _artifact_rows(path: Path, maximum: int) -> list[dict]:
    if path.stat().st_size > _MAX_ARTIFACT_BYTES:
        raise ValueError("Oversized replay artifact")
    result = []
    with path.open("rb") as handle:
        while raw := handle.readline(4_000_001):
            if len(raw) > 4_000_000 or not raw.endswith(b"\n") or len(result) >= maximum:
                raise ValueError("Invalid replay artifact size")
            row = _strict_json(raw)
            if not isinstance(row, dict):
                raise ValueError("Replay artifact rows must be objects")
            result.append(row)
    return result


@dataclass
class _Job:
    public: dict
    root: Path
    dataset_path: Path
    journal: Any
    cancel: Event = field(default_factory=Event)
    thread: Thread | None = None
    snapshot: dict = field(default_factory=dict)
    recent_messages: list[dict] = field(default_factory=list)


class ReplayManager:
    """Own at most one active replay and its complete local output artifacts."""

    def __init__(self, dataset_root: str | Path, output_root: str | Path,
                 parser=parse_request_full, *, import_root: str | Path | None = None):
        self.dataset_root = Path(dataset_root).expanduser().resolve()
        self.output_root = Path(output_root).expanduser().resolve()
        if self.output_root.is_relative_to(self.dataset_root):
            raise ReplayValidationError("Replay outputs must be outside input datasets")
        self._parser = parser
        self._lock = RLock()
        self._jobs: dict[str, _Job] = {}
        self._active: str | None = None
        self._closed = False
        self.imports = DatasetRegistry(import_root if import_root is not None
                                       else self.output_root.parent / "imported-datasets")
        if (self.imports.root.is_relative_to(self.dataset_root) or self.dataset_root.is_relative_to(self.imports.root)
                or self.output_root.is_relative_to(self.imports.root) or self.imports.root.is_relative_to(self.output_root)):
            raise ReplayValidationError("Imported datasets need a separate folder outside original data and replay outputs")
        self._playbacks: dict[str, Any] = {}
        self._archive_catalog: dict[str, str] | None = None

    def _archived_replays(self):
        """Discover confined completed summaries; opening validates full output."""
        with self._lock:
            if self._archive_catalog is None:
                found = {}
                for root in self.output_root.glob("replay-*"):
                    if not _JOB_ID.fullmatch(root.name) or root.is_symlink() or not root.is_dir():
                        continue
                    path = root / ARTIFACTS["summary"]
                    try:
                        if path.is_symlink() or not path.is_file() or path.stat().st_size > 8 * 1024 * 1024:
                            continue
                        row = _strict_json(path.read_bytes())
                        dataset = row.get("dataset_id")
                        if (not self._dataset_id(dataset) or row.get("id") != root.name or row.get("complete") is not True
                                or row.get("status") not in {"completed", "degraded"}):
                            continue
                        choice = (instant(row["finished_at"]), root.name)
                        if dataset not in found or choice > found[dataset]:
                            found[dataset] = choice
                    except (OSError, ValueError, KeyError, TypeError, AttributeError):
                        continue
                self._archive_catalog = {dataset: value[1] for dataset, value in found.items()}
            return dict(self._archive_catalog)

    def _playback(self, job_id: str):
        with self._lock:
            job = self._job(job_id)
            if job_id not in self._playbacks:
                from .playback import PlaybackTimeline
                timeline = PlaybackTimeline(job)
                # Bound resident source indexes when several archives are used.
                if len(self._playbacks) >= 2:
                    del self._playbacks[next(iter(self._playbacks))]
                self._playbacks[job_id] = timeline
            return self._playbacks[job_id]

    def playback_manifest(self, job_id: str) -> dict:
        return self._playback(job_id).manifest()

    def playback_frame(self, job_id: str, *, cursor=None, at=None) -> dict:
        return self._playback(job_id).frame(cursor=cursor, at=at)

    def timeline(self, job_id: str, after=0, limit=1000) -> dict:
        return self._playback(job_id).timeline(after=after, limit=limit)

    @staticmethod
    def _dataset_id(dataset_id):
        return isinstance(dataset_id, str) and (dataset_id in DATASETS or IMPORTED_ID.fullmatch(dataset_id) is not None)

    def import_dataset(self, stream, length, *, cancelled=lambda: False):
        with self._lock:
            if self._closed:
                raise ReplayConflictError("Replay manager is shutting down")
        result = self.imports.import_zip(stream, length, cancelled=lambda: self._closed or cancelled())
        result["dataset"]["replay_id"] = self._archived_replays().get(result["dataset"]["id"])
        return result

    def _dataset(self, dataset_id: str) -> Path:
        if not self._dataset_id(dataset_id):
            raise ReplayValidationError("Choose a dataset from the list")
        if dataset_id not in DATASETS:
            return self.imports.resolve(dataset_id)
        path = (self.dataset_root / DATASETS[dataset_id]).resolve()
        if not path.is_relative_to(self.dataset_root) or not path.is_dir():
            raise ReplayNotFoundError("The selected dataset is unavailable")
        return path

    def list_datasets(self) -> list[dict]:
        result = []
        archives = self._archived_replays()
        for dataset_id, folder in DATASETS.items():
            row = {"id": dataset_id, "name": "Quickstart" if dataset_id == "quickstart" else "Dev",
                   "folder": folder, "available": False, "synthetic": True,
                   "replay_id": archives.get(dataset_id)}
            try:
                metadata = inspect_dataset(self._dataset(dataset_id))
                row.update({"available": True, **metadata})
            except (OSError, ValueError, KeyError, TypeError):
                row["error"] = "Dataset files are missing or invalid"
            result.append(row)
        for row in self.imports.list_datasets():
            row["replay_id"] = archives.get(row["id"])
            result.append(row)
        return result

    def start(self, dataset_id: str) -> dict:
        dataset = self._dataset(dataset_id)
        try:
            metadata = inspect_dataset(dataset)
        except (OSError, ValueError, KeyError, TypeError):
            raise ReplayValidationError("Dataset files are missing or invalid") from None
        with self._lock:
            if self._closed:
                raise ReplayConflictError("Replay manager is shutting down")
            if self._active is not None and self._jobs[self._active].public["status"] in _ACTIVE:
                raise ReplayConflictError("Another replay is already running")
            from .journal import SessionJournal

            identifier = "replay-" + uuid4().hex
            root = self.output_root / identifier
            paths = [root / filename for filename in ARTIFACTS.values()]
            preflight_outputs(paths, dataset)
            journal = SessionJournal(path=root / ARTIFACTS["journal"])
            public = {
                "id": identifier, "dataset_id": dataset_id, "status": "queued",
                "started_at": _now(), "finished_at": None, "dataset": metadata,
                "progress": {"events_processed": 0, "total_events": 0, "guest_processed": 0,
                    "total_guests": 0, "decisions": 0, "audit_rows": 0, "as_of_utc": None,
                    "llm_count": 0, "fallback_count": 0, "source_label_count": 0,
                    "parser_errors": 0, "source_errors": 0, "needs_review_count": 0,
                    "autonomous_count": 0, "pending_approval_count": 0, "safety_rejections": 0},
                "validation": None, "error": None, "artifacts": {},
                "complete": False, "effects_applied": False, "observed_history_changed": False,
                "accuracy_measured": False,
                "llm_count_includes_cache": True, "fresh_api_calls_measured": False,
                "pending_approval_count_scope": "emitted_pending_audits",
                "approval_mode": "historical_read_only",
            }
            job = _Job(public, root, dataset, journal)
            try:
                journal.append("replay_started", "Historical replay started",
                    "The actual DecisionEngine will process one causal event stream; only guest messages use the configured parser.",
                    status="queued", details={"dataset_id": dataset_id, "dataset": metadata})
                job.thread = Thread(target=self._run, args=(job,), name=identifier, daemon=True)
                job.thread.start()
            except Exception:
                job.cancel.set()
                try:
                    journal.append("replay_failed", "Replay could not start", "No replay worker was committed; another run may be started.", status="failed")
                except Exception:
                    pass
                raise
            # The worker first acquires this same lock. Register only after both
            # durable startup logging and Thread.start have succeeded.
            self._jobs[identifier] = job
            self._active = identifier
            return deepcopy(public)

    def _job(self, job_id: str) -> _Job:
        if not isinstance(job_id, str) or not _JOB_ID.fullmatch(job_id):
            raise ReplayNotFoundError("Replay was not found")
        if job_id not in self._jobs:
            self._jobs[job_id] = self._restore(job_id)
        return self._jobs[job_id]

    def _restore(self, job_id: str) -> _Job:
        """Restore a committed archive without a parser call or engine replay."""
        from .journal import SessionJournal

        root = self.output_root / job_id
        if root.is_symlink() or not root.is_dir() or root.resolve().parent != self.output_root:
            raise ReplayNotFoundError("Replay was not found")
        paths = {kind: root / name for kind, name in ARTIFACTS.items()}
        try:
            for path in paths.values():
                if path.is_symlink() or not path.is_file() or path.resolve().parent != root.resolve():
                    raise ValueError("Missing or aliased replay artifact")
                if path.stat().st_size > _MAX_ARTIFACT_BYTES:
                    raise ValueError("Oversized replay artifact")
            if paths["summary"].stat().st_size > 8 * 1024 * 1024 or paths["validation"].stat().st_size > 8 * 1024 * 1024:
                raise ValueError("Oversized replay metadata")
            summary_bytes = paths["summary"].read_bytes()
            summary = _strict_json(summary_bytes)
            validation = _strict_json(paths["validation"].read_bytes())
            if (not isinstance(summary, dict) or summary.get("id") != job_id
                    or not self._dataset_id(summary.get("dataset_id")) or summary.get("status") not in _TERMINAL
                    or summary.get("effects_applied") is not False or summary.get("observed_history_changed") is not False
                    or summary.get("accuracy_measured") is not False or not isinstance(validation, dict)):
                raise ValueError("Invalid replay metadata")
            progress = summary["progress"]
            if not isinstance(progress, dict):
                raise ValueError("Invalid replay progress")
            for key in ("decisions", "audit_rows", "guest_processed", "events_processed", "total_events", "total_guests"):
                if type(progress.get(key)) is not int or not 0 <= progress[key] <= 200_000:
                    raise ValueError("Invalid replay counts")
            instant(summary["started_at"])
            instant(summary["finished_at"])
            if progress.get("as_of_utc") is not None:
                instant(progress["as_of_utc"])
            expected_hashes = summary.get("input_hashes_sha256")
            if (not isinstance(expected_hashes, dict) or set(expected_hashes) != set(REQUIRED_FILES)
                    or any(not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value) for value in expected_hashes.values())):
                raise ValueError("Missing input provenance")
            records = [DecisionRecordV2.model_validate(row) for row in _artifact_rows(paths["decisions"], progress["decisions"])]
            audits = _artifact_rows(paths["audit"], progress["audit_rows"])
            extractions = _artifact_rows(paths["extractions"], progress["guest_processed"])
            if (len(records) != progress["decisions"] or len(audits) != progress["audit_rows"]
                    or len(extractions) != progress["guest_processed"]):
                raise ValueError("Replay artifact counts differ")
            by_id = {record.event_id: record for record in records}
            audit_by_id = {row["decision_id"]: row for row in audits}
            if len(by_id) != len(records) or len(audit_by_id) != len(audits) or set(by_id) != set(audit_by_id):
                raise ValueError("Replay decisions and audits do not join")
            for row in audits:
                if row.get("effects_applied") is not False or row.get("observed_history_changed") is not False or row.get("simulated") is not True:
                    raise ValueError("Replay audit claims physical effects")
            for kind in ("decisions", "audit"):
                expected = validation.get("hashes_sha256", {}).get(kind)
                if expected is not None and sha256(paths[kind].read_bytes()).hexdigest() != expected:
                    raise ValueError("Replay output checksum differs")
            journal = SessionJournal(path=paths["journal"])
            entries, cursor = [], 0
            while cursor < journal.total:
                page = journal.read(after=cursor, limit=500)
                if not page["entries"] or len(entries) + len(page["entries"]) > 500_000:
                    raise ValueError("Invalid replay journal size")
                entries.extend(page["entries"])
                cursor = page["next_cursor"]
            terminals = [entry for entry in entries if entry["kind"] in {"replay_finished", "replay_failed"}]
            if not terminals:
                raise ValueError("Replay output lacks a durable terminal marker")
            terminal = terminals[-1]
            starts = [entry for entry in entries if entry["kind"] == "replay_started"]
            if len(starts) != 1 or starts[0]["details"].get("dataset_id") != summary["dataset_id"]:
                raise ValueError("Replay dataset provenance differs")
            if terminal["kind"] == "replay_failed":
                # A failed final commit cannot become a success merely because
                # a preceding summary rename left a stale completed file.
                summary.update(status="failed", complete=False,
                    error={"code": "artifact_publication_failed", "message": "Archived replay finalization failed."})
            elif terminal["status"] != summary["status"]:
                raise ValueError("Replay terminal states differ")
            if terminal["kind"] == "replay_finished" and terminal["details"].get("progress") != progress:
                raise ValueError("Replay terminal counters differ")
            terminal_hash = terminal["details"].get("summary_sha256")
            if terminal_hash is not None and sha256(summary_bytes).hexdigest() != terminal_hash:
                raise ValueError("Replay commit marker checksum differs")
            logged = [entry for entry in entries if entry["kind"] == "engine_decision"]
            logged_ids = []
            for entry in logged:
                record = DecisionRecordV2.model_validate(entry["details"]["decision_record"])
                if (record.event_id not in by_id or record != by_id[record.event_id]
                        or entry["details"]["audit"] != audit_by_id[record.event_id]):
                    raise ValueError("Replay decision journal differs")
                logged_ids.append(record.event_id)
            if len(logged_ids) != len(set(logged_ids)):
                raise ValueError("Duplicate replay decision log")
            if summary["status"] in {"completed", "degraded"} and set(logged_ids) != set(by_id):
                raise ValueError("Incomplete replay decision journal")
            if summary["status"] in {"completed", "degraded"}:
                guest_logs = [entry["details"] for entry in entries if entry["kind"] == "guest_extraction"]
                if guest_logs != extractions or summary.get("complete") is not True or progress["events_processed"] != progress["total_events"]:
                    raise ValueError("Incomplete replay extraction journal")
            for row in extractions:
                NormalizedEventV1.model_validate(row["normalized_event"])
            public_keys = {
                "id", "dataset_id", "status", "started_at", "finished_at", "dataset", "progress", "validation",
                "error", "complete", "effects_applied", "observed_history_changed", "accuracy_measured",
                "llm_count_includes_cache", "fresh_api_calls_measured", "pending_approval_count_scope", "approval_mode",
                "input_hashes_sha256", "changed_input_files", "cancel_requested", "autonomous_count", "pending_approval_count",
                "ingestion_audit", "notifications_suppressed",
            }
            public = {key: deepcopy(value) for key, value in summary.items() if key in public_keys}
            public.update(restored=True, restored_at=_now(),
                          artifacts={kind: f"/api/replays/{job_id}/artifacts/{kind}" for kind in ARTIFACTS})
            dataset_path = (self.dataset_root / DATASETS[summary["dataset_id"]] if summary["dataset_id"] in DATASETS
                            else self.imports.location(summary["dataset_id"]))
            job = _Job(public, root, dataset_path, journal)
            for row in extractions[-10:]:
                event = row["normalized_event"]
                job.recent_messages.append({"id": event["source"]["record_id"], "room_id": event["entities"].get("room_id"),
                    "text": row["source_record"].get("text"), "language": row["source_record"].get("language"),
                    "created_at": event["timestamp_utc"], "intents": deepcopy(event["payload"]["intents"]),
                    "needs_review": event["extraction"]["needs_review"],
                    "status": "needs_review" if event["extraction"]["needs_review"] else "processed",
                    "extraction_method": event["extraction"]["method"], "normalized_event": deepcopy(event)})
        except (OSError, ValueError, KeyError, TypeError, RecursionError):
            raise ReplayValidationError("Archived replay files are incomplete or inconsistent") from None
        try:
            dataset_path = self._dataset(summary["dataset_id"])
            unchanged = _source_hashes(dataset_path) == expected_hashes
        except (OSError, ValueError):
            unchanged = False
        job.public["source_unchanged"] = unchanged
        if unchanged and progress.get("as_of_utc"):
            try:
                # No iter_events, parser, or DecisionEngine invocation here.
                repo = ReplayData(dataset_path, use_llm=False)
                self._snapshot(job, repo, progress["as_of_utc"], records, audits)
                job.public["view_reconstructed_without_inference"] = True
            except Exception:
                job.snapshot = {}
                job.public["view_reconstructed_without_inference"] = False
                job.public["error"] = {"code": "archived_view_unavailable", "message": "The archived log is available, but the historical view could not be reconstructed."}
        elif not unchanged:
            job.snapshot = {}
            job.public["view_reconstructed_without_inference"] = False
            job.public["error"] = {"code": "source_changed_since_replay", "message": "The source dataset has changed or is unavailable. Archived logs remain available; the historical view is hidden."}
        return job

    def status(self, job_id: str, after: int = 0, limit: int = 200) -> dict:
        if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 200:
            raise ReplayValidationError("Invalid replay log cursor or page size")
        with self._lock:
            job = self._job(job_id)
            public, snapshot = deepcopy(job.public), deepcopy(job.snapshot)
        page = job.journal.read(after=after, limit=limit)
        return {"job": public, "snapshot": snapshot, "timeline": page["entries"],
                "next_cursor": page["next_cursor"], "total": page["total"],
                "has_more": page["next_cursor"] < page["total"]}

    def cancel(self, job_id: str) -> dict:
        with self._lock:
            job = self._job(job_id)
            if job.public["status"] in _ACTIVE:
                job.cancel.set()
                job.public["cancel_requested"] = True
            return deepcopy(job.public)

    def close(self, wait: bool = True, timeout: float = 65) -> bool:
        with self._lock:
            self._closed = True
            jobs = [job for job in self._jobs.values() if job.thread and job.thread.is_alive()]
            for job in jobs:
                job.cancel.set()
                job.public["cancel_requested"] = True
        # A request already in progress uses its captured credentials. No new
        # model call can begin after cancellation is observed at an event edge.
        if wait:
            for job in jobs:
                job.thread.join(timeout=max(0, timeout))
        return all(not job.thread.is_alive() for job in jobs)

    def artifact(self, job_id: str, kind: str) -> Path:
        with self._lock:
            job = self._job(job_id)
            if not isinstance(kind, str) or kind not in ARTIFACTS:
                raise ReplayNotFoundError("Replay artifact was not found")
            if job.public["status"] in _ACTIVE:
                raise ReplayConflictError("Replay artifacts become available when the run stops")
            path = job.root / ARTIFACTS[kind]
            if not path.is_file() or path.is_symlink() or path.resolve().parent != job.root.resolve():
                raise ReplayNotFoundError("Replay artifact is not ready")
            return path

    def _update(self, job: _Job, *, progress: dict | None = None, **fields):
        with self._lock:
            if fields.get("status") in _TERMINAL:
                self._archive_catalog = None
            if progress:
                job.public["progress"].update(progress)
            job.public.update(fields)

    def _guest(self, job: _Job, repo: ReplayData, event: NormalizedEventV1, sources: dict) -> tuple[NormalizedEventV1, dict]:
        original = sources.get(event.source.record_id)
        source_error = original is None
        record = deepcopy(original) if original is not None else {
            "request_id": event.source.record_id, "timestamp_utc": event.timestamp_utc,
            "request_type": event.payload.get("request_type"), "text": event.payload.get("original_text"),
        }
        now = event.available_at_utc
        booking = repo.get_booking(record["booking_id"], now) if record.get("booking_id") else None
        context = {"booking": booking, "timezone": repo.ds.local_tz_name, "as_of_utc": now,
                   "room_ids": repo.ds.room_ids, "use_llm": True}
        parser_error = False
        try:
            if source_error:
                raise ValueError("Missing source record")
            items, meta = self._parser(deepcopy(record), deepcopy(context))
            if not isinstance(items, list) or not items:
                raise ValueError("Missing parsed intentions")
            items = [GuestIntentV1.model_validate(item.model_dump() if isinstance(item, GuestIntentV1) else item,
                                                strict=True).model_dump(mode="json") for item in items]
            meta = ExtractionMetaV1.model_validate(meta.model_dump() if isinstance(meta, ExtractionMetaV1) else meta)
        except Exception:
            parser_error = True
            items, meta = source_label_intents(record, method="fallback")
        label = record.get("request_type")
        if meta.method == "llm" and label and label not in {item["intent"] for item in items}:
            meta = meta.model_copy(update={"needs_review": True,
                "uncertain_fields": sorted(set(meta.uncertain_fields) | {"intent"})})
        copied = event.model_copy(deep=True)
        copied.payload["intents"] = deepcopy(items)
        copied.extraction = meta
        copied = NormalizedEventV1.model_validate(copied.model_dump())
        row = {"source_record": record, "context": context, "normalized_event": copied.model_dump(mode="json"),
               "parser_error": parser_error, "source_error": source_error}
        return copied, row

    def _snapshot(self, job: _Job, repo: ReplayData, now: str, decisions: list, audits: list):
        rooms = []
        definitions = {r["id"]: r for r in repo.ds.building.get("building", {}).get("rooms", []) if r.get("id")}
        for room_id in repo.ds.room_ids:
            state = deepcopy(repo.get_room_state(room_id, now) or {})
            occupied = state.get("occupancy", "unknown")
            cleaning = state.get("housekeeping_state")
            status = ("occupied" if occupied == "occupied" else "cleaning" if cleaning in {"cleaning", "in_progress"}
                      else "attention" if cleaning == "dirty" else "vacant" if occupied == "vacant" else "unknown")
            definition = definitions.get(room_id, {})
            rooms.append({**state, "id": room_id, "floor": definition.get("floor"), "status": status,
                          "fixture": False, "source": "historical_dataset", "release_confirmed": False})
        end = utc(instant(now) + timedelta(seconds=1))
        slots = repo.get_staff_slots(now, end, now)
        people = []
        for slot in slots:
            if slot.get("simulated_assumption"):
                continue
            if not slot.get("start_utc") or not slot.get("end_utc"):
                continue
            if not instant(slot["start_utc"]) <= instant(now) < instant(slot["end_utc"]):
                continue
            people.append({"id": slot.get("resource_id"), "role": slot.get("role"),
                           "status": slot.get("effective_status_asof", "unknown"),
                           "fixture": False, "source": "staff_roster.csv", "shift": deepcopy(slot),
                           "position_observed": False})
        by_id = {row["decision_id"]: row for row in audits}
        cancelled = {row.get("parameters", {}).get("cancelled_decision_id") for row in audits}
        pending = [{"decision": d.model_dump(mode="json"), "audit": deepcopy(by_id.get(d.event_id))}
                   for d in decisions if d.requires_human_approval
                   and d.event_id not in cancelled
                   and (not by_id.get(d.event_id, {}).get("parameters", {}).get("end_utc")
                        or instant(by_id[d.event_id]["parameters"]["end_utc"]) > instant(now))
                   and str(by_id.get(d.event_id, {}).get("status", "")).startswith("pending_")]
        recent_decisions = [{"decision": d.model_dump(mode="json"), "audit": deepcopy(by_id.get(d.event_id)),
                             "autonomous": not d.requires_human_approval and not d.requires_guest_consent}
                            for d in decisions[-20:]]
        view = {"source": "historical_dataset", "dataset_id": job.public["dataset_id"], "as_of_utc": now,
                "synthetic": True, "effects_applied": False, "rooms": rooms, "people": people,
                "pending_approvals": pending[-200:], "pending_approvals_total": len(pending),
                "pending_approvals_truncated": len(pending) > 200,
                "decisions": recent_decisions, "approval_mode": "historical_read_only"}
        with self._lock:
            view["recent_messages"] = deepcopy([message for message in job.recent_messages
                                                if instant(message["created_at"]) <= instant(now)])
            job.snapshot = view

    def _run(self, job: _Job):
        records, extraction_rows = [], []
        engine = repo = None
        failure = None
        last_snapshot = 0.0
        input_hashes = None
        counts = deepcopy(job.public["progress"])
        try:
            self._update(job, status="running")
            input_hashes = _source_hashes(job.dataset_path)
            self._update(job, input_hashes_sha256=input_hashes)
            repo = ReplayData(job.dataset_path, use_llm=False)
            engine = DecisionEngine(repo, repo.ds.building, enable_guest_services=True)
            sources = {row["request_id"]: row for row in repo.ds.guest_requests}
            # Use exactly DecisionEngine.run's UTC comparator and clock-last tie
            # ordering. No source or periodic event is turned into a model call.
            events = list(repo.iter_events())
            counts.update(total_events=len(events), total_guests=sum(e.event_type == "guest_request" for e in events))
            self._update(job, progress=counts)
            for _, same_time in groupby(events, key=lambda e: instant(e.available_at_utc)):
                for original in sorted(same_time, key=lambda e: (e.event_type == "clock_tick", e.event_id)):
                    if job.cancel.is_set():
                        break
                    event = original.model_copy(deep=True)
                    if event.event_type == "guest_request":
                        event, extraction = self._guest(job, repo, event, sources)
                        extraction_rows.append(extraction)
                        counts["guest_processed"] += 1
                        method = event.extraction.method
                        if method in {"llm", "fallback", "source_label"}:
                            counts[method + "_count"] += 1
                        else:
                            counts["source_errors"] += 1
                        counts["parser_errors"] += int(extraction["parser_error"])
                        counts["source_errors"] += int(extraction["source_error"])
                        counts["needs_review_count"] += int(event.extraction.needs_review)
                        with self._lock:
                            job.recent_messages.append({"id": event.source.record_id,
                                "room_id": event.entities.room_id, "text": extraction["source_record"].get("text"),
                                "language": extraction["source_record"].get("language"),
                                "created_at": event.timestamp_utc, "intents": deepcopy(event.payload["intents"]),
                                "needs_review": event.extraction.needs_review,
                                "status": "needs_review" if event.extraction.needs_review else "processed",
                                "extraction_method": method,
                                "normalized_event": deepcopy(extraction["normalized_event"])})
                            job.recent_messages = job.recent_messages[-10:]
                        job.journal.append("guest_extraction", "Guest request interpreted" if method == "llm" else "Guest request kept for review",
                            "Extracted guest intentions are evidence for the engine; they do not authorize an action." if method == "llm"
                            else "Model extraction was unavailable. The source label remains unverified and requires review.",
                            timestamp=event.available_at_utc, room_id=event.entities.room_id,
                            request_id=event.source.record_id, status=method,
                            evidence=[f"guest_requests.csv:{event.source.record_id}"], details=extraction)
                    audit_before = len(engine.executor.audit)
                    rejection_before = len(engine.rejections)
                    produced = engine.process(event)
                    records.extend(produced)
                    fresh_audits = engine.executor.audit[audit_before:]
                    by_id = {row["decision_id"]: row for row in fresh_audits}
                    for decision in produced:
                        audit = by_id.get(decision.event_id)
                        if audit is None:
                            counts["source_errors"] += 1
                        status = audit.get("status", "missing_audit") if audit else "missing_audit"
                        counts["autonomous_count"] += int(not decision.requires_human_approval and not decision.requires_guest_consent)
                        counts["pending_approval_count"] += int(decision.requires_human_approval and status.startswith("pending_"))
                        job.journal.append("engine_decision", decision.action, decision.reason,
                            timestamp=decision.timestamp_utc, action=decision.action, status=status,
                            room_id=next((r for r in decision.affected_assets if r in repo.ds.room_ids), None),
                            evidence=decision.evidence, details={
                                "decision_record": decision.model_dump(mode="json"), "audit": deepcopy(audit),
                                "trigger": {"event_id": event.event_id, "event_type": event.event_type,
                                            "source": event.source.model_dump(mode="json"), "available_at_utc": event.available_at_utc}})
                    for rejection in engine.rejections[rejection_before:]:
                        job.journal.append("safety_rejection", "Engine proposal rejected", rejection.get("reason", "Safety validation rejected the proposal"),
                            timestamp=event.available_at_utc, status="rejected", details=deepcopy(rejection))
                    counts.update(events_processed=engine.event_count, decisions=len(records),
                                  audit_rows=len(engine.executor.audit), safety_rejections=len(engine.rejections),
                                  as_of_utc=event.available_at_utc)
                    self._update(job, progress=counts)
                    # Publish views after guest/decision events and regular
                    # progress intervals, without recomputing 40 rooms per tick.
                    if monotonic() - last_snapshot >= 0.5:
                        self._snapshot(job, repo, event.available_at_utc, records, engine.executor.audit)
                        last_snapshot = monotonic()
                if job.cancel.is_set():
                    break
            if counts["as_of_utc"]:
                self._snapshot(job, repo, counts["as_of_utc"], records, engine.executor.audit)
        except Exception as exc:
            failure = type(exc).__name__
        if repo is not None:
            try:
                counts["source_errors"] += sum(
                    int(value) if isinstance(value, (int, bool)) else 1
                    for key, value in repo.audit.items()
                    if any(marker in key for marker in (":unparseable_", ":invalid_", ":empty_no_header", ":missing"))
                )
                counts["source_errors"] += sum(not row.get("timestamp_utc") for row in repo.ds.guest_requests)
            except Exception:
                counts["source_errors"] += 1
        if input_hashes is not None:
            try:
                changed = [name for name, digest in _source_hashes(job.dataset_path).items() if input_hashes[name] != digest]
                if changed:
                    failure = "SourceDataChanged"
                    counts["source_errors"] += len(changed)
                    self._update(job, changed_input_files=changed)
            except Exception:
                failure = failure or "SourceDataUnavailable"
                counts["source_errors"] += 1
        self._finish(job, repo, engine, records, extraction_rows, counts, failure)

    def _reconcile_counts(self, job, repo, engine, records, extraction_rows, counts):
        """Derive terminal counts from committed engine state, even after log I/O failure."""
        audits = engine.executor.audit if engine is not None else []
        by_id = {row["decision_id"]: row for row in audits}
        methods = [row["normalized_event"]["extraction"]["method"] for row in extraction_rows]
        source_errors = sum(bool(row["source_error"]) for row in extraction_rows)
        source_errors += sum(method not in {"llm", "fallback", "source_label"} for method in methods)
        source_errors += sum(record.event_id not in by_id for record in records)
        source_errors += len(job.public.get("changed_input_files", []))
        if repo is not None:
            try:
                source_errors += sum(int(value) if isinstance(value, (int, bool)) else 1
                    for key, value in repo.audit.items()
                    if any(marker in key for marker in (":unparseable_", ":invalid_", ":empty_no_header", ":missing")))
                source_errors += sum(not row.get("timestamp_utc") for row in repo.ds.guest_requests)
            except Exception:
                source_errors += 1
        counts.update(events_processed=engine.event_count if engine else 0, decisions=len(records),
            audit_rows=len(audits), guest_processed=len(extraction_rows), llm_count=methods.count("llm"),
            fallback_count=methods.count("fallback"), source_label_count=methods.count("source_label"),
            parser_errors=sum(bool(row["parser_error"]) for row in extraction_rows),
            source_errors=max(source_errors, counts["source_errors"]),
            needs_review_count=sum(bool(row["normalized_event"]["extraction"]["needs_review"]) for row in extraction_rows),
            autonomous_count=sum(not record.requires_human_approval and not record.requires_guest_consent for record in records),
            pending_approval_count=sum(record.requires_human_approval and str(by_id.get(record.event_id, {}).get("status", "")).startswith("pending_") for record in records),
            safety_rejections=len(engine.rejections) if engine else 0,
            as_of_utc=utc(engine._last_order) if engine and engine._last_order is not None else None)

    def _finish(self, job, repo, engine, records, extraction_rows, counts, failure):
        self._reconcile_counts(job, repo, engine, records, extraction_rows, counts)
        self._update(job, status="finalizing", progress=counts)
        audits = engine.executor.audit if engine is not None else []
        rejections = engine.rejections if engine is not None else []
        paths = {kind: job.root / filename for kind, filename in ARTIFACTS.items()}
        validation = None
        try:
            if repo is not None and counts["as_of_utc"]:
                self._snapshot(job, repo, counts["as_of_utc"], records, audits)
            artifacts = {paths["decisions"]: lambda path: write_decisions(records, path)}
            for kind, rows in (("audit", audits), ("extractions", extraction_rows), ("rejections", rejections)):
                text = "".join(_json(row) + "\n" for row in rows)
                artifacts[paths[kind]] = lambda path, text=text: path.write_text(text, encoding="utf-8")
            publish_artifacts(artifacts)
            try:
                validation = validate_outputs(job.dataset_path, paths["decisions"], paths["audit"])
            except Exception as exc:
                validation = {"valid": False, "error_count": 1, "issue_counts": {"validation_error": 1},
                              "error_type": type(exc).__name__}
            complete = not failure and not job.cancel.is_set() and counts["events_processed"] == counts["total_events"]
            degraded = any(counts[k] for k in ("fallback_count", "source_label_count", "parser_errors", "source_errors")) or not validation["valid"]
            status = "failed" if failure else "cancelled" if job.cancel.is_set() else "degraded" if degraded else "completed"
            error = {"code": "replay_failed", "message": "Replay stopped because input or processing failed.", "type": failure} if failure else None
            final = {"status": status, "finished_at": _now(), "complete": complete, "error": error,
                     "validation": {"valid": validation["valid"], "error_count": validation.get("error_count", 0),
                                    "issue_counts": validation.get("issue_counts", {})},
                     "artifacts": {kind: f"/api/replays/{job.public['id']}/artifacts/{kind}" for kind in ARTIFACTS}}
            with self._lock:
                summary = {**deepcopy(job.public), **final, "progress": deepcopy(counts),
                           "ingestion_audit": repo.audit if repo else {},
                           "notifications_suppressed": engine.notifications.suppressed if engine else 0,
                           "autonomous_count": counts["autonomous_count"], "pending_approval_count": counts["pending_approval_count"],
                           "snapshot": deepcopy(job.snapshot)}
            summary_text = _json(summary) + "\n"
            publish_artifacts({paths["summary"]: lambda path: path.write_text(summary_text, encoding="utf-8"),
                               paths["validation"]: lambda path: path.write_text(_json(validation) + "\n", encoding="utf-8")})
            # This durable record is the commit marker, written only after both
            # terminal artifacts were published. Restore checks its exact hash.
            job.journal.append("replay_finished", "Historical replay finished",
                "These are recorded simulated engine decisions, not changed observations or measured model accuracy.",
                status=status, details={"progress": counts, "complete": complete, "validation": final["validation"],
                                       "error": error, "summary_sha256": sha256(summary_text.encode("utf-8")).hexdigest()})
            self._update(job, **final)
        except Exception as exc:
            failed = {"status": "failed", "finished_at": _now(), "complete": False,
                      "error": {"code": "artifact_publication_failed", "message": "Replay output could not be finalized.", "type": type(exc).__name__}}
            persisted = False
            try:
                job.journal.append("replay_failed", "Replay finalization failed",
                    "The output generation did not commit successfully. No completed replay is claimed.",
                    status="failed", details={"progress": counts, "complete": False, "error": failed["error"]})
                persisted = True
            except Exception:
                pass  # A failed disk cannot guarantee another durable write.
            failed["failure_log_persisted"] = persisted
            # Best effort: if only the previous publication failed, retain a
            # truthful failed summary. A stale success file is never trusted by
            # restore without a matching successful journal commit marker.
            try:
                with self._lock:
                    summary = {**deepcopy(job.public), **failed, "progress": deepcopy(counts),
                               "snapshot": deepcopy(job.snapshot), "autonomous_count": counts["autonomous_count"],
                               "pending_approval_count": counts["pending_approval_count"]}
                writers = {paths["summary"]: lambda path: path.write_text(_json(summary) + "\n", encoding="utf-8")}
                if validation is not None:
                    writers[paths["validation"]] = lambda path: path.write_text(_json(validation) + "\n", encoding="utf-8")
                publish_artifacts(writers)
            except Exception:
                pass
            self._update(job, **failed)
