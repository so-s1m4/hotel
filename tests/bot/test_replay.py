"""Real miniature dataset + real engine; all model replies are injected locally."""
from copy import deepcopy
import csv
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from hashlib import sha256
import socket
from threading import Event
from time import monotonic, sleep

import pytest

from common.contracts.schemas import ExtractionMetaV1, GuestIntentV1
from engine.core import instant
from engine.planner import DecisionEngine
from hotel_bot.replay import (ReplayManager, ReplayConflictError, ReplayNotFoundError,
                              ReplayValidationError, ARTIFACTS)
from ingestion.loader import REQUIRED_COLUMNS


def csv_rows(path, rows):
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


class Parser:
    def __init__(self, method="llm", failure=False):
        self.calls = []
        self.method = method
        self.failure = failure

    def __call__(self, record, context):
        self.calls.append((deepcopy(record), deepcopy(context)))
        if self.failure:
            raise RuntimeError("synthetic-private-api-key-must-not-leak")
        item = GuestIntentV1(intent=record["request_type"], evidence_quote=record["text"],
                             needs_review=self.method != "llm")
        return [item.model_dump()], ExtractionMetaV1(method=self.method, model_name="injected-test-model",
            needs_review=self.method != "llm", uncertain_fields=[])


@pytest.fixture
def datasets(tmp_path, monkeypatch):
    def no_network(*args, **kwargs):
        raise AssertionError("Replay tests must not contact any model")

    monkeypatch.setattr(socket, "create_connection", no_network)
    monkeypatch.setattr(socket.socket, "connect", no_network)
    root = tmp_path / "data"
    quick = root / "PLATO40_v2_quickstart"
    quick.mkdir(parents=True)
    for name, columns in REQUIRED_COLUMNS.items():
        (quick / name).write_text(",".join(columns) + "\n")
    building = {"time": {"local_timezone": "Europe/Vienna"},
        "building": {"rooms": [{"id": "R101", "floor": 1}, {"id": "R102", "floor": 1}], "assets": []},
        "constraints": {"occupied_comfort_c": [20, 23]},
        "staff": {"resources": [{"resource_id": "CLEANER_1", "role": "cleaner"},
                                {"resource_id": "TECH_1", "role": "technician"}]}}
    (quick / "building.json").write_text(json.dumps(building))
    manifest = {"model_id": "PLATO-40-test", "profile": "quickstart", "synthetic": True,
                "start_utc": "2026-01-06T09:00:00Z", "end_utc": "2026-01-06T10:30:00Z", "interval_seconds": 900}
    (quick / "manifest.json").write_text(json.dumps(manifest))
    csv_rows(quick / "bookings.csv", [
        {"booking_id": "B1", "room_id": "R101", "created_utc": "2026-01-05T00:00:00Z",
         "arrival_utc": "2026-01-06T14:00:00Z", "departure_utc": "2026-01-07T10:00:00Z"},
        {"booking_id": "B2", "room_id": "R102", "created_utc": "2026-01-06T10:00:00Z",
         "arrival_utc": "2026-01-07T14:00:00Z", "departure_utc": "2026-01-08T10:00:00Z"},
    ])
    csv_rows(quick / "guest_requests.csv", [
        {"request_id": "G1", "timestamp_utc": "2026-01-06T09:00:00Z", "room_id": "R101",
         "booking_id": "B1", "request_type": "extra_towels", "text": "Please bring extra towels.",
         "language": "en", "priority": "low", "custom_note": "kept-from-original-source"},
        {"request_id": "G2", "timestamp_utc": "2026-01-06T09:15:00Z", "room_id": "R102",
         "booking_id": "B2", "request_type": "wifi_help", "text": "What is the Wi-Fi password?",
         "language": "en", "priority": "low", "custom_note": "future-booking-not-visible"},
    ])
    csv_rows(quick / "staff_roster.csv", [
        {"shift_id": "S1", "resource_id": "CLEANER_1", "role": "cleaner",
         "start_utc": "2026-01-06T08:00:00Z", "end_utc": "2026-01-06T15:00:00Z", "status": "scheduled",
         "status_known_utc": "2026-01-05T00:00:00Z"},
        {"shift_id": "S2", "resource_id": "TECH_1", "role": "technician",
         "start_utc": "2026-01-06T08:00:00Z", "end_utc": "2026-01-06T15:00:00Z", "status": "scheduled",
         "status_known_utc": "2026-01-05T00:00:00Z"},
    ])
    times = [f"2026-01-06T{hour:02d}:{minute:02d}:00Z" for hour, minute in
             [(9, 0), (9, 15), (9, 30), (9, 45), (10, 0), (10, 15), (10, 30)]]
    csv_rows(quick / "room_telemetry.csv", [
        {"timestamp_utc": time, "room_id": room, "housekeeping_state": "clean", "temperature_c": 21,
         "setpoint_c": 21, "presence": 0, "window_open": 0}
        for time in times for room in ["R101", "R102"]])
    csv_rows(quick / "room_water.csv", [{"timestamp_utc": time, "room_id": "R101", "meter_total_l": 100 + 5 * index}
                                        for index, time in enumerate(times)])
    events = [{"event_id": "VACANT", "type": "checkout", "room_id": "R101", "timestamp_utc": "2026-01-06T08:45:00Z"},
              {"event_id": "OCCUPIED", "type": "checkin", "room_id": "R102", "timestamp_utc": "2026-01-06T08:45:00Z"},
              {"event_id": "TIE", "type": "note", "room_id": "R101", "timestamp_utc": "2026-01-06T09:00:00Z"}]
    (quick / "events.jsonl").write_text("".join(json.dumps(row) + "\n" for row in events))
    yield root, quick, tmp_path / "outputs"


def await_terminal(manager, job_id):
    deadline = monotonic() + 8
    while monotonic() < deadline:
        result = manager.status(job_id)
        if result["job"]["status"] not in {"queued", "running", "finalizing"}:
            return result
        sleep(0.01)
    pytest.fail("Miniature replay did not finish")


def rows(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_real_engine_pipeline_and_every_decision_are_audited_and_logged(datasets):
    root, _, outputs = datasets
    parser = Parser()
    manager = ReplayManager(root, outputs, parser=parser)
    try:
        job = manager.start("quickstart")
        result = await_terminal(manager, job["id"])
        final = result["job"]
        assert final["status"] == "completed", final
        assert final["complete"] and final["validation"]["valid"]
        assert final["progress"]["llm_count"] == final["progress"]["guest_processed"] == 2
        assert final["progress"]["fallback_count"] == final["progress"]["source_errors"] == 0
        assert len(parser.calls) == 2
        assert parser.calls[0][0]["custom_note"] == "kept-from-original-source"
        assert parser.calls[0][0]["request_type"] == "extra_towels"
        assert parser.calls[0][1]["booking"]["booking_id"] == "B1"
        assert parser.calls[1][1]["booking"] is None
        assert all("source_label_policy" not in context for _, context in parser.calls)
        decisions = rows(manager.artifact(job["id"], "decisions"))
        audits = rows(manager.artifact(job["id"], "audit"))
        journal = rows(manager.artifact(job["id"], "journal"))
        entries = [row for row in journal if row["kind"] == "engine_decision"]
        assert decisions and len(decisions) == len(audits) == len(entries)
        assert {row["event_id"] for row in decisions} == {row["details"]["decision_record"]["event_id"] for row in entries}
        assert all(row["reason"] == row["details"]["decision_record"]["reason"] for row in entries)
        assert all(row["status"] == row["details"]["audit"]["status"] for row in entries)
        assert all(row["details"]["trigger"]["source"]["file"] for row in entries)
        assert any(row["action"] == "create_service_task" for row in decisions)
        assert any(not row["requires_human_approval"] for row in decisions)
        assert final["progress"]["autonomous_count"] == sum(not row["requires_human_approval"] for row in decisions)
        assert all(audit["effects_applied"] is False for audit in audits)
        assert final["accuracy_measured"] is False
        view = result["snapshot"]
        assert view["source"] == "historical_dataset" and view["dataset_id"] == "quickstart"
        assert {room["id"] for room in view["rooms"]} == {"R101", "R102"}
        assert all(not room["fixture"] and not room["release_confirmed"] for room in view["rooms"])
        assert {person["id"] for person in view["people"]} == {"CLEANER_1", "TECH_1"}
        assert [message["id"] for message in view["recent_messages"]] == ["G1", "G2"]
        assert view["recent_messages"][0]["text"] == "Please bring extra towels."
        assert all(item["autonomous"] == (not item["decision"]["requires_human_approval"]
                                          and not item["decision"]["requires_guest_consent"])
                   for item in view["decisions"])
        for kind in ARTIFACTS:
            assert manager.artifact(job["id"], kind).is_file()
    finally:
        manager.close()


def test_same_time_facts_precede_clock_and_source_events_are_not_mutated(datasets, monkeypatch):
    root, _, outputs = datasets
    observed = []
    original = DecisionEngine.process

    def track(self, event):
        observed.append((instant(event.available_at_utc), event.event_type, event.event_id))
        return original(self, event)

    monkeypatch.setattr(DecisionEngine, "process", track)
    manager = ReplayManager(root, outputs, parser=Parser())
    try:
        job = manager.start("quickstart")
        assert await_terminal(manager, job["id"])["job"]["complete"]
        assert observed == sorted(observed, key=lambda row: (row[0], row[1] == "clock_tick", row[2]))
        assert len({row[2] for row in observed}) == len(observed)
        extracted = rows(manager.artifact(job["id"], "extractions"))
        assert len(extracted) == 2
        assert extracted[0]["normalized_event"]["source"]["record_id"] == "G1"
    finally:
        manager.close()


@pytest.mark.parametrize("method,failure", [("fallback", False), ("source_label", False), ("llm", True)])
def test_model_failure_and_source_only_extraction_never_claim_success(datasets, method, failure):
    root, _, outputs = datasets
    manager = ReplayManager(root, outputs, parser=Parser(method, failure))
    try:
        job = manager.start("quickstart")
        result = await_terminal(manager, job["id"])
        assert result["job"]["status"] == "degraded" and result["job"]["complete"]
        progress = result["job"]["progress"]
        assert progress["llm_count"] == 0
        assert progress["fallback_count"] + progress["source_label_count"] == 2
        assert progress["parser_errors"] == (2 if failure else 0)
        assert "synthetic-private-api-key" not in json.dumps(result)
        assert "synthetic-private-api-key" not in manager.artifact(job["id"], "journal").read_text()
    finally:
        manager.close()


def test_single_job_cancel_and_no_eager_remaining_model_calls(datasets):
    root, _, outputs = datasets
    entered, release = Event(), Event()
    parser = Parser()

    def blocked(record, context):
        entered.set()
        assert release.wait(timeout=4)
        return parser(record, context)

    manager = ReplayManager(root, outputs, parser=blocked)
    try:
        job = manager.start("quickstart")
        assert entered.wait(timeout=4)
        with pytest.raises(ReplayConflictError):
            manager.start("quickstart")
        with pytest.raises(ReplayConflictError):
            manager.artifact(job["id"], "journal")
        assert manager.cancel(job["id"])["cancel_requested"]
        release.set()
        result = await_terminal(manager, job["id"])
        assert result["job"]["status"] == "cancelled" and not result["job"]["complete"]
        assert len(parser.calls) == 1
        assert result["job"]["progress"]["guest_processed"] == 1
        assert manager.artifact(job["id"], "summary").is_file()
    finally:
        release.set()
        manager.close()


def test_dataset_allowlist_paths_artifacts_and_cursors_are_bounded(datasets):
    root, quick, outputs = datasets
    manager = ReplayManager(root, outputs, parser=Parser())
    assert [row["id"] for row in manager.list_datasets()] == ["quickstart", "dev"]
    assert manager.list_datasets()[0]["available"]
    assert not manager.list_datasets()[1]["available"]
    for invalid in ("holdout", "../PLATO40_v2_quickstart", str(quick), None, []):
        with pytest.raises(ReplayValidationError):
            manager.start(invalid)
    with pytest.raises(ReplayValidationError):
        ReplayManager(root, root / "PLATO40_v2_quickstart" / "outputs")
    with pytest.raises(ReplayNotFoundError):
        manager.status("missing")
    job = manager.start("quickstart")
    result = await_terminal(manager, job["id"])
    pages, cursor = [], 0
    while True:
        page = manager.status(job["id"], after=cursor, limit=2)
        pages.extend(page["timeline"])
        cursor = page["next_cursor"]
        if not page["has_more"]:
            break
    assert len(pages) == result["total"]
    assert [row["id"] for row in pages] == list(range(1, len(pages) + 1))
    for after, limit in ((-1, 2), (False, 2), (0, 201), (0, 0)):
        with pytest.raises(ReplayValidationError):
            manager.status(job["id"], after=after, limit=limit)
    with pytest.raises(ReplayNotFoundError):
        manager.artifact(job["id"], "../building.json")
    manager.close()
    with pytest.raises(ReplayConflictError):
        manager.start("quickstart")


def test_processing_failure_persists_partial_outputs_with_generic_failure(datasets, monkeypatch):
    root, _, outputs = datasets
    original = DecisionEngine.process

    def fail(self, event):
        if event.event_type == "clock_tick":
            raise ValueError("synthetic-private-api-key-must-not-leak")
        return original(self, event)

    monkeypatch.setattr(DecisionEngine, "process", fail)
    manager = ReplayManager(root, outputs, parser=Parser())
    try:
        job = manager.start("quickstart")
        result = await_terminal(manager, job["id"])
        assert result["job"]["status"] == "failed" and not result["job"]["complete"]
        assert result["job"]["error"]["type"] == "ValueError"
        assert "synthetic-private-api-key" not in json.dumps(result)
        summary = json.loads(manager.artifact(job["id"], "summary").read_text())
        assert summary["status"] == "failed" and summary["complete"] is False
    finally:
        manager.close()


def test_inputs_changed_during_replay_are_reported_as_failed_not_verified(datasets):
    root, quick, outputs = datasets
    parser = Parser()
    edited = False

    def change_source(record, context):
        nonlocal edited
        if not edited:
            with (quick / "weather.csv").open("a") as stream:
                stream.write("2026-01-06T09:00:00Z\n")
            edited = True
        return parser(record, context)

    manager = ReplayManager(root, outputs, parser=change_source)
    try:
        job = manager.start("quickstart")
        result = await_terminal(manager, job["id"])
        assert result["job"]["status"] == "failed" and not result["job"]["complete"]
        assert result["job"]["error"]["type"] == "SourceDataChanged"
        assert result["job"]["changed_input_files"] == ["weather.csv"]
        assert result["job"]["progress"]["source_errors"] >= 1
    finally:
        manager.close()


def test_output_publication_failure_never_claims_completed_results(datasets, monkeypatch):
    from hotel_bot import replay

    root, _, outputs = datasets

    def refuse(*args, **kwargs):
        raise OSError("synthetic-private-api-key-in-provider-error")

    monkeypatch.setattr(replay, "publish_artifacts", refuse)
    manager = ReplayManager(root, outputs, parser=Parser())
    try:
        job = manager.start("quickstart")
        result = await_terminal(manager, job["id"])
        assert result["job"]["status"] == "failed" and not result["job"]["complete"]
        assert result["job"]["error"]["code"] == "artifact_publication_failed"
        assert "synthetic-private-api-key" not in json.dumps(result)
        with pytest.raises(ReplayNotFoundError):
            manager.artifact(job["id"], "decisions")
        assert manager.artifact(job["id"], "journal").is_file()
    finally:
        manager.close()


def completed_run(datasets):
    root, _, outputs = datasets
    manager = ReplayManager(root, outputs, parser=Parser())
    job = manager.start("quickstart")
    final = await_terminal(manager, job["id"])
    assert final["job"]["status"] == "completed"
    manager.close()
    return job["id"], final, outputs / job["id"]


@pytest.mark.parametrize("legacy", [False, True])
def test_completed_job_restores_without_model_or_engine_and_downloads_survive_restart(datasets, monkeypatch, legacy):
    root, _, outputs = datasets
    job_id, old, folder = completed_run(datasets)
    if legacy:
        summary = json.loads((folder / "summary.json").read_text())
        summary.pop("snapshot", None)
        (folder / "summary.json").write_text(json.dumps(summary) + "\n")
        entries = rows(folder / "journal.jsonl")
        entries[-1]["details"].pop("summary_sha256", None)
        (folder / "journal.jsonl").write_text("".join(json.dumps(entry) + "\n" for entry in entries))

    def forbidden(*args, **kwargs):
        raise AssertionError("Restoring an archive must not run inference or the engine")

    monkeypatch.setattr(DecisionEngine, "__init__", forbidden)
    restored = ReplayManager(root, outputs, parser=forbidden)
    try:
        state = restored.status(job_id)
        assert state["job"]["status"] == "completed" and state["job"]["restored"]
        assert state["job"]["source_unchanged"] and state["job"]["view_reconstructed_without_inference"]
        assert state["job"]["progress"] == old["job"]["progress"]
        assert state["snapshot"] == old["snapshot"]
        assert state["timeline"] == old["timeline"] or legacy
        assert restored.status(job_id, after=state["total"], limit=1)["timeline"] == []
        for kind in ARTIFACTS:
            assert restored.artifact(job_id, kind) == folder / ARTIFACTS[kind]
    finally:
        restored.close()


def test_changed_input_on_restore_hides_view_but_keeps_original_archived_logs(datasets):
    root, quick, outputs = datasets
    job_id, old, _ = completed_run(datasets)
    with (quick / "weather.csv").open("a") as handle:
        handle.write("2026-01-06T09:00:00Z\n")
    restored = ReplayManager(root, outputs, parser=Parser(failure=True))
    result = restored.status(job_id)
    assert result["job"]["restored"] and result["job"]["source_unchanged"] is False
    assert result["job"]["error"]["code"] == "source_changed_since_replay"
    assert result["snapshot"] == {} and result["timeline"] == old["timeline"]
    assert restored.artifact(job_id, "decisions").is_file()
    restored.close()


@pytest.mark.parametrize("bad_id", ["../summary.json", "replay-../x", "replay-" + "z" * 32,
                                     "replay-" + "a" * 33, "holdout", None, []])
def test_restore_identifiers_never_become_filesystem_paths(datasets, bad_id):
    root, _, outputs = datasets
    manager = ReplayManager(root, outputs, parser=Parser())
    with pytest.raises(ReplayNotFoundError):
        manager.status(bad_id)


@pytest.mark.parametrize("kind", ["job_directory", "audit"])
def test_restore_rejects_symlinked_job_or_artifact(datasets, kind):
    root, _, outputs = datasets
    job_id, _, folder = completed_run(datasets)
    if kind == "job_directory":
        target = outputs / "outside-archive"
        folder.rename(target)
        folder.symlink_to(target, target_is_directory=True)
        expected = ReplayNotFoundError
    else:
        target = outputs / "outside-audit.jsonl"
        (folder / "audit.jsonl").rename(target)
        (folder / "audit.jsonl").symlink_to(target)
        expected = ReplayValidationError
    with pytest.raises(expected):
        ReplayManager(root, outputs, parser=Parser()).status(job_id)


@pytest.mark.parametrize("corruption", ["summary_id", "nonterminal", "checksum", "journal", "oversized"])
def test_restore_rejects_uncommitted_corrupt_or_oversized_artifacts(datasets, corruption):
    root, _, outputs = datasets
    job_id, _, folder = completed_run(datasets)
    summary_path = folder / "summary.json"
    if corruption in {"summary_id", "nonterminal"}:
        summary = json.loads(summary_path.read_text())
        if corruption == "summary_id":
            summary["id"] = "replay-" + "0" * 32
        else:
            summary["status"] = "running"
        summary_path.write_text(json.dumps(summary) + "\n")
    elif corruption == "checksum":
        # Even an innocuous change invalidates a new committed summary hash.
        with summary_path.open("a") as handle:
            handle.write(" ")
    elif corruption == "journal":
        with (folder / "journal.jsonl").open("a") as handle:
            handle.write('{"partial":')
    else:
        with summary_path.open("wb") as handle:
            handle.truncate(8 * 1024 * 1024 + 1)
    with pytest.raises(ReplayValidationError):
        ReplayManager(root, outputs, parser=Parser()).status(job_id)


def test_terminal_publication_failure_has_no_success_log_and_restores_as_failed(datasets, monkeypatch):
    from hotel_bot import replay

    root, _, outputs = datasets
    publish = replay.publish_artifacts
    calls = 0

    def fail_terminal_once(writers):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("synthetic terminal publication failure")
        return publish(writers)

    monkeypatch.setattr(replay, "publish_artifacts", fail_terminal_once)
    manager = ReplayManager(root, outputs, parser=Parser())
    job = manager.start("quickstart")
    result = await_terminal(manager, job["id"])
    assert result["job"]["status"] == "failed"
    assert result["job"]["failure_log_persisted"] is True
    entries = rows(manager.artifact(job["id"], "journal"))
    assert not any(entry["kind"] == "replay_finished" for entry in entries)
    assert entries[-1]["kind"] == "replay_failed" and entries[-1]["status"] == "failed"
    summary = json.loads(manager.artifact(job["id"], "summary").read_text())
    assert summary["status"] == "failed" and not summary["complete"]
    manager.close()
    restored = ReplayManager(root, outputs, parser=Parser(failure=True))
    assert restored.status(job["id"])["job"]["status"] == "failed"
    restored.close()


def test_restore_cannot_trust_stale_success_summary_after_failure_marker(datasets):
    from hotel_bot.journal import SessionJournal

    root, _, outputs = datasets
    job_id, _, folder = completed_run(datasets)
    journal = SessionJournal(path=folder / "journal.jsonl")
    journal.append("replay_failed", "Publication failed", "A later failed commit invalidates the success marker.", status="failed")
    restored = ReplayManager(root, outputs, parser=Parser())
    state = restored.status(job_id)
    assert state["job"]["status"] == "failed" and state["job"]["complete"] is False
    assert state["job"]["error"]["code"] == "artifact_publication_failed"
    restored.close()


def test_failed_decision_log_reconciles_terminal_counts_from_committed_engine(datasets, monkeypatch):
    from hotel_bot.journal import SessionJournal

    root, _, outputs = datasets
    append = SessionJournal.append
    failed = False

    def fail_decision_once(self, kind, *args, **kwargs):
        nonlocal failed
        if kind == "engine_decision" and not failed:
            failed = True
            raise OSError("synthetic journal write failure")
        return append(self, kind, *args, **kwargs)

    monkeypatch.setattr(SessionJournal, "append", fail_decision_once)
    manager = ReplayManager(root, outputs, parser=Parser())
    job = manager.start("quickstart")
    state = await_terminal(manager, job["id"])
    assert state["job"]["status"] == "failed" and not state["job"]["complete"]
    decisions = rows(manager.artifact(job["id"], "decisions"))
    audits = rows(manager.artifact(job["id"], "audit"))
    progress = state["job"]["progress"]
    assert decisions and progress["decisions"] == len(decisions)
    assert progress["audit_rows"] == len(audits)
    assert progress["autonomous_count"] == sum(not row["requires_human_approval"] and not row.get("requires_guest_consent", False) for row in decisions)
    assert progress["events_processed"] > 0 and progress["as_of_utc"] == max(row["timestamp_utc"] for row in decisions)
    manager.close()
    restored = ReplayManager(root, outputs, parser=Parser())
    assert restored.status(job["id"])["job"]["status"] == "failed"
    restored.close()


@pytest.mark.parametrize("failure", ["startup_log", "thread_start"])
def test_failed_start_does_not_leave_an_active_job_blocking_retries(datasets, monkeypatch, failure):
    from hotel_bot.journal import SessionJournal
    from hotel_bot import replay

    root, _, outputs = datasets
    append, thread_start = SessionJournal.append, replay.Thread.start
    failed = False

    def fail_log_once(self, kind, *args, **kwargs):
        nonlocal failed
        if failure == "startup_log" and kind == "replay_started" and not failed:
            failed = True
            raise OSError("synthetic startup log failure")
        return append(self, kind, *args, **kwargs)

    def fail_thread_once(self):
        nonlocal failed
        if failure == "thread_start" and not failed:
            failed = True
            raise RuntimeError("synthetic worker startup failure")
        return thread_start(self)

    monkeypatch.setattr(SessionJournal, "append", fail_log_once)
    monkeypatch.setattr(replay.Thread, "start", fail_thread_once)
    manager = ReplayManager(root, outputs, parser=Parser())
    with pytest.raises((OSError, RuntimeError)):
        manager.start("quickstart")
    assert manager._active is None and manager._jobs == {}
    job = manager.start("quickstart")
    assert await_terminal(manager, job["id"])["job"]["status"] == "completed"
    manager.close()
