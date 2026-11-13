"""Bounded benchmark behavior with injected parser transport and no network."""
from __future__ import annotations

import copy
from dataclasses import replace
import hashlib
import importlib
import json
import logging
import os
from pathlib import Path
import socket
from threading import Event, Lock
from unittest.mock import Mock
from urllib import request as urllib_request

import pytest


REVISION = "sha256:" + "a" * 64


@pytest.fixture(autouse=True)
def isolated_environment_and_network(monkeypatch):
    for name in tuple(os.environ):
        if name.startswith(("LLM_", "OPENAI_", "OPENROUTER_", "CEREBRAS_", "GUEST_PARSER_")):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LLM_API_KEY", "synthetic-benchmark-test-key")
    attempted = []

    def block_network(*args, **kwargs):
        attempted.append(True)
        raise AssertionError("Benchmark tests must never use HTTP or sockets")

    monkeypatch.setattr(urllib_request, "urlopen", block_network)
    monkeypatch.setattr(socket, "create_connection", block_network)
    monkeypatch.setattr(socket.socket, "connect", block_network)
    monkeypatch.setattr(socket.socket, "connect_ex", block_network)
    yield
    assert not attempted


@pytest.fixture
def benchmark():
    return importlib.import_module("scripts.benchmark_guest_parser")


@pytest.fixture
def parser():
    return importlib.import_module("ingestion.guest_parser")


@pytest.fixture
def cases():
    return [{
        "case_id": f"case-{index}",
        "record": {
            "request_id": f"synthetic-request-{index}",
            "timestamp_utc": "2026-01-06T09:00:00Z",
            "room_id": "R101", "language": "en", "request_type": "extra_towels",
            "text": f"Please bring fresh towels, request {index}.",
        },
        "context": {},
        "gold": {"intents": ["extra_towels"], "requested_time_utc": None,
                 "needs_review": False, "rationale": "Synthetic test expectation."},
    } for index in range(5)]


def write_cases(path, cases):
    path.write_text("".join(json.dumps(case) + "\n" for case in cases), encoding="utf-8")
    return path


def config_for(benchmark, tmp_path, input_path, **changes):
    values = dict(input_path=input_path, limit=3, candidate="candidate-a", revision=REVISION,
                  run_id="run-a", cache_root=tmp_path / "cache", out=tmp_path / "report.json",
                  base_url="https://example.invalid/v1", model="synthetic-model")
    values.update(changes)
    return benchmark.BenchmarkConfig(**values)


def guest_record(messages):
    return json.loads(next(message["content"] for message in messages
                           if message["role"] == "user"))["guest_record"]


def reply(record, details=None):
    return json.dumps({"intents": [{
        "intent": "extra_towels", "requested_time_utc": None, "requested_time_text": None,
        "evidence_quote": record["text"], "details": details or {}, "needs_review": False,
    }]})


def test_cli_defaults_to_plan_without_parser_cache_or_transport(benchmark, parser, cases, tmp_path, monkeypatch):
    input_path = write_cases(tmp_path / "cases.jsonl", cases)
    original = input_path.read_bytes()
    guards = {name: Mock(side_effect=AssertionError("Plan must not invoke " + name))
              for name in ("parse_request_full", "_call_llm", "_read_cache", "_write_cache")}
    for name, guard in guards.items():
        monkeypatch.setattr(parser, name, guard)
    out = tmp_path / "plan.json"

    result = benchmark.main([
        "--input", str(input_path), "--limit", "2", "--candidate", "candidate-a",
        "--revision", REVISION, "--run-id", "plan-a", "--cache-root", str(tmp_path / "cache"),
        "--out", str(out), "--base-url", "https://example.invalid/v1", "--model", "synthetic-model",
        "--workers", "1",
    ])

    assert result == 0
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["schema_version"] == "1.1" and report["status"] == "planned"
    assert report["mode"] == "plan" and report["results"] == []
    assert report["manifest"]["selected_count"] == 2
    assert report["summary"]["selected_count"] == 2
    assert report["summary"]["completed_count"] == 0
    for name in ("transport_calls", "cache_lookups", "cache_hits", "live_llm", "cache_llm",
                 "source_label", "fallback", "error", "total_seconds", "raw_predictions_count"):
        assert report["summary"][name] == 0
    assert report["summary"]["raw_gold_pass_count"] is None
    assert report["summary"]["host_gold_pass_count"] is None
    for guard in guards.values():
        guard.assert_not_called()
    assert input_path.read_bytes() == original


@pytest.mark.parametrize("workers", [1, 6])
def test_limit_precedes_calls_and_parallel_completion_keeps_case_attribution(benchmark, parser, cases, tmp_path, monkeypatch, workers):
    input_path = write_cases(tmp_path / "cases.jsonl", cases)
    input_before = input_path.read_bytes()
    source_before = Path(parser.__file__).read_bytes()
    second_completed, lock = Event(), Lock()
    completion_order = []
    raw_by_request = {case["record"]["request_id"]: reply(case["record"]) for case in cases}

    def transport(messages, config):
        record = guest_record(messages)
        request_id = record["request_id"]
        if workers == 6 and request_id == cases[0]["record"]["request_id"]:
            assert second_completed.wait(5), "Multiple workers must permit another case to finish"
        with lock:
            completion_order.append(request_id)
        if request_id == cases[1]["record"]["request_id"]:
            second_completed.set()
        return raw_by_request[request_id]

    transport_mock = Mock(side_effect=transport)
    parse_mock = Mock(side_effect=parser.parse_request_full)
    monkeypatch.setattr(parser, "_call_llm", transport_mock)
    monkeypatch.setattr(parser, "parse_request_full", parse_mock)
    original_read_cache = parser._read_cache
    config = config_for(benchmark, tmp_path, input_path, workers=workers, execute=True, limit=3)

    report = benchmark.run_benchmark(config, parser_module=parser)

    assert report["schema_version"] == "1.1" and report["status"] == "complete"
    selected = cases[:3]
    selected_request_ids = [case["record"]["request_id"] for case in selected]
    assert parse_mock.call_count == transport_mock.call_count == 3
    assert sorted(completion_order) == sorted(selected_request_ids)
    if workers == 1:
        assert completion_order == selected_request_ids
    else:
        assert completion_order != selected_request_ids
    assert [result["case_id"] for result in report["results"]] == [case["case_id"] for case in selected]
    for case, result in zip(selected, report["results"]):
        assert result["path"] == "live_llm"
        assert result["raw_response"] == raw_by_request[case["record"]["request_id"]]
        assert result["normalized"]["intents"][0]["evidence_quote"] == case["record"]["text"]
        assert result["counts"]["transport_calls"] == 1
        assert result["checks"]["raw"]["passed"] is True
    assert report["summary"]["selected_count"] == report["summary"]["completed_count"] == 3
    assert report["summary"]["transport_calls"] == report["summary"]["live_llm"] == 3
    assert all(type(value) in (int, float) for value in report["summary"].values())
    assert parser._call_llm is transport_mock and parser._read_cache is original_read_cache
    assert parser.parse_request_full is parse_mock
    assert input_path.read_bytes() == input_before and Path(parser.__file__).read_bytes() == source_before


@pytest.mark.parametrize("fail_interrupted_save", [False, True], ids=["interrupt", "interrupt-save-failure"])
def test_interrupt_retains_unknown_measurements_and_restores_runner(benchmark, parser, cases, tmp_path, monkeypatch, fail_interrupted_save):
    input_path = write_cases(tmp_path / "cases.jsonl", cases[:1])
    input_before = input_path.read_bytes()
    config = config_for(benchmark, tmp_path, input_path, limit=1, workers=1, execute=True)
    config_before = copy.deepcopy(config)
    environment_before = dict(os.environ)
    original_parse = parser.parse_request_full
    sentinel = KeyboardInterrupt("synthetic interruption details must remain private")
    initial_reports, failed_saves = [], []
    original_dump = benchmark.json.dump

    def save_report(value, *args, **kwargs):
        if fail_interrupted_save and value.get("status") == "interrupted":
            failed_saves.append(True)
            raise OSError("synthetic report save failure")
        return original_dump(value, *args, **kwargs)

    def transport(messages, parser_config):
        initial_reports.append(json.loads(Path(config.out).read_text(encoding="utf-8")))
        raise sentinel

    transport_mock = Mock(side_effect=transport)
    cache_mock = Mock(side_effect=parser._read_cache)
    monkeypatch.setattr(benchmark.json, "dump", save_report)
    monkeypatch.setattr(parser, "_call_llm", transport_mock)
    monkeypatch.setattr(parser, "_read_cache", cache_mock)

    with pytest.raises(KeyboardInterrupt) as caught:
        benchmark.run_benchmark(config, parser_module=parser)

    assert caught.value is sentinel
    assert transport_mock.call_count == cache_mock.call_count == len(initial_reports) == 1
    assert parser._call_llm is transport_mock and parser._read_cache is cache_mock
    assert parser.parse_request_full is original_parse
    assert dict(os.environ) == environment_before and config == config_before
    assert input_path.read_bytes() == input_before
    retained = json.loads(Path(config.out).read_text(encoding="utf-8"))

    # A subsequent public run proves that the process-wide run lock was released.
    plan = benchmark.run_benchmark(replace(config, execute=False, run_id="after-interrupt",
                                           out=tmp_path / "after-interrupt.json"), parser_module=parser)
    assert plan["mode"] == "plan" and plan["summary"]["transport_calls"] == 0
    assert transport_mock.call_count == cache_mock.call_count == 1

    for report, status in ((initial_reports[0], "running"),
                           (retained, "running" if fail_interrupted_save else "interrupted")):
        assert report.get("status") == status, json.dumps(report["summary"], sort_keys=True)
        assert report["schema_version"] == "1.1" and report["results"] == []
        assert report["summary"]["selected_count"] == 1
        for name, value in report["summary"].items():
            if name != "selected_count":
                assert value is None, (name, value)
        assert str(sentinel) not in json.dumps(report)
    if fail_interrupted_save:
        assert failed_saves == [True]
    else:
        assert retained["error_type"] == "KeyboardInterrupt"


def test_cache_and_fallback_accounting_retains_raw_and_validated_outputs(benchmark, parser, cases, tmp_path, monkeypatch):
    selected = copy.deepcopy(cases[:3])
    selected[1]["context"]["use_llm"] = False
    for case in selected:
        case["gold"]["needs_review"] = True
    input_path = write_cases(tmp_path / "cases.jsonl", selected)
    raw = reply(selected[0]["record"], details={"room_ready": True})

    def transport(messages, config):
        request_id = guest_record(messages)["request_id"]
        if request_id == selected[2]["record"]["request_id"]:
            raise TimeoutError("synthetic unavailable endpoint")
        assert request_id == selected[0]["record"]["request_id"]
        return raw

    transport_mock = Mock(side_effect=transport)
    monkeypatch.setattr(parser, "_call_llm", transport_mock)
    config = config_for(benchmark, tmp_path, input_path, execute=True)
    cold = benchmark.run_benchmark(config, parser_module=parser)
    warm = benchmark.run_benchmark(replace(config, warm=True, out=tmp_path / "warm.json"), parser_module=parser)

    assert [result["path"] for result in cold["results"]] == ["live_llm", "source_label", "fallback"]
    assert [result["path"] for result in warm["results"]] == ["cache_llm", "source_label", "fallback"]
    assert cold["results"][0]["raw_response"] == raw
    assert cold["results"][0]["raw_response_sha256"] == hashlib.sha256(raw.encode("utf-8")).hexdigest()
    assert cold["results"][0]["normalized"]["intents"][0]["details"] == {}
    assert warm["results"][0]["raw_response"] is None
    assert cold["results"][0]["normalized"]["extraction"]["method"] == "llm"
    assert warm["results"][0]["normalized"]["extraction"]["method"] == "llm"
    assert warm["results"][0]["counts"]["transport_calls"] == 0
    assert warm["results"][0]["counts"]["cache_hits"] == 1
    assert "TimeoutError" in cold["results"][2]["error_types"]
    for report, transports, live, cached in ((cold, 2, 1, 0), (warm, 1, 0, 1)):
        summary = report["summary"]
        assert summary["completed_count"] == 3
        assert summary["transport_calls"] == transports
        assert summary["cache_lookups"] == 2 and summary["cache_hits"] == cached
        assert summary["live_llm"] == live and summary["cache_llm"] == cached
        assert summary["source_label"] == summary["fallback"] == 1
    assert transport_mock.call_count == 3


@pytest.mark.parametrize("changes", [
    {"requested_time_text": "13:00"},
    {"details": {"approximate_time": "yes"}},
    {"details": {"assistance_requested": "The room is ready."}},
], ids=["fabricated-time-quote", "invalid-detail-value", "ungrounded-assistance"])
def test_raw_scoring_rejects_ungrounded_or_invalid_semantic_fields(benchmark, parser, cases, tmp_path, monkeypatch, changes):
    input_path = write_cases(tmp_path / "cases.jsonl", cases[:1])
    response = json.loads(reply(cases[0]["record"]))
    response["intents"][0].update(changes)
    transport = Mock(return_value=json.dumps(response))
    monkeypatch.setattr(parser, "_call_llm", transport)
    config = config_for(benchmark, tmp_path, input_path, limit=1, execute=True)

    report = benchmark.run_benchmark(config, parser_module=parser)

    raw_checks = report["results"][0]["checks"]["raw"]
    assert transport.call_count == 1
    assert raw_checks["schema_valid"] is True
    assert raw_checks["intents_match"] is True
    assert raw_checks["requested_time_utc_match"] is True
    assert raw_checks["passed"] is False


@pytest.mark.parametrize("response_kind", ["exception", "raw_key_echo"])
def test_credentials_and_error_header_values_never_enter_artifacts(benchmark, parser, cases, tmp_path, monkeypatch, capsys, caplog, response_kind):
    secrets = ("synthetic-private-llm-value-724", "synthetic-private-provider-value-917")
    monkeypatch.setenv("LLM_API_KEY", secrets[0])
    monkeypatch.setenv("CEREBRAS_API_KEY", secrets[1])
    environment_before = {name: os.environ.get(name) for name in
                          ("LLM_API_KEY", "CEREBRAS_API_KEY", "LLM_MODEL", "LLM_BASE_URL", "GUEST_PARSER_CACHE_DIR")}
    caplog.set_level(logging.DEBUG)
    input_path = write_cases(tmp_path / "cases.jsonl", cases[:1])

    def transport(messages, config):
        if response_kind == "exception":
            raise RuntimeError(f"Authorization: Bearer {secrets[0]}; X-API-Key: {secrets[1]}; env LLM_API_KEY={secrets[0]}")
        return reply(guest_record(messages), details={"authorization": "Bearer " + secrets[0], "provider_key": secrets[1]})

    monkeypatch.setattr(parser, "_call_llm", transport)
    config = config_for(benchmark, tmp_path, input_path, limit=1, execute=True)
    report = benchmark.run_benchmark(config, parser_module=parser)

    captured = capsys.readouterr()
    exposed = json.dumps(report) + captured.out + captured.err + caplog.text
    exposed += "".join(path.read_text(encoding="utf-8") for path in tmp_path.rglob("*") if path.is_file())
    for secret in secrets:
        assert secret not in exposed
    if response_kind == "exception":
        assert report["results"][0]["error_types"] == ["RuntimeError"]
        assert report["results"][0]["raw_response"] is None
    assert {name: os.environ.get(name) for name in environment_before} == environment_before


def test_revision_identity_and_cache_run_isolation_are_enforced(benchmark, parser, cases, tmp_path, monkeypatch):
    input_path = write_cases(tmp_path / "cases.jsonl", cases[:1])
    transport = Mock(side_effect=lambda messages, config: reply(guest_record(messages)))
    parse = Mock(side_effect=parser.parse_request_full)
    monkeypatch.setattr(parser, "_call_llm", transport)
    monkeypatch.setattr(parser, "parse_request_full", parse)
    config = config_for(benchmark, tmp_path, input_path, limit=1, execute=True)
    benchmark.run_benchmark(config, parser_module=parser)
    assert transport.call_count == parse.call_count == 1

    with pytest.raises((ValueError, OSError)):
        benchmark.run_benchmark(replace(config, out=tmp_path / "reused-cold.json"), parser_module=parser)
    with pytest.raises((ValueError, OSError)):
        benchmark.run_benchmark(replace(config, warm=True, revision="sha256:" + "b" * 64,
                                        out=tmp_path / "changed-revision.json"), parser_module=parser)
    assert transport.call_count == parse.call_count == 1

    another_run = benchmark.run_benchmark(replace(config, run_id="run-b", out=tmp_path / "another-run.json"), parser_module=parser)
    another_candidate = benchmark.run_benchmark(replace(config, candidate="candidate-b", out=tmp_path / "another-candidate.json"), parser_module=parser)
    assert transport.call_count == parse.call_count == 3
    assert another_run["results"][0]["path"] == another_candidate["results"][0]["path"] == "live_llm"
    for candidate, run_id in (("candidate-a", "run-a"), ("candidate-a", "run-b"), ("candidate-b", "run-a")):
        assert (tmp_path / "cache" / candidate / run_id).is_dir()


def test_mutable_revision_is_rejected_before_parser_calls(benchmark, parser, cases, tmp_path, monkeypatch):
    input_path = write_cases(tmp_path / "cases.jsonl", cases[:1])
    parse = Mock(side_effect=AssertionError("Invalid revision must fail before parsing"))
    transport = Mock(side_effect=AssertionError("Invalid revision must fail before transport"))
    monkeypatch.setattr(parser, "parse_request_full", parse)
    monkeypatch.setattr(parser, "_call_llm", transport)
    with pytest.raises(ValueError):
        config = config_for(benchmark, tmp_path, input_path, limit=1, revision="latest", execute=True)
        benchmark.run_benchmark(config, parser_module=parser)
    parse.assert_not_called()
    transport.assert_not_called()


def test_invalid_input_beyond_limit_is_rejected_before_any_parser_calls(benchmark, parser, cases, tmp_path, monkeypatch):
    input_path = write_cases(tmp_path / "cases.jsonl", cases[:1])
    input_path.write_text(input_path.read_text(encoding="utf-8") + "{\n", encoding="utf-8")
    original = input_path.read_bytes()
    parse = Mock(side_effect=AssertionError("The entire input must validate before parsing"))
    transport = Mock(side_effect=AssertionError("The entire input must validate before transport"))
    monkeypatch.setattr(parser, "parse_request_full", parse)
    monkeypatch.setattr(parser, "_call_llm", transport)
    config = config_for(benchmark, tmp_path, input_path, limit=1, execute=True)

    with pytest.raises(ValueError):
        benchmark.run_benchmark(config, parser_module=parser)

    parse.assert_not_called()
    transport.assert_not_called()
    assert input_path.read_bytes() == original


@pytest.mark.parametrize("output_fault", ["input_hardlink", "unusable_parent"])
def test_unusable_or_input_alias_output_is_rejected_before_calls(benchmark, parser, cases, tmp_path, monkeypatch, output_fault):
    input_path = write_cases(tmp_path / "cases.jsonl", cases[:1])
    original = input_path.read_bytes()
    if output_fault == "input_hardlink":
        out = tmp_path / "input-alias.json"
        os.link(input_path, out)
        protected = out
    else:
        protected = tmp_path / "not-a-directory"
        protected.write_bytes(b"keep this existing file")
        out = protected / "report.json"
    protected_before = protected.read_bytes()
    parse = Mock(side_effect=AssertionError("Output preflight must precede parser calls"))
    transport = Mock(side_effect=AssertionError("Output preflight must precede transport calls"))
    monkeypatch.setattr(parser, "parse_request_full", parse)
    monkeypatch.setattr(parser, "_call_llm", transport)
    config = config_for(benchmark, tmp_path, input_path, limit=1, execute=True, out=out)

    with pytest.raises((ValueError, OSError)):
        benchmark.run_benchmark(config, parser_module=parser)

    parse.assert_not_called()
    transport.assert_not_called()
    assert input_path.read_bytes() == original
    assert protected.read_bytes() == protected_before


@pytest.mark.parametrize('changed', [
    {'response_format_mode': 'json_schema'},
    {'max_tokens': 256},
    {'runtime_metadata': {'served_model_digest': 'sha256:' + 'b' * 64, 'num_parallel': 2}},
], ids=['response-mode', 'output-token-cap', 'declared-runtime'])
def test_generation_settings_cannot_reuse_a_warm_run_identity(benchmark, parser, cases, tmp_path, monkeypatch, changed):
    path = write_cases(tmp_path/'cases.jsonl', cases[:1])
    transport = Mock(side_effect=lambda messages, settings: reply(guest_record(messages)))
    monkeypatch.setattr(parser, '_call_llm', transport)
    config = config_for(benchmark, tmp_path, path, limit=1, execute=True)
    cold = benchmark.run_benchmark(config, parser_module=parser)
    assert cold['summary']['transport_calls'] == 1
    with pytest.raises(ValueError, match='Warm cache identity'):
        benchmark.run_benchmark(replace(config, warm=True, out=tmp_path/'changed.json', **changed), parser_module=parser)
    assert not (tmp_path/'changed.json').exists()
    assert transport.call_count == 1
    warm = benchmark.run_benchmark(replace(config, warm=True, out=tmp_path/'same.json'), parser_module=parser)
    assert warm['summary']['cache_hits'] == 1
    assert transport.call_count == 1


@pytest.mark.parametrize('mode', ['json_object', 'json_schema'])
def test_exact_generation_format_and_declared_runtime_are_pinned_and_forwarded(benchmark, parser, cases, tmp_path, monkeypatch, mode):
    from ingestion.guest_generation import response_format, generation_schema_sha256
    path = write_cases(tmp_path/'cases.jsonl', cases[:1])
    runtime = {'served_model_digest': 'sha256:' + 'c' * 64, 'runtime': 'synthetic-server',
               'runtime_version': 'test', 'context_tokens': 4096, 'num_parallel': 6,
               'max_loaded_models': 1, 'gpu_layers': 0, 'load_state': 'warm_declared',
               'memory_sampling': 'not sampled', 'total_ram_gib': 16}
    seen = []
    monkeypatch.setenv('LLM_RESPONSE_FORMAT', 'json_schema' if mode == 'json_object' else 'json_object')
    monkeypatch.setenv('LLM_MAX_TOKENS', '99')
    before = dict(os.environ)

    def transport(messages, settings):
        seen.append(dict(settings))
        return reply(guest_record(messages))

    monkeypatch.setattr(parser, '_call_llm', transport)
    config = config_for(benchmark, tmp_path, path, limit=1, execute=True,
                        response_format_mode=mode, max_tokens=384, runtime_metadata=runtime)
    report = benchmark.run_benchmark(config, parser_module=parser)
    manifest = report['manifest']
    assert manifest['response_format'] == response_format(mode)
    expected = json.dumps(response_format(mode), ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
    assert manifest['response_format_sha256'] == hashlib.sha256(expected.encode()).hexdigest()
    assert manifest['generation_schema_sha256'] == generation_schema_sha256()
    assert manifest['max_tokens'] == 384
    assert manifest['runtime_metadata'] == {'provenance': 'operator_supplied_not_measured', 'values': runtime}
    assert manifest['runtime_metadata_sha256'] == hashlib.sha256(benchmark._canonical(runtime).encode()).hexdigest()
    assert seen[0]['response_format_mode'] == mode
    assert seen[0]['max_tokens'] == 384
    assert dict(os.environ) == before
    assert config.runtime_metadata == runtime


def test_default_generation_mode_clears_inherited_cap_without_claiming_server_measurement(benchmark, parser, cases, tmp_path, monkeypatch):
    path = write_cases(tmp_path/'cases.jsonl', cases[:1])
    monkeypatch.setenv('LLM_MAX_TOKENS', '2048')
    monkeypatch.setenv('LLM_RESPONSE_FORMAT', 'json_schema')
    settings_seen = []

    def transport(messages, settings):
        settings_seen.append(dict(settings))
        return reply(guest_record(messages))

    monkeypatch.setattr(parser, '_call_llm', transport)
    report = benchmark.run_benchmark(config_for(benchmark, tmp_path, path, limit=1, execute=True), parser_module=parser)
    assert settings_seen[0]['response_format_mode'] == 'json_object'
    assert settings_seen[0]['max_tokens'] is None
    assert report['manifest']['max_tokens'] is None
    assert report['manifest']['runtime_metadata'] == {'provenance': 'operator_supplied_not_measured', 'values': {}}
    assert os.environ['LLM_MAX_TOKENS'] == '2048'


@pytest.mark.parametrize('changes', [
    {'timeout_seconds': True}, {'timeout_seconds': '15'},
    {'response_format_mode': 'text'}, {'max_tokens': True}, {'max_tokens': 0}, {'max_tokens': 16385},
    {'runtime_metadata': {'served_model_digest': 'latest'}},
    {'runtime_metadata': {'api_key': 'never-record-credentials'}},
    {'runtime_metadata': {'num_parallel': True}},
    {'runtime_metadata': {'server_slots': 1, 'num_parallel': 6}},
    {'runtime_metadata': {'total_ram_gib': float('nan')}},
])
def test_invalid_generation_or_runtime_settings_fail_before_calls(benchmark, parser, cases, tmp_path, monkeypatch, changes):
    path = write_cases(tmp_path/'cases.jsonl', cases[:1])
    parse = Mock(side_effect=AssertionError('invalid settings must not parse'))
    monkeypatch.setattr(parser, 'parse_request_full', parse)
    with pytest.raises(ValueError):
        benchmark.run_benchmark(config_for(benchmark, tmp_path, path, execute=True, **changes), parser_module=parser)
    parse.assert_not_called()
    assert not (tmp_path/'report.json').exists()
    assert not (tmp_path/'cache').exists()


def test_cli_reads_operator_metadata_and_generation_options_without_calls(benchmark, cases, tmp_path):
    path = write_cases(tmp_path/'cases.jsonl', cases[:1])
    runtime = tmp_path/'runtime.json'
    runtime.write_text(json.dumps({'served_model_digest': 'd' * 64, 'num_parallel': 1}))
    report = tmp_path/'plan.json'
    assert benchmark.main(['--input', str(path), '--limit', '1', '--candidate', 'local',
        '--revision', REVISION, '--run-id', 'plan', '--base-url', 'http://127.0.0.1:11434/v1',
        '--model', 'synthetic-model', '--cache-root', str(tmp_path/'cache'), '--out', str(report),
        '--workers', '1', '--response-format-mode', 'json_schema', '--max-tokens', '384',
        '--runtime-metadata', str(runtime)]) == 0
    manifest = json.loads(report.read_text())['manifest']
    assert manifest['response_format_mode'] == 'json_schema'
    assert manifest['max_tokens'] == 384
    assert manifest['runtime_metadata']['values']['served_model_digest'] == 'd' * 64
    assert not (tmp_path/'cache').exists()


def test_full_generation_schema_drift_invalidates_even_json_object_warm_identity(benchmark, parser, cases, tmp_path, monkeypatch):
    import ingestion.guest_generation as generation
    path = write_cases(tmp_path/'cases.jsonl', cases[:1])
    transport = Mock(side_effect=lambda messages, settings: reply(guest_record(messages)))
    monkeypatch.setattr(parser, '_call_llm', transport)
    config = config_for(benchmark, tmp_path, path, limit=1, execute=True)
    cold = benchmark.run_benchmark(config, parser_module=parser)
    # The JSON-object request itself stays identical; the envelope's nested
    # generation contract must still isolate its old caches from a new run.
    previous_hash = cold['manifest']['generation_schema_sha256']
    monkeypatch.setattr(generation, 'generation_schema_sha256', lambda: '0' * 64 if previous_hash != '0' * 64 else '1' * 64)
    with pytest.raises(ValueError, match='Warm cache identity'):
        benchmark.run_benchmark(replace(config, warm=True, out=tmp_path/'schema-drift.json'), parser_module=parser)
    assert transport.call_count == 1
    assert not (tmp_path/'schema-drift.json').exists()
