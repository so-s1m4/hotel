"""Cache recovery through the public parser; synthetic responses and no network."""
from __future__ import annotations

import copy
import importlib
import json
import os
import socket
from unittest.mock import Mock

import pytest

from common.contracts.schemas import GuestIntentV1


@pytest.fixture
def parser(monkeypatch, tmp_path):
    for name in tuple(os.environ):
        if name.startswith(("LLM_", "OPENAI_", "OPENROUTER_", "CEREBRAS_", "GUEST_PARSER_")):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LLM_API_KEY", "synthetic-test-key")
    monkeypatch.setenv("LLM_MODEL", "synthetic-test-model")
    monkeypatch.setenv("LLM_BASE_URL", "https://example.invalid/v1")
    monkeypatch.setenv("GUEST_PARSER_CACHE_DIR", str(tmp_path / "cache"))
    module = importlib.import_module("ingestion.guest_parser")
    network_calls = []

    def block_network(*args, **kwargs):
        network_calls.append(True)
        raise AssertionError("Cache recovery tests must never use the network")

    monkeypatch.setattr(module.urllib_request, "urlopen", block_network)
    monkeypatch.setattr(socket, "create_connection", block_network)
    monkeypatch.setattr(socket.socket, "connect", block_network)
    yield module
    assert not network_calls


@pytest.fixture
def record():
    return {
        "request_id": "synthetic-cache-recovery",
        "timestamp_utc": "2026-01-06T09:00:00Z",
        "room_id": "R101",
        "language": "en",
        "request_type": "extra_towels",
        "text": "Please bring fresh towels.",
    }


@pytest.fixture
def llm(parser, record, monkeypatch):
    response = {"intents": [{
        "intent": "extra_towels",
        "evidence_quote": record["text"],
        "requested_time_utc": None,
        "requested_time_text": None,
        "details": {},
        "needs_review": False,
    }]}
    transport = Mock(return_value=json.dumps(response))
    monkeypatch.setattr(parser, "_call_llm", transport)
    return transport


def assert_valid_extraction(result, meta, record):
    assert meta.method == "llm" and not meta.needs_review
    assert len(result) == 1
    item = GuestIntentV1.model_validate(result[0])
    assert item.intent == record["request_type"]
    assert item.evidence_quote == record["text"]
    assert item.requested_time_utc is None and not item.needs_review


def assert_safe_fallback(result, meta, record):
    assert meta.method == "fallback" and meta.needs_review
    assert len(result) == 1
    item = GuestIntentV1.model_validate(result[0])
    assert item.intent == record["request_type"]
    assert item.requested_time_utc is None and item.requested_time_text is None
    assert item.evidence_quote is None and item.details == {} and item.needs_review


def seed_cache(parser, record, cache_dir):
    result, meta = parser.parse_request_full(record, {})
    assert_valid_extraction(result, meta, record)
    files = list(cache_dir.glob("*.json"))
    assert len(files) == 1
    return files[0], result, meta


def corrupt_cache(path, corruption):
    if corruption == "invalid_intent":
        value = json.loads(path.read_text(encoding="utf-8"))
        value["intents"][0]["intent"] = "release_room"
    else:
        value = {"array": [], "null": None, "number": 42, "string": "not a cache"}[corruption]
    text = json.dumps(value)
    path.write_text(text, encoding="utf-8")
    return text


@pytest.mark.parametrize("corruption", ["array", "null", "number", "string", "invalid_intent"])
def test_corrupt_cache_recovers_through_transport_and_then_reuses_cache(parser, record, llm, tmp_path, corruption):
    cache_file, _, _ = seed_cache(parser, record, tmp_path / "cache")
    corrupt_cache(cache_file, corruption)
    original = copy.deepcopy(record)

    recovered, recovered_meta = parser.parse_request_full(record, {})

    assert llm.call_count == 2, "An unusable cache must not suppress a healthy transport retry"
    assert_valid_extraction(recovered, recovered_meta, record)
    assert json.loads(cache_file.read_text(encoding="utf-8"))["intents"] == recovered
    cached, cached_meta = parser.parse_request_full(record, {})
    assert llm.call_count == 2
    assert (cached, cached_meta) == (recovered, recovered_meta)
    assert record == original


@pytest.mark.parametrize("override", ["environment", "context"])
def test_explicit_offline_bypasses_even_corrupt_cache(parser, record, llm, tmp_path, monkeypatch, override):
    cache_file, _, _ = seed_cache(parser, record, tmp_path / "cache")
    bad_cache = corrupt_cache(cache_file, "array")
    cache_read = Mock(side_effect=AssertionError("Offline mode must not read cache"))
    monkeypatch.setattr(parser, "_read_cache", cache_read)
    context = {}
    if override == "environment":
        monkeypatch.setenv("GUEST_PARSER_OFFLINE", "1")
    else:
        context["use_llm"] = False

    result, meta = parser.parse_request_full(record, context)

    cache_read.assert_not_called()
    assert llm.call_count == 1
    assert meta.method == "source_label" and meta.needs_review
    assert result[0]["intent"] == record["request_type"]
    assert result[0]["requested_time_utc"] is None and result[0]["evidence_quote"] is None
    assert result[0]["details"] == {} and result[0]["needs_review"]
    assert cache_file.read_text(encoding="utf-8") == bad_cache


@pytest.mark.parametrize("cache_state", ["cold", "corrupt"])
def test_endpoint_failure_is_not_cached_and_later_recovery_is_attempted(parser, record, llm, tmp_path, cache_state):
    cache_dir = tmp_path / "cache"
    cache_file = None
    if cache_state == "corrupt":
        cache_file, _, _ = seed_cache(parser, record, cache_dir)
        bad_cache = corrupt_cache(cache_file, "array")
    before_failure = llm.call_count
    llm.side_effect = TimeoutError("synthetic backend timeout")

    result, meta = parser.parse_request_full(record, {})

    assert llm.call_count == before_failure + 1
    assert_safe_fallback(result, meta, record)
    if cache_file is None:
        assert not list(cache_dir.glob("*.json"))
    elif cache_file.exists():
        # Either leaving or removing the unusable entry is acceptable; caching
        # the fallback in its place would prevent a future healthy retry.
        assert cache_file.read_text(encoding="utf-8") == bad_cache

    llm.side_effect = None
    recovered, recovered_meta = parser.parse_request_full(record, {})
    assert llm.call_count == before_failure + 2
    assert_valid_extraction(recovered, recovered_meta, record)
    cached, cached_meta = parser.parse_request_full(record, {})
    assert llm.call_count == before_failure + 2
    assert (cached, cached_meta) == (recovered, recovered_meta)


def test_valid_cache_remains_usable_without_another_transport_attempt(parser, record, llm, tmp_path):
    _, first, first_meta = seed_cache(parser, record, tmp_path / "cache")
    llm.side_effect = AssertionError("A valid cache should avoid transport")

    cached, cached_meta = parser.parse_request_full(copy.deepcopy(record), {})

    assert llm.call_count == 1
    assert_valid_extraction(cached, cached_meta, record)
    assert (cached, cached_meta) == (first, first_meta)
