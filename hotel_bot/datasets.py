"""Bounded local ZIP import for one independent PLATO-compatible dataset."""
from __future__ import annotations

import codecs
from copy import deepcopy
import csv
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import io
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import tempfile
from threading import Lock
from time import monotonic
import unicodedata
from zipfile import BadZipFile, ZipFile, ZIP_DEFLATED, ZIP_STORED
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from engine.core import instant
from ingestion.loader import REQUIRED_COLUMNS, REQUIRED_FILES, inspect_dataset, parse_ts

IMPORTED_ID = re.compile(r"dataset-[0-9a-f]{64}\Z")
MAX_UPLOAD_BYTES = 128 * 1024 * 1024


@dataclass(frozen=True)
class ImportLimits:
    upload_bytes: int = MAX_UPLOAD_BYTES
    expanded_bytes: int = 512 * 1024 * 1024
    file_bytes: int = 256 * 1024 * 1024
    entries: int = 2000
    compression_ratio: int = 200
    upload_seconds: int = 180


class DatasetImportError(ValueError):
    """Only controlled, user-actionable text may cross the HTTP boundary."""

    def __init__(self, code, message, status=400):
        super().__init__(message)
        self.public_code, self.public_message, self.status_code = code, message, status


def _fail(code, message, status=400):
    raise DatasetImportError(code, message, status)


def _json(raw, *, allow_bom=True):
    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise ValueError("Duplicate JSON field")
            result[key] = value
        return result

    def invalid(value):
        raise ValueError("Non-finite JSON value")

    def finite_float(value):
        parsed = float(value)
        if not math.isfinite(parsed):
            raise ValueError("Non-finite JSON value")
        return parsed

    return json.loads(raw.decode("utf-8-sig" if allow_bom else "utf-8"), object_pairs_hook=pairs,
                      parse_constant=invalid, parse_float=finite_float)


def _digest(path):
    digest = sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _private_file(path):
    return os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb")


class DatasetRegistry:
    """Private, immutable content-addressed imports, separate from original data."""

    def __init__(self, root, *, limits=None):
        root = Path(root).expanduser().absolute()
        if root.is_symlink():
            _fail("unsafe_import_storage", "The local import folder must not be a symbolic link.")
        self.root = root.resolve()
        self.limits = limits or ImportLimits()
        self._import_lock = Lock()

    def location(self, dataset_id):
        if not isinstance(dataset_id, str) or not IMPORTED_ID.fullmatch(dataset_id):
            _fail("unknown_dataset", "Choose a dataset from the list.", 404)
        return self.root / dataset_id / "data"

    def _record(self, dataset_id, *, verify=False):
        data = self.location(dataset_id)
        root, metadata = data.parent, data.parent / "import.json"
        try:
            if root.is_symlink() or data.is_symlink() or metadata.is_symlink() or not data.is_dir():
                raise ValueError
            if not metadata.is_file() or metadata.stat().st_size > 1024 * 1024:
                raise ValueError
            record = _json(metadata.read_bytes())
            hashes = record["files_sha256"]
            if (record["id"] != dataset_id or record["dataset"]["id"] != dataset_id
                    or set(hashes) != set(REQUIRED_FILES)
                    or any(not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value) for value in hashes.values())):
                raise ValueError
            content_hash = sha256(json.dumps(hashes, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            if dataset_id != "dataset-" + content_hash or record.get("content_sha256") != content_hash:
                raise ValueError
            for name in REQUIRED_FILES:
                path = data / name
                if path.is_symlink() or not path.is_file() or (verify and _digest(path) != hashes[name]):
                    raise ValueError
            return record
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            _fail("import_unavailable", "This imported dataset is missing or changed. Restore the original files in its existing saved folder; uploading the same ZIP will not overwrite or repair it. Re-upload only if the entire saved folder is missing.", 404)

    def resolve(self, dataset_id):
        self._record(dataset_id, verify=True)
        return self.location(dataset_id)

    def list_datasets(self):
        result = []
        if not self.root.is_dir():
            return result
        for root in sorted(self.root.glob("dataset-*")):
            if not IMPORTED_ID.fullmatch(root.name):
                continue
            try:
                result.append(deepcopy(self._record(root.name)["dataset"]))
            except DatasetImportError:
                continue
        return result

    def _members(self, archive):
        entries = archive.infolist()
        if len(entries) > self.limits.entries:
            _fail("zip_entry_limit", "The ZIP has too many entries; include only one dataset folder.", 413)
        seen, files, total = set(), {}, 0
        for member in entries:
            name = member.orig_filename
            parts = name.rstrip("/").split("/")
            if (not name or len(name.encode("utf-8")) > 1024 or name.startswith("/") or "\\" in name
                    or "\x00" in name or any(part in {"", ".", ".."} or ":" in part for part in parts)):
                _fail("unsafe_zip_path", "The ZIP contains an unsafe path. Recreate it from the dataset folder.")
            key = unicodedata.normalize("NFC", name.rstrip("/")).casefold()
            if key in seen:
                _fail("duplicate_zip_path", "The ZIP contains duplicate or conflicting file names.")
            seen.add(key)
            mode = stat.S_IFMT(member.external_attr >> 16)
            if mode not in {0, stat.S_IFREG, stat.S_IFDIR}:
                _fail("unsafe_zip_entry", "Links and special files are not allowed in a dataset ZIP.")
            if member.flag_bits & 1:
                _fail("encrypted_zip", "This ZIP is encrypted. Decrypt it on your computer, then upload an unencrypted ZIP.")
            if member.compress_type not in {ZIP_STORED, ZIP_DEFLATED}:
                _fail("unsupported_zip", "Use a standard ZIP with stored files or Deflate compression.")
            total += member.file_size
            if member.file_size > self.limits.file_bytes or total > self.limits.expanded_bytes:
                _fail("expanded_size_limit", "The uncompressed ZIP is too large (512 MiB total, 256 MiB per file).", 413)
            if member.file_size > max(1, member.compress_size) * self.limits.compression_ratio:
                _fail("zip_compression_limit", "This ZIP expands too aggressively. Recreate it with normal compression.", 413)
            if not member.is_dir():
                files[PurePosixPath(name)] = member
        candidates = {}
        required = set(REQUIRED_FILES)
        for path in files:
            if path.name in required:
                candidates.setdefault(path.parent, set()).add(path.name)
        roots = [root for root, names in candidates.items() if required <= names]
        if len(roots) > 1:
            _fail("ambiguous_dataset", "The ZIP contains multiple complete datasets. Upload Quickstart, Dev, or the new dataset separately.")
        if not roots:
            closest = max(candidates.values(), key=len, default=set())
            missing = ", ".join(sorted(required - closest))
            _fail("missing_dataset_files", "No complete dataset folder was found. Missing files: " + missing + ".")
        root = roots[0]
        return root, {name: files[root / name] for name in REQUIRED_FILES}

    def _validate(self, data, check):
        source_events = 0
        event_weights = {"bookings.csv": 1, "guest_requests.csv": 1, "staff_roster.csv": 2, "work_orders.csv": 2}
        for name, required in REQUIRED_COLUMNS.items():
            check()
            try:
                with (data / name).open("r", encoding="utf-8-sig", newline="") as stream:
                    line = stream.readline(65537)
                    if len(line) > 65536:
                        raise ValueError
                    header = [column.strip() for column in next(csv.reader(io.StringIO(line), strict=True), [])]
                    if not header or any(not column for column in header) or len(header) != len(set(header)):
                        raise ValueError
                    missing = [column for column in required if column not in header]
                    if missing:
                        _fail("invalid_dataset_schema", f"{name} is missing columns: {', '.join(missing)}.")
                    timestamp_columns = [index for index, column in enumerate(header) if column.endswith("_utc")]
                    for index, row in enumerate(csv.reader(stream, strict=True)):
                        if index % 1024 == 0:
                            check()
                        if not row:
                            continue
                        if len(row) != len(header):
                            _fail("invalid_dataset_schema", f"{name} contains a row whose fields do not match its header.")
                        for position in timestamp_columns:
                            if row[position].strip():
                                parse_ts(row[position])
                        source_events += event_weights.get(name, 0)
                        if source_events > 200_000:
                            _fail("dataset_event_limit", "This dataset exceeds the supported 200,000-event replay size.", 413)
            except (OSError, UnicodeError, csv.Error, ValueError) as exc:
                if isinstance(exc, DatasetImportError):
                    raise
                _fail("invalid_dataset_schema", f"{name} needs valid UTF-8 CSV rows, matching columns and valid timestamp values.")
        try:
            check()
            objects = {}
            for name in ("manifest.json", "building.json"):
                if (data / name).stat().st_size > 8 * 1024 * 1024:
                    raise ValueError
                objects[name] = _json((data / name).read_bytes())
            manifest, building = objects["manifest.json"], objects["building.json"]
            if (not isinstance(manifest, dict) or not isinstance(building, dict)
                    or any(not isinstance(manifest.get(key), str) or not 1 <= len(manifest[key]) <= 128
                           for key in ("model_id", "profile"))):
                raise ValueError
            start, end = instant(manifest["start_utc"]), instant(manifest["end_utc"])
            interval = manifest.get("interval_seconds", 900)
            if (end <= start or type(interval) is not int or not 60 <= interval <= 86400
                    or (end - start).total_seconds() / interval > 200_000):
                _fail("invalid_dataset_period", "Use a valid UTC period and sampling interval with at most 200,000 clock events.")
            source_events += math.ceil((end - start).total_seconds() / interval)
            rooms = building["building"]["rooms"]
            if (not isinstance(rooms, list) or not 1 <= len(rooms) <= 40
                    or any(not isinstance(room, dict) or not isinstance(room.get("id"), str)
                           or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", room["id"]) for room in rooms)
                    or len({room["id"] for room in rooms}) != len(rooms)):
                _fail("invalid_dataset_rooms", "building.json needs 1–40 distinct PLATO40 room IDs.")
            supported = {f"R{floor}{number:02d}" for floor in range(1, 5) for number in range(1, 11)}
            if any(room["id"] not in supported or ("floor" in room and
                   (type(room["floor"]) is not int or room["floor"] != int(room["id"][1]))) for room in rooms):
                _fail("unsupported_hotel_layout", "This 3D demo supports the PLATO40 rooms R101–R110, R201–R210, R301–R310 and R401–R410. Room floors must match their IDs.")
            ZoneInfo((building.get("time") or {}).get("local_timezone") or "Europe/Vienna")
            with (data / "events.jsonl").open("rb") as stream:
                first_line = True
                while line := stream.readline(4_000_001):
                    check()
                    if len(line) > 4_000_000:
                        raise ValueError
                    # Match the loader's file-level decoder, including an
                    # initial BOM followed by blank lines. Keep stored bytes.
                    if first_line:
                        line = line.removeprefix(codecs.BOM_UTF8)
                        first_line = False
                    if not line.strip():
                        continue
                    event = _json(line, allow_bom=False)
                    if not isinstance(event, dict) or not isinstance(event.get("event_id"), (str, int)):
                        raise ValueError
                    parse_ts(event["timestamp_utc"])
                    if "available_at_utc" in event:
                        parse_ts(event["available_at_utc"])
                    source_events += 1
                    if source_events > 200_000:
                        _fail("dataset_event_limit", "Source events and generated clock ticks together exceed 200,000. Split this dataset into smaller periods.", 413)
            if source_events > 200_000:
                _fail("dataset_event_limit", "Source events and generated clock ticks together exceed 200,000. Split this dataset into smaller periods.", 413)
            check()
            metadata = inspect_dataset(data)
            metadata["synthetic"] = manifest.get("synthetic") if type(manifest.get("synthetic")) is bool else None
            return metadata
        except DatasetImportError:
            raise
        except (OSError, ValueError, KeyError, TypeError, AttributeError, UnicodeError, ZoneInfoNotFoundError):
            _fail("invalid_dataset_metadata", "Check manifest.json, building.json and events.jsonl: valid UTF-8 JSON, UTC dates and hotel metadata are required.")

    def import_zip(self, stream, length, *, cancelled=lambda: False):
        if type(length) is not int or not 0 < length <= self.limits.upload_bytes:
            _fail("upload_size_limit", "Choose a ZIP no larger than 128 MiB.", 413)
        if not self._import_lock.acquire(blocking=False):
            _fail("import_busy", "Another ZIP is being imported. Wait for it to finish.", 409)
        stage = None
        started = monotonic()

        def check():
            if cancelled():
                _fail("import_cancelled", "The server is stopping; the ZIP was not registered.", 503)
            if monotonic() - started > self.limits.upload_seconds:
                _fail("upload_timeout", "The upload took too long. Try the ZIP again.", 408)

        try:
            check()
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            if self.root.is_symlink():
                _fail("unsafe_import_storage", "The local import folder must not be a symbolic link.")
            self.root.chmod(0o700)
            if shutil.disk_usage(self.root).free < length + 32 * 1024 * 1024:
                _fail("insufficient_storage", "There is not enough free disk space to import this ZIP.", 507)
            stage = Path(tempfile.mkdtemp(prefix=".import-", dir=self.root))
            upload = stage / "upload.zip"
            digest, remaining = sha256(), length
            read = getattr(stream, "read1", stream.read)
            with _private_file(upload) as target:
                while remaining:
                    check()
                    block = read(min(64 * 1024, remaining))
                    if not block or len(block) > remaining:
                        _fail("incomplete_upload", "The ZIP upload was interrupted. Upload it again.")
                    target.write(block)
                    digest.update(block)
                    remaining -= len(block)
                target.flush()
                os.fsync(target.fileno())
            check()
            hashes, expanded = {}, 0
            data = stage / "data"
            data.mkdir(mode=0o700)
            with ZipFile(upload) as archive:
                source_root, members = self._members(archive)
                if shutil.disk_usage(self.root).free < sum(info.file_size for info in members.values()) + 32 * 1024 * 1024:
                    _fail("insufficient_storage", "There is not enough free disk space to unpack this dataset.", 507)
                for name, member in members.items():
                    file_digest, written = sha256(), 0
                    decoder = codecs.getincrementaldecoder("utf-8-sig")()
                    with archive.open(member) as source, _private_file(data / name) as target:
                        while block := source.read(64 * 1024):
                            check()
                            written += len(block)
                            expanded += len(block)
                            if written > member.file_size or written > self.limits.file_bytes or expanded > self.limits.expanded_bytes:
                                _fail("expanded_size_limit", "The ZIP exceeds the permitted unpacked size.", 413)
                            if b"\x00" in block:
                                _fail("invalid_dataset_encoding", "Dataset files must be UTF-8 text without null bytes.")
                            decoder.decode(block)
                            file_digest.update(block)
                            target.write(block)
                        decoder.decode(b"", final=True)
                        if written != member.file_size:
                            _fail("invalid_zip", "The ZIP is incomplete or damaged. Recreate it and upload again.")
                        target.flush()
                        os.fsync(target.fileno())
                    hashes[name] = file_digest.hexdigest()
            metadata = self._validate(data, check)
            content_hash = sha256(json.dumps(hashes, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            dataset_id = "dataset-" + content_hash
            name = (f"Imported · {metadata['model_id'][:32]} / {metadata['profile'][:20]} · "
                    f"{metadata['start_utc'][:10]}–{metadata['end_utc'][:10]} · {content_hash[:8]}")
            dataset = {"id": dataset_id, "name": name, "available": True,
                       "imported": True, "synthetic": None, "replay_id": None, **metadata}
            record = {"version": 1, "id": dataset_id, "content_sha256": content_hash,
                "imported_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "archive_sha256": digest.hexdigest(), "source_root": str(source_root),
                "files_sha256": hashes, "dataset": dataset}
            with _private_file(stage / "import.json") as target:
                target.write((json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n").encode())
                target.flush()
                os.fsync(target.fileno())
            upload.unlink()
            check()
            destination = self.root / dataset_id
            if destination.exists() or destination.is_symlink():
                existing = self._record(dataset_id, verify=True)
                return {"dataset": deepcopy(existing["dataset"]), "duplicate": True}
            stage.rename(destination)
            stage = None
            return {"dataset": deepcopy(dataset), "duplicate": False}
        except DatasetImportError:
            raise
        except UnicodeError:
            _fail("invalid_dataset_encoding", "Dataset files must use UTF-8 text.")
        except (BadZipFile, EOFError, NotImplementedError, RuntimeError):
            _fail("invalid_zip", "The ZIP is damaged or unsupported. Recreate a standard, unencrypted ZIP and try again.")
        except TimeoutError:
            _fail("upload_timeout", "The upload timed out. Try the ZIP again.", 408)
        finally:
            if stage is not None:
                shutil.rmtree(stage, ignore_errors=True)
            self._import_lock.release()
