"""Current matched intervals must not depend on unrelated invalid older pairs."""
from engine.core import EngineConfig
from engine.detectors.water import WaterDetector
from tests.engine.fixtures import BUILDING, water_repo, tick


def complete_source():
    repo = water_repo(flow=0)
    room = repo.measurements["room_water.csv"]
    room += [{**r, "room_id": "R102"} for r in list(room)]
    repo.measurements["water_main.csv"] = [
        {"timestamp_utc": r["timestamp_utc"], "meter_total_l": 1000+i*10}
        for i, r in enumerate(room[:5])]
    return repo


def test_older_room_reset_does_not_discard_current_complete_interval():
    repo = complete_source()
    repo.measurements["room_water.csv"][1]["meter_total_l"] = 1
    detector = WaterDetector(EngineConfig())
    result = detector.detect(tick("2026-01-06T02:00:00Z"), repo, BUILDING, [])
    assert not result.candidates
    assert detector.diagnostics["main_complete_windows"] == 0
    assert detector.diagnostics["main_complete_intervals"] == 1
    assert len(detector.residual_history) == 1
    assert detector.residual_history[-1][1] == 10


def test_older_main_gap_does_not_discard_current_complete_interval():
    repo = complete_source()
    repo.measurements["water_main.csv"][1]["meter_total_l"] = None
    detector = WaterDetector(EngineConfig())
    detector.detect(tick("2026-01-06T02:00:00Z"), repo, BUILDING, [])
    assert detector.diagnostics["main_complete_intervals"] == 1
    assert detector.diagnostics["main_complete_windows"] == 0


def test_invalid_latest_room_pair_never_becomes_zero_filled_residual():
    for value in [None, 1]:
        repo = complete_source()
        repo.measurements["room_water.csv"][4]["meter_total_l"] = value
        detector = WaterDetector(EngineConfig())
        result = detector.detect(tick("2026-01-06T02:00:00Z"), repo, BUILDING, [])
        assert not result.candidates
        assert not detector.residual_history
        assert detector.diagnostics["main_incomplete_room_intervals"] == 1


def test_missing_current_pair_breaks_residual_persistence_not_previous_valid_history():
    detector = WaterDetector(EngineConfig())
    repo = complete_source()
    detector.detect(tick("2026-01-06T02:00:00Z"), repo, BUILDING, [])
    broken = complete_source()
    detector.detect(tick("2026-01-06T02:15:00Z"), broken, BUILDING, [])
    assert len(detector.residual_history) == 1
    assert not detector.residual_run
