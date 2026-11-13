"""Lifecycle, reassignment and cancellation-provenance regressions for S01."""
from copy import deepcopy

import pytest

from engine.core import EngineConfig
from engine.detectors.turnaround import TurnaroundDetector
from engine.detectors.surge import SurgeDetector
from engine.planner import DecisionEngine
from engine.resources import ResourceCalendar
from tests.engine.fixtures import FakeGateway, BUILDING, state, slot, request, tick
from tests.engine.test_extended import full_building, surge_repo

NOW = "2026-01-06T09:00:00Z"
LATER = "2026-01-06T09:15:00Z"


def repository(cleaning="dirty", occupancy="vacant"):
    repo = FakeGateway()
    repo.states["R101"] = [state(cleaning=cleaning, occupancy=occupancy)]
    repo.slots = [slot(start="2026-01-06T10:00:00Z")]
    repo.bookings = [{"booking_id": "B1", "room_id": "R101", "created_utc": "2026-01-05T00:00:00Z",
                      "arrival_utc": "2026-01-06T10:00:00Z", "departure_utc": "2026-01-07T10:00:00Z"}]
    return repo


@pytest.mark.parametrize("observed", ["checked_in", "checked_out", "no_show"])
def test_arrival_request_retires_when_booking_lifecycle_is_already_observed(observed):
    repo = repository(occupancy="occupied")
    engine = DecisionEngine(repo, BUILDING)
    engine.process(request())
    repo.bookings[0].update(observed_state=observed, observed_room_id="R102")
    repo.states["R101"].append(state(t=LATER))
    rows = engine.process(tick(LATER))
    assert rows == []
    assert not engine.turnaround.pending
    assert not any(engine.calendar.reservations.values())


def test_existing_old_room_job_is_cancelled_with_booking_evidence_after_checkin_elsewhere():
    repo = repository()
    engine = DecisionEngine(repo, BUILDING)
    initial = engine.process(request(deadline="2026-01-06T12:00:00Z"))
    original = next(r for r in initial if r.action == "schedule_cleaning")
    repo.bookings[0].update(observed_state="checked_in", observed_room_id="R102",
        observed_checkin_utc="2026-01-06T09:10:00Z", evidence=["events.jsonl:CHECKED_IN:R102:2026-01-06T09:10:00Z"])
    repo.states["R101"].append(state(t=LATER))
    rows = engine.process(tick(LATER))
    assert not any(r.action == "schedule_cleaning" for r in rows)
    assert not engine.turnaround.pending
    assert not any(engine.calendar.reservations.values())
    cancel = next(r for r in rows if r.action == "defer_action" and original.event_id in r.dependencies)
    assert "observed checked_in in room R102" in cancel.reason
    assert "bookings.csv:B1" in cancel.evidence
    assert any("CHECKED_IN" in ref for ref in cancel.evidence)
    assert next(c for c in engine.calendar.cancellations if c["decision_id"] == original.event_id)["evidence"]


def test_known_precheckin_reassignment_retargets_without_rewriting_original_event():
    repo = repository(occupancy="occupied")
    engine = DecisionEngine(repo, BUILDING)
    source = request()
    engine.process(source)
    repo.bookings[0].update(observed_state="waiting_for_room", observed_room_id="R102")
    repo.events.append({"event_id": "MOVE1", "timestamp_utc": "2026-01-06T09:10:00Z", "type": "room_reassigned",
                        "booking_id": "B1", "from_room": "R101", "to_room": "R102"})
    repo.states["R102"] = [state(room="R102", t=LATER)]
    rows = engine.process(tick(LATER))
    cleaning = next(r for r in rows if r.action == "schedule_cleaning")
    assert cleaning.affected_assets == ["R102"]
    assert any("MOVE1" in ref for ref in cleaning.evidence)
    assert source.entities.room_id == "R101"
    assert engine.turnaround.pending[source.event_id].entities.room_id == "R101"
    assert engine.turnaround.target_rooms[source.event_id] == "R102"


def test_assignment_disagreement_without_observed_move_never_schedules_old_room():
    repo = repository()
    repo.bookings[0].update(observed_state="not_arrived", observed_room_id="R102")
    rows = DecisionEngine(repo, BUILDING).process(request())
    assert [r.action for r in rows] == ["notify_guest"]
    assert "assignment" in rows[0].reason


def test_future_lifecycle_claim_does_not_retire_or_authorise_work():
    repo = repository()
    repo.bookings[0].update(observed_state="checked_in", observed_room_id="R101", observed_checkin_utc="2026-01-07T09:00:00Z")
    engine = DecisionEngine(repo, BUILDING)
    rows = engine.process(request())
    assert all(r.action != "schedule_cleaning" for r in rows)
    assert engine.turnaround.pending


def test_untimestamped_final_cancellation_is_not_observed_lifecycle():
    repo = repository()
    repo.bookings[0]["status"] = "cancelled"
    rows = DecisionEngine(repo, BUILDING).process(request())
    assert any(r.action == "schedule_cleaning" for r in rows)


def test_future_available_assignment_does_not_retarget_early():
    repo = repository()
    repo.bookings[0].update(observed_state="not_arrived", observed_room_id="R102")
    repo.events.append({"event_id": "FUTURE_MOVE", "timestamp_utc": "2026-01-06T08:00:00Z",
                        "available_at_utc": "2026-01-06T10:00:00Z", "type": "room_reassigned",
                        "booking_id": "B1", "from_room": "R101", "to_room": "R102"})
    engine = DecisionEngine(repo, BUILDING)
    rows = engine.process(request())
    assert all(r.action != "schedule_cleaning" for r in rows)
    assert not engine.turnaround.target_rooms


def test_completion_newer_than_dirty_telemetry_does_not_duplicate_cleaning_or_mutate_state():
    repo = repository()
    original = deepcopy(repo.states)
    detector = TurnaroundDetector(EngineConfig())
    calendar = ResourceCalendar()
    calendar.last_human_completion = {"R101": "2026-01-06T09:10:00Z"}
    calendar.last_dirty_trigger = {}
    event = request(t=LATER)
    rows = detector.detect(event, repo, calendar, BUILDING).candidates
    assert [c.action for c in rows] == ["defer_action"]
    assert "completed after" in rows[0].reason
    assert repo.states == original


def test_later_checkout_allows_new_turnover_despite_earlier_completion():
    repo = repository()
    calendar = ResourceCalendar()
    calendar.last_human_completion = {"R101": "2026-01-06T09:10:00Z"}
    calendar.last_dirty_trigger = {"R101": "2026-01-06T09:12:00Z"}
    rows = TurnaroundDetector(EngineConfig()).detect(request(t=LATER), repo, calendar, BUILDING).candidates
    assert any(c.action == "schedule_cleaning" for c in rows)


def test_s05_also_ignores_pre_completion_dirty_readings():
    repo = surge_repo()
    calendar = ResourceCalendar()
    calendar.last_human_completion = {"R101": "2026-01-06T08:59:00Z"}
    calendar.last_dirty_trigger = {}
    # Last sensor value for R101 is still dirty from before the completion.
    repo.states["R101"] = [state(t="2026-01-06T08:55:00Z")]
    rows = SurgeDetector(EngineConfig()).detect(tick(), repo, full_building(), calendar).candidates
    assert not any(c.asset == "R101" and c.action == "schedule_cleaning" for c in rows)
    assert any(c.asset == "R102" and c.action == "schedule_cleaning" for c in rows)


def test_staff_invalidation_records_concrete_shift_evidence():
    repo = repository()
    repo.states["R101"] = [state(t=LATER)]
    repo.slots[0].update(status="sick", effective_status_asof="sick", status_known_utc=LATER)
    detector = TurnaroundDetector(EngineConfig())
    source = request()
    detector.pending[source.event_id] = source
    key = "turnaround:"+source.event_id
    detector.scheduled_jobs[key] = {"decision_id": "D1", "room_id": "R101", "resource_id": "CLEANER_1",
        "start_utc": "2026-01-06T10:00:00Z", "end_utc": "2026-01-06T10:45:00Z"}
    calendar = ResourceCalendar()
    calendar.reserve("CLEANER_1", "2026-01-06T10:00:00Z", "2026-01-06T10:45:00Z", "D1", NOW, room="R101")
    detector.detect(tick(LATER), repo, calendar, BUILDING)
    cancellation = calendar.cancellations[0]
    assert "CLEANER_1" in cancellation["reason"] and "09:15" in cancellation["reason"]
    assert any("staff_roster.csv:SH1" in ref and "sick" in ref for ref in cancellation["evidence"])


@pytest.mark.parametrize("role,worker", [("cleaner", "CLEANER_1"), ("robot", "ROBOT_1")])
def test_oncall_does_not_enter_a_room_with_observed_work_of_unknown_end(role, worker):
    now = "2026-01-06T15:00:00Z"
    repo = FakeGateway()
    repo.states["R101"] = [state(t=now)]
    calendar = ResourceCalendar()
    calendar.resource_roles = {r["resource_id"]: r["role"] for r in BUILDING["staff"]["resources"]}
    calendar.observe_start(worker, "R101", "STARTED", "2026-01-06T14:45:00Z", role=role)
    rows = TurnaroundDetector(EngineConfig()).detect(request(t=now, waiting=True), repo, calendar, BUILDING).candidates
    assert [c.action for c in rows] == ["notify_guest"]
    assert "future availability is not verified" in rows[0].reason
    assert any("STARTED" in ref for ref in rows[0].evidence)


def test_real_quickstart_r302_waiting_arrival_during_robot_work_never_proposes_oncall_entry():
    """Minimal factual regression from Quickstart E0001278–E0001281, Jan 24 12Z."""
    from common.contracts.schemas import NormalizedEventV1
    now = "2026-01-24T12:00:00Z"
    b = deepcopy(BUILDING)
    b["building"]["rooms"] = [{"id": r} for r in ["R302", "R105", "R210"]]
    b["staff"]["resources"] += [{"resource_id": "ROBOT_2", "role": "robot"},
                                   {"resource_id": "CLEANER_2", "role": "cleaner"}]
    repo = FakeGateway()
    repo.states["R302"] = [state(room="R302", t=now)]
    repo.events = [
        {"event_id": "E0001276", "timestamp_utc": "2026-01-24T11:45:00Z", "type": "checkout", "room_id": "R302", "booking_id": "BK000102"},
        {"event_id": "E0001278", "timestamp_utc": "2026-01-24T11:45:00Z", "type": "robot_floor_cleaning_started", "room_id": "R302", "resource_id": "ROBOT_2"},
    ]
    raw = [
        {"event_id": "E0001279", "type": "arrival_waiting_room", "room_id": "R302", "booking_id": "BK000085"},
        {"event_id": "E0001280", "type": "cleaning_completed", "room_id": "R105", "resource_id": "CLEANER_2"},
        {"event_id": "E0001281", "type": "cleaning_started", "room_id": "R210", "resource_id": "CLEANER_2"},
    ]
    engine = DecisionEngine(repo, b)
    records = []
    for row in raw:
        row = {**row, "timestamp_utc": now}
        event = NormalizedEventV1(event_id="raw:"+row["event_id"], timestamp_utc=now, available_at_utc=now,
            event_type="operational_event", source={"file": "events.jsonl", "record_id": row["event_id"], "timestamp_utc": now},
            entities={k: row[k] for k in ["room_id", "booking_id", "resource_id"] if k in row}, payload=row)
        records.extend(engine.process(event))
    records.extend(engine.process(tick(now)))
    assert not engine.rejections
    assert not any(r.action == "schedule_cleaning" and r.resource_id == "ONCALL_1" for r in records)
    assert any(r.action == "notify_guest" and r.affected_assets == ["R302"] for r in records)
