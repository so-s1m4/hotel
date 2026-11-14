"""Reproducible local-model pipeline verification, never model-quality certification.

Planning is offline and is the default. --execute runs Quickstart then Dev in
isolated Python processes through the existing run.main CLI. Each uses a fresh
cache. No outputs are promoted to submission files. Ollama identity is observed
via /api/tags and /api/ps (https://docs.ollama.com/api/tags,
https://docs.ollama.com/api/ps); an MLX manifest verifies local files only, not
that the HTTP server actually loaded them. No inference is performed by identity
queries. Transport counts are parser invocations, not server receipts.
"""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import redirect_stdout
import hashlib
from ipaddress import ip_address
import io
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from threading import Lock
from unittest.mock import patch
from urllib.parse import urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def digest(path):
    result = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(block)
    return result.hexdigest()


def read_json(path):
    def invalid(value):
        raise ValueError('Non-finite JSON number')
    return json.loads(Path(path).read_text(encoding='utf-8'), parse_constant=invalid)


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')


def loopback_endpoint(value):
    from ingestion.llm_transport import origin
    _, host, _ = origin(value)
    parts = urlsplit(value)
    if parts.query or parts.fragment:
        raise ValueError('Endpoint must not contain query or fragment')
    try:
        local = ip_address(host).is_loopback
    except ValueError:
        local = host == 'localhost'
    if not local:
        raise ValueError('Only an explicit loopback model endpoint is permitted')
    return value.rstrip('/')


def tree_hashes(root):
    root = Path(root)
    return {str(p.relative_to(root)): digest(p) for p in sorted(root.rglob('*')) if p.is_file()}


def source_hashes():
    paths = [p for area in ('common', 'ingestion', 'engine')
             for p in (ROOT / area).rglob('*')
             if p.is_file() and p.suffix in {'.py', '.json'} and 'tests' not in p.parts]
    paths += [ROOT / name for name in ('run.py', 'pyproject.toml', 'uv.lock')]
    paths.append(Path(__file__).resolve())
    return {str(p.relative_to(ROOT)): digest(p) for p in sorted(paths) if p.is_file()}


def parser_identity(profile, mode):
    from ingestion import guest_parser
    from ingestion.guest_generation import generation_schema_sha256, response_format_sha256
    return {'parser_version': guest_parser.PARSER_VERSION,
            'prompt_sha256': hashlib.sha256(guest_parser._system_prompt(profile).encode()).hexdigest(),
            'generation_schema_sha256': generation_schema_sha256(profile),
            'response_format_sha256': response_format_sha256(mode, profile)}


def _local_json(endpoint, route):
    from ingestion.llm_transport import open_llm
    from urllib.request import Request
    parts = urlsplit(loopback_endpoint(endpoint))
    url = urlunsplit((parts.scheme, parts.netloc, route, '', ''))
    with open_llm(Request(url, method='GET'), timeout=10) as response:
        raw = response.read(2_000_001)
    if len(raw) > 2_000_000:
        raise ValueError('Oversized identity response')
    value = json.loads(raw)
    if not isinstance(value, dict) or not isinstance(value.get('models'), list):
        raise ValueError('Invalid identity response')
    return value['models']


def model_identity(config):
    if config['backend'] == 'mlx':
        manifest_path = Path(config['model_manifest']).resolve()
        manifest = read_json(manifest_path)
        if manifest.get('model') != config['model'] or not manifest.get('files'):
            raise ValueError('MLX manifest must identify this model and its files')
        files = {}
        for row in manifest['files']:
            path = (manifest_path.parent / row['path']).resolve()
            actual = digest(path)
            if actual != row['sha256'] or path in files:
                raise ValueError('MLX model or adapter file hash mismatch/duplicate')
            files[path] = actual
        return {'kind': 'local_mlx_files', 'manifest_sha256': digest(manifest_path),
                'files_sha256': {str(k): v for k, v in files.items()},
                'server_loaded_files_attested': False}
    matches = []
    for row in _local_json(config['endpoint'], '/api/tags'):
        if config['model'] in {row.get('name'), row.get('model')}:
            if row.get('remote_model') or row.get('remote_host'):
                raise ValueError('Cloud-backed models are not local model evidence')
            matches.append(row.get('digest'))
    if len(matches) != 1 or not isinstance(matches[0], str) or not re.fullmatch(r'(sha256:)?[a-fA-F0-9]{64}', matches[0]):
        raise ValueError('Model must have one observed Ollama digest')
    loaded = [r.get('digest') for r in _local_json(config['endpoint'], '/api/ps')
              if config['model'] in {r.get('name'), r.get('model')}]
    if any(value != matches[0] for value in loaded):
        raise ValueError('Loaded model digest differs from installed model')
    return {'kind': 'observed_ollama', 'digest': matches[0], 'loaded_at_probe': bool(loaded)}


def same_model(before, after):
    return {k: v for k, v in before.items() if k != 'loaded_at_probe'} == {
        k: v for k, v in after.items() if k != 'loaded_at_probe'}


def worker(config, profile, output):
    """Instrument the production CLI; no alternative planner or serialization path."""
    from ingestion import guest_parser, replay
    import run as replay_cli
    from engine.validation import validate_outputs
    output = Path(output)
    output.mkdir()
    counters, lock = Counter(), Lock()
    call, read, real_repo = guest_parser._call_llm, guest_parser._read_cache, replay.ReplayData

    def inc(name):
        with lock:
            counters[name] += 1

    def tracked_call(*args, **kwargs):
        inc('transport_attempts')
        started = time.perf_counter()
        trace = {'sequence': counters['transport_attempts']}
        try:
            messages = args[0] if args else kwargs['messages']
            record = json.loads(messages[1]['content'])['guest_record']
            trace['request_id'] = record['request_id']
            trace['record_sha256'] = hashlib.sha256(json.dumps(
                record, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
            trace['messages_sha256'] = hashlib.sha256(json.dumps(
                messages, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
            result = call(*args, **kwargs)
            trace['raw_response'] = result
            return result
        except Exception as exc:
            inc('transport_errors')
            trace['error_type'] = type(exc).__name__
            raise
        finally:
            trace['seconds'] = time.perf_counter() - started
            with lock:
                with (output / 'model_responses.jsonl').open('a', encoding='utf-8') as stream:
                    stream.write(json.dumps(trace, ensure_ascii=False) + '\n')
                if counters['transport_attempts'] % 25 == 0:
                    write_json(output / 'progress.json', dict(counters))

    def tracked_read(*args, **kwargs):
        inc('cache_reads')
        result = read(*args, **kwargs)
        if result is not None:
            inc('cache_hits')
        return result

    class ObservedReplay(real_repo):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            counters['expected_guest_events'] = sum(bool(row.get('timestamp_utc')) for row in self.ds.guest_requests)

        def iter_events(self):
            for event in super().iter_events():
                if event.event_type == 'guest_request':
                    inc('guest_events')
                    meta = event.extraction
                    inc('method_' + (meta.method if meta else 'missing'))
                    if (meta and meta.needs_review) or any(i.get('needs_review') for i in event.payload.get('intents', [])):
                        inc('review_events')
                    with (output / 'guest_events.jsonl').open('a', encoding='utf-8') as stream:
                        stream.write(event.model_dump_json() + '\n')
                yield event

    env = {'LLM_BASE_URL': config['endpoint'], 'LLM_MODEL': config['model'],
           'LLM_API_KEY': 'local-release-no-secret', 'GUEST_PARSER_OFFLINE': '0',
           'GUEST_PARSER_WORKERS': str(config['workers']),
           'LLM_TIMEOUT_SECONDS': str(config['timeout_seconds']),
           'LLM_MAX_TOKENS': str(config['max_tokens']),
           'LLM_RESPONSE_FORMAT': config['response_format'],
           'LLM_GENERATION_PROFILE': config['generation_profile'],
           'GUEST_PARSER_CACHE_DIR': str(output / 'cache')}
    args = ['run.py', '--data-dir', config['datasets'][profile], '--llm',
            '--out', str(output / 'decisions.jsonl'), '--audit', str(output / 'audit.jsonl')]
    if config.get('engine_config'):
        args += ['--config', config['engine_config']]
    captured = io.StringIO()
    with patch.dict(os.environ, env), patch.object(sys, 'argv', args), redirect_stdout(captured), \
            patch.object(guest_parser, '_call_llm', tracked_call), \
            patch.object(guest_parser, '_read_cache', tracked_read), patch.object(replay, 'ReplayData', ObservedReplay):
        code = replay_cli.main()
    run_meta = json.loads(captured.getvalue())
    validation = validate_outputs(config['datasets'][profile], output / 'decisions.jsonl', output / 'audit.jsonl')
    write_json(output / 'validation.json', validation)
    keys = ('transport_attempts', 'transport_errors', 'cache_reads', 'cache_hits', 'guest_events', 'expected_guest_events',
            'method_llm', 'method_fallback', 'method_source_label', 'method_missing', 'review_events')
    result = {'status': 'complete' if code == 0 else 'failed', 'run': run_meta,
              'counts': {k: counters[k] for k in keys}, 'validation': validation,
              'artifact_hashes_sha256': {p.name: digest(p) for p in sorted(output.glob('*.jsonl'))}}
    write_json(output / 'worker.json', result)
    return result


def gate(report):
    """Fail closed on missing evidence; review/quality is deliberately separate."""
    failures = []
    if any(not isinstance(report.get(key), dict) or not report[key]
           for key in ('sources_before', 'sources_after', 'inputs_before', 'inputs_after')):
        failures.append('source_or_input_identity_missing')
    elif any(set(report[key]) != {'quickstart', 'dev'} or any(
            not isinstance(value, dict) or not value for value in report[key].values())
             for key in ('inputs_before', 'inputs_after')):
        failures.append('dataset_identity_incomplete')
    if report.get('sources_before') != report.get('sources_after'):
        failures.append('source_drift')
    if report.get('inputs_before') != report.get('inputs_after'):
        failures.append('input_drift')
    if not report.get('model_before') or not report.get('model_after') or not same_model(report['model_before'], report['model_after']):
        failures.append('model_identity_missing_or_changed')
    for profile in ('quickstart', 'dev'):
        row = report.get('profiles', {}).get(profile, {})
        run_meta, counts, validation = row.get('run', {}), row.get('counts', {}), row.get('validation', {})
        ok = (row.get('status') == 'complete' and run_meta.get('status') == 'replay_complete'
              and run_meta.get('events', 0) > 0 and run_meta.get('safety_rejections') == 0
              and validation.get('valid') is True and validation.get('error_count') == 0
              and counts.get('guest_events', 0) > 0 and counts.get('method_llm') == counts.get('guest_events')
              and counts.get('expected_guest_events') == counts.get('guest_events')
              and counts.get('transport_attempts') == counts.get('guest_events')
              and all(counts.get(k) == 0 for k in ('transport_errors', 'cache_hits', 'method_fallback', 'method_source_label', 'method_missing'))
              and isinstance(counts.get('review_events'), int))
        if not ok:
            failures.append(profile + '_evidence_incomplete_or_failed')
    return failures


def verify(config, execute=False):
    endpoint = loopback_endpoint(config['endpoint'])
    if not isinstance(config['model'], str) or not config['model'].strip():
        raise ValueError('Model is required')
    if config['workers'] != 1 or not math.isfinite(config['timeout_seconds']) or not 0.1 <= config['timeout_seconds'] <= 60 or not 1 <= config['max_tokens'] <= 16384:
        raise ValueError('Use one worker, timeout 0.1..60 and token cap 1..16384')
    config = dict(config, endpoint=endpoint)
    roots = {k: Path(v).resolve() for k, v in config['datasets'].items()}
    if set(roots) != {'quickstart', 'dev'} or roots['quickstart'] == roots['dev']:
        raise ValueError('Provide distinct Quickstart and Dev datasets')
    for profile, root in roots.items():
        if read_json(root / 'manifest.json').get('profile') != profile:
            raise ValueError('Dataset profile must match its independent run')
    output = Path(config['output']).resolve()
    if output.exists() or any(output.is_relative_to(p) or p.is_relative_to(output) for p in roots.values()):
        raise ValueError('Use a new output directory outside both input datasets')
    if config['backend'] not in {'ollama', 'mlx'} or config['backend'] == 'mlx' and not config.get('model_manifest'):
        raise ValueError('Select Ollama or provide an MLX file manifest')
    config['datasets'] = {k: str(v) for k, v in roots.items()}
    config['output'] = str(output)
    report = {'schema_version': '1.0', 'status': 'planned', 'model_quality': 'not_certified',
              'scope': 'Pipeline verification only. No release acceptance or semantic accuracy claim.',
              'python_version': sys.version, 'config': config, 'parser_identity': parser_identity(config['generation_profile'], config['response_format']),
              'sources_before': source_hashes(), 'profiles': {}}
    if not execute:
        return report
    output.mkdir(parents=True, exist_ok=False)
    report['status'] = 'running'
    report_path = output / 'verification.json'
    write_json(report_path, report)
    try:
        report['inputs_before'] = {k: tree_hashes(v) for k, v in roots.items()}
        report['model_before'] = model_identity(config)
        if config.get('engine_config'):
            report['engine_config_sha256_before'] = digest(config['engine_config'])
        write_json(output / 'config.json', config)
        for profile in ('quickstart', 'dev'):
            command = [sys.executable, str(Path(__file__).resolve()), '--worker', profile,
                       '--worker-config', str(output / 'config.json')]
            completed = subprocess.run(command, cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if completed.returncode != 0:
                report['profiles'][profile] = {'status': 'failed', 'exit_code': completed.returncode}
                break
            report['profiles'][profile] = read_json(output / profile / 'worker.json')
            observed = model_identity(config)
            report['profiles'][profile]['model_after'] = observed
            if not same_model(report['model_before'], observed):
                raise ValueError('Model identity changed during replay')
            if config['backend'] == 'ollama' and not observed['loaded_at_probe']:
                raise ValueError('No matching running model was observed after live replay')
            write_json(report_path, report)
        report['model_after'] = model_identity(config)
        report['inputs_after'] = {k: tree_hashes(v) for k, v in roots.items()}
        report['sources_after'] = source_hashes()
        report['failures'] = gate(report)
        if config.get('engine_config') and digest(config['engine_config']) != report['engine_config_sha256_before']:
            report['failures'].append('engine_config_drift')
        report['status'] = 'pipeline_verified' if not report['failures'] else 'verification_failed'
    except BaseException as exc:
        report['status'] = 'verification_failed'
        report['failure_type'] = type(exc).__name__
        write_json(report_path, report)
        raise
    write_json(report_path, report)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--quickstart', type=Path)
    parser.add_argument('--dev', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--model')
    parser.add_argument('--endpoint', default='http://127.0.0.1:11434/v1')
    parser.add_argument('--backend', choices=['ollama', 'mlx'], default='ollama')
    parser.add_argument('--model-manifest', type=Path)
    parser.add_argument('--engine-config', type=Path)
    parser.add_argument('--generation-profile', choices=['contract', 'extraction'], default='extraction')
    parser.add_argument('--response-format', choices=['json_object', 'json_schema'], default='json_schema')
    parser.add_argument('--timeout-seconds', type=float, default=30)
    parser.add_argument('--max-tokens', type=int, default=1024)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--worker', choices=['quickstart', 'dev'], help=argparse.SUPPRESS)
    parser.add_argument('--worker-config', type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        if args.worker:
            config = read_json(args.worker_config)
            worker(config, args.worker, Path(config['output']) / args.worker)
            return 0
        if any(v is None for v in (args.quickstart, args.dev, args.output, args.model)):
            parser.error('--quickstart, --dev, --output and --model are required')
        config = {'datasets': {'quickstart': str(args.quickstart), 'dev': str(args.dev)},
                  'output': str(args.output), 'model': args.model, 'endpoint': args.endpoint,
                  'backend': args.backend, 'model_manifest': str(args.model_manifest) if args.model_manifest else None,
                  'engine_config': str(args.engine_config.resolve()) if args.engine_config else None,
                  'workers': 1, 'timeout_seconds': args.timeout_seconds, 'max_tokens': args.max_tokens,
                  'generation_profile': args.generation_profile, 'response_format': args.response_format}
        report = verify(config, args.execute)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        print(json.dumps({'status': 'verification_failed', 'error_type': type(exc).__name__}), file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report['status'] in {'planned', 'pipeline_verified'} else 1


if __name__ == '__main__':
    raise SystemExit(main())
