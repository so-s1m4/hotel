"""Independent controls for persistent weak-water review, without repair labels."""
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace

import pytest

from engine.core import instant, utc
from engine.detectors.weak_water import PersistentWeakFlowDetector
from engine.detectors.water import meter_deltas

START = "2026-01-04T23:00:00Z"  # Vienna00:00, winter; sixteen intervals end04:00 local


def points(deltas, start=START, initial=100.0):
    rows = [{"room_id": "R101", "timestamp_utc": start, "meter_total_l": initial}]
    value = initial
    for i, delta in enumerate(deltas, 1):
        value += delta
        rows.append({"room_id": "R101", "timestamp_utc": utc(instant(start)+timedelta(minutes=15*i)), "meter_total_l": value})
    return rows


def detector(**overrides):
    settings = dict(water_low_min_l_per_interval=1.0, water_min_l_per_interval=3.0,
                    water_weak_persistent_window_minutes=240, water_weak_max_backstep_l=0.5)
    settings.update(overrides)
    return PersistentWeakFlowDetector(SimpleNamespace(**settings))


def run(rows, *, eligibility=None, skip=None, d=None):
    d = d or detector()
    candidates = []
    for i in range(1, len(rows)):
        if skip and i in skip:
            continue
        candidate = d.observe("R101", rows[max(0, i-4):i+1], rows[i]["timestamp_utc"],
                              eligible=True if eligibility is None else eligibility[i-1],
                              eligibility_evidence=[f"room_telemetry.csv:R101:{rows[i]['timestamp_utc']}"])
        if candidate:
            candidates.append(candidate)
    return d, candidates


def test_baseline_free_fallback_requires_four_hours_and_remains_review_only():
    rows = points([1.1]*16)
    _, short = run(rows[:-1])
    _, complete = run(rows)
    assert not short
    assert len(complete) == 1
    candidate = complete[0]
    assert candidate.action == "flag_anomaly" and candidate.issue == "water_low_rate_review"
    assert candidate.key == "water:R101"
    assert "240" in candidate.reason or "4 h" in candidate.reason
    assert "bias" in candidate.reason.casefold() or "drift" in candidate.reason.casefold()
    assert "confirmed" not in candidate.reason.casefold() or "not" in candidate.reason.casefold()
    assert any(rows[0]["timestamp_utc"] in ref and rows[-1]["timestamp_utc"] in ref for ref in candidate.evidence)


@pytest.mark.parametrize("deltas", [
    [0.0]*16,
    [1.1, -1.1]*8,
    [0.1, -0.1]*8,
    [0.0]*7+[17.6]+[0.0]*8,
    [17.6]+[0.0]*15,
    [0.0]*15+[17.6],
    [4.0,0.0,0.0,0.0]*4,
])
def test_quiet_oscillating_rounding_and_single_tap_bursts_are_not_persistent_review(deltas):
    assert not run(points(deltas))[1]


def test_bounded_signed_backsteps_never_become_zero_filled_consumption():
    deltas = [1.25]*16
    deltas[4] = deltas[12] = -0.25
    rows = points(deltas)
    before = deepcopy(rows)
    _, candidates = run(rows)
    assert candidates and candidates[0].action == "flag_anomaly"
    metrics = candidates[0].context["weak_water_metrics"]
    assert metrics["net_liters"] == pytest.approx(sum(deltas))
    assert metrics["net_l_per_interval"] == pytest.approx(sum(deltas)/16)
    assert metrics["net_liters"] < sum(max(0, delta) for delta in deltas)
    assert rows == before
    # The existing strict meter/aggregate path must remain invalid for a signed reversal.
    assert meter_deltas(rows) is None


@pytest.mark.parametrize("change", ["missing", "gap", "large_drop", "invalid_nan", "negative_counter", "conflicting_duplicate"])
def test_invalid_readings_and_reset_like_discontinuities_break_evidence(change):
    rows = points([1.1]*16)
    d = detector()
    candidates = []
    for i in range(1, len(rows)):
        window = deepcopy(rows[max(0, i-4):i+1])
        now = rows[i]["timestamp_utc"]
        if i == 8:
            if change == "missing": window[-1]["meter_total_l"] = None
            elif change == "gap":
                window[-2]["timestamp_utc"] = utc(instant(window[-2]["timestamp_utc"])-timedelta(minutes=1))
            elif change == "large_drop": window[-1]["meter_total_l"] = 1.0
            elif change == "invalid_nan": window[-1]["meter_total_l"] = float("nan")
            elif change == "negative_counter": window[-1]["meter_total_l"] = -1
            elif change == "conflicting_duplicate":
                window.append({**window[-1], "meter_total_l": 999})
        candidate = d.observe("R101", window, now, eligible=True)
        if candidate:
            candidates.append(candidate)
    assert not candidates


@pytest.mark.parametrize("skip", [{8}, {7,8}])
def test_unobserved_ticks_cannot_be_interpolated_into_continuous_evidence(skip):
    assert not run(points([1.1]*16), skip=skip)[1]


@pytest.mark.parametrize("ineligible_index", [1, 8, 16])
def test_single_unknown_occupied_dirty_cleaning_or_transition_interval_breaks_run(ineligible_index):
    eligibility = [True]*16
    eligibility[ineligible_index-1] = False
    assert not run(points([1.1]*16), eligibility=eligibility)[1]


def test_repeated_same_tick_cannot_manufacture_four_hours_of_observations():
    rows = points([1.1]*4)
    d = detector()
    candidates = []
    for i in range(1, len(rows)):
        for _ in range(10):
            candidate = d.observe("R101", rows[max(0, i-4):i+1], rows[i]["timestamp_utc"], eligible=True)
            if candidate:
                candidates.append(candidate)
    assert not candidates


def test_future_only_counter_pair_cannot_influence_current_time():
    rows = points([1.1]*16)
    d = detector()
    for i in range(1,16):
        assert d.observe("R101", rows[i-1:i+1], rows[i]["timestamp_utc"], eligible=True) is None
    assert d.observe("R101", rows[-2:], rows[-2]["timestamp_utc"], eligible=True) is None


def test_daytime_or_cross_night_readings_do_not_override_eligible_flag():
    daytime = points([1.1]*16, start="2026-01-05T09:00:00Z")
    assert not run(daytime)[1]
    # A four-hour window beginning before localmidnight may not borrow daytime data.
    boundary = points([1.1]*16, start="2026-01-04T21:00:00Z")
    assert not run(boundary)[1]


def test_constant_linear_meter_drift_is_not_claimed_as_verified_fixture_leak():
    # This is observationally identical to a real constant flow: a review may be
    # appropriate, but no detector can identify the cause from these readings alone.
    _, candidates = run(points([1.1]*16))
    assert candidates
    assert all(c.action == "flag_anomaly" for c in candidates)
    assert all("bias" in c.reason.casefold() or "drift" in c.reason.casefold() or "calibrat" in c.reason.casefold() for c in candidates)
    assert all(not c.human and not c.consent and c.resource is None for c in candidates)


def test_rooms_cannot_share_evidence_or_complete_each_others_history():
    d = detector()
    rows = points([1.1]*16)
    for i in range(1,9):
        assert d.observe("R101", rows[i-1:i+1], rows[i]["timestamp_utc"], eligible=True) is None
    for i in range(9,17):
        other = [{**r, "room_id": "R102"} for r in rows[i-1:i+1]]
        assert d.observe("R102", other, rows[i]["timestamp_utc"], eligible=True) is None


def test_partial_windows_on_separate_nights_cannot_be_stitched_together():
    d = detector()
    first = points([1.1]*8)
    second = points([1.1]*8, start="2026-01-05T23:00:00Z", initial=first[-1]["meter_total_l"])
    assert not run(first, d=d)[1]
    assert not run(second, d=d)[1]


def integrated_run(deltas, *, occupancy="vacant", cleaning="clean", events=None):
    from engine.core import EngineConfig
    from engine.detectors.water import WaterDetector
    from tests.engine.fixtures import BUILDING, FakeGateway, state, tick
    rows = points(deltas)
    repo = FakeGateway()
    repo.measurements["room_water.csv"] = rows
    repo.states["R101"] = [state(t=row["timestamp_utc"], occupancy=occupancy, cleaning=cleaning) for row in rows]
    repo.events = events or []
    d = WaterDetector(EngineConfig())
    candidates = []
    for row in rows[1:]:
        candidates += d.detect(tick(row["timestamp_utc"]), repo, BUILDING, []).candidates
    return d, candidates


def test_real_water_caller_does_not_gate_signed_weak_review_on_strict_delta_success():
    deltas = [1.25]*16
    deltas[4] = deltas[12] = -0.25
    d, candidates = integrated_run(deltas)
    reviews = [c for c in candidates if c.issue == "water_low_rate_review"]
    assert len(reviews) == 1 and reviews[0].action == "flag_anomaly"
    assert reviews[0].context["weak_water_metrics"]["net_liters"] == pytest.approx(17.0)
    assert d.diagnostics["main_incomplete_room_windows"] > 0
    assert not any(c.issue == "water_balance" for c in candidates)


@pytest.mark.parametrize("state_args", [{"occupancy": "occupied"}, {"occupancy": "unknown"}, {"cleaning": "dirty"}, {"cleaning": "in_progress"}])
def test_real_water_caller_requires_fresh_vacancy_and_cleanliness_for_full_run(state_args):
    assert not integrated_run([1.1]*16, **state_args)[1]


def test_real_water_caller_known_cleaning_breaks_the_four_hour_evidence_run():
    events = [{"event_id": "C1", "timestamp_utc": "2026-01-05T01:00:00Z", "type": "cleaning_started", "room_id": "R101"}]
    assert not integrated_run([1.1]*16, events=events)[1]


@pytest.mark.parametrize("start", ["2026-03-28T23:00:00Z", "2026-10-24T22:00:00Z"])
def test_default_four_elapsed_hours_fit_both_dst_nights_without_fabricated_samples(start):
    rows = points([1.1]*16, start=start)
    _, candidates = run(rows)
    assert len(candidates) == 1
    assert candidates[0].context["weak_water_metrics"]["intervals"] == 16
    assert candidates[0].context["weak_water_metrics"]["window_minutes"] == 240
