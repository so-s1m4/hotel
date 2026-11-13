"""Malformed source type remains unknown instead of crashing the public API."""
import pytest

from common.contracts.schemas import NormalizedEventV1
from engine.planner import DecisionEngine
from tests.engine.fixtures import BUILDING, FakeGateway


@pytest.mark.parametrize("kind", [[], {}, ["cleaning_started"], {"type": "checkout"}, None, 123])
def test_non_string_operational_type_cannot_create_work_or_poison_retry(kind):
    repo = FakeGateway()
    event = NormalizedEventV1(
        event_id="raw:malformed", timestamp_utc="2026-01-06T09:00:00Z",
        available_at_utc="2026-01-06T09:00:00Z", event_type="operational_event",
        source={"file": "events.jsonl", "record_id": "malformed"},
        entities={"room_id": "R101", "resource_id": "CLEANER_1"}, payload={"type": kind})
    engine = DecisionEngine(repo, BUILDING)
    assert engine.process(event) == []
    assert engine.process(event) == []
    assert engine.event_count == 1
    assert engine.calendar.observed_busy == {}
    assert engine.calendar.last_dirty_trigger == {}
    assert engine.executor.audit == []
    assert event.payload["type"] == kind
