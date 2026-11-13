"""Available operations must hydrate the calendar before same-asof planning.

The R308/CLEANER_1 witness mirrors Quickstart GR000005 and E0000040/52/73,
without reading public datasets or invoking the guest model.
"""
from copy import deepcopy

import pytest

from engine.core import instant
from engine.planner import DecisionEngine
from tests.engine.fixtures import BUILDING, FakeGateway, request, slot, state, tick
from tests.engine.test_planner_delayed_state import operation

NOW = '2026-01-06T08:30:00Z'
LATER = '2026-01-06T08:45:00Z'
REQUESTED = '2026-01-05T22:37:53Z'


class AvailabilityGateway(FakeGateway):
    """Bound raw queries by availability; public rows retain old occurrence."""
    fail_query = False
    fail_measurements = False

    def get_events(self,start_utc,end_utc,as_of_utc,room_id=None,event_type=None):
        if self.fail_query:
            self.fail_query=False
            raise OSError('injected activity query failure')
        self.calls.append(('activity_window',(start_utc,end_utc,as_of_utc)))
        return [deepcopy(row) for row in self.events
                if instant(start_utc) <= instant(row.get('available_at_utc',row['timestamp_utc'])) <= min(instant(end_utc),instant(as_of_utc))
                and (not room_id or row.get('room_id')==room_id)
                and (not event_type or row.get('type')==event_type)]

    def get_events_available_between(self,start_utc,end_utc,as_of_utc):
        return self.get_events(start_utc,end_utc,as_of_utc)

    def get_measurements(self,*args,**kwargs):
        if self.fail_measurements:
            self.fail_measurements=False
            raise OSError('injected failure after activity hydration')
        return super().get_measurements(*args,**kwargs)


def prepared(*,start_known=NOW,start_occurred=NOW):
    building=deepcopy(BUILDING)
    building['building']['rooms'] += [{'id':'R308'},{'id':'R201'}]
    repo=AvailabilityGateway()
    repo.states['R308']=[state(room='R308',t=NOW)]
    repo.slots=[slot(start=NOW)]
    repo.events=[
        {'event_id':'E0000040','timestamp_utc':NOW,'type':'checkout','room_id':'R102'},
        {'event_id':'E0000052','timestamp_utc':NOW,'type':'checkout','room_id':'R308'},
        {'event_id':'E0000073','timestamp_utc':start_occurred,'available_at_utc':start_known,
         'type':'cleaning_started','room_id':'R201','resource_id':'CLEANER_1'},
    ]
    engine=DecisionEngine(repo,building)
    engine.process(request(t=REQUESTED,room='R308',deadline='2026-01-06T12:00:00Z',rid='GR000005'))
    return repo,engine


def first_checkout():
    return operation('checkout','E0000040',NOW,room='R102',resource=None)


def test_r308_witness_queryable_other_room_work_prevents_schedule_cancel_churn():
    repo,engine=prepared()
    # E73 sorts after E40 in the normalized batch, but is already queryable.
    assert any(row['event_id']=='E0000073' for row in repo.get_events(NOW,NOW,NOW))
    rows=engine.process(first_checkout())
    assert not any(row.action=='schedule_cleaning' for row in rows)
    assert not engine.calendar.cancellations
    assert engine.calendar.observed_busy['CLEANER_1']['E0000073']['room_id']=='R201'
    assert engine.calendar.last_dirty_trigger['R308']==NOW
    assert {'E0000040','E0000052','E0000073'} <= engine._observed_event_ids
    before=deepcopy(engine.calendar.observed_busy)
    # Consuming the already hydrated external envelope must not reapply work.
    later=engine.process(operation('cleaning_started','E0000073',NOW,room='R201'))
    assert engine.calendar.observed_busy==before
    assert not any(row.action in {'schedule_cleaning','defer_action'} for row in later)
    assert not engine.rejections


def test_future_available_past_occurrence_cannot_block_before_disclosure():
    repo,engine=prepared(start_known=LATER)
    rows=engine.process(first_checkout())
    original=next(row for row in rows if row.action=='schedule_cleaning')
    assert 'E0000073' not in engine._observed_event_ids
    assert not engine.calendar.observed_busy
    # The source occurrence is older than the cursor. Availability-indexed
    # incremental hydration must still consume it once disclosed at08:45.
    later=engine.process(tick(LATER))
    assert 'E0000073' in engine._observed_event_ids
    assert original.event_id in engine.calendar._cancelled_ids
    assert any(original.event_id in row.dependencies for row in later)
    assert not engine.rejections


def test_failed_activity_query_does_not_advance_cursor_and_identical_retry_hydrates():
    repo,engine=prepared()
    cursor=engine._observed_query_utc
    ids=set(engine._observed_event_ids)
    repo.fail_query=True
    event=first_checkout()
    with pytest.raises(OSError,match='activity query'):
        engine.process(event)
    assert engine._observed_query_utc==cursor
    assert engine._observed_event_ids==ids
    assert event.event_id not in engine._seen_ids
    rows=engine.process(event)
    assert engine._observed_query_utc==NOW
    assert 'E0000073' in engine._observed_event_ids
    assert not any(row.action=='schedule_cleaning' for row in rows)


def test_late_detector_failure_rolls_back_cursor_dedup_and_withdrawal_then_retry_matches():
    repo,engine=prepared(start_known=LATER)
    _,healthy=prepared(start_known=LATER)
    for instance in (engine,healthy):instance.process(first_checkout())
    original_calendar=engine.calendar
    before=(engine._observed_query_utc,set(engine._observed_event_ids),
            deepcopy(engine.calendar.reservations),deepcopy(engine.calendar.cancellations),len(engine.executor.audit))
    repo.fail_measurements=True
    event=tick(LATER)
    with pytest.raises(OSError,match='after activity hydration'):
        engine.process(event)
    assert engine.calendar is original_calendar
    assert (engine._observed_query_utc,set(engine._observed_event_ids),engine.calendar.reservations,
            engine.calendar.cancellations,len(engine.executor.audit))==before
    assert not engine.calendar.observed_busy
    actual=engine.process(event)
    expected=healthy.process(event)
    assert [row.model_dump() for row in actual]==[row.model_dump() for row in expected]
    assert engine.calendar.observed_busy==healthy.calendar.observed_busy
    assert engine._observed_query_utc==healthy._observed_query_utc==LATER
