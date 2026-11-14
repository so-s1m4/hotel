"""Fresh generic source-polarity regressions; injected replies, never a model.

The same narrow model quote is tested against affirmative and declined source
messages. Refusal marking preserves the extracted label; it is not actuation.
"""
from copy import deepcopy
import json
from unittest.mock import Mock

import pytest

from ingestion import guest_parser
from ingestion.request_polarity import declined_intents
from tests.ingestion.test_parser_benchmark import isolated_environment_and_network


SERVICE_CASES = [
    pytest.param('en', 'luggage_storage', 'luggage storage',
        'I do not need luggage storage for these two suitcases.',
        'I need luggage storage for these two suitcases.', id='en-luggage'),
    pytest.param('de', 'luggage_storage', 'Gepäckaufbewahrung',
        'Wir benötigen keine Gepäckaufbewahrung für unsere Reisetaschen.',
        'Wir benötigen eine Gepäckaufbewahrung für unsere Reisetaschen.', id='de-luggage'),
    pytest.param('it', 'luggage_storage', 'deposito bagagli',
        'Non desideriamo il deposito bagagli per le nostre valigie.',
        'Desideriamo il deposito bagagli per le nostre valigie.', id='it-luggage'),
    pytest.param('fr', 'luggage_storage', 'consigne à bagages',
        'Nous ne souhaitons pas de consigne à bagages pour nos valises.',
        'Nous souhaitons une consigne à bagages pour nos valises.', id='fr-luggage'),
    pytest.param('es', 'luggage_storage', 'guardar las maletas',
        'No necesitamos guardar las maletas mientras salimos.',
        'Necesitamos guardar las maletas mientras salimos.', id='es-luggage'),
    pytest.param('nl', 'luggage_storage', 'bagageopslag',
        'Wij willen geen bagageopslag voor onze reistassen.',
        'Wij willen bagageopslag voor onze reistassen.', id='nl-luggage'),
    pytest.param('en', 'cleaning_request', 'housekeeping',
        'We do not want housekeeping for this room.',
        'We want housekeeping for this room.', id='en-cleaning'),
    pytest.param('de', 'cleaning_request', 'Zimmerreinigung',
        'Ich brauche heute keine Zimmerreinigung.',
        'Ich brauche heute eine Zimmerreinigung.', id='de-cleaning'),
    pytest.param('it', 'cleaning_request', 'pulizia della stanza',
        'Non richiedo la pulizia della stanza.',
        'Richiedo la pulizia della stanza.', id='it-cleaning'),
    pytest.param('fr', 'cleaning_request', 'nettoyage',
        'Je ne souhaite pas le nettoyage de cette chambre.',
        'Je souhaite le nettoyage de cette chambre.', id='fr-cleaning'),
    pytest.param('es', 'cleaning_request', 'limpien nuestra habitación',
        'No queremos que limpien nuestra habitación mientras trabajamos.',
        'Queremos que limpien nuestra habitación mientras trabajamos.', id='es-cleaning-subjunctive'),
    pytest.param('es', 'cleaning_request', 'limpien esta habitación',
        'Por favor, no limpien esta habitación durante la videollamada.',
        'Por favor, limpien esta habitación durante la videollamada.', id='es-cleaning-imperative'),
    pytest.param('es', 'cleaning_request', 'limpien mi habitación',
        'Por favor no limpien mi habitación mientras atiendo esta llamada.',
        'Por favor limpien mi habitación mientras atiendo esta llamada.', id='es-cleaning-imperative-no-comma'),
    pytest.param('nl', 'cleaning_request', 'schoonmaak',
        'Ik wil geen schoonmaak van mijn kamer.',
        'Ik wil schoonmaak van mijn kamer.', id='nl-cleaning'),
]


def prediction(label, quote):
    return {'intent': label, 'requested_time_utc': None, 'requested_time_text': None,
            'evidence_quote': quote, 'details': {}, 'needs_review': False}


@pytest.fixture
def parse_stub(monkeypatch, tmp_path, isolated_environment_and_network):
    monkeypatch.setenv('LLM_MODEL', 'generic-polarity-stub')
    monkeypatch.setenv('LLM_BASE_URL', 'https://example.invalid/v1')
    monkeypatch.setenv('LLM_GENERATION_PROFILE', 'extraction')
    monkeypatch.setenv('GUEST_PARSER_CACHE_DIR', str(tmp_path / 'cache'))
    sequence = 0

    def parse(text, language, predictions, source_label=None):
        nonlocal sequence
        sequence += 1
        record = {'request_id': f'GENERIC-POLARITY-{sequence}',
                  'timestamp_utc': '2026-02-03T08:00:00Z', 'room_id': 'R205',
                  'language': language, 'request_type': source_label or predictions[0]['intent'],
                  'text': text}
        before = deepcopy(record)
        transport = Mock(return_value=json.dumps({'intents': predictions}, ensure_ascii=False))
        monkeypatch.setattr(guest_parser, '_call_llm', transport)
        result, meta = guest_parser.parse_request_full(record, {
            'as_of_utc': record['timestamp_utc'], 'room_ids': ['R205']})
        transport.assert_called_once()
        sent = json.loads(transport.call_args.args[0][-1]['content'])
        assert sent['guest_record'] == before and record == before
        assert meta.method == 'llm'
        return result, meta

    return parse


@pytest.mark.parametrize('language,label,quote,negative,positive', SERVICE_CASES)
def test_full_source_refusal_marks_a_narrow_positive_model_quote(
        parse_stub, language, label, quote, negative, positive):
    proposed = prediction(label, quote)
    assert quote in positive and quote in negative
    affirmative, affirmative_meta = parse_stub(positive, language, [proposed])
    assert affirmative == [proposed]
    assert affirmative_meta.needs_review is False
    assert affirmative_meta.uncertain_fields == []

    refused, refused_meta = parse_stub(negative, language, [proposed])
    assert len(refused) == 1 and refused[0]['intent'] == label
    assert refused[0]['evidence_quote'] == quote
    assert refused[0]['requested_time_utc'] is None
    assert refused[0]['needs_review'] is True
    assert refused_meta.needs_review is True
    assert set(refused_meta.uncertain_fields) == {'semantic_negation'}


@pytest.mark.parametrize('reverse_order', [False, True], ids=['declined-first', 'affirmative-first'])
@pytest.mark.parametrize('language,text,refused_label,refused_quote,affirmative_label,affirmative_quote', [
    pytest.param('en', 'I do not need luggage storage; please arrange housekeeping.',
        'luggage_storage', 'luggage storage', 'cleaning_request', 'housekeeping', id='en'),
    pytest.param('de', 'Ich brauche keine Zimmerreinigung, aber ich möchte einen Parkplatz.',
        'cleaning_request', 'Zimmerreinigung', 'parking', 'Parkplatz', id='de'),
    pytest.param('it', 'Non desidero il deposito bagagli; vorrei la pulizia della stanza.',
        'luggage_storage', 'deposito bagagli', 'cleaning_request', 'pulizia della stanza', id='it'),
    pytest.param('fr', 'Je ne souhaite pas le nettoyage, mais je voudrais une consigne à bagages.',
        'cleaning_request', 'nettoyage', 'luggage_storage', 'consigne à bagages', id='fr'),
    pytest.param('es', 'No queremos que limpien nuestra habitación, pero necesitamos guardar las maletas.',
        'cleaning_request', 'limpien nuestra habitación', 'luggage_storage', 'guardar las maletas', id='es'),
    pytest.param('nl', 'Ik wil geen bagageopslag, maar ik wil wel schoonmaak van mijn kamer.',
        'luggage_storage', 'bagageopslag', 'cleaning_request', 'schoonmaak', id='nl'),
])
def test_refusal_review_stays_with_its_item_when_another_service_is_requested(
        parse_stub, language, text, refused_label, refused_quote,
        affirmative_label, affirmative_quote, reverse_order):
    proposed = [prediction(refused_label, refused_quote), prediction(affirmative_label, affirmative_quote)]
    if reverse_order:
        proposed.reverse()
    result, meta = parse_stub(text, language, proposed, source_label=affirmative_label)
    assert [item['intent'] for item in result] == [item['intent'] for item in proposed]
    by_label = {item['intent']: item for item in result}
    assert by_label[refused_label]['needs_review'] is True
    assert by_label[affirmative_label] == prediction(affirmative_label, affirmative_quote)
    assert meta.needs_review is True
    assert set(meta.uncertain_fields) == {'semantic_negation'}


@pytest.mark.parametrize('language,cancel,keep,waiting,courtesy', [
    pytest.param('en', 'Cancel the luggage storage reservation.',
        'Do not cancel the luggage storage reservation.',
        'I do not want to wait for luggage storage.',
        'No problem with luggage storage; I can leave the bags later.', id='en'),
    pytest.param('de', 'Stornieren Sie bitte die Gepäckaufbewahrung.',
        'Ich möchte die Gepäckaufbewahrung nicht stornieren.',
        'Ich möchte nicht warten, bis die Gepäckaufbewahrung öffnet.',
        'Kein Problem mit der Gepäckaufbewahrung.', id='de'),
    pytest.param('it', 'Annullate la prenotazione del deposito bagagli.',
        'Non voglio annullare la prenotazione del deposito bagagli.',
        'Non desidero aspettare per il deposito bagagli.',
        'Nessun problema con il deposito bagagli.', id='it'),
    pytest.param('fr', 'Veuillez annuler la réservation de la consigne à bagages.',
        'Je ne souhaite pas annuler la réservation de la consigne à bagages.',
        'Nous ne voulons pas attendre pour la consigne à bagages.',
        'Pas de problème avec la consigne à bagages.', id='fr'),
    pytest.param('es', 'Anule la reserva para guardar las maletas.',
        'No quiero anular la reserva para guardar las maletas.',
        'No quiero esperar para guardar las maletas.',
        'Sin problemas para guardar las maletas.', id='es'),
    pytest.param('nl', 'Annuleer de reservering voor bagageopslag.',
        'Ik wil de reservering voor bagageopslag niet annuleren.',
        'Ik wil niet wachten op de bagageopslag.',
        'Geen probleem met bagageopslag.', id='nl'),
])
def test_cancellation_is_distinct_from_waiting_courtesy_and_negated_cancellation(
        language, cancel, keep, waiting, courtesy):
    for text in (keep, waiting, courtesy):
        assert declined_intents(text, language) == set()
    assert declined_intents(cancel, language) == {'luggage_storage'}


@pytest.mark.parametrize('text,expected', [
    ("Please don't forget to clean the room.", set()),
    ("Don't worry about parking; please reserve a space.", set()),
    ('There is no parking problem; please reserve a space.', set()),
    ('I do not need cleaning and parking.', {'cleaning_request', 'parking'}),
    ('I do not need cleaning and I would like parking.', {'cleaning_request'}),
])
def test_refusal_scope_distinguishes_service_objects_from_independent_predicates(text, expected):
    assert declined_intents(text, 'en') == expected


@pytest.mark.parametrize('reverse_order', [False, True], ids=['declined-first', 'affirmative-first'])
def test_shared_target_contract_keeps_opposed_same_service_requests_under_review(parse_stub, reverse_order):
    negative = 'I decline cleaning for the bathroom. Please clean the bedroom.'
    positive = 'Please arrange cleaning for the bathroom. Please clean the bedroom.'
    refused_quote, affirmative_quote = 'cleaning for the bathroom', 'Please clean the bedroom'
    proposed = [prediction('cleaning_request', refused_quote),
                prediction('cleaning_request', affirmative_quote)]
    if reverse_order:
        proposed.reverse()
    affirmative, affirmative_meta = parse_stub(positive, 'en', proposed)
    assert affirmative == proposed and affirmative_meta.needs_review is False
    assert affirmative_meta.uncertain_fields == []

    result, meta = parse_stub(negative, 'en', proposed)
    assert [item['evidence_quote'] for item in result] == [item['evidence_quote'] for item in proposed]
    # GuestIntent has no per-item room/area target. Literal quotes cannot make
    # a scoped exception safe when both items share the event's room target.
    assert all(item['intent'] == 'cleaning_request' and item['needs_review'] is True
               for item in result)
    assert meta.needs_review is True
    assert set(meta.uncertain_fields) == {'semantic_negation'}


def test_repeated_narrow_quote_cannot_disambiguate_refused_and_requested_service(parse_stub):
    text = 'I do not need cleaning in the bathroom. Please arrange cleaning in the bedroom.'
    proposed = [prediction('cleaning_request', 'cleaning'), prediction('cleaning_request', 'cleaning')]
    result, meta = parse_stub(text, 'en', proposed)
    assert len(result) == 2
    assert all(item['intent'] == 'cleaning_request' and item['evidence_quote'] == 'cleaning'
               and item['needs_review'] is True for item in result)
    assert meta.needs_review is True
    assert set(meta.uncertain_fields) == {'semantic_negation'}


@pytest.mark.parametrize('language,text', [
    pytest.param('en', 'I do not need cleaning and reserve parking for me.', id='en-reserve-after-refusal'),
    pytest.param('en', 'Cancel cleaning and reserve parking for tomorrow.', id='en-reserve-after-cancellation'),
    pytest.param('es', 'No queremos limpieza y reserven aparcamiento para esta noche.', id='es-reserve-after-refusal'),
    pytest.param('en', 'Please do not clean the room and hold our bags until lunch.', id='en-hold-after-refusal'),
    pytest.param('en', 'I do not need cleaning and parking is essential for us.', id='en-noun-first-affirmative'),
])
def test_independent_affirmative_continuation_does_not_inherit_refusal(language, text):
    assert declined_intents(text, language) == {'cleaning_request'}


@pytest.mark.parametrize('language,text', [
    pytest.param('fr', 'Je ne veux pas de ménage et de parking.', id='fr-two-objects'),
    pytest.param('nl', 'Ik wil geen schoonmaak en parkeerruimte.', id='nl-two-objects'),
])
def test_one_refusal_covers_both_nominal_service_objects(language, text):
    assert declined_intents(text, language) == {'cleaning_request', 'parking'}


def test_reminder_to_cancel_differs_from_negated_cancellation_or_reassurance():
    assert declined_intents("Please don't forget to cancel parking.", 'en') == {'parking'}
    assert declined_intents("Please don't forget not to clean the room.", 'en') == {'cleaning_request'}
    for text in (
        "Please don't forget not to cancel parking.",
        "Please don't forget to not cancel parking.",
        "Please don't worry if we cancel parking.",
    ):
        assert declined_intents(text, 'en') == set()
