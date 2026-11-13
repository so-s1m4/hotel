"""Independent narrow-quote controls for clock precision and date scope."""
from copy import deepcopy

import pytest

from ingestion.guest_parser import _validated


MESSAGE = '2026-01-05T10:00:00Z'


def item(label,quote,evidence,utc):
    return {'intent':label,'requested_time_utc':utc,'requested_time_text':quote,
            'evidence_quote':evidence,'details':{},'needs_review':False}


@pytest.mark.parametrize('token',['13:00:59','13:00.001','-13:00'])
@pytest.mark.parametrize('quote_scope',['narrow','whole_token'])
def test_unsupported_numeric_clock_precision_cannot_be_truncated_into_a_confident_minute(token,quote_scope):
    text=f'I arrive tomorrow at {token}.'
    quote='13:00' if quote_scope=='narrow' else token
    record={'text':text,'language':'en','timestamp_utc':MESSAGE,'request_type':'arrival_delay'}
    raw=[item('arrival_delay',quote,quote,'2026-01-06T12:00:00Z')]
    before=deepcopy((record,raw))
    result,uncertain=_validated(raw,record,{},set())
    assert result[0]['requested_time_utc'] is None
    assert result[0]['needs_review'] and 'requested_time_utc' in uncertain
    assert (record,raw)==before


@pytest.mark.parametrize('separator',[' and ','; '])
@pytest.mark.parametrize('short_quote',[False,True])
def test_independent_requests_with_own_dates_remain_resolvable_across_and_or_semicolon(separator,short_quote):
    first='I check in tomorrow at 13:00'
    second='I check out the day after tomorrow at 13:00'
    text=first+separator+second+'.'
    raw=[item('early_checkin','13:00' if short_quote else 'tomorrow at 13:00',first,'2026-01-06T12:00:00Z'),
         item('late_checkout','13:00' if short_quote else 'the day after tomorrow at 13:00',second,'2026-01-07T12:00:00Z')]
    result,uncertain=_validated(raw,{'text':text,'language':'en','timestamp_utc':MESSAGE,
                                   'request_type':'early_checkin'}, {},set())
    assert [value['requested_time_utc'] for value in result]==['2026-01-06T12:00:00Z','2026-01-07T12:00:00Z']
    assert not uncertain and all(not value['needs_review'] for value in result)


def test_ordinary_sentence_period_is_not_treated_as_fractional_clock_precision():
    text='I arrive tomorrow at 13:00.'
    result,uncertain=_validated([item('arrival_delay','13:00','13:00','2026-01-06T12:00:00Z')],
        {'text':text,'language':'en','timestamp_utc':MESSAGE,'request_type':'arrival_delay'}, {},set())
    assert result[0]['requested_time_utc']=='2026-01-06T12:00:00Z'
    assert not result[0]['needs_review'] and not uncertain
