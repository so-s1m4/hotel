"""Injected parser replies cross the real normalized guest envelope and planner."""
from types import SimpleNamespace

import pytest

from ingestion.events import _guest_events
from engine.planner import DecisionEngine
from tests.engine.fixtures import BUILDING, FakeGateway, state, slot, tick
from tests.ingestion.test_guest_parser import parser, llm

NOW = '2026-01-06T09:00:00Z'
PHYSICAL = {'schedule_cleaning', 'dispatch_robot', 'adjust_room_setpoint',
            'release_room', 'block_room', 'reassign_room', 'request_inspection'}


def record(text, label='early_checkin'):
    return dict(request_id='BOUNDARY1', timestamp_utc=NOW, room_id='R101',
                booking_id=None, language='en', request_type=label, text=text,
                priority='medium', requires_guest_consent='required')


def item(intent, quote, time_text=None, review=False):
    return dict(intent=intent, evidence_quote=quote, requested_time_text=time_text,
                requested_time_utc=None, details={}, needs_review=review)


def envelope(parser, monkeypatch, source):
    import ingestion.events as events
    parsed = parser.parse_request_full(source, {})
    monkeypatch.setattr(events, '_parse_guest_requests', lambda repo, records: [parsed])
    repo = SimpleNamespace(ds=SimpleNamespace(guest_requests=[source]))
    return _guest_events(repo)[0]


def engine(occupancy='vacant'):
    gateway = FakeGateway()
    gateway.states['R101'] = [state(occupancy=occupancy)]
    gateway.slots = [slot()]
    return DecisionEngine(gateway, BUILDING)


def assert_no_operational_effects(subject, event):
    rows = subject.process(event)
    rows += subject.process(tick('2026-01-06T09:15:00Z'))
    assert not [row for row in rows if row.action in PHYSICAL]
    assert not subject.calendar.reservations.get('CLEANER_1')
    assert not [row for row in subject.executor.audit if row.get('effects_applied')]
    return rows


@pytest.mark.parametrize('label', ['early_checkin', 'waiting_for_room'])
def test_explicit_service_refusal_review_cannot_authorize_cleaning(parser, llm, monkeypatch, label):
    text = 'We do not want an early check-in. Please keep the ordinary arrival arrangement.'
    llm({'intents': [item('early_checkin', text)]})
    event = envelope(parser, monkeypatch, record(text, label))
    assert event.extraction.needs_review
    assert 'semantic_negation' in event.extraction.uncertain_fields
    assert_no_operational_effects(engine(), event)


def test_clear_timed_intent_does_not_override_an_uncertain_second_intent(parser, llm, monkeypatch):
    text = 'May we check in today at 11:10? We might also need help with our luggage.'
    llm({'intents': [item('early_checkin', 'May we check in today at 11:10?', 'today at 11:10'),
                      item('luggage_storage', 'We might also need help with our luggage.', review=True)]})
    event = envelope(parser, monkeypatch, record(text))
    assert event.payload['intents'][0]['requested_time_utc'] == '2026-01-06T10:10:00Z'
    assert not event.payload['intents'][0]['needs_review']
    assert event.extraction.needs_review
    assert_no_operational_effects(engine(), event)


@pytest.mark.parametrize('label', ['early_checkin', 'waiting_for_room'])
def test_malformed_model_fallback_is_not_an_operational_authorization(parser, llm, monkeypatch, label):
    llm('{not valid json')
    event = envelope(parser, monkeypatch, record('Please clarify our arrival arrangements.', label))
    assert event.extraction.method == 'fallback' and event.extraction.needs_review
    assert_no_operational_effects(engine(), event)


def test_verified_timed_request_still_plans_cleaning_in_a_vacant_room(parser, llm, monkeypatch):
    text = 'May we check in today at 11:10?'
    llm({'intents': [item('early_checkin', text, 'today at 11:10')]})
    event = envelope(parser, monkeypatch, record(text))
    assert not event.extraction.needs_review
    rows = engine().process(event)
    assert any(row.action == 'schedule_cleaning' for row in rows)


@pytest.mark.parametrize('occupancy', ['occupied', 'unknown'])
def test_even_verified_request_cannot_grant_room_entry_or_guest_consent(parser, llm, monkeypatch, occupancy):
    text = 'May we check in today at 11:10? You may enter the room.'
    llm({'intents': [item('early_checkin', text, 'today at 11:10')]})
    event = envelope(parser, monkeypatch, record(text))
    assert not event.extraction.needs_review
    rows = assert_no_operational_effects(engine(occupancy), event)
    assert all(row.action == 'notify_guest' for row in rows)


def test_authoritative_operational_waiting_does_not_depend_on_guest_extraction():
    from tests.engine.test_planner_audit import raw_event
    event = raw_event('arrival_waiting_room', NOW, room='R101', resource=None)
    rows = engine().process(event)
    assert any(row.action == 'schedule_cleaning' for row in rows)


def test_explicit_offline_metadata_priority_remains_available(parser, monkeypatch):
    monkeypatch.setenv('GUEST_PARSER_OFFLINE', '1')
    event = envelope(parser, monkeypatch, record('We are at reception waiting for our room.', 'waiting_for_room'))
    assert event.extraction.method == 'source_label' and event.extraction.needs_review
    assert 'offline_mode' in event.extraction.uncertain_fields
    assert any(row.action == 'schedule_cleaning' for row in engine().process(event))


def test_missing_model_configuration_cannot_verify_immediate_waiting(parser, monkeypatch):
    event = envelope(parser, monkeypatch, record('Please clarify our arrival arrangements.', 'waiting_for_room'))
    assert event.extraction.needs_review
    assert_no_operational_effects(engine(), event)


def test_offline_explicit_refusal_requires_a_contradiction_marker(parser, monkeypatch):
    monkeypatch.setenv('GUEST_PARSER_OFFLINE', '1')
    text = 'We do not want an early check-in. Please keep the ordinary arrival arrangement.'
    event = envelope(parser, monkeypatch, record(text, 'waiting_for_room'))
    assert event.extraction.method == 'source_label'
    assert 'semantic_negation' in event.extraction.uncertain_fields
    assert_no_operational_effects(engine(), event)


@pytest.mark.parametrize('tamper', ['fallback_metadata', 'missing_item_review'])
def test_malformed_waiting_extraction_cannot_claim_verification(parser, llm, monkeypatch, tamper):
    text = 'We are at reception waiting for our room.'
    llm({'intents': [item('waiting_for_room', text)]})
    event = envelope(parser, monkeypatch, record(text, 'waiting_for_room'))
    assert not event.extraction.needs_review
    if tamper == 'fallback_metadata':
        event.extraction.method = 'fallback'
    else:
        event.payload['intents'][0].pop('needs_review')
    assert_no_operational_effects(engine(), event)


def source_label_envelope(monkeypatch, source):
    import ingestion.events as events
    parsed = events.source_label_intents(source)
    monkeypatch.setattr(events, '_parse_guest_requests', lambda repo, records: [parsed])
    return _guest_events(SimpleNamespace(ds=SimpleNamespace(guest_requests=[source])))[0]


def test_legacy_source_label_refusal_cannot_schedule_cleaning(monkeypatch):
    text = 'We do not want an early check-in. Please keep the ordinary arrival arrangement.'
    event = source_label_envelope(monkeypatch, record(text, 'waiting_for_room'))
    assert event.extraction.method == 'source_label'
    assert 'semantic_negation' in event.extraction.uncertain_fields
    assert_no_operational_effects(engine(), event)


def test_affirmative_legacy_source_label_retains_metadata_priority(monkeypatch):
    event = source_label_envelope(monkeypatch, record('We are at reception waiting for our room.', 'waiting_for_room'))
    assert event.extraction.needs_review
    assert set(event.extraction.uncertain_fields) == {'requested_time_utc', 'details'}
    assert any(row.action == 'schedule_cleaning' for row in engine().process(event))


def test_legacy_source_label_semantic_helper_failure_does_not_verify_waiting(monkeypatch):
    import ingestion.request_polarity as polarity

    def broken_helper(*args, **kwargs):
        raise RuntimeError('injected unavailable semantic check')

    monkeypatch.setattr(polarity, 'declined_intents', broken_helper)
    event = source_label_envelope(monkeypatch, record('We are at reception waiting for our room.', 'waiting_for_room'))
    assert event.extraction.method == 'source_label' and event.extraction.needs_review
    assert 'semantic_validation_unavailable' in event.extraction.uncertain_fields
    assert_no_operational_effects(engine(), event)
