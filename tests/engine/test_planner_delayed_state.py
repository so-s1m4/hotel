"""Occurrence ordering and obsolete room holds through the real planner."""
import pytest
from common.contracts.schemas import NormalizedEventV1
from engine.planner import DecisionEngine
from tests.engine.fixtures import BUILDING, FakeGateway, request, slot, state, tick


def operation(kind, ident, at, known=None, room='R101', resource='CLEANER_1', **payload):
    return NormalizedEventV1(event_id='raw:'+ident, timestamp_utc=at, available_at_utc=known or at,
        event_type='operational_event', source={'file':'events.jsonl','record_id':ident,'timestamp_utc':at},
        entities={'room_id':room,'resource_id':resource},
        payload={'type':kind,'room_id':room,'resource_id':resource,**payload})


def test_delayed_completed_start_does_not_block_a_different_room():
    repo = FakeGateway()
    repo.slots = [slot()]
    repo.states['R102'] = [state(room='R102', t='2026-01-06T09:30:00Z')]
    engine = DecisionEngine(repo, BUILDING)
    engine.process(operation('cleaning_completed','END','2026-01-06T09:00:00Z'))
    engine.process(operation('cleaning_started','START','2026-01-06T08:00:00Z','2026-01-06T09:30:00Z'))
    rows = engine.process(request(t='2026-01-06T09:30:00Z',room='R102',deadline='2026-01-06T11:00:00Z'))
    assert not engine.calendar.observed_busy
    assert any(r.action == 'schedule_cleaning' and r.resource_id == 'CLEANER_1' for r in rows)


@pytest.mark.parametrize('kind,attribute', [('cleaning_completed','last_human_completion'), ('checkout','last_dirty_trigger')])
def test_delayed_older_occurrence_does_not_replace_newer_history(kind, attribute):
    engine = DecisionEngine(FakeGateway(), BUILDING)
    engine.process(operation(kind,'NEW','2026-01-06T09:15:00Z'))
    engine.process(operation(kind,'OLD','2026-01-06T08:00:00Z','2026-01-06T09:30:00Z'))
    assert getattr(engine.calendar, attribute)['R101'] == '2026-01-06T09:15:00Z'


@pytest.mark.parametrize('end_room,end_resource,start_room,start_resource', [
    ('R102','CLEANER_1','R101','CLEANER_1'),
    ('R101','CLEANER_1','R101','ROBOT_1'),
    ('R101','CLEANER_1','R101',None),
])
def test_completion_does_not_mask_a_nonmatching_delayed_start(end_room,end_resource,start_room,start_resource):
    engine = DecisionEngine(FakeGateway(), BUILDING)
    engine.process(operation('cleaning_completed','END','2026-01-06T09:00:00Z',room=end_room,resource=end_resource))
    kind = 'robot_floor_cleaning_started' if start_resource == 'ROBOT_1' else 'cleaning_started'
    engine.process(operation(kind,'START','2026-01-06T08:00:00Z','2026-01-06T09:30:00Z',room=start_room,resource=start_resource))
    # An identified same-room completion can close unidentified same-role work.
    expected_busy = not (start_resource is None and end_room == start_room)
    assert bool(engine.calendar.observed_busy) is expected_busy


def test_stale_completion_does_not_clear_newer_observed_start():
    engine = DecisionEngine(FakeGateway(), BUILDING)
    engine.process(operation('cleaning_started','START','2026-01-06T09:15:00Z'))
    engine.process(operation('cleaning_completed','END','2026-01-06T09:00:00Z','2026-01-06T09:30:00Z'))
    assert 'START' in engine.calendar.observed_busy['CLEANER_1']


@pytest.mark.parametrize('pending', [False, True])
def test_incoming_occupied_reassignment_cancels_room_entry_hold(pending):
    repo = FakeGateway()
    now = '2026-01-06T15:00:00Z'
    later = '2026-01-06T15:15:00Z'
    repo.states['R101'] = [state(t=now), state(t=later,occupancy='occupied')]
    if not pending:
        repo.slots = [slot(start='2026-01-06T16:00:00Z',end='2026-01-06T18:00:00Z')]
    engine = DecisionEngine(repo, BUILDING)
    original = next(r for r in engine.process(request(t=now,waiting=True,deadline='2026-01-06T18:00:00Z')) if r.action=='schedule_cleaning')
    rows = engine.process(operation('room_reassigned','MOVE',later,resource=None,booking_id='OTHER',from_room='R102',to_room='R101'))
    assert original.event_id in engine.calendar._cancelled_ids
    assert not engine.calendar.room_jobs('R101')
    assert any(r.action=='defer_action' and original.event_id in r.dependencies for r in rows)


@pytest.mark.parametrize('clean_time,expected_cancel', [
    ('2026-01-06T15:15:00Z',True), ('2026-01-06T15:01:00Z',False),
])
def test_fresh_clean_telemetry_releases_future_oncall_hold(clean_time,expected_cancel):
    repo = FakeGateway()
    repo.states['R101'] = [state(t='2026-01-06T15:00:00Z')]
    engine = DecisionEngine(repo, BUILDING)
    original = next(r for r in engine.process(request(t='2026-01-06T15:00:00Z',waiting=True)) if r.action=='schedule_cleaning')
    # Stale by 44 minutes or fresh by 30 minutes at the configured cutoff.
    repo.states['R101'].append(state(t=clean_time,cleaning='clean',blocked=False,inspection_required=False))
    engine.process(tick('2026-01-06T15:45:00Z'))
    assert (original.event_id in engine.calendar._cancelled_ids) is expected_cancel


def test_late_completion_before_current_checkout_does_not_cancel_new_cleaning():
    repo = FakeGateway()
    repo.slots = [slot(start='2026-01-06T10:00:00Z')]
    repo.states['R101'] = [state(t='2026-01-06T09:15:00Z')]
    engine = DecisionEngine(repo,BUILDING)
    engine.process(operation('checkout','CHECKOUT','2026-01-06T09:10:00Z'))
    original = next(row for row in engine.process(request(t='2026-01-06T09:15:00Z',deadline='2026-01-06T12:00:00Z')) if row.action=='schedule_cleaning')
    engine.process(operation('cleaning_completed','OLD_END','2026-01-06T08:00:00Z','2026-01-06T09:30:00Z'))
    assert original.event_id not in engine.calendar._cancelled_ids
    assert any(ident==original.event_id for _,_,ident in engine.calendar.room_jobs('R101'))


def test_clean_sample_before_new_checkout_cannot_cancel_necessary_future_job():
    repo = FakeGateway()
    repo.states['R101'] = [state(t='2026-01-06T15:00:00Z')]
    engine = DecisionEngine(repo,BUILDING)
    original = next(row for row in engine.process(request(t='2026-01-06T15:00:00Z',waiting=True)) if row.action=='schedule_cleaning')
    repo.states['R101'].append(state(t='2026-01-06T15:05:00Z',cleaning='clean'))
    engine.process(operation('checkout','NEW_CHECKOUT','2026-01-06T15:10:00Z',resource=None))
    engine.process(tick('2026-01-06T15:15:00Z'))
    assert original.event_id not in engine.calendar._cancelled_ids


def test_completed_late_start_does_not_cancel_a_newer_other_room_plan():
    repo = FakeGateway()
    repo.states['R102'] = [state(room='R102',t='2026-01-06T09:15:00Z')]
    repo.slots = [slot(start='2026-01-06T10:00:00Z')]
    engine = DecisionEngine(repo,BUILDING)
    engine.process(operation('cleaning_completed','END','2026-01-06T09:00:00Z'))
    original = next(row for row in engine.process(request(t='2026-01-06T09:15:00Z',room='R102',deadline='2026-01-06T12:00:00Z')) if row.action=='schedule_cleaning')
    engine.process(operation('cleaning_started','START','2026-01-06T08:00:00Z','2026-01-06T09:30:00Z'))
    assert original.event_id not in engine.calendar._cancelled_ids
    assert not engine.calendar.observed_busy


def test_planned_vacant_room_reassignment_alone_does_not_cancel_entry_plan():
    repo = FakeGateway()
    repo.states['R101'] = [state(t='2026-01-06T15:00:00Z'),state(t='2026-01-06T15:15:00Z')]
    engine = DecisionEngine(repo,BUILDING)
    original = next(row for row in engine.process(request(t='2026-01-06T15:00:00Z',waiting=True)) if row.action=='schedule_cleaning')
    engine.process(operation('room_reassigned','MOVE','2026-01-06T15:15:00Z',resource=None,booking_id='OTHER',from_room='R102',to_room='R101'))
    assert original.event_id not in engine.calendar._cancelled_ids


@pytest.mark.parametrize('approved', [False,True])
def test_confirmed_plant_recovery_releases_unexecuted_hold_even_after_planned_start(approved):
    from datetime import timedelta
    from engine.core import instant,utc
    from tests.engine.test_extended import ahu_repo,full_building
    repo = ahu_repo()
    repo.slots = [slot(resource='TECH_1',role='technician',start='2026-01-06T10:30:00Z',end='2026-01-06T12:30:00Z')]
    engine = DecisionEngine(repo,full_building())
    original = next(row for row in engine.process(tick()) if row.action=='dispatch_technician')
    plan = engine._proposals[original.event_id]
    if approved:
        # Capacity control: an accepted reservation is distinct from a tentative
        # proposal. This does not pretend that the planner granted an approval.
        engine.calendar.reserve('TECH_1',plan.context['start_utc'],plan.context['end_utc'],original.event_id,original.timestamp_utc)
    engine.calendar.observe_start('TECH_1',None,'ACTUAL_TECH_WORK','2026-01-06T09:00:00Z',role='technician')
    rows=[]
    for minute in range(15,106,15):
        now=utc(instant('2026-01-06T09:00:00Z')+timedelta(minutes=minute))
        for metric,value in [('filter_dp_pa',100),('airflow_m3h',4200),('fan_kw',1.2)]:
            repo.measurements['equipment.csv'].append({'timestamp_utc':now,'asset_id':'AHU1','metric':metric,'value':value})
        rows.extend(engine.process(tick(now)))
    assert 'plant:AHU1' not in engine.episodes.active
    assert bool(original.event_id in engine.calendar._cancelled_ids) is not approved
    assert bool(any(row.action=='defer_action' and original.event_id in row.dependencies for row in rows)) is not approved
    assert 'ACTUAL_TECH_WORK' in engine.calendar.observed_busy['TECH_1']
    if approved:
        assert any(ident==original.event_id for _,_,ident in engine.calendar.reservations['TECH_1'])
    else:
        assert not engine.calendar.tentative_reservations['TECH_1']
