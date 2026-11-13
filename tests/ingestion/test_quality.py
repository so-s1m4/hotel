"""Data-quality report: deterministic, offline, with source references."""
from __future__ import annotations

import json

import pytest

from ingestion.quality import build_report, main
from tests.ingestion.test_replay import DEV, QS


def test_quickstart_quality_report(tmp_path):
    if not QS.is_dir():
        pytest.skip("Quickstart data not present")
    report = build_report(QS)
    tel = report["room_telemetry"]
    assert tel["duplicates_dropped"] == 2 and tel["missing_rows"] == 0
    assert tel["rows_after_dedup"] == tel["expected_rows"] == 80640
    # floor-3 gateway outages: all ten rooms lose their sensors at the same instant
    assert tel["floor_wide_outages"]["count"] == 2
    assert all(o["floor"] == 3 and o["rooms_affected"] == 10
               for o in tel["floor_wide_outages"]["examples"])
    assert tel["implausible_values"]["examples"][0]["value"] == 85.0
    water = report["water"]["room_water.csv"]
    top = water["largest_decreases"][0]
    assert top["asset_id"] == "WM_R403" and top["metering_work_orders"] == ["WO00004"]
    assert water["large_unexplained_decreases"] == []
    assert water["decreases_matched_to_metering_work_order"] == 1   # one per work order
    assert water["small_unexplained_decreases"] > 300      # meter jitter, not resets
    assert "never use at decision time" in report["note"]
    out = tmp_path / "q.json"
    assert main(["--data-dir", str(QS), "--out", str(out)]) == 0
    assert json.loads(out.read_text())["room_telemetry"] == json.loads(json.dumps(tel))


def test_dev_quality_report_is_deterministic():
    if not DEV.is_dir():
        pytest.skip("Dev data not present")
    first, second = build_report(DEV), build_report(DEV)
    assert first == second
    assert first["room_telemetry"]["duplicates_dropped"] == 40
    assert first["room_telemetry"]["floor_wide_outages"]["count"] == 14
    assert first["water"]["room_water.csv"]["decreases_matched_to_metering_work_order"] == 2


def test_quality_counts_declared_rooms_and_one_match_per_work_order(tmp_path):
    d = tmp_path / "q"
    d.mkdir()
    (d / "manifest.json").write_text(json.dumps({
        "start_utc": "2026-01-05T00:00:00Z", "end_utc": "2026-01-05T01:00:00Z",
        "interval_seconds": 900}))
    (d / "building.json").write_text(json.dumps(
        {"building": {"rooms": [{"id": "R101"}, {"id": "R102"}]}}))
    (d / "room_telemetry.csv").write_text(
        "timestamp_utc,room_id,temperature_c,humidity_pct,co2_ppm\n" + "".join(
            f"2026-01-05T00:{m:02d}:00Z,R101,20.0,40,500\n" for m in (0, 15, 30, 45)))
    (d / "room_water.csv").write_text(
        "timestamp_utc,room_id,meter_total_l\n" + "".join(
            f"2026-01-05T00:{m:02d}:00Z,R101,{v}\n" for m, v in ((0, 100), (15, 99), (30, 10), (45, 9))))
    (d / "water_main.csv").write_text("timestamp_utc,meter_total_l\n")
    (d / "work_orders.csv").write_text(
        "work_order_id,created_utc,asset_id,type,status,scheduled_utc,completed_utc,assigned_to,summary\n"
        "WO1,2026-01-04T00:00:00Z,WM_R101,metering,completed,2026-01-05T00:30:00Z,"
        "2026-01-05T00:30:00Z,EXT,exchange\n")
    report = build_report(d)
    tel = report["room_telemetry"]
    assert tel["expected_rows"] == 8 and tel["missing_rows"] == 4
    assert tel["rooms_without_any_rows"] == ["R102"]
    water = report["water"]["room_water.csv"]
    assert water["counter_decreases"] == 3
    assert water["decreases_matched_to_metering_work_order"] == 1
    assert water["largest_decreases"][0]["drop_l"] == 89.0
    assert water["largest_decreases"][0]["metering_work_orders"] == ["WO1"]
    assert water["small_unexplained_decreases"] == 2


def test_quality_missing_slots_ignore_rows_outside_the_grid(tmp_path):
    d = tmp_path / "grid"
    d.mkdir()
    (d / "manifest.json").write_text(json.dumps({
        "start_utc": "2026-01-05T00:00:00Z", "end_utc": "2026-01-05T01:00:00Z",
        "interval_seconds": 900}))
    (d / "building.json").write_text(json.dumps({"building": {"rooms": [{"id": "R101"}]}}))
    (d / "room_telemetry.csv").write_text(
        "timestamp_utc,room_id,temperature_c,humidity_pct,co2_ppm\n" + "".join(
            f"2026-01-05T{t}:00Z,R101,20.0,40,500\n" for t in ("00:00", "00:15", "01:00", "01:15")))
    (d / "room_water.csv").write_text("timestamp_utc,room_id,meter_total_l\n")
    (d / "water_main.csv").write_text("timestamp_utc,meter_total_l\n")
    tel = build_report(d)["room_telemetry"]
    assert tel["expected_rows"] == 4 and tel["missing_rows"] == 2
    assert tel["rows_outside_manifest_grid"] == 2
