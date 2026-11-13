"""Real-file regressions for once-per-booking-and-room preheat proposals."""
from copy import deepcopy
import csv
import json

import pytest

from engine.core import instant
from engine.planner import DecisionEngine
from ingestion.replay import ReplayData
from tests.engine.fixtures import BUILDING


START = "2026-01-06T09:00:00Z"
MOVED = "2026-01-06T09:30:00Z"
LATER = "2026-01-06T10:00:00Z"
ARRIVAL = "2026-01-06T11:00:00Z"
CREATED = "2026-01-05T12:00:00Z"
HEADERS = {
    "bookings.csv": "booking_id,room_id,created_utc,arrival_utc,departure_utc,guest_count,channel,status",
    "access_log.csv": "timestamp_utc,door_id,credential,result",
    "room_telemetry.csv": "timestamp_utc,room_id,temperature_c,setpoint_c,humidity_pct,co2_ppm,presence,window_open,housekeeping_state",
    "room_water.csv": "timestamp_utc,room_id,meter_total_l",
    "water_main.csv": "timestamp_utc,meter_total_l",
    "energy.csv": "timestamp_utc,building_load_kw,hvac_electric_kw,pv_kw,battery_discharge_kw_signed,battery_soc_kwh,grid_import_kw,grid_export_kw",
    "equipment.csv": "timestamp_utc,asset_id,metric,value",
    "logistics.csv": "timestamp_utc,asset_id,metric,value",
    "weather.csv": "timestamp_utc,outdoor_c,cloud_cover_pct",
    "weather_forecast.csv": "issued_utc,target_utc,outdoor_c_forecast",
    "tariff.csv": "timestamp_utc,price_eur_kwh,published_utc",
    "staff_roster.csv": "shift_id,resource_id,role,start_utc,end_utc,status,status_known_utc",
    "work_orders.csv": "work_order_id,created_utc,asset_id,type,status,scheduled_utc,completed_utc,assigned_to,summary",
    "guest_requests.csv": "request_id,timestamp_utc,room_id,booking_id,channel,language,request_type,text,priority,response_due_utc,requires_guest_consent",
}


def preheat_replay(tmp_path, *, move_at=MOVED, target_occupancy="vacant"):
    building = deepcopy(BUILDING)
    building["time"] = {"data_timezone": "UTC", "local_timezone": "Europe/Vienna", "interval_seconds": 900}
    (tmp_path / "building.json").write_text(json.dumps(building), encoding="utf-8")
    (tmp_path / "manifest.json").write_text(json.dumps({
        "model_id": "PLATO-40", "dataset_version": "2.0.0-dev", "synthetic": True,
        "profile": "preheat-reassignment-test", "start_utc": "2026-01-06T08:30:00Z",
        "end_utc": "2026-01-06T10:45:00Z", "interval_seconds": 900,
    }), encoding="utf-8")
    rows = {
        "bookings.csv": [{"booking_id": "B_PREHEAT", "room_id": "R101", "created_utc": CREATED,
                          "arrival_utc": ARRIVAL, "departure_utc": "2026-01-07T10:00:00Z",
                          "guest_count": 1, "channel": "direct", "status": "confirmed"}],
        "room_telemetry.csv": [{"timestamp_utc": stamp, "room_id": room, "temperature_c": 18,
                                "setpoint_c": 18, "humidity_pct": 45, "co2_ppm": 650,
                                "presence": 0, "window_open": 0, "housekeeping_state": "clean"}
                               for stamp in (START, MOVED, LATER) for room in ("R101", "R102")],
        "tariff.csv": [{"timestamp_utc": f"2026-01-06T{hour:02d}:00:00Z", "price_eur_kwh": price,
                        "published_utc": CREATED} for hour, price in ((9, .1), (10, .1), (11, .4), (12, .4))],
        "weather_forecast.csv": [{"issued_utc": "2026-01-06T05:00:00Z", "target_utc": stamp,
                                  "outdoor_c_forecast": -5} for stamp in (LATER, ARRIVAL)],
    }
    for filename, header in HEADERS.items():
        with (tmp_path / filename).open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=header.split(","))
            writer.writeheader()
            writer.writerows(rows.get(filename, []))
    events = [{"event_id": f"OUT_{room}", "timestamp_utc": "2026-01-06T08:30:00Z", "type": "checkout",
               "room_id": room, "booking_id": f"PREVIOUS_{room}"}
              for room in ("R101", "R102") if room == "R101" or target_occupancy != "unknown"]
    if target_occupancy == "occupied":
        events.append({"event_id": "IN_R102", "timestamp_utc": "2026-01-06T08:45:00Z", "type": "checkin",
                       "room_id": "R102", "booking_id": "B_EXISTING"})
    if move_at:
        events.append({"event_id": "MOVE_PREHEAT", "timestamp_utc": move_at, "type": "room_reassigned",
                       "booking_id": "B_PREHEAT", "from_room": "R101", "to_room": "R102"})
    (tmp_path / "events.jsonl").write_text("".join(json.dumps(row) + "\n" for row in events), encoding="utf-8")
    return ReplayData(tmp_path, use_llm=False), building


def replay_until(repo, building, until=LATER):
    events = [event for event in repo.iter_events() if instant(event.available_at_utc) <= instant(until)]
    assert any(event.event_type == "clock_tick" and event.available_at_utc == until for event in events)
    events.sort(key=lambda event: (instant(event.available_at_utc), event.event_type == "clock_tick", event.event_id))
    engine = DecisionEngine(repo, building)
    records = [record for event in events for record in engine.process(event)
               if record.issue_type == "energy_optimization"]
    return engine, records


def test_observed_prearrival_move_preheats_new_room_with_stable_unique_ids(tmp_path):
    repo, building = preheat_replay(tmp_path)
    before = {path.name: path.read_bytes() for path in tmp_path.iterdir()}
    assert repo.get_bookings_for_room("R102", START, ARRIVAL, START) == []
    booking = repo.get_booking("B_PREHEAT", LATER)
    assert booking["room_id"] == "R101"
    assert (booking["observed_room_id"], booking["observed_state"]) == ("R102", "not_arrived")
    assert repo.get_room_state("R102", LATER)["occupancy"] == "vacant"
    assert [row["booking_id"] for row in repo.get_bookings_for_room("R102", LATER, ARRIVAL, LATER)] == ["B_PREHEAT"]
    move = next(event for event in repo.iter_events() if event.source.record_id == "MOVE_PREHEAT")
    assert move.event_type == "operational_event"
    assert move.available_at_utc == MOVED
    assert move.entities.room_id == "R102"

    engine, records = replay_until(repo, building)
    assert [(row.timestamp_utc, row.affected_assets, row.action) for row in records] == [
        (START, ["R101"], "adjust_room_setpoint"), (LATER, ["R102"], "adjust_room_setpoint")]
    assert records[0].episode_id == records[1].episode_id
    assert len({row.event_id for row in records}) == 2
    assert not engine.rejections
    _, repeated = replay_until(ReplayData(tmp_path, use_llm=False), building)
    assert [row.model_dump() for row in repeated] == [row.model_dump() for row in records]
    assert {path.name: path.read_bytes() for path in tmp_path.iterdir()} == before


def test_unchanged_preheat_target_is_not_repeated_next_hour(tmp_path):
    repo, building = preheat_replay(tmp_path, move_at=None)
    engine, records = replay_until(repo, building)
    assert [(row.timestamp_utc, row.affected_assets) for row in records] == [(START, ["R101"])]
    assert not engine.rejections


def test_future_room_reassignment_does_not_change_preheat_target(tmp_path):
    repo, building = preheat_replay(tmp_path, move_at="2026-01-06T10:30:00Z")
    assert repo.get_booking("B_PREHEAT", LATER)["observed_room_id"] == "R101"
    assert repo.get_bookings_for_room("R102", LATER, ARRIVAL, LATER) == []
    engine, records = replay_until(repo, building)
    assert [(row.timestamp_utc, row.affected_assets) for row in records] == [(START, ["R101"])]
    assert not engine.rejections


@pytest.mark.parametrize("target_occupancy", ["occupied", "unknown"])
def test_reassignment_does_not_preheat_destination_without_observed_vacancy(tmp_path, target_occupancy):
    repo, building = preheat_replay(tmp_path, target_occupancy=target_occupancy)
    assert repo.get_booking("B_PREHEAT", LATER)["observed_room_id"] == "R102"
    assert repo.get_room_state("R102", LATER)["occupancy"] == target_occupancy
    _, records = replay_until(repo, building)
    assert [(row.timestamp_utc, row.affected_assets) for row in records] == [(START, ["R101"])]
