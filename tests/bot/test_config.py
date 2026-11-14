"""Startup isolation tests use synthetic secrets and never open network sockets."""
from __future__ import annotations

import json
import os
from pathlib import Path
import pickle
import socket
from threading import Thread

import pytest

from hotel_bot import CerebrasConfig, CerebrasConfigurationError, load_cerebras_config
from hotel_bot import config as config_module


KEY = "csk-synthetic-test-key-for-hotel-only-1234"
OTHER_KEY = "csk-another-synthetic-test-key-123456789"
PREFIXES = ("LLM_", "OPENAI_", "OPENROUTER_", "CEREBRAS_", "GUEST_PARSER_")


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch, tmp_path):
    for name in tuple(os.environ):
        if name.startswith(PREFIXES) or name == "SSL_CERT_FILE":
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    def no_network(*args, **kwargs):
        raise AssertionError("Config tests must not contact a provider")

    monkeypatch.setattr(socket, "create_connection", no_network)
    monkeypatch.setattr(socket.socket, "connect", no_network)


def test_environment_key_precedes_file_without_reading_the_file(tmp_path):
    conf = load_cerebras_config(environ={"CEREBRAS_API_KEY": KEY}, key_file=tmp_path / "missing")
    with conf.environment():
        assert os.environ["LLM_API_KEY"] == KEY
        assert os.environ["CEREBRAS_API_KEY"] == KEY
    assert "LLM_API_KEY" not in os.environ


def test_default_private_key_path_and_custom_key_path(tmp_path):
    default = tmp_path / ".config" / "hotel" / "cerebras.key"
    default.parent.mkdir(parents=True)
    default.write_text(KEY + "\n")
    with load_cerebras_config(environ={}).environment():
        assert os.environ["LLM_API_KEY"] == KEY
    custom = tmp_path / "custom.key"
    custom.write_text(OTHER_KEY)
    with load_cerebras_config(environ={}, key_file=custom).environment():
        assert os.environ["LLM_API_KEY"] == OTHER_KEY


@pytest.mark.parametrize("key", [None, "", "  "])
def test_absent_or_blank_environment_key_uses_private_file(tmp_path, key):
    path = tmp_path / "private.key"
    path.write_text(KEY)
    with load_cerebras_config(environ={"CEREBRAS_API_KEY": key}, key_file=path).environment():
        assert os.environ["LLM_API_KEY"] == KEY


@pytest.mark.parametrize("key", [
    "invalid-secret-that-must-not-leak", "sk-other-provider-secret-123456789",
    "csk-short", "csk-a secret with spaces-123456789", "csk-abc\nsecond-line-12345678",
    "csk-" + "x" * 257, 123, object(),
])
def test_malformed_environment_key_never_falls_back_to_file_or_echoes_it(tmp_path, key, caplog):
    path = tmp_path / "valid.key"
    path.write_text(KEY)
    with pytest.raises(CerebrasConfigurationError) as exc:
        load_cerebras_config(environ={"CEREBRAS_API_KEY": key}, key_file=path)
    assert "Cerebras" in str(exc.value)
    assert "invalid-secret" not in str(exc.value) + caplog.text
    assert KEY not in str(exc.value) + caplog.text


@pytest.mark.parametrize("content", [b"", b"invalid-file-secret", b"\xff", b"x" * 513])
def test_malformed_or_oversized_key_files_fail_without_exposing_content(tmp_path, content, caplog):
    path = tmp_path / "private.key"
    path.write_bytes(content)
    with pytest.raises(CerebrasConfigurationError) as exc:
        load_cerebras_config(environ={}, key_file=path)
    assert "invalid-file-secret" not in str(exc.value) + caplog.text
    assert "x" * 32 not in str(exc.value) + caplog.text


def test_missing_key_is_a_friendly_generic_startup_error(tmp_path):
    with pytest.raises(CerebrasConfigurationError) as exc:
        load_cerebras_config(environ={}, key_file=tmp_path / "private-account-name.key")
    assert "CEREBRAS_API_KEY" in str(exc.value)
    assert "private-account-name" not in str(exc.value)
    assert "LLM_API_KEY" not in os.environ


def test_public_settings_and_representations_do_not_serialize_credentials():
    conf = CerebrasConfig(KEY)
    public = conf.public_settings()
    assert public["model"] == "gpt-oss-120b"
    assert public["reasoning_effort"] == "low"
    assert public["max_completion_tokens"] == 4096
    assert KEY not in repr(conf) + str(conf) + json.dumps(public)
    assert "api_key" not in public
    public["model"] = "modified"
    assert conf.public_settings()["model"] == "gpt-oss-120b"
    with pytest.raises(TypeError):
        vars(conf)
    with pytest.raises(TypeError):
        json.dumps(conf)
    with pytest.raises(TypeError):
        pickle.dumps(conf)


def test_startup_pins_exact_profile_despite_competing_aliases_and_dotenv(monkeypatch, tmp_path):
    from ingestion.events import _load_dotenv
    from ingestion.guest_parser import _config

    monkeypatch.setenv("LLM_API_KEY", "unrelated-generic-key")
    monkeypatch.setenv("LLM_MODEL", "old-local-model")
    monkeypatch.setenv("LLM_BASE_URL", "https://unrelated.invalid/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "unrelated-openai-key")
    monkeypatch.setenv("GUEST_PARSER_OFFLINE", "1")
    monkeypatch.setenv("GUEST_PARSER_WORKERS", "16")
    dotenv = tmp_path / "old.env"
    dotenv.write_text("\n".join([
        "LLM_API_KEY=dotenv-key", "LLM_MODEL=dotenv-model", "LLM_BASE_URL=https://dotenv.invalid/v1",
        "OPENROUTER_API_KEY=dotenv-router-key", "OPENROUTER_MODEL=dotenv-router-model",
        "OPENAI_API_KEY=dotenv-openai-key", "OPENAI_BASE_URL=https://dotenv.invalid/v1",
        "LLM_RESPONSE_FORMAT=json_object", "LLM_GENERATION_PROFILE=contract",
        "LLM_REASONING_EFFORT=high", "LLM_MAX_TOKENS=512", "LLM_TIMEOUT_SECONDS=1",
        "GUEST_PARSER_OFFLINE=1", "GUEST_PARSER_WORKERS=16", "GUEST_PARSER_CACHE_DIR=old-cache",
    ]))
    before = dict(os.environ)
    conf = CerebrasConfig(KEY, cache_dir=tmp_path / "bot-cache")
    with conf.environment():
        _load_dotenv(str(dotenv))
        actual = _config()
        assert actual["api_key"] == KEY and actual["model"] == "gpt-oss-120b"
        assert actual["base_url"] == "https://api.cerebras.ai/v1"
        assert actual["response_format_mode"] == "json_schema"
        assert actual["generation_profile"] == "extraction"
        assert actual["reasoning_effort"] == "low"
        assert actual["max_tokens"] == 4096 and actual["timeout"] == 60
        assert actual["offline"] is False
        assert os.environ["GUEST_PARSER_WORKERS"] == "1"
        assert os.environ["GUEST_PARSER_CACHE_DIR"] == str(tmp_path / "bot-cache")
        assert os.environ["OPENAI_API_KEY"] == os.environ["OPENROUTER_API_KEY"] == ""
    assert dict(os.environ) == before


def test_environment_restores_managed_settings_after_error_but_keeps_unrelated_changes(monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "old-model")
    monkeypatch.setenv("GUEST_PARSER_OFFLINE", "1")
    monkeypatch.setenv("HOTEL_CONFIG_TEST_UNRELATED", "before")
    conf = CerebrasConfig(KEY)
    with pytest.raises(RuntimeError, match="synthetic failure"):
        with conf.environment():
            os.environ["LLM_NEW_SETTING"] = "temporary"
            os.environ["HOTEL_CONFIG_TEST_UNRELATED"] = "after"
            raise RuntimeError("synthetic failure")
    assert os.environ["LLM_MODEL"] == "old-model"
    assert os.environ["GUEST_PARSER_OFFLINE"] == "1"
    assert "LLM_API_KEY" not in os.environ and "LLM_NEW_SETTING" not in os.environ
    assert os.environ["HOTEL_CONFIG_TEST_UNRELATED"] == "after"
    with conf.environment():  # The lock is released even when the body fails.
        assert os.environ["LLM_API_KEY"] == KEY


def test_nested_or_concurrent_configuration_cannot_replace_a_running_servers_key():
    conf = CerebrasConfig(KEY)
    other = CerebrasConfig(OTHER_KEY)
    errors = []

    def concurrent_attempt():
        try:
            with other.environment():
                errors.append("unexpectedly entered")
        except CerebrasConfigurationError as exc:
            errors.append(type(exc))

    with conf.environment():
        with pytest.raises(CerebrasConfigurationError):
            with other.environment():
                pytest.fail("Nested context must not replace the credential")
        thread = Thread(target=concurrent_attempt)
        thread.start()
        thread.join(timeout=2)
        assert not thread.is_alive() and errors == [CerebrasConfigurationError]
        assert os.environ["LLM_API_KEY"] == KEY
    with other.environment():
        assert os.environ["LLM_API_KEY"] == OTHER_KEY


@pytest.mark.parametrize("platform,exists,expected", [
    ("darwin", True, True), ("darwin", False, False), ("linux", True, False),
])
def test_system_ca_fallback_only_on_macos_when_bundle_exists(monkeypatch, tmp_path, platform, exists, expected):
    ca_path = tmp_path / "system-ca.pem"
    if exists:
        ca_path.write_text("synthetic bundle marker, never used for a connection")
    monkeypatch.setattr(config_module.sys, "platform", platform)
    monkeypatch.setattr(config_module, "_SYSTEM_CA_FILE", ca_path)
    with CerebrasConfig(KEY).environment():
        assert (os.environ.get("SSL_CERT_FILE") == str(ca_path)) is expected
    assert "SSL_CERT_FILE" not in os.environ


def test_explicit_custom_ca_is_preserved_inside_and_after_context(monkeypatch, tmp_path):
    monkeypatch.setenv("SSL_CERT_FILE", "custom-ca.pem")
    monkeypatch.setattr(config_module.sys, "platform", "darwin")
    ca_path = tmp_path / "system-ca.pem"
    ca_path.write_text("synthetic bundle marker")
    monkeypatch.setattr(config_module, "_SYSTEM_CA_FILE", ca_path)
    with CerebrasConfig(KEY).environment():
        assert os.environ["SSL_CERT_FILE"] == "custom-ca.pem"
    assert os.environ["SSL_CERT_FILE"] == "custom-ca.pem"
