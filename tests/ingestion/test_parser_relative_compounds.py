"""Bounded Dutch date regressions with injected replies; no live model calls.

These tests exercise host normalization, not model language comprehension.
"""
from __future__ import annotations

import copy

import pytest

from tests.ingestion.test_guest_parser import assert_contract, llm, parser


@pytest.fixture
def arrival():
    record = {
        "request_id": "synthetic:dutch-relative-date",
        "timestamp_utc": "2026-01-05T10:00:00Z",
        "room_id": "R308",
        "booking_id": "BK-SYNTHETIC",
        "channel": "app_chat",
        "language": "nl",
        "request_type": "arrival_delay",
        "text": "Door vertraging komen we morgenavond om 21:00 aan.",
        "priority": "medium",
        "response_due_utc": "2026-01-05T13:00:00Z",
        "requires_guest_consent": "not_required",
    }
    context = {
        "timezone": "Europe/Vienna",
        "as_of_utc": record["timestamp_utc"],
        "booking": {
            "booking_id": record["booking_id"],
            "created_utc": "2026-01-04T10:00:00Z",
            "arrival_utc": "2026-01-05T14:00:00Z",
            "departure_utc": "2026-01-10T10:00:00Z",
        },
    }
    return record, context


def extract(parser, llm, record, context, time_text, model_time, *, model_review=False):
    before = copy.deepcopy((record, context))
    calls = llm({"intents": [{
        "intent": "arrival_delay",
        "requested_time_utc": model_time,
        "requested_time_text": time_text,
        "evidence_quote": record["text"],
        "details": {"approximate_time": False},
        "needs_review": model_review,
    }]})
    items, meta = parser.parse_request_full(record, context)
    assert len(calls) == 1
    assert_contract(record, items, meta)
    assert (record, context) == before
    assert len(items) == 1 and items[0]["intent"] == "arrival_delay"
    assert items[0]["evidence_quote"] == record["text"]
    assert items[0]["requested_time_text"] == time_text
    return items[0], meta


@pytest.mark.parametrize("daypart,clock,correct,wrong", [
    ("morgenochtend", "09:00", "2026-01-06T08:00:00Z", "2026-01-05T08:00:00Z"),
    ("morgenmiddag", "15:00", "2026-01-06T14:00:00Z", "2026-01-05T14:00:00Z"),
    ("morgenavond", "21:00", "2026-01-06T20:00:00Z", "2026-01-05T20:00:00Z"),
])
@pytest.mark.parametrize("model_choice", ["correct", "null", "booking_day"])
def test_tomorrow_compound_cannot_take_booking_day(
    parser, llm, arrival, daypart, clock, correct, wrong, model_choice,
):
    record, context = arrival
    time_text = f"{daypart} om {clock}"
    record["text"] = f"Door vertraging komen we {time_text} aan."
    model_time = {"correct": correct, "null": None, "booking_day": wrong}[model_choice]
    item, meta = extract(parser, llm, record, context, time_text, model_time)
    # An explicit relative date takes precedence over the known arrival plan.
    # Merely marking the wrong booking-day timestamp for review is insufficient.
    assert item["requested_time_utc"] == correct
    assert item["requested_time_utc"] != wrong
    assert item["needs_review"] is (model_choice == "booking_day")
    assert meta.needs_review is (model_choice == "booking_day")
    if model_choice == "booking_day":
        assert "model_time_mismatch" in meta.uncertain_fields


def test_short_clock_quote_still_uses_compound_in_evidence(parser, llm, arrival):
    record, context = arrival
    item, meta = extract(parser, llm, record, context, "21:00", "2026-01-06T20:00:00Z")
    assert item["requested_time_utc"] == "2026-01-06T20:00:00Z"
    assert not item["needs_review"] and not meta.needs_review


def test_overmorgenavond_keeps_two_day_precedence(parser, llm, arrival):
    record, context = arrival
    record["text"] = "Door vertraging komen we overmorgenavond om 21:00 aan."
    item, meta = extract(
        parser, llm, record, context, "overmorgenavond om 21:00", "2026-01-07T20:00:00Z",
    )
    assert item["requested_time_utc"] == "2026-01-07T20:00:00Z"
    assert item["requested_time_utc"] not in {"2026-01-05T20:00:00Z", "2026-01-06T20:00:00Z"}
    assert not item["needs_review"] and not meta.needs_review


@pytest.mark.parametrize("message_time,correct", [
    # Jan 5 UTC is already Jan 6 in Vienna; tomorrow is Jan 7.
    ("2026-01-05T23:30:00Z", "2026-01-07T20:00:00Z"),
    # Mar 28 local + one day reaches Mar 29, after the switch to CEST.
    ("2026-03-27T23:30:00Z", "2026-03-29T19:00:00Z"),
])
def test_plain_tomorrow_keeps_vienna_calendar_and_requested_day_offset(
    parser, llm, arrival, message_time, correct,
):
    record, context = arrival
    record.update(timestamp_utc=message_time, text="We komen morgen om 21:00 aan.")
    context["as_of_utc"] = message_time
    item, meta = extract(parser, llm, record, context, "morgen om 21:00", correct)
    assert item["requested_time_utc"] == correct
    assert not item["needs_review"] and not meta.needs_review


@pytest.mark.parametrize("created_present", [True, False])
def test_undated_known_arrival_keeps_direct_api_precondition(parser, llm, arrival, created_present):
    record, context = arrival
    record["text"] = "We komen om 21:00 aan."
    if not created_present:
        # Direct callers must provide historically known context. This change
        # does not add a requirement to independently prove that via created_utc.
        context["booking"].pop("created_utc")
    item, meta = extract(parser, llm, record, context, "om 21:00", "2026-01-05T20:00:00Z")
    assert item["requested_time_utc"] == "2026-01-05T20:00:00Z"
    assert not item["needs_review"] and not meta.needs_review


def test_undated_future_booking_stays_unusable_at_message_time(parser, llm, arrival):
    record, context = arrival
    record["text"] = "We komen om 21:00 aan."
    context["as_of_utc"] = "2026-01-07T00:00:00Z"
    context["booking"].update(
        created_utc="2026-01-06T00:00:00Z", arrival_utc="2026-01-08T14:00:00Z",
    )
    item, meta = extract(parser, llm, record, context, "om 21:00", "2026-01-08T20:00:00Z")
    assert item["requested_time_utc"] is None
    assert item["needs_review"] and meta.needs_review
    assert {"context.booking", "requested_time_utc"} <= set(meta.uncertain_fields)


def test_conflicting_relative_days_remain_unresolved(parser, llm, arrival):
    record, context = arrival
    record["text"] = "We komen morgen of overmorgen om 21:00 aan."
    item, meta = extract(
        parser, llm, record, context, "morgen of overmorgen om 21:00", "2026-01-06T20:00:00Z",
    )
    assert item["requested_time_utc"] is None
    assert item["needs_review"] and meta.needs_review
    assert "requested_time_utc" in meta.uncertain_fields


def test_compound_without_clock_does_not_invent_evening_hour(parser, llm, arrival):
    record, context = arrival
    record["text"] = "Door vertraging komen we morgenavond aan."
    item, meta = extract(parser, llm, record, context, "morgenavond", "2026-01-06T20:00:00Z")
    assert item["requested_time_utc"] is None
    assert item["needs_review"] and meta.needs_review
    assert "requested_time_utc" in meta.uncertain_fields


def test_explicit_model_uncertainty_is_preserved_for_compound(parser, llm, arrival):
    record, context = arrival
    item, meta = extract(
        parser, llm, record, context, "morgenavond om 21:00", None, model_review=True,
    )
    assert item["requested_time_utc"] is None
    assert item["needs_review"] and meta.needs_review
    assert "requested_time_utc" in meta.uncertain_fields
