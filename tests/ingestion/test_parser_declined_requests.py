"""Explicit service refusals cannot become actionable model-selected requests."""
from copy import deepcopy
import json

import pytest

from ingestion import guest_parser
from ingestion.events import iter_normalized_events
from ingestion.replay import ReplayData
from engine.planner import DecisionEngine
from tests.ingestion.test_guest_parser import parser, llm
from tests.ingestion.test_local_parser_end_to_end import local_dataset, write_csv


CASES = [
    ('en', 'I do not want early check-in today at 13:00.',
     'I would like early check-in today at 13:00.', 'today at 13:00'),
    ('de', 'Ich möchte keinen frühen Check-in heute um 13:00.',
     'Ich möchte einen frühen Check-in heute um 13:00.', 'heute um 13:00'),
    ('it', 'Non voglio il check-in anticipato oggi alle 13:00.',
     'Voglio il check-in anticipato oggi alle 13:00.', 'oggi alle 13:00'),
    ('fr', "Je ne veux pas de check-in anticipé aujourd'hui à 13:00.",
     "Je voudrais un check-in anticipé aujourd'hui à 13:00.", "aujourd'hui à 13:00"),
    ('es', 'No quiero hacer el check-in temprano hoy a las 13:00.',
     'Quiero hacer el check-in temprano hoy a las 13:00.', 'hoy a las 13:00'),
    ('nl', 'Ik wil geen vroege check-in vandaag om 13:00.',
     'Ik wil een vroege check-in vandaag om 13:00.', 'vandaag om 13:00'),
]


def request(text, language='en', kind='early_checkin'):
    return {'request_id': 'DECLINE-AUDIT', 'timestamp_utc': '2026-01-06T09:00:00Z',
            'room_id': 'R101', 'booking_id': 'B1', 'language': language,
            'request_type': kind, 'text': text}


def context():
    return {'as_of_utc': '2026-01-06T09:00:00Z', 'room_ids': ['R101'],
            'booking': {'booking_id': 'B1', 'created_utc': '2026-01-01T00:00:00Z',
                        'arrival_utc': '2026-01-06T14:00:00Z', 'departure_utc': '2026-01-07T10:00:00Z'}}


def response(quote, time_text=None, kind='early_checkin'):
    return {'intents': [{'intent': kind, 'requested_time_utc': None,
                         'requested_time_text': time_text, 'evidence_quote': quote,
                         'details': {}, 'needs_review': False}]}


def configure(monkeypatch):
    monkeypatch.setenv('LLM_GENERATION_PROFILE', 'extraction')
    monkeypatch.setenv('LLM_RESPONSE_FORMAT', 'json_schema')


@pytest.mark.parametrize('language,negative,positive,time_text', CASES)
@pytest.mark.parametrize('quote_mode', ['full', 'narrow'])
def test_explicit_refusal_marks_wrong_positive_model_intent_for_review(parser, llm, monkeypatch, language, negative, positive, time_text, quote_mode):
    configure(monkeypatch)
    source, known = request(negative, language), context()
    before = deepcopy((source, known))
    calls = llm(response(negative if quote_mode == 'full' else time_text, time_text))
    rows, meta = parser.parse_request_full(source, known)
    assert len(calls) == 1 and meta.method == 'llm'
    assert calls[0][1]['generation_profile'] == 'extraction'
    assert calls[0][1]['response_format_mode'] == 'json_schema'
    assert rows[0]['requested_time_utc'] is None
    assert rows[0]['needs_review'] and meta.needs_review
    assert 'semantic_negation' in meta.uncertain_fields
    assert (source, known) == before


@pytest.mark.parametrize('language,negative,positive,time_text', CASES)
def test_affirmative_service_requests_still_resolve_exact_times(parser, llm, monkeypatch, language, negative, positive, time_text):
    configure(monkeypatch)
    llm(response(positive, time_text))
    rows, meta = parser.parse_request_full(request(positive, language), context())
    assert meta.method == 'llm'
    assert rows[0]['requested_time_utc'] == '2026-01-06T12:00:00Z'
    assert not rows[0]['needs_review'] and not meta.needs_review
    assert 'semantic_negation' not in meta.uncertain_fields


@pytest.mark.parametrize('text,kind', [
    ('The wifi does not work.', 'wifi_help'),
    ('There is no hot water.', 'water_issue'),
    ('Das WLAN funktioniert nicht.', 'wifi_help'),
    ("Il wifi non funziona.", 'wifi_help'),
    ("Le wifi ne fonctionne pas.", 'wifi_help'),
    ('El wifi no funciona.', 'wifi_help'),
    ('De wifi werkt niet.', 'wifi_help'),
])
def test_problem_negation_is_not_a_service_refusal(parser, llm, monkeypatch, text, kind):
    configure(monkeypatch)
    language = {'Das': 'de', 'Il': 'it', 'Le': 'fr', 'El': 'es', 'De': 'nl'}.get(text.split()[0], 'en')
    llm(response(text, kind=kind))
    rows, meta = parser.parse_request_full(request(text, language, kind), context())
    assert meta.method == 'llm'
    assert rows[0]['intent'] == kind
    assert not rows[0]['needs_review'] and not meta.needs_review
    assert 'semantic_negation' not in meta.uncertain_fields


@pytest.mark.parametrize('declined', [False, True, 'not-asking'], ids=['positive-autonomous-control', 'explicit-refusal', 'original-narrow-not-asking-witness'])
def test_real_csv_ingestion_does_not_reserve_a_cleaner_for_declined_service(local_dataset, monkeypatch, declined):
    root, building = local_dataset
    configure(monkeypatch)
    now = '2026-01-06T09:00:00Z'
    (root/'manifest.json').write_text(json.dumps({'start_utc': now, 'end_utc': '2026-01-06T09:15:00Z', 'interval_seconds': 900}))
    building['staff']['resources'].append({'resource_id': 'CLEANER_1', 'role': 'cleaner'})
    (root/'building.json').write_text(json.dumps(building))
    write_csv(root/'room_telemetry.csv', [{'timestamp_utc': now, 'room_id': 'R101',
        'temperature_c': 21, 'setpoint_c': 21, 'window_open': 0, 'presence': 0, 'housekeeping_state': 'dirty'}])
    write_csv(root/'staff_roster.csv', [{'shift_id': 'C1', 'resource_id': 'CLEANER_1', 'role': 'cleaner',
        'start_utc': '2026-01-06T08:00:00Z', 'end_utc': '2026-01-06T14:00:00Z',
        'status': 'scheduled', 'status_known_utc': '2026-01-01T00:00:00Z'}])
    source = request(CASES[0][1] if declined else CASES[0][2])
    if declined == 'not-asking':
        source['text'] = ('I am not asking to check in today at 13:00; '
                          'please keep my normal arrival tomorrow at 18:00.')
    write_csv(root/'guest_requests.csv', [{**source, 'channel': 'app_chat', 'priority': 'medium',
                                         'requires_guest_consent': 'not_required'}])
    (root/'events.jsonl').write_text(json.dumps({'event_id': 'VACANCY', 'type': 'checkout',
        'timestamp_utc': '2026-01-06T08:45:00Z', 'room_id': 'R101'})+'\n')
    calls = []

    def model(messages, config):
        calls.append(deepcopy(config))
        return json.dumps(response('today at 13:00', 'today at 13:00'))

    monkeypatch.setattr(guest_parser, '_call_llm', model)
    repo = ReplayData(root, use_llm=True)
    events = list(iter_normalized_events(repo))
    guest = next(event for event in events if event.event_type == 'guest_request')
    assert len(calls) == 1 and guest.extraction.method == 'llm'
    engine = DecisionEngine(repo, building)
    rows = engine.run()
    cleaning = [row for row in rows if row.action == 'schedule_cleaning']
    if declined:
        assert guest.extraction.needs_review
        assert 'semantic_negation' in guest.extraction.uncertain_fields
        assert guest.payload['intents'][0]['requested_time_utc'] is None
        assert not cleaning
        assert not engine.calendar.reservations_for('CLEANER_1')
    else:
        assert not guest.extraction.needs_review
        assert cleaning and cleaning[0].resource_id == 'CLEANER_1', ([row.model_dump() for row in rows], engine.rejections, repo.get_room_state('R101', now))
        assert not cleaning[0].requires_human_approval
        assert engine.calendar.reservations_for('CLEANER_1')
        assert any(row['decision_id'] == cleaning[0].event_id and row['status'] == 'simulated'
                   for row in engine.executor.audit)
    assert not engine.rejections


@pytest.mark.parametrize('language,text,kind,time_text', [
    ('en', 'Please do not check us in early tomorrow at 13:00.', 'early_checkin', 'tomorrow at 13:00'),
    ('en', 'We refuse early check-in tomorrow at 13:00.', 'early_checkin', 'tomorrow at 13:00'),
    ('en', 'Please do not clean our room.', 'cleaning_request', None),
    ('fr', "Nous ne voulons pas de départ tardif aujourd'hui à 14:00.", 'late_checkout', "aujourd'hui à 14:00"),
    ('es', 'No queremos una salida tardía hoy a las 14:00.', 'late_checkout', 'hoy a las 14:00'),
])
@pytest.mark.parametrize('quote_mode', ['full', 'narrow'])
def test_independent_refusal_aliases_cannot_be_hidden_by_model_quotes(parser, llm, monkeypatch, language, text, kind, time_text, quote_mode):
    configure(monkeypatch)
    llm(response(time_text if quote_mode == 'narrow' and time_text else text, time_text, kind))
    rows, meta = parser.parse_request_full(request(text, language, kind), context())
    assert meta.method == 'llm' and meta.needs_review and rows[0]['needs_review']
    assert rows[0]['requested_time_utc'] is None
    assert 'semantic_negation' in meta.uncertain_fields
