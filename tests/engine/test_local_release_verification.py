"""Release tooling controls use only temporary datasets and injected replies."""
from copy import deepcopy
import csv
import json
from pathlib import Path
import socket
from types import SimpleNamespace

import pytest

from scripts import verify_local_release as release


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError('No network allowed in release tooling tests')
    monkeypatch.setattr(socket, 'create_connection', blocked)
    monkeypatch.setattr(socket.socket, 'connect', blocked)


@pytest.fixture
def config(tmp_path):
    datasets = {}
    for profile in ('quickstart', 'dev'):
        root = tmp_path / profile
        root.mkdir()
        (root / 'manifest.json').write_text(json.dumps({'profile': profile}))
        datasets[profile] = str(root)
    return {'datasets': datasets, 'output': str(tmp_path / 'new-release'),
            'endpoint': 'http://127.0.0.1:11434/v1', 'model': 'fixture:3b',
            'backend': 'ollama', 'model_manifest': None, 'engine_config': None,
            'workers': 1, 'timeout_seconds': 15., 'max_tokens': 512,
            'generation_profile': 'extraction', 'response_format': 'json_schema'}


def complete_profile():
    return {'status': 'complete',
            'run': {'status': 'replay_complete', 'events': 4, 'decisions': 0, 'safety_rejections': 0},
            'validation': {'valid': True, 'error_count': 0},
            'counts': {'guest_events': 1, 'expected_guest_events': 1, 'method_llm': 1, 'transport_attempts': 1,
                       'transport_errors': 0, 'cache_hits': 0, 'method_fallback': 0,
                       'method_source_label': 0, 'method_missing': 0, 'review_events': 1}}


def complete_report():
    return {'sources_before': {'parser': 'a'}, 'sources_after': {'parser': 'a'},
            'inputs_before': {'quickstart': {'manifest.json': 'a'}, 'dev': {'manifest.json': 'b'}},
            'inputs_after': {'quickstart': {'manifest.json': 'a'}, 'dev': {'manifest.json': 'b'}},
            'model_before': {'digest': 'd', 'loaded_at_probe': False},
            'model_after': {'digest': 'd', 'loaded_at_probe': True},
            'profiles': {p: complete_profile() for p in ('quickstart', 'dev')}}


@pytest.mark.parametrize('keys', [
    ('sources_before', 'sources_after'), ('inputs_before', 'inputs_after'),
    ('sources_before', 'sources_after', 'inputs_before', 'inputs_after')])
@pytest.mark.parametrize('replacement', [None, {}])
def test_gate_rejects_matching_missing_or_empty_identity_maps(keys, replacement):
    report = complete_report()
    for key in keys:
        report[key] = replacement
    assert release.gate(report)


def test_gate_rejects_matching_missing_dataset_fingerprints():
    report = complete_report()
    report['inputs_before'] = report['inputs_after'] = {'quickstart': {}, 'dev': {}}
    assert release.gate(report)


def test_plan_has_no_network_output_or_subprocess(config, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('Planning must not probe models or run workers')
    monkeypatch.setattr(release, 'model_identity', forbidden)
    monkeypatch.setattr(release.subprocess, 'run', forbidden)
    report = release.verify(config)
    assert report['status'] == 'planned' and report['model_quality'] == 'not_certified'
    assert report['parser_identity']['generation_schema_sha256']
    assert not Path(config['output']).exists()


@pytest.mark.parametrize('endpoint', ['https://example.org/v1', 'http://127.0.0.1.evil.test/v1',
    'http://user:secret@localhost:11434/v1', 'http://localhost:11434/v1?token=secret',
    'http://localhost/v1#secret', 'file:///tmp/socket'])
def test_endpoints_fail_before_any_model_call(config, endpoint):
    config['endpoint'] = endpoint
    with pytest.raises(ValueError):
        release.verify(config, execute=True)
    assert not Path(config['output']).exists()


@pytest.mark.parametrize('endpoint', ['http://127.0.0.1:11434/v1', 'http://[::1]:11434/v1', 'http://localhost:8080/v1'])
def test_explicit_loopback_is_allowed(endpoint):
    assert release.loopback_endpoint(endpoint) == endpoint


def test_dataset_separation_and_existing_output_fail_closed(config):
    destination = Path(config['output']); destination.mkdir()
    (destination / 'keep.txt').write_text('existing generation')
    with pytest.raises(ValueError):
        release.verify(config)
    assert (destination / 'keep.txt').read_text() == 'existing generation'
    destination.joinpath('keep.txt').unlink(); destination.rmdir()
    config['datasets']['dev'] = config['datasets']['quickstart']
    with pytest.raises(ValueError):
        release.verify(config)


def test_wrong_profile_and_output_inside_input_rejected(config):
    manifest = Path(config['datasets']['dev']) / 'manifest.json'
    manifest.write_text('{"profile":"quickstart"}')
    with pytest.raises(ValueError):
        release.verify(config)
    manifest.write_text('{"profile":"dev"}')
    config['output'] = str(manifest.parent / 'release')
    with pytest.raises(ValueError):
        release.verify(config)


def test_gate_allows_review_without_claiming_model_quality():
    assert release.gate(complete_report()) == []


@pytest.mark.parametrize('change', ['missing_profile', 'fallback', 'missing_review_count',
    'invalid_output', 'missing_validation', 'transport_error', 'cache_only',
    'rejection', 'source_drift', 'input_drift', 'model_drift', 'no_model', 'lost_guest'])
def test_gate_rejects_missing_or_failed_evidence(change):
    report = complete_report()
    row = report['profiles']['dev']
    if change == 'missing_profile': del report['profiles']['dev']
    elif change == 'fallback': row['counts']['method_fallback'] = 1
    elif change == 'missing_review_count': del row['counts']['review_events']
    elif change == 'invalid_output': row['validation']['valid'] = False
    elif change == 'missing_validation': del row['validation']
    elif change == 'transport_error': row['counts']['transport_errors'] = 1
    elif change == 'cache_only': row['counts'].update(cache_hits=1, transport_attempts=0)
    elif change == 'rejection': row['run']['safety_rejections'] = 1
    elif change == 'source_drift': report['sources_after']['parser'] = 'changed'
    elif change == 'input_drift': report['inputs_after']['dev'] = 'changed'
    elif change == 'model_drift': report['model_after']['digest'] = 'changed'
    elif change == 'no_model': del report['model_after']
    elif change == 'lost_guest': row['counts']['expected_guest_events'] = 2
    assert release.gate(report)


def test_ollama_identity_uses_exact_model_and_digest(config, monkeypatch):
    digest = 'a' * 64
    responses = {'/api/tags': [{'name': 'fixture:3b', 'digest': digest},
                              {'name': 'unrelated', 'digest': 'b' * 64}],
                 '/api/ps': [{'model': 'fixture:3b', 'digest': digest}]}
    monkeypatch.setattr(release, '_local_json', lambda endpoint, route: responses[route])
    observed = release.model_identity(config)
    assert observed == {'kind': 'observed_ollama', 'digest': digest, 'loaded_at_probe': True}
    responses['/api/ps'][0]['digest'] = 'b' * 64
    with pytest.raises(ValueError): release.model_identity(config)
    responses['/api/tags'] = [{'name': 'different', 'digest': digest}]
    with pytest.raises(ValueError): release.model_identity(config)


def test_mlx_manifest_verifies_actual_bytes_not_declaration(config, tmp_path):
    weights = tmp_path / 'weights.safetensors'; weights.write_bytes(b'fixture weights')
    adapter = tmp_path / 'adapter.safetensors'; adapter.write_bytes(b'fixture adapter')
    manifest = tmp_path / 'model.json'
    manifest.write_text(json.dumps({'model': config['model'], 'files': [
        {'path': p.name, 'sha256': release.digest(p)} for p in (weights, adapter)]}))
    config.update(backend='mlx', model_manifest=str(manifest))
    actual = release.model_identity(config)
    assert len(actual['files_sha256']) == 2 and actual['server_loaded_files_attested'] is False
    weights.write_bytes(b'changed weights')
    with pytest.raises(ValueError): release.model_identity(config)


@pytest.mark.parametrize('drift', [False, True])
def test_orchestrator_runs_separate_sequential_workers_and_detects_input_drift(config, monkeypatch, drift):
    called = []
    monkeypatch.setattr(release, 'model_identity', lambda cfg: {'digest': 'a', 'loaded_at_probe': True})
    def subprocess_run(command, **kwargs):
        profile = command[command.index('--worker') + 1]
        called.append(profile)
        destination = Path(config['output']) / profile; destination.mkdir()
        release.write_json(destination / 'worker.json', complete_profile())
        if profile == 'dev' and drift:
            (Path(config['datasets']['dev']) / 'late.csv').write_text('changed')
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(release.subprocess, 'run', subprocess_run)
    report = release.verify(config, execute=True)
    assert called == ['quickstart', 'dev']
    expected_status = 'verification_failed' if drift else 'pipeline_verified'
    assert report['status'] == expected_status
    assert ('input_drift' in report['failures']) is drift
    assert report['model_quality'] == 'not_certified'
    assert release.read_json(Path(config['output']) / 'verification.json')['status'] == expected_status


@pytest.mark.parametrize('transport_failure', [False, True])
def test_worker_goes_through_real_cli_ingestion_and_validator(config, tmp_path, monkeypatch, transport_failure):
    from ingestion.loader import REQUIRED_COLUMNS
    from ingestion import guest_parser
    root = Path(config['datasets']['quickstart'])
    for filename, columns in REQUIRED_COLUMNS.items():
        (root / filename).write_text(','.join(columns) + '\n')
    release.write_json(root / 'building.json', {'time': {'local_timezone': 'Europe/Vienna'},
        'building': {'rooms': [{'id': 'R101'}], 'assets': []}, 'staff': {'resources': []},
        'constraints': {'occupied_comfort_c': [20, 23]}})
    release.write_json(root / 'manifest.json', {'profile': 'quickstart', 'model_id': 'PLATO-40',
        'start_utc': '2026-01-06T12:00:00Z', 'end_utc': '2026-01-06T12:15:00Z', 'interval_seconds': 900})
    (root / 'events.jsonl').write_text('')
    with (root / 'guest_requests.csv').open('w', newline='') as stream:
        row = {'request_id': 'W1', 'timestamp_utc': '2026-01-06T12:00:00Z', 'room_id': 'R101',
               'booking_id': '', 'channel': 'app_chat', 'language': 'en', 'request_type': 'wifi_help',
               'text': 'Please help with wifi.', 'priority': 'low', 'requires_guest_consent': 'required'}
        writer = csv.DictWriter(stream, fieldnames=list(row)); writer.writeheader(); writer.writerow(row)
    def reply(messages, settings):
        assert settings['model'] == config['model']
        assert settings['generation_profile'] == 'extraction'
        if transport_failure:
            raise OSError('injected transport failure')
        return json.dumps({'intents': [{'intent': 'wifi_help', 'requested_time_utc': None,
            'requested_time_text': None, 'evidence_quote': 'Please help with wifi.',
            'details': {}, 'needs_review': False}]})
    monkeypatch.setattr(guest_parser, '_call_llm', reply)
    result = release.worker(config, 'quickstart', tmp_path / 'worker')
    assert result['status'] == 'complete' and result['validation']['valid'] is True
    assert result['counts']['guest_events'] == result['counts']['transport_attempts'] == 1
    assert result['counts']['method_llm'] == int(not transport_failure)
    assert result['counts']['method_fallback'] == result['counts']['transport_errors'] == int(transport_failure)
    assert result['counts']['cache_hits'] == 0
    assert result['run']['status'] == 'replay_complete'
    assert set(result['artifact_hashes_sha256']) == {
        'decisions.jsonl', 'audit.jsonl', 'decisions_rejections.jsonl',
        'model_responses.jsonl', 'guest_events.jsonl'}
    trace = json.loads((tmp_path / 'worker/model_responses.jsonl').read_text())
    assert trace['sequence'] == 1 and trace['seconds'] >= 0
    assert trace['request_id'] == 'W1'
    assert len(trace['record_sha256']) == len(trace['messages_sha256']) == 64
    assert ('raw_response' in trace) is not transport_failure
    assert ('error_type' in trace) is transport_failure
    event = json.loads((tmp_path / 'worker/guest_events.jsonl').read_text())
    assert event['event_type'] == 'guest_request'
    assert event['source']['record_id'] == trace['request_id']
