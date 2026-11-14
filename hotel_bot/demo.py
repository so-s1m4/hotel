"""Invented presentation property. None of these values are live observations."""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


def demo_property(now: datetime) -> dict:
    """Create an isolated 40-room fixture, with no dependency on Hotel datasets."""
    stamp = now.isoformat(timespec="seconds").replace("+00:00", "Z")
    names = ("Alex Morgan", "Sam Rivera", "Jamie Chen", "Taylor Reed", "Robin Blake",
             "Charlie Lane", "Avery Brooks", "Jordan Bell", "Casey Park", "Riley Stone")
    rooms = []
    for floor in range(1, 5):
        for number in range(1, 11):
            ident = f"R{floor}{number:02d}"
            status = "occupied" if number in {1, 3, 5, 6, 8} else "ready"
            if ident in {"R102", "R204", "R409"}:
                status = "attention"
            if ident in {"R104", "R307"}:
                status = "cleaning"
            rooms.append({
                "id": ident, "number": f"{floor}{number:02d}", "floor": floor,
                "status": status, "occupancy": "occupied" if status == "occupied" else "vacant",
                "housekeeping_state": "dirty" if status == "attention" else "in_progress" if status == "cleaning" else "clean",
                "guest_name": names[(floor * 3 + number) % len(names)] if status == "occupied" else None,
                "temperature_c": round(20.6 + ((floor + number) % 7) * 0.3, 1),
                "x": ((number - 1) % 5) + 0.5, "z": 0.5 if number <= 5 else 2.5,
                "fixture": True, "fixture_at_utc": stamp,
            })
    people = [
        {"id": "DEMO_HK1", "name": "Anna", "role": "housekeeping", "floor": 1,
         "room_id": None, "x": 3.5, "z": 1.4, "status": "Available in demo", "fixture": True},
        {"id": "DEMO_HK2", "name": "Ben", "role": "housekeeping", "floor": 3,
         "room_id": None, "x": 1.5, "z": 1.4, "status": "Available in demo", "fixture": True},
        {"id": "DEMO_TECH1", "name": "Leon", "role": "maintenance", "floor": 2,
         "room_id": None, "x": 3.1, "z": 1.4, "status": "Available in demo", "fixture": True},
        {"id": "DEMO_RECEPTION", "name": "Mira", "role": "reception", "floor": 1,
         "room_id": None, "x": 0.3, "z": 1.4, "status": "Front desk in demo", "fixture": True},
    ]
    for room in rooms:
        if room["status"] == "occupied" and room["number"].endswith(("01", "05", "08")):
            people.append({"id": "DEMO_GUEST_" + room["id"], "name": room["guest_name"],
                           "role": "guest", "floor": room["floor"], "room_id": room["id"],
                           "x": room["x"], "z": room["z"], "status": "In room in demo", "fixture": True})
    return {
        "property": {"id": "hotel-presentation-demo", "name": "PLATO 40", "mode": "synthetic_demo",
                     "floors": 4, "room_count": 40, "as_of_utc": stamp,
                     "disclaimer": "Invented demo property. Room states and people are illustrative; API classifications are real. Approvals create simulated staff tickets, never physical actions."},
        "rooms": rooms, "people": people,
        "building": {"building": {"rooms": [{"id": room["id"]} for room in rooms], "assets": []},
                     "time": {"local_timezone": "Europe/Vienna"},
                     "constraints": {"occupied_comfort_c": [20, 23]},
                     "staff": {"resources": [
                         {"resource_id": "DEMO_HK1", "role": "cleaner", "fixture": True},
                         {"resource_id": "DEMO_HK2", "role": "cleaner", "fixture": True},
                         {"resource_id": "DEMO_TECH1", "role": "technician", "fixture": True},
                     ]}},
    }


def demo_booking(room: dict, now: datetime) -> dict:
    """A clearly invented plan, never a claim about the source Hotel bookings."""
    local = now.astimezone(ZoneInfo("Europe/Vienna"))
    arrival = local.replace(hour=15, minute=0, second=0, microsecond=0)
    if room["occupancy"] == "occupied":
        arrival -= timedelta(days=1)
    departure = (local + timedelta(days=1)).replace(hour=11, minute=0, second=0, microsecond=0)
    def utc(value):
        return value.astimezone(ZoneInfo("UTC")).isoformat(timespec="seconds").replace("+00:00", "Z")
    return {"booking_id": "DEMO_BOOKING_" + room["id"], "room_id": room["id"],
            "created_utc": utc(now - timedelta(days=7)),
            "arrival_utc": utc(arrival), "departure_utc": utc(departure), "fixture": True}
