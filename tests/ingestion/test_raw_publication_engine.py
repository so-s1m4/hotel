"""Explicit raw publication time survives the real ingestion-to-engine path."""
import json

from engine.planner import DecisionEngine
from ingestion import guest_parser
from ingestion.events import iter_normalized_events
from ingestion.replay import ReplayData
from tests.engine.fixtures import tick
from tests.ingestion.test_local_parser_end_to_end import local_dataset, write_csv


OCCURRED = '2026-01-06T08:30:00Z'
BEFORE = '2026-01-06T08:45:00Z'
PUBLISHED = '2026-01-06T09:00:00Z'


def test_explicit_publication_delays_all_real_views_and_normalized_engine_observation(local_dataset, monkeypatch):
    root, building = local_dataset
    building['staff']['resources'].append({'resource_id': 'CLEANER_1', 'role': 'cleaner'})
    (root/'building.json').write_text(json.dumps(building))
    (root/'manifest.json').write_text(json.dumps({'start_utc': BEFORE, 'end_utc': PUBLISHED, 'interval_seconds': 900}))
    # No guest extraction is needed. Keep the actual required CSV header only.
    (root/'guest_requests.csv').write_text('request_id,timestamp_utc,request_type,text\n')
    write_csv(root/'room_telemetry.csv', [
        {'timestamp_utc': BEFORE, 'room_id': room, 'temperature_c': 21, 'setpoint_c': 21,
         'window_open': 0, 'presence': 0, 'housekeeping_state': 'dirty'}
        for room in ('R101', 'R102')])
    raw = [
        {'event_id': 'PUBLISHED_WORK', 'type': 'cleaning_started', 'room_id': 'R101',
         'resource_id': 'CLEANER_1', 'timestamp_utc': OCCURRED, 'available_at_utc': PUBLISHED},
        {'event_id': 'PUBLISHED_CHECKIN', 'type': 'checkin', 'room_id': 'R102',
         'booking_id': 'B2', 'timestamp_utc': OCCURRED, 'available_at_utc': PUBLISHED},
    ]
    (root/'events.jsonl').write_text(''.join(json.dumps(row)+'\n' for row in raw))

    def forbidden_model(*args, **kwargs):
        raise AssertionError('Publication integration must never call a model')

    monkeypatch.setattr(guest_parser, '_call_llm', forbidden_model)
    repo = ReplayData(root, use_llm=False)
    stream = list(iter_normalized_events(repo))
    operational = [event for event in stream if event.event_id.startswith('raw:PUBLISHED_')]
    assert len(operational) == 2
    for event in operational:
        assert event.timestamp_utc == event.source.timestamp_utc == OCCURRED
        assert event.available_at_utc == PUBLISHED
        assert event.payload['timestamp_utc'] == OCCURRED
    assert not any(event.available_at_utc <= BEFORE for event in operational)

    assert repo.get_events('2026-01-06T00:00:00Z', BEFORE, BEFORE) == []
    assert repo.get_events_available_between(BEFORE, PUBLISHED, BEFORE) == []
    assert repo.get_room_state('R102', BEFORE)['occupancy'] == 'unknown'
    before_booking = repo.get_booking('B2', BEFORE)
    assert before_booking['observed_state'] == 'not_arrived'
    assert before_booking['observed_checkin_utc'] is None
    assert not before_booking['evidence']

    public = repo.get_events('2026-01-06T00:00:00Z', PUBLISHED, PUBLISHED)
    assert {row['event_id'] for row in public} == {'PUBLISHED_WORK', 'PUBLISHED_CHECKIN'}
    assert all(row['timestamp_utc'] == OCCURRED and row['available_at_utc'] == PUBLISHED for row in public)
    assert repo.get_room_state('R102', PUBLISHED)['occupancy'] == 'occupied'
    assert repo.get_booking('B2', PUBLISHED)['observed_state'] == 'checked_in'

    engine = DecisionEngine(repo, building)
    engine.process(tick(BEFORE))
    assert not engine.calendar.observed_busy
    assert not ({'PUBLISHED_WORK', 'PUBLISHED_CHECKIN'} & engine._observed_event_ids)
    # A same-asof clock before the normalized raw rows must still hydrate facts
    # now legitimately queryable, while retaining their original occurrence.
    engine.process(tick(PUBLISHED))
    assert 'PUBLISHED_WORK' in engine.calendar.observed_busy['CLEANER_1']
    assert engine.calendar.observed_busy['CLEANER_1']['PUBLISHED_WORK']['start_utc'] == OCCURRED
    assert {'PUBLISHED_WORK', 'PUBLISHED_CHECKIN'} <= engine._observed_event_ids
    for event in operational:
        engine.process(event)
    assert len(engine.calendar.observed_busy['CLEANER_1']) == 1
    assert all(record['effects_applied'] is False for record in engine.executor.audit)
