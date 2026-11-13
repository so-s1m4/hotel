"""Regressions from independent review; proposals never imply observed work."""
from copy import deepcopy
from datetime import datetime, timezone

import pytest

from engine.core import EngineConfig, instant, utc
from engine.detectors.turnaround import TurnaroundDetector
from engine.planner import DecisionEngine
from engine.resources import ResourceCalendar
from tests.engine.fixtures import BUILDING, FakeGateway, request, slot, state, tick


def prepared():
    repo = FakeGateway()
    repo.states['R101'] = [state()]
    repo.slots = [slot()]
    return repo


def test_utc_preserves_subsecond_instant_and_existing_whole_second_format():
    assert utc(datetime(2026, 1, 6, 9, tzinfo=timezone.utc)) == '2026-01-06T09:00:00Z'
    for stamp in ['2026-01-06T09:00:00.500Z', '2026-01-06T09:00:00.000001Z',
                  '2026-01-06T09:00:00.123456Z']:
        assert instant(utc(instant(stamp))) == instant(stamp)


def test_fractional_request_plans_without_rounding_start_into_the_past():
    engine = DecisionEngine(prepared(), BUILDING)
    now = '2026-01-06T09:00:00.500Z'
    rows = engine.process(request(t=now, deadline='2026-01-06T11:00:00Z'))
    assert {row.action for row in rows} == {'schedule_cleaning', 'notify_guest'}
    assert not engine.rejections
    job = next(iter(engine.turnaround.scheduled_jobs.values()))
    assert instant(job['start_utc']) == instant(now)
    assert instant(job['end_utc']) == instant('2026-01-06T09:45:00.500Z')


@pytest.mark.parametrize('value', [None, [], {}, 'invalid'])
def test_non_guest_payload_is_not_parsed_as_guest_intents(value):
    event = tick().model_copy(update={'event_type':'operational_event',
                                    'payload':{'type':'bms_alarm', 'intents':value}})
    engine = DecisionEngine(prepared(), BUILDING)
    engine.process(event)
    assert not engine.turnaround.pending


@pytest.mark.parametrize('field,value', [('request_type', []), ('original_text', None)])
def test_schema_accepted_malformed_guest_payloads_do_not_crash(field, value):
    event = request()
    event.payload[field] = value
    engine = DecisionEngine(prepared(), BUILDING)
    rows = engine.process(event)
    assert not engine.rejections
    assert all(row.action not in {'schedule_cleaning', 'request_inspection'} for row in rows)


def booking(created='2026-01-01T09:00:00Z', **extra):
    return {'booking_id':'B1', 'room_id':'R101', 'created_utc':created,
            'arrival_utc':'2026-01-06T10:00:00Z', 'departure_utc':'2026-01-07T10:00:00Z', **extra}


@pytest.mark.parametrize('missing_room', [None, 'BUILDING'])
def test_missing_room_resolves_from_causally_known_booking(missing_room):
    repo = prepared()
    repo.bookings = [booking(observed_room_id='R101', observed_state='not_arrived')]
    engine = DecisionEngine(repo, BUILDING)
    rows = engine.process(request(room=missing_room))
    assert any(row.action == 'schedule_cleaning' and row.affected_assets == ['R101'] for row in rows)
    assert any('bookings.csv:B1' in row.evidence for row in rows)
    assert ('booking', '2026-01-06T09:00:00Z') in repo.calls
    assert not engine.rejections


def test_future_booking_cannot_supply_missing_identity_and_request_expires_at_48h():
    repo = prepared()
    repo.bookings = [booking(created='2026-01-07T09:00:00Z')]
    engine = DecisionEngine(repo, BUILDING)
    initial = engine.process(request(room=None))
    assert initial and all(row.action == 'notify_guest' for row in initial)
    assert all(row.affected_assets == ['BUILDING'] for row in initial)
    assert engine.turnaround.pending
    # Remove the future row to leave identity genuinely unknown throughout.
    repo.bookings.clear()
    expired = engine.process(tick('2026-01-08T09:00:00Z'))
    assert not engine.turnaround.pending
    assert any('48-hour' in row.reason for row in expired)
    assert all(row.action == 'notify_guest' for row in expired)
    assert not engine.rejections
    assert not engine.process(tick('2026-01-09T09:00:00Z'))


def test_missing_room_with_observed_checked_in_booking_retires_without_entry():
    repo = prepared()
    repo.bookings = [booking(observed_state='checked_in', observed_room_id='R101',
                             observed_checkin_utc='2026-01-06T08:30:00Z')]
    engine = DecisionEngine(repo, BUILDING)
    rows = engine.process(request(room=None))
    assert not engine.turnaround.pending
    assert not any(row.action == 'schedule_cleaning' for row in rows)


def oncall_repo(now):
    repo = FakeGateway()
    repo.states = {room:[state(room=room, t=now)] for room in ['R101','R102']}
    return repo


def test_oncall_batch_uses_local_tentative_holds_without_mutating_actual_calendar():
    now = '2026-01-06T12:00:00Z'
    repo = oncall_repo(now)
    calendar = ResourceCalendar()
    detector = TurnaroundDetector(EngineConfig())
    for room, rid in [('R101','G1'),('R102','G2')]:
        event = request(t=now, room=room, rid=rid, waiting=True)
        detector.pending[event.event_id] = event
    before = deepcopy(calendar.__dict__)
    result = detector.detect(tick(now), repo, calendar, BUILDING)
    jobs = [c for c in result.candidates if c.action == 'schedule_cleaning']
    assert [(c.context['start_utc'],c.context['end_utc']) for c in jobs] == [
        ('2026-01-06T14:30:00Z','2026-01-06T15:15:00Z'),
        ('2026-01-06T15:15:00Z','2026-01-06T16:00:00Z')]
    assert calendar.__dict__ == before
    assert not detector.scheduled_jobs


def test_engine_keeps_both_feasible_oncall_proposals_without_gate_rejection():
    repo = oncall_repo('2026-01-06T09:00:00Z')
    engine = DecisionEngine(repo, BUILDING)
    for room, rid, bid in [('R101','G1','B1'),('R102','G2','B2')]:
        event = request(room=room, rid=rid, waiting=True)
        event.entities.booking_id = bid
        engine.process(event)
    now = '2026-01-06T12:00:00Z'
    for room in ['R101','R102']:
        repo.states[room].append(state(room=room, t=now))
    rows = engine.process(tick(now))
    jobs = [row for row in rows if row.action == 'schedule_cleaning']
    assert {tuple(row.affected_assets) for row in jobs} == {('R101',),('R102',)}
    assert not engine.rejections
    assert len(engine.calendar.reservations_for('ONCALL_1')) == 2
    assert all(audit['effects_applied'] is False for audit in engine.executor.audit)


@pytest.mark.parametrize("clean_stamp,should_resolve",[("2026-01-06T15:05:00Z",False),
                                                       ("2026-01-06T15:10:00Z",True),
                                                       ("2026-01-06T15:15:00Z",True)])
def test_clean_readiness_must_not_precede_latest_observed_dirty_trigger(clean_stamp,should_resolve):
    repo=FakeGateway()
    repo.states['R101']=[state(t=clean_stamp,cleaning='clean')]
    calendar=ResourceCalendar()
    calendar.last_dirty_trigger={'R101':'2026-01-06T15:10:00Z'}
    detector=TurnaroundDetector(EngineConfig())
    event=request(t='2026-01-06T15:00:00Z',waiting=True)
    detector.pending[event.event_id]=event
    result=detector.detect(tick('2026-01-06T15:15:00Z'),repo,calendar,BUILDING)
    assert ('turnaround:'+event.event_id in result.resolved) is should_resolve
    assert not any(c.action in {'schedule_cleaning','release_room'} for c in result.candidates)
