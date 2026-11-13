"""Seal/validate a new synthetic cohort without model calls or host date helpers."""
from collections import Counter
from datetime import datetime,timedelta,timezone
from pathlib import Path
import socket
from urllib import request as urllib_request
from zoneinfo import ZoneInfo

import pytest

from scripts.benchmark_guest_parser import load_cases

PATH=Path(__file__).resolve().parents[1]/'fixtures/parser_local_acceptance.jsonl'
VIENNA=ZoneInfo('Europe/Vienna')
FAMILIES={'ordinary_non_timed','undated_exact_checkin','relative_vienna_date','ambiguous_alternatives'}
LANGUAGES={'de','en','it','fr','es','nl'}


def utc(value):
    return datetime.fromisoformat(value.replace('Z','+00:00'))


def candidates(local_text):
    naive=datetime.fromisoformat(local_text)
    values=set()
    for fold in (0,1):
        candidate=naive.replace(tzinfo=VIENNA,fold=fold).astimezone(timezone.utc)
        if candidate.astimezone(VIENNA).replace(tzinfo=None)==naive:
            values.add(candidate)
    return values


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args,**kwargs):
        raise AssertionError('Acceptance fixture audit must not contact any server')
    monkeypatch.setattr(urllib_request,'urlopen',forbidden)
    monkeypatch.setattr(socket,'create_connection',forbidden)
    monkeypatch.setattr(socket.socket,'connect',forbidden)
    monkeypatch.setattr(socket.socket,'connect_ex',forbidden)


def test_fixture_matches_benchmark_contract_and_covers_six_languages_by_four_families():
    cases=load_cases(PATH)
    assert len(cases)==24
    assert Counter(case['record']['language'] for case in cases)=={language:4 for language in LANGUAGES}
    assert Counter(case['category'] for case in cases)=={family:6 for family in FAMILIES}
    assert {(case['record']['language'],case['category']) for case in cases}=={(language,family) for language in LANGUAGES for family in FAMILIES}
    assert len({case['record']['room_id'] for case in cases})==24
    assert all(case['gold']['adjudication']=='synthetic_manual_independent_acceptance' for case in cases)
    assert all(case['gold']['rationale'] and case['gold']['evidence_notes'] for case in cases)


def test_records_are_new_synthetic_inputs_not_duplicates_of_the_tuning_cohort():
    current=load_cases(PATH)
    original=load_cases(PATH.with_name('parser_benchmark_cases.jsonl'))
    assert {case['record']['text'] for case in current}.isdisjoint(case['record']['text'] for case in original)
    assert {case['case_id'] for case in current}.isdisjoint(case['case_id'] for case in original)
    for case in current:
        record,context=case['record'],case['context']
        assert record['request_id'].startswith('synthetic:')
        assert record['room_id'].startswith('R-LA-')
        assert record['timestamp_utc'].startswith('2027-')
        assert context['as_of_utc']==record['timestamp_utc']
        assert context['room_ids']==[record['room_id']]
        assert utc(record['response_due_utc'])>utc(record['timestamp_utc'])


# Manual local clocks/dates are separate from the fixture's UTC answers. Only
# ZoneInfo round trips are used; no production date resolver or LLM is invoked.
LOCAL_REQUESTS={
    'accept_de_booking_known':'2027-04-08T12:40:00',
    'accept_it_booking_known':'2027-01-21T11:50:00',
    'accept_es_booking_known':'2027-11-11T12:15:00',
    'accept_de_relative_winter_midnight':'2027-02-12T11:20:00',
    'accept_en_relative_spring_offset':'2027-03-28T14:25:00',
    'accept_es_relative_summer_midnight':'2027-07-18T12:35:00',
    'accept_nl_relative_autumn_offset':'2027-11-01T11:45:00',
}


@pytest.mark.parametrize('case_id,local_text',LOCAL_REQUESTS.items())
def test_exact_date_gold_independently_matches_vienna_offset(case_id,local_text):
    case=next(case for case in load_cases(PATH) if case['case_id']==case_id)
    expected=candidates(local_text)
    assert len(expected)==1
    assert expected=={utc(case['gold']['requested_time_utc'])}
    assert case['gold']['needs_review'] is False
    if case['category']=='relative_vienna_date':
        message_day=utc(case['record']['timestamp_utc']).astimezone(VIENNA).date()
        assert datetime.fromisoformat(local_text).date()==message_day+timedelta(days=1)
    else:
        booking=case['context']['booking']
        assert utc(booking['created_utc'])<=utc(case['context']['as_of_utc'])
        assert datetime.fromisoformat(local_text).date()==utc(booking['arrival_utc']).astimezone(VIENNA).date()


@pytest.mark.parametrize('case_id,local_text,expected_utc',[
    ('accept_it_relative_spring_gap','2027-03-28T02:15:00',set()),
    ('accept_fr_relative_autumn_fold','2027-10-31T02:45:00',{'2027-10-31T00:45:00Z','2027-10-31T01:45:00Z'}),
])
def test_dst_unknown_gold_preserves_zero_or_two_real_instants(case_id,local_text,expected_utc):
    case=next(case for case in load_cases(PATH) if case['case_id']==case_id)
    assert candidates(local_text)=={utc(value) for value in expected_utc}
    assert datetime.fromisoformat(local_text).date()==utc(case['record']['timestamp_utc']).astimezone(VIENNA).date()+timedelta(days=1)
    assert case['gold']['requested_time_utc'] is None
    assert case['gold']['needs_review'] is True
    assert 'requested_time_utc' in case['gold']['required_uncertain_fields']


def test_future_booking_contexts_are_explicit_negative_controls_not_date_evidence():
    cases=[case for case in load_cases(PATH) if case['category']=='undated_exact_checkin']
    future=[case for case in cases if utc(case['context']['booking']['created_utc'])>utc(case['context']['as_of_utc'])]
    assert len(future)==3
    for case in future:
        assert case['gold']['requested_time_utc'] is None
        assert case['gold']['needs_review'] is True
        assert {'context.booking','requested_time_utc'}<=set(case['gold']['required_uncertain_fields'])
        assert case['context']['booking']['booking_id']==case['record']['booking_id']


def test_ordinary_and_ambiguous_families_have_distinct_review_policies():
    cases=load_cases(PATH)
    for case in cases:
        if case['category']=='ordinary_non_timed':
            assert case['gold']['requested_time_utc'] is None
            assert case['gold']['needs_review'] is False
        if case['category']=='ambiguous_alternatives':
            assert case['gold']['requested_time_utc'] is None
            assert case['gold']['needs_review'] is True
            assert 'requested_time_utc' in case['gold']['required_uncertain_fields']
