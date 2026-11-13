"""S05 recovery requires evidence; unavailable readiness does not resolve demand."""
from copy import deepcopy

import pytest

from common.contracts.schemas import NormalizedEventV1
from engine.planner import DecisionEngine
from tests.engine.fixtures import BUILDING, FakeGateway, state, slot, tick

ROOMS = ("R101", "R102")


def timestamp(hour):
    return f"2026-01-06T{hour:02d}:00:00Z"


def setup_case(absent=False, enough_capacity=False):
    building = deepcopy(BUILDING)
    building["building"]["assets"] = []
    repo = FakeGateway()
    for index, room in enumerate(ROOMS, 1):
        repo.states[room] = [state(room=room, t=timestamp(9))]
        repo.bookings.append({
            "booking_id": f"B{index}", "room_id": room,
            "created_utc": "2026-01-05T00:00:00Z",
            "arrival_utc": timestamp(17),
            "departure_utc": "2026-01-07T10:00:00Z",
        })
    # A causally known eligible shift exists, but starts after both deadlines.
    repo.slots = [slot(start=timestamp(9 if enough_capacity else 18),
                       end=timestamp(20), status="sick" if absent else "scheduled")]
    return repo, DecisionEngine(repo, building)


def fresh(repo, hour, occupancy="vacant", cleaning="dirty"):
    for room in ROOMS:
        repo.states[room].append(state(room=room, t=timestamp(hour),
                                       occupancy=occupancy, cleaning=cleaning))


def run_step(engine, hour):
    return [row for row in engine.process(tick(timestamp(hour)))
            if row.issue_type == "occupancy_surge"]


def flags(rows):
    return [row for row in rows if row.action == "flag_anomaly"]


@pytest.mark.parametrize("gap", ["stale", "unknown_housekeeping", "unknown_occupancy"])
def test_unobserved_recovery_does_not_reopen_unchanged_surge(gap):
    repo, engine = setup_case()
    rows = run_step(engine, 9)
    for hour in (10, 11, 12):
        if gap == "unknown_housekeeping":
            fresh(repo, hour, cleaning="unknown")
        elif gap == "unknown_occupancy":
            fresh(repo, hour, occupancy="unknown")
        rows.extend(run_step(engine, hour))
    fresh(repo, 13)
    rows.extend(run_step(engine, 13))
    # Bookings, deadlines, shifts and observed work did not improve in the gap.
    assert len(flags(rows)) == 1
    assert not engine.rejections


def test_repeated_fresh_dirty_observations_are_deduplicated():
    repo, engine = setup_case()
    rows = run_step(engine, 9)
    for hour in (10, 11, 12, 13):
        fresh(repo, hour)
        rows.extend(run_step(engine, hour))
    assert len(flags(rows)) == 1
    assert sum(row.action == "notify_guest" for row in rows) == 2


def test_two_stale_scans_do_not_reopen():
    repo, engine = setup_case()
    rows = [row for hour in (9, 10, 11) for row in run_step(engine, hour)]
    fresh(repo, 12)
    rows.extend(run_step(engine, 12))
    assert len(flags(rows)) == 1


def test_known_absence_keeps_episode_open_across_stale_readiness():
    repo, engine = setup_case(absent=True)
    rows = [row for hour in (9, 10, 11, 12) for row in run_step(engine, hour)]
    fresh(repo, 13)
    rows.extend(run_step(engine, 13))
    assert len(flags(rows)) == 1


def test_three_fresh_clean_observations_allow_a_later_new_episode():
    repo, engine = setup_case()
    rows = run_step(engine, 9)
    for hour in (10, 11, 12):
        fresh(repo, hour, cleaning="clean")
        rows.extend(run_step(engine, hour))
    assert "surge:BUILDING" not in engine.episodes.active
    fresh(repo, 13)
    rows.extend(run_step(engine, 13))
    assert len(flags(rows)) == 2
    assert len({row.episode_id for row in flags(rows)}) == 2


def test_enough_known_capacity_does_not_trigger_surge():
    repo, engine = setup_case(enough_capacity=True)
    assert not flags(run_step(engine, 9))


def test_observed_retired_bookings_do_not_trigger_surge():
    repo, engine = setup_case(absent=True)
    for booking in repo.bookings:
        booking["observed_state"] = "checked_out"
    assert not flags(run_step(engine, 9))


def test_actual_human_completion_can_resolve_stale_dirty_readiness():
    repo, engine = setup_case()
    rows = run_step(engine, 9)
    for room, stamp in (("R101", "2026-01-06T09:45:00Z"),
                        ("R102", "2026-01-06T10:30:00Z")):
        raw = {"event_id": "completed:" + room, "timestamp_utc": stamp,
               "type": "cleaning_completed", "room_id": room,
               "resource_id": "CLEANER_1"}
        repo.events.append(raw)
        event = NormalizedEventV1(
            event_id="observed:" + room, timestamp_utc=stamp, available_at_utc=stamp,
            event_type="operational_event",
            source={"file": "events.jsonl", "record_id": raw["event_id"],
                    "timestamp_utc": stamp},
            entities={"room_id": room, "resource_id": "CLEANER_1"}, payload=raw)
        engine.process(event)
    for hour in (11, 12, 13):
        rows.extend(run_step(engine, hour))
    assert "surge:BUILDING" not in engine.episodes.active
    fresh(repo, 14)
    rows.extend(run_step(engine, 14))
    assert len(flags(rows)) == 2
    assert not engine.rejections


def test_restored_eligible_capacity_can_resolve_then_new_shortage_reopens():
    repo, engine = setup_case()
    rows = run_step(engine, 9)
    repo.slots = [slot(start=timestamp(10), end=timestamp(17),
                       status_known_utc=timestamp(10))]
    for hour in (10, 11, 12):
        fresh(repo, hour)
        rows.extend(run_step(engine, hour))
    assert "surge:BUILDING" not in engine.episodes.active
    repo.slots = [slot(start=timestamp(18), end=timestamp(20),
                       status_known_utc=timestamp(13))]
    fresh(repo, 13)
    rows.extend(run_step(engine, 13))
    assert len(flags(rows)) == 2
    assert not engine.rejections


def test_inconclusive_readiness_interrupts_partial_recovery():
    repo, engine = setup_case()
    rows = run_step(engine, 9)
    for hour in (10, 11):
        fresh(repo, hour, cleaning="clean")
        rows.extend(run_step(engine, hour))
    fresh(repo, 12, cleaning="unknown")
    rows.extend(run_step(engine, 12))
    fresh(repo, 13, cleaning="clean")
    rows.extend(run_step(engine, 13))
    # The clean scans on either side of an unknown gap are not one recovery streak.
    assert "surge:BUILDING" in engine.episodes.active
    fresh(repo, 14)
    rows.extend(run_step(engine, 14))
    assert len(flags(rows)) == 1
