"""Annual input signatures must not change metrics, source bytes or outputs."""
from codecs import BOM_UTF8
import csv
from hashlib import sha256
import json
import socket
import sys

import pytest

from engine.annual import analyze_dataset, main


FIELDS = {
    "room_telemetry.csv": ["timestamp_utc", "room_id", "temperature_c", "window_open"],
    "room_water.csv": ["timestamp_utc", "room_id", "meter_total_l"],
    "water_main.csv": ["timestamp_utc", "meter_total_l"],
    "energy.csv": ["timestamp_utc", "building_load_kw", "hvac_electric_kw", "pv_kw",
                   "battery_discharge_kw_signed", "battery_soc_kwh", "grid_import_kw", "grid_export_kw"],
    "tariff.csv": ["timestamp_utc", "price_eur_kwh"],
    "equipment.csv": ["timestamp_utc", "asset_id", "metric", "value"],
    "logistics.csv": ["timestamp_utc", "asset_id", "metric", "value"],
    "staff_roster.csv": ["role", "status"],
}


def write_csv(root, name, records):
    with (root / name).open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=FIELDS[name])
        writer.writeheader()
        writer.writerows(records)


def input_bytes(root):
    return {path.name: path.read_bytes() for path in root.iterdir()}


def analyze_preserving_bytes(root):
    before = input_bytes(root)
    try:
        result = analyze_dataset(root)
    finally:
        assert input_bytes(root) == before
    assert result["source_sha256"] == {name: sha256(raw).hexdigest() for name, raw in before.items()}
    return result


@pytest.fixture
def source(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Synthetic annual analysis must not use a network or model")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    root = tmp_path / "source"
    root.mkdir()
    (root / "manifest.json").write_text(json.dumps({
        "profile": "synthetic-bom", "model_id": "ANNUAL_BOM_TEST", "days": 1,
        "start_utc": "2030-04-16T00:00:00Z", "end_utc": "2030-04-17T00:00:00Z",
    }), encoding="utf-8")
    (root / "building.json").write_text(json.dumps({
        "building": {"rooms": [{"id": "R901", "orientation": "N"}]},
        "constraints": {"occupied_comfort_c": [20, 23]},
    }), encoding="utf-8")
    events = [
        {"event_id": "SYN_IN", "timestamp_utc": "2030-04-16T08:00:00Z", "type": "checkin",
         "room_id": "R901", "booking_id": "SYN_BOOK"},
        {"event_id": "SYN_OUT", "timestamp_utc": "2030-04-16T08:30:00Z", "type": "checkout",
         "room_id": "R901", "booking_id": "SYN_BOOK"},
    ]
    (root / "events.jsonl").write_text("".join(json.dumps(row) + "\n" for row in events), encoding="utf-8")
    for name in FIELDS:
        write_csv(root, name, [])
    times = ["2030-04-16T08:00:00Z", "2030-04-16T08:15:00Z"]
    write_csv(root, "room_telemetry.csv", [
        {"timestamp_utc": t, "room_id": "R901", "temperature_c": value, "window_open": 0}
        for t, value in zip(times, [19, 21])])
    write_csv(root, "room_water.csv", [
        {"timestamp_utc": t, "room_id": "R901", "meter_total_l": value}
        for t, value in zip(times, [100, 109])])
    write_csv(root, "water_main.csv", [
        {"timestamp_utc": t, "meter_total_l": value} for t, value in zip(times, [200, 213])])
    write_csv(root, "staff_roster.csv", [{"role": "cleaner", "status": "scheduled"}])
    return root


@pytest.mark.parametrize("filename", [None, "building.json", "manifest.json", "events.jsonl", "room_telemetry.csv"],
                         ids=["plain", "building", "manifest", "events", "csv-header"])
def test_annual_cli_accepts_one_leading_input_bom_without_changing_metrics(source, tmp_path, monkeypatch, capsys, filename):
    expected = analyze_preserving_bytes(source)
    assert expected["temperature"]["occupied_samples"] == 2
    assert expected["temperature"]["cold_samples"] == 1
    assert expected["water"]["valid_room_interval_liters"] == 9
    assert expected["water"]["complete_interval_residual_liters"] == 4
    if filename:
        path = source / filename
        path.write_bytes(BOM_UTF8 + path.read_bytes())
    before = input_bytes(source)
    report, analysis = tmp_path / "report.md", tmp_path / "analysis.json"
    monkeypatch.setattr(sys, "argv", ["engine.annual", "--data-dir", str(source),
                                     "--out", str(report), "--json", str(analysis)])
    main()
    assert json.loads(capsys.readouterr().out)["status"] == "retrospective_analysis_complete"
    observed = json.loads(analysis.read_text(encoding="utf-8"))
    assert {k: v for k, v in observed.items() if k != "source_sha256"} == {
        k: v for k, v in expected.items() if k != "source_sha256"}
    assert observed["source_sha256"] == {name: sha256(raw).hexdigest() for name, raw in before.items()}
    assert input_bytes(source) == before
    assert not report.read_bytes().startswith(BOM_UTF8)
    assert not analysis.read_bytes().startswith(BOM_UTF8)


@pytest.mark.parametrize("position", ["later-line", "repeated-leading"])
def test_jsonl_bom_is_only_a_single_file_leading_signature(source, position):
    path = source / "events.jsonl"
    raw = path.read_bytes()
    if position == "later-line":
        lines = raw.splitlines(keepends=True)
        raw = lines[0] + BOM_UTF8 + b"".join(lines[1:])
    else:
        raw = BOM_UTF8 + BOM_UTF8 + raw
    path.write_bytes(raw)
    with pytest.raises(json.JSONDecodeError):
        analyze_preserving_bytes(source)


def test_literal_feff_inside_json_jsonl_and_csv_values_is_preserved(source):
    for name, update in [
        ("manifest.json", lambda row: row.update(profile="synthetic\ufeffprofile")),
        ("building.json", lambda row: row["building"]["rooms"][0].update(orientation="N\ufeffvalue")),
    ]:
        path = source / name
        value = json.loads(path.read_text(encoding="utf-8"))
        update(value)
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    event = {"event_id": "SYN_LITERAL", "timestamp_utc": "2030-04-16T08:45:00Z", "type": "note\ufeffvalue"}
    with (source / "events.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(event, ensure_ascii=False) + "\n")
    write_csv(source, "staff_roster.csv", [{"role": "cleaner\ufeffvalue", "status": "scheduled"}])
    result = analyze_preserving_bytes(source)
    assert result["metadata"]["profile"] == "synthetic\ufeffprofile"
    assert "N\ufeffvalue" in result["temperature_by_orientation"]
    assert result["event_counts"]["note\ufeffvalue"] == 1
    assert result["staffing"]["cleaner\ufeffvalue:scheduled:shifts"] == 1
