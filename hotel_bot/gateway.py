"""Causal, explicitly invented property state for the real decision engine.

No Hotel dataset is read here. Advancing this demonstration clock republishes
the same fixture observation; it never applies simulated work or invents a
check-in, cleaning completion, inspection, delivery or physical measurement.
Bookings and eight-hour staff shifts are fixed when the session is created.
"""
from __future__ import annotations

from bisect import bisect_right
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Iterator

from common.contracts.schemas import NormalizedEventV1
from engine.core import instant
from hotel_bot.demo import demo_booking


def _stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _aware(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Demo clock must be timezone-aware")
    # Live event envelopes use second precision; keep fixture publication at
    # that same precision rather than making it appear later than the event.
    return value.astimezone(timezone.utc).replace(microsecond=0)


class DemoGateway:
    """ReplayDataGateway over one immutable synthetic property fixture.

    ``fixture`` is the complete result of ``demo_property(now)``. Call
    ``set_now(now)`` before processing a new live message/approval; the service
    passes incoming events directly to ``DecisionEngine.process``.
    """

    def __init__(self, fixture: dict, now: datetime | None = None):
        self._fixture = deepcopy(fixture)
        self._started = _aware(now) if now is not None else instant(fixture["property"]["as_of_utc"])
        self._now = self._started
        self._observations = [self._started]
        self._rooms = {room["id"]: deepcopy(room) for room in fixture["rooms"]}
        if len(self._rooms) != len(fixture["rooms"]):
            raise ValueError("Demo room identities must be unique")
        self.building = deepcopy(fixture["building"])
        self._bookings = {}
        for room in self._rooms.values():
            booking = demo_booking(room, self._started)
            booking["fixture_known_utc"] = _stamp(self._started)
            booking["evidence"] = [f"demo_fixture:booking:{booking['booking_id']}:known={_stamp(self._started)}"]
            self._bookings[booking["booking_id"]] = booking
        self._slots = [
            {"shift_id": "DEMO_SHIFT_" + resource["resource_id"],
             "resource_id": resource["resource_id"], "role": resource["role"],
             "start_utc": _stamp(self._started), "end_utc": _stamp(self._started + timedelta(hours=8)),
             "status": "scheduled", "status_known_utc": _stamp(self._started),
             "effective_status_asof": "scheduled", "fixture": True}
            for resource in self.building.get("staff", {}).get("resources", [])
        ]

    def set_now(self, now: datetime) -> None:
        """Publish a fresh unchanged demo observation without advancing outcomes."""
        now = _aware(now)
        if now < self._now:
            raise ValueError("Demo clock cannot move backwards")
        if now > self._now:
            self._observations.append(now)
            self._now = now

    def _cutoff(self, as_of_utc: str) -> datetime:
        return min(instant(as_of_utc), self._now)

    def iter_events(self) -> Iterator[NormalizedEventV1]:
        # User messages enter through the service. There are deliberately no
        # manufactured historical occupancy/work events or telemetry ticks.
        return iter(())

    def get_room_state(self, room_id: str, as_of_utc: str) -> dict | None:
        cutoff = self._cutoff(as_of_utc)
        index = bisect_right(self._observations, cutoff) - 1
        room = self._rooms.get(room_id)
        if room is None or index < 0:
            return None
        observed = _stamp(self._observations[index])
        occupancy = room.get("occupancy", "unknown")
        return {"room_id": room_id, "as_of_utc": as_of_utc, "last_observed_utc": observed,
                "temperature_c": room.get("temperature_c"), "setpoint_c": None,
                "humidity_pct": None, "co2_ppm": None, "presence": None, "window_open": None,
                "housekeeping_state": room.get("housekeeping_state"), "occupancy": occupancy,
                "current_booking_id": "DEMO_BOOKING_" + room_id if occupancy == "occupied" else None,
                "fixture": True, "observation_kind": "synthetic_demo_fixture",
                "evidence": [f"demo_fixture:room:{room_id}:{observed}"]}

    def get_booking(self, booking_id: str, as_of_utc: str) -> dict | None:
        cutoff = self._cutoff(as_of_utc)
        booking = self._bookings.get(booking_id)
        if booking is None or cutoff < self._started or instant(booking["created_utc"]) > cutoff:
            return None
        return deepcopy(booking)

    def get_bookings_for_room(self, room_id: str, start_utc: str, end_utc: str,
                              as_of_utc: str) -> list[dict]:
        start, end = instant(start_utc), instant(end_utc)
        self._cutoff(as_of_utc)
        if start >= end:
            return []
        return [booking for identity in self._bookings
                if (booking := self.get_booking(identity, as_of_utc)) is not None
                and booking["room_id"] == room_id
                and instant(booking["arrival_utc"]) < end and start < instant(booking["departure_utc"])]

    def get_staff_slots(self, start_utc: str, end_utc: str, as_of_utc: str,
                        role: str | None = None) -> list[dict]:
        start, end, cutoff = instant(start_utc), instant(end_utc), self._cutoff(as_of_utc)
        if start >= end or cutoff < self._started:
            return []
        return [deepcopy(slot) for slot in self._slots
                if (role is None or slot["role"] == role) and instant(slot["status_known_utc"]) <= cutoff
                and instant(slot["start_utc"]) < end and start < instant(slot["end_utc"])]

    def get_measurements(self, filename: str, start_utc: str, end_utc: str,
                         as_of_utc: str, entity_id: str | None = None,
                         metric: str | None = None) -> list[dict]:
        instant(start_utc), instant(end_utc), self._cutoff(as_of_utc)
        return []

    def get_weather_forecast(self, as_of_utc: str, until_utc: str) -> list[dict]:
        self._cutoff(as_of_utc), instant(until_utc)
        return []

    def get_tariffs(self, as_of_utc: str, until_utc: str) -> list[dict]:
        self._cutoff(as_of_utc), instant(until_utc)
        return []

    def get_events(self, start_utc: str, end_utc: str, as_of_utc: str,
                   room_id: str | None = None, event_type: str | None = None) -> list[dict]:
        instant(start_utc), instant(end_utc), self._cutoff(as_of_utc)
        return []
