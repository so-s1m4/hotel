"""Human housekeeping observations share a work class with strict worker identity."""
import pytest

from common.contracts.schemas import NormalizedEventV1
from engine.planner import DecisionEngine
from engine.resources import ResourceCalendar, canonical_work_role
from tests.engine.fixtures import BUILDING, FakeGateway, request, state


START = "2026-01-06T09:10:00Z"
END = "2026-01-06T09:15:00Z"
LATE = "2026-01-06T09:30:00Z"


def operation(kind, ident, occurred, available, room, resource):
    return NormalizedEventV1(
        event_id="synthetic:" + ident,
        timestamp_utc=occurred,
        available_at_utc=available,
        event_type="operational_event",
        source={"file": "synthetic_events.jsonl", "record_id": ident,
                "timestamp_utc": occurred},
        entities={"room_id": room, "resource_id": resource},
        payload={"type": kind, "room_id": room, "resource_id": resource},
    )


def activity(engine):
    return [job for jobs in engine.calendar.observed_busy.values()
            for job in jobs.values()]


@pytest.mark.parametrize("start_resource,end_resource", [
    (None, "ONCALL_1"),
    ("ONCALL_1", None),
])
@pytest.mark.parametrize("late_start", [False, True], ids=["same-order", "end-before-late-start"])
def test_same_room_generic_and_oncall_cleaning_completion_match(
        start_resource, end_resource, late_start):
    engine = DecisionEngine(FakeGateway(), BUILDING)
    start = operation("cleaning_started", "START", START, START, "R101", start_resource)
    end = operation("cleaning_completed", "END", END, END, "R101", end_resource)

    if late_start:
        engine.process(end)
        engine.process(start.model_copy(update={"available_at_utc": LATE}))
    else:
        engine.process(start)
        engine.process(end)

    assert activity(engine) == []
    assert engine.calendar.resource_roles["ONCALL_1"] == "on_call_housekeeping"


@pytest.mark.parametrize("start_resource,end_resource", [
    (None, "ONCALL_1"),
    ("ONCALL_1", None),
])
@pytest.mark.parametrize("late_start", [False, True], ids=["same-order", "end-before-late-start"])
def test_completion_in_another_room_does_not_release_housekeeping(
        start_resource, end_resource, late_start):
    engine = DecisionEngine(FakeGateway(), BUILDING)
    start = operation("cleaning_started", "START", START, START, "R101", start_resource)
    end = operation("cleaning_completed", "END", END, END, "R102", end_resource)

    if late_start:
        engine.process(end)
        engine.process(start.model_copy(update={"available_at_utc": LATE}))
    else:
        engine.process(start)
        engine.process(end)

    assert len(activity(engine)) == 1
    assert activity(engine)[0]["room_id"] == "R101"


@pytest.mark.parametrize("late_start", [False, True], ids=["same-order", "end-before-late-start"])
def test_known_different_worker_completion_does_not_release_oncall_job(late_start):
    engine = DecisionEngine(FakeGateway(), BUILDING)
    start = operation("cleaning_started", "START", START, START, "R101", "ONCALL_1")
    end = operation("cleaning_completed", "END", END, END, "R101", "CLEANER_1")
    if late_start:
        engine.process(end)
        engine.process(start.model_copy(update={"available_at_utc": LATE}))
    else:
        engine.process(start)
        engine.process(end)
    assert activity(engine)[0]["resource_id"] == "ONCALL_1"


def test_earlier_completion_does_not_cover_later_oncall_start():
    engine = DecisionEngine(FakeGateway(), BUILDING)
    earlier = "2026-01-06T09:05:00Z"
    engine.process(operation("cleaning_completed", "END", earlier, earlier, "R101", None))
    engine.process(operation("cleaning_started", "START", START, LATE, "R101", "ONCALL_1"))
    assert activity(engine)[0]["resource_id"] == "ONCALL_1"


@pytest.mark.parametrize("late_start", [False, True], ids=["same-order", "end-before-late-start"])
def test_robot_completion_does_not_release_human_housekeeping(late_start):
    engine = DecisionEngine(FakeGateway(), BUILDING)
    start = operation("cleaning_started", "START", START, START, "R101", "ONCALL_1")
    end = operation("robot_floor_cleaning_completed", "ROBOT_END", END, END,
                    "R101", "ROBOT_1")
    if late_start:
        engine.process(end)
        engine.process(start.model_copy(update={"available_at_utc": LATE}))
    else:
        engine.process(start)
        engine.process(end)
    assert activity(engine)[0]["resource_id"] == "ONCALL_1"


def test_calendar_role_wildcards_and_resource_identity_are_preserved():
    assert canonical_work_role(None) is None
    assert canonical_work_role("robot") == "robot"
    calendar = ResourceCalendar()
    calendar.resource_roles = {"ONCALL_1": "on_call_housekeeping"}
    calendar.observe_start("ONCALL_1", "R101", "START", START, role="on_call_housekeeping")
    calendar.observe_end(None, "R101", END, role=None)
    assert not calendar.observed_busy

    calendar.observe_start("ONCALL_1", "R101", "START2", START, role="on_call_housekeeping")
    calendar.observe_end("CLEANER_1", "R101", END, role="cleaner")
    assert calendar.observed_busy["ONCALL_1"]["START2"]


def test_unidentified_human_cleaning_holds_cleaner_and_oncall_capacity_only():
    calendar = ResourceCalendar()
    calendar.resource_roles = {r["resource_id"]: r["role"] for r in BUILDING["staff"]["resources"]}
    calendar.observe_start(None, "R101", "START", START, role="cleaner")

    assert not calendar.available("CLEANER_1", "2026-01-06T10:00:00Z",
                                  "2026-01-06T10:45:00Z", START)
    assert not calendar.available("ONCALL_1", "2026-01-06T10:00:00Z",
                                  "2026-01-06T10:45:00Z", START)
    assert calendar.available("ROBOT_1", "2026-01-06T10:00:00Z",
                              "2026-01-06T10:45:00Z", START)
    assert not calendar.room_available("R101", "2026-01-06T10:00:00Z",
                                       "2026-01-06T10:45:00Z", START)
    assert calendar.room_available("R102", "2026-01-06T10:00:00Z",
                                   "2026-01-06T10:45:00Z", START)


def test_unknown_role_still_holds_only_its_room():
    calendar = ResourceCalendar()
    calendar.resource_roles = {r["resource_id"]: r["role"] for r in BUILDING["staff"]["resources"]}
    calendar.observe_start(None, "R101", "START", START, role=None)
    assert calendar.available("ONCALL_1", "2026-01-06T10:00:00Z",
                              "2026-01-06T10:45:00Z", START)
    assert not calendar.room_available("R101", "2026-01-06T10:00:00Z",
                                        "2026-01-06T10:45:00Z", START)


def add_future_hold(engine, resource, ident, room, start="2026-01-06T10:00:00Z",
                    end="2026-01-06T10:30:00Z"):
    engine.calendar.reserve_tentative(resource, start, end, ident, START, room=room)


def test_unidentified_human_start_cancels_housekeeping_and_room_conflicts_only():
    engine = DecisionEngine(FakeGateway(), BUILDING)
    add_future_hold(engine, "CLEANER_1", "CLEANER_OTHER_ROOM", "R102")
    add_future_hold(engine, "ONCALL_1", "ONCALL_OTHER_ROOM", "R103")
    add_future_hold(engine, "ROBOT_1", "ROBOT_OTHER_ROOM", "R104",
                    "2026-01-06T10:00:00Z", "2026-01-06T10:30:00Z")
    add_future_hold(engine, "ROBOT_1", "ROBOT_SAME_ROOM", "R101",
                    "2026-01-06T10:30:00Z", "2026-01-06T11:00:00Z")
    # An elapsed hold can remain in the calendar but must not be cancelled by
    # a new observation after its interval ended.
    engine.calendar.tentative_reservations.setdefault("ONCALL_1", []).append(
        ("2026-01-06T08:00:00Z", "2026-01-06T09:00:00Z", "ONCALL_EXPIRED"))

    engine._observe_activity(operation("cleaning_started", "START", START, START,
                                       "R101", None))

    cancelled = engine.calendar._cancelled_ids
    assert {"CLEANER_OTHER_ROOM", "ONCALL_OTHER_ROOM", "ROBOT_SAME_ROOM"} <= cancelled
    assert "ROBOT_OTHER_ROOM" not in cancelled
    assert "ONCALL_EXPIRED" not in cancelled


def test_known_cleaner_start_does_not_hold_or_cancel_oncall_elsewhere():
    engine = DecisionEngine(FakeGateway(), BUILDING)
    add_future_hold(engine, "ONCALL_1", "ONCALL_OTHER_ROOM", "R102")
    add_future_hold(engine, "CLEANER_1", "CLEANER_OTHER_ROOM", "R103")

    engine._observe_activity(operation("cleaning_started", "START", START, START,
                                       "R101", "CLEANER_1"))

    assert "CLEANER_OTHER_ROOM" in engine.calendar._cancelled_ids
    assert "ONCALL_OTHER_ROOM" not in engine.calendar._cancelled_ids
    assert engine.calendar.available("ONCALL_1", "2026-01-06T10:30:00Z",
                                     "2026-01-06T11:00:00Z", START)


def test_completed_delayed_oncall_start_does_not_cancel_new_pending_proposal():
    repo = FakeGateway()
    request_time = "2026-01-06T15:00:00Z"
    repo.states["R102"] = [state(room="R102", t=request_time)]
    engine = DecisionEngine(repo, BUILDING)
    rows = engine.process(request(t=request_time, room="R102", waiting=True))
    proposal = next(row for row in rows
                    if row.action == "schedule_cleaning" and row.resource_id == "ONCALL_1")
    assert engine.calendar.tentative_reservations["ONCALL_1"]

    completion_time = "2026-01-06T15:10:00Z"
    delayed_start_time = "2026-01-06T15:05:00Z"
    engine.process(operation("cleaning_completed", "GENERIC_END", completion_time,
                             completion_time, "R101", None))
    engine.process(operation("cleaning_started", "DELAYED_ONCALL_START", delayed_start_time,
                             "2026-01-06T15:30:00Z", "R101", "ONCALL_1"))

    assert proposal.event_id not in engine.calendar._cancelled_ids
    assert any(row[2] == proposal.event_id
               for row in engine.calendar.tentative_reservations["ONCALL_1"])
    assert not activity(engine)


def test_roomless_unassigned_human_start_does_not_block_or_cancel_capacity():
    engine = DecisionEngine(FakeGateway(), BUILDING)
    add_future_hold(engine, "ONCALL_1", "ONCALL_FUTURE", "R102")
    engine._observe_activity(operation("cleaning_started", "ROOMLESS_START", START, START,
                                       None, None))
    assert not activity(engine)
    assert "ONCALL_FUTURE" not in engine.calendar._cancelled_ids
    assert engine.calendar.available("ONCALL_1", "2026-01-06T10:30:00Z",
                                     "2026-01-06T11:00:00Z", START)
