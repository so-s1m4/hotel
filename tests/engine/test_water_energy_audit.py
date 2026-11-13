"""Independent adversarial regressions for detector reasoning, not shape-only tests."""
from copy import deepcopy
from datetime import timedelta

import pytest

from engine.core import EngineConfig, instant, utc
from engine.detectors.water import WaterDetector
from engine.detectors.energy import EnergyDetector
from engine.detectors.optimization import OptimizationDetector
from tests.engine.fixtures import BUILDING, FakeGateway, state, tick, water_repo, energy_repo
from tests.engine.test_extended import battery_repo, full_building


def test_missing_new_weather_cannot_make_hour_old_weather_fresh_for_hp_service():
    repo = energy_repo()
    for row in repo.measurements["weather.csv"][1:]:
        row["outdoor_c"] = None
    candidates = EnergyDetector(EngineConfig()).detect(tick(), repo, BUILDING).candidates
    plant = next(c for c in candidates if c.issue == "heating_performance")
    assert plant.action == "flag_anomaly"
    assert "uncertain" in plant.reason


def test_unknown_windows_are_not_closed_windows_for_heating_or_plant_diagnosis():
    repo = energy_repo()
    for row in repo.measurements["room_telemetry.csv"]:
        row["window_open"] = None
    candidates = EnergyDetector(EngineConfig()).detect(tick(), repo, BUILDING).candidates
    assert not any(c.action in {"adjust_room_setpoint", "dispatch_technician"} for c in candidates)
    assert any("unknown" in c.reason.lower() or "incomplete" in c.reason.lower() for c in candidates)


def test_checkout_and_checkin_between_samples_do_not_prove_continuous_occupied_cold():
    repo = energy_repo()
    repo.events = [
        {"timestamp_utc": "2026-01-06T08:37:00Z", "type": "checkout", "room_id": "R101"},
        {"timestamp_utc": "2026-01-06T08:42:00Z", "type": "checkin", "room_id": "R101"},
    ]
    candidates = EnergyDetector(EngineConfig()).detect(tick(), repo, BUILDING).candidates
    assert not any(c.issue == "heating_comfort" for c in candidates)


def test_water_room_reassignment_without_room_id_invalidates_vacancy_window():
    repo = water_repo()
    repo.events = [{"timestamp_utc": "2026-01-06T01:20:00Z", "type": "room_reassigned", "from_room": "R101", "to_room": "R102"}]
    candidates = WaterDetector(EngineConfig()).detect(tick("2026-01-06T02:00:00Z"), repo, BUILDING, []).candidates
    assert not candidates


def test_water_stale_clean_state_is_not_proof_of_cleanliness_during_flow_window():
    repo = water_repo()
    repo.states["R101"] = [state(t="2026-01-05T20:00:00Z", cleaning="clean")]
    candidates = WaterDetector(EngineConfig()).detect(tick("2026-01-06T02:00:00Z"), repo, BUILDING, []).candidates
    assert not candidates


def test_water_recovery_windows_must_be_consecutive_not_separated_by_missing_data():
    detector = WaterDetector(EngineConfig())
    repo = water_repo(flow=0)
    first = "2026-01-06T02:00:00Z"
    assert not detector.detect(tick(first), repo, BUILDING, []).resolved
    # A later valid window after a measurement outage must restart recovery count.
    second_repo = water_repo(now="2026-01-06T05:00:00Z", flow=0)
    assert not detector.detect(tick("2026-01-06T05:00:00Z"), second_repo, BUILDING, []).resolved
    third_repo = water_repo(now="2026-01-06T05:15:00Z", flow=0)
    assert "water:R101" not in detector.detect(tick("2026-01-06T05:15:00Z"), third_repo, BUILDING, []).resolved


def test_water_invalid_window_breaks_recovery_even_when_ticks_are_contiguous():
    detector = WaterDetector(EngineConfig())
    detector.detect(tick("2026-01-06T02:00:00Z"), water_repo(flow=0), BUILDING, [])
    detector.detect(tick("2026-01-06T02:15:00Z"), water_repo(now="2026-01-06T02:15:00Z", flow=0), BUILDING, [])
    broken = water_repo(now="2026-01-06T02:30:00Z", flow=0)
    broken.measurements["room_water.csv"][2]["meter_total_l"] = None
    detector.detect(tick("2026-01-06T02:30:00Z"), broken, BUILDING, [])
    final = detector.detect(tick("2026-01-06T02:45:00Z"), water_repo(now="2026-01-06T02:45:00Z", flow=0), BUILDING, [])
    assert "water:R101" not in final.resolved


def preheat_repo(**changes):
    repo = battery_repo()
    repo.forecasts = [{"issued_utc": "2026-01-06T05:00:00Z", "target_utc": "2026-01-06T10:00:00Z", "outdoor_c_forecast": -5}]
    repo.bookings = [{"booking_id": "B1", "room_id": "R101", "created_utc": "2026-01-05T00:00:00Z", "arrival_utc": "2026-01-06T10:00:00Z", "departure_utc": "2026-01-07T10:00:00Z", **changes}]
    repo.states["R101"] = [state(temperature_c=18, setpoint_c=18, window_open=0)]
    return repo


@pytest.mark.parametrize("booking_changes", [{"observed_state": "checked_out"}, {"observed_state": "no_show"}, {"observed_room_id": "R102"}])
def test_preheat_does_not_use_booking_known_departed_or_reassigned_elsewhere(booking_changes):
    repo = preheat_repo(**booking_changes)
    candidates = OptimizationDetector(EngineConfig()).detect(tick(), repo, full_building()).candidates
    assert not any(c.action == "adjust_room_setpoint" for c in candidates)


@pytest.mark.parametrize("temperature", [None, 21, 28, 999])
def test_vacant_preheat_requires_known_valid_temperature_below_target(temperature):
    repo = preheat_repo()
    repo.states["R101"][0]["temperature_c"] = temperature
    candidates = OptimizationDetector(EngineConfig()).detect(tick(), repo, full_building()).candidates
    assert not any(c.action == "adjust_room_setpoint" for c in candidates)


def test_known_cold_vacant_room_with_real_near_arrival_still_can_preheat():
    candidates = OptimizationDetector(EngineConfig()).detect(tick(), preheat_repo(), full_building()).candidates
    assert any(c.action == "adjust_room_setpoint" for c in candidates)


def test_healthy_energy_windows_separated_by_missing_data_do_not_close_episode():
    detector = EnergyDetector(EngineConfig())
    detector.detect(tick(), energy_repo(temperature=21), BUILDING)
    detector.detect(tick("2026-01-06T09:15:00Z"), energy_repo(now="2026-01-06T09:15:00Z", temperature=21), BUILDING)
    broken = energy_repo(now="2026-01-06T09:30:00Z", temperature=21)
    broken.measurements["room_telemetry.csv"][-1]["temperature_c"] = None
    detector.detect(tick("2026-01-06T09:30:00Z"), broken, BUILDING)
    result = detector.detect(tick("2026-01-06T09:45:00Z"), energy_repo(now="2026-01-06T09:45:00Z", temperature=21), BUILDING)
    assert "comfort:R101" not in result.resolved


def test_a_future_scheduled_flush_does_not_explain_current_water_flow():
    repo = water_repo()
    orders = [{"created_utc": "2026-01-05T12:00:00Z", "scheduled_utc": "2026-01-06T03:00:00Z", "asset_id": "R101", "summary": "Planned tap flushing"}]
    result = WaterDetector(EngineConfig()).detect(tick("2026-01-06T02:00:00Z"), repo, BUILDING, orders)
    assert any(c.issue == "water_leak" for c in result.candidates)


@pytest.mark.parametrize("window", [None, 1])
def test_preheat_does_not_assume_unknown_or_open_window_is_closed(window):
    repo = preheat_repo()
    repo.states["R101"][0]["window_open"] = window
    candidates = OptimizationDetector(EngineConfig()).detect(tick(), repo, full_building()).candidates
    assert not any(c.action == "adjust_room_setpoint" for c in candidates)


@pytest.mark.parametrize("new_value", [None, 999])
def test_invalid_latest_forecast_does_not_resurrect_old_cold_preheat_forecast(new_value):
    repo = preheat_repo()
    repo.forecasts.append({"issued_utc": "2026-01-06T08:00:00Z", "target_utc": "2026-01-06T10:00:00Z", "outdoor_c_forecast": new_value})
    candidates = OptimizationDetector(EngineConfig()).detect(tick(), repo, full_building()).candidates
    assert not any(c.action == "adjust_room_setpoint" for c in candidates)


def test_missing_latest_published_price_does_not_resurrect_old_cheaper_price():
    repo = battery_repo()
    repo.tariffs.append({"timestamp_utc": "2026-01-06T09:00:00Z", "published_utc": "2026-01-06T08:00:00Z", "price_eur_kwh": None})
    candidates = OptimizationDetector(EngineConfig()).detect(tick(), repo, full_building()).candidates
    assert not candidates


def test_comfort_boundary_noise_does_not_rearm_until_recovery_margin_is_sustained():
    detector = EnergyDetector(EngineConfig())
    now = "2026-01-06T09:00:00Z"
    first = detector.detect(tick(now), energy_repo(now=now, temperature=19.9), BUILDING)
    assert any(c.issue == "heating_comfort" for c in first.candidates)
    for minutes in (15, 30, 45, 60):
        at = utc(instant(now)+timedelta(minutes=minutes))
        result = detector.detect(tick(at), energy_repo(now=at, temperature=20.05), BUILDING)
        assert "comfort:R101" not in result.resolved
    for minutes in (75, 90):
        at = utc(instant(now)+timedelta(minutes=minutes))
        result = detector.detect(tick(at), energy_repo(now=at, temperature=20.5), BUILDING)
        assert "comfort:R101" not in result.resolved
    at = utc(instant(now)+timedelta(minutes=105))
    result = detector.detect(tick(at), energy_repo(now=at, temperature=20.5), BUILDING)
    assert "comfort:R101" in result.resolved


def low_rate_nights(previous=None, current=None, occupancy="vacant"):
    """Two separate nights; source facts are exposed only through as-of getters."""
    repo = FakeGateway()
    visits = []
    reading = 100.0
    nights = []
    if previous is not None:
        nights.append(("2026-01-05T23:00:00Z", previous))
    nights.append(("2026-01-06T23:00:00Z", current if current is not None else [1.1]*12))
    for start, deltas in nights:
        points = [(start, reading)]
        for i, delta in enumerate(deltas, 1):
            at = utc(instant(start)+timedelta(minutes=15*i))
            if delta is not None:
                reading += delta
            points.append((at, reading if delta is not None else None))
            visits.append(at)
        for at, value in points:
            repo.measurements.setdefault("room_water.csv", []).append({"timestamp_utc": at, "room_id": "R101", "meter_total_l": value})
            repo.states.setdefault("R101", []).append(state(t=at, occupancy=occupancy, cleaning="clean"))
    return repo, visits


def run_low_rate(repo, visits):
    detector = WaterDetector(EngineConfig())
    findings = []
    for at in visits:
        findings += detector.detect(tick(at), repo, BUILDING, []).candidates
    return detector, findings


def test_low_rate_review_requires_long_duration_and_independent_quiet_earlier_night():
    repo, visits = low_rate_nights(previous=[0.0]*12)
    detector, findings = run_low_rate(repo, visits)
    reviews = [c for c in findings if c.issue == "water_low_rate_review"]
    assert len(reviews) == 1
    assert reviews[0].action == "flag_anomaly"
    assert "180 min" in reviews[0].reason and "uncertain" in reviews[0].reason
    assert any("earlier_night_baseline" in ref for ref in reviews[0].evidence)
    assert not any(c.action == "request_inspection" for c in findings)
    assert detector.diagnostics["low_rate_review_windows"] == 1


def test_low_rate_without_prior_night_baseline_is_diagnosed_but_not_invented_as_leak():
    repo, visits = low_rate_nights()
    detector, findings = run_low_rate(repo, visits)
    assert not findings
    assert detector.diagnostics["low_rate_baseline_unavailable_windows"] == 1


@pytest.mark.parametrize("current", [[0.0]*12, [1.0, 2.0]*6, [1.1]*5+[None]+[1.1]*6, [1.1]*11])
def test_normal_variable_incomplete_or_too_short_flow_does_not_trigger_low_rate_review(current):
    repo, visits = low_rate_nights(previous=[0.0]*12, current=current)
    _, findings = run_low_rate(repo, visits)
    assert not any(c.issue == "water_low_rate_review" for c in findings)


def test_low_rate_not_flagged_if_earlier_nights_already_have_nonquiet_background():
    repo, visits = low_rate_nights(previous=[0.0, 0.8]*6)
    detector, findings = run_low_rate(repo, visits)
    assert not any(c.issue == "water_low_rate_review" for c in findings)
    assert detector.diagnostics["low_rate_baseline_not_quiet_windows"] == 1


def test_low_rate_occupied_room_or_known_cleaning_is_not_a_vacant_flow_review():
    repo, visits = low_rate_nights(previous=[0.0]*12, occupancy="occupied")
    _, findings = run_low_rate(repo, visits)
    assert not any(c.issue == "water_low_rate_review" for c in findings)
    repo, visits = low_rate_nights(previous=[0.0]*12)
    repo.events = [{"timestamp_utc": "2026-01-07T00:00:00Z", "type": "cleaning_started", "room_id": "R101"}]
    _, findings = run_low_rate(repo, visits)
    assert not any(c.issue == "water_low_rate_review" for c in findings)


def test_water_main_completeness_exclusions_are_exportable_without_zero_filling():
    repo = water_repo(flow=0)
    # Main itself is valid. Missing R102 prevents the residual path before thresholds.
    repo.measurements["water_main.csv"] = [{"timestamp_utc": r["timestamp_utc"], "meter_total_l": 1000+i*10} for i, r in enumerate(repo.measurements["room_water.csv"])]
    detector = WaterDetector(EngineConfig())
    result = detector.detect(tick("2026-01-06T02:00:00Z"), repo, BUILDING, [])
    assert not any(c.issue == "water_balance" for c in result.candidates)
    assert detector.diagnostics["main_windows_evaluated"] == 1
    assert detector.diagnostics["main_incomplete_room_windows"] == 1
    assert detector.diagnostics["main_blocked_room:R102"] == 1
    assert detector.diagnostics["main_complete_windows"] == 0
    assert detector.diagnostics["main_invalid_or_missing_windows"] == 0
