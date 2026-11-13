"""Owner A: ReplayDataGateway implementation. Methods are explicit TODOs."""
from pathlib import Path
from typing import Iterator, Any
from common.contracts.schemas import NormalizedEventV1
from ingestion.loader import inspect_dataset

class ReplayData:

    def __init__(self, data_dir: str | Path):
        self.data_dir = Path(data_dir)
        self.metadata = inspect_dataset(self.data_dir)

    def iter_events(self) -> Iterator[NormalizedEventV1]:
        """Sorted by (available_at_utc, event_id); no future event leakage."""
        raise NotImplementedError('TODO A: implement time-causal iter_events')

    def get_room_state(self, room_id: str, as_of_utc: str) -> dict[str, Any] | None:
        """Latest known room state; missing != zero; motion != occupancy proof."""
        raise NotImplementedError('TODO A: implement time-causal get_room_state')

    def get_booking(self, booking_id: str, as_of_utc: str) -> dict[str, Any] | None:
        """Only if created_utc <= as_of; no use of future final cancellation status."""
        raise NotImplementedError('TODO A: implement time-causal get_booking')

    def get_bookings_for_room(self, room_id: str, start_utc: str, end_utc: str, as_of_utc: str) -> list[dict[str, Any]]:
        """Known bookings overlapping desired time window, no future leakage."""
        raise NotImplementedError('TODO A: implement time-causal get_bookings_for_room')

    def get_staff_slots(self, start_utc: str, end_utc: str, as_of_utc: str, role: str | None=None) -> list[dict[str, Any]]:
        """Resource slots with sick/vacation updates only after status_known_utc."""
        raise NotImplementedError('TODO A: implement time-causal get_staff_slots')

    def get_measurements(self, filename: str, start_utc: str, end_utc: str, as_of_utc: str, entity_id: str | None=None, metric: str | None=None) -> list[dict[str, Any]]:
        """Historical observations bounded by <= as_of; caller must handle gaps/resets."""
        raise NotImplementedError('TODO A: implement time-causal get_measurements')

    def get_weather_forecast(self, as_of_utc: str, until_utc: str) -> list[dict[str, Any]]:
        """Only forecasts with issued_utc <= as_of, select latest as issued."""
        raise NotImplementedError('TODO A: implement time-causal get_weather_forecast')

    def get_tariffs(self, as_of_utc: str, until_utc: str) -> list[dict[str, Any]]:
        """Only price values with published_utc <= as_of."""
        raise NotImplementedError('TODO A: implement time-causal get_tariffs')

    def get_events(self, start_utc: str, end_utc: str, as_of_utc: str, room_id: str | None=None, event_type: str | None=None) -> list[dict[str, Any]]:
        """Observed historical events; no future checkin/checkout/cleaning outcomes."""
        raise NotImplementedError('TODO A: implement time-causal get_events')
