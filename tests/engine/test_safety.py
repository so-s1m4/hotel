from copy import deepcopy

import pytest

from engine.core import Candidate
from engine.executor import MockExecutor
from engine.resources import ResourceCalendar
from engine.safety import SafetyViolation, validate_safety
from tests.engine.fixtures import BUILDING, state, slot

NOW = "2026-01-06T09:00:00Z"


def decision(action="flag_anomaly", human=False, consent=False, resource=None):
    return Candidate("case", "R101", "test", action, "Observed evidence", ["events.jsonl:E1"],
                     "Verify observed outcome", "No real effects", human=human, consent=consent,
                     resource=resource).record(NOW, "episode:test")


def context(**kwargs):
    return {"building": deepcopy(BUILDING), "room": state(cleaning="clean"),
            "calendar": ResourceCalendar(), **kwargs}


@pytest.mark.parametrize("action", ["dispatch_technician", "order_service", "block_room", "reassign_room"])
def test_human_approval_cannot_be_omitted(action):
    with pytest.raises(SafetyViolation, match="Human approval"):
        validate_safety(decision(action), context())
    validate_safety(decision(action, human=True), context())


@pytest.mark.parametrize("occupancy", ["occupied", "unknown"])
def test_occupied_or_unknown_entry_requires_both_flags(occupancy):
    ctx = context(room=state(occupancy=occupancy, cleaning="clean"), entry_required=True)
    with pytest.raises(SafetyViolation, match="consent"):
        validate_safety(decision("request_inspection", human=True), ctx)
    validate_safety(decision("request_inspection", human=True, consent=True), ctx)


def test_safety_intervention_needs_consent_even_in_vacant_room():
    with pytest.raises(SafetyViolation, match="consent"):
        validate_safety(decision("request_inspection", human=True), context(water_shutoff=True))


@pytest.mark.parametrize("changes", [{"occupancy": "occupied"}, {"housekeeping_state": "dirty"}, {"blocked": True}, {"blocked": None}, {"inspection_required": True}, {"inspection_required": None}])
def test_release_requires_all_four_independent_checks(changes):
    room = state(cleaning="clean", blocked=False, inspection_required=False)
    room.update(changes)
    with pytest.raises(SafetyViolation):
        validate_safety(decision("release_room"), context(room=room))


def test_release_only_after_explicit_clean_vacant_unblocked_inspection_checks():
    validate_safety(decision("release_room"), context(room=state(cleaning="clean", blocked=False,
                   inspection_required=True, inspection_passed=True)))


@pytest.mark.parametrize("target,occupancy", [(19, "occupied"), (24, "occupied"), (None, "occupied"), (17, "vacant"), (21, "unknown")])
def test_setpoint_rejects_unsafe_or_unknown_control(target, occupancy):
    with pytest.raises(SafetyViolation):
        validate_safety(decision("adjust_room_setpoint"), context(setpoint_c=target, room=state(occupancy=occupancy)))


def test_stale_and_future_room_observations_do_not_authorize_control():
    for t in ["2026-01-05T09:00:00Z", "2026-01-07T09:00:00Z"]:
        with pytest.raises(SafetyViolation):
            validate_safety(decision("adjust_room_setpoint"), context(setpoint_c=21, room=state(t=t, occupancy="occupied")))


def test_unknown_action_or_asset_fails_closed():
    with pytest.raises(SafetyViolation, match="Unknown action"):
        validate_safety(decision("shutoff_water"), context())
    row = decision()
    row.affected_assets = ["MADE_UP"]
    with pytest.raises(SafetyViolation, match="asset"):
        validate_safety(row, context())


def test_robot_cannot_clean_bathroom_or_release_room():
    row = decision("dispatch_robot", resource="ROBOT_1")
    ctx = context(task="bathroom_cleaning", slot=slot(resource="ROBOT_1", role="robot"), start_utc=NOW, end_utc="2026-01-06T09:20:00Z")
    with pytest.raises(SafetyViolation, match="capability"):
        validate_safety(row, ctx)
    with pytest.raises(SafetyViolation, match="Robots"):
        validate_safety(decision("release_room", resource="ROBOT_1"), context(room=state(cleaning="clean", blocked=False, inspection_required=False)))


def test_cleaning_resource_must_have_actual_shift_and_no_overlaps():
    row = decision("schedule_cleaning", resource="CLEANER_1")
    ctx = context(slot=slot(), start_utc=NOW, end_utc="2026-01-06T09:45:00Z")
    validate_safety(row, ctx)
    ctx["calendar"].reserve("CLEANER_1", NOW, "2026-01-06T09:45:00Z", "other", NOW)
    with pytest.raises(SafetyViolation, match="reserved"):
        validate_safety(row, ctx)
    ctx = context(slot=slot(end="2026-01-06T09:30:00Z"), start_utc=NOW, end_utc="2026-01-06T09:45:00Z")
    with pytest.raises(SafetyViolation, match="shift"):
        validate_safety(row, ctx)


def test_executor_never_treats_requirement_as_permission():
    executor = MockExecutor()
    row = decision("request_inspection", human=True, consent=True)
    ctx = context(room=state(occupancy="occupied"), entry_required=True)
    assert executor.execute(row, ctx)["status"] == "pending_human_approval"
    assert executor.execute(row, {**ctx, "human_approval_granted": True})["status"] == "pending_guest_consent"
    output = executor.execute(row, {**ctx, "human_approval_granted": True, "guest_consent_granted": True})
    assert output["status"] == "simulated" and output["observed_history_changed"] is False


def test_executor_resource_reservation_is_idempotent_and_messages_not_delivered():
    executor = MockExecutor()
    row = decision("schedule_cleaning", resource="CLEANER_1")
    ctx = context(slot=slot(), start_utc=NOW, end_utc="2026-01-06T09:45:00Z")
    assert executor.execute(row, ctx) == executor.execute(row, ctx)
    assert len(ctx["calendar"].reservations["CLEANER_1"]) == 1
    assert executor.execute(decision("notify_guest"), context())["status"] == "simulated_message_proposed"


def test_two_different_workers_cannot_reserve_same_room_simultaneously():
    ctx = context(slot=slot(), start_utc=NOW, end_utc="2026-01-06T09:45:00Z", entry_required=True)
    ctx["calendar"].reserve("CLEANER_2", NOW, "2026-01-06T09:45:00Z", "other", NOW, room="R101")
    with pytest.raises(SafetyViolation, match="Room already"):
        validate_safety(decision("schedule_cleaning", resource="CLEANER_1"), ctx)


def test_executor_rejects_reusing_decision_id_with_different_content():
    executor = MockExecutor()
    row = decision()
    executor.execute(row, context())
    changed = row.model_copy(update={"reason": "Changed after execution"})
    with pytest.raises(SafetyViolation, match="Conflicting"):
        executor.execute(changed, context())
