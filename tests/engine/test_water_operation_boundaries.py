"""Causal interval endpoints at inclusive known-operation expiry boundaries.

Synthetic dates and room IDs are independent of the supplied Dev witnesses.
No repair outcome, completed status, future timestamp or gap is used as a label.
"""
from datetime import timedelta

import pytest

from engine.core import EngineConfig, instant, utc
from engine.detectors.water import WaterDetector
from tests.engine.fixtures import BUILDING, FakeGateway, state, tick

CURRENT_NIGHT = "2026-02-10T23:00:00Z"  # 00:00 Europe/Vienna.
ORDER = {
    "work_order_id": "WO_BOUNDARY",
    "asset_id": "R101",
    "created_utc": "2026-02-10T18:00:00Z",
    "scheduled_utc": "2026-02-10T21:45:00Z",  # 22:45 local.
    "summary": "Known tap flushing on this room branch",
}


def scenario(*, prior_quiet_night=False, missing_at=None, housekeeping_unknown_at=None, flow_per_interval=1.1):
    repo = FakeGateway()
    visits = []
    counter = 100.0
    nights = []
    if prior_quiet_night:
        nights.append(("2026-02-09T23:00:00Z", 12, 0.0))
    nights.append((CURRENT_NIGHT, 23, flow_per_interval))
    for start, intervals, flow in nights:
        at = instant(start)
        points = [(utc(at), counter)]
        for _ in range(intervals):
            at += timedelta(minutes=15)
            counter += flow
            visits.append(utc(at))
            points.append((utc(at), counter))
        for stamp, value in points:
            repo.measurements.setdefault("room_water.csv", []).append({
                "timestamp_utc": stamp, "room_id": "R101",
                "meter_total_l": None if stamp == missing_at else value,
            })
            repo.states.setdefault("R101", []).append(state(
                t=stamp, cleaning=None if stamp == housekeeping_unknown_at else "clean"))
    return repo, visits


def review_times(repo, visits, orders):
    detector = WaterDetector(EngineConfig())
    proposals = []
    for stamp in visits:
        result = detector.detect(tick(stamp), repo, BUILDING, orders)
        proposals.extend((stamp, candidate) for candidate in result.candidates
                         if candidate.issue == "water_low_rate_review")
    return detector, proposals


def test_operation_expiry_is_inclusive_and_the_next_interval_starts_after_it():
    # The inclusive boundary is an intentional contract; caller interval checks
    # must not silently redefine it to make the first review appear earlier.
    assert WaterDetector._known_work("R101", "2026-02-10T23:45:00Z", [ORDER])
    assert not WaterDetector._known_work("R101", "2026-02-11T00:00:00Z", [ORDER])


def test_persistent_review_needs_four_full_hours_after_both_endpoints_clear_operation():
    repo, visits = scenario()
    _, proposals = review_times(repo, visits, [ORDER])
    assert proposals
    # 00:45 local is still excluded. The first eligible pair is 01:00→01:15,
    # so the sixteen-pair window is 01:00→05:00 local, not 00:45→04:45.
    stamp, candidate = proposals[0]
    assert stamp == "2026-02-11T04:00:00Z"
    assert candidate.context["weak_water_metrics"]["window_minutes"] == 240
    assert candidate.evidence[0] == "room_water.csv:R101:2026-02-11T00:00:00Z..2026-02-11T04:00:00Z"
    assert all(at != "2026-02-11T03:45:00Z" for at, _ in proposals)


def test_quiet_baseline_review_also_excludes_the_entire_boundary_pair():
    repo, visits = scenario(prior_quiet_night=True)
    _, proposals = review_times(repo, visits, [ORDER])
    assert proposals
    stamp, candidate = proposals[0]
    assert stamp == "2026-02-11T03:00:00Z"  # 01:00→04:00 local, three complete hours.
    assert "180 min" in candidate.reason
    assert any("earlier_night_baseline" in ref for ref in candidate.evidence)
    assert "room_water.csv:R101:2026-02-11T00:00:00Z..2026-02-11T03:00:00Z" in candidate.evidence
    assert all(at != "2026-02-11T02:45:00Z" for at, _ in proposals)


@pytest.mark.parametrize("changes", [
    {"created_utc": "2026-02-11T06:00:00Z"},
    {"asset_id": "R102", "summary": "Known tap flushing only on R102"},
    {"scheduled_utc": "2026-02-11T05:00:00Z"},
])
def test_unavailable_other_room_or_future_operation_does_not_rewrite_current_eligibility(changes):
    repo, visits = scenario()
    order = {**ORDER, **changes}
    _, proposals = review_times(repo, visits, [order])
    _, without_order = review_times(repo, visits, [])
    assert [(at, c.reason, c.evidence) for at, c in proposals] == [
        (at, c.reason, c.evidence) for at, c in without_order]
    assert proposals[0][0] == "2026-02-11T03:00:00Z"  # 00:00→04:00 local.


@pytest.mark.parametrize("missing_meter,unknown_housekeeping", [(True, False), (False, True)])
def test_invalid_first_clear_endpoint_delays_until_a_complete_new_window(missing_meter, unknown_housekeeping):
    boundary = "2026-02-11T00:00:00Z"  # First potential post-operation start, 01:00 local.
    repo, visits = scenario(missing_at=boundary if missing_meter else None,
                            housekeeping_unknown_at=boundary if unknown_housekeeping else None)
    _, proposals = review_times(repo, visits, [ORDER])
    assert proposals
    assert proposals[0][0] == "2026-02-11T04:15:00Z"
    assert proposals[0][1].evidence[0] == "room_water.csv:R101:2026-02-11T00:15:00Z..2026-02-11T04:15:00Z"


def test_new_observed_cleaning_after_expiry_prevents_reusing_an_old_eligible_run():
    repo, visits = scenario()
    repo.events = [{"event_id": "E_BOUNDARY_CLEAN", "timestamp_utc": "2026-02-11T00:30:00Z",
                    "type": "cleaning_started", "room_id": "R101"}]
    _, proposals = review_times(repo, visits, [ORDER])
    # The observed operation plus the two-hour exclusion leaves insufficient
    # continuous eligible night time for either low-rate review branch.
    assert proposals == []


def test_strong_flow_review_requires_every_reading_endpoint_clear_of_known_operation():
    repo, visits = scenario(flow_per_interval=5.0)
    detector = WaterDetector(EngineConfig())
    proposals = []
    for stamp in visits:
        result = detector.detect(tick(stamp), repo, BUILDING, [ORDER])
        proposals.extend((stamp, candidate) for candidate in result.candidates
                         if candidate.issue == "water_leak")
    assert proposals
    # The full 60-minute window must start at 01:00 local, after the inclusive
    # 00:45 endpoint exclusion. An end-only check could falsely accept 00:00→01:00.
    stamp, candidate = proposals[0]
    assert stamp == "2026-02-11T01:00:00Z"  # 02:00 local.
    assert candidate.action == "request_inspection"
    assert candidate.evidence[0] == "room_water.csv:R101:2026-02-11T00:00:00Z..2026-02-11T01:00:00Z"
    assert all(instant(at) >= instant("2026-02-11T01:00:00Z") for at, _ in proposals)
