"""Explicit Cerebras startup configuration, independent of the bot interface.

Enter ``config.environment()`` once around the complete server or CLI lifetime.
The parser reads process environment; changing this configuration per request
would be unsafe when a server handles concurrent requests. A process-local lock
rejects overlapping configuration contexts instead of silently changing models.
Credentials never appear in public settings, representations or error messages.
"""
from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import re
import sys
from threading import Lock
from typing import Iterator, Mapping

__all__ = ["CerebrasConfig", "CerebrasConfigurationError", "load_cerebras_config"]

_MODEL = "gpt-oss-120b"
_BASE_URL = "https://api.cerebras.ai/v1"
_PREFIXES = ("LLM_", "OPENAI_", "OPENROUTER_", "CEREBRAS_", "GUEST_PARSER_")
_ALIAS_NAMES = {
    f"{provider}_{suffix}"
    for provider in ("OPENAI", "OPENROUTER", "CEREBRAS")
    for suffix in ("API_KEY", "MODEL", "BASE_URL")
}
_ENVIRONMENT_LOCK = Lock()
_SYSTEM_CA_FILE = Path("/etc/ssl/cert.pem")
_KEY_PATTERN = re.compile(r"csk-[A-Za-z0-9_-]{16,256}\Z", re.ASCII)


class CerebrasConfigurationError(ValueError):
    """A startup configuration problem with a credential-free user message."""


def _validated_key(value: object) -> str:
    if not isinstance(value, str) or not _KEY_PATTERN.fullmatch(value.strip()):
        raise CerebrasConfigurationError(
            "Invalid Cerebras API key. Check CEREBRAS_API_KEY or the private key file."
        ) from None
    return value.strip()


def _managed(name: str) -> bool:
    return name.startswith(_PREFIXES) or name == "SSL_CERT_FILE"


class CerebrasConfig:
    """Fixed extraction profile with a private credential and redacted repr.

    Use :meth:`public_settings` for status output. Do not serialize the config
    itself. Unlike a dataclass, it has no field dictionary that ``asdict`` or
    ordinary JSON serialization can accidentally expose.
    """

    __slots__ = ("__api_key", "__cache_dir")

    def __init__(self, api_key: str, *, cache_dir: str | Path | None = None):
        self.__api_key = _validated_key(api_key)
        try:
            self.__cache_dir = (Path(cache_dir).expanduser() if cache_dir is not None
                                else Path.home() / ".cache" / "hotel" / "cerebras-guest-parser")
            if "\x00" in str(self.__cache_dir):
                raise ValueError
        except (TypeError, ValueError, RuntimeError):
            raise CerebrasConfigurationError("Invalid Hotel cache directory.") from None

    def __repr__(self) -> str:
        return "CerebrasConfig(provider='cerebras', model='gpt-oss-120b', credentials='<redacted>')"

    def __reduce_ex__(self, protocol):
        raise TypeError("CerebrasConfig cannot be serialized; use public_settings()")

    def public_settings(self) -> dict[str, str | int | bool]:
        """Return fresh, credential-free status data suitable for JSON output."""
        return {
            "provider": "cerebras",
            "model": _MODEL,
            "base_url": _BASE_URL,
            "response_format": "json_schema",
            "generation_profile": "extraction",
            "reasoning_effort": "low",
            "max_completion_tokens": 4096,
            "timeout_seconds": 60,
            "workers": 1,
            "offline": False,
        }

    @contextmanager
    def environment(self) -> Iterator[CerebrasConfig]:
        """Pin one process-wide profile and restore its previous environment.

        Known aliases are set explicitly, including empty unused aliases, so
        ingestion's non-overriding ``.env`` loader cannot repopulate a competing
        provider. Generic settings also take precedence over provider aliases.
        Unrelated environment changes made inside the context are preserved.
        """
        if not _ENVIRONMENT_LOCK.acquire(blocking=False):
            raise CerebrasConfigurationError(
                "Hotel configuration is already active. Configure the model once at startup."
            )
        previous = {name: value for name, value in os.environ.items() if _managed(name)}
        try:
            values = {name: "" for name in set(previous) | _ALIAS_NAMES if name != "SSL_CERT_FILE"}
            values.update({
                "LLM_API_KEY": self.__api_key,
                "LLM_MODEL": _MODEL,
                "LLM_BASE_URL": _BASE_URL,
                "CEREBRAS_API_KEY": self.__api_key,
                "CEREBRAS_MODEL": _MODEL,
                "CEREBRAS_BASE_URL": _BASE_URL,
                "LLM_RESPONSE_FORMAT": "json_schema",
                "LLM_GENERATION_PROFILE": "extraction",
                "LLM_REASONING_EFFORT": "low",
                "LLM_MAX_TOKENS": "4096",
                "LLM_TIMEOUT_SECONDS": "60",
                "LLM_CACHE_DIR": str(self.__cache_dir),
                "GUEST_PARSER_OFFLINE": "0",
                "GUEST_PARSER_CACHE_DIR": str(self.__cache_dir),
                "GUEST_PARSER_WORKERS": "1",
            })
            # Some macOS Python distributions omit the system CA bundle from
            # their default paths. Use that verified bundle only when no custom
            # trust file was selected; TLS verification stays fully enabled.
            if (not os.environ.get("SSL_CERT_FILE") and sys.platform == "darwin"
                    and _SYSTEM_CA_FILE.is_file()):
                values["SSL_CERT_FILE"] = str(_SYSTEM_CA_FILE)
            os.environ.update(values)
            yield self
        finally:
            try:
                for name in tuple(os.environ):
                    if _managed(name):
                        os.environ.pop(name, None)
                os.environ.update(previous)
            finally:
                _ENVIRONMENT_LOCK.release()


def load_cerebras_config(*, key_file: str | Path | None = None,
                         cache_dir: str | Path | None = None,
                         environ: Mapping[str, str] | None = None) -> CerebrasConfig:
    """Read a key from CEREBRAS_API_KEY, otherwise the private user-level file.

    Explicit malformed environment credentials fail instead of silently using
    another account. This function does not apply environment settings, make
    network requests, create files or inspect any other provider's credentials.
    """
    source = os.environ if environ is None else environ
    key = source.get("CEREBRAS_API_KEY")
    if key is not None and (not isinstance(key, str) or key.strip()):
        return CerebrasConfig(key, cache_dir=cache_dir)
    try:
        path = (Path(key_file).expanduser() if key_file is not None
                else Path.home() / ".config" / "hotel" / "cerebras.key")
        with path.open("rb") as handle:
            # Bound even malformed files; never include their content in errors.
            raw = handle.read(513)
        if len(raw) > 512:
            raise ValueError
        key = raw.decode("ascii")
    except (OSError, UnicodeError, TypeError, ValueError, RuntimeError):
        raise CerebrasConfigurationError(
            "Could not read the Cerebras API key. Set CEREBRAS_API_KEY "
            "or save the key to ~/.config/hotel/cerebras.key."
        ) from None
    return CerebrasConfig(key, cache_dir=cache_dir)
