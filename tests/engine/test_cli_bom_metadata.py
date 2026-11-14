"""The offline CLI must consume the same building metadata as its preflight."""
from codecs import BOM_UTF8
import csv
from hashlib import sha256
import json
from pathlib import Path
import subprocess
import sys

import pytest

from ingestion.loader import REQUIRED_COLUMNS


ROOT = Path(__file__).resolve().parents[2]


def write_csv(path, rows):
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def synthetic_dataset(path, prefix):
    path.mkdir()
    for name, columns in REQUIRED_COLUMNS.items():
        (path / name).write_text(",".join(columns) + "\n", encoding="utf-8")
    building = {
        "time": {"local_timezone": "Europe/Vienna"},
        "building": {"name": "Synthetic Hôtel", "rooms": [{"id": "R101"}], "assets": []},
        "constraints": {"occupied_comfort_c": [20, 23]},
        "staff": {"resources": [{"resource_id": "CLEANER_TEST", "role": "cleaner"}]},
    }
    (path / "building.json").write_bytes(
        prefix + json.dumps(building, ensure_ascii=False).encode("utf-8"))
    (path / "manifest.json").write_text(json.dumps({
        "model_id": "synthetic-cli", "profile": "bom-regression", "synthetic": True,
        "start_utc": "2026-02-11T08:00:00Z", "end_utc": "2026-02-11T08:15:00Z",
        "interval_seconds": 900,
    }), encoding="utf-8")
    write_csv(path / "bookings.csv", [{
        "booking_id": "B_TEST", "room_id": "R101", "created_utc": "2026-02-10T12:00:00Z",
        "arrival_utc": "2026-02-11T12:00:00Z", "departure_utc": "2026-02-12T10:00:00Z",
    }])
    write_csv(path / "guest_requests.csv", [{
        "request_id": "G_TEST", "timestamp_utc": "2026-02-11T08:00:00Z", "room_id": "R101",
        "booking_id": "B_TEST", "request_type": "waiting_for_room",
        "text": "We have arrived and are waiting for our room.", "language": "en",
    }])
    write_csv(path / "staff_roster.csv", [{
        "shift_id": "SHIFT_TEST", "resource_id": "CLEANER_TEST", "role": "cleaner",
        "start_utc": "2026-02-11T07:00:00Z", "end_utc": "2026-02-11T16:00:00Z",
        "status": "scheduled", "status_known_utc": "2026-02-10T12:00:00Z",
    }])
    write_csv(path / "room_telemetry.csv", [{
        "timestamp_utc": "2026-02-11T08:00:00Z", "room_id": "R101", "presence": 0,
        "housekeeping_state": "dirty", "temperature_c": 21, "setpoint_c": 21,
    }])
    (path / "events.jsonl").write_text(json.dumps({
        "event_id": "CHECKOUT_TEST", "type": "checkout", "room_id": "R101",
        "timestamp_utc": "2026-02-11T07:55:00Z",
    }) + "\n", encoding="utf-8")


def invoke(*args):
    return subprocess.run([sys.executable, *map(str, args)], cwd=ROOT,
                          capture_output=True, text=True, timeout=20)


@pytest.mark.parametrize("prefix", [b"", BOM_UTF8], ids=["plain", "leading-bom"])
def test_cli_metadata_preflight_and_replay_preserve_input_bytes(tmp_path, prefix):
    data = tmp_path / "synthetic-data"
    synthetic_dataset(data, prefix)
    before = {path.name: path.read_bytes() for path in data.iterdir()}
    assert len(before) == 17
    preflight = invoke(ROOT / "run.py", "--data-dir", data, "--check")
    assert preflight.returncode == 0, preflight.stderr
    assert json.loads(preflight.stdout)["status"] == "preflight_ok"

    decisions, audit = tmp_path / "decisions.jsonl", tmp_path / "audit.jsonl"
    run = invoke(ROOT / "run.py", "--data-dir", data, "--out", decisions, "--audit", audit)
    assert run.returncode == 0, run.stderr
    summary = json.loads(run.stdout)
    assert summary["status"] == "replay_complete" and summary["events"] > 0
    records = [json.loads(line) for line in decisions.read_text(encoding="utf-8").splitlines()]
    audits = [json.loads(line) for line in audit.read_text(encoding="utf-8").splitlines()]
    assert records and len(records) == len(audits) == summary["decisions"]
    assert all(row["simulated"] and row["effects_applied"] is False
               and row["observed_history_changed"] is False for row in audits)

    report = tmp_path / "validation.json"
    checked = invoke("-m", "engine.validation", "--data-dir", data,
                     "--decisions", decisions, "--audit", audit, "--out", report)
    assert checked.returncode == 0, checked.stderr
    validation = json.loads(report.read_text(encoding="utf-8"))
    assert validation["valid"] and validation["error_count"] == 0
    assert validation["stats"]["joined_decisions"] == len(records)
    paths = {"decisions": decisions, "audit": audit,
             **{path.name: path for path in data.iterdir()}}
    assert validation["hashes_sha256"] == {
        name: sha256(paths[name].read_bytes()).hexdigest()
        for name in validation["hashes_sha256"]}
    assert validation["hashes_sha256"]["building.json"] == sha256(before["building.json"]).hexdigest()
    assert {path.name: path.read_bytes() for path in data.iterdir()} == before
    assert not decisions.read_bytes().startswith(BOM_UTF8)
    assert not audit.read_bytes().startswith(BOM_UTF8)
