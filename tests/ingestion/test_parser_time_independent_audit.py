"""Source alternatives must survive a model selecting only one time quotation."""
from copy import deepcopy

import pytest

from ingestion.guest_parser import _validated


ALTERNATIVES = [
    ('en', 'Can we arrive tomorrow at 13:00 or at 14:00?', 'tomorrow at 13:00'),
    ('de', 'Können wir morgen um 13:00 oder um 14:00 anreisen?', 'morgen um 13:00'),
    ('it', 'Possiamo arrivare domani alle 13:00 o alle 14:00?', 'domani alle 13:00'),
    ('fr', 'Pouvons-nous arriver demain à 13:00 ou à 14:00 ?', 'demain à 13:00'),
    ('es', '¿Podemos llegar mañana a las 13:00 o a las 14:00?', 'mañana a las 13:00'),
    ('nl', 'Kunnen we morgen om 13:00 of om 14:00 aankomen?', 'morgen om 13:00'),
]


def extract(language, text, quotations):
    record = {'text': text, 'language': language, 'request_type': 'early_checkin',
              'timestamp_utc': '2026-01-06T09:00:00Z'}
    raw = [{'intent': 'early_checkin', 'requested_time_utc': None,
            'requested_time_text': quote, 'evidence_quote': quote,
            'details': {}, 'needs_review': False} for quote in quotations]
    before = deepcopy((record, raw))
    result, uncertain = _validated(raw, record, {}, set())
    assert (record, raw) == before
    return result, uncertain


@pytest.mark.parametrize('language,text,first_quote', ALTERNATIVES)
@pytest.mark.parametrize('selected', ['first', 'second', 'both'])
def test_preposition_between_alternative_joiner_and_clock_does_not_make_a_selection(language, text, first_quote, selected):
    quotes = [first_quote] if selected == 'first' else ['14:00'] if selected == 'second' else [first_quote, '14:00']
    rows, uncertain = extract(language, text, quotes)
    assert all(row['requested_time_utc'] is None for row in rows)
    assert all(row['needs_review'] for row in rows)
    assert 'requested_time_utc' in uncertain


@pytest.mark.parametrize('text', [
    'Can we arrive tomorrow at 113:00?',
    'Can we arrive tomorrow at 13:001?',
])
def test_quoted_clock_cannot_cut_digits_from_the_source_token(text):
    rows, uncertain = extract('en', text, ['13:00'])
    assert rows[0]['requested_time_utc'] is None
    assert rows[0]['needs_review']
    assert 'requested_time_utc' in uncertain


@pytest.mark.parametrize('language,first,second', [
    ('en', 'tomorrow at 13:00', 'the day after tomorrow at 14:00'),
    ('de', 'morgen um 13:00', 'übermorgen um 14:00'),
    ('it', 'domani alle 13:00', 'dopodomani alle 14:00'),
    ('fr', 'demain à 13:00', 'après-demain à 14:00'),
    ('es', 'mañana a las 13:00', 'pasado mañana a las 14:00'),
    ('nl', 'morgen om 13:00', 'overmorgen om 14:00'),
])
def test_independent_requests_with_their_own_dates_keep_distinct_exact_times(language, first, second):
    rows, uncertain = extract(language, first + '; ' + second + '.', [first, second])
    assert [row['requested_time_utc'] for row in rows] == ['2026-01-07T12:00:00Z', '2026-01-08T13:00:00Z']
    assert not any(row['needs_review'] for row in rows)
    assert not uncertain
