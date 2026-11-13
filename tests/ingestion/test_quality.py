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
    assert first["water"]["room_water.csv"]["decreases_explained_by_metering_work_order"] == 25
