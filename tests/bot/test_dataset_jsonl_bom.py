"""ZIP admission must agree with the physical loader's file-level BOM policy."""
from hashlib import sha256
import io
import json
import socket
from zipfile import ZipFile, ZIP_STORED

import pytest

from hotel_bot.datasets import DatasetImportError, DatasetRegistry
from ingestion.loader import Dataset, REQUIRED_COLUMNS, REQUIRED_FILES

BOM = b"\xef\xbb\xbf"
EVENTS = [
    {"event_id": "BOM_NOTE_A", "timestamp_utc": "2029-04-09T00:10:00Z", "type": "note", "room_id": "R101"},
    {"event_id": "BOM_NOTE_B", "timestamp_utc": "2029-04-09T00:20:00Z", "type": "note", "room_id": "R101"},
]


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False) + "\n").encode("utf-8")


LINE_A, LINE_B = map(json_bytes, EVENTS)
STRING_EVENTS = [{**EVENTS[0], "note": "Keep the literal \ufeff character."}, EVENTS[1]]


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Synthetic admission tests must not contact a model or network")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)


def synthetic_archive(tmp_path, event_bytes, metadata_bom=None):
    source = tmp_path / "synthetic-inputs"
    source.mkdir()
    payloads = {name: (",".join(columns) + "\n").encode() for name, columns in REQUIRED_COLUMNS.items()}
    payloads.update({
        "building.json": json_bytes({"building": {"rooms": [{"id": "R101", "floor": 1}]}}),
        "manifest.json": json_bytes({"model_id": "BOM-admission-test", "profile": "synthetic",
            "start_utc": "2029-04-09T00:00:00Z", "end_utc": "2029-04-09T01:00:00Z", "interval_seconds": 900}),
        "events.jsonl": event_bytes,
    })
    if metadata_bom is not None:
        payloads[metadata_bom] = BOM + payloads[metadata_bom]
    out = io.BytesIO()
    with ZipFile(out, "w", compression=ZIP_STORED) as archive:
        for name in REQUIRED_FILES:
            (source / name).write_bytes(payloads[name])
            archive.writestr("synthetic/hotel/" + name, payloads[name])
    return source, payloads, out.getvalue()


@pytest.mark.parametrize("body,expected", [
    (LINE_A + LINE_B, EVENTS),
    (BOM + LINE_A + LINE_B, EVENTS),
    (b"\n \t\r\n" + LINE_A + LINE_B, EVENTS),
    (BOM + b"\n \t\r\n" + LINE_A + LINE_B, EVENTS),
    (b"".join(map(json_bytes, STRING_EVENTS)), STRING_EVENTS),
], ids=["plain", "file-leading-bom", "leading-blank-lines", "bom-before-blank-lines", "literal-bom-in-string"])
def test_admitted_jsonl_preserves_all_events_and_original_bytes(tmp_path, body, expected):
    source, payloads, archive = synthetic_archive(tmp_path, body)
    registry = DatasetRegistry(tmp_path / "imports")
    result = registry.import_zip(io.BytesIO(archive), len(archive))
    imported = registry.resolve(result["dataset"]["id"])
    assert result["dataset"]["available"] and not result["duplicate"]
    dataset = Dataset(imported)
    assert dataset.raw_events == expected
    assert dataset.audit == {}
    assert {name: (imported / name).read_bytes() for name in REQUIRED_FILES} == payloads
    assert {name: (source / name).read_bytes() for name in REQUIRED_FILES} == payloads
    metadata = json.loads((imported.parent / "import.json").read_text())
    assert metadata["archive_sha256"] == sha256(archive).hexdigest()
    assert metadata["files_sha256"] == {name: sha256(raw).hexdigest() for name, raw in payloads.items()}


@pytest.mark.parametrize("body", [
    LINE_A + BOM + LINE_B,
    BOM + LINE_A + BOM + LINE_B,
    b"\n" + BOM + LINE_A + LINE_B,
    BOM + BOM + LINE_A + LINE_B,
], ids=["later-line", "leading-and-later", "after-blank-first-line", "repeated-file-prefix"])
def test_nonleading_or_repeated_bom_is_rejected_before_publication(tmp_path, body):
    source, payloads, archive = synthetic_archive(tmp_path, body)
    physical = Dataset(source)
    assert len(physical.raw_events) == 1
    assert physical.audit["events.jsonl:unparseable_lines"] == 1
    registry = DatasetRegistry(tmp_path / "imports")
    with pytest.raises(DatasetImportError) as error:
        registry.import_zip(io.BytesIO(archive), len(archive))
    assert error.value.public_code == "invalid_dataset_metadata"
    assert error.value.status_code == 400
    assert registry.list_datasets() == []
    assert list(registry.root.iterdir()) == []
    assert {name: (source / name).read_bytes() for name in REQUIRED_FILES} == payloads


@pytest.mark.parametrize("name", ["building.json", "manifest.json"])
def test_metadata_json_keeps_its_existing_leading_bom_support(tmp_path, name):
    source, payloads, archive = synthetic_archive(tmp_path, LINE_A + LINE_B, metadata_bom=name)
    registry = DatasetRegistry(tmp_path / "imports")
    result = registry.import_zip(io.BytesIO(archive), len(archive))
    imported = registry.resolve(result["dataset"]["id"])
    assert Dataset(imported).raw_events == EVENTS
    assert (imported / name).read_bytes() == (source / name).read_bytes() == payloads[name]
