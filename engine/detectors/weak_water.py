"""Causal review of persistent weak flow, without assuming an elevated baseline is normal.

This helper does not identify true leaks. A slow meter drift or unrecorded activity
can produce the same cumulative readings. Its only output is an uncertain review
flag. No missing interval is reconstructed and negative deltas remain signed.

The caller establishes historical vacancy, fresh cleanliness and absence of known
operations for BOTH endpoints of each interval; ``eligible=False`` breaks a run.
The helper independently validates the raw pair and night/time continuity.
"""
from __future__ import annotations

from collections import Counter, defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from math import fsum
from typing import Any

from engine.core import Candidate, VIENNA, instant, number, utc

# Binary subtraction of decimal cumulative readings can straddle an exact policy
# boundary by a few ULPs. This is a comparison tolerance, never a rewrite/clamp of
# observed counters or signed metrics. The retrospective evaluator uses the same
# absolute tolerance for boundary classification.
WATER_BOUNDARY_ABS_TOL = 1e-9


def water_boundary_tolerance(*boundary_scales: float) -> float:
    """Tiny policy scales/bands must not be swallowed by an absolute tolerance.

    Defaults retain a 1e-9 L cap; the relative cap is one millionth of the
    smallest compared boundary. A strict zero allowance stays exactly zero.
    This function only controls comparisons; raw source quantities stay signed.
    """
    return min(WATER_BOUNDARY_ABS_TOL,
               *(abs(scale)*1e-6 for scale in boundary_scales)) if boundary_scales else WATER_BOUNDARY_ABS_TOL


@dataclass(frozen=True)
class _Interval:
    start: datetime
    end: datetime
    before: float
    after: float
    eligible: bool
    evidence: tuple[str, ...]

    @property
    def delta(self) -> float:
        return self.after-self.before


class PersistentWeakFlowDetector:
    """Bounded signed-counter persistence review; all thresholds are prototype assumptions.

    Default persistence is four elapsed hours, which is reachable inside the
    unchanged [00:00,06:00) local night even on the spring DST night. Independent
    quiet-baseline increase detection may remain a separate, faster rule.
    """

    def __init__(self, config):
        self.window_minutes = getattr(config, "water_weak_persistent_window_minutes", 240)
        self.minimum_rate = getattr(config, "water_low_min_l_per_interval", 1.0)
        self.maximum_rate = getattr(config, "water_min_l_per_interval", 3.0)
        self.max_backstep_l = getattr(config, "water_weak_max_backstep_l", 0.5)
        if (not isinstance(self.window_minutes, int) or isinstance(self.window_minutes, bool)
                or self.window_minutes not in {240, 300}):
            raise ValueError("Persistent weak-water window must be 240 or 300 minutes within the local night")
        if not 0 < self.minimum_rate < self.maximum_rate:
            raise ValueError("Weak-water review rates must be positive and below the strong-flow floor")
        if not 0 <= self.max_backstep_l <= 1:
            raise ValueError("Weak-water small-backstep tolerance must be between 0 and 1 L")
        self.rate_tolerance = water_boundary_tolerance(self.minimum_rate, self.maximum_rate-self.minimum_rate)
        self.backstep_tolerance = water_boundary_tolerance(self.max_backstep_l)
        self.intervals = self.window_minutes//15
        self._history: dict[str, deque[_Interval]] = defaultdict(lambda: deque(maxlen=self.intervals))
        self._last: dict[str, tuple[datetime, tuple[Any, ...] | None]] = {}
        self.diagnostics = Counter()

    def invalidate_operation(self, room: str, start: str, end: str) -> None:
        """Drop evidence affected by a newly known operation, preserving clean tail.

        Both the source intervals and operation scope use inclusive endpoints.
        Earlier unaffected prefix cannot be joined across an affected interval;
        only the continuous suffix after the last intersection remains usable.
        The caller controls availability of this information. This method never
        issues/retracts decisions and never changes the last-observation guard.
        """
        start_dt, end_dt = instant(start), instant(end)
        if end_dt < start_dt:
            raise ValueError("Operation scope end precedes start")
        self.diagnostics["operation_invalidation_calls"] += 1
        history = self._history.get(room)
        if not history:
            return
        last_overlap = None
        for index, interval in enumerate(history):
            if interval.start <= end_dt and start_dt <= interval.end:
                last_overlap = index
        if last_overlap is None:
            return
        unaffected_tail = list(history)[last_overlap+1:]
        history.clear()
        history.extend(unaffected_tail)
        self.diagnostics["operation_invalidations"] += 1
        self.diagnostics["operation_intervals_invalidated"] += last_overlap+1

    @staticmethod
    def _pair(room: str, readings: list[dict], now: datetime):
        """Select the exact two source endpoints; conflicting duplicates stay unknown."""
        required = {now-timedelta(minutes=15), now}
        found: dict[datetime, float | None] = {}
        conflicts = set()
        for row in readings:
            if row.get("room_id") != room:
                continue
            stamp = row.get("timestamp_utc")
            if not stamp:
                continue
            try:
                at = instant(stamp)
            except (ValueError, TypeError):
                continue
            if at not in required:
                continue
            value = number(row.get("meter_total_l"))
            if value is not None and value < 0:
                value = None
            if at in found and found[at] != value:
                conflicts.add(at)
            else:
                found.setdefault(at, value)
        if conflicts:
            return None, "conflicting_duplicate_pair"
        if set(found) != required or any(value is None for value in found.values()):
            return None, "missing_or_invalid_pair"
        start = now-timedelta(minutes=15)
        return (start, now, found[start], found[now]), None

    def observe(self, room: str, readings: list[dict], now: str, *, eligible: bool,
                eligibility_evidence: list[str] | None = None) -> Candidate | None:
        """Observe one historical interval; return a review proposal, never execution.

        ``readings`` can be the caller's existing room window. Only the exact
        previous-quarter and current source readings are used. A gap, an invalid
        pair, large reversal or an ineligible operation invalidates continuity.
        """
        now_dt = instant(now)
        pair, invalid = self._pair(room, readings, now_dt)
        fingerprint = (*pair, eligible is True) if pair is not None else None
        previous = self._last.get(room)
        if previous:
            last_at, last_fingerprint = previous
            if now_dt < last_at:
                self._history[room].clear()
                self.diagnostics["out_of_order_observations"] += 1
                return None
            if now_dt == last_at:
                if fingerprint != last_fingerprint:
                    self._history[room].clear()
                    self._last[room] = (now_dt, None)
                    self.diagnostics["conflicting_repeated_observations"] += 1
                return None
            if now_dt-last_at != timedelta(minutes=15):
                self._history[room].clear()
                self.diagnostics["observation_gaps"] += 1
        self._last[room] = (now_dt, fingerprint)
        self.diagnostics["observations"] += 1
        history = self._history[room]
        if pair is None:
            history.clear()
            self.diagnostics["pair_invalid"] += 1
            self.diagnostics[invalid] += 1
            return None
        start, end, before, after = pair
        if history and (history[-1].end != start or history[-1].after != before):
            history.clear()
            self.diagnostics["counter_endpoint_discontinuities"] += 1
        local_night = all(0 <= t.astimezone(VIENNA).hour < 6 for t in (start, end))
        valid_eligibility = eligible is True and local_night
        delta = after-before
        if delta < -self.max_backstep_l - self.backstep_tolerance:
            valid_eligibility = False
            self.diagnostics["large_counter_reversals"] += 1
        elif delta > 2000 + WATER_BOUNDARY_ABS_TOL:
            valid_eligibility = False
            self.diagnostics["implausible_counter_increases"] += 1
        interval = _Interval(start, end, before, after, valid_eligibility,
                             tuple(eligibility_evidence or ()))
        history.append(interval)
        if not valid_eligibility:
            self.diagnostics["ineligible_intervals"] += 1
            # Discard rather than bridging a failed interval when it ages out.
            history.clear()
            return None
        self.diagnostics["eligible_intervals"] += 1
        if len(history) < self.intervals:
            self.diagnostics["history_incomplete"] += 1
            return None
        window = list(history)
        if (window[-1].end-window[0].start != timedelta(minutes=self.window_minutes)
                or any(a.end != b.start or a.after != b.before for a, b in zip(window, window[1:]))):
            history.clear()
            self.diagnostics["window_discontinuities"] += 1
            return None
        net = window[-1].after-window[0].before
        rate = net/self.intervals
        if (rate < self.minimum_rate - self.rate_tolerance
                or rate >= self.maximum_rate - self.rate_tolerance):
            self.diagnostics["net_rate_outside_weak_band"] += 1
            return None
        negatives = [-step.delta for step in window if step.delta < 0]
        negative_liters = fsum(negatives)
        # Small reversals are retained in endpoint/net accounting; never clipped.
        negative_budget = min(1.0, net*0.1)
        if len(negatives) > 2 or negative_liters > negative_budget + water_boundary_tolerance(negative_budget):
            self.diagnostics["excessive_counter_jitter"] += 1
            return None
        meaningful_limit = self.minimum_rate*0.25
        meaningful = [step.delta > meaningful_limit + water_boundary_tolerance(meaningful_limit) for step in window]
        if sum(meaningful) < self.intervals*0.75 or any(sum(meaningful[i:i+4]) < 2 for i in range(0, len(window), 4)):
            self.diagnostics["sparse_or_quantized_progress"] += 1
            return None
        concentration_limit = net*0.2
        if max(step.delta for step in window) > concentration_limit + water_boundary_tolerance(concentration_limit):
            self.diagnostics["concentrated_counter_progress"] += 1
            return None
        hourly = [window[i+3].after-window[i].before for i in range(0, len(window), 4)]
        hourly_floor = self.minimum_rate*2
        if min(hourly) < hourly_floor - water_boundary_tolerance(hourly_floor):
            self.diagnostics["nonpersistent_hourly_progress"] += 1
            return None
        hourly_ceiling = min(hourly)*4
        if max(hourly) > hourly_ceiling + water_boundary_tolerance(hourly_ceiling):
            self.diagnostics["bursty_hourly_progress"] += 1
            return None
        # Distributed quarter-hour and hourly endpoint progress exclude sparse
        # taps/large quantization jumps; monotone meter drift remains fundamentally
        # indistinguishable and is explicitly disclosed below.
        self.diagnostics["review_windows"] += 1
        metrics = {"evidence_kind": "persistent_unexplained", "baseline_used": False,
                   "window_minutes": self.window_minutes, "intervals": self.intervals,
                   "net_liters": net, "net_l_per_interval": rate,
                   "negative_intervals": len(negatives), "negative_liters": negative_liters,
                   "hourly_nets_l": hourly, "small_backstep_limit_l": self.max_backstep_l,
                   "meaningfully_positive_intervals": sum(meaningful)}
        proof = [f"room_water.csv:{room}:{utc(window[0].start)}..{now}"]
        proof.extend(ref for interval in window for ref in interval.evidence)
        return Candidate(
            "water:"+room, room, "water_low_rate_review", "flag_anomaly",
            f"Persistent unexplained night counter increase in a historically verified vacant/clean room: "
            f"signed endpoint net {net:.2f} L across {self.window_minutes} min ({rate:.2f} L/15 min), "
            f"hourly net range {min(hourly):.2f}–{max(hourly):.2f} L, {len(negatives)} bounded small counter reversals retained. "
            "No quiet prior baseline is assumed: an elevated historical rate is not proof of normality. "
            "This is an uncertain review candidate, not a confirmed leak; meter drift/bias, calibration, quantization and unrecorded activity can produce the same evidence.",
            list(dict.fromkeys(proof)),
            "Independently verify meter resolution/calibration and actual fixture flow; check unrecorded operations before any room entry, service or shutoff. Large resets and missing intervals require new evidence, not imputation.",
            "Flag prolonged unexplained weak flow for review only; no inspection, water intervention, repair or statistical confidence is asserted.",
            context={"weak_water_metrics": metrics, "water_path": "persistent_weak"}, variant="initial",
        )
