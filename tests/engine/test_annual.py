from copy import deepcopy
import csv
import json

from engine.annual import analyze_dataset, render_report
from tests.engine.fixtures import BUILDING


def write_csv(root, name, fields, entries):
    with (root/name).open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(entries)


def test_annual_analysis_computes_observed_metrics_without_inventing_savings(tmp_path):
    building = deepcopy(BUILDING)
    for room, orientation in zip(building["building"]["rooms"], ["N", "S"]):
        room["orientation"] = orientation
    (tmp_path/"building.json").write_text(json.dumps(building))
    (tmp_path/"manifest.json").write_text(json.dumps({"profile": "test", "model_id": "PLATO-40", "days": 1,
        "start_utc": "2026-01-01T00:00:00Z", "end_utc": "2026-01-02T00:00:00Z"}))
    times = ["2026-01-01T00:00:00Z", "2026-01-01T00:15:00Z", "2026-01-01T00:30:00Z"]
    events = [{"event_id": "E1", "type": "stay_in_progress", "timestamp_utc": times[0], "room_id": "R101", "booking_id": "B1"},
              {"event_id": "E2", "type": "checkout", "timestamp_utc": times[0], "room_id": "R102", "booking_id": "B2"},
              # A not-yet-checked-in booking reassignment cannot mark R101 vacant.
              {"event_id": "E3", "type": "room_reassigned", "timestamp_utc": times[1], "from_room": "R101", "to_room": "R102", "booking_id": "B3"}]
    (tmp_path/"events.jsonl").write_text("".join(json.dumps(e)+"\n" for e in events))
    temp = [{"timestamp_utc": t, "room_id": "R101", "temperature_c": v, "window_open": "0"} for t, v in zip(times, [19, 21, ""])]
    temp.insert(1, dict(temp[0]))
    write_csv(tmp_path, "room_telemetry.csv", list(temp[0]), temp)
    water = [{"timestamp_utc": t, "room_id": room, "meter_total_l": value} for room, values in [("R101", [100, 110, 1]), ("R102", [100, 110, ""])] for t, value in zip(times, values)]
    write_csv(tmp_path, "room_water.csv", list(water[0]), water)
    write_csv(tmp_path, "water_main.csv", ["timestamp_utc", "meter_total_l"], [{"timestamp_utc": t, "meter_total_l": v} for t, v in zip(times, [200, 225, 245])])
    energy = [{"timestamp_utc": t, "building_load_kw": 20, "hvac_electric_kw": 10, "pv_kw": 5,
               "battery_discharge_kw_signed": 0, "battery_soc_kwh": 50, "grid_import_kw": 15, "grid_export_kw": 0} for t in times[:2]]
    write_csv(tmp_path, "energy.csv", list(energy[0]), energy)
    write_csv(tmp_path, "tariff.csv", ["timestamp_utc", "price_eur_kwh"], [{"timestamp_utc": times[0], "price_eur_kwh": 0.2}])
    write_csv(tmp_path, "equipment.csv", ["timestamp_utc", "asset_id", "metric", "value"], [])
    write_csv(tmp_path, "logistics.csv", ["timestamp_utc", "asset_id", "metric", "value"], [])
    write_csv(tmp_path, "staff_roster.csv", ["role", "status"], [])
    a = analyze_dataset(tmp_path)
    assert a["retrospective_only"] is True
    assert a["temperature"]["occupied_samples"] == 2
    assert a["temperature"]["cold_samples"] == 1
    assert a["temperature"]["missing_temperature"] == 1
    assert a["telemetry_duplicates_skipped"] == 1
    assert a["water"]["room_negative_deltas"] == 1
    assert a["water"]["valid_room_interval_liters"] == 20
    assert a["water"]["complete_main_room_intervals"] == 1
    assert a["water"]["median_complete_interval_residual_liters"] == 5
    assert a["energy"]["building_load_kwh"] == 10
    assert a["energy"]["grid_import_cost_eur_observed"] == 1.5
    report = render_report(a)
    assert "CLARA" in report and "PLATO" in report
    assert "**not** measured leakage" in report
    assert "not real-world savings" in report
    assert len(a["source_sha256"]["room_water.csv"]) == 64
