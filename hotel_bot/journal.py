"""Append-only, durable action journal with explicit reasons and provenance.

This is a record of application events, not evidence that a proposed physical
action happened. Authentication headers and credential fields are never accepted.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
from threading import RLock
from uuid import uuid4


class JournalCorruptionError(ValueError):
    """An existing journal is incomplete or invalid; it must not be truncated."""


_FIELDS = {"id", "timestamp", "kind", "title", "reason", "room_id", "request_id",
           "proposal_id", "action", "status", "evidence", "details"}
_CREDENTIAL_FIELDS = {"authorization", "proxyauthorization", "apikey", "password", "passwd",
                      "secret", "clientsecret", "accesstoken", "refreshtoken", "idtoken",
                      "cookie", "setcookie", "headers", "httpheaders"}
_CREDENTIAL_VALUE = re.compile(r"\bcsk-[A-Za-z0-9_-]{16,}|\bBearer\s+[A-Za-z0-9_.~+/-]{16,}=?|\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}", re.I)


def _time(value):
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise ValueError("Journal timestamp must be a valid UTC instant.") from None
        if not value.endswith("Z") or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
            raise ValueError("Journal timestamp must use UTC with Z.")
        return value
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("Journal timestamp requires a timezone.")
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _safe(value, *, redact):
    """Validate JSON recursively and redact recognizable tokens in text fields."""
    if isinstance(value, dict):
        result = {}
        for key, child in value.items():
            if not isinstance(key, str):
                raise ValueError("Journal object keys must be strings.")
            normalized = re.sub(r"[^a-z0-9]", "", key.lower())
            if normalized in _CREDENTIAL_FIELDS or normalized.endswith("apikey"):
                raise ValueError("Credential fields cannot be written to an action journal.")
            result[key] = _safe(child, redact=redact)
        return result
    if isinstance(value, list):
        return [_safe(child, redact=redact) for child in value]
    if isinstance(value, str):
        if not redact and _CREDENTIAL_VALUE.search(value):
            raise ValueError("A journal contains a credential-shaped value.")
        return _CREDENTIAL_VALUE.sub("[credential redacted]", value) if redact else value
    if value is None or type(value) in {bool, int, float}:
        return value
    raise ValueError("Journal values must be JSON compatible.")


def _validate(entry, expected_id):
    if not isinstance(entry, dict) or set(entry) != _FIELDS:
        raise ValueError("Journal entry fields are invalid.")
    if type(entry["id"]) is not int or entry["id"] != expected_id:
        raise ValueError("Journal sequence is not contiguous.")
    _time(entry["timestamp"])
    for name in ("kind", "title", "reason"):
        if not isinstance(entry[name], str) or not entry[name].strip():
            raise ValueError("Journal entries require a kind, title and reason.")
    if len(entry["kind"]) > 100 or len(entry["title"]) > 1000 or len(entry["reason"]) > 100000:
        raise ValueError("Journal text exceeds its limit.")
    for name in ("room_id", "request_id", "proposal_id", "action", "status"):
        if entry[name] is not None and (not isinstance(entry[name], str) or not entry[name].strip()):
            raise ValueError("Journal identities, actions and statuses must be strings or null.")
    if not isinstance(entry["evidence"], list) or not all(isinstance(x, str) and bool(x.strip()) for x in entry["evidence"]):
        raise ValueError("Journal evidence must be a list of nonempty strings.")
    if not isinstance(entry["details"], dict):
        raise ValueError("Journal details must be an object.")
    _safe(entry, redact=False)
    json.dumps(entry, allow_nan=False)


def _strict(raw):
    def unique(pairs):
        out = {}
        for key, value in pairs:
            if key in out:
                raise ValueError("Duplicate journal field.")
            out[key] = value
        return out
    def constant(value):
        raise ValueError("Invalid JSON constant.")
    return json.loads(raw, object_pairs_hook=unique, parse_constant=constant)


class SessionJournal:
    """Persistent sequence; after is the last seen ID, and limit is 1..500.

    ``path`` supports a replay manager's own output generation directory. The
    default is outputs/bot-session/<session_id>/actions.jsonl. Paths are trusted
    application configuration, never accepted from guest request bodies.
    """

    def __init__(self, root=None, session_id=None, clock=None, *, path=None):
        self._lock = RLock()
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        if path is not None:
            if root is not None or session_id is not None:
                raise ValueError("Choose a journal path or a session root, not both.")
            candidate = Path(path).expanduser()
            if candidate.suffix != ".jsonl" or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", candidate.name):
                raise ValueError("Use a safe .jsonl journal filename.")
            self.path = candidate.absolute()
            self.session_id = self.path.parent.name
        else:
            session_id = session_id if session_id is not None else "session-" + uuid4().hex
            if not isinstance(session_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", session_id):
                raise ValueError("Use a safe session identifier.")
            base = Path(root).expanduser() if root is not None else Path(__file__).resolve().parents[1] / "outputs" / "bot-session"
            self.path = base.absolute() / session_id / "actions.jsonl"
            self.session_id = session_id
        if self.path.parent.is_symlink() or self.path.is_symlink():
            raise ValueError("A journal cannot use a symlink destination.")
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._entries = []
        self._signature = None
        with self._file(exclusive=True) as handle:
            self._refresh(handle)

    @contextmanager
    def _file(self, *, exclusive):
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(self.path, flags, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "r+b") as handle:
                descriptor = None
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
                try:
                    yield handle
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            if descriptor is not None:
                os.close(descriptor)

    @staticmethod
    def _stat(handle):
        info = os.fstat(handle.fileno())
        return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns

    def _refresh(self, handle):
        signature = self._stat(handle)
        if signature == self._signature:
            return
        handle.seek(0)
        data = handle.read()
        rows = []
        try:
            if data and not data.endswith(b"\n"):
                raise ValueError("Incomplete final journal line.")
            for index, line in enumerate(data.splitlines(), start=1):
                item = _strict(line.decode("utf-8"))
                _validate(item, index)
                rows.append(item)
        except (ValueError, TypeError, KeyError, UnicodeError, RecursionError) as exc:
            raise JournalCorruptionError("The action journal is invalid or incomplete; no entries were discarded.") from None
        self._entries, self._signature = rows, signature

    def append(self, kind, title, reason, *, timestamp=None, room_id=None, request_id=None,
               proposal_id=None, action=None, status=None, evidence=None, details=None):
        entry = _safe({
            "id": 1, "timestamp": _time(self._clock() if timestamp is None else timestamp),
            "kind": kind, "title": title, "reason": reason, "room_id": room_id,
            "request_id": request_id, "proposal_id": proposal_id, "action": action,
            "status": status, "evidence": [] if evidence is None else evidence,
            "details": {} if details is None else details,
        }, redact=True)
        _validate(entry, 1)
        with self._lock, self._file(exclusive=True) as handle:
            self._refresh(handle)
            entry["id"] = len(self._entries) + 1
            encoded = (json.dumps(entry, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n").encode("utf-8")
            if len(encoded) > 4_000_000:
                raise ValueError("Journal entry exceeds the size limit.")
            handle.seek(0, os.SEEK_END)
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
            self._entries.append(entry)
            self._signature = self._stat(handle)
            return deepcopy(entry)

    def read(self, after=0, limit=100):
        if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 500:
            raise ValueError("Journal pagination requires after >= 0 and limit 1..500.")
        with self._lock, self._file(exclusive=False) as handle:
            self._refresh(handle)
            entries = self._entries[after:after + limit]
            return {"entries": deepcopy(entries), "next_cursor": entries[-1]["id"] if entries else after,
                    "total": len(self._entries)}

    @property
    def total(self):
        return self.read(limit=1)["total"]

    def sync(self):
        """Revalidate and sync a previously written but unacknowledged append."""
        with self._lock, self._file(exclusive=True) as handle:
            self._refresh(handle)
            os.fsync(handle.fileno())
