"""A withdrawn command cannot be revived by approval or a cached result."""
from copy import deepcopy

import pytest

from engine.executor import MockExecutor
from engine.resources import ResourceCalendar
from engine.safety import SafetyViolation
from tests.engine.test_safety_audit import oncall_proposal


def _oncall_command():
    executor, calendar = MockExecutor(), ResourceCalendar()
    decision, context = oncall_proposal("R101")
    calendar.current_time = decision.timestamp_utc
    context["calendar"] = calendar
    return executor, calendar, decision, context


def _calendar_state(calendar):
    return deepcopy({name: getattr(calendar, name) for name in (
        "reservations", "room_reservations", "tentative_reservations",
        "tentative_room_reservations", "volume_reservations", "cancellations",
    )})


def _retry_context(context, calendar_mode):
    retry = dict(context)
    replacement = None
    if calendar_mode == "replaced":
        replacement = ResourceCalendar()
        retry["calendar"] = replacement
    elif calendar_mode == "omitted":
        retry.pop("calendar")
    return retry, replacement


def _withdraw(calendar, decision):
    calendar.cancel(decision.event_id, reason="Guest no longer waiting")
    assert calendar.cancellations == [{
        "decision_id": decision.event_id,
        "timestamp_utc": decision.timestamp_utc,
        "reason": "Guest no longer waiting",
    }]
    assert not calendar.reservations_for("ONCALL_1")
    assert not calendar.room_jobs("R101")
    # Repeating a withdrawal must neither duplicate nor replace its history.
    before = deepcopy(calendar.cancellations)
    calendar.cancel(decision.event_id, reason="Repeated withdrawal notification")
    assert calendar.cancellations == before


@pytest.mark.parametrize("calendar_mode", ["original", "replaced", "omitted"])
@pytest.mark.parametrize("approved", [False, True], ids=["pending-again", "approved"])
def test_withdrawn_pending_command_cannot_be_replayed(approved, calendar_mode):
    executor, calendar, decision, context = _oncall_command()
    pending = executor.execute(decision, context)
    assert pending["status"] == "pending_human_approval"
    assert calendar.reservations_for("ONCALL_1")
    _withdraw(calendar, decision)
    retry, replacement = _retry_context(context, calendar_mode)
    if approved:
        retry["human_approval_granted"] = True
    audit_before = deepcopy(executor.audit)
    calendar_before = _calendar_state(calendar)
    replacement_before = _calendar_state(replacement) if replacement else None
    decision_before = decision.model_dump()

    with pytest.raises(SafetyViolation, match="(?i)cancelled"):
        executor.execute(decision, retry)

    assert executor.audit == audit_before
    assert _calendar_state(calendar) == calendar_before
    if replacement:
        assert _calendar_state(replacement) == replacement_before
    assert decision.model_dump() == decision_before
    assert pending == audit_before[0]


@pytest.mark.parametrize("calendar_mode", ["original", "replaced", "omitted"])
def test_withdrawn_completed_command_cannot_return_cached_success(calendar_mode):
    executor, calendar, decision, context = _oncall_command()
    approved_context = {**context, "human_approval_granted": True}
    completed = executor.execute(decision, approved_context)
    assert completed["status"] == "simulated"
    assert calendar.reservations_for("ONCALL_1", include_tentative=False)
    _withdraw(calendar, decision)
    retry, replacement = _retry_context(approved_context, calendar_mode)
    audit_before = deepcopy(executor.audit)
    calendar_before = _calendar_state(calendar)
    replacement_before = _calendar_state(replacement) if replacement else None

    with pytest.raises(SafetyViolation, match="(?i)cancelled"):
        executor.execute(decision, retry)

    assert executor.audit == audit_before
    assert _calendar_state(calendar) == calendar_before
    if replacement:
        assert _calendar_state(replacement) == replacement_before
    assert completed == audit_before[0]


def test_unwithdrawn_completed_retry_remains_idempotent():
    executor, calendar, decision, context = _oncall_command()
    approved_context = {**context, "human_approval_granted": True}
    completed = executor.execute(decision, approved_context)
    audit_before = deepcopy(executor.audit)
    calendar_before = _calendar_state(calendar)

    assert executor.execute(decision, approved_context) == completed
    assert executor.audit == audit_before
    assert _calendar_state(calendar) == calendar_before
    assert len(executor.audit) == 1


@pytest.mark.parametrize("history", [None, [None], [{"decision_id": 42}]],
                         ids=["invalid-ledger", "invalid-entry", "invalid-decision-id"])
def test_malformed_cancellation_history_fails_cleanly_without_upgrading_hold(history):
    executor, calendar, decision, context = _oncall_command()
    assert executor.execute(decision, context)["status"] == "pending_human_approval"
    calendar.cancellations = deepcopy(history)
    audit_before = deepcopy(executor.audit)
    calendar_before = _calendar_state(calendar)

    with pytest.raises(SafetyViolation, match="Invalid safety context"):
        executor.execute(decision, {**context, "human_approval_granted": True})

    assert executor.audit == audit_before
    assert _calendar_state(calendar) == calendar_before


def test_fresh_id_replan_can_reuse_capacity_released_by_withdrawal():
    executor, calendar, decision, context = _oncall_command()
    assert executor.execute(decision, context)["status"] == "pending_human_approval"
    _withdraw(calendar, decision)
    cancellations_before = deepcopy(calendar.cancellations)
    # A distinct proposal can be re-evaluated at this same replay instant.
    replanned = deepcopy(decision)
    replanned.event_id += ":replanned"
    replanned.episode_id += ":replanned"

    assert executor.execute(replanned, context)["status"] == "pending_human_approval"
    approved = executor.execute(replanned, {**context, "human_approval_granted": True})
    assert approved["status"] == "simulated"
    assert calendar.reservations_for("ONCALL_1") == [
        (context["start_utc"], context["end_utc"], replanned.event_id)
    ]
    assert calendar.room_jobs("R101") == calendar.reservations_for("ONCALL_1")
    assert calendar.cancellations == cancellations_before
    assert [row["status"] for row in executor.audit] == [
        "pending_human_approval", "pending_human_approval", "simulated",
    ]
    assert all(row["effects_applied"] is False for row in executor.audit)
