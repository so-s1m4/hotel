from copy import deepcopy

import pytest

from common.contracts.schemas import NormalizedEventV1
from engine.planner import DecisionEngine
from tests.engine.fixtures import BUILDING, FakeGateway, state, slot, tick, request


def raw_event(kind, t, eid="E2", room="R102", resource="CLEANER_1", booking=None):
    return NormalizedEventV1(event_id="raw:"+eid, timestamp_utc=t, available_at_utc=t,
        event_type="operational_event", source={"file": "events.jsonl", "record_id": eid, "timestamp_utc": t},
        entities={"room_id": room, "resource_id": resource, "booking_id": booking},
        payload={"type": kind, "room_id": room, "resource_id": resource, "booking_id": booking})


def base_repo():
    repo = FakeGateway()
    repo.states["R101"] = [state()]
    repo.slots = [slot()]
    return repo


def test_retained_input_event_is_a_snapshot_not_caller_owned_mutable_payload():
    repo = base_repo()
    engine = DecisionEngine(repo, BUILDING)
    event = request()
    engine.process(event)
    event.payload["intents"][0]["requested_time_utc"] = "2026-01-07T20:00:00Z"
    assert engine.turnaround.pending[event.event_id].payload["intents"][0]["requested_time_utc"] == "2026-01-06T10:00:00Z"


def test_conflicting_reused_input_id_is_not_silently_dropped():
    engine = DecisionEngine(FakeGateway(), BUILDING)
    event = tick()
    engine.process(event)
    changed = event.model_copy(deep=True)
    changed.payload["injected"] = "different facts"
    with pytest.raises(ValueError, match="Conflicting"):
        engine.process(changed)


def test_observed_cleaner_busy_at_engine_start_is_not_allocated_elsewhere():
    repo = base_repo()
    repo.events = [{"event_id": "E1", "type": "cleaning_started", "timestamp_utc": "2026-01-06T08:30:00Z", "room_id": "R102", "resource_id": "CLEANER_1"}]
    engine = DecisionEngine(repo, BUILDING)
    rows = engine.process(request())
    assert all(r.action != "schedule_cleaning" for r in rows)
    assert engine.calendar.observed_busy
    released = engine.process(raw_event("cleaning_completed", "2026-01-06T09:15:00Z"))
    assert any(r.action == "schedule_cleaning" for r in released)


def test_unknown_observed_cleaner_blocks_unverified_crew_capacity():
    repo = base_repo()
    repo.events = [{"event_id": "E1", "type": "cleaning_started", "timestamp_utc": "2026-01-06T08:30:00Z", "room_id": "R102"}]
    rows = DecisionEngine(repo, BUILDING).process(request())
    assert all(r.action != "schedule_cleaning" for r in rows)


def test_observed_work_cancels_plan_with_explicit_dependencies_and_guest_correction():
    repo = base_repo()
    engine = DecisionEngine(repo, BUILDING)
    first = engine.process(request())
    plan = next(r for r in first if r.action == "schedule_cleaning")
    rows = engine.process(raw_event("cleaning_started", "2026-01-06T09:10:00Z"))
    cancelled = next(r for r in rows if r.action == "defer_action" and plan.event_id in r.dependencies)
    assert "Cancel/supersede" in cancelled.reason
    assert any(r.action == "notify_guest" and cancelled.event_id in r.dependencies for r in rows)
    assert not engine.calendar.reservations["CLEANER_1"]
    assert all(r.action != "schedule_cleaning" for r in rows)


def test_repeated_waiting_events_same_booking_do_not_spam_messages():
    repo = FakeGateway()
    repo.states["R101"] = [state(occupancy="occupied")]
    engine = DecisionEngine(repo, BUILDING)
    a = raw_event("arrival_waiting_room", "2026-01-06T09:00:00Z", eid="E1", room="R101", resource=None, booking="B1")
    b = raw_event("arrival_waiting_room", "2026-01-06T09:01:00Z", eid="E2", room="R101", resource=None, booking="B1")
    first = engine.process(a)
    second = engine.process(b)
    assert sum(r.action == "notify_guest" for r in first+second) == 1
    assert engine.notifications.suppressed == 1


def test_messages_for_distinct_bookings_are_not_suppressed_together():
    repo = FakeGateway()
    repo.states["R101"] = [state(occupancy="occupied")]
    engine = DecisionEngine(repo, BUILDING)
    a = request(rid="G1")
    b = request(t="2026-01-06T09:01:00Z", rid="G2")
    b.entities.booking_id = "B2"
    assert any(r.action == "notify_guest" for r in engine.process(a))
    assert any(r.action == "notify_guest" for r in engine.process(b))


def test_work_order_completion_becomes_known_only_at_observed_event():
    engine = DecisionEngine(FakeGateway(), BUILDING)
    creation = NormalizedEventV1(event_id="wo:W1", timestamp_utc="2026-01-06T08:00:00Z", available_at_utc="2026-01-06T08:00:00Z",
        event_type="work_order_created", source={"file": "work_orders.csv", "record_id": "W1"}, entities={"asset_id": "HP1"},
        payload={"work_order_id": "W1", "created_utc": "2026-01-06T08:00:00Z", "asset_id": "HP1", "completed_utc": "2026-01-06T10:00:00Z"})
    engine.process(creation)
    assert "observed_completed_utc" not in engine.work_orders[0]
    done = creation.model_copy(deep=True)
    done.event_id = "wo:W1:resolved"
    done.timestamp_utc = done.available_at_utc = "2026-01-06T10:00:00Z"
    done.event_type = "operational_event"
    done.payload["type"] = "work_order_completed"
    engine.process(done)
    assert engine.work_orders[0]["observed_completed_utc"] == "2026-01-06T10:00:00Z"


def test_identical_old_retry_after_later_event_is_idempotent():
    engine = DecisionEngine(FakeGateway(), BUILDING)
    first = tick()
    engine.process(first)
    engine.process(tick("2026-01-06T09:15:00Z"))
    assert engine.process(first) == []
    assert engine.event_count == 2


def test_returned_decisions_do_not_mutate_internal_cancellation_state():
    repo = base_repo()
    engine = DecisionEngine(repo, BUILDING)
    rows = engine.process(request())
    old = next(r for r in rows if r.action == "schedule_cleaning")
    original_id = old.event_id
    old.affected_assets[:] = ["MADE_UP"]
    next_rows = engine.process(raw_event("cleaning_started", "2026-01-06T09:10:00Z"))
    assert any(original_id in r.dependencies and r.affected_assets == ["R101"] for r in next_rows)


def test_sickness_sends_one_withdrawal_not_two():
    repo = base_repo()
    repo.slots[0]["start_utc"] = "2026-01-06T10:00:00Z"
    engine = DecisionEngine(repo, BUILDING)
    engine.process(request(deadline="2026-01-06T12:00:00Z"))
    repo.slots[0].update(status="sick", effective_status_asof="sick", status_known_utc="2026-01-06T09:15:00Z")
    repo.states["R101"].append(state(t="2026-01-06T09:15:00Z"))
    rows = engine.process(tick("2026-01-06T09:15:00Z"))
    assert sum(r.action == "notify_guest" for r in rows) == 1
    assert any("withdraw" in r.reason for r in rows)


def test_new_accepted_estimate_is_not_lost_after_recent_clarification():
    repo = base_repo()
    engine = DecisionEngine(repo, BUILDING)
    first = request(rid="G1")
    first.payload["intents"][0]["requested_time_utc"] = None
    assert any(r.action == "notify_guest" for r in engine.process(first))
    rows = engine.process(request(t="2026-01-06T09:01:00Z", rid="G2"))
    assert any(r.action == "schedule_cleaning" for r in rows)
    assert any(r.action == "notify_guest" and "estimate" in r.reason for r in rows)


def test_reopened_episode_same_timestamp_has_new_deterministic_identity():
    from engine.decisions import EpisodeTracker
    tracker = EpisodeTracker()
    first = tracker.episode("same", "2026-01-06T09:00:00Z")
    tracker.close("same")
    second = tracker.episode("same", "2026-01-06T09:00:00Z")
    assert first != second
    another = EpisodeTracker()
    assert another.episode("same", "2026-01-06T09:00:00Z") == first
    another.close("same")
    assert another.episode("same", "2026-01-06T09:00:00Z") == second


def test_non_catalog_performer_is_uncertainty_not_an_extra_free_worker():
    repo = base_repo()
    repo.events = [{"event_id": "E1", "type": "cleaning_started", "timestamp_utc": "2026-01-06T08:30:00Z", "room_id": "R102", "resource_id": "housekeeping"}]
    assert all(r.action != "schedule_cleaning" for r in DecisionEngine(repo, BUILDING).process(request()))


def test_observed_oncall_housekeeper_is_supported_without_role_crash():
    repo = base_repo()
    engine = DecisionEngine(repo, BUILDING)
    engine.process(raw_event("cleaning_started", "2026-01-06T09:00:00Z", resource="ONCALL_1"))
    assert engine.calendar.observed_busy["ONCALL_1"]
    engine.process(raw_event("cleaning_completed", "2026-01-06T09:15:00Z", eid="E3", resource="ONCALL_1"))
    assert not engine.calendar.observed_busy


def test_pending_oncall_capacity_is_withdrawn_when_guest_already_checked_in_elsewhere():
    now = "2026-01-06T15:00:00Z"
    repo = FakeGateway()
    repo.states["R101"] = [state(t=now)]
    engine = DecisionEngine(repo, BUILDING)
    rows = engine.process(request(t=now, waiting=True))
    proposal = next(r for r in rows if r.resource_id == "ONCALL_1")
    assert engine.calendar.tentative_reservations["ONCALL_1"]
    repo.bookings = [{"booking_id": "B1", "room_id": "R101", "created_utc": "2026-01-05T00:00:00Z", "arrival_utc": now,
                      "departure_utc": "2026-01-07T10:00:00Z", "observed_state": "checked_in", "observed_room_id": "R102"}]
    updated = engine.process(raw_event("checkin", "2026-01-06T15:15:00Z", room="R102", resource=None, booking="B1"))
    assert not engine.calendar.tentative_reservations.get("ONCALL_1")
    assert any(proposal.event_id in r.dependencies for r in updated if r.action == "defer_action")


def test_different_hash_seeds_produce_identical_serialized_surge_evidence(tmp_path):
    import os
    import subprocess
    import sys
    code = '''from tests.engine.test_extended import full_building,surge_repo
from engine.planner import DecisionEngine
from engine.decisions import write_decisions
from tests.engine.fixtures import tick
from pathlib import Path
import sys
write_decisions(DecisionEngine(surge_repo(),full_building()).process(tick()),Path(sys.argv[1]))
'''
    outputs = []
    for seed in ["11", "71"]:
        target = tmp_path/('seed'+seed+'.jsonl')
        subprocess.run([sys.executable, "-c", code, str(target)], env={**os.environ, "PYTHONHASHSEED": seed}, check=True)
        outputs.append(target.read_bytes())
    assert outputs[0] == outputs[1]
