"""Generic multi-intent evaluator controls; injected transport, no real model."""
from copy import deepcopy
from dataclasses import replace
import json
import os
from unittest.mock import Mock

import pytest

from scripts import benchmark_multi_intent as multi
from tests.ingestion.test_parser_benchmark import isolated_environment_and_network, parser


def example():
    return {'case_id': 'synthetic-multi', 'record': {'request_id': 'SYNTHETIC-MULTI',
        'timestamp_utc': '2026-01-06T09:00:00Z', 'language': 'en', 'request_type': 'extra_towels',
        'room_id': 'R101', 'text': 'Please bring towels and help with the wifi.'},
        'context': {'as_of_utc': '2026-01-06T09:00:00Z', 'room_ids': ['R101']},
        'expected_intents': [
            {'intent': 'extra_towels', 'requested_time_utc': None, 'needs_review': False},
            {'intent': 'wifi_help', 'requested_time_utc': None, 'needs_review': False}],
        'extraction_needs_review': False}


def prediction(intent, quote, *, utc=None, time_text=None, review=False):
    return {'intent': intent, 'requested_time_utc': utc, 'requested_time_text': time_text,
            'evidence_quote': quote, 'details': {}, 'needs_review': review}


def good():
    return [prediction('wifi_help', 'wifi'), prediction('extra_towels', 'towels')]


def config(tmp_path, rows=None, **changes):
    path = tmp_path/'cases.jsonl'
    path.write_text(''.join(json.dumps(row)+'\n' for row in (rows if rows is not None else [example()])))
    return multi.BenchmarkConfig(input_path=path, limit=1, candidate='synthetic', revision='a'*40,
        run_id='run1', cache_root=tmp_path/'cache', out=tmp_path/'report.json',
        base_url='https://example.invalid/v1', model='synthetic-model',
        generation_profile='extraction', response_format_mode='json_schema', **changes)


def test_plan_selects_validated_cases_without_any_parser_calls(parser, tmp_path, monkeypatch):
    cfg = config(tmp_path)
    guard = Mock(side_effect=AssertionError('Planning must not parse'))
    monkeypatch.setattr(parser, 'parse_request_full', guard)
    report = multi.run_benchmark(cfg, parser_module=parser)
    assert report['mode'] == 'plan' and report['status'] == 'planned'
    assert report['summary']['selected_count'] == 1
    assert report['summary']['transport_calls'] == 0 and report['results'] == []
    assert report['summary']['raw_generation_schema_valid_count'] is None
    assert not (tmp_path/'cache').exists()
    guard.assert_not_called()


def test_reversed_model_order_passes_with_raw_metadata_and_counts_retained(parser, tmp_path, monkeypatch):
    cfg = config(tmp_path, execute=True)
    before = dict(os.environ)
    responses = json.dumps({'intents': good()})
    calls = []

    def transport(messages, settings):
        user = json.loads(messages[-1]['content'])
        assert set(user) == {'guest_record', 'known_context'}
        assert 'expected_intents' not in user['guest_record']
        assert settings['generation_profile'] == 'extraction'
        assert settings['response_format_mode'] == 'json_schema'
        calls.append(deepcopy(settings))
        return responses

    monkeypatch.setattr(parser, '_call_llm', transport)
    report = multi.run_benchmark(cfg, parser_module=parser)
    result = report['results'][0]
    assert result['raw_response'] == responses
    assert result['raw_response_sha256'] == multi.bench._digest(responses)
    assert result['normalized']['extraction']['method'] == 'llm'
    assert result['checks']['raw']['generation_schema_valid']
    assert result['checks']['normalized']['passed']
    assert all(row['expected_time_match'] and row['expected_review_match'] for row in result['checks']['normalized']['per_intent'])
    assert report['summary']['live_host_pass_count'] == 1
    assert report['summary']['transport_calls'] == report['summary']['live_llm'] == len(calls) == 1
    assert dict(os.environ) == before
    warm = multi.run_benchmark(replace(cfg, warm=True, out=tmp_path/'warm.json'), parser_module=parser)
    assert len(calls) == 1 and warm['summary']['transport_calls'] == 0
    assert warm['summary']['cached_host_pass_count'] == 1
    assert warm['summary']['raw_generation_schema_valid_count'] is None
    assert warm['results'][0]['checks']['raw'] is None


@pytest.mark.parametrize('variant', ['omitted', 'extraneous', 'duplicated', 'review'])
def test_missing_extra_duplicate_and_wrong_review_fail_independently(parser, tmp_path, monkeypatch, variant):
    items = good()
    if variant == 'omitted':
        items.pop(0)
    elif variant == 'extraneous':
        items.append(prediction('invoice', 'Please'))
    elif variant == 'duplicated':
        items.append(deepcopy(items[0]))
    else:
        items[0]['needs_review'] = True
    monkeypatch.setattr(parser, '_call_llm', lambda *args: json.dumps({'intents': items}))
    report = multi.run_benchmark(config(tmp_path, execute=True), parser_module=parser)
    checks = report['results'][0]['checks']['normalized']
    assert not checks['passed']
    assert report['summary']['live_host_pass_count'] == 0
    if variant == 'review':
        assert checks['intent_multiset_match'] and checks['intent_time_multiset_match']
        assert not checks['intent_time_review_multiset_match'] and not checks['extraction_review_match']
    else:
        assert not checks['intent_multiset_match']


def test_same_intent_different_dates_cannot_swap_review_or_hide_duplicate():
    case = example()
    case['record']['text'] = 'today 11:20; tomorrow 12:30'
    case['expected_intents'] = [
        {'intent': 'early_checkin', 'requested_time_utc': '2026-01-06T10:20:00Z', 'needs_review': False},
        {'intent': 'early_checkin', 'requested_time_utc': '2026-01-07T11:30:00Z', 'needs_review': True}]
    case['extraction_needs_review'] = True
    items = [prediction('early_checkin', 'today 11:20', utc='2026-01-06T10:20:00Z', time_text='today 11:20', review=True),
             prediction('early_checkin', 'tomorrow 12:30', utc='2026-01-07T11:30:00Z', time_text='tomorrow 12:30')]
    score = multi.score_normalized(items, {'needs_review': True, 'uncertain_fields': []}, case)
    assert score['intent_time_multiset_match']
    assert not score['intent_time_review_multiset_match'] and not score['passed']
    items[0]['needs_review'], items[1]['needs_review'] = False, True
    assert multi.score_normalized(list(reversed(items)), {'needs_review': True, 'uncertain_fields': []}, case)['passed']


def test_raw_contract_review_must_match_same_intent_time_tuple_not_marginals():
    case = example()
    case['record']['text'] = 'today 11:20; tomorrow 12:30'
    case['expected_intents'] = [
        {'intent': 'early_checkin', 'requested_time_utc': '2026-01-06T10:20:00Z', 'needs_review': False},
        {'intent': 'early_checkin', 'requested_time_utc': '2026-01-07T11:30:00Z', 'needs_review': True}]
    items = [prediction('early_checkin', 'today 11:20', utc='2026-01-06T10:20:00Z', time_text='today 11:20', review=True),
             prediction('early_checkin', 'tomorrow 12:30', utc='2026-01-07T11:30:00Z', time_text='tomorrow 12:30')]
    raw = multi.score_raw(json.dumps({'intents': items}), case, 'contract')
    assert raw['review_flags_match_host_gold'] and raw['raw_utc_matches_host_gold']
    assert raw['intent_time_review_multiset_match'] is False
    items[0]['needs_review'], items[1]['needs_review'] = False, True
    assert multi.score_raw(json.dumps({'intents': list(reversed(items))}), case, 'contract')['intent_time_review_multiset_match'] is True
    for item in items: item['requested_time_utc'] = None
    extraction = multi.score_raw(json.dumps({'intents': items}), case, 'extraction')
    assert extraction['generation_schema_valid']
    assert extraction['intent_time_review_multiset_match'] is None


@pytest.mark.parametrize('swap', [False, True])
def test_contract_summary_counts_joint_tuple_diagnostic_against_observed_replies(parser, tmp_path, monkeypatch, swap):
    case = example()
    case['record']['text'] = 'today 11:20; tomorrow 12:30'
    case['expected_intents'] = [
        {'intent': 'early_checkin', 'requested_time_utc': '2026-01-06T10:20:00Z', 'needs_review': False},
        {'intent': 'early_checkin', 'requested_time_utc': '2026-01-07T11:30:00Z', 'needs_review': True}]
    case['extraction_needs_review'] = True
    items = [prediction('early_checkin', 'today 11:20', utc='2026-01-06T10:20:00Z', time_text='today 11:20', review=swap),
             prediction('early_checkin', 'tomorrow 12:30', utc='2026-01-07T11:30:00Z', time_text='tomorrow 12:30', review=not swap)]
    monkeypatch.setattr(parser, '_call_llm', lambda *args: json.dumps({'intents': items}))
    cfg = replace(config(tmp_path, [case], execute=True), generation_profile='contract')
    report = multi.run_benchmark(cfg, parser_module=parser)
    assert report['summary']['raw_predictions_count'] == 1
    assert report['summary']['raw_intent_time_review_multiset_match_count'] == int(not swap)


@pytest.mark.parametrize('level', ['envelope', 'intent', 'details'])
def test_raw_duplicate_json_keys_never_get_generation_credit(level):
    raw = json.dumps({'intents': good()})
    if level == 'envelope': raw = '{"intents": [],' + raw[1:]
    elif level == 'intent': raw = raw.replace('"intent": "wifi_help"', '"intent": "invoice", "intent": "wifi_help"')
    else: raw = raw.replace('"details": {}', '"details": {"approximate_time": true, "approximate_time": false}', 1)
    score = multi.score_raw(raw, example(), 'contract')
    assert score['generation_schema_valid'] is False
    assert score['intent_time_review_multiset_match'] is False
    assert score['per_intent'] == []


@pytest.mark.parametrize('failure', ['timeout', 'invalid_json'])
def test_transport_failure_counts_are_retained_without_live_model_credit(parser, tmp_path, monkeypatch, failure):
    def transport(*args):
        if failure == 'timeout':
            raise TimeoutError('synthetic timeout')
        return '{bad json'
    monkeypatch.setattr(parser, '_call_llm', transport)
    report = multi.run_benchmark(config(tmp_path, execute=True), parser_module=parser)
    assert report['status'] == 'complete'
    assert report['summary']['transport_calls'] == report['summary']['fallback'] == 1
    assert report['summary']['live_host_pass_count'] == 0
    result = report['results'][0]
    assert result['normalized']['extraction']['method'] == 'fallback'
    if failure == 'timeout':
        assert result['error_types'] == ['TimeoutError']
        assert result['raw_response'] is None and result['checks']['raw'] is None
    else:
        assert result['raw_response'] == '{bad json'
        assert result['checks']['raw']['generation_schema_valid'] is False


def test_interrupt_preserves_unknown_measurements_and_restores_environment(parser, tmp_path, monkeypatch):
    before = dict(os.environ)
    monkeypatch.setattr(parser, '_call_llm', Mock(side_effect=KeyboardInterrupt))
    cfg = config(tmp_path, execute=True)
    with pytest.raises(KeyboardInterrupt):
        multi.run_benchmark(cfg, parser_module=parser)
    report = json.loads(cfg.out.read_text())
    assert report['status'] == 'interrupted'
    assert report['summary']['selected_count'] == 1
    assert all(value is None for name, value in report['summary'].items() if name != 'selected_count')
    assert dict(os.environ) == before
    assert multi.run_benchmark(replace(cfg, execute=False, run_id='after', out=tmp_path/'after.json'), parser_module=parser)['status'] == 'planned'


def test_validate_entire_input_before_limit_and_prevent_profile_warm_collision(parser, tmp_path, monkeypatch):
    cfg = config(tmp_path, execute=True)
    with cfg.input_path.open('a') as stream:
        stream.write('{bad line\n')
    guard = Mock(side_effect=AssertionError('Invalid input must not parse'))
    original_parse = parser.parse_request_full
    monkeypatch.setattr(parser, 'parse_request_full', guard)
    with pytest.raises(ValueError):
        multi.run_benchmark(cfg, parser_module=parser)
    guard.assert_not_called()
    assert not cfg.out.exists()
    cfg = config(tmp_path, execute=True)
    monkeypatch.setattr(parser, 'parse_request_full', original_parse)
    monkeypatch.setattr(parser, '_call_llm', lambda *args: json.dumps({'intents': good()}))
    multi.run_benchmark(cfg, parser_module=parser)
    with pytest.raises(ValueError, match='Warm cache identity'):
        multi.run_benchmark(replace(cfg, warm=True, generation_profile='contract', out=tmp_path/'other.json'), parser_module=parser)


def test_cli_defaults_to_plan_and_does_not_open_model(parser, tmp_path, monkeypatch):
    cfg = config(tmp_path)
    guard = Mock(side_effect=AssertionError('CLI plan must not parse'))
    monkeypatch.setattr(parser, 'parse_request_full', guard)
    assert multi.main(['--input', str(cfg.input_path), '--limit', '1', '--candidate', cfg.candidate,
        '--revision', cfg.revision, '--run-id', cfg.run_id, '--cache-root', str(cfg.cache_root),
        '--out', str(cfg.out), '--base-url', cfg.base_url, '--model', cfg.model, '--workers', '1']) == 0
    guard.assert_not_called()


def test_actual_host_derives_two_distinct_times_while_raw_extraction_utc_stays_null(parser, tmp_path, monkeypatch):
    case = example()
    case['record'].update(request_type='early_checkin',
        text='May I check in today at 11:40? Could I check out tomorrow at 12:20?')
    case['expected_intents'] = [
        {'intent': 'early_checkin', 'requested_time_utc': '2026-01-06T10:40:00Z', 'needs_review': False},
        {'intent': 'late_checkout', 'requested_time_utc': '2026-01-07T11:20:00Z', 'needs_review': False}]
    items = [prediction('late_checkout', 'check out tomorrow at 12:20', time_text='tomorrow at 12:20'),
             prediction('early_checkin', 'check in today at 11:40', time_text='today at 11:40')]
    monkeypatch.setattr(parser, '_call_llm', lambda *args: json.dumps({'intents': items}))
    report = multi.run_benchmark(config(tmp_path, [case], execute=True), parser_module=parser)
    assert report['summary']['live_host_pass_count'] == 1
    raw = report['results'][0]['checks']['raw']
    assert raw['generation_schema_valid'] and raw['intent_multiset_match']
    assert raw['raw_utc_matches_host_gold'] is False  # diagnostic, not extraction failure
    assert raw['intent_time_review_multiset_match'] is None
    assert report['summary']['raw_intent_time_review_multiset_match_count'] is None
    assert report['results'][0]['checks']['normalized']['intent_time_multiset_match']


def test_forbidden_scope_and_existing_output_are_not_silently_accepted(parser, tmp_path, monkeypatch):
    case = example()
    case['forbidden_affirmative_intents'] = ['cleaning_request']
    score = multi.score_normalized(good() + [prediction('cleaning_request', 'Please')],
                                  {'needs_review': False, 'uncertain_fields': []}, case)
    assert score['forbidden_intents_absent'] is False and score['passed'] is False
    cfg = config(tmp_path)
    multi.run_benchmark(cfg, parser_module=parser)
    before = cfg.out.read_bytes()
    guard = Mock(side_effect=AssertionError('Existing output must fail before parser'))
    monkeypatch.setattr(parser, 'parse_request_full', guard)
    with pytest.raises(FileExistsError):
        multi.run_benchmark(replace(cfg, execute=True), parser_module=parser)
    assert cfg.out.read_bytes() == before
    assert not (tmp_path/'cache').exists()
    guard.assert_not_called()
