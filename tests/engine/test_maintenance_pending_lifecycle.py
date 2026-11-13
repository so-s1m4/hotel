"""Pending AHU proposals hold capacity without becoming observed service."""
from datetime import timedelta

from engine.core import EngineConfig, instant, utc
from engine.detectors.maintenance import MaintenanceDetector
from engine.planner import DecisionEngine
from engine.resources import ResourceCalendar
from tests.engine.fixtures import slot, tick
from tests.engine.test_extended import ahu_repo, full_building

START = '2026-01-06T09:00:00Z'


def append_reading(repo, now, *, dp=300, flow=2500):
    for metric, value in [('filter_dp_pa',dp),('airflow_m3h',flow),('fan_kw',1.2)]:
        repo.measurements['equipment.csv'].append({'timestamp_utc':now,'asset_id':'AHU1',
                                                   'metric':metric,'value':value})


def future_technician(start='2026-01-06T10:00:00Z',end='2026-01-06T11:00:00Z'):
    repo = ahu_repo()
    repo.slots = [slot(resource='TECH_1',role='technician',shift_id='SHT',start=start,end=end)]
    return repo


def test_own_live_pending_technician_hold_does_not_trigger_redundant_external_service():
    repo = future_technician()
    engine = DecisionEngine(repo,full_building())
    first = engine.process(tick())
    assert [r.action for r in first] == ['dispatch_technician']
    append_reading(repo,'2026-01-06T09:15:00Z')
    second = engine.process(tick('2026-01-06T09:15:00Z'))
    assert not second
    assert engine.calendar.tentative_reservations['TECH_1'] == [
        ('2026-01-06T10:00:00Z','2026-01-06T11:00:00Z',first[0].event_id)]
    assert len(engine.executor.audit) == 1
    assert engine.executor.audit[0]['status'] == 'pending_human_approval'
    assert not engine.calendar.reservations
    assert not engine.rejections


def test_pending_technician_is_cancelled_after_verified_recovery_and_releases_capacity():
    repo = future_technician('2026-01-06T12:00:00Z','2026-01-06T13:00:00Z')
    engine = DecisionEngine(repo,full_building())
    original = next(r for r in engine.process(tick()) if r.action == 'dispatch_technician')
    transitions = []
    for minutes in range(15,121,15):
        now = utc(instant(START)+timedelta(minutes=minutes))
        append_reading(repo,now,dp=100,flow=4200)
        transitions.extend(engine.process(tick(now)))
    assert 'plant:AHU1' not in engine.episodes.active
    assert original.event_id in engine.calendar._cancelled_ids
    assert not any(engine.calendar.tentative_reservations.values())
    assert engine.calendar.available('TECH_1','2026-01-06T12:00:00Z','2026-01-06T13:00:00Z','2026-01-06T11:00:00Z')
    cancellation = next(row for row in transitions if original.event_id in row.dependencies)
    assert cancellation.action == 'defer_action'
    audit = next(a for a in engine.executor.audit if a['decision_id'] == cancellation.event_id)
    assert audit['parameters']['cancelled_decision_id'] == original.event_id
    assert not any(r.action == 'order_service' for r in transitions)
    assert all(a['effects_applied'] is False and a['observed_history_changed'] is False for a in engine.executor.audit)
    assert not engine.rejections


def test_mixed_recovery_window_is_not_enough_to_cancel_pending_service():
    repo = future_technician('2026-01-06T12:00:00Z','2026-01-06T13:00:00Z')
    engine = DecisionEngine(repo,full_building())
    original = engine.process(tick())[0]
    for minutes in [15,30,45,60]:
        now = utc(instant(START)+timedelta(minutes=minutes))
        append_reading(repo,now,dp=100,flow=4200)
        engine.process(tick(now))
    assert 'plant:AHU1' in engine.episodes.active
    assert original.event_id not in engine.calendar._cancelled_ids
    assert any(ident == original.event_id for _,end,ident in engine.calendar.tentative_reservations['TECH_1'])
    assert not engine.rejections


def test_expired_own_hold_does_not_suppress_a_new_external_service_review():
    repo = future_technician()
    engine = DecisionEngine(repo,full_building())
    engine.process(tick())
    latest = []
    for minutes in range(15,121,15):
        now = utc(instant(START)+timedelta(minutes=minutes))
        append_reading(repo,now)
        latest = engine.process(tick(now))
    assert any(row.action == 'order_service' for row in latest)
    assert not engine.rejections


def test_unrelated_pending_resource_hold_does_not_hide_missing_technician_capacity():
    repo = future_technician()
    calendar = ResourceCalendar()
    calendar.reserve_tentative('TECH_1','2026-01-06T10:00:00Z','2026-01-06T11:00:00Z','unrelated',START)
    detector = MaintenanceDetector(EngineConfig())
    result = detector.detect(tick(),repo,full_building(),calendar,[],pending_plans=[{
        'decision_id':'unrelated','key':'plant:OTHER','asset_id':'OTHER','action':'dispatch_technician'}])
    assert [candidate.action for candidate in result.candidates] == ['order_service']
    assert calendar.tentative_reservations['TECH_1'][0][2] == 'unrelated'
