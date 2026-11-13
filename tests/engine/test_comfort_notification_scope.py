"""Comfort recipient updates preserve one physical incident and its hysteresis."""
from copy import deepcopy
from datetime import timedelta

import pytest

from common.contracts.schemas import NormalizedEventV1
from engine.core import instant, utc
from engine.planner import DecisionEngine
from tests.engine.fixtures import BUILDING, FakeGateway, state, tick


def at(index):
    return utc(instant("2026-01-06T08:30:00Z") + timedelta(minutes=15 * index))


def comfort_notices(temperatures, *, bookings=None, occupancies=None, window=0, transitions=()):
    """Replay observed room state; no planned booking table supplies identities."""
    repo = FakeGateway()
    building = deepcopy(BUILDING)
    building["building"]["rooms"] = [{"id": "R101"}]
    bookings = bookings if bookings is not None else ["B1"] * len(temperatures)
    occupancies = occupancies if occupancies is not None else ["occupied"] * len(temperatures)
    rows = []
    repo.states["R101"] = []
    for index, temperature in enumerate(temperatures):
        stamp = at(index)
        rows.append({"room_id": "R101", "timestamp_utc": stamp,
                     "temperature_c": temperature, "setpoint_c": 21, "window_open": window})
        repo.states["R101"].append(state(
            t=stamp, occupancy=occupancies[index], cleaning="clean", current_booking_id=bookings[index],
            temperature_c=temperature, setpoint_c=21, window_open=window))
    repo.measurements["room_telemetry.csv"] = rows
    stream = [tick(at(index)) for index in range(2, len(temperatures))]
    for index, kind, booking in transitions:
        event = {"event_id": f"{kind}:{index}", "timestamp_utc": at(index),
                 "type": kind, "room_id": "R101", "booking_id": booking}
        repo.events.append(event)
        stream.append(NormalizedEventV1(
            event_id="raw:" + event["event_id"], timestamp_utc=at(index), available_at_utc=at(index),
            event_type="operational_event",
            source={"file": "events.jsonl", "record_id": event["event_id"], "timestamp_utc": at(index)},
            entities={"room_id": "R101", "booking_id": booking}, payload=deepcopy(event)))
    stream.sort(key=lambda event: (instant(event.available_at_utc), event.event_type == "clock_tick", event.event_id))
    engine = DecisionEngine(repo, building)
    return [record for event in stream for record in engine.process(event)
            if record.issue_type == "heating_comfort" and record.action == "notify_guest"]


def assert_one_incident(notices):
    assert len({record.event_id for record in notices}) == len(notices)
    assert len({record.episode_id for record in notices}) == 1


@pytest.mark.parametrize("missing", [False, True], ids=["unchanged", "missing-reading"])
def test_same_booking_does_not_repeat_after_cooldown_or_missing_temperature(missing):
    temperatures = [24] * 13
    if missing:
        temperatures[3] = None
    notices = comfort_notices(temperatures)
    assert [(record.timestamp_utc, record.priority) for record in notices] == [(at(2), "medium")]


@pytest.mark.parametrize("temperature,window", [(24, 0), (19.5, 1)], ids=["hot", "cold-open-window"])
def test_new_observed_booking_gets_its_first_notice_in_the_existing_incident(temperature, window):
    notices = comfort_notices(
        [temperature] * 13, window=window,
        bookings=["B1"] * 6 + [None] + ["B2"] * 6,
        occupancies=["occupied"] * 6 + ["vacant"] + ["occupied"] * 6,
        transitions=[(6, "checkout", "B1"), (7, "checkin", "B2")])
    assert [record.timestamp_utc for record in notices] == [at(2), at(9)]
    assert_one_incident(notices)


def test_upward_severity_gets_one_update_without_reopening_the_incident():
    notices = comfort_notices([24] * 3 + [26] * 10)
    assert [(record.timestamp_utc, record.priority) for record in notices] == [
        (at(2), "medium"), (at(3), "high")]
    assert_one_incident(notices)


def test_high_medium_high_does_not_repeat_even_when_downgrade_follows_cooldown():
    # The first complete medium-only window is at 10:15, over one hour after
    # the high notice. The ledger alone would permit this different text.
    notices = comfort_notices([26] * 5 + [24] * 8 + [26] * 4)
    assert [(record.timestamp_utc, record.priority) for record in notices] == [(at(2), "high")]


def test_returning_booking_keeps_its_previous_notice_within_the_same_incident():
    # Returning B1 has changed medium-severity readings, so suppressing its
    # notice must not depend on an identical text fingerprint or cooldown.
    notices = comfort_notices(
        [24] * 12 + [24.5] * 6,
        bookings=["B1"] * 6 + [None] + ["B2"] * 4 + [None] + ["B1"] * 6,
        occupancies=["occupied"] * 6 + ["vacant"] + ["occupied"] * 4 + ["vacant"] + ["occupied"] * 6,
        transitions=[(6, "checkout", "B1"), (7, "checkin", "B2"),
                     (11, "checkout", "B2"), (12, "checkin", "B1")])
    assert [record.timestamp_utc for record in notices] == [at(2), at(9)]
    assert_one_incident(notices)


def test_newly_observed_booking_identity_is_distinct_from_unknown_recipient():
    notices = comfort_notices(
        [24] * 13, bookings=[None] * 3 + ["B1"] * 10,
        transitions=[(3, "checkin", "B1")])
    assert [record.timestamp_utc for record in notices] == [at(2), at(5)]
    assert_one_incident(notices)


def test_cold_to_hot_keeps_distinct_notice_kinds_inside_the_one_hour_cooldown():
    notices = comfort_notices([19.5] * 3 + [24] * 10, window=1)
    assert [(record.timestamp_utc, record.priority) for record in notices] == [
        (at(2), "medium"), (at(5), "medium")]
    assert "below" in notices[0].reason and "above" in notices[1].reason
    assert_one_incident(notices)


def test_confirmed_recovery_resets_notice_severity_for_a_new_incident():
    # Five healthy samples give three complete recovery windows. The next
    # physical incident may start at medium and later escalate to high again.
    notices = comfort_notices([26] * 3 + [21] * 5 + [24] * 3 + [26] * 2)
    assert [(record.timestamp_utc, record.priority) for record in notices] == [
        (at(2), "high"), (at(10), "medium"), (at(11), "high")]
    assert len({record.event_id for record in notices}) == 3
    assert notices[0].episode_id != notices[1].episode_id
    assert notices[1].episode_id == notices[2].episode_id
