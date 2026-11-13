"""Test-only causal gateway; never used by the submission pipeline."""
from copy import deepcopy
from datetime import timedelta
import json
from pathlib import Path

from common.contracts.schemas import NormalizedEventV1
from engine.core import instant, utc

BUILDING = {
    "building": {
        "rooms": [{"id": "R101"}, {"id": "R102"}],
        "assets": [{"id": "HP1"}, {"id": "WM_MAIN"}, {"id": "BAT1"}],
    },
    "staff": {"resources": [{"resource_id": "CLEANER_1", "role": "cleaner"},
                             {"resource_id": "TECH_1", "role": "technician"},
                             {"resource_id": "ROBOT_1", "role": "robot"},
                             {"resource_id": "ONCALL_1", "role": "on_call_housekeeping"}]},
    "constraints": {"occupied_comfort_c": [20, 23]},
}


def state(room="R101", t="2026-01-06T09:00:00Z", occupancy="vacant", cleaning="dirty", **kwargs):
    return {"room_id": room, "last_observed_utc": t, "occupancy": occupancy,
            "housekeeping_state": cleaning, "evidence": [f"room_telemetry.csv:{room}:{t}", f"events.jsonl:occupancy:{room}:{t}"], **kwargs}


def tick(t="2026-01-06T09:00:00Z", event_id=None):
    return NormalizedEventV1(event_id=event_id or "tick:" + t, timestamp_utc=t, available_at_utc=t,
        event_type="clock_tick", source={"file": "__simulation_clock__", "record_id": t}, entities={})


def request(t="2026-01-06T09:00:00Z", room="R101", deadline="2026-01-06T10:00:00Z", rid="G1", waiting=False):
    example = json.loads((Path(__file__).resolve().parents[2] / "common/examples/normalized_event_early_checkin.json").read_text())
    example.update(event_id="guest:" + rid, timestamp_utc=t, available_at_utc=t)
    example["source"].update(record_id=rid, timestamp_utc=t)
    example["entities"].update(room_id=room, booking_id="B1", guest_request_id=rid)
    example["payload"]["request_type"] = "waiting_for_room" if waiting else "early_checkin"
    example["payload"]["intents"][0]["requested_time_utc"] = deadline
    return NormalizedEventV1.model_validate(example)


def slot(resource="CLEANER_1", start="2026-01-06T08:00:00Z", end="2026-01-06T14:30:00Z", status="scheduled", **kwargs):
    return {"shift_id": "SH1", "resource_id": resource, "role": "cleaner", "start_utc": start,
            "end_utc": end, "status": status, "status_known_utc": "2026-01-05T00:00:00Z",
            "effective_status_asof": status, **kwargs}


class FakeGateway:
    def __init__(self):
        self.states = {}
        self.measurements = {}
        self.slots = []
        self.events = []
        self.bookings = []
        self.forecasts = []
        self.tariffs = []
        self.stream = []
        self.calls = []
        self.leak_future_rows = False  # adversarial gateway test

    def iter_events(self):
        return iter(self.stream)

    def get_room_state(self, room_id, as_of_utc):
        self.calls.append(("state", as_of_utc))
        rows = [s for s in self.states.get(room_id, []) if instant(s["last_observed_utc"]) <= instant(as_of_utc)]
        return deepcopy(max(rows, key=lambda s: instant(s["last_observed_utc"]))) if rows else None

    def get_booking(self, booking_id, as_of_utc):
        self.calls.append(("booking", as_of_utc))
        return next((deepcopy(b) for b in self.bookings if b["booking_id"] == booking_id and
                     instant(b["created_utc"]) <= instant(as_of_utc)), None)

    def get_bookings_for_room(self, room_id, start_utc, end_utc, as_of_utc):
        self.calls.append(("bookings", as_of_utc))
        return [deepcopy(b) for b in self.bookings if b["room_id"] == room_id and
                instant(b["created_utc"]) <= instant(as_of_utc) and
                instant(b["arrival_utc"]) < instant(end_utc) and instant(b["departure_utc"]) > instant(start_utc)]

    def get_staff_slots(self, start_utc, end_utc, as_of_utc, role=None):
        self.calls.append(("staff", as_of_utc))
        return [deepcopy(s) for s in self.slots if (not role or s["role"] == role) and
                instant(s["start_utc"]) < instant(end_utc) and instant(s["end_utc"]) > instant(start_utc)]

    def get_measurements(self, filename, start_utc, end_utc, as_of_utc, entity_id=None, metric=None):
        self.calls.append((filename, as_of_utc))
        return [deepcopy(r) for r in self.measurements.get(filename, []) if
                (self.leak_future_rows or instant(start_utc) <= instant(r["timestamp_utc"]) <= min(instant(end_utc), instant(as_of_utc))) and
                (entity_id is None or entity_id in (r.get("room_id"), r.get("asset_id"))) and
                (metric is None or metric == r.get("metric"))]

    def get_events(self, start_utc, end_utc, as_of_utc, room_id=None, event_type=None):
        self.calls.append(("events", as_of_utc))
        return [deepcopy(r) for r in self.events if instant(start_utc) <= instant(r["timestamp_utc"]) <= min(instant(end_utc), instant(as_of_utc))
                and (not room_id or r.get("room_id") == room_id) and (not event_type or r.get("type") == event_type)]

    def get_weather_forecast(self, as_of_utc, until_utc):
        self.calls.append(("forecast", as_of_utc))
        return deepcopy(self.forecasts)  # intentionally unfiltered; tests assert engine boundary

    def get_tariffs(self, as_of_utc, until_utc):
        self.calls.append(("tariff", as_of_utc))
        return deepcopy(self.tariffs)


def water_repo(now="2026-01-06T02:00:00Z", flow=5, occupancy="vacant", cleaning="clean"):
    repo = FakeGateway()
    rows = []
    for i in range(5):
        t = utc(instant(now) - timedelta(minutes=15 * (4 - i)))
        rows.append({"timestamp_utc": t, "room_id": "R101", "meter_total_l": 100 + i * flow})
        repo.states.setdefault("R101", []).append(state(t=t, occupancy=occupancy, cleaning=cleaning))
    repo.measurements["room_water.csv"] = rows
    return repo


def energy_repo(now="2026-01-06T09:00:00Z", temperature=18.5, occupancy="occupied", setpoint=21):
    repo = FakeGateway()
    for i in range(5):
        t = utc(instant(now) - timedelta(minutes=15 * (4 - i)))
        repo.states.setdefault("R101", []).append(state(t=t, occupancy=occupancy, cleaning="clean"))
        repo.measurements.setdefault("room_telemetry.csv", []).append({"timestamp_utc": t, "room_id": "R101",
            "temperature_c": temperature, "setpoint_c": setpoint, "window_open": 0})
        for metric, value in [("heat_output_kw", 10), ("electric_kw", 10), ("status", "running")]:
            repo.measurements.setdefault("equipment.csv", []).append({"timestamp_utc": t, "asset_id": "HP1", "metric": metric, "value": value})
        repo.measurements.setdefault("weather.csv", []).append({"timestamp_utc": t, "outdoor_c": 0})
    return repo
