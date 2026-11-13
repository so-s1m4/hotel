"""Failure injection must leave exactly the state of the last committed input."""
from datetime import datetime
import pytest
from common.contracts.schemas import NormalizedEventV1
from engine.planner import DecisionEngine
from tests.engine.fixtures import BUILDING, FakeGateway, request, slot, state, tick
from tests.engine.test_planner_delayed_state import operation


def frozen(value):
    if isinstance(value, dict):
        return {key: frozen(item) for key,item in value.items()}
    if isinstance(value, (list,tuple)):
        return [frozen(item) for item in value]
    if isinstance(value,set):
        return sorted(map(repr,value))
    if hasattr(value, 'model_dump'):
        return value.model_dump()
    if isinstance(value,datetime):
        return value.isoformat()
    if hasattr(value,'__dict__'):
        return {key:frozen(item) for key,item in vars(value).items()
                if key not in {'repo','building','config','_transaction'} and not callable(item)}
    # Bounded detector deque contents are immutable; repr includes their values.
    return repr(value)


class FlakyGateway(FakeGateway):
    fail_method = None
    def get_events(self,*args,**kwargs):
        self.check('events')
        return super().get_events(*args,**kwargs)
    def get_measurements(self,filename,*args,**kwargs):
        self.check(filename)
        return super().get_measurements(filename,*args,**kwargs)
    def get_tariffs(self,*args,**kwargs):
        self.check('tariffs')
        return super().get_tariffs(*args,**kwargs)
    def check(self,name):
        if self.fail_method == name:
            self.fail_method = None
            raise OSError('injected '+name)


def prepared():
    repo = FlakyGateway()
    repo.states['R101'] = [state()]
    return repo,DecisionEngine(repo,BUILDING)


def records(rows):
    return [row.model_dump() for row in rows]


@pytest.mark.parametrize('failure', ['events','room_water.csv','equipment.csv','tariffs'])
def test_failed_input_rolls_back_every_owned_state_and_retry_matches_healthy(failure):
    repo,engine = prepared()
    healthy_repo,healthy = prepared()
    # First request has no staff, then a tick can accept the cleaning proposal
    # before a later gateway read fails. Tariff failure comes after water's
    # and energy's scan timestamps/counters were already advanced.
    engine.process(request())
    healthy.process(request())
    for gateway in (repo,healthy_repo):
        gateway.slots = [slot()]
        gateway.states['R101'].append(state(t='2026-01-06T10:00:00Z'))
    event = tick('2026-01-06T10:00:00Z')
    if failure == 'events':
        # Exercise the first hydration read, before any candidate is accepted.
        engine._observed_hydrated = healthy._observed_hydrated = False
    before = frozen(engine)
    identities = engine.calendar,engine.executor,engine.turnaround,engine.water
    repo.fail_method = failure
    with pytest.raises(OSError,match='injected'):
        engine.process(event)
    assert frozen(engine) == before
    assert identities == (engine.calendar,engine.executor,engine.turnaround,engine.water)
    expected = healthy.process(event)
    actual = engine.process(event)
    assert records(actual) == records(expected)
    assert frozen(engine) == frozen(healthy)
    assert engine.event_count == 2
    assert engine.process(event) == []
    assert len(engine.executor.audit) == len(healthy.executor.audit)
    assert len({row['decision_id'] for row in engine.executor.audit}) == len(engine.executor.audit)


def test_first_hydration_failure_does_not_consume_event_or_dedup_fingerprint():
    repo,engine = prepared()
    repo.slots = [slot()]
    before = frozen(engine)
    repo.fail_method = 'events'
    event = request()
    with pytest.raises(OSError):
        engine.process(event)
    assert frozen(engine) == before
    assert event.event_id not in engine._seen_ids
    assert event.event_id not in engine._event_fingerprints
    actual = engine.process(event)
    assert {r.action for r in actual} == {'schedule_cleaning','notify_guest'}
    assert engine.event_count == 1


def test_failure_after_notification_flush_restores_outbox_ledger_episodes_and_audit(monkeypatch):
    repo,engine = prepared()
    repo.slots = [slot()]
    before = frozen(engine)
    original = engine._flush_notifications
    def fail_after_flush(event):
        result = original(event)
        assert any(row.action=='notify_guest' for row in result)
        raise OSError('after messages')
    monkeypatch.setattr(engine,'_flush_notifications',fail_after_flush)
    with pytest.raises(OSError,match='after messages'):
        engine.process(request())
    assert frozen(engine) == before
    monkeypatch.setattr(engine,'_flush_notifications',original)
    rows = engine.process(request())
    assert {r.action for r in rows} == {'schedule_cleaning','notify_guest'}
    assert len(engine.executor.audit) == 2
    assert len(engine.notifications.explicit_queries) == 1


def test_failed_cancellation_does_not_withdraw_previously_returned_plan():
    repo,engine = prepared()
    repo.slots = [slot(start='2026-01-06T11:00:00Z')]
    original = next(r for r in engine.process(request(deadline='2026-01-06T13:00:00Z')) if r.action=='schedule_cleaning')
    repo.states['R101'].append(state(t='2026-01-06T10:00:00Z',cleaning='clean'))
    before = frozen(engine)
    repo.fail_method = 'room_water.csv'
    with pytest.raises(OSError):
        engine.process(tick('2026-01-06T10:00:00Z'))
    assert frozen(engine) == before
    assert original.event_id not in engine.calendar._cancelled_ids
    rows = engine.process(tick('2026-01-06T10:00:00Z'))
    assert original.event_id in engine.calendar._cancelled_ids
    assert sum(r.action=='defer_action' and original.event_id in r.dependencies for r in rows) == 1


def test_failed_work_completion_restores_previously_retained_work_order(monkeypatch):
    repo,engine = prepared()
    created = NormalizedEventV1(event_id='wo:ONE', timestamp_utc='2026-01-06T08:00:00Z',available_at_utc='2026-01-06T08:00:00Z',
        event_type='work_order_created',source={'file':'work_orders.csv','record_id':'ONE'},entities={'asset_id':'HP1'},
        payload={'work_order_id':'ONE','created_utc':'2026-01-06T08:00:00Z','summary':'known creation','asset_id':'HP1'})
    engine.process(created)
    before = frozen(engine)
    original = engine.optimization.detect
    def fail(*args,**kwargs):
        raise OSError('after work context mutation')
    monkeypatch.setattr(engine.optimization,'detect',fail)
    event = operation('work_order_completed','ONE','2026-01-06T09:00:00Z',summary='completion-only narrative')
    with pytest.raises(OSError):
        engine.process(event)
    assert frozen(engine) == before
    monkeypatch.setattr(engine.optimization,'detect',original)
    engine.process(event)
    assert engine.work_orders[0]['summary'] == 'completion-only narrative'
    assert engine.event_count == 2


def test_failed_future_input_does_not_advance_successful_input_order():
    repo,engine = prepared()
    repo.fail_method = 'events'
    with pytest.raises(OSError):
        engine.process(tick('2026-01-06T11:00:00Z'))
    engine.process(tick('2026-01-06T09:00:00Z'))
    assert engine.event_count == 1
    assert len(engine._seen_ids) == 1


def test_original_submission_calendar_metadata_rolls_back_without_cloning_identity(monkeypatch):
    repo,engine = prepared()
    repo.slots = [slot()]
    # The ledger stores lists of borrowed original-calendar references.
    previous_calendars = [engine.calendar]
    engine.executor._submitted_calendars['previous-command'] = previous_calendars
    original_flush = engine._flush_notifications
    def fail_after_submissions(event):
        original_flush(event)
        assert len(engine.executor._submitted_calendars) == 3
        raise OSError('after original-calendar metadata')
    monkeypatch.setattr(engine,'_flush_notifications',fail_after_submissions)
    with pytest.raises(OSError,match='original-calendar metadata'):
        engine.process(request())
    assert list(engine.executor._submitted_calendars) == ['previous-command']
    assert engine.executor._submitted_calendars['previous-command'] is previous_calendars
    assert len(previous_calendars) == 1
    assert previous_calendars[0] is engine.calendar
    assert engine.executor.audit == []
    assert engine.event_count == 0
    monkeypatch.setattr(engine,'_flush_notifications',original_flush)
    rows = engine.process(request())
    assert {row.action for row in rows} == {'schedule_cleaning','notify_guest'}
    assert set(engine.executor._submitted_calendars) == {'previous-command',*(row.event_id for row in rows)}
    assert engine.executor._submitted_calendars['previous-command'] is previous_calendars
    for calendars in engine.executor._submitted_calendars.values():
        assert isinstance(calendars, list) and len(calendars) == 1
        assert calendars[0] is engine.calendar
    assert len(engine.executor.audit) == 2
    assert engine.process(request()) == []


def test_failure_after_energy_scoped_notice_restores_existing_nested_priority_state():
    from tests.engine.fixtures import energy_repo
    source = energy_repo(temperature=26)
    repo = FlakyGateway()
    repo.states,repo.measurements = source.states,source.measurements
    for row in repo.states['R101']:
        row['current_booking_id'] = 'CURRENT'
    engine = DecisionEngine(repo,BUILDING)
    old_key = ('booking:PREVIOUS','overheating')
    new_key = ('booking:CURRENT','overheating')
    engine.energy._notice_priorities['R101'][old_key] = 'high'
    before = frozen(engine)
    repo.fail_method = 'tariffs'
    with pytest.raises(OSError,match='tariffs'):
        engine.process(tick())
    assert frozen(engine) == before
    assert engine.energy._notice_priorities.default_factory is dict
    assert engine.energy._notice_priorities['R101'] == {old_key:'high'}
    rows = engine.process(tick())
    assert sum(row.action=='notify_guest' and row.issue_type=='heating_comfort' for row in rows) == 1
    assert engine.energy._notice_priorities['R101'] == {old_key:'high',new_key:'high'}
    assert engine.event_count == 1
    assert len({row['decision_id'] for row in engine.executor.audit}) == len(engine.executor.audit)
    assert engine.process(tick()) == []


@pytest.mark.parametrize('access',['item','get','setdefault'])
def test_original_calendar_list_append_rolls_back_in_place_without_cloning_calendars(monkeypatch,access):
    from engine.resources import ResourceCalendar
    repo,engine=prepared()
    original_list=[engine.calendar]
    engine.executor._submitted_calendars['previous-command']=original_list
    other_calendar=ResourceCalendar()
    original=engine.optimization.detect
    def append_and_fail(*args,**kwargs):
        ledger=engine.executor._submitted_calendars
        calendars=(ledger['previous-command'] if access=='item' else
                   ledger.get('previous-command') if access=='get' else
                   ledger.setdefault('previous-command',[]))
        calendars.append(other_calendar)
        assert len(calendars)==2
        raise OSError('after calendar-list append')
    monkeypatch.setattr(engine.optimization,'detect',append_and_fail)
    with pytest.raises(OSError,match='calendar-list append'):
        engine.process(tick())
    restored=engine.executor._submitted_calendars['previous-command']
    assert restored is original_list
    assert len(restored)==1
    assert restored[0] is engine.calendar
    assert other_calendar not in restored
    assert engine.event_count==0
    assert engine.executor.audit==[]
    monkeypatch.setattr(engine.optimization,'detect',original)
    engine.process(tick())
    assert engine.event_count==1
    assert engine.executor._submitted_calendars['previous-command'][0] is engine.calendar
