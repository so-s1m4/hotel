"""Unlabelled water evaluation fixtures; no real datasets or Git history needed."""
from collections import Counter
import csv
import json
from datetime import timedelta
from types import SimpleNamespace

import pytest

from engine.core import Candidate, DetectionResult, EngineConfig, instant, utc
from scripts.evaluate_water import ExposureProfile, Histogram, RuleSummary, TickCache, evaluate_replay, pair_delta
from tests.engine.fixtures import BUILDING, FakeGateway, state, tick

NOW = "2026-01-06T02:00:00Z"


def exposure_repo(problem=None):
    repo = FakeGateway()
    for i in range(5):
        t = utc(instant(NOW)-timedelta(minutes=15*(4-i)))
        for room in ["R101","R102"]:
            value = 100+i
            if room == "R101":
                if problem == "old_missing" and i == 1: value = None
                if problem == "latest_missing" and i == 4: value = None
                if problem == "counter_drop" and i == 4: value = 1
                if problem == "small_backstep" and i == 4: value = 102.8
            repo.measurements.setdefault("room_water.csv",[]).append({"timestamp_utc":t,"room_id":room,"meter_total_l":value})
            repo.states.setdefault(room,[]).append(state(room=room,t=t,cleaning="clean"))
        repo.measurements.setdefault("water_main.csv",[]).append({"timestamp_utc":t,"meter_total_l":200+3*i})
    return repo


def test_independent_availability_distinguishes_latest_pair_from_old_hour_missing():
    profile = ExposureProfile(BUILDING,EngineConfig())
    profile.observe(NOW,exposure_repo("old_missing"),[])
    counts = profile.report()["counts"]
    assert counts["main_latest_pair_complete_ticks"] == 1
    assert counts["main_strict_1h_complete_ticks"] == 0
    assert counts["main_latest_pair_available_but_1h_blocked_ticks"] == 1


@pytest.mark.parametrize("problem",["latest_missing","counter_drop"])
def test_latest_missing_or_reset_is_never_imputed_into_main_completeness(problem):
    profile = ExposureProfile(BUILDING,EngineConfig())
    profile.observe(NOW,exposure_repo(problem),[])
    counts = profile.report()["counts"]
    assert counts["main_latest_pair_complete_ticks"] == 0
    assert counts["main_strict_1h_complete_ticks"] == 0


def test_small_negative_pair_remains_signed_not_clamped_or_called_true_leak():
    repo = exposure_repo("small_backstep")
    rows = [r for r in repo.measurements["room_water.csv"] if r["room_id"] == "R101"]
    delta,status = pair_delta(rows,NOW)
    assert status == "negative_delta" and delta == pytest.approx(-.2)
    profile = ExposureProfile(BUILDING,EngineConfig())
    profile.observe(NOW,repo,[])
    counts = profile.report()["counts"]
    assert counts["negative_step_le_0.5l"] == 1
    assert counts["causally_eligible_signed_night_intervals"] == 2
    assert counts["weak_pair_quality_eligible_night_intervals"] == 2
    assert counts["eligible_nonnegative_night_intervals"] == 1


def test_earlier_hour_missing_does_not_erase_current_eligible_room_pair():
    profile = ExposureProfile(BUILDING,EngineConfig())
    profile.observe(NOW,exposure_repo("old_missing"),[])
    assert profile.report()["counts"]["eligible_nonnegative_night_intervals"] == 2


def test_observed_operation_exposure_is_described_not_labelled_as_leak():
    repo = exposure_repo()
    repo.events = [{"event_id":"CLEAN","timestamp_utc":"2026-01-06T01:30:00Z","room_id":"R101","type":"cleaning_completed"}]
    profile = ExposureProfile(BUILDING,EngineConfig())
    profile.observe(NOW,repo,[])
    report = profile.report()
    assert report["primary_eligibility_exclusions"]["recent_observed_cleaning"] == 1
    assert report["distributions"]["vacant_night_pairs_with_observed_or_known_planned_water_operations"]["count"] == 1
    assert "not confirmed benign" in report["interpretation"]


def test_future_work_order_cannot_explain_earlier_water_exposure():
    profile = ExposureProfile(BUILDING,EngineConfig())
    orders = [{"created_utc":"2026-01-07T00:00:00Z","asset_id":"R101","scheduled_utc":"2026-01-06T01:30:00Z","summary":"tap flushing"}]
    profile.observe(NOW,exposure_repo(),orders)
    assert profile.report()["counts"]["eligible_nonnegative_night_intervals"] == 2


def test_tick_cache_is_bounded_and_variants_do_not_share_mutable_query_results():
    repo = exposure_repo()
    cache = TickCache(repo)
    first = cache.get_room_state("R101",NOW)
    first["occupancy"] = "occupied"
    assert cache.get_room_state("R101",NOW)["occupancy"] == "vacant"
    first["evidence"].append("injected")
    assert "injected" not in cache.get_room_state("R101",NOW)["evidence"]
    cache.reset()
    assert not cache.cache


def review_candidate():
    return Candidate("water:R101","R101","water_low_rate_review","flag_anomaly","Uncertain flow review",
        ["room_water.csv:R101:source_window"],"Verify independently","Review only",context={"water_path":"persistent_weak"})


class NoRule:
    def __init__(self,config):
        self.diagnostics = Counter()
    def detect(self,event,repo,building,orders):
        self.diagnostics["ticks"] += 1
        return DetectionResult()


class ReviewRule(NoRule):
    def detect(self,event,repo,building,orders):
        self.diagnostics["ticks"] += 1
        return DetectionResult(candidates=[review_candidate()])


def test_paired_run_counts_raw_windows_separately_from_suppressed_episode_emissions():
    repo = exposure_repo()
    repo.stream = [tick("2026-01-06T01:45:00Z"),tick(NOW)]
    report = evaluate_replay(repo,BUILDING,{"baseline":NoRule,"candidate":ReviewRule},sample_limit=1)
    assert report["ticks_evaluated"] == 2
    candidate = report["rules"]["candidate"]
    assert candidate["candidate_windows_by_path"]["persistent_weak"] == 2
    assert candidate["episode_emissions_by_path"]["persistent_weak"] == 1
    assert len(candidate["bounded_emission_examples"]["persistent_weak"]) == 1
    assert report["paired_comparison"]["added_candidate_windows"] == 2
    assert report["paired_comparison"]["added_emission_windows"] == 1
    assert "not true/false positives" in report["paired_comparison"]["interpretation"]


def test_evaluation_feeds_only_creation_work_order_view_not_later_repair_narratives():
    instances = []
    class OrderObserver(NoRule):
        def __init__(self,config):
            super().__init__(config)
            self.orders_seen = []
            instances.append(self)
        def detect(self,event,repo,building,orders):
            self.orders_seen.append([dict(o) for o in orders])
            return DetectionResult()
    repo = exposure_repo()
    created = SimpleNamespace(event_type="work_order_created",payload={"work_order_id":"WO1","created_utc":"2026-01-06T01:00:00Z",
        "asset_id":"R101","scheduled_utc":"2026-01-06T01:30:00Z","summary":"planned tap flushing","completed_utc":"2026-01-06T02:00:00Z"})
    completed = SimpleNamespace(event_type="operational_event",payload={"type":"work_order_completed","work_order_id":"WO1","summary":"future repair outcome narrative"})
    repo.stream = [created,tick("2026-01-06T01:45:00Z"),completed,tick(NOW)]
    evaluate_replay(repo,BUILDING,{"baseline":OrderObserver,"candidate":OrderObserver})
    for detector in instances:
        assert len(detector.orders_seen) == 2
        assert all(snapshot[0]["summary"] == "planned tap flushing" for snapshot in detector.orders_seen)
        assert all("completed_utc" not in snapshot[0] for snapshot in detector.orders_seen)


def test_partial_run_is_explicit_and_tick_gaps_rejected():
    repo = exposure_repo()
    repo.stream = [tick("2026-01-06T01:30:00Z"),tick(NOW)]
    report = evaluate_replay(repo,BUILDING,{"baseline":NoRule,"candidate":NoRule},max_ticks=1)
    assert report["partial_run"] is True and report["ticks_evaluated"] == 1
    with pytest.raises(ValueError,match="uninterrupted"):
        evaluate_replay(repo,BUILDING,{"baseline":NoRule,"candidate":NoRule})


def test_quantile_bin_reporting_does_not_reclassify_binary_noise_as_a_physical_step():
    histogram = Histogram()
    for value in [.999999999999,1.0,1.000000000001]:
        histogram.add(value)
    result = histogram.report()
    assert result["range_counts"]["1_to_lt3"] == 3
    assert result["quantiles_0.1l_binned_within_plus_minus2000l"]["p50"] == 1.0
    assert result["min_l"] == .999999999999


def test_independent_raw_csv_crosscheck_uses_exact_latest_pairs_not_hole_imputation(tmp_path):
    from scripts.evaluate_water import raw_completeness
    repo = exposure_repo("old_missing")
    (tmp_path/"building.json").write_text(json.dumps(BUILDING))
    (tmp_path/"manifest.json").write_text(json.dumps({"profile":"fixture","start_utc":"2026-01-06T01:00:00Z","end_utc":"2026-01-06T02:15:00Z"}))
    for name in ["room_water.csv","water_main.csv"]:
        rows = repo.measurements[name]
        with (tmp_path/name).open("w",newline="") as stream:
            writer = csv.DictWriter(stream,fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
    result = raw_completeness(tmp_path)
    assert result["counts"]["room_rows"] == 10
    assert result["counts"]["missing_room_rows"] == 1
    assert result["counts"]["latest_pair_complete_ticks"] == 2
    assert result["counts"]["strict_1h_complete_ticks"] == 0
    assert result["counts"]["latest_pair_available_but_1h_blocked_ticks"] == 2
    assert all(len(digest)==64 for digest in result["source_sha256"].values())
