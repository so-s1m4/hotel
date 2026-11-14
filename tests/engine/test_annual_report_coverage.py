"""Report missing support honestly while retaining real measured zero values."""
from copy import deepcopy
import json
import re

import pytest

from engine.annual import render_report


def analysis():
    return {
        "metadata": {"profile": "synthetic_report", "days": 1,
                     "start_utc": "2030-03-01T00:00:00Z", "end_utc": "2030-03-02T00:00:00Z"},
        "temperature": {}, "energy": {},
        "water": {"complete_main_room_intervals": 0, "complete_interval_residual_liters": 0,
                  "median_complete_interval_residual_liters": None},
        "temperature_by_orientation": {"North": {}, "South": {}},
        "telemetry_duplicates_skipped": 0, "telemetry_out_of_order_skipped": 0,
        "checkout_cleaning_pairs": 0, "observed_checkout_to_cleaning_minutes_median": None,
        "logistics": {}, "staffing": {}, "source_sha256": {"synthetic.csv": "1" * 64},
    }


def render_unchanged(a):
    before = deepcopy(a)
    serialized = json.dumps(a, sort_keys=True)
    report = render_report(a)
    assert a == before
    assert json.dumps(a, sort_keys=True) == serialized
    return report


def line(report, prefix):
    return next(text for text in report.splitlines() if text.startswith(prefix))


def covered_zero():
    a = analysis()
    a["temperature"] = {"samples": 2, "occupied_samples": 2, "cold_samples": 0, "hot_samples": 0}
    a["temperature_by_orientation"] = {name: {"samples": 1, "occupied_samples": 1, "cold_samples": 0}
                                       for name in ["North", "South"]}
    a["water"] = {"complete_main_room_intervals": 1, "complete_interval_residual_liters": 0,
                  "median_complete_interval_residual_liters": 0}
    a["energy"] = {"samples": 1, "valid_samples": 1, "building_load_kwh": 0,
                   "hvac_electric_kwh": 0, "pv_kwh": 0, "cost_covered_samples": 1,
                   "grid_import_cost_eur_observed": 0}
    a["checkout_cleaning_pairs"] = 1
    a["observed_checkout_to_cleaning_minutes_median"] = 0.0
    return a


@pytest.mark.parametrize("missing_rows", [False, True], ids=["empty", "missing-only"])
def test_unobserved_metrics_are_unavailable_not_zero(missing_rows):
    a = analysis()
    if missing_rows:
        a["temperature"] = {"samples": 1, "missing_temperature": 1}
        a["energy"] = {"samples": 1, "missing_samples": 1}
        a["water"]["missing_or_invalid_room_samples"] = 1
    report = render_unchanged(a)
    for prefix in ["- Comfort:", "- Water:", "- Energy:", "- Actual checkout"]:
        assert "unavailable" in line(report, prefix).lower()
    assert not re.search(r"\d[\d,.]*%", line(report, "- Comfort:"))
    assert "0.00 L/15 min" not in line(report, "- Water:")
    assert "0.0 kWh" not in line(report, "- Energy:")
    assert "0.00 EUR" not in line(report, "- Energy:")
    assert "None" not in report
    for name in ["North", "South"]:
        orientation = line(report, f"| {name} |")
        assert "unavailable" in orientation.lower() and "%" not in orientation
    assert "show measurable unallocated consumption" not in report
    assert "demonstrated attribution gap" not in report
    if not missing_rows:
        assert "demonstrate observability gaps" not in report


def test_zero_measurements_with_positive_support_remain_numeric():
    report = render_unchanged(covered_zero())
    assert "0.00%" in line(report, "- Comfort:")
    assert "0.00 L/15 min" in line(report, "- Water:")
    assert "building load 0.0 kWh" in line(report, "- Energy:")
    assert "import cost 0.00 EUR" in line(report, "- Energy:")
    assert re.search(r"median: 0(?:\.0+)? minutes", line(report, "- Actual checkout"))
    for name in ["North", "South"]:
        assert "0.00%" in line(report, f"| {name} |")
    for prefix in ["- Comfort:", "- Water:", "- Energy:", "- Actual checkout"]:
        assert "unavailable" not in line(report, prefix).lower()
    assert "show measurable unallocated consumption" not in report
    assert "demonstrated attribution gap" not in report
    assert "demonstrate observability gaps" not in report


def test_partial_energy_and_price_coverage_are_disclosed_without_imputation():
    a = covered_zero()
    a["energy"].update(samples=3, valid_samples=2, missing_samples=1,
                       building_load_kwh=3, hvac_electric_kwh=1, pv_kwh=0.5,
                       cost_covered_samples=1, grid_import_cost_eur_observed=0.2)
    report = render_unchanged(a)
    energy = line(report, "- Energy:")
    assert "2 of 3" in energy
    assert "1 of 2" in energy
    assert "covered subtotal" in energy.lower()
    assert "building load 3.0 kWh" in energy and "HVAC 1.0 kWh" in energy and "PV 0.5 kWh" in energy
    assert "import cost 0.20 EUR" in energy


def test_valid_energy_without_prices_does_not_acquire_a_zero_cost():
    a = covered_zero()
    a["energy"].update(building_load_kwh=1, cost_covered_samples=0)
    del a["energy"]["grid_import_cost_eur_observed"]
    report = render_unchanged(a)
    energy = line(report, "- Energy:")
    assert "building load 1.0 kWh" in energy
    assert "import cost unavailable" in energy.lower()
    assert "0 of 1" in energy and "0.00 EUR" not in energy


def test_orientation_rates_use_occupied_coverage_of_each_orientation():
    a = covered_zero()
    a["temperature"] = {"samples": 2, "occupied_samples": 1, "cold_samples": 1, "unknown_samples": 1}
    a["temperature_by_orientation"] = {
        "North": {"samples": 1, "occupied_samples": 1, "cold_samples": 1},
        "South": {"samples": 1, "unknown_samples": 1},
    }
    report = render_unchanged(a)
    assert "100.00%" in line(report, "- Comfort:")
    assert "100.00%" in line(report, "| North |")
    south = line(report, "| South |")
    assert "unavailable" in south.lower() and "%" not in south


@pytest.mark.parametrize("residual", [-3.0, 5.0], ids=["negative", "positive"])
def test_supported_signed_residual_and_other_nonzero_findings_are_retained(residual):
    a = covered_zero()
    a["water"].update(complete_interval_residual_liters=residual,
                      median_complete_interval_residual_liters=residual)
    a["temperature"].update(samples=3, missing_temperature=1, cold_samples=1)
    a["temperature_by_orientation"]["North"]["cold_samples"] = 1
    a["observed_checkout_to_cleaning_minutes_median"] = 20.0
    report = render_unchanged(a)
    assert f"{residual:.2f} L/15 min" in line(report, "- Water:")
    assert "50.00%" in line(report, "- Comfort:")
    assert re.search(r"median: 20(?:\.0+)? minutes", line(report, "- Actual checkout"))
    assert "1 missing temperature" in report
    assert "**not** measured leakage" in report
    assert "not real-world savings" in report
    if residual < 0:
        assert "show measurable unallocated consumption" not in report
