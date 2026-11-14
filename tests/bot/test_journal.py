from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
from pathlib import Path
import stat

import pytest

from hotel_bot.journal import JournalCorruptionError, SessionJournal


NOW = datetime(2026, 11, 16, 9, tzinfo=timezone.utc)


def journal(tmp_path, name="test-session"):
    return SessionJournal(root=tmp_path, session_id=name, clock=lambda: NOW)


def append(log, **kwargs):
    return log.append("classification", "Guest message classified",
                      "The validated extraction contains a water issue; review the source quote before any intervention.",
                      room_id="R208", request_id="request-1", status="needs_review", **kwargs)


def test_persists_reload_and_immutable_copies(tmp_path):
    log = journal(tmp_path)
    details = {"model": "gpt-oss-120b", "method": "llm", "uncertain_fields": [], "effects_applied": False}
    result = append(log, details=details, evidence=["guest message: exact source quote"])
    details["model"] = "changed"
    result["details"]["model"] = "also changed"
    loaded = journal(tmp_path)
    page = loaded.read()
    assert page["total"] == page["next_cursor"] == 1
    entry = page["entries"][0]
    assert entry["id"] == 1
    assert entry["timestamp"] == "2026-11-16T09:00:00.000Z"
    assert entry["details"]["model"] == "gpt-oss-120b"
    page["entries"][0]["evidence"].clear()
    assert loaded.read()["entries"][0]["evidence"]
    assert stat.S_IMODE(log.path.stat().st_mode) == 0o600


def test_full_pagination_is_not_limited_to_latest_100(tmp_path):
    log = journal(tmp_path)
    for _ in range(205):
        append(log)
    first = log.read()
    second = log.read(after=first["next_cursor"])
    third = log.read(after=second["next_cursor"])
    assert [len(x["entries"]) for x in (first, second, third)] == [100, 100, 5]
    assert [entry["id"] for page in (first, second, third) for entry in page["entries"]] == list(range(1, 206))
    assert log.read(after=205) == {"entries": [], "next_cursor": 205, "total": 205}


def test_independent_instances_and_threads_share_contiguous_append_sequence(tmp_path):
    left, right = journal(tmp_path), journal(tmp_path)
    with ThreadPoolExecutor(max_workers=8) as pool:
        rows = list(pool.map(lambda index: append(left if index % 2 else right), range(40)))
    assert sorted(item["id"] for item in rows) == list(range(1, 41))
    assert left.total == right.total == 40


@pytest.mark.parametrize("tail", [b'{"id":2', b'{}\n', b'\n', b'not json\n', b'\xff\n'])
def test_corrupt_tail_fails_explicitly_without_dropping_or_truncating(tmp_path, tail):
    log = journal(tmp_path)
    append(log)
    with log.path.open("ab") as handle:
        handle.write(tail)
    original = log.path.read_bytes()
    with pytest.raises(JournalCorruptionError):
        journal(tmp_path)
    with pytest.raises(JournalCorruptionError):
        log.read()
    with pytest.raises(JournalCorruptionError):
        append(log)
    assert log.path.read_bytes() == original


def test_valid_json_without_final_newline_is_not_silently_accepted(tmp_path):
    log = journal(tmp_path)
    append(log)
    log.path.write_bytes(log.path.read_bytes().rstrip(b"\n"))
    with pytest.raises(JournalCorruptionError):
        journal(tmp_path)


def test_duplicate_keys_or_noncontiguous_ids_are_corruption(tmp_path):
    for name, mutate in (("duplicate", lambda s: s.replace('"id":1', '"id":1,"id":1')),
                         ("gap", lambda s: s.replace('"id":1', '"id":2'))):
        log = journal(tmp_path, name)
        append(log)
        log.path.write_text(mutate(log.path.read_text()))
        with pytest.raises(JournalCorruptionError):
            journal(tmp_path, name)


@pytest.mark.parametrize("name", ["../outside", ".", "..", "a/b", "", "a\\b", "x" * 81])
def test_session_names_cannot_escape_root(tmp_path, name):
    with pytest.raises(ValueError):
        journal(tmp_path, name)


def test_explicit_replay_path_uses_no_extra_session_directory(tmp_path):
    path = tmp_path / "replay-01" / "journal.jsonl"
    log = SessionJournal(path=path, clock=lambda: NOW)
    append(log, details={"model_name": "test", "source_record": {"request_id": "R1"},
                         "extraction": {"method": "llm"}, "decision_record": {"action": "notify_guest"},
                         "audit": {"effects_applied": False}})
    assert log.path == path
    assert SessionJournal(path=path).total == 1


def test_symlink_destination_is_rejected(tmp_path):
    outside = tmp_path / "outside.jsonl"
    outside.write_text("")
    link = tmp_path / "linked.jsonl"
    link.symlink_to(outside)
    with pytest.raises(ValueError):
        SessionJournal(path=link)


@pytest.mark.parametrize("details", [
    {"Authorization": "not a real credential"}, {"api_key": "not a real credential"},
    {"source": {"http_headers": {}}}, {"CEREBRAS_API_KEY": "not a real credential"},
    {"password": "not a real credential"}, {"value": float("nan")},
])
def test_credentials_and_non_json_numbers_cannot_be_persisted(tmp_path, details):
    log = journal(tmp_path)
    with pytest.raises(ValueError):
        append(log, details=details)
    assert log.total == 0


def test_token_shaped_text_is_redacted_in_all_fields(tmp_path):
    log = journal(tmp_path)
    fake = "csk-" + "x" * 30
    row = log.append("request", "Guest message", "Guest wrote: " + fake,
                     evidence=[fake], details={"original_text": fake})
    assert fake not in log.path.read_text()
    assert fake not in str(row)
    assert "[credential redacted]" in row["reason"]


@pytest.mark.parametrize("after,limit", [(-1, 100), (False, 100), (0, 0), (0, 501), (0, True), ("1", 100)])
def test_pagination_is_strict(tmp_path, after, limit):
    with pytest.raises(ValueError):
        journal(tmp_path).read(after, limit)


def test_reason_is_mandatory_and_failed_validation_does_not_consume_sequence(tmp_path):
    log = journal(tmp_path)
    with pytest.raises(ValueError):
        log.append("action", "Some action", " ")
    assert append(log)["id"] == 1


def test_refresh_detects_same_size_external_corruption(tmp_path):
    log = journal(tmp_path)
    append(log)
    old = log.path.read_bytes()
    log.path.write_bytes(old.replace(b'"id":1', b'"id":9'))
    with pytest.raises(JournalCorruptionError):
        log.read()
