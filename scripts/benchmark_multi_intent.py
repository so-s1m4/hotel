"""Bounded multi-intent evaluation; plan by default, --execute permits calls.

Uses the single-intent benchmark's scoped transport/cache instrumentation, not its
single-intent scoring. Expectations stay evaluator-side; only record/context
reach the production parser. Client transport counts are not server receipts.
"""
from __future__ import annotations

import argparse
from collections import Counter
import copy
import importlib
import json
import os
from pathlib import Path
import sys
import time
from typing import Any
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import benchmark_guest_parser as bench
from common.contracts.schemas import GuestIntentV1, SCHEMA_VERSION

BenchmarkConfig = bench.BenchmarkConfig


def load_cases(path: str | Path) -> list[dict]:
    cases, identifiers, records = [], set(), set()
    with Path(path).open(encoding='utf-8-sig') as stream:
        for line in stream:
            if not line.strip():
                continue
            row = json.loads(line, parse_constant=bench._invalid_constant)
            if not isinstance(row, dict):
                raise ValueError('Multi case must be an object')
            identifier, record, context = row.get('case_id'), row.get('record'), row.get('context')
            if not isinstance(identifier, str) or not identifier.strip() or identifier in identifiers:
                raise ValueError('Multi case IDs must be nonempty and unique')
            if not isinstance(record, dict) or not isinstance(context, dict):
                raise ValueError('Multi record/context must be objects')
            for field in ('request_id', 'timestamp_utc', 'language', 'request_type', 'text'):
                if not isinstance(record.get(field), str) or not record[field].strip():
                    raise ValueError('Missing required source string')
            if not bench._utc(record['timestamp_utc']):
                raise ValueError('Source timestamp must be UTC with Z')
            if 'use_llm' in context and type(context['use_llm']) is not bool:
                raise ValueError('Context use_llm must be boolean')
            expected = row.get('expected_intents')
            if not isinstance(expected, list) or not 1 <= len(expected) <= 32:
                raise ValueError('Expected intents must contain 1..32 items')
            for item in expected:
                if (not isinstance(item, dict) or set(item) != {'intent', 'requested_time_utc', 'needs_review'}
                        or not isinstance(item['intent'], str) or item['intent'] not in bench._INTENTS
                        or type(item['needs_review']) is not bool
                        or (item['requested_time_utc'] is not None and not bench._utc(item['requested_time_utc']))):
                    raise ValueError('Invalid expected intent/time/review tuple')
            if type(row.get('extraction_needs_review')) is not bool:
                raise ValueError('Expected extraction review must be explicit boolean')
            forbidden = row.get('forbidden_affirmative_intents', [])
            if not isinstance(forbidden, list) or any(not isinstance(x, str) or x not in bench._INTENTS for x in forbidden):
                raise ValueError('Forbidden intents must be known labels')
            required = row.get('required_uncertain_fields', [])
            if not isinstance(required, list) or any(not isinstance(x, str) for x in required):
                raise ValueError('Required uncertainty fields must be strings')
            encoded = bench._canonical(record)
            if encoded in records:
                raise ValueError('Duplicate source record')
            bench._canonical(row)
            identifiers.add(identifier)
            records.add(encoded)
            cases.append(row)
    if not cases:
        raise ValueError('At least one multi case is required')
    return cases


def _counts(items: list[dict], fields: tuple[str, ...]) -> Counter:
    # Canonical keys keep malformed raw types comparable without coercing them
    # into valid labels/timestamps, and retain duplicate-intent cardinality.
    return Counter(bench._canonical([item.get(field) for field in fields]) for item in items)


def _literal_checks(items: list[dict], text: str) -> list[dict]:
    return [{'intent': item.get('intent'), 'requested_time_utc': item.get('requested_time_utc'),
             'needs_review': item.get('needs_review'),
             'exact_evidence': isinstance(item.get('evidence_quote'), str) and bool(item['evidence_quote']) and item['evidence_quote'] in text,
             'exact_time_evidence': 'requested_time_text' in item and (
                 item['requested_time_text'] is None and item.get('requested_time_utc') is None
                 or isinstance(item['requested_time_text'], str) and bool(item['requested_time_text']) and item['requested_time_text'] in text),
             'details_grounded': 'details' in item and bench._grounded_details(item['details'], text),
             'explicit_review': type(item.get('needs_review')) is bool}
            for item in items]


def score_normalized(items: list[dict], meta: dict | None, case: dict) -> dict:
    expected = case['expected_intents']
    per_item = _literal_checks(items, case['record']['text'])
    remaining_times = _counts(expected, ('intent', 'requested_time_utc'))
    remaining_reviews = _counts(expected, ('intent', 'requested_time_utc', 'needs_review'))
    for actual, diagnostics in zip(items, per_item):
        for fields, remaining, name in [
            (('intent', 'requested_time_utc'), remaining_times, 'expected_time_match'),
            (('intent', 'requested_time_utc', 'needs_review'), remaining_reviews, 'expected_review_match'),
        ]:
            key = bench._canonical([actual.get(field) for field in fields])
            diagnostics[name] = remaining[key] > 0
            if remaining[key] > 0:
                remaining[key] -= 1
    try:
        for item in items:
            GuestIntentV1.model_validate(item, strict=True)
        schema_valid = bool(items)
    except (ValueError, TypeError):
        schema_valid = False
    flags = {
        'schema_valid': schema_valid,
        'intent_multiset_match': _counts(items, ('intent',)) == _counts(expected, ('intent',)),
        'intent_time_multiset_match': _counts(items, ('intent', 'requested_time_utc')) == _counts(expected, ('intent', 'requested_time_utc')),
        'intent_time_review_multiset_match': _counts(items, ('intent', 'requested_time_utc', 'needs_review')) == _counts(expected, ('intent', 'requested_time_utc', 'needs_review')),
        'exact_evidence': bool(items) and all(x['exact_evidence'] for x in per_item),
        'exact_time_evidence': bool(items) and all(x['exact_time_evidence'] for x in per_item),
        'details_grounded': bool(items) and all(x['details_grounded'] for x in per_item),
        'forbidden_intents_absent': not any(item.get('intent') in case.get('forbidden_affirmative_intents', []) for item in items),
        'extraction_review_match': isinstance(meta, dict) and meta.get('needs_review') is case['extraction_needs_review'],
        'required_uncertainty_present': isinstance(meta, dict) and set(case.get('required_uncertain_fields', [])) <= set(meta.get('uncertain_fields', [])),
    }
    return {**flags, 'passed': all(flags.values()), 'per_intent': per_item}


def score_raw(raw: str | None, case: dict, profile: str) -> dict | None:
    if raw is None:
        return None
    from ingestion.guest_generation import generation_schema
    flags = {name: False for name in ('generation_schema_valid', 'intent_multiset_match',
        'exact_evidence', 'exact_time_evidence', 'details_grounded', 'explicit_review_flags_valid',
        'review_flags_match_host_gold', 'raw_utc_matches_host_gold')}
    # Extraction generation must leave UTC null: host-derived UTC gold cannot
    # identify which raw same-label occurrence owns a review flag. Do not count
    # a missing comparison as a pass or reconstruct dates in the evaluator.
    flags['intent_time_review_multiset_match'] = False if profile == 'contract' else None
    try:
        value = bench._strict_raw_json(raw)
        flags['generation_schema_valid'] = bench._matches_generation_schema(value, generation_schema(profile))
        items = value.get('intents') if isinstance(value, dict) else None
        if not isinstance(items, list) or not items or not all(isinstance(item, dict) for item in items):
            return {**flags, 'per_intent': []}
        per_item = _literal_checks(items, case['record']['text'])
        flags.update(intent_multiset_match=_counts(items, ('intent',)) == _counts(case['expected_intents'], ('intent',)),
            exact_evidence=all(x['exact_evidence'] for x in per_item),
            exact_time_evidence=all(x['exact_time_evidence'] for x in per_item),
            details_grounded=all(x['details_grounded'] for x in per_item),
            explicit_review_flags_valid=all(x['explicit_review'] for x in per_item),
            review_flags_match_host_gold=all(x['explicit_review'] for x in per_item) and _counts(items, ('intent', 'needs_review')) == _counts(case['expected_intents'], ('intent', 'needs_review')),
            raw_utc_matches_host_gold=_counts(items, ('intent', 'requested_time_utc')) == _counts(case['expected_intents'], ('intent', 'requested_time_utc')))
        if profile == 'contract':
            flags['intent_time_review_multiset_match'] = (
                _counts(items, ('intent', 'requested_time_utc', 'needs_review'))
                == _counts(case['expected_intents'], ('intent', 'requested_time_utc', 'needs_review')))
        return {**flags, 'per_intent': per_item}
    except (ValueError, KeyError, TypeError, AttributeError, RecursionError):
        return {**flags, 'per_intent': []}


def run_benchmark(config: BenchmarkConfig, *, parser_module: Any = None) -> dict:
    bench._validate_config(config)
    cases = load_cases(config.input_path)
    selected = cases[:config.limit]
    parser = parser_module or importlib.import_module('ingestion.guest_parser')
    from ingestion.guest_generation import generation_schema_sha256, response_format, response_format_sha256
    cache = (Path(config.cache_root)/config.candidate/config.run_id).resolve()
    out = Path(config.out)
    if out.resolve().is_relative_to(cache):
        raise ValueError('Report must be outside managed cache')
    declared = bench._runtime_metadata(config.runtime_metadata)
    identity = {
        'evaluator': 'multi-intent-v1', 'evaluator_sha256': bench._digest(Path(__file__).read_bytes()),
        'instrumentation_sha256': bench._digest(Path(bench.__file__).read_bytes()),
        'candidate': config.candidate, 'revision': config.revision, 'run_id': config.run_id,
        'workers': config.workers, 'model': config.model, 'endpoint_sha256': bench._digest(config.base_url.rstrip('/')),
        'input_sha256': bench._digest(Path(config.input_path).read_bytes()),
        'selected_sha256': bench._digest(bench._canonical(selected)),
        'parser_version': parser.PARSER_VERSION, 'parser_sha256': bench._digest(Path(parser.__file__).read_bytes()),
        'prompt_sha256': bench._digest(parser._system_prompt(config.generation_profile) if hasattr(parser, '_system_prompt') else parser.SYSTEM_PROMPT),
        'schema_version': SCHEMA_VERSION, 'schema_sha256': bench._digest(bench._canonical(GuestIntentV1.model_json_schema())),
        'generation_profile': config.generation_profile, 'generation_schema_sha256': generation_schema_sha256(config.generation_profile),
        'response_format_mode': config.response_format_mode, 'response_format': response_format(config.response_format_mode, config.generation_profile),
        'response_format_sha256': response_format_sha256(config.response_format_mode, config.generation_profile),
        'max_tokens': config.max_tokens, 'timeout_seconds': config.timeout_seconds,
        'runtime_metadata_sha256': bench._digest(bench._canonical(declared)),
    }
    marker = cache/bench._MARKER
    cache_identity = {'identity_sha256': bench._digest(bench._canonical(identity))}
    if config.warm:
        if not marker.is_file() or json.loads(marker.read_text()) != cache_identity:
            raise ValueError('Warm cache identity does not match this multi run')
    elif cache.exists() and (not cache.is_dir() or any(cache.iterdir())):
        raise ValueError('Fresh mode requires an absent or empty multi cache')
    summary = {name: 0 for name in ('completed_count', 'transport_calls', 'cache_lookups', 'cache_hits',
        'live_llm', 'cache_llm', 'source_label', 'fallback', 'error', 'raw_predictions_count',
        'normalized_host_pass_count', 'live_host_pass_count', 'cached_host_pass_count', 'fallback_host_pass_count')}
    summary.update(selected_count=len(selected), total_seconds=0.0,
        raw_generation_schema_valid_count=None, raw_intent_multiset_match_count=None,
        raw_exact_evidence_count=None, raw_review_flags_match_host_gold_count=None,
        raw_intent_time_review_multiset_match_count=None)
    unknown = {key: value if key == 'selected_count' else None for key, value in summary.items()}
    report = {'schema_version': 'multi-intent-v1', 'mode': 'execute' if config.execute else 'plan',
        'status': 'running' if config.execute else 'planned',
        'manifest': {**identity, 'endpoint': bench._public_url(config.base_url), 'case_ids': [row['case_id'] for row in selected],
            'selected_count': len(selected), 'input_count': len(cases), 'cache_dir': str(cache), 'warm': config.warm,
            'transport_kind': 'injected_parser' if parser_module is not None else 'configured_parser',
            'measurement': 'Original _call_llm invocations, not server receipts; review/raw UTC comparisons use host gold only as diagnostics.',
            'scoring_scope': 'Order-independent intent/time/review tuples and literal quotation grounding; substring checks alone do not establish semantic sufficiency or authorization.',
            'runtime_metadata': {'provenance': 'operator_supplied_not_measured', 'values': declared}},
        'summary': unknown if config.execute else summary, 'results': []}
    key = os.environ.get('LLM_API_KEY', '').strip()
    if config.execute and not key:
        raise ValueError('Execution requires explicit LLM_API_KEY')
    secrets = bench._secrets(config.base_url)
    if not bench._RUN_LOCK.acquire(blocking=False):
        raise RuntimeError('Only one benchmark may run in this process')
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open('x', encoding='utf-8') as stream:
            def write(value):
                stream.seek(0)
                json.dump(bench._scrub(value, secrets), stream, ensure_ascii=False, indent=2)
                stream.write('\n'); stream.truncate(); stream.flush()
            write(report)
            try:
                if config.execute:
                    if not config.warm:
                        cache.mkdir(parents=True, exist_ok=True)
                        with marker.open('x', encoding='utf-8') as metadata:
                            metadata.write(bench._canonical(cache_identity))
                    environment = {'LLM_API_KEY': key, 'LLM_MODEL': config.model, 'LLM_BASE_URL': config.base_url,
                        'LLM_TIMEOUT_SECONDS': str(config.timeout_seconds), 'LLM_GENERATION_PROFILE': config.generation_profile,
                        'LLM_RESPONSE_FORMAT': config.response_format_mode,
                        'LLM_MAX_TOKENS': '' if config.max_tokens is None else str(config.max_tokens),
                        'GUEST_PARSER_CACHE_DIR': str(cache)}
                    # The shared instrumentation needs legacy gold keys, but its
                    # single-intent scores are discarded. This shim is evaluator
                    # metadata only; parse_request_full receives record/context.
                    instrumented = [dict(row, gold={'intents': [x['intent'] for x in row['expected_intents']],
                        'requested_time_utc': None, 'needs_review': row['extraction_needs_review']}) for row in selected]
                    start = time.perf_counter()
                    with patch.dict(os.environ, environment):
                        results = bench._run_cases(config, instrumented, parser)
                    summary['total_seconds'] = time.perf_counter() - start
                    for result, case in zip(results, selected):
                        result.pop('gold', None)
                        result['expectations'] = {field: copy.deepcopy(case[field]) for field in
                            ('expected_intents', 'extraction_needs_review', 'forbidden_affirmative_intents', 'required_uncertain_fields') if field in case}
                        result['checks'] = {
                            'raw': score_raw(result['raw_response'], case, config.generation_profile),
                            'normalized': score_normalized(result['normalized']['intents'], result['normalized']['extraction'], case)}
                        for field, count in result['counts'].items():
                            summary[field] += count
                        if result['path'] in {'live_llm', 'cache_llm', 'source_label', 'fallback', 'error'}:
                            summary[result['path']] += 1
                        passed = result['checks']['normalized']['passed']
                        summary['normalized_host_pass_count'] += int(passed)
                        for path, field in [('live_llm', 'live_host_pass_count'), ('cache_llm', 'cached_host_pass_count'), ('fallback', 'fallback_host_pass_count')]:
                            summary[field] += int(passed and result['path'] == path)
                    predictions = [result for result in results if result['raw_response'] is not None]
                    summary['raw_predictions_count'] = len(predictions)
                    if predictions:
                        for field in ('generation_schema_valid', 'intent_multiset_match', 'exact_evidence', 'review_flags_match_host_gold'):
                            summary['raw_' + field + '_count'] = sum(result['checks']['raw'][field] is True for result in predictions)
                        if config.generation_profile == 'contract':
                            summary['raw_intent_time_review_multiset_match_count'] = sum(
                                result['checks']['raw']['intent_time_review_multiset_match'] is True for result in predictions)
                    summary['completed_count'] = len(results)
                    report.update(results=results, summary=summary, status='complete')
                sanitized = bench._scrub(report, secrets)
                for original, result in zip(report['results'], sanitized['results']):
                    result['raw_response_redacted'] = original['raw_response'] != result['raw_response']
                    result['normalized_redacted'] = original['normalized'] != result['normalized']
                write(sanitized)
                return sanitized
            except BaseException as exc:
                report.update(status='failed' if isinstance(exc, Exception) else 'interrupted', error_type=type(exc).__name__, summary=unknown, results=[])
                try:
                    write(report)
                except BaseException:
                    pass
                raise
    finally:
        bench._RUN_LOCK.release()


def main(argv=None) -> int:
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument('--input', dest='input_path', required=True, type=Path)
    cli.add_argument('--limit', required=True, type=int)
    for name in ('candidate', 'revision', 'run-id', 'base-url', 'model'):
        cli.add_argument('--' + name, required=True)
    for name in ('cache-root', 'out'):
        cli.add_argument('--' + name, required=True, type=Path)
    cli.add_argument('--workers', required=True, choices=(1, 6), type=int)
    cli.add_argument('--timeout-seconds', type=float, default=15.0)
    cli.add_argument('--response-format-mode', choices=('json_object', 'json_schema'), default='json_object')
    cli.add_argument('--generation-profile', choices=('contract', 'extraction'), default='contract')
    cli.add_argument('--max-tokens', type=int)
    cli.add_argument('--runtime-metadata', type=Path)
    cli.add_argument('--execute', action='store_true')
    cli.add_argument('--warm', action='store_true')
    args = vars(cli.parse_args(argv))
    try:
        if args['runtime_metadata'] is not None:
            args['runtime_metadata'] = json.loads(args['runtime_metadata'].read_text(), parse_constant=bench._invalid_constant)
        report = run_benchmark(BenchmarkConfig(**args))
    except Exception as exc:
        print(f'Multi benchmark failed ({type(exc).__name__}).', file=sys.stderr)
        return 2
    print(f"{report['mode']}: {report['manifest']['selected_count']} selected multi cases; {report['summary']['transport_calls']} transport calls.")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
