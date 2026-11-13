"""On-call availability never grants equipment or technical capabilities."""
import pytest

from engine.executor import MockExecutor
from engine.planner import DecisionEngine
from engine.resources import ResourceCalendar
from engine.safety import SafetyViolation, validate_safety
from tests.engine.fixtures import BUILDING, FakeGateway, request, state
from tests.engine.test_safety_audit import oncall_proposal


@pytest.mark.parametrize('action', ['dispatch_robot', 'dispatch_technician'])
@pytest.mark.parametrize('granted', [False, True])
def test_oncall_cannot_be_relabelled_as_robot_or_technician(action, granted):
    row, context = oncall_proposal('R101')
    row.action = action
    calendar = context['calendar'] = ResourceCalendar()
    context.update(task='floor_cleaning', human_approval_granted=granted)
    executor = MockExecutor()
    with pytest.raises(SafetyViolation, match='role|robot resource'):
        executor.execute(row, context)
    assert not executor.audit
    assert not executor._submitted
    assert not any(calendar.reservations.values())
    assert not any(calendar.tentative_reservations.values())


@pytest.mark.parametrize('action', ['schedule_cleaning', 'request_inspection'])
def test_real_housekeeping_capabilities_keep_approval_and_hold_the_room(action):
    row, context = oncall_proposal('R101')
    row.action = action
    calendar = context['calendar'] = ResourceCalendar()
    executor = MockExecutor()
    pending = executor.execute(row, context)
    assert pending['status'] == 'pending_human_approval'
    assert pending['effects_applied'] is False
    assert not any(calendar.reservations.values())
    assert calendar.room_jobs('R101') == [(context['start_utc'], context['end_utc'], row.event_id)]
    accepted = executor.execute(row, {**context, 'human_approval_granted': True})
    assert accepted['status'] == 'simulated'
    assert accepted['phase'] == 'scheduled'
    assert accepted['effects_applied'] is False
    assert calendar.reservations['ONCALL_1'] == [(context['start_utc'], context['end_utc'], row.event_id)]


def test_oncall_catalog_cannot_be_reconfigured_to_turn_housekeeping_into_a_robot():
    row, context = oncall_proposal('R101')
    row.action = 'dispatch_robot'
    context.update(calendar=ResourceCalendar(), task='floor_cleaning')
    resource = next(r for r in context['building']['staff']['resources'] if r['resource_id'] == 'ONCALL_1')
    resource['role'] = 'robot'
    with pytest.raises(SafetyViolation, match='On-call resource must be housekeeping'):
        validate_safety(row, context)


@pytest.mark.parametrize('action', ['dispatch_robot', 'dispatch_technician'])
def test_engine_rejects_a_detector_that_assigns_technical_work_to_oncall(action, monkeypatch):
    now = '2026-01-06T15:00:00Z'
    repo = FakeGateway()
    repo.states['R101'] = [state(t=now)]
    engine = DecisionEngine(repo, BUILDING)
    detect = engine.turnaround.detect

    def wrong_capability(*args, **kwargs):
        result = detect(*args, **kwargs)
        for candidate in result.candidates:
            if candidate.action == 'schedule_cleaning':
                candidate.action = action
                candidate.context['task'] = 'floor_cleaning'
        return result

    monkeypatch.setattr(engine.turnaround, 'detect', wrong_capability)
    rows = engine.process(request(t=now, rid='waiting-oncall', waiting=True))
    assert not any(row.action == action for row in rows)
    assert any(rejection.get('action') == action and ('role' in rejection['reason'] or 'robot resource' in rejection['reason']) for rejection in engine.rejections)
    assert not any(engine.calendar.reservations.values())
    assert not any(engine.calendar.tentative_reservations.values())
    assert not any(record['action'] == action for record in engine.executor.audit)


def test_engine_still_proposes_legitimate_oncall_housekeeping():
    now = '2026-01-06T15:00:00Z'
    repo = FakeGateway()
    repo.states['R101'] = [state(t=now)]
    engine = DecisionEngine(repo, BUILDING)
    rows = engine.process(request(t=now, rid='waiting-oncall', waiting=True))
    cleaning = next(row for row in rows if row.action == 'schedule_cleaning')
    assert cleaning.resource_id == 'ONCALL_1'
    audit = next(record for record in engine.executor.audit if record['decision_id'] == cleaning.event_id)
    assert audit['status'] == 'pending_human_approval'
    assert audit['effects_applied'] is False
