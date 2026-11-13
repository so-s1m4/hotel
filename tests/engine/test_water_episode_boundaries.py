"""Read-only independent checks around strong/weak water episode transitions."""
from datetime import timedelta

from engine.core import instant, utc
from engine.planner import DecisionEngine
from tests.engine.fixtures import BUILDING, FakeGateway, state, tick

START = "2026-02-10T23:00:00Z"  # local midnight, with no production witness ID.


def source(room_traces, *, missing=None):
    repo = FakeGateway()
    points = []
    for room, deltas in room_traces.items():
        value = 100.0
        rows = [{"room_id": room, "timestamp_utc": START, "meter_total_l": value}]
        for i, delta in enumerate(deltas, 1):
            value += delta
            stamp = utc(instant(START)+timedelta(minutes=15*i))
            rows.append({"room_id": room, "timestamp_utc": stamp,
                         "meter_total_l": None if missing and (room, i) in missing else value})
        repo.measurements.setdefault("room_water.csv", []).extend(rows)
        repo.states[room] = [state(room=room, t=r["timestamp_utc"], cleaning="clean") for r in rows]
        if len(rows) > len(points):
            points = rows
    return repo, points


def water_records(records):
    return [r for r in records if r.issue_type in {"water_leak", "water_low_rate_review"}]


def test_reduced_then_returning_strong_flow_without_quiet_is_the_same_incident():
    repo, points = source({"R101": [4.0]*4+[1.1]*12+[4.0]*4})
    engine = DecisionEngine(repo, BUILDING)
    records = []
    for row in points[1:]:
        records.extend(engine.process(tick(row["timestamp_utc"])))
    findings = water_records(records)
    assert len(findings) == 1
    assert findings[0].action == "request_inspection"
    assert findings[0].timestamp_utc == points[4]["timestamp_utc"]
    assert "water:R101" in engine.episodes.active
    assert engine.water.healthy_counts["water:R101"] == 0
    assert engine.water.diagnostics["weak_review_suppressed_by_strong_episode"] > 0


def test_missing_tick_or_meter_during_reduced_flow_cannot_manufacture_recovery():
    for missing_meter, skipped_tick in [(True, False), (False, True)]:
        repo, points = source({"R101": [4.0]*4+[1.1]*16}, missing={("R101", 8)} if missing_meter else None)
        engine = DecisionEngine(repo, BUILDING)
        records = []
        for index, row in enumerate(points[1:], 1):
            if skipped_tick and index == 8:
                continue
            records.extend(engine.process(tick(row["timestamp_utc"])))
        findings = water_records(records)
        assert len(findings) == 1 and findings[0].action == "request_inspection"
        assert "water:R101" in engine.episodes.active
        assert engine.water.healthy_counts["water:R101"] == 0


def test_one_rooms_new_strong_candidate_does_not_remove_another_rooms_weak_review():
    # The weak R101 candidate is already present when sorted room processing
    # reaches R102's simultaneous stronger finding. Filtering must be room-scoped.
    repo, points = source({"R101": [1.1]*16, "R102": [1.1]*12+[4.0]*4})
    engine = DecisionEngine(repo, BUILDING)
    records = []
    for row in points[1:]:
        records.extend(engine.process(tick(row["timestamp_utc"])))
    findings = water_records(records)
    assert len(findings) == 2
    assert {(r.affected_assets[0], r.action) for r in findings} == {
        ("R101", "flag_anomaly"), ("R102", "request_inspection")}
    assert all(r.timestamp_utc == points[-1]["timestamp_utc"] for r in findings)
    assert len({r.episode_id for r in findings}) == 2


def test_invalid_reading_breaks_quiet_recovery_before_later_valid_quiet_clears_it():
    repo, points = source({"R101": [4.0]*4+[0.0]*14}, missing={("R101", 9)})
    engine = DecisionEngine(repo, BUILDING)
    for i, row in enumerate(points[1:], 1):
        engine.process(tick(row["timestamp_utc"]))
        if i in {9, 10, 11, 12}:
            assert "water:R101" in engine.episodes.active
            assert "R101" in engine.water._strong_active
    assert "water:R101" not in engine.episodes.active
    assert "R101" not in engine.water._strong_active


def test_confirmed_quiet_recovery_allows_a_later_independent_weak_incident():
    # Continued observation through a quiet day is actual recovery evidence;
    # mere absence of ticks or a reset would not establish it.
    repo, points = source({"R101": [4.0]*4+[0.0]*92+[1.1]*16})
    engine = DecisionEngine(repo, BUILDING)
    records = []
    for row in points[1:]:
        records.extend(engine.process(tick(row["timestamp_utc"])))
    findings = water_records(records)
    assert len(findings) == 2
    assert findings[0].action == "request_inspection"
    assert findings[1].action == "flag_anomaly"
    assert findings[0].episode_id != findings[1].episode_id
    assert findings[1].timestamp_utc == points[-1]["timestamp_utc"]


def detector_timeline(deltas, order, *, reveal_at, initial_summary="", prior_quiet_night=False):
    """A mutable public order view reveals a narrative only when available."""
    from engine.core import EngineConfig
    from engine.detectors.water import WaterDetector
    repo, points = source({"R101": deltas})
    visits = [r["timestamp_utc"] for r in points[1:]]
    if prior_quiet_night:
        baseline_start = instant(START)-timedelta(days=1)
        baseline = [{"room_id": "R101", "timestamp_utc": utc(baseline_start+timedelta(minutes=15*i)), "meter_total_l": 100.0} for i in range(13)]
        repo.measurements["room_water.csv"] = baseline+repo.measurements["room_water.csv"]
        repo.states["R101"] = [state(t=r["timestamp_utc"], cleaning="clean") for r in baseline]+repo.states["R101"]
        visits = [r["timestamp_utc"] for r in baseline[1:]]+visits
    detector = WaterDetector(EngineConfig())
    public_view = {**order, "summary": initial_summary}
    findings = []
    for stamp in visits:
        if instant(stamp) >= instant(reveal_at):
            public_view["summary"] = order["summary"]
        known = [public_view] if instant(stamp) >= instant(order["created_utc"]) else []
        result = detector.detect(tick(stamp), repo, BUILDING, known)
        findings.extend((stamp, c) for c in result.candidates if c.issue in {"water_leak", "water_low_rate_review"})
    return detector, findings


def test_late_known_past_work_cannot_leave_contaminated_weak_history_eligible():
    # Work 00:30→02:30 local is first documented at03:00. At04:00 a four-hour
    # candidate would incorrectly include those now-known operational intervals.
    order = {"work_order_id": "LATE_SCOPE", "asset_id": "R101",
             "created_utc": "2026-02-11T02:00:00Z", "scheduled_utc": "2026-02-10T23:30:00Z",
             "summary": "Known tap flushing in this room"}
    detector, findings = detector_timeline([1.1]*23, order, reveal_at=order["created_utc"])
    assert findings == []
    assert detector.diagnostics["late_known_work_reclassified_intervals"] > 0


def test_late_scope_preserves_the_clean_tail_after_its_inclusive_expiry():
    # Scope22:15→00:15 is first known at03:00. Do not reuse00:15 as a clear
    # starting endpoint, but do preserve the uninterrupted clean tail00:30 onward.
    order = {"work_order_id": "LATE_SHORT_SCOPE", "asset_id": "R101",
             "created_utc": "2026-02-11T02:00:00Z", "scheduled_utc": "2026-02-10T21:15:00Z",
             "summary": "Known tap flushing in this room"}
    _, findings = detector_timeline([1.1]*23, order, reveal_at=order["created_utc"])
    assert findings
    assert findings[0][0] == "2026-02-11T03:30:00Z"  # 00:30→04:30local.
    assert findings[0][1].evidence[0] == "room_water.csv:R101:2026-02-10T23:30:00Z..2026-02-11T03:30:00Z"


def test_newly_available_summary_updates_existing_order_scope_instead_of_order_id_only():
    # Creation metadata is known all night; its initially hidden summary becomes
    # available only at03:00. Reclassification must notice a change on the same ID.
    order = {"work_order_id": "SAME_ID_NEW_SCOPE", "asset_id": "R101",
             "created_utc": "2026-02-10T18:00:00Z", "scheduled_utc": "2026-02-10T21:15:00Z",
             "summary": "Observed known tap flushing in this room"}
    _, findings = detector_timeline([1.1]*23, order, reveal_at="2026-02-11T02:00:00Z")
    assert findings
    assert findings[0][0] == "2026-02-11T03:30:00Z"


def test_late_known_work_invalidates_independent_baseline_intervals_too():
    # Yesterday's apparently quiet baseline includes an operation whose scope is
    # disclosed today. It cannot still justify the faster3h comparison at03:00.
    order = {"work_order_id": "BASELINE_SCOPE_DISCLOSED_LATER", "asset_id": "R101",
             "created_utc": "2026-02-11T02:00:00Z", "scheduled_utc": "2026-02-09T23:30:00Z",
             "summary": "Known tap flushing in this room"}
    _, findings = detector_timeline([1.1]*23, order, reveal_at=order["created_utc"], prior_quiet_night=True)
    assert findings
    assert findings[0][0] == "2026-02-11T03:00:00Z"  # The independent4h path remains available.
    assert "No quiet prior baseline" in findings[0][1].reason


def test_future_work_scope_does_not_invalidate_present_history():
    for created, scheduled in [("2026-02-11T06:00:00Z", "2026-02-10T23:30:00Z"),
                                ("2026-02-10T18:00:00Z", "2026-02-11T06:00:00Z")]:
        order = {"work_order_id": "NOT_CURRENT_SCOPE", "asset_id": "R101",
                 "created_utc": created, "scheduled_utc": scheduled,
                 "summary": "Known tap flushing in this room"}
        _, findings = detector_timeline([1.1]*23, order, reveal_at=created)
        assert findings
        assert findings[0][0] == "2026-02-11T03:00:00Z"


def test_different_future_summary_cannot_change_already_emitted_historical_candidates():
    outputs = []
    for summary in ["Known tap flushing in this room", "Unrelated paperwork, no operation scope"]:
        order = {"work_order_id": "OUTCOME_HIDDEN_UNTIL_KNOWN", "asset_id": "R101",
                 "created_utc": "2026-02-10T18:00:00Z", "scheduled_utc": "2026-02-10T23:30:00Z",
                 "summary": summary}
        _, findings = detector_timeline([4.0]*23, order, reveal_at="2026-02-11T02:00:00Z")
        outputs.append([(at, c.reason, c.evidence, c.action) for at, c in findings
                        if instant(at) < instant("2026-02-11T02:00:00Z")])
    assert outputs[0]
    assert outputs[0] == outputs[1]


def run_with_order_fact_at_clock(*, disclosed_at, creation_discloses_scope=False, standalone=False):
    from common.contracts.schemas import NormalizedEventV1
    repo, points = source({"R101": [1.1]*23})
    order = {"work_order_id": "SAME_CLOCK_SCOPE", "asset_id": "R101",
             "created_utc": disclosed_at if creation_discloses_scope else "2026-02-10T18:00:00Z",
             "scheduled_utc": "2026-02-10T23:30:00Z", "summary": "Known tap flushing in this room"}
    created = NormalizedEventV1(
        event_id="wo:SAME_CLOCK_SCOPE", timestamp_utc=order["created_utc"],
        available_at_utc=order["created_utc"], event_type="work_order_created",
        source={"file": "work_orders.csv", "record_id": "SAME_CLOCK_SCOPE", "timestamp_utc": order["created_utc"]},
        entities={"room_id": "R101", "asset_id": "R101"},
        payload={**order, "summary": order["summary"] if creation_discloses_scope else ""})
    facts = [created]
    if not creation_discloses_scope:
        facts.append(NormalizedEventV1(
            event_id="wo:SAME_CLOCK_SCOPE:resolved", timestamp_utc=disclosed_at,
            available_at_utc=disclosed_at, event_type="operational_event",
            source={"file": "work_orders.csv", "record_id": "SAME_CLOCK_SCOPE", "timestamp_utc": disclosed_at},
            entities={"room_id": "R101", "asset_id": "R101"},
            payload={**order, "type": "work_order_completed", "completed_utc": disclosed_at}))
    repo.stream = sorted([tick(row["timestamp_utc"]) for row in points[1:]]+facts,
                         key=lambda e: (instant(e.available_at_utc), e.event_id))
    engine = DecisionEngine(repo, BUILDING)
    if standalone:
        decisions = [d for event in repo.iter_events() for d in engine.process(event)]
    else:
        decisions = engine.run()
    return engine, repo.stream, decisions


def test_run_applies_same_time_current_creation_scope_before_clock_decision():
    engine, stream, decisions = run_with_order_fact_at_clock(
        disclosed_at="2026-02-11T03:00:00Z", creation_discloses_scope=True)
    # The frozen stream order really places tick before wo at this timestamp.
    same_time = [e for e in stream if e.available_at_utc == "2026-02-11T03:00:00Z"]
    assert [e.event_type for e in same_time] == ["clock_tick", "work_order_created"]
    assert not water_records(decisions)
    assert engine.event_count == len(stream)


def test_run_discloses_same_time_completed_scope_before_clock_without_future_narrative():
    engine, stream, decisions = run_with_order_fact_at_clock(disclosed_at="2026-02-11T03:00:00Z")
    # This follows ingestion's real contract: empty creation summary, known
    # completed summary only at its availability timestamp; no unseen outcome.
    assert next(e for e in stream if e.event_type == "work_order_created").payload["summary"] == ""
    assert not water_records(decisions)
    assert engine.event_count == len(stream)


def test_known_completion_before_clock_also_updates_the_retained_work_scope():
    _, _, decisions = run_with_order_fact_at_clock(disclosed_at="2026-02-11T02:45:00Z")
    assert not water_records(decisions)


def test_run_does_not_borrow_a_future_completed_scope_to_suppress_past_review():
    _, _, decisions = run_with_order_fact_at_clock(disclosed_at="2026-02-11T03:15:00Z")
    findings = water_records(decisions)
    assert len(findings) == 1
    assert findings[0].timestamp_utc == "2026-02-11T03:00:00Z"
    assert findings[0].action == "flag_anomaly"


def test_standalone_process_cannot_anticipate_an_unseen_same_time_fact():
    # This API consumes one event at a time, so its caller must batch/preload
    # equal-time facts if it wants the stronger run-level timestamp semantics.
    _, _, decisions = run_with_order_fact_at_clock(
        disclosed_at="2026-02-11T03:00:00Z", creation_discloses_scope=True, standalone=True)
    findings = water_records(decisions)
    assert len(findings) == 1
    assert findings[0].timestamp_utc == "2026-02-11T03:00:00Z"


def test_run_rejects_unsorted_distinct_availability_times_before_processing_old_tick():
    import pytest
    repo = FakeGateway()
    repo.stream = [tick("2026-02-11T03:00:00Z"), tick("2026-02-11T02:45:00Z")]
    engine = DecisionEngine(repo, BUILDING)
    with pytest.raises(ValueError, match="sorted by available_at_utc"):
        engine.run()
    assert engine.event_count == 1
    assert engine.water.diagnostics["ticks_processed"] == 1


def test_same_time_fact_batch_still_rejects_future_occurrence_and_source_timestamp():
    import pytest
    from common.contracts.schemas import NormalizedEventV1
    for future_field in ("occurrence", "source"):
        now = "2026-02-11T03:00:00Z"
        later = "2026-02-11T03:15:00Z"
        fact = NormalizedEventV1(
            event_id="wo:INVALID_FUTURE", event_type="work_order_created",
            timestamp_utc=later if future_field == "occurrence" else now,
            available_at_utc=now,
            source={"file": "work_orders.csv", "record_id": "INVALID_FUTURE",
                    "timestamp_utc": later if future_field == "source" else now},
            entities={"room_id": "R101", "asset_id": "R101"},
            payload={"work_order_id": "INVALID_FUTURE", "created_utc": now,
                     "scheduled_utc": "2026-02-11T00:00:00Z", "asset_id": "R101",
                     "summary": "Tap flushing"})
        repo = FakeGateway()
        repo.stream = [tick(now), fact]
        engine = DecisionEngine(repo, BUILDING)
        with pytest.raises(ValueError, match="Future"):
            engine.run()
        # The equal-time fact is checked before its periodic scan, but its
        # invalid future occurrence/source cannot be used as current context.
        assert engine.event_count == 0
        assert engine.work_orders == []
        assert engine.water.diagnostics["ticks_processed"] == 0


def test_run_snapshots_reused_mutable_envelopes_before_equal_time_lookahead():
    # A streaming adapter may reuse a mutable Pydantic envelope. Batching must
    # freeze a yielded record before advancing the iterator and seeing a later
    # timestamp; retaining the same object reference loses the first event.
    repo = FakeGateway()
    first_at, second_at = "2026-02-11T03:00:00Z", "2026-02-11T03:15:00Z"

    def reused_envelope_stream():
        envelope = tick(first_at)
        yield envelope
        next_record = tick(second_at)
        for field in type(envelope).model_fields:
            setattr(envelope, field, getattr(next_record, field))
        yield envelope

    repo.iter_events = reused_envelope_stream
    engine = DecisionEngine(repo, BUILDING)
    assert engine.run() == []
    assert engine.event_count == 2
    assert engine._seen_ids == {"tick:"+first_at, "tick:"+second_at}
    assert engine.water.diagnostics["ticks_processed"] == 2


def test_narrow_configured_weak_band_midpoint_is_not_promoted_to_strong_inspection():
    from decimal import Decimal
    from engine.core import EngineConfig
    low_floor = Decimal("1")
    strong_floor = Decimal("1.000000000001")
    rate = Decimal("1.0000000000005")
    value = Decimal("100")
    rows = [{"room_id": "R101", "timestamp_utc": START, "meter_total_l": str(value)}]
    for i in range(1, 17):
        value += rate
        rows.append({"room_id": "R101", "timestamp_utc": utc(instant(START)+timedelta(minutes=15*i)),
                     "meter_total_l": str(value)})
    repo = FakeGateway()
    repo.measurements["room_water.csv"] = rows
    repo.states["R101"] = [state(t=r["timestamp_utc"], cleaning="clean") for r in rows]
    repo.stream = [tick(r["timestamp_utc"]) for r in rows[1:]]
    config = EngineConfig(water_low_min_l_per_interval=float(low_floor),
                          water_min_l_per_interval=float(strong_floor))
    engine = DecisionEngine(repo, BUILDING, config)
    records = engine.run()
    findings = water_records(records)
    assert len(findings) == 1
    assert findings[0].issue_type == "water_low_rate_review"
    assert findings[0].action == "flag_anomaly"
    assert findings[0].timestamp_utc == rows[16]["timestamp_utc"]
    assert all(r.action != "request_inspection" for r in records)
    assert not engine.rejections
