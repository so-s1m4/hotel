"""Synthetic gateway causality and real planner capacity, without data or API."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from common.contracts.schemas import EventEntitiesV1, ExtractionMetaV1, GuestIntentV1, NormalizedEventV1, SourceRefV1
from engine.planner import DecisionEngine
from engine.core import instant
from hotel_bot.demo import demo_property
from hotel_bot.gateway import DemoGateway


NOW = datetime(2026, 11, 16, 9, tzinfo=timezone.utc)
STAMP = "2026-11-16T09:00:00Z"


def gateway():
    return DemoGateway(demo_property(NOW), NOW)


def test_no_room_booking_or_shift_is_visible_before_fixture_publication():
    repo = gateway()
    before = "2026-11-16T08:59:59Z"
    assert repo.get_room_state("R101", before) is None
    assert repo.get_booking("DEMO_BOOKING_R101", before) is None
    assert repo.get_staff_slots(STAMP, "2026-11-16T17:00:00Z", before) == []
    assert repo.get_bookings_for_room("R101", before, "2026-11-17T12:00:00Z", before) == []


def test_clock_refresh_preserves_historical_views_and_never_invents_outcomes():
    repo = gateway()
    first = repo.get_room_state("R102", STAMP)
    repo.set_now(NOW + timedelta(hours=1))
    current = repo.get_room_state("R102", "2026-11-16T10:00:00Z")
    historical = repo.get_room_state("R102", "2026-11-16T09:30:00Z")
    future = repo.get_room_state("R102", "2026-11-20T00:00:00Z")
    assert first["last_observed_utc"] == historical["last_observed_utc"] == STAMP
    assert current["last_observed_utc"] == future["last_observed_utc"] == "2026-11-16T10:00:00Z"
    assert first["housekeeping_state"] == current["housekeeping_state"] == "dirty"
    assert current["observation_kind"] == "synthetic_demo_fixture" and current["fixture"]
    assert all(ref.startswith("demo_fixture:") for ref in current["evidence"])
    assert list(repo.iter_events()) == []
    assert repo.get_events(STAMP, "2026-11-20T00:00:00Z", "2026-11-20T00:00:00Z") == []
    assert repo.get_measurements("room_water.csv", STAMP, "2026-11-20T00:00:00Z", STAMP) == []
    assert repo.get_weather_forecast(STAMP, "2026-11-20T00:00:00Z") == []
    assert repo.get_tariffs(STAMP, "2026-11-20T00:00:00Z") == []
    with pytest.raises(ValueError, match="backwards"):
        repo.set_now(NOW)


def test_bookings_and_real_shaped_staff_shifts_remain_fixed_across_midnight():
    repo = gateway()
    first = repo.get_booking("DEMO_BOOKING_R102", STAMP)
    slots = repo.get_staff_slots(STAMP, "2026-11-16T17:00:00Z", STAMP)
    assert len(slots) == 3
    assert [slot["role"] for slot in slots].count("cleaner") == 2
    assert [slot["role"] for slot in slots].count("technician") == 1
    assert all(slot["status_known_utc"] == STAMP and slot["effective_status_asof"] == "scheduled"
               and slot["fixture"] for slot in slots)
    assert len(repo.get_staff_slots(STAMP, "2026-11-16T17:00:00Z", STAMP, role="cleaner")) == 2
    assert repo.get_staff_slots("2026-11-16T17:00:00Z", "2026-11-16T18:00:00Z", STAMP) == []
    repo.set_now(NOW + timedelta(days=1))
    assert repo.get_booking("DEMO_BOOKING_R102", "2026-11-17T09:00:00Z") == first
    assert repo.get_staff_slots("2026-11-17T09:00:00Z", "2026-11-17T10:00:00Z", "2026-11-17T09:00:00Z") == []
    assert first["room_id"] == "R102" and first["fixture"]


def test_gateway_results_and_fixture_are_independent_and_occupancy_is_explicit():
    fixture = demo_property(NOW)
    before = deepcopy(fixture)
    repo = DemoGateway(fixture, NOW)
    room = repo.get_room_state("R101", STAMP)
    assert room["occupancy"] == "occupied"
    assert room["current_booking_id"] == "DEMO_BOOKING_R101"
    assert room["presence"] is None
    room["occupancy"] = "vacant"
    room["evidence"].clear()
    booking = repo.get_booking("DEMO_BOOKING_R101", STAMP)
    booking["arrival_utc"] = "2099-01-01T00:00:00Z"
    slots = repo.get_staff_slots(STAMP, "2026-11-16T17:00:00Z", STAMP)
    slots[0]["status"] = "sick"
    assert repo.get_room_state("R101", STAMP)["occupancy"] == "occupied"
    assert repo.get_booking("DEMO_BOOKING_R101", STAMP)["arrival_utc"] != booking["arrival_utc"]
    assert repo.get_staff_slots(STAMP, "2026-11-16T17:00:00Z", STAMP)[0]["status"] == "scheduled"
    assert fixture == before


def test_subsecond_clock_matches_second_precision_live_events():
    repo = DemoGateway(demo_property(NOW), NOW + timedelta(microseconds=900000))
    assert repo.get_room_state("R102", STAMP) is not None
    assert repo.get_booking("DEMO_BOOKING_R102", STAMP) is not None
    repo.set_now(NOW + timedelta(seconds=1, microseconds=10000))
    assert repo.get_room_state("R102", "2026-11-16T09:00:01Z")["last_observed_utc"] == "2026-11-16T09:00:01Z"


def test_real_engine_schedules_three_dirty_rooms_with_two_nonoverlapping_cleaners():
    repo = gateway()
    engine = DecisionEngine(repo, repo.building, enable_guest_services=True)
    decisions = []
    for index, room in enumerate(("R102", "R204", "R409")):
        ident = f"fixture-wait-{index}"
        text = "I have arrived and am waiting for my room."
        event = NormalizedEventV1(
            event_id="guest:" + ident, timestamp_utc=STAMP, available_at_utc=STAMP,
            event_type="guest_request", source=SourceRefV1(file="synthetic_messages", record_id=ident),
            entities=EventEntitiesV1(room_id=room, booking_id="DEMO_BOOKING_" + room, guest_request_id=ident),
            payload={"request_type": None, "original_text": text, "intents": [
                GuestIntentV1(intent="waiting_for_room", evidence_quote=text, needs_review=False).model_dump()]},
            extraction=ExtractionMetaV1(method="llm", model_name="injected-no-network", needs_review=False))
        decisions.extend(engine.process(event))
    jobs = [decision for decision in decisions if decision.action == "schedule_cleaning"]
    assert len(jobs) == 3
    assert {job.resource_id for job in jobs} == {"DEMO_HK1", "DEMO_HK2"}
    for resource, reservations in engine.calendar.iter_reservations():
        ordered = sorted(reservations, key=lambda row: instant(row[0]))
        assert all(instant(first[1]) <= instant(second[0]) for first, second in zip(ordered, ordered[1:]))
    assert not engine.rejections
    assert not any(decision.action == "release_room" for decision in decisions)
    assert all(not audit["effects_applied"] and not audit["observed_history_changed"] for audit in engine.executor.audit)
    assert all(repo.get_room_state(room, STAMP)["housekeeping_state"] == "dirty" for room in ("R102", "R204", "R409"))
