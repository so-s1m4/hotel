"""Independent integration and alias controls for composite command failures."""
from copy import deepcopy

import pytest

from engine.planner import DecisionEngine
from engine.resources import ResourceCalendar
from engine.safety import SafetyViolation
from tests.engine.fixtures import slot, state, tick, request
from tests.engine.test_extended import full_building, logistics_repo
from tests.engine.test_executor_atomic_reservations import (
    CAPACITY_FIELDS, _command, _calendar_state, _executor_state,
)
from tests.engine.test_safety_audit import oncall_proposal
from engine.executor import MockExecutor


@pytest.mark.parametrize('pending', [False, True])
@pytest.mark.parametrize('write_volume', [False, True])
def test_failed_volume_write_preserves_preexisting_list_aliases_and_submission_identity(monkeypatch, pending, write_volume):
    executor, calendar, decision, context = _command(human=pending)
    if pending:
        executor.execute(decision, context)
    # Unlike value-only snapshots, these aliases detect silent replacement of a
    # pending hold or of somebody else's ledger rows after inner rollback.
    map_refs = {name: getattr(calendar, name) for name in CAPACITY_FIELDS}
    row_refs = {(name, key): rows for name, mapping in map_refs.items() for key, rows in mapping.items()}
    audit_ref, submitted_ref = executor.audit, executor._submitted
    calendar_metadata = {key: (values, tuple(values)) for key, values in executor._submitted_calendars.items()}
    before = _calendar_state(calendar), _executor_state(executor)
    original = calendar.reserve_volume

    def fail(destination, liters, identifier):
        assert any(row[2] == identifier for row in calendar.reservations['ROBOT_1'])
        if write_volume:
            original(destination, liters, identifier)
        raise SafetyViolation('independent composite failure')

    with monkeypatch.context() as patch:
        patch.setattr(calendar, 'reserve_volume', fail)
        with pytest.raises(SafetyViolation, match='independent composite failure'):
            executor.execute(decision, {**context, 'human_approval_granted': True})
    assert (_calendar_state(calendar), _executor_state(executor)) == before
    assert executor.audit is audit_ref and executor._submitted is submitted_ref
    for name, mapping in map_refs.items():
        assert getattr(calendar, name) is mapping
    for (name, key), rows in row_refs.items():
        assert getattr(calendar, name)[key] is rows
    for key, (values, calendars) in calendar_metadata.items():
        assert executor._submitted_calendars[key] is values
        assert all(actual is expected for actual, expected in zip(values, calendars))
    assert executor.execute(decision, {**context, 'human_approval_granted': True})['status'] == 'simulated'


@pytest.mark.parametrize('completed', [False, True])
def test_withdrawal_of_any_previously_used_calendar_is_terminal_and_fresh_id_is_valid(completed):
    executor = MockExecutor()
    decision, context = oncall_proposal('R101')
    original, alternate = ResourceCalendar(), ResourceCalendar()
    for calendar in (original, alternate):
        calendar.current_time = decision.timestamp_utc
    executor.execute(decision, {**context, 'calendar': original})
    # The API permits the same command on another calendar. Withdrawal in the
    # earlier calendar must still veto the latest calendar's approval/cache.
    executor.execute(decision, {**context, 'calendar': alternate, 'human_approval_granted': completed})
    metadata = executor._submitted_calendars[decision.event_id]
    original.cancel(decision.event_id, reason='withdraw original proposal')
    before = deepcopy(executor.audit)
    for retry in (original, alternate, ResourceCalendar()):
        with pytest.raises(SafetyViolation, match='Cancelled'):
            executor.execute(decision, {**context, 'calendar': retry, 'human_approval_granted': True})
    assert executor.audit == before
    assert executor._submitted_calendars[decision.event_id] is metadata
    assert metadata[0] is original and metadata[1] is alternate
    # Capacity in the original calendar is genuinely reusable by a new proposal.
    fresh = decision.model_copy(update={'event_id': decision.event_id + ':fresh'})
    assert executor.execute(fresh, {**context, 'calendar': original, 'human_approval_granted': True})['status'] == 'simulated'


def test_candidate_failure_keeps_other_accepted_work_and_event_commit(monkeypatch):
    repo = logistics_repo()
    repo.states['R101'] = [state()]
    repo.slots.append(slot())
    engine = DecisionEngine(repo, full_building())
    # Persist a request first so a subsequent clock tick proposes housekeeping
    # before the independently failing logistics command in the same event.
    repo.slots = [row for row in repo.slots if row['resource_id'] != 'CLEANER_1']
    engine.process(request())
    repo.slots.append(slot())
    calendar, executor = engine.calendar, engine.executor
    reserve_volume = ResourceCalendar.reserve_volume
    failed_ids = []

    def fail(target, destination, liters, identifier):
        reserve_volume(target, destination, liters, identifier)
        if target is not calendar:
            return
        failed_ids.append(identifier)
        assert any(row[2] == identifier for row in calendar.reservations['ROBOT_1'])
        raise SafetyViolation('candidate destination rejected after reservation')

    monkeypatch.setattr(ResourceCalendar, 'reserve_volume', fail)
    event = tick()
    rows = engine.process(event)
    cleaning = next(row for row in rows if row.action == 'schedule_cleaning')
    assert calendar is engine.calendar and executor is engine.executor
    assert failed_ids and any('candidate destination rejected' in row['reason'] for row in engine.rejections)
    assert any(row[2] == cleaning.event_id for row in calendar.reservations['CLEANER_1'])
    assert any(row['decision_id'] == cleaning.event_id for row in executor.audit)
    assert executor._submitted_calendars[cleaning.event_id][0] is calendar
    for identifier in failed_ids:
        assert identifier not in executor._submitted
        assert identifier not in executor._completed
        assert identifier not in executor._submitted_calendars
        assert not any(row['decision_id'] == identifier for row in executor.audit)
        assert not any(row[2] == identifier for _, entries in calendar.iter_reservations() for row in entries)
        assert not any(row[0] == identifier for entries in calendar.volume_reservations.values() for row in entries)
    assert engine.event_count == 2
    assert engine.process(event) == []
