"""Adversarial regressions found independently of the implementation's happy paths."""
from copy import deepcopy
from datetime import timedelta

import pytest

from engine.core import Candidate, instant, utc
from engine.executor import MockExecutor
from engine.resources import ResourceCalendar, scheduled
from engine.safety import SafetyViolation, validate_safety
from tests.engine.fixtures import BUILDING, state, slot
from tests.engine.test_extended import full_building, battery_repo

NOW = "2026-01-06T09:00:00Z"
END = "2026-01-06T09:45:00Z"


def proposal(action="schedule_cleaning", *, asset="R101", resource="CLEANER_1", human=False, consent=False):
    return Candidate("adversarial", asset, "test", action, "Actual evidence", ["events.jsonl:E1"],
                     "Verify actual outcome", "Propose a safe action", resource=resource,
                     human=human, consent=consent).record(NOW, "audit:episode")


def context(**overrides):
    return {"building": deepcopy(BUILDING), "room": state(), "calendar": ResourceCalendar(),
            "slot": slot(), "start_utc": NOW, "end_utc": END, **overrides}


@pytest.mark.parametrize("action", ["schedule_cleaning", "dispatch_robot"])
def test_an_action_cannot_execute_without_an_assigned_worker(action):
    with pytest.raises(SafetyViolation, match="resource assignment"):
        validate_safety(proposal(action, resource=None), context(task="floor_cleaning"))


@pytest.mark.parametrize("action", ["release_room", "schedule_cleaning", "adjust_room_setpoint"])
def test_room_controls_cannot_target_an_asset_or_multiple_rooms_with_one_snapshot(action):
    for assets in [["BUILDING"], ["R101", "R102"]]:
        row = proposal(action, resource=None if action != "schedule_cleaning" else "CLEANER_1")
        row.affected_assets = assets
        with pytest.raises(SafetyViolation, match="exactly one affected room"):
            validate_safety(row, context(room=state(cleaning="clean", blocked=False, inspection_required=False), setpoint_c=21))


@pytest.mark.parametrize("action", ["request_inspection", "dispatch_technician"])
def test_entry_false_is_not_an_escape_hatch_for_occupied_room_consent(action):
    row = proposal(action, resource=None, human=True)
    with pytest.raises(SafetyViolation, match="guest consent"):
        validate_safety(row, context(room=state(occupancy="occupied"), entry_required=False))


def test_pending_technician_proposal_cannot_become_unassigned_execution():
    executor = MockExecutor()
    row = proposal("dispatch_technician", asset="HP1", resource=None, human=True)
    ctx = {"building": BUILDING, "calendar": ResourceCalendar()}
    assert executor.execute(row, ctx)["status"] == "pending_human_approval"
    with pytest.raises(SafetyViolation, match="resource assignment"):
        executor.execute(row, {**ctx, "human_approval_granted": True})
    assert len(executor.audit) == 1


def test_pending_approval_is_bound_to_the_exact_proposed_command_parameters():
    executor = MockExecutor()
    row = proposal(human=True)
    ctx = context()
    executor.execute(row, ctx)
    with pytest.raises(SafetyViolation, match="command parameters"):
        executor.execute(row, {**ctx, "human_approval_granted": True, "end_utc": "2026-01-06T10:00:00Z"})
    assert not any(ctx["calendar"].reservations.values())


def test_default_room_entry_also_reserves_the_room_not_just_the_worker():
    executor = MockExecutor()
    ctx = context()  # deliberately omit entry_required
    row = proposal()
    executor.execute(row, ctx)
    assert ctx["calendar"].room_reservations["R101"] == [(NOW, END, row.event_id)]


def test_future_jobs_are_audited_as_scheduled_without_applied_effects():
    executor = MockExecutor()
    ctx = context(start_utc="2026-01-06T10:00:00Z", end_utc="2026-01-06T10:45:00Z")
    audit = executor.execute(proposal(), ctx)
    assert audit["phase"] == "scheduled" and audit["effects_applied"] is False
    assert audit["observed_history_changed"] is False


def test_granting_old_decision_later_requires_fresh_replanning():
    executor = MockExecutor()
    row = proposal(human=True)
    ctx = context()
    executor.execute(row, ctx)
    with pytest.raises(SafetyViolation, match="replanned"):
        executor.execute(row, {**ctx, "human_approval_granted": True, "execution_utc": "2026-01-06T10:00:00Z"})


def test_invalid_context_rejects_cleanly_instead_of_crashing_the_replay():
    for ctx in [context(slot={}), context(building={"building": None}), context(room=None)]:
        with pytest.raises(SafetyViolation):
            validate_safety(proposal(), ctx)


def test_mutated_truthy_string_does_not_count_as_human_approval_requirement():
    row = proposal("order_service", resource=None, human=True)
    row.requires_human_approval = "false"
    with pytest.raises(SafetyViolation, match="Invalid decision contract"):
        validate_safety(row, context())


def test_observed_work_blocks_worker_and_room_until_matching_actual_end():
    calendar = ResourceCalendar()
    calendar.observe_start("CLEANER_1", "R101", "raw:start", NOW)
    future = "2026-01-07T09:00:00Z"
    assert not calendar.available("CLEANER_1", future, "2026-01-07T10:00:00Z", NOW)
    assert not calendar.room_available("R101", NOW, END, NOW)
    calendar.observe_end("CLEANER_1", "R102", END)  # wrong room cannot clear it
    assert not calendar.available("CLEANER_1", END, "2026-01-06T10:45:00Z", END)
    calendar.observe_end("CLEANER_1", "R101", END)
    assert calendar.available("CLEANER_1", END, "2026-01-06T10:45:00Z", END)
    assert calendar.room_available("R101", END, "2026-01-06T10:45:00Z", END)


def test_unknown_performer_still_blocks_the_observed_work_room():
    calendar = ResourceCalendar()
    calendar.observe_start(None, "R101", "raw:unknown", NOW)
    assert not calendar.room_available("R101", NOW, END, NOW)
    calendar.observe_end(None, "R101", END)
    assert calendar.room_available("R101", END, "2026-01-06T10:45:00Z", END)


@pytest.mark.parametrize("start,end", [(NOW, NOW), (END, NOW), ("2026-01-06T08:00:00Z", END)])
def test_calendar_rejects_empty_negative_and_past_reservations(start, end):
    with pytest.raises(ValueError):
        ResourceCalendar().reserve("CLEANER_1", start, end, "D", NOW)


@pytest.mark.parametrize("amount", [-1, 0, float("nan"), float("inf")])
def test_capacity_reservations_cannot_use_negative_or_nonfinite_volume(amount):
    calendar = ResourceCalendar()
    assert not calendar.capacity_available("WASTE_MAIN", 0, 100, amount)
    with pytest.raises(ValueError):
        calendar.reserve_volume("WASTE_MAIN", amount, "D")


def test_volume_reservation_is_idempotent_and_cannot_change_destination():
    calendar = ResourceCalendar()
    calendar.reserve_volume("WASTE_MAIN", 20, "D")
    calendar.reserve_volume("WASTE_MAIN", 20, "D")
    assert calendar.volume_reservations["WASTE_MAIN"] == [("D", 20)]
    with pytest.raises(ValueError):
        calendar.reserve_volume("WASTE_MAIN", 30, "D")
    with pytest.raises(ValueError):
        calendar.reserve_volume("OTHER", 20, "D")


def test_known_absence_cannot_be_hidden_by_a_conflicting_effective_status():
    assert not scheduled(slot(status="sick", effective_status_asof="scheduled"), NOW)
    # A later absence must not contaminate a legitimately known earlier plan.
    assert scheduled(slot(status="sick", effective_status_asof="scheduled", status_known_utc="2026-01-07T09:00:00Z"), NOW)


def test_battery_dispatch_rejects_old_soc_snapshot_even_inside_old_tolerance():
    building = full_building()
    battery = next(a for a in building["building"]["assets"] if a["id"] == "BAT1")
    row = proposal("optimize_energy", asset="BAT1", resource=None)
    observed = battery_repo().measurements["energy.csv"][0]
    observed["timestamp_utc"] = "2026-01-06T08:45:00Z"
    ctx = {"building": building, "battery": battery, "observed_energy": observed,
           "battery_power_kw": 0, "duration_minutes": 15, "expected_soc_kwh": 50,
           "strategy": "peak_discharge", "calendar": ResourceCalendar()}
    with pytest.raises(SafetyViolation, match="Fresh observed battery"):
        validate_safety(row, ctx)


def test_cancellation_is_append_only_and_references_the_withdrawn_decision():
    calendar = ResourceCalendar()
    calendar.reserve("CLEANER_1", NOW, END, "D", NOW, room="R101")
    calendar.current_time = "2026-01-06T09:15:00Z"
    calendar.cancel("D", reason="Observed human completion made the future job unnecessary")
    calendar.cancel("D")
    assert calendar.cancellations == [{"decision_id": "D", "timestamp_utc": calendar.current_time,
                                       "reason": "Observed human completion made the future job unnecessary"}]
    assert not calendar.reservations["CLEANER_1"] and not calendar.room_reservations["R101"]


def test_unidentified_cleaner_holds_known_cleaner_capacity_until_matching_completion():
    calendar = ResourceCalendar()
    calendar.resource_roles = {"CLEANER_1": "cleaner", "CLEANER_2": "cleaner", "ROBOT_1": "robot"}
    calendar.observe_start(None, "R101", "raw:unassigned", NOW, role="cleaner")
    assert not calendar.available("CLEANER_1", NOW, END, NOW)
    assert not calendar.available("CLEANER_2", NOW, END, NOW)
    assert calendar.available("ROBOT_1", NOW, END, NOW)
    job = next(iter(calendar.observed_busy["__unassigned__:R101"].values()))
    assert "no identified performer" in job["uncertainty_reason"]
    calendar.observe_end("ROBOT_1", "R101", END)  # robot completion cannot certify human work
    assert not calendar.available("CLEANER_1", END, "2026-01-06T10:00:00Z", END)
    calendar.observe_end("CLEANER_2", "R101", END)
    assert calendar.available("CLEANER_1", END, "2026-01-06T10:00:00Z", END)


def test_unknown_robot_work_blocks_only_robot_role_and_role_specific_end_is_supported():
    calendar = ResourceCalendar()
    calendar.resource_roles = {"CLEANER_1": "cleaner", "ROBOT_1": "robot"}
    calendar.observe_start(None, "R101", "raw:unknown-robot", NOW, role="robot")
    assert not calendar.available("ROBOT_1", NOW, END, NOW)
    assert calendar.available("CLEANER_1", NOW, END, NOW)
    calendar.observe_end(None, "R101", END, role="cleaner")
    assert not calendar.available("ROBOT_1", END, "2026-01-06T10:00:00Z", END)
    calendar.observe_end(None, "R101", END, role="robot")
    assert calendar.available("ROBOT_1", END, "2026-01-06T10:00:00Z", END)


def test_observed_identity_cannot_claim_a_conflicting_role():
    calendar = ResourceCalendar()
    calendar.resource_roles = {"ROBOT_1": "robot"}
    with pytest.raises(ValueError, match="role conflicts"):
        calendar.observe_start("ROBOT_1", "R101", "raw:invalid", NOW, role="cleaner")


def test_cancellation_marker_is_structured_and_immutable_under_reused_id():
    executor = MockExecutor()
    row = proposal("defer_action", resource=None)
    ctx = {"building": BUILDING, "calendar": ResourceCalendar(), "cancelled_decision_id": "prior:job"}
    audit = executor.execute(row, ctx)
    assert audit["parameters"]["cancelled_decision_id"] == "prior:job"
    with pytest.raises(SafetyViolation, match="command parameters"):
        executor.execute(row, {**ctx, "cancelled_decision_id": "different:job"})
    assert len(executor.audit) == 1


def oncall_proposal(room, start="2026-01-06T15:45:00Z", end="2026-01-06T16:30:00Z"):
    now = "2026-01-06T15:00:00Z"
    row = Candidate("pending:"+room, room, "room_turnaround", "schedule_cleaning", "Known waiting guest, available on-call window",
                    ["events.jsonl:waiting:"+room], "Verify actual readiness", "Pending on-call cleaning",
                    resource="ONCALL_1", human=True).record(now, "pending:"+room)
    ctx = {"building": deepcopy(BUILDING), "room": state(room=room, t=now),
           "start_utc": start, "end_utc": end, "entry_required": True,
           "oncall": True, "waiting_guest": True, "notable_cost": True}
    return row, ctx


def test_pending_oncall_approval_holds_capacity_without_claiming_execution():
    executor, calendar = MockExecutor(), ResourceCalendar()
    first, ctx = oncall_proposal("R101")
    ctx["calendar"] = calendar
    result = executor.execute(first, ctx)
    assert result["status"] == "pending_human_approval" and result["effects_applied"] is False
    assert not any(calendar.reservations.values())
    assert calendar.tentative_reservations["ONCALL_1"] == [(ctx["start_utc"], ctx["end_utc"], first.event_id)]
    second, other = oncall_proposal("R102")
    other["calendar"] = calendar
    with pytest.raises(SafetyViolation, match="reserved"):
        executor.execute(second, other)
    assert len(executor.audit) == 1


def test_second_waiting_room_can_use_a_later_nonoverlapping_oncall_hold():
    from engine.resources import oncall_slot
    executor, calendar = MockExecutor(), ResourceCalendar()
    first, ctx = oncall_proposal("R101")
    ctx["calendar"] = calendar
    executor.execute(first, ctx)
    start, end = oncall_slot(first.timestamp_utc, "2026-01-06T20:00:00Z", 45, calendar)
    assert start == ctx["end_utc"]
    second, other = oncall_proposal("R102", start, end)
    other["calendar"] = calendar
    assert executor.execute(second, other)["status"] == "pending_human_approval"
    rows = sorted(calendar.tentative_reservations["ONCALL_1"])
    assert len(rows) == 2 and rows[0][1] <= rows[1][0]


def test_approval_upgrades_its_own_exact_hold_and_preserves_other_pending_holds():
    executor, calendar = MockExecutor(), ResourceCalendar()
    first, ctx = oncall_proposal("R101")
    ctx["calendar"] = calendar
    executor.execute(first, ctx)
    second, other = oncall_proposal("R102", "2026-01-06T16:30:00Z", "2026-01-06T17:15:00Z")
    other["calendar"] = calendar
    executor.execute(second, other)
    result = executor.execute(first, {**ctx, "human_approval_granted": True})
    assert result["status"] == "simulated" and result["phase"] == "scheduled"
    assert calendar.reservations["ONCALL_1"] == [(ctx["start_utc"], ctx["end_utc"], first.event_id)]
    assert calendar.room_reservations["R101"] == calendar.reservations["ONCALL_1"]
    assert all(row[2] != first.event_id for row in calendar.tentative_reservations["ONCALL_1"])
    assert calendar.tentative_reservations["ONCALL_1"][0][2] == second.event_id


def test_cancelling_pending_approval_frees_worker_room_and_logs_withdrawal():
    executor, calendar = MockExecutor(), ResourceCalendar()
    first, ctx = oncall_proposal("R101")
    ctx["calendar"] = calendar
    executor.execute(first, ctx)
    calendar.current_time = "2026-01-06T15:05:00Z"
    calendar.cancel(first.event_id, reason="Guest no longer waiting")
    assert calendar.available("ONCALL_1", ctx["start_utc"], ctx["end_utc"], calendar.current_time)
    assert calendar.room_available("R101", ctx["start_utc"], ctx["end_utc"], calendar.current_time)
    assert calendar.cancellations[-1]["decision_id"] == first.event_id
    assert not any(calendar.reservations.values())
    assert not any(calendar.tentative_reservations.values())


def test_combined_calendar_helpers_expose_tentative_holds_to_planners():
    calendar = ResourceCalendar()
    calendar.reserve_tentative("CLEANER_1", NOW, END, "D", NOW, room="R101")
    assert list(calendar.iter_reservations()) == [("CLEANER_1", [(NOW, END, "D")])]
    assert calendar.reservations_for("CLEANER_1") == calendar.room_jobs("R101") == [(NOW, END, "D")]
    assert calendar.reservations_for("CLEANER_1", include_tentative=False) == []
    assert calendar.room_jobs("R101", include_tentative=False) == []
