"""Archived presentation never repeats inference or the decision engine."""
import json

import pytest

from engine.core import instant
from engine.planner import DecisionEngine
from hotel_bot.replay import ReplayManager, ReplayConflictError, ReplayValidationError
from ingestion import events as source_events, guest_parser
from ingestion.replay import ReplayData
from tests.bot.test_replay import datasets, Parser, await_terminal, rows


@pytest.fixture
def archive(datasets, monkeypatch):
    root, dataset, output = datasets
    with (dataset / "events.jsonl").open("a") as stream:
        for event in [
            {"event_id": "ZZ_CHECKIN", "type": "checkin", "room_id": "R101", "booking_id": "B1",
             "timestamp_utc": "2026-01-06T09:00:00Z"},
            {"event_id": "DELAYED_CHECKOUT", "type": "checkout", "room_id": "R102", "booking_id": "B2",
             "timestamp_utc": "2026-01-06T08:50:00Z", "available_at_utc": "2026-01-06T09:30:00Z"},
        ]:
            stream.write(json.dumps(event) + "\n")
    expected = sorted(ReplayData(dataset, use_llm=False).iter_events(),
                      key=lambda e: (instant(e.available_at_utc), e.event_type == "clock_tick", e.event_id))
    parser = Parser()

    def saved_parser(record, context):
        items, meta = parser(record, context)
        items[0]["details"] = {"saved_result_marker": "original-extraction"}
        return items, meta

    manager = ReplayManager(root, output, parser=saved_parser)
    job = manager.start("quickstart")
    terminal = await_terminal(manager, job["id"])
    assert terminal["job"]["complete"]
    manager.close()

    def forbidden(*args, **kwargs):
        raise AssertionError("Playback must never parse messages or run the engine")

    for name in ("parse_request_full", "parse_requests_full_batch", "_call_llm"):
        monkeypatch.setattr(guest_parser, name, forbidden, raising=False)
    monkeypatch.setattr(source_events, "_parse_guest_requests", forbidden)
    monkeypatch.setattr(DecisionEngine, "process", forbidden)
    reader = ReplayManager(root, output, parser=forbidden)
    yield reader, job["id"], terminal, expected, dataset
    reader.close()


def test_every_original_event_has_exact_source_ordinal_first_last_and_tie_order(archive):
    manager, job, terminal, expected, _ = archive
    manifest = manager.playback_manifest(job)
    assert manifest["total_events"] == len(expected) == terminal["job"]["progress"]["events_processed"]
    assert manifest["start_utc"] == expected[0].available_at_utc
    assert manifest["end_utc"] == expected[-1].available_at_utc
    assert manifest["model_calls"] == 0 and manifest["engine_rerun"] is False
    assert manifest["operational_start_utc"] == "2026-01-06T09:00:00Z"
    assert manifest["observation_boundary"] == "all_source_facts_available_at_frame_timestamp"
    actual, cursor = [], 0
    while True:
        page = manager.timeline(job, after=cursor, limit=3)
        actual.extend(page["events"])
        cursor = page["next_cursor"]
        if not page["has_more"]:
            break
    assert [row["ordinal"] for row in actual] == list(range(1, len(expected) + 1))
    assert [row["event_id"] for row in actual] == [event.event_id for event in expected]
    assert len({row["event_id"] for row in actual}) == len(expected)
    tied = [row for row in actual if row["available_at_utc"] == "2026-01-06T09:00:00Z"]
    assert tied[-1]["event_type"] == "clock_tick"


def test_initial_frame_is_unknown_and_final_frame_matches_committed_outputs(archive):
    manager, job, terminal, expected, _ = archive
    initial = manager.playback_frame(job, cursor=0)
    assert initial["events_consumed"] == initial["source_ordinal"] == initial["journal_total"] == 0
    assert initial["as_of_utc"] is None and initial["current_event"] is None
    assert initial["next_event"]["event_id"] == expected[0].event_id
    assert all(room["occupancy"] == "unknown" and room["temperature_c"] is None
               and room["last_observed_utc"] is None and room["evidence"] == [] for room in initial["snapshot"]["rooms"])
    assert initial["snapshot"]["people"] == initial["snapshot"]["recent_messages"] == initial["snapshot"]["decisions"] == []
    assert initial["progress"]["decisions"] == initial["progress"]["guest_processed"] == 0
    final = manager.playback_frame(job, cursor=len(expected))
    assert final["next_event"] is None and final["progress"] == terminal["job"]["progress"]
    assert final["journal_total"] == terminal["total"]
    assert final["snapshot"]["rooms"] == terminal["snapshot"]["rooms"]
    assert final["snapshot"]["decisions"] == terminal["snapshot"]["decisions"]


def test_observations_match_engine_timestamp_boundary_and_exclude_later_publications(archive):
    manager, job, _, expected, dataset = archive
    positions = {event.event_id: index for index, event in enumerate(expected, 1)}
    before = manager.playback_frame(job, cursor=positions["guest:G1"])
    after = manager.playback_frame(job, cursor=positions["raw:ZZ_CHECKIN"])
    room = lambda frame, ident: next(row for row in frame["snapshot"]["rooms"] if row["id"] == ident)
    # The original engine also hydrates the whole available timestamp batch
    # before guest:G1; hiding the same-time check-in would misrepresent its facts.
    actual_gateway = ReplayData(dataset, use_llm=False)
    assert room(before, "R101")["occupancy"] == actual_gateway.get_room_state("R101", before["as_of_utc"])["occupancy"] == "occupied"
    assert before["source_ordinal"] < after["source_ordinal"]
    assert room(after, "R101")["occupancy"] == "occupied"
    assert room(manager.playback_frame(job, at="2026-01-06T09:29:59Z"), "R102")["occupancy"] == "occupied"
    assert room(manager.playback_frame(job, at="2026-01-06T09:30:00Z"), "R102")["occupancy"] == "vacant"
    rewound = manager.playback_frame(job, cursor=positions["guest:G1"])
    assert rewound == before  # forward seek cannot contaminate the earlier source index


def test_messages_decisions_audits_and_counters_only_use_saved_processed_prefix(archive):
    manager, job, _, expected, _ = archive
    logs = rows(manager.artifact(job, "journal"))
    extracts = rows(manager.artifact(job, "extractions"))
    positions = {event.event_id: index for index, event in enumerate(expected, 1)}
    for cursor in range(len(expected) + 1):
        frame = manager.playback_frame(job, cursor=cursor)
        visible = [entry["details"] for entry in logs if entry["kind"] == "engine_decision"
                   and positions[entry["details"]["trigger"]["event_id"]] <= cursor]
        assert frame["progress"]["decisions"] == len(visible)
        assert [row["decision"] for row in frame["snapshot"]["decisions"]] == [row["decision_record"] for row in visible[-20:]]
        assert [row["audit"] for row in frame["snapshot"]["decisions"]] == [row["audit"] for row in visible[-20:]]
        expected_guests = [row["normalized_event"] for row in extracts
                           if positions[row["normalized_event"]["event_id"]] <= cursor]
        assert frame["progress"]["guest_processed"] == len(expected_guests)
        assert [row["normalized_event"] for row in frame["snapshot"]["recent_messages"]] == expected_guests[-10:]
        for row in frame["snapshot"]["recent_messages"]:
            assert row["intents"][0]["details"] == {"saved_result_marker": "original-extraction"}


def test_time_seek_is_inclusive_monotonic_and_read_only(archive):
    manager, job, _, expected, _ = archive
    assert manager.playback_frame(job, at="2020-01-01T00:00:00Z")["source_ordinal"] == 0
    for event in expected:
        frame = manager.playback_frame(job, at=event.available_at_utc)
        assert frame["source_ordinal"] == sum(instant(e.available_at_utc) <= instant(event.available_at_utc) for e in expected)
        assert all(instant(row["available_at_utc"]) <= instant(frame["as_of_utc"])
                   for row in frame["snapshot"]["recent_messages"])
    first = manager.playback_frame(job, cursor=0)
    first["snapshot"]["rooms"][0]["occupancy"] = "occupied"
    assert manager.playback_frame(job, cursor=0)["snapshot"]["rooms"][0]["occupancy"] == "unknown"


def test_changed_source_and_incomplete_archive_cannot_claim_full_playback(archive):
    manager, job, _, _, dataset = archive
    restored = manager._job(job)
    restored.public["complete"] = False
    with pytest.raises(ReplayConflictError):
        manager.playback_manifest(job)
    restored.public["complete"] = True
    with (dataset / "events.jsonl").open("a") as stream:
        stream.write("\n")
    with pytest.raises(ReplayConflictError):
        manager.playback_manifest(job)


@pytest.mark.parametrize("query", [{"cursor": -1}, {"cursor": True}, {"cursor": 999999},
                                  {"cursor": 0, "at": "2026-01-06T09:00:00Z"}, {"at": "invalid"}])
def test_invalid_seek_fails_explicitly(archive, query):
    manager, job, _, _, _ = archive
    with pytest.raises(ReplayValidationError):
        manager.playback_frame(job, **query)


def test_dataset_discovery_exposes_existing_archive_without_preparing_again(archive):
    manager, job, _, _, _ = archive
    dataset = next(row for row in manager.list_datasets() if row["id"] == "quickstart")
    assert dataset["replay_id"] == job
    assert manager._active is None
