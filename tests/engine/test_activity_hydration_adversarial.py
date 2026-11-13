"""Independent causal controls for incremental observed-activity hydration."""
from copy import deepcopy

import pytest

from engine.core import DetectionResult, instant
from engine.planner import DecisionEngine
from tests.engine.fixtures import BUILDING, FakeGateway, request, slot, state, tick
from tests.engine.test_planner_delayed_state import operation
from tests.engine.test_planner_transactions import frozen

NOW = '2026-01-06T09:00:00Z'
LATER = '2026-01-06T09:15:00Z'


class ActivityGateway(FakeGateway):
    def __init__(self, *, leak=False):
        super().__init__()
        self.activity_queries = []
        self.leak = leak

    def get_events_available_between(self, start_utc, end_utc, as_of_utc):
        return self.get_events(start_utc, end_utc, as_of_utc)

    def get_events(self, start_utc, end_utc, as_of_utc, room_id=None, event_type=None):
        self.activity_queries.append((start_utc, end_utc, as_of_utc))
        # Availability-indexed feed can return an old occurrence that only now
        # became queryable. A deliberately leaking feed tests caller defence.
        return [deepcopy(row) for row in self.events if self.leak or
                instant(start_utc) <= instant(row.get('available_at_utc', row['timestamp_utc'])) <= instant(end_utc)
                and instant(row.get('available_at_utc', row['timestamp_utc'])) <= instant(as_of_utc)]


def isolate_detectors(engine, monkeypatch):
    # Isolate the observed-fact boundary and its query cost without unrelated
    # detector reads; the planner/calendar/transaction implementations stay real.
    for detector in (engine.turnaround, engine.water, engine.energy, engine.maintenance,
                     engine.surge, engine.logistics):
        monkeypatch.setattr(detector, 'detect', lambda *args, **kwargs: DetectionResult())


def test_all_same_asof_checkout_checkin_and_work_are_visible_before_first_detector(monkeypatch):
    repo = ActivityGateway()
    engine = DecisionEngine(repo, BUILDING)
    isolate_detectors(engine, monkeypatch)
    engine.process(tick(NOW))
    engine.calendar.reserve('CLEANER_1', '2026-01-06T10:00:00Z', '2026-01-06T10:45:00Z',
                            'old-proposal', NOW, room='R101')
    repo.events = [
        {'event_id': 'dirty', 'type': 'checkout', 'room_id': 'R102', 'timestamp_utc': LATER},
        {'event_id': 'guest-in', 'type': 'checkin', 'room_id': 'R101', 'timestamp_utc': LATER},
        {'event_id': 'actual-work', 'type': 'cleaning_started', 'room_id': 'R102',
         'resource_id': 'CLEANER_1', 'timestamp_utc': LATER},
    ]
    observations = []
    original = engine._advance_occurrence
    checkout_calls = []

    def advance(history, key, stamp):
        if history is engine.calendar.last_dirty_trigger:
            checkout_calls.append((key, stamp))
        return original(history, key, stamp)

    monkeypatch.setattr(engine, '_advance_occurrence', advance)

    def first_detector(*args):
        observations.append(True)
        assert engine.calendar.last_dirty_trigger['R102'] == LATER
        assert 'actual-work' in engine.calendar.observed_busy['CLEANER_1']
        assert not engine.calendar.reservations_for('CLEANER_1')
        assert {'dirty', 'guest-in', 'actual-work'} <= engine._observed_event_ids
        return DetectionResult()

    monkeypatch.setattr(engine.turnaround, 'detect', first_detector)
    event = tick(LATER)
    engine.process(event)
    assert checkout_calls == [('R102', LATER)]
    # The matching raw stream event and inclusive next query may repeat facts;
    # the global fact ledger must guard checkout/checkin as well as work starts.
    engine.process(operation('checkout', 'dirty', LATER, room='R102', resource=None))
    engine.process(operation('checkin', 'guest-in', LATER, room='R101', resource=None))
    engine.process(tick('2026-01-06T09:30:00Z'))
    assert checkout_calls == [('R102', LATER)]
    assert len(engine.calendar.cancellations) == 1
    before = frozen(engine)
    assert engine.process(event) == []
    assert frozen(engine) == before
    assert observations


@pytest.mark.parametrize('future_field', ['timestamp_utc', 'available_at_utc'])
def test_leaking_gateway_cannot_publish_future_activity_or_consume_its_id(monkeypatch, future_field):
    repo = ActivityGateway(leak=True)
    repo.events = [{'event_id': 'not-yet', 'type': 'cleaning_started', 'room_id': 'R101',
                    'resource_id': 'CLEANER_1', 'timestamp_utc': NOW, 'available_at_utc': NOW,
                    future_field: LATER}]
    engine = DecisionEngine(repo, BUILDING)
    isolate_detectors(engine, monkeypatch)
    engine.process(tick(NOW))
    assert not engine.calendar.observed_busy
    assert 'not-yet' not in engine._observed_event_ids
    engine.process(tick(LATER))
    assert 'not-yet' in engine._observed_event_ids
    assert 'not-yet' in engine.calendar.observed_busy['CLEANER_1']


def test_newly_available_old_occurrence_is_not_lost_behind_query_cursor(monkeypatch):
    repo = ActivityGateway()
    engine = DecisionEngine(repo, BUILDING)
    isolate_detectors(engine, monkeypatch)
    engine.process(tick(NOW))
    repo.events = [{'event_id': 'late-start', 'type': 'cleaning_started', 'room_id': 'R101',
                    'resource_id': 'CLEANER_1', 'timestamp_utc': '2026-01-06T08:30:00Z',
                    'available_at_utc': LATER}]
    engine.process(tick(LATER))
    assert engine.calendar.observed_busy['CLEANER_1']['late-start']['start_utc'] == '2026-01-06T08:30:00Z'
    # The normalized replay of the same late fact remains idempotent too.
    engine.process(operation('cleaning_started', 'late-start', '2026-01-06T08:30:00Z', known=LATER))
    assert len(engine.calendar.observed_busy['CLEANER_1']) == 1


def test_hydration_failure_restores_cursor_known_facts_cancelled_slots_and_retry(monkeypatch):
    def prepared():
        repo = ActivityGateway()
        engine = DecisionEngine(repo, BUILDING)
        isolate_detectors(engine, monkeypatch)
        engine.process(tick(NOW))
        engine.calendar.reserve('CLEANER_1', '2026-01-06T10:00:00Z', '2026-01-06T10:45:00Z',
                                'prior-job', NOW, room='R101')
        repo.events = [{'event_id': 'new-work', 'type': 'cleaning_started', 'room_id': 'R101',
                        'resource_id': 'CLEANER_1', 'timestamp_utc': LATER}]
        return repo, engine

    _, engine = prepared()
    _, healthy = prepared()
    before = frozen(engine)
    calendar = engine.calendar
    current_map = engine.calendar.reservations
    observed = engine._observed_event_ids
    event = tick(LATER)

    def fail_after_mutation(*args):
        assert 'new-work' in engine.calendar.observed_busy['CLEANER_1']
        assert engine.calendar.cancellations
        assert not engine.calendar.reservations_for('CLEANER_1')
        raise OSError('injected failure after observed fact mutation')

    monkeypatch.setattr(engine.turnaround, 'detect', fail_after_mutation)
    with pytest.raises(OSError, match='after observed fact mutation'):
        engine.process(event)
    assert engine.calendar is calendar
    assert engine.calendar.reservations is current_map
    assert engine._observed_event_ids is observed
    assert frozen(engine) == before
    assert event.event_id not in engine._seen_ids
    assert event.event_id not in engine._event_fingerprints
    monkeypatch.setattr(engine.turnaround, 'detect', lambda *args, **kwargs: DetectionResult())
    actual, expected = engine.process(event), healthy.process(event)
    assert [row.model_dump() for row in actual] == [row.model_dump() for row in expected]
    assert frozen(engine) == frozen(healthy)
    assert 'new-work' in engine._observed_event_ids
    assert len(engine.calendar.cancellations) == 1


def test_after_bootstrap_observed_fact_queries_do_not_rescan_history(monkeypatch):
    repo = ActivityGateway()
    engine = DecisionEngine(repo, BUILDING)
    isolate_detectors(engine, monkeypatch)
    times = [NOW, LATER, '2026-01-06T09:30:00Z']
    for value in times:
        engine.process(tick(value))
    assert len(repo.activity_queries) == 3
    first, *incremental = repo.activity_queries
    assert instant(first[0]) < instant(NOW)
    assert [(start, end) for start, end, _ in incremental] == list(zip(times, times[1:]))


class OccurrenceOnlyGateway(FakeGateway):
    """Original gateway contract: range is occurrence, availability filters asof."""
    def get_events(self, start_utc, end_utc, as_of_utc, room_id=None, event_type=None):
        return [row for row in super().get_events(start_utc, end_utc, as_of_utc, room_id, event_type)
                if instant(row.get('available_at_utc', row['timestamp_utc'])) <= instant(as_of_utc)]


def test_generic_occurrence_gateway_finds_late_past_work_before_guest_planning():
    def prepared():
        repo = OccurrenceOnlyGateway()
        repo.slots = [slot()]
        repo.states['R102'] = [state(room='R102')]
        engine = DecisionEngine(repo, BUILDING)
        engine.process(tick('2026-01-06T08:45:00Z'))
        repo.events = [{'event_id': 'late-past-work', 'type': 'cleaning_started',
                        'resource_id': 'CLEANER_1', 'room_id': 'R101',
                        'timestamp_utc': '2026-01-06T08:30:00Z', 'available_at_utc': NOW}]
        return engine

    guest_first, operation_first = prepared(), prepared()
    guest = request(room='R102', deadline='2026-01-06T10:00:00Z')
    first_rows = guest_first.process(guest)
    operation_first.process(operation('cleaning_started', 'late-past-work',
                                     '2026-01-06T08:30:00Z', known=NOW))
    second_rows = operation_first.process(guest)
    # Merely delivering the normalized operational row first must not be what
    # keeps a worker already busy elsewhere from being assigned to this guest.
    assert not any(row.action == 'schedule_cleaning' for row in first_rows)
    assert not any(row.action == 'schedule_cleaning' for row in second_rows)
    assert not guest_first.calendar.reservations_for('CLEANER_1')
    assert not operation_first.calendar.reservations_for('CLEANER_1')
    assert guest_first.calendar.observed_busy == operation_first.calendar.observed_busy
    assert 'late-past-work' in guest_first._observed_event_ids


def historical_batch(monkeypatch, *, future_end=None):
    repo = ActivityGateway(leak=True)
    engine = DecisionEngine(repo, BUILDING)
    isolate_detectors(engine, monkeypatch)
    engine.process(tick(NOW))
    engine.calendar.reserve('CLEANER_1', '2026-01-06T11:00:00Z', '2026-01-06T11:45:00Z',
                            'valid-other-room-job', NOW, room='R102')
    now = '2026-01-06T10:00:00Z'
    start = {'event_id': 'historic-start', 'type': 'cleaning_started', 'room_id': 'R101',
             'resource_id': 'CLEANER_1', 'timestamp_utc': '2026-01-06T08:00:00Z',
             'available_at_utc': now}
    end = {'event_id': 'historic-end', 'type': 'cleaning_completed', 'room_id': 'R101',
           'resource_id': 'CLEANER_1', 'timestamp_utc': '2026-01-06T08:30:00Z',
           'available_at_utc': now}
    if future_end is not None:
        end[future_end] = '2026-01-06T10:15:00Z'
    repo.events = [end, start]  # intentionally reverse input; causal result is unchanged
    return engine, tick(now)


def test_same_batch_completed_historical_work_does_not_cancel_other_room_future_job(monkeypatch):
    engine, event = historical_batch(monkeypatch)
    original = list(engine.calendar.reservations_for('CLEANER_1'))
    engine.process(event)
    assert engine.calendar.reservations_for('CLEANER_1') == original
    assert engine.calendar.room_jobs('R102') == original
    assert not engine.calendar.cancellations
    assert not engine.calendar.observed_busy
    assert {'historic-start', 'historic-end'} <= engine._observed_event_ids
    assert engine._observed_completions[('CLEANER_1', 'R101', 'cleaner')] == '2026-01-06T08:30:00Z'


@pytest.mark.parametrize('future_end', ['timestamp_utc', 'available_at_utc'])
def test_future_end_cannot_preseed_completion_and_silence_known_ongoing_work(monkeypatch, future_end):
    engine, event = historical_batch(monkeypatch, future_end=future_end)
    engine.process(event)
    assert 'historic-start' in engine.calendar.observed_busy['CLEANER_1']
    assert 'historic-end' not in engine._observed_event_ids
    assert not engine._observed_completions
    assert not engine.calendar.reservations_for('CLEANER_1')
    assert [row['decision_id'] for row in engine.calendar.cancellations] == ['valid-other-room-job']


def test_preseeded_completion_watermarks_are_rolled_back_with_cursor_and_capacity(monkeypatch):
    engine, event = historical_batch(monkeypatch)
    healthy, _ = historical_batch(monkeypatch)
    before = frozen(engine)
    calendar, completion_map = engine.calendar, engine._observed_completions
    cursor = engine._observed_query_utc

    def fail(*args):
        assert engine._observed_completions
        assert engine.calendar.reservations_for('CLEANER_1')
        raise OSError('after historical completion preseed')

    monkeypatch.setattr(engine.turnaround, 'detect', fail)
    with pytest.raises(OSError, match='completion preseed'):
        engine.process(event)
    assert engine.calendar is calendar
    assert engine._observed_completions is completion_map
    assert not completion_map
    assert engine._observed_query_utc == cursor
    assert frozen(engine) == before
    monkeypatch.setattr(engine.turnaround, 'detect', lambda *args: DetectionResult())
    actual, expected = engine.process(event), healthy.process(event)
    assert [row.model_dump() for row in actual] == [row.model_dump() for row in expected]
    assert frozen(engine) == frozen(healthy)


@pytest.mark.parametrize('future_checkout', [False, True], ids=['finished-historical-stay', 'checkout-not-yet-known'])
def test_late_known_checkin_does_not_cancel_valid_work_after_already_completed_stay(monkeypatch, future_checkout):
    repo = ActivityGateway(leak=True)
    engine = DecisionEngine(repo, BUILDING)
    isolate_detectors(engine, monkeypatch)
    engine.process(tick(NOW))
    engine.calendar.reserve('CLEANER_1', '2026-01-06T11:00:00Z', '2026-01-06T11:45:00Z',
                            'post-stay-cleaning', NOW, room='R101')
    now = '2026-01-06T10:00:00Z'
    repo.events = [
        {'event_id': 'late-checkin', 'type': 'checkin', 'room_id': 'R101',
         'timestamp_utc': '2026-01-06T08:00:00Z', 'available_at_utc': now},
        {'event_id': 'late-checkout', 'type': 'checkout', 'room_id': 'R101',
         'timestamp_utc': '2026-01-06T08:30:00Z',
         'available_at_utc': '2026-01-06T10:15:00Z' if future_checkout else now},
    ]
    if not future_checkout:
        repo.states['R101'] = [state(t=now)]  # fresh observed vacancy after that stay
    engine.process(tick(now))
    if future_checkout:
        assert not engine.calendar.room_jobs('R101')
        assert 'late-checkout' not in engine._observed_event_ids
        assert [row['decision_id'] for row in engine.calendar.cancellations] == ['post-stay-cleaning']
    else:
        assert repo.get_room_state('R101', now)['occupancy'] == 'vacant'
        assert engine.calendar.room_jobs('R101') == [
            ('2026-01-06T11:00:00Z', '2026-01-06T11:45:00Z', 'post-stay-cleaning')]
        assert not engine.calendar.cancellations
        assert {'late-checkin', 'late-checkout'} <= engine._observed_event_ids
