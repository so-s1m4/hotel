"""Generic causal weak-flow controls; no production room IDs or repair labels."""
from datetime import timedelta
from types import SimpleNamespace

import pytest

from engine.core import EngineConfig, instant, utc
from engine.detectors.weak_water import PersistentWeakFlowDetector

ROOM = "ROOM_TEST"
START = "2026-02-10T23:00:00Z"  # 00:00 local; four hours fit the unchanged night gate.


def feed(deltas, *, start=START, eligibility=None, missing=None, detector=None):
    detector = detector or PersistentWeakFlowDetector(EngineConfig())
    total = 100.0
    at = instant(start)
    flags = []
    for i, delta in enumerate(deltas):
        before = total
        total += delta
        end = at+timedelta(minutes=15)
        pair = [{"timestamp_utc": utc(at), "room_id": ROOM, "meter_total_l": before},
                {"timestamp_utc": utc(end), "room_id": ROOM, "meter_total_l": None if missing == i else total}]
        flag = detector.observe(ROOM, pair, utc(end), eligible=eligibility[i] if eligibility else True,
                                eligibility_evidence=[f"room_telemetry.csv:{ROOM}:{utc(at)}..{utc(end)}"])
        if flag:
            flags.append(flag)
        at = end
    return detector, flags


def test_persistent_baseline_free_flow_is_an_uncertain_review_after_four_hours_only():
    detector, flags = feed([1.1]*16)
    assert len(flags) == 1
    flag = flags[0]
    assert flag.action == "flag_anomaly" and flag.issue == "water_low_rate_review"
    assert flag.key == "water:"+ROOM and flag.variant == "initial"
    assert "not a confirmed leak" in flag.reason and "drift/bias" in flag.reason
    assert flag.context["weak_water_metrics"]["baseline_used"] is False
    assert flag.context["weak_water_metrics"]["net_liters"] == pytest.approx(17.6)
    assert detector.diagnostics["review_windows"] == 1
    assert not feed([1.1]*15)[1]


def test_gradually_increasing_weak_flow_does_not_need_a_false_quiet_baseline():
    _, flags = feed([0.6]*4+[0.9]*4+[1.2]*4+[1.5]*4)
    assert len(flags) == 1
    assert flags[0].context["weak_water_metrics"]["net_l_per_interval"] == pytest.approx(1.05)


def test_small_counter_reversals_are_signed_not_positive_clamped():
    values = [1.25]*16
    values[1] = values[2] = -0.25
    _, flags = feed(values)
    assert len(flags) == 1
    metrics = flags[0].context["weak_water_metrics"]
    assert metrics["net_liters"] == pytest.approx(17.0)
    assert metrics["negative_liters"] == pytest.approx(0.5)
    assert metrics["negative_intervals"] == 2


@pytest.mark.parametrize("deltas", [[0]*16, [0.2, -0.2]*8, [0, 0, 0, 18]+[0]*12, [1.1]*7+[-10]+[1.1]*8])
def test_zero_rounding_tap_burst_or_large_reset_cannot_look_like_persistent_flow(deltas):
    assert not feed(deltas)[1]


def test_sparse_hourly_taps_or_coarse_quantization_do_not_prove_distributed_flow():
    detector, flags = feed([4.4, 0, 0, 0]*4)
    assert not flags
    assert detector.diagnostics["sparse_or_quantized_progress"] == 1


def test_missing_sample_and_ineligible_operation_break_run_without_interpolation():
    assert not feed([1.1]*16, missing=7)[1]
    assert not feed([1.1]*16, eligibility=[True]*7+[False]+[True]*8)[1]


def test_daytime_eligibility_cannot_bypass_the_local_night_requirement():
    detector, flags = feed([1.1]*16, start="2026-02-11T10:00:00Z")
    assert not flags
    assert detector.diagnostics["ineligible_intervals"] == 16


def test_four_hour_night_path_is_reachable_during_spring_dst_transition():
    # 00:00 local -> 05:00 local contains the spring skipped hour but four real hours.
    _, flags = feed([1.1]*16, start="2026-03-28T23:00:00Z")
    assert len(flags) == 1


def test_repeated_identical_tick_cannot_accumulate_evidence_twice():
    detector, _ = feed([1.1]*8)
    now = utc(instant(START)+timedelta(minutes=15*8))
    pair = [{"timestamp_utc": utc(instant(now)-timedelta(minutes=15)), "room_id": ROOM, "meter_total_l": 107.7},
            {"timestamp_utc": now, "room_id": ROOM, "meter_total_l": 108.8}]
    # Use exact original arithmetic endpoint to avoid manufacturing a precision conflict.
    last = detector._history[ROOM][-1]
    pair[0]["meter_total_l"], pair[1]["meter_total_l"] = last.before, last.after
    for _ in range(20):
        assert detector.observe(ROOM, pair, now, eligible=True) is None
    assert len(detector._history[ROOM]) == 8


def test_future_endpoint_and_conflicting_duplicate_endpoint_are_not_evidence():
    detector = PersistentWeakFlowDetector(EngineConfig())
    at = instant(START)
    now = utc(at+timedelta(minutes=15))
    pair = [{"timestamp_utc": utc(at), "room_id": ROOM, "meter_total_l": 100},
            {"timestamp_utc": utc(at+timedelta(minutes=30)), "room_id": ROOM, "meter_total_l": 102}]
    assert detector.observe(ROOM, pair, now, eligible=True) is None
    assert detector.diagnostics["pair_invalid"] == 1
    pair[1]["timestamp_utc"] = now
    pair.append({**pair[1], "meter_total_l": 105})
    detector = PersistentWeakFlowDetector(EngineConfig())
    assert detector.observe(ROOM, pair, now, eligible=True) is None
    assert detector.diagnostics["conflicting_duplicate_pair"] == 1


@pytest.mark.parametrize("window", [0, 180, 241, 360, 420, True])
def test_configured_persistence_cannot_silently_require_an_unreachable_ordinary_night(window):
    with pytest.raises(ValueError):
        PersistentWeakFlowDetector(SimpleNamespace(water_weak_persistent_window_minutes=window))
