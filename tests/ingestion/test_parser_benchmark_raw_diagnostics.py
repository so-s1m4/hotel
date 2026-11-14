"""Additive raw diagnostics distinguish generation grammar from host gold."""
from copy import deepcopy
from dataclasses import replace
import json
from unittest.mock import Mock

import pytest

from scripts import benchmark_guest_parser as bench
from tests.ingestion.test_parser_benchmark import (
    isolated_environment_and_network, benchmark, parser, cases,
    config_for, guest_record, reply, write_cases,
)


def item():
    return {'intent': 'extra_towels', 'requested_time_utc': None, 'requested_time_text': None,
            'evidence_quote': 'fresh towels', 'details': {}, 'needs_review': False}


def case():
    return {'record': {'text': 'Please bring fresh towels.'},
            'gold': {'intents': ['extra_towels'], 'requested_time_utc': None, 'needs_review': False}}


def diagnose(value, profile='extraction'):
    return bench._raw_diagnostics(json.dumps(value), case(), profile)


def test_valid_raw_has_separate_diagnostics_without_an_opaque_combined_pass():
    result = diagnose({'intents': [item()]})
    assert all(result[name] is True for name in bench._RAW_DIAGNOSTIC_CHECKS)
    assert result['model_review_flags'] == [False]
    assert 'passed' not in result


def test_public_defaults_cannot_substitute_for_required_generation_fields():
    raw = json.dumps({'intents': [{'intent': 'extra_towels', 'evidence_quote': 'fresh towels'}]})
    assert bench._raw_checks(raw, case())['passed'] is True  # legacy meaning preserved
    result = bench._raw_diagnostics(raw, case(), 'extraction')
    assert result['generation_schema_valid'] is False
    assert result['explicit_review_flags_valid'] is False
    assert result['review_flags_match_host_gold'] is False
    assert result['time_span_substrings_valid'] is False
    assert result['utc_generation_policy_valid'] is False
    assert result['model_review_flags'] == [None]


@pytest.mark.parametrize('mutation', [
    {'needs_review': 'false'}, {'requested_time_utc': 0}, {'requested_time_text': 12},
    {'evidence_quote': ''}, {'details': []}, {'intent': 'authorize_room_release'},
    {'extra': 'not an allowed generated field'},
])
def test_full_schema_rejects_wrong_types_empty_evidence_and_unknown_item_fields(mutation):
    candidate = {**item(), **mutation}
    assert diagnose({'intents': [candidate]})['generation_schema_valid'] is False


@pytest.mark.parametrize('raw', ['{', '{}', '{"intents":[]}', '{"intents":["string"]}',
                                  '{"intents":[NaN]}', '{"intents":true}'])
def test_malformed_observed_reply_is_failed_diagnostics_not_unknown(raw):
    result = bench._raw_diagnostics(raw, case(), 'contract')
    assert result is not None
    assert all(result[name] is False for name in bench._RAW_DIAGNOSTIC_CHECKS)
    assert bench._raw_diagnostics(None, case(), 'contract') is None


def test_unknown_root_fields_fail_even_when_intent_and_quote_match():
    result = diagnose({'intents': [item()], 'consent_granted': True})
    assert result['generation_schema_valid'] is False
    assert result['intents_match'] and result['evidence_substrings_valid']


@pytest.mark.parametrize('level', ['envelope', 'intent', 'details'])
def test_duplicate_keys_at_any_depth_fail_new_diagnostics_without_rewriting_legacy(level):
    encoded = json.dumps({'intents': [item()]})
    if level == 'envelope':
        encoded = '{"intents": [],' + encoded[1:]
    elif level == 'intent':
        encoded = encoded.replace('"intent": "extra_towels"', '"intent": "wifi_help", "intent": "extra_towels"')
    else:
        encoded = encoded.replace('"details": {}', '"details": {"approximate_time": true, "approximate_time": false}')
    assert bench._raw_checks(encoded, case())['passed'] is True
    result = bench._raw_diagnostics(encoded, case(), 'contract')
    assert result is not None
    assert all(result[name] is False for name in bench._RAW_DIAGNOSTIC_CHECKS)
    assert result['model_review_flags'] is None


@pytest.mark.parametrize('change,failed_check', [
    ({'intent': 'wifi_help'}, 'intents_match'),
    ({'evidence_quote': 'Please send champagne'}, 'evidence_substrings_valid'),
    ({'requested_time_text': 'tomorrow at noon'}, 'time_span_substrings_valid'),
    ({'details': {'assistance_requested': 'champagne'}}, 'details_grounded'),
])
def test_valid_contract_shape_is_not_quoted_semantic_correctness(change, failed_check):
    result = diagnose({'intents': [{**item(), **change}]}, 'contract')
    assert result['generation_schema_valid'] is True
    assert result[failed_check] is False


def test_extraction_policy_rejects_utc_and_details_that_contract_generation_allows():
    value = {'intents': [{**item(), 'requested_time_utc': '2027-07-03T10:20:00Z',
                          'details': {'assistance_requested': 'fresh towels'}}]}
    assert diagnose(value, 'contract')['generation_schema_valid'] is True
    extraction = diagnose(value, 'extraction')
    assert extraction['generation_schema_valid'] is False
    assert extraction['utc_generation_policy_valid'] is False
    assert extraction['details_grounded'] is True  # literal grounding is distinct


def test_raw_review_is_explicit_and_gold_comparison_is_diagnostic_only():
    value = {'intents': [{**item(), 'needs_review': True}]}
    result = diagnose(value)
    assert result['generation_schema_valid'] and result['explicit_review_flags_valid']
    assert result['model_review_flags'] == [True]
    assert result['review_flags_match_host_gold'] is False
    assert bench._raw_checks(json.dumps(value), case())['passed'] is True  # legacy ignores raw review


def test_unknown_future_schema_keyword_is_not_silently_ignored():
    from ingestion.guest_generation import generation_schema
    schema = deepcopy(generation_schema('extraction'))
    schema['$defs']['intent']['properties']['evidence_quote']['maxLength'] = 2
    assert bench._matches_generation_schema({'intents': [item()]}, schema) is False


def test_report_counts_profile_diagnostics_without_changing_legacy_raw_utc(benchmark, parser, cases, tmp_path, monkeypatch):
    candidate = deepcopy(cases[0])
    candidate['record'].update(text='Could I check in tomorrow at 11:40?', request_type='early_checkin')
    candidate['gold'].update(intents=['early_checkin'], requested_time_utc='2026-01-07T10:40:00Z')
    path = write_cases(tmp_path/'cases.jsonl', [candidate])
    raw = {'intents': [{'intent': 'early_checkin', 'requested_time_utc': None,
        'requested_time_text': 'tomorrow at 11:40', 'evidence_quote': 'tomorrow at 11:40',
        'details': {}, 'needs_review': False}]}
    monkeypatch.setattr(parser, '_call_llm', lambda *args: json.dumps(raw))
    cfg = config_for(benchmark, tmp_path, path, limit=1, execute=True, generation_profile='extraction')
    report = benchmark.run_benchmark(cfg, parser_module=parser)
    assert report['summary']['raw_gold_pass_count'] == 0
    assert report['summary']['host_gold_pass_count'] == 1
    assert report['summary']['raw_diagnostics_observed_count'] == 1
    assert report['summary']['raw_generation_schema_valid_count'] == 1
    assert report['results'][0]['checks']['raw_diagnostics']['utc_generation_policy_valid'] is True
    warm = benchmark.run_benchmark(replace(cfg, warm=True, out=tmp_path/'warm.json'), parser_module=parser)
    assert warm['summary']['raw_diagnostics_observed_count'] == 0
    assert warm['summary']['raw_generation_schema_valid_count'] is None
    assert warm['results'][0]['checks']['raw_diagnostics'] is None


def test_interruption_keeps_all_new_diagnostics_unknown(benchmark, parser, cases, tmp_path, monkeypatch):
    path = write_cases(tmp_path/'cases.jsonl', cases[:1])
    monkeypatch.setattr(parser, '_call_llm', Mock(side_effect=KeyboardInterrupt))
    cfg = config_for(benchmark, tmp_path, path, execute=True)
    with pytest.raises(KeyboardInterrupt):
        benchmark.run_benchmark(cfg, parser_module=parser)
    report = json.loads(cfg.out.read_text())
    assert report['status'] == 'interrupted'
    assert report['summary']['raw_diagnostics_observed_count'] is None
    assert all(report['summary']['raw_' + name + '_count'] is None for name in bench._RAW_DIAGNOSTIC_CHECKS)
