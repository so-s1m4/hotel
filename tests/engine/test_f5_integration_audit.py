"""Independent end-to-end invariants across all room-water decision paths."""
from datetime import timedelta
import json

import pytest

from engine.core import instant, utc
from engine.decisions import write_decisions
from engine.planner import DecisionEngine
from tests.engine.fixtures import BUILDING, FakeGateway, state, tick

START = "2026-01-04T23:00:00Z"  # localmidnight before first evaluated night


def source(deltas, start=START):
    repo = FakeGateway()
    value = 100.0
    water = [{"room_id": "R101", "timestamp_utc": start, "meter_total_l": value}]
    for index, delta in enumerate(deltas, 1):
        value += delta
        water.append({"room_id": "R101", "timestamp_utc": utc(instant(start)+timedelta(minutes=15*index)), "meter_total_l": value})
    repo.measurements["room_water.csv"] = water
    repo.states["R101"] = [state(t=row["timestamp_utc"], occupancy="vacant", cleaning="clean") for row in water]
    return repo, water


def replay(repo, water):
    engine = DecisionEngine(repo, BUILDING)
    decisions = []
    for row in water[1:]:
        decisions += engine.process(tick(row["timestamp_utc"]))
    assert not engine.rejections
    return engine, decisions


def reviews(decisions):
    return [row for row in decisions if row.issue_type == "water_low_rate_review"]


def test_persistent_review_recovers_only_after_observed_zero_then_new_episode():
    # First review at04:00local; quiet observations then cover the remainder of
    # the day, so a second night cannot inherit an unresolved first-night episode.
    repo, water = source([1.1]*16 + [0.0]*80 + [1.1]*16)
    engine, decisions = replay(repo, water)
    findings = reviews(decisions)
    assert len(findings) == 2
    assert findings[0].episode_id != findings[1].episode_id
    assert findings[0].event_id != findings[1].event_id
    assert findings[0].timestamp_utc == water[16]["timestamp_utc"]
    assert findings[1].timestamp_utc == water[-1]["timestamp_utc"]
    assert all(row.action == "flag_anomaly" for row in findings)
    assert engine.water.healthy_counts["water:R101"] == 0


def test_quiet_baseline_review_and_four_hour_persistence_share_one_decision_episode():
    # A full prior day is observed, not imported from a future repair narrative.
    repo, water = source([0.0]*96 + [1.1]*20)
    engine, decisions = replay(repo, water)
    findings = reviews(decisions)
    assert len(findings) == 1
    assert "earlier-night baseline" in findings[0].reason
    assert findings[0].timestamp_utc == water[96+12]["timestamp_utc"]
    # The independent four-hour path saw enough evidence, but did not create a
    # second flag_anomaly while the same water episode remained active.
    assert engine.water.weak.diagnostics["review_windows"] > 0
    assert len({row.episode_id for row in findings}) == 1


def test_signed_endpoint_metrics_are_serialized_exactly_and_do_not_extend_official_schema(tmp_path):
    deltas = [1.25]*16
    deltas[4] = deltas[12] = -0.25
    repo, water = source(deltas)
    engine, decisions = replay(repo, water)
    finding, = reviews(decisions)
    row = next(row for row in engine.executor.audit if row["decision_id"] == finding.event_id)
    # The actual JSON audit format must preserve raw signed accounting, not just
    # display a plausible rounded number in the decision reason.
    encoded = json.loads(json.dumps(row, allow_nan=False))
    metrics = encoded["parameters"]["weak_water_metrics"]
    assert metrics["net_liters"] == pytest.approx(17.0)
    assert metrics["net_l_per_interval"] == pytest.approx(17.0/16)
    assert metrics["negative_intervals"] == 2
    assert metrics["negative_liters"] == pytest.approx(0.5)
    assert metrics["baseline_used"] is False
    assert row["effects_applied"] is False and row["observed_history_changed"] is False
    output = tmp_path/"decisions.jsonl"
    write_decisions(decisions, output)
    official = [json.loads(line) for line in output.read_text().splitlines()]
    assert all("weak_water_metrics" not in row for row in official)
    assert "17.00 L" in finding.reason


def test_persistent_weak_review_escalates_only_after_a_full_strong_window():
    repo, water = source([1.1]*16 + [4.0]*5)
    _, decisions = replay(repo, water)
    flag, = reviews(decisions)
    inspections = [row for row in decisions if row.action == "request_inspection" and row.issue_type == "water_leak"]
    assert len(inspections) == 1
    inspection = inspections[0]
    assert inspection.timestamp_utc == water[20]["timestamp_utc"]
    assert flag.episode_id == inspection.episode_id
    assert inspection.priority == "high"
    assert "4.0–4.0" in inspection.reason
    assert all(row.action == "flag_anomaly" for row in decisions if row.timestamp_utc < inspection.timestamp_utc)


def test_duplicate_ticks_do_not_accelerate_persistence_or_healthy_recovery():
    repo, water = source([1.1]*16 + [0.0]*6)
    engine = DecisionEngine(repo, BUILDING)
    findings = []
    for row in water[1:]:
        event = tick(row["timestamp_utc"])
        findings += engine.process(event)
        assert engine.process(event) == []
    assert len(reviews(findings)) == 1
    assert engine.event_count == len(water)-1
    assert "water:R101" not in engine.episodes.active


def test_future_counter_and_future_quiet_baseline_do_not_shorten_review_evidence():
    repo, water = source([1.1]*16 + [0.0]*96)
    repo.leak_future_rows = True  # adversarial gateway returns measurements beyond as_of
    engine = DecisionEngine(repo, BUILDING)
    decisions = []
    for row in water[1:17]:
        decisions += engine.process(tick(row["timestamp_utc"]))
    finding, = reviews(decisions)
    assert finding.timestamp_utc == water[16]["timestamp_utc"]
    assert "No quiet prior baseline is assumed" in finding.reason
    metrics = next(row for row in engine.executor.audit if row["decision_id"] == finding.event_id)["parameters"]["weak_water_metrics"]
    assert metrics["baseline_used"] is False and metrics["intervals"] == 16


def test_rejected_future_event_does_not_mutate_water_history_or_event_counts():
    repo, water = source([1.1]*16)
    engine = DecisionEngine(repo, BUILDING)
    for row in water[1:16]:
        assert engine.process(tick(row["timestamp_utc"])) == []
    event = tick(water[16]["timestamp_utc"], event_id="tick:bad-future")
    event.timestamp_utc = utc(instant(event.available_at_utc)+timedelta(minutes=15))
    with pytest.raises(ValueError, match="Future"):
        engine.process(event)
    assert engine.event_count == 15
    finding, = reviews(engine.process(tick(water[16]["timestamp_utc"])))
    assert finding.timestamp_utc == water[16]["timestamp_utc"]
    assert engine.event_count == 16


def test_distinct_same_time_clock_ids_do_not_manufacture_extra_water_intervals():
    repo, water = source([1.1]*16)
    engine = DecisionEngine(repo, BUILDING)
    decisions = []
    for row in water[1:]:
        original = tick(row["timestamp_utc"])
        decisions += engine.process(original)
        decisions += engine.process(tick(row["timestamp_utc"], event_id=original.event_id+"~duplicate"))
    assert len(reviews(decisions)) == 1
    assert engine.water.diagnostics["ticks_processed"] == 16
    finding, = reviews(decisions)
    audit = next(row for row in engine.executor.audit if row["decision_id"] == finding.event_id)
    assert audit["parameters"]["weak_water_metrics"]["intervals"] == 16


def test_strong_flow_dropping_to_persistent_weak_flow_is_not_recovery_or_new_incident():
    # There is no quiet interval or fixture repair: only a reduction below the
    # strong threshold. An already-requested inspection must not be downgraded
    # into another weaker review or split into an unrelated episode.
    repo, water = source([4.0]*4 + [1.1]*17)
    engine, decisions = replay(repo, water)
    inspections = [row for row in decisions if row.issue_type == "water_leak" and row.action == "request_inspection"]
    assert len(inspections) == 1
    assert not reviews(decisions)
    assert len({row.episode_id for row in decisions if row.issue_type.startswith("water_")}) == 1
    assert "water:R101" in engine.episodes.active
    assert engine.water.healthy_counts["water:R101"] == 0


@pytest.mark.parametrize("rate,expected_inspections,expected_reviews", [(0.99,0,0), (2.99,0,1), (3.0,1,0)])
def test_weak_extension_does_not_lower_the_strong_inspection_floor(rate, expected_inspections, expected_reviews):
    repo, water = source([rate]*16)
    _, decisions = replay(repo, water)
    assert sum(row.action == "request_inspection" for row in decisions) == expected_inspections
    assert len(reviews(decisions)) == expected_reviews
    if expected_inspections:
        assert next(row for row in decisions if row.action == "request_inspection").timestamp_utc == water[4]["timestamp_utc"]


def test_observed_quiet_recovery_after_strong_incident_allows_later_weak_review():
    repo, water = source([4.0]*4 + [0.0]*92 + [1.1]*16)
    _, decisions = replay(repo, water)
    inspection, = [row for row in decisions if row.action == "request_inspection" and row.issue_type == "water_leak"]
    later, = reviews(decisions)
    assert inspection.episode_id != later.episode_id
    assert later.timestamp_utc > inspection.timestamp_utc
    assert later.action == "flag_anomaly"


def test_first_weak_and_strong_qualification_same_tick_only_requests_strong_inspection():
    # Twelve weak quarters are insufficient for baseline-free persistence. The
    # next four strong quarters complete both the four-hour review window and
    # the first strong hour at the exact same tick. The weaker flag adds no new
    # action when the stronger evidence already justifies an inspection request.
    repo, water = source([1.1]*12+[4.0]*4)
    _, decisions = replay(repo, water)
    assert len(decisions) == 1
    assert decisions[0].action == "request_inspection"
    assert decisions[0].issue_type == "water_leak"
    assert decisions[0].timestamp_utc == water[-1]["timestamp_utc"]


def replace_with_decimal_counter_values(repo, water, initial, increment):
    # Directly constructed endpoints model independent CSV decimal parsing,
    # unlike repeated binary addition of a fixture delta.
    for index, row in enumerate(water):
        row["meter_total_l"] = initial+increment*index
    repo.measurements["room_water.csv"] = water


@pytest.mark.parametrize("rate", [3.0,100.0])
def test_decimal_counter_values_at_strong_inclusive_floor_and_ceiling_qualify_at_one_hour(rate):
    repo, water = source([rate]*4)
    replace_with_decimal_counter_values(repo, water, 120.2, rate)
    _, decisions = replay(repo, water)
    inspection, = [row for row in decisions if row.action == "request_inspection"]
    assert inspection.timestamp_utc == water[4]["timestamp_utc"]
    assert not reviews(decisions)


@pytest.mark.parametrize("rate", [3.0-1e-8,100.0+1e-8])
def test_values_really_outside_strong_band_are_not_admitted_by_numeric_tolerance(rate):
    repo, water = source([rate]*4)
    replace_with_decimal_counter_values(repo, water, 120.2, rate)
    _, decisions = replay(repo, water)
    assert not any(row.action == "request_inspection" for row in decisions)


@pytest.mark.parametrize("continued_rate,should_resolve", [(0.5-0.5e-9,False),(0.5-1e-8,True)])
def test_strict_quiet_recovery_uses_tight_boundary_without_changing_signed_samples(continued_rate,should_resolve):
    repo, water = source([1.1]*16+[continued_rate]*8)
    raw = [row["meter_total_l"] for row in water]
    engine, decisions = replay(repo, water)
    assert len(reviews(decisions)) == 1
    assert ("water:R101" not in engine.episodes.active) is should_resolve
    assert [row["meter_total_l"] for row in water] == raw
