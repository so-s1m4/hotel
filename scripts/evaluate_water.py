"""Paired, chronological water-rule diagnostics; no ground-truth accuracy claims.

Runs a pinned trusted local Git version of WaterDetector and the current source
against the same causal gateway and configuration. Data profiles remain separate.
Creation-time work-order envelopes are supplied first; completion context is
usable only once its event is available, never as a label for earlier detection.
"""
from __future__ import annotations

import argparse
import csv
from collections import Counter, defaultdict
from dataclasses import asdict
from copy import deepcopy
from datetime import timedelta
from functools import lru_cache
from itertools import groupby
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from types import ModuleType
import time

from engine.core import EngineConfig, VIENNA, ago, instant, number
from engine.decisions import EpisodeTracker
from ingestion.replay import ReplayData

REPO_ROOT = Path(__file__).resolve().parents[1]
BASELINE_REF = "3ab9541"
NUMERIC_ABS_TOL = 1e-9


@lru_cache(maxsize=4096)
def _time(stamp):
    return instant(stamp)


@lru_cache(maxsize=4096)
def _night(stamp):
    return 0 <= _time(stamp).astimezone(VIENNA).hour < 6


def _digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


class Histogram:
    """Bounded 0.1-L-bin distribution; quantiles are descriptive approximations."""
    def __init__(self):
        self.bins = Counter()
        self.count = 0
        self.total = 0.0
        self.minimum = None
        self.maximum = None
        self.ranges = Counter()

    def add(self, value):
        self.count += 1
        self.total += value
        self.minimum = value if self.minimum is None else min(self.minimum, value)
        self.maximum = value if self.maximum is None else max(self.maximum, value)
        # Quality outliers are counted, not allowed to grow an unbounded histogram.
        if -2000 <= value <= 2000:
            self.bins[round(value*10)] += 1
        described = value  # reporting only; preserve detector inputs/raw signed values
        if value >= 0:
            for boundary in (0.5,1.0,3.0,100.0):
                if boundary-NUMERIC_ABS_TOL <= value <= boundary+NUMERIC_ABS_TOL:
                    described = boundary
                    break
        self.ranges["negative" if described < 0 else "zero" if described == 0 else
                    "positive_lt0.5" if described < 0.5 else "0.5_to_lt1" if described < 1 else
                    "1_to_lt3" if described < 3 else "3_to_100" if described <= 100 else "gt100"] += 1

    def report(self):
        quantiles = {}
        if self.bins:
            ordered = sorted(self.bins.items())
            n = sum(self.bins.values())
            for name, fraction in [("p10", .1), ("p50", .5), ("p90", .9), ("p99", .99)]:
                target = max(1, int((n-1)*fraction)+1)
                total = 0
                for value, count in ordered:
                    total += count
                    if total >= target:
                        quantiles[name] = value/10
                        break
        return {"count": self.count, "mean_l": self.total/self.count if self.count else None,
                "min_l": self.minimum, "max_l": self.maximum,
                "quantiles_0.1l_binned_within_plus_minus2000l": quantiles,
                "quantile_covered_count": sum(self.bins.values()), "range_counts": dict(self.ranges),
                "reporting_precision_note":"Range labels snap only positive 0.5/1/3/100 L thresholds within 1e-9 L; raw signed deltas, negative signs, detector inputs and extrema are unchanged."}


class TickCache:
    """Share read-only query snapshots between paired rules within one tick only."""
    def __init__(self, repo):
        self.repo = repo
        self.cache = {}

    def reset(self):
        self.cache.clear()

    def __getattr__(self, name):
        original = getattr(self.repo, name)
        if not name.startswith("get_"):
            return original
        def query(*args, **kwargs):
            key = (name, args, tuple(sorted((k,v) for k,v in kwargs.items() if v is not None)))
            if key not in self.cache:
                self.cache[key] = deepcopy(original(*args, **kwargs))
            return deepcopy(self.cache[key])
        return query


def pair_delta(readings, now, maximum=2000):
    """Signed adjacent-pair geometry/quality, without clamping a negative delta."""
    if len(readings) < 2:
        return None, "insufficient_pair"
    a, b = readings[-2:]
    if _time(b["timestamp_utc"]) != _time(now):
        return None, "missing_latest"
    if _time(b["timestamp_utc"])-_time(a["timestamp_utc"]) != timedelta(minutes=15):
        return None, "noncontiguous_pair"
    before, after = number(a.get("meter_total_l")), number(b.get("meter_total_l"))
    if before is None or after is None:
        return None, "missing_meter_value"
    if min(before, after) < 0:
        return None, "invalid_negative_total"
    delta = after-before
    if delta > maximum:
        return delta, "implausible_positive_delta"
    if delta < 0:
        return delta, "negative_delta"
    return delta, "nonnegative_pair"


def _strict_window(readings, now, maximum):
    if len(readings) < 5 or _time(readings[-1]["timestamp_utc"]) != _time(now):
        return False
    for i in range(1, len(readings)):
        _, status = pair_delta(readings[i-1:i+1], readings[i]["timestamp_utc"], maximum)
        if status != "nonnegative_pair":
            return False
    return True


def _planned_water_work(room, effective_at, orders, *, known_at):
    for order in orders:
        if not order.get("created_utc") or _time(order["created_utc"]) > _time(known_at):
            continue
        scheduled = order.get("scheduled_utc")
        summary = str(order.get("summary") or "").casefold()
        applies = order.get("asset_id") in {room, "WM_"+room} or room.casefold() in summary
        if (applies and scheduled and 0 <= (_time(effective_at)-_time(scheduled)).total_seconds() <= 7200
                and any(word in summary for word in ("flush", "meter", "legionella", "tap"))):
            return True
    return False


class ExposureProfile:
    def __init__(self, building, config):
        self.rooms = sorted(r["id"] for r in building["building"]["rooms"])
        self.config = config
        self.counts = Counter()
        self.exclusions = Counter()
        self.per_room = defaultdict(Counter)
        self.distributions = defaultdict(Histogram)
        self.main_blockers = Counter()
        self._eligible_runs = Counter()
        self._weak_positive_runs = Counter()
        self.longest_eligible = Counter()
        self.longest_positive_weak = Counter()

    def observe(self, now, repo, orders):
        self.counts["ticks"] += 1
        begin = ago(now, 60)
        grouped = defaultdict(list)
        for row in repo.get_measurements("room_water.csv", begin, now, now):
            if row.get("room_id") in self.rooms and row.get("timestamp_utc") and _time(begin) <= _time(row["timestamp_utc"]) <= _time(now):
                grouped[row["room_id"]].append(row)
        for readings in grouped.values():
            readings.sort(key=lambda r: _time(r["timestamp_utc"]))
        recent = [r for r in repo.get_events(ago(now, 120), now, now)
                  if r.get("timestamp_utc") and _time(ago(now,120)) <= _time(r["timestamp_utc"]) <= _time(now)]
        cleaners = {r.get("room_id") for r in recent if r.get("type") in {
            "cleaning_started", "cleaning_completed", "robot_floor_cleaning_started", "robot_floor_cleaning_completed"}}
        latest_deltas = {}
        strict_rooms = set()
        for room in self.rooms:
            self.counts["room_tick_opportunities"] += 1
            readings = grouped.get(room, [])
            delta, status = pair_delta(readings, now)
            self.per_room[room][status] += 1
            self.counts["room_"+status] += 1
            if _strict_window(readings, now, 2000):
                strict_rooms.add(room)
            else:
                self.main_blockers[room] += 1
            if status == "nonnegative_pair":
                latest_deltas[room] = delta
            if delta is not None:
                self.distributions["all_signed_room_pairs"].add(delta)
                if delta < 0:
                    self.counts["negative_step_le_0.5l"] += abs(delta) <= .5+NUMERIC_ABS_TOL
                    self.counts["negative_step_gt100l"] += abs(delta) > 100
            eligible = False
            if status not in {"nonnegative_pair", "negative_delta"}:
                self.exclusions[status] += 1
            elif not all(_night(r["timestamp_utc"]) for r in readings[-2:]):
                self.exclusions["day_or_night_boundary"] += 1
            else:
                self.distributions["night_signed_room_pairs"].add(delta)
                states = [repo.get_room_state(room, r["timestamp_utc"]) or {} for r in readings[-2:]]
                vacant = all(s.get("occupancy") == "vacant" for s in states)
                clean = all(s.get("housekeeping_state") == "clean" for s in states)
                fresh = all(s.get("room_id") == room and s.get("last_observed_utc")
                            and _time(r["timestamp_utc"])-timedelta(minutes=self.config.telemetry_max_age_minutes) <= _time(s["last_observed_utc"]) <= _time(r["timestamp_utc"])
                            for r,s in zip(readings[-2:],states))
                transition = any(room in {r.get("room_id"),r.get("from_room"),r.get("to_room")}
                                 and _time(readings[-2]["timestamp_utc"]) < _time(r["timestamp_utc"]) <= _time(now)
                                 and r.get("type") in {"checkin","checkin_early","checkin_after_wait","checkout","checkout_early","room_reassigned","stay_in_progress"} for r in recent)
                work = any(_planned_water_work(room,row["timestamp_utc"],orders,known_at=now) for row in readings[-2:])
                if vacant:
                    self.distributions["observed_vacant_night_pairs_before_operation_exclusions"].add(delta)
                    if room in cleaners or work:
                        self.distributions["vacant_night_pairs_with_observed_or_known_planned_water_operations"].add(delta)
                if not vacant:
                    self.exclusions["occupied_or_unknown"] += 1
                elif not clean:
                    self.exclusions["housekeeping_not_observed_clean"] += 1
                elif not fresh:
                    self.exclusions["stale_or_missing_room_state"] += 1
                elif room in cleaners:
                    self.exclusions["recent_observed_cleaning"] += 1
                elif transition:
                    self.exclusions["observed_lifecycle_transition"] += 1
                elif work:
                    self.exclusions["known_planned_water_work"] += 1
                else:
                    eligible = True
                    self.counts["causally_eligible_signed_night_intervals"] += 1
                    self.per_room[room]["causally_eligible_signed_night_intervals"] += 1
                    self.distributions["eligible_signed_night_intervals"].add(delta)
                    backstep = getattr(self.config,"water_weak_max_backstep_l",0.5)
                    tolerance = min(NUMERIC_ABS_TOL,abs(backstep)*1e-6)
                    if delta >= -backstep-tolerance:
                        self.counts["weak_pair_quality_eligible_night_intervals"] += 1
                    if status == "nonnegative_pair":
                        self.counts["eligible_nonnegative_night_intervals"] += 1
            self._eligible_runs[room] = self._eligible_runs[room]+1 if eligible else 0
            self._weak_positive_runs[room] = self._weak_positive_runs[room]+1 if eligible and delta is not None and 1-NUMERIC_ABS_TOL <= delta < 3-NUMERIC_ABS_TOL else 0
            self.longest_eligible[room] = max(self.longest_eligible[room],self._eligible_runs[room])
            self.longest_positive_weak[room] = max(self.longest_positive_weak[room],self._weak_positive_runs[room])
        main = [r for r in repo.get_measurements("water_main.csv",begin,now,now)
                if r.get("timestamp_utc") and _time(begin) <= _time(r["timestamp_utc"]) <= _time(now)]
        main.sort(key=lambda r:_time(r["timestamp_utc"]))
        main_delta, main_status = pair_delta(main,now,50000)
        self.counts["main_"+main_status] += 1
        latest_complete = main_status == "nonnegative_pair" and len(latest_deltas) == len(self.rooms)
        strict_complete = _strict_window(main,now,50000) and len(strict_rooms) == len(self.rooms)
        self.counts["main_latest_pair_complete_ticks"] += latest_complete
        self.counts["main_strict_1h_complete_ticks"] += strict_complete
        self.counts["main_latest_pair_available_but_1h_blocked_ticks"] += latest_complete and not strict_complete
        if latest_complete:
            self.distributions["main_minus_all_room_latest_interval_residual_liters"].add(main_delta-sum(latest_deltas.values()))

    def report(self):
        return {"counts":dict(self.counts),"primary_eligibility_exclusions":dict(self.exclusions),
                "distributions":{k:v.report() for k,v in sorted(self.distributions.items())},
                "per_room":{room:{**dict(self.per_room[room]),"strict_main_window_blocks":self.main_blockers[room],
                                  "longest_eligible_run_intervals":self.longest_eligible[room],
                                  "longest_positive_1_to_lt3l_run_intervals":self.longest_positive_weak[room]}
                            for room in self.rooms},
                "interpretation":"Exposure/quality counts, not confirmed benign flow or leak labels. Causal-context-only exposure can include counter drops; weak pair-quality exposure separately excludes steps below the configured small-backstep bound. Negative deltas remain signed; no missing interval is imputed. Exposure reflects availability at each observation time; a later-known work order can subsequently invalidate retained decision windows. Exposure is neither a label nor a full replay of later evidence revision."}


def candidate_path(candidate):
    if candidate.context.get("water_path"):
        return str(candidate.context["water_path"])
    if candidate.issue == "water_balance":
        return "main_residual"
    if candidate.issue == "water_leak":
        return "strong_room"
    if candidate.issue == "water_low_rate_review":
        return "low_prior_night" if any("earlier_night_baseline:" in ref for ref in candidate.evidence) else "low_without_prior_night"
    return candidate.issue


class RuleSummary:
    def __init__(self, sample_limit):
        self.tracker = EpisodeTracker()
        self.windows = Counter()
        self.emissions = Counter()
        self.episodes = defaultdict(set)
        self.samples = defaultdict(list)
        self.window_keys = set()
        self.emission_keys = set()
        self.sample_limit = sample_limit

    def observe(self, result, now):
        for key in result.resolved:
            self.tracker.close(key)
        for candidate in result.candidates:
            path = candidate_path(candidate)
            key = (now,candidate.key,path,candidate.action,candidate.variant)
            self.window_keys.add(key)
            self.windows[path] += 1
            episode = self.tracker.episode(candidate.key,now)
            self.episodes[path].add(episode)
            if self.tracker.emitted(candidate.key,candidate.action,candidate.variant):
                continue
            self.tracker.mark(candidate.key,candidate.action,candidate.variant)
            self.emissions[path] += 1
            self.emission_keys.add(key)
            if len(self.samples[path]) < self.sample_limit:
                self.samples[path].append({"at_utc":now,"asset":candidate.asset,"path":path,"issue_type":candidate.issue,
                    "action":candidate.action,"reason":candidate.reason[:1000],"evidence":candidate.evidence[:8],"episode_id":episode})

    def report(self, detector):
        diagnostics = {"detector":dict(getattr(detector,"diagnostics",{}))}
        for name, child in vars(detector).items():
            if child is not detector and hasattr(child,"diagnostics"):
                diagnostics[name] = dict(child.diagnostics)
        return {"candidate_windows_by_path":dict(self.windows),"episode_emissions_by_path":dict(self.emissions),
                "unique_episodes_by_path":{k:len(v) for k,v in self.episodes.items()},
                "bounded_emission_examples":dict(self.samples),"diagnostics":diagnostics}


def _module_factory(source, label):
    name = "_water_evaluation_"+label.replace("-","_")
    module = ModuleType(name)
    module.__package__ = "engine.detectors"
    sys.modules[name] = module
    exec(compile(source,label,"exec"),module.__dict__)
    return module.WaterDetector


def load_rule_pair(baseline_ref=BASELINE_REF):
    process = subprocess.run(["git","rev-parse","--verify",baseline_ref+"^{commit}"],cwd=REPO_ROOT,capture_output=True,text=True)
    if process.returncode:
        raise ValueError("Pinned baseline commit is unavailable locally; fetch its history first: "+baseline_ref)
    revision = process.stdout.strip()
    process = subprocess.run(["git","show",revision+":engine/detectors/water.py"],cwd=REPO_ROOT,capture_output=True,text=True)
    if process.returncode:
        raise ValueError("Cannot read baseline water module: "+process.stderr.strip())
    old_source = process.stdout
    new_source = (REPO_ROOT/"engine/detectors/water.py").read_text(encoding="utf-8")
    provenance = {"baseline_commit":revision,"baseline_water_source_sha256":hashlib.sha256(old_source.encode()).hexdigest(),
                  "candidate_water_source_sha256":hashlib.sha256(new_source.encode()).hexdigest(),
                  "candidate_detector_sources_sha256":{p.name:_digest(p) for p in sorted((REPO_ROOT/"engine/detectors").glob("*.py"))},
                  "config_source_sha256":_digest(REPO_ROOT/"engine/core.py"),
                  "shared_episode_tracker_source_sha256":_digest(REPO_ROOT/"engine/decisions.py"),
                  "shared_planner_source_sha256":_digest(REPO_ROOT/"engine/planner.py"),
                  "event_time_policy":"equal-availability facts before clock scans; no future context"}
    return {"baseline":_module_factory(old_source,"baseline_"+revision),"candidate":_module_factory(new_source,"candidate_working_copy")},provenance


def evaluate_replay(repo,building,factories,config=None,sample_limit=12,max_ticks=None):
    config = config or EngineConfig()
    cached = TickCache(repo)
    detectors = {name:factory(config) for name,factory in factories.items()}
    summaries = {name:RuleSummary(sample_limit) for name in factories}
    exposure = ExposureProfile(building,config)
    orders = {}
    ticks = 0
    last_tick = None
    began = time.perf_counter()
    last_available = None
    snapshots = (deepcopy(event) for event in repo.iter_events())
    for available,events in groupby(snapshots,key=lambda e:_time(e.available_at_utc)):
        if max_ticks is not None and ticks >= max_ticks:
            break
        if last_available is not None and available < last_available:
            raise ValueError("Evaluation events must be ordered by availability")
        last_available = available
        batch = sorted(events,key=lambda e:(e.event_type == "clock_tick",e.event_id))
        for event in batch:
            occurrence = getattr(event,"timestamp_utc",event.available_at_utc)
            if _time(occurrence) > available:
                raise ValueError("Future occurrence cannot become known before it occurs")
            source_stamp = getattr(getattr(event,"source",None),"timestamp_utc",None)
            if source_stamp and _time(source_stamp) > available:
                raise ValueError("Future source timestamp")
            if event.event_type == "work_order_created":
                # Creation view has no future outcome. Later disclosed context
                # updates only when its completion envelope becomes available.
                safe = {k:event.payload.get(k) for k in ("work_order_id","created_utc","asset_id","scheduled_utc","summary","assigned_to","type")}
                if safe["work_order_id"]:
                    orders[safe["work_order_id"]] = safe
            elif event.event_type == "operational_event" and event.payload.get("type") == "work_order_completed":
                identifier = event.payload.get("work_order_id") or getattr(getattr(event,"source",None),"record_id",None)
                if identifier in orders:
                    order = orders[identifier]
                    order["observed_completed_utc"] = event.available_at_utc
                    for field in ("summary","scheduled_utc","asset_id","assigned_to"):
                        if event.payload.get(field) is not None:
                            order[field] = deepcopy(event.payload[field])
            if event.event_type != "clock_tick":
                continue
            if max_ticks is not None and ticks >= max_ticks:
                break
            now = event.available_at_utc
            if last_tick is not None and _time(now)-_time(last_tick) != timedelta(minutes=15):
                raise ValueError("Evaluation requires uninterrupted chronological quarter-hour ticks")
            last_tick = now
            cached.reset()
            known_orders = list(orders.values())
            exposure.observe(now,cached,known_orders)
            for name,detector in detectors.items():
                summaries[name].observe(detector.detect(event,cached,building,known_orders),now)
            ticks += 1
    output = {"ticks_evaluated":ticks,"last_tick_utc":last_tick,"partial_run":max_ticks is not None,
              "elapsed_seconds":time.perf_counter()-began,"config":asdict(config),"exposure":exposure.report(),
              "rules":{name:summaries[name].report(detector) for name,detector in detectors.items()}}
    if "baseline" in summaries and "candidate" in summaries:
        a,b = summaries["baseline"],summaries["candidate"]
        new,removed = b.emission_keys-a.emission_keys,a.emission_keys-b.emission_keys
        output["paired_comparison"] = {"added_candidate_windows":len(b.window_keys-a.window_keys),
            "removed_candidate_windows":len(a.window_keys-b.window_keys),"added_emission_windows":len(new),"removed_emission_windows":len(removed),
            "added_emission_examples":[list(x) for x in sorted(new)[:sample_limit]],
            "removed_emission_examples":[list(x) for x in sorted(removed)[:sample_limit]],
            "interpretation":"Changes in review burden/availability, not true/false positives or labelled recall. Episode timing can shift when a new earlier path opens the same asset episode."}
    return output


def evaluate_dataset(data_dir,factories,provenance,config=None,sample_limit=12,max_ticks=None):
    root = Path(data_dir)
    manifest = json.loads((root/"manifest.json").read_text(encoding="utf-8"))
    building = json.loads((root/"building.json").read_text(encoding="utf-8"))
    repo = ReplayData(root,use_llm=False)
    output = evaluate_replay(repo,building,factories,config,sample_limit,max_ticks)
    output["dataset"] = {k:manifest.get(k) for k in ("profile","start_utc","end_utc","days","interval_seconds")}
    output["source_sha256"] = {name:_digest(root/name) for name in ("manifest.json","building.json","room_water.csv","water_main.csv","room_telemetry.csv","events.jsonl","work_orders.csv")}
    output["rule_provenance"] = provenance
    output["ingestion_audit"] = repo.audit
    return output


def raw_completeness(data_dir):
    """Independent streaming CSV cross-check; no gateway/detector queries or labels."""
    root = Path(data_dir)
    building = json.loads((root/"building.json").read_text(encoding="utf-8"))
    manifest = json.loads((root/"manifest.json").read_text(encoding="utf-8"))
    rooms = {r["id"] for r in building["building"]["rooms"]}
    previous = {}
    paired = defaultdict(lambda:[0,0.0])
    counts = Counter()
    signed = Histogram()
    negative = Histogram()
    with (root/"room_water.csv").open(encoding="utf-8",newline="") as stream:
        for row in csv.DictReader(stream):
            room,t = row["room_id"],row["timestamp_utc"]
            if room not in rooms:
                continue
            counts["room_rows"] += 1
            value = number(row.get("meter_total_l"))
            if value is None:
                counts["missing_room_rows"] += 1
            if room in previous:
                old_t,old = previous[room]
                if t <= old_t:
                    counts["duplicate_or_out_of_order_room_rows"] += 1
                    continue
                if _time(t)-_time(old_t) != timedelta(minutes=15):
                    counts["room_interval_gaps"] += 1
                elif old is None or value is None:
                    counts["room_intervals_touching_missing_value"] += 1
                elif min(old,value) < 0:
                    counts["room_intervals_with_negative_total"] += 1
                else:
                    delta = value-old
                    signed.add(delta)
                    if delta < 0:
                        counts["room_negative_intervals"] += 1
                        counts["room_small_negative_le0.5l"] += abs(delta) <= .5+NUMERIC_ABS_TOL
                        counts["room_negative_gt100l"] += abs(delta) > 100
                        negative.add(delta)
                    elif delta <= 2000:
                        paired[t][0] += 1
                        paired[t][1] += delta
                    else:
                        counts["room_positive_delta_gt2000l"] += 1
            previous[room] = (t,value)
    prior = None
    complete_run = 0
    last_t = None
    residual = Histogram()
    with (root/"water_main.csv").open(encoding="utf-8",newline="") as stream:
        for row in csv.DictReader(stream):
            t,value = row["timestamp_utc"],number(row.get("meter_total_l"))
            counts["main_rows"] += 1
            complete = False
            if prior:
                old_t,old = prior
                valid = (_time(t)-_time(old_t) == timedelta(minutes=15) and old is not None and value is not None
                         and min(old,value) >= 0 and 0 <= value-old <= 50000)
                if valid and paired[t][0] == len(rooms):
                    complete = True
                    residual.add(value-old-paired[t][1])
            contiguous = last_t is None or _time(t)-_time(last_t) == timedelta(minutes=15)
            complete_run = complete_run+1 if complete and contiguous else 0
            strict = complete_run >= 4
            counts["latest_pair_complete_ticks"] += complete
            counts["strict_1h_complete_ticks"] += strict
            counts["latest_pair_available_but_1h_blocked_ticks"] += complete and not strict
            prior,last_t = (t,value),t
    return {"dataset":{k:manifest.get(k) for k in ("profile","start_utc","end_utc")},
            "counts":dict(counts),"signed_room_pair_distribution":signed.report(),
            "negative_room_pair_distribution":negative.report(),"matched_main_room_residual_distribution":residual.report(),
            "source_sha256":{name:_digest(root/name) for name in ("building.json","manifest.json","room_water.csv","water_main.csv")},
            "scope":"Raw CSV interval availability and signed-quality distribution only. No occupancy classification, missing-value reconstruction, physical reset truth, leak labels or accuracy claims."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir",type=Path,action="append",required=True,help="Repeat for independent profiles")
    parser.add_argument("--baseline-ref",default=BASELINE_REF)
    parser.add_argument("--out",type=Path,required=True)
    parser.add_argument("--raw-only",action="store_true",help="Independent streaming CSV completeness cross-check only")
    parser.add_argument("--sample-limit",type=int,default=12)
    parser.add_argument("--max-ticks",type=int,help="Explicitly partial smoke run")
    args = parser.parse_args()
    if not 1 <= args.sample_limit <= 100 or args.max_ticks is not None and args.max_ticks <= 0:
        parser.error("Use positive limits; sample-limit at most100")
    if args.raw_only and args.max_ticks is not None:
        parser.error("max-ticks applies only to paired detector evaluation")
    if any(args.out.resolve().is_relative_to(p.resolve()) for p in args.data_dir):
        parser.error("Diagnostics must be outside source datasets")
    try:
        factories,provenance = ({}, {}) if args.raw_only else load_rule_pair(args.baseline_ref)
        reports = []
        for root in args.data_dir:
            report = raw_completeness(root) if args.raw_only else evaluate_dataset(root,factories,provenance,sample_limit=args.sample_limit,max_ticks=args.max_ticks)
            reports.append(report)
            print(json.dumps({"profile":report["dataset"]["profile"],"ticks":report.get("ticks_evaluated"),"elapsed_seconds":round(report.get("elapsed_seconds",0),3),"raw_only":args.raw_only}),flush=True)
        result = {"evaluation_version":"1.0", "evaluation_mode":"raw_csv_crosscheck" if args.raw_only else "paired_chronological_detector",
                  "scope":"Unlabelled water diagnostics only; no ground-truth precision/recall, leak truth or measured intervention benefit.",
                  "evaluator_source_sha256":_digest(Path(__file__)),"profiles_are_independent":True,"profiles":reports}
    except (OSError,ValueError,KeyError,TypeError) as exc:
        parser.exit(2,f"Water evaluation failed: {exc}\n")
    args.out.parent.mkdir(parents=True,exist_ok=True)
    args.out.write_text(json.dumps(result,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
