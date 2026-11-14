"""Annual comparisons and joins use UTC instants without rewriting source data."""
import csv
from hashlib import sha256
import json
import socket

import pytest

from engine.annual import analyze_dataset

DAY = "2029-10-28"
ROOM = "R101"
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


def write_csv(root, name, rows):
    with (root / name).open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=FIELDS[name])
        writer.writeheader()
        writer.writerows(rows)


def write_events(root, rows):
    (root / "events.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def event(identifier, kind, timestamp):
    return {"event_id": identifier, "type": kind, "timestamp_utc": timestamp,
            "room_id": ROOM, "booking_id": "SYN_BOOK"}


def temperature(timestamp, value=21):
    return {"timestamp_utc": timestamp, "room_id": ROOM, "temperature_c": value, "window_open": "0"}


@pytest.fixture
def source(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Annual synthetic tests must not contact a model or network")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    (tmp_path / "building.json").write_text(json.dumps({"building": {"rooms": [{"id": ROOM}]},
        "constraints": {"occupied_comfort_c": [20, 23]}}), encoding="utf-8")
    (tmp_path / "manifest.json").write_text(json.dumps({"profile": "synthetic", "model_id": "ANNUAL_TIME_TEST",
        "days": 1, "start_utc": DAY + "T00:00:00Z", "end_utc": "2029-10-29T00:00:00Z"}), encoding="utf-8")
    write_events(tmp_path, [event("SYN_STAY", "stay_in_progress", DAY + "T11:00:00Z")])
    for name in FIELDS:
        write_csv(tmp_path, name, [])
    return tmp_path


def analyze_preserving_bytes(root):
    before = {path.name: path.read_bytes() for path in root.iterdir()}
    try:
        result = analyze_dataset(root)
    finally:
        assert {path.name: path.read_bytes() for path in root.iterdir()} == before
    assert result["source_sha256"] == {name: sha256(raw).hexdigest() for name, raw in before.items()}
    return result


@pytest.mark.parametrize("suffix", ["Z", ".000Z", "+00:00"])
def test_equal_instant_occupancy_ignores_timestamp_spelling(source, suffix):
    write_events(source, [event("SYN_IN", "checkin", DAY + "T12:00:00Z")])
    write_csv(source, "room_telemetry.csv", [temperature(DAY + "T12:00:00" + suffix)])
    result = analyze_preserving_bytes(source)
    assert result["temperature"].get("occupied_samples", 0) == 1
    assert result["temperature"].get("unknown_samples", 0) == 0


def test_fractional_event_chronology_and_turnaround_use_instants(source):
    write_events(source, [
        event("SYN_CLEAN", "cleaning_completed", DAY + "T12:00:01Z"),
        event("SYN_OUT", "checkout", DAY + "T12:00:00.500Z"),
        event("SYN_IN", "checkin", DAY + "T12:00:00Z"),
    ])
    write_csv(source, "room_telemetry.csv", [temperature(DAY + "T12:00:02Z")])
    result = analyze_preserving_bytes(source)
    assert result["temperature"].get("vacant_samples", 0) == 1
    assert result["temperature"].get("occupied_samples", 0) == 0
    assert result["checkout_cleaning_pairs"] == 1
    assert result["observed_checkout_to_cleaning_minutes_median"] == pytest.approx(0.5 / 60)


@pytest.mark.parametrize("first_suffix", ["Z", ".000Z"])
def test_equivalent_telemetry_duplicate_does_not_move_the_watermark(source, first_suffix):
    write_csv(source, "room_telemetry.csv", [temperature(DAY + "T12:00:00" + first_suffix),
        temperature(DAY + "T12:00:00Z"), temperature(DAY + "T11:59:00Z")])
    result = analyze_preserving_bytes(source)
    assert result["temperature"]["samples"] == 1
    assert result["telemetry_duplicates_skipped"] == 1
    assert result["telemetry_out_of_order_skipped"] == 1


@pytest.mark.parametrize("main_suffix", ["Z", ".000Z", "+00:00"])
def test_room_main_water_join_uses_equal_instants(source, main_suffix):
    write_csv(source, "room_water.csv", [
        {"timestamp_utc": DAY + "T00:00:00Z", "room_id": ROOM, "meter_total_l": 100},
        {"timestamp_utc": DAY + "T00:15:00Z", "room_id": ROOM, "meter_total_l": 110},
    ])
    write_csv(source, "water_main.csv", [
        {"timestamp_utc": DAY + "T00:00:00" + main_suffix, "meter_total_l": 500},
        {"timestamp_utc": DAY + "T00:15:00" + main_suffix, "meter_total_l": 513},
    ])
    result = analyze_preserving_bytes(source)
    assert result["water"]["valid_room_intervals"] == result["water"]["valid_main_intervals"] == 1
    assert result["water"]["complete_main_room_intervals"] == 1
    assert result["water"]["complete_interval_residual_liters"] == 3


def test_actual_water_gaps_and_unknown_occupancy_stay_unknown(source):
    write_events(source, [])
    write_csv(source, "room_telemetry.csv", [temperature(DAY + "T12:00:00.000Z", 19)])
    write_csv(source, "room_water.csv", [
        {"timestamp_utc": DAY + "T00:00:00Z", "room_id": ROOM, "meter_total_l": 100},
        {"timestamp_utc": DAY + "T00:30:00Z", "room_id": ROOM, "meter_total_l": 110},
    ])
    write_csv(source, "water_main.csv", [
        {"timestamp_utc": DAY + "T00:00:00Z", "meter_total_l": 500},
        {"timestamp_utc": DAY + "T00:30:00Z", "meter_total_l": 513},
    ])
    result = analyze_preserving_bytes(source)
    assert result["temperature"]["unknown_samples"] == 1
    assert result["temperature"].get("cold_samples", 0) == 0
    assert result["water"]["noncontiguous_room_intervals"] == 1
    assert result["water"].get("valid_room_intervals", 0) == result["water"].get("valid_main_intervals", 0) == 0
    assert result["water"]["complete_main_room_intervals"] == 0


@pytest.mark.parametrize("tariff_suffix", ["Z", ".000Z", "+00:00"])
def test_tariffs_keep_distinct_utc_fold_hours_and_missing_coverage(source, tariff_suffix):
    # 00:15Z and 01:15Z are two distinct UTC instants in Vienna's repeated hour.
    write_csv(source, "energy.csv", [{"timestamp_utc": DAY + f"T{hour:02d}:15:00+00:00",
        "building_load_kw": 4, "hvac_electric_kw": 1, "pv_kw": 0, "battery_discharge_kw_signed": 0,
        "battery_soc_kwh": 5, "grid_import_kw": 4, "grid_export_kw": 0} for hour in [0, 1, 2]])
    write_csv(source, "tariff.csv", [{"timestamp_utc": DAY + f"T{hour:02d}:00:00" + tariff_suffix,
        "price_eur_kwh": price} for hour, price in [(0, 0.1), (1, 0.4)]])
    result = analyze_preserving_bytes(source)
    assert result["energy"]["valid_samples"] == 3
    assert result["energy"].get("cost_covered_samples", 0) == 2
    assert result["energy"].get("grid_import_cost_eur_observed", 0) == pytest.approx(0.5)


@pytest.mark.parametrize("stream,timestamp", [
    ("event", DAY + "T13:00:00+01:00"),
    ("telemetry", "2029-10-32T12:00:00Z"),
])
def test_relevant_invalid_or_non_utc_timestamps_fail_explicitly(source, stream, timestamp):
    if stream == "event":
        write_events(source, [event("SYN_BAD_TIME", "checkin", timestamp)])
        write_csv(source, "room_telemetry.csv", [temperature(DAY + "T12:00:00Z")])
    else:
        write_csv(source, "room_telemetry.csv", [temperature(timestamp)])
    with pytest.raises(ValueError):
        analyze_preserving_bytes(source)
