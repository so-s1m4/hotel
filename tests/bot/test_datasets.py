"""Untrusted ZIPs become separate local datasets only after complete validation."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import io
import json
import stat
import warnings
from zipfile import ZipFile, ZipInfo, ZIP_DEFLATED, ZIP_STORED

import pytest

from hotel_bot.datasets import DatasetRegistry, DatasetImportError, ImportLimits
from hotel_bot.replay import ReplayManager
from ingestion.loader import REQUIRED_FILES
from tests.bot.test_replay import datasets, Parser, await_terminal


def packed(dataset, *, prefix="delivery/nested/hotel", overrides=None, extras=(), missing=(), second_root=None):
    out = io.BytesIO()
    with warnings.catch_warnings(), ZipFile(out, "w", compression=ZIP_DEFLATED) as archive:
        warnings.simplefilter("ignore", UserWarning)
        for root in [prefix] + ([second_root] if second_root is not None else []):
            for name in REQUIRED_FILES:
                if name not in missing:
                    raw = (overrides or {}).get(name, (dataset / name).read_bytes())
                    archive.writestr((root + "/" if root else "") + name, raw)
        for name, raw in extras:
            archive.writestr(name, raw)
    return out.getvalue()


def upload(registry, raw, **kwargs):
    return registry.import_zip(io.BytesIO(raw), len(raw), **kwargs)


def test_nested_import_preserves_bytes_provenance_permissions_and_deduplicates(datasets, tmp_path):
    _, dataset, _ = datasets
    original = {name: (dataset / name).read_bytes() for name in REQUIRED_FILES}
    raw = packed(dataset, extras=[("delivery/private-not-a-dataset.txt", b"do not retain unrelated files")])
    registry = DatasetRegistry(tmp_path / "imports")
    result = upload(registry, raw)
    ident = result["dataset"]["id"]
    assert ident.startswith("dataset-") and len(ident) == 72
    assert not result["duplicate"] and result["dataset"]["imported"] and result["dataset"]["available"]
    assert result["dataset"]["replay_id"] is None and "2026-01-06" in result["dataset"]["name"]
    imported = registry.resolve(ident)
    assert {path.name for path in imported.iterdir()} == set(REQUIRED_FILES)
    assert {name: (imported / name).read_bytes() for name in REQUIRED_FILES} == original
    assert {name: (dataset / name).read_bytes() for name in REQUIRED_FILES} == original
    record = json.loads((imported.parent / "import.json").read_text())
    assert record["archive_sha256"] == sha256(raw).hexdigest()
    assert record["source_root"] == "delivery/nested/hotel"
    assert record["files_sha256"] == {name: sha256(value).hexdigest() for name, value in original.items()}
    assert stat.S_IMODE(registry.root.stat().st_mode) == 0o700
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in imported.iterdir())
    repeat = upload(registry, packed(dataset, prefix="another-wrapper"))
    assert repeat["duplicate"] and repeat["dataset"] == result["dataset"]
    assert list(registry.root.iterdir()) == [imported.parent]
    assert DatasetRegistry(registry.root).list_datasets() == [result["dataset"]]


def test_imported_dataset_replays_only_after_explicit_start_and_restores(datasets, tmp_path):
    root, dataset, outputs = datasets
    parser = Parser()
    manager = ReplayManager(root, outputs, parser=parser, import_root=tmp_path / "imports")
    raw = packed(dataset)
    result = manager.import_dataset(io.BytesIO(raw), len(raw))
    ident = result["dataset"]["id"]
    assert parser.calls == [] and manager._jobs == {} and manager._active is None
    assert {row["id"] for row in manager.list_datasets()} == {"quickstart", "dev", ident}
    job = manager.start(ident)
    final = await_terminal(manager, job["id"])
    assert final["job"]["complete"] and final["job"]["dataset_id"] == ident
    assert len(parser.calls) == 2
    manager.close()

    def forbidden(*args, **kwargs):
        raise AssertionError("Opening an imported archive must not call the model")

    restored = ReplayManager(root, outputs, parser=forbidden, import_root=tmp_path / "imports")
    assert restored.status(job["id"])["job"]["complete"]
    assert restored.playback_manifest(job["id"])["dataset_id"] == ident
    assert next(row for row in restored.list_datasets() if row["id"] == ident)["replay_id"] == job["id"]
    restored.close()


@pytest.mark.parametrize("name", ["../outside.txt", "/absolute.txt", "C:/private.txt", "a\\evil.txt", "a/../evil.txt", "a//evil.txt"])
def test_unsafe_archive_paths_are_rejected_without_publication(datasets, tmp_path, name):
    _, dataset, _ = datasets
    registry = DatasetRegistry(tmp_path / "imports")
    with pytest.raises(DatasetImportError, match="unsafe path"):
        upload(registry, packed(dataset, extras=[(name, b"unsafe")]))
    assert list(registry.root.iterdir()) == []


def test_symlink_duplicate_missing_and_ambiguous_roots_are_rejected(datasets, tmp_path):
    _, dataset, _ = datasets
    link = ZipInfo("linked")
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    cases = [
        (packed(dataset, extras=[(link, b"/private")]), "unsafe_zip_entry"),
        (packed(dataset, extras=[("delivery/nested/hotel/building.json", b"{}")]), "duplicate_zip_path"),
        (packed(dataset, missing=["events.jsonl"]), "missing_dataset_files"),
        (packed(dataset, second_root="second-dataset"), "ambiguous_dataset"),
    ]
    registry = DatasetRegistry(tmp_path / "imports")
    for raw, code in cases:
        with pytest.raises(DatasetImportError) as error:
            upload(registry, raw)
        assert error.value.public_code == code
        assert list(registry.root.iterdir()) == []


def test_encrypted_flags_are_rejected_with_decryption_instruction(datasets, tmp_path):
    _, dataset, _ = datasets
    raw = bytearray(packed(dataset))
    for signature, offset in [(b"PK\x03\x04", 6), (b"PK\x01\x02", 8)]:
        start = 0
        while (position := raw.find(signature, start)) >= 0:
            raw[position + offset] |= 1
            start = position + 4
    registry = DatasetRegistry(tmp_path / "imports")
    with pytest.raises(DatasetImportError) as error:
        upload(registry, bytes(raw))
    assert error.value.public_code == "encrypted_zip" and "Decrypt" in str(error.value)
    assert list(registry.root.iterdir()) == []


@pytest.mark.parametrize("field,value", [("upload_bytes", 10), ("expanded_bytes", 500), ("file_bytes", 30), ("entries", 5), ("compression_ratio", 1)])
def test_all_size_limits_are_enforced_before_publication(datasets, tmp_path, field, value):
    _, dataset, _ = datasets
    registry = DatasetRegistry(tmp_path / "imports", limits=replace(ImportLimits(), **{field: value}))
    with pytest.raises(DatasetImportError) as error:
        upload(registry, packed(dataset))
    assert error.value.status_code == 413
    assert not registry.root.exists() or list(registry.root.iterdir()) == []


@pytest.mark.parametrize("body", [
    b"request_id,timestamp_utc,request_type,text\nG1,2026-01-01T00:00:00Z,wifi_help,\"unterminated\n",
    b"request_id,timestamp_utc,request_type,text\nG1,2026-01-01T00:00:00Z,wifi_help,hello,extra\n",
    b"request_id,timestamp_utc,request_type,text\nG1,bad-time,wifi_help,hello\n",
    b"request_id,timestamp_utc,request_type,text,text\n",
    b"request_id,timestamp_utc,request_type,text, text\nG1,2026-01-01T00:00:00Z,extra_towels,Towels,Lost text\n",
    b"request_id,timestamp_utc,request_type,text,\n",
    b"",
])
def test_malformed_csv_bodies_headers_and_dates_are_rejected(datasets, tmp_path, body):
    _, dataset, _ = datasets
    registry = DatasetRegistry(tmp_path / "imports")
    with pytest.raises(DatasetImportError) as error:
        upload(registry, packed(dataset, overrides={"guest_requests.csv": body}))
    assert error.value.public_code == "invalid_dataset_schema"
    assert list(registry.root.iterdir()) == []


@pytest.mark.parametrize("body", [
    b'{"event_id":"E1","timestamp_utc":"2026-01-01T00:00:00Z","value":1e999}\n',
    b'{"event_id":"E1","timestamp_utc":"2026-01-01T00:00:00Z","value":NaN}\n',
    b'{"event_id":"E1","event_id":"E2","timestamp_utc":"2026-01-01T00:00:00Z"}\n',
    b'{"event_id":"E1","timestamp_utc":"not-time"}\n',
])
def test_invalid_json_and_nonfinite_numbers_are_rejected(datasets, tmp_path, body):
    _, dataset, _ = datasets
    with pytest.raises(DatasetImportError):
        upload(DatasetRegistry(tmp_path / "imports"), packed(dataset, overrides={"events.jsonl": body}))


def test_combined_source_and_clock_events_fit_archive_restore_limit(datasets, tmp_path):
    _, dataset, _ = datasets
    manifest = json.loads((dataset / "manifest.json").read_text())
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    manifest.update(start_utc=start.isoformat(), end_utc=(start + timedelta(minutes=199999)).isoformat(), interval_seconds=60)
    with pytest.raises(DatasetImportError) as error:
        upload(DatasetRegistry(tmp_path / "imports"), packed(dataset, overrides={"manifest.json": json.dumps(manifest)}))
    assert error.value.public_code == "dataset_event_limit"


@pytest.mark.parametrize("room", [{"id": "R999", "floor": 9}, {"id": "R101", "floor": 2}])
def test_unsupported_3d_room_topology_is_explicit(datasets, tmp_path, room):
    _, dataset, _ = datasets
    building = json.loads((dataset / "building.json").read_text())
    building["building"]["rooms"] = [room]
    with pytest.raises(DatasetImportError) as error:
        upload(DatasetRegistry(tmp_path / "imports"), packed(dataset, overrides={"building.json": json.dumps(building)}))
    assert error.value.public_code == "unsupported_hotel_layout"


def test_streaming_reads_are_bounded_and_interruption_or_shutdown_cleans_staging(datasets, tmp_path, monkeypatch):
    _, dataset, _ = datasets
    raw = packed(dataset)
    registry = DatasetRegistry(tmp_path / "imports")

    class Bounded(io.BytesIO):
        def read1(self, size):
            assert 0 < size <= 64 * 1024
            return super().read1(size)

    with pytest.raises(DatasetImportError, match="interrupted"):
        registry.import_zip(Bounded(raw[:-10]), len(raw))
    assert list(registry.root.iterdir()) == []
    validate = registry._validate

    def stopping(data, check):
        with pytest.raises(DatasetImportError) as error:
            validate(data, lambda: (_ for _ in ()).throw(DatasetImportError("import_cancelled", "Stopping", 503)))
        raise error.value

    monkeypatch.setattr(registry, "_validate", stopping)
    with pytest.raises(DatasetImportError) as error:
        registry.import_zip(Bounded(raw), len(raw))
    assert error.value.status_code == 503 and list(registry.root.iterdir()) == []


def test_imported_source_changes_are_not_silently_accepted(datasets, tmp_path):
    _, dataset, _ = datasets
    registry = DatasetRegistry(tmp_path / "imports")
    result = upload(registry, packed(dataset))
    ident = result["dataset"]["id"]
    (registry.location(ident) / "events.jsonl").write_text("changed source")
    with pytest.raises(DatasetImportError):
        registry.resolve(ident)
