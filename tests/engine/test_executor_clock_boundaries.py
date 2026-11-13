"""A pending command cannot execute against a clock its origin already passed."""
from copy import deepcopy
import pytest
from engine.executor import MockExecutor
from engine.resources import ResourceCalendar
from engine.safety import SafetyViolation
from tests.engine.test_safety_audit import oncall_proposal


@pytest.mark.parametrize('clock', ['2026-01-06T15:30:00Z', '2026-01-06T17:00:00Z'])
@pytest.mark.parametrize('replacement', [False, True])
@pytest.mark.parametrize('explicit_old', [False, True])
def test_pending_approval_cannot_rewind_original_calendar(clock, replacement, explicit_old):
    executor, original = MockExecutor(), ResourceCalendar()
    row, ctx = oncall_proposal('R101')
    ctx['calendar'] = original
    original.current_time = row.timestamp_utc
    executor.execute(row, ctx)
    original.current_time = clock
    retry = {**ctx, 'human_approval_granted': True}
    if replacement:
        retry['calendar'] = ResourceCalendar()
        retry['calendar'].current_time = row.timestamp_utc
    if explicit_old:
        retry['execution_utc'] = row.timestamp_utc
    audit = deepcopy(executor.audit)
    holds = deepcopy(original.tentative_reservations)
    with pytest.raises(SafetyViolation, match='replanned'):
        executor.execute(row, retry)
    assert executor.audit == audit
    assert original.tentative_reservations == holds
    assert not any(original.reservations.values())
    assert row.event_id not in executor._completed
    assert executor._submitted_calendars[row.event_id][0] is original


def test_fresh_replan_at_current_clock_and_completed_receipt_remain_valid():
    executor, calendar = MockExecutor(), ResourceCalendar()
    row, ctx = oncall_proposal('R101')
    ctx['calendar'] = calendar
    calendar.current_time = row.timestamp_utc
    receipt = executor.execute(row, {**ctx, 'human_approval_granted': True})
    calendar.current_time = '2026-01-06T17:00:00Z'
    assert executor.execute(row, {**ctx, 'human_approval_granted': True}) == receipt
    assert len(executor.audit) == 1
    calendar.cancel(row.event_id)
    with pytest.raises(SafetyViolation, match='Cancelled'):
        executor.execute(row, {**ctx, 'human_approval_granted': True})
    # Replanning uses a new identity and newly observed room state, with a new
    # travel window; the earlier receipt is not treated as new physical work.
    fresh, fresh_ctx = oncall_proposal('R101', start='2026-01-06T17:45:00Z', end='2026-01-06T18:30:00Z')
    fresh = fresh.model_copy(update={'event_id': row.event_id + ':new', 'timestamp_utc': calendar.current_time})
    fresh_ctx['room']['last_observed_utc'] = calendar.current_time
    assert executor.execute(fresh, {**fresh_ctx, 'calendar': calendar, 'human_approval_granted': True})['status'] == 'simulated'
