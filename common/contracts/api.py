"""Strict integration surface. Implement ReplayDataGateway in ingestion/.

Every method MUST enforce temporal causality using as_of_utc. Always return
copies/dicts; never leak future outcomes from completed_utc or final statuses.
Methods are synchronous for a 24h hackathon and return a bounded time range.
"""
from typing import Protocol, Iterator, Any

from .schemas import NormalizedEventV1


class ReplayDataGateway(Protocol):
    def iter_events(self) -> Iterator[NormalizedEventV1]:
        """Sorted by (available_at_utc, event_id); no future event leakage."""
        ...

    def get_room_state(self, room_id: str, as_of_utc: str) -> dict[str, Any] | None:
        """Latest known room state; missing != zero; motion != occupancy proof."""
        ...

    def get_booking(self, booking_id: str, as_of_utc: str) -> dict[str, Any] | None:
        """Only if created_utc <= as_of; no use of future final cancellation status."""
        ...

    def get_bookings_for_room(self, room_id: str, start_utc: str,
                              end_utc: str, as_of_utc: str) -> list[dict[str, Any]]:
        """Known bookings overlapping desired time window, no future leakage."""
        ...

    def get_staff_slots(self, start_utc: str, end_utc: str,
                        as_of_utc: str, role: str | None = None) -> list[dict[str, Any]]:
        """Resource slots with sick/vacation updates only after status_known_utc."""
        ...

    def get_measurements(self, filename: str, start_utc: str, end_utc: str,
                         as_of_utc: str, entity_id: str | None = None,
                         metric: str | None = None) -> list[dict[str, Any]]:
        """Historical observations bounded by <= as_of; caller must handle gaps/resets."""
        ...

    def get_weather_forecast(self, as_of_utc: str,
                             until_utc: str) -> list[dict[str, Any]]:
        """Only forecasts with issued_utc <= as_of, select latest as issued."""
        ...

    def get_tariffs(self, as_of_utc: str,
                    until_utc: str) -> list[dict[str, Any]]:
        """Only price values with published_utc <= as_of."""
        ...

    def get_events(self, start_utc: str, end_utc: str,
                   as_of_utc: str, room_id: str | None = None,
                   event_type: str | None = None) -> list[dict[str, Any]]:
        """Observed historical events; no future checkin/checkout/cleaning outcomes."""
        ...
