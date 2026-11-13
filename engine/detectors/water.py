"""S02: sustained vacant-room night flow and conservative main-meter residuals."""
from collections import Counter, defaultdict, deque
from datetime import timedelta
from statistics import median

from engine.core import (Candidate, DetectionResult, EngineConfig, VIENNA, ago,
                         instant, number, observations, room_state, utc)


def meter_deltas(rows: list[dict], max_delta: float = 2000) -> list[tuple[str, float]] | None:
    """Require contiguous quarter-hour samples; reset/gap/invalid != consumption."""
    if len(rows) < 2:
        return None
    result = []
    for a, b in zip(rows, rows[1:]):
        before, after = number(a.get("meter_total_l")), number(b.get("meter_total_l"))
        if (before is None or after is None or before < 0 or after < before
                or instant(b["timestamp_utc"]) - instant(a["timestamp_utc"]) != timedelta(minutes=15)):
            return None
        delta = after - before
        if delta > max_delta:  # implausible single-room interval; no interpolation over it
            return None
        result.append((b["timestamp_utc"], delta))
    return result


class WaterDetector:
    def __init__(self, config: EngineConfig):
        self.config = config
        self.residual_history = deque(maxlen=672)  # preceding week, quarter-hourly
        self.residual_run = deque(maxlen=4)
        self.healthy_counts: dict[str, int] = defaultdict(int)
        self._last_tick = None
        self.diagnostics = Counter()
        self._room_history = defaultdict(lambda: deque(maxlen=672))
        self._low_active = set()

    def detect(self, event, repo, building: dict, work_orders: list[dict]) -> DetectionResult:
        result = DetectionResult()
        if event.event_type != "clock_tick":
            return result
        now = event.available_at_utc
        now_dt = instant(now)
        if self._last_tick is not None:
            if now_dt <= self._last_tick:
                return result
            if now_dt-self._last_tick != timedelta(minutes=15):
                self.healthy_counts.clear()
                self.residual_run.clear()
        self._last_tick = now_dt
        self.diagnostics["ticks_processed"] += 1
        while self.residual_history and self.residual_history[0][0] < now_dt-timedelta(days=7):
            self.residual_history.popleft()
        start = ago(now, self.config.water_window_minutes)
        groups = defaultdict(list)
        for row in observations(repo, "room_water.csv", start, now):
            if row.get("room_id"):
                groups[row["room_id"]].append(row)
        local = instant(now).astimezone(VIENNA)
        night = 0 <= local.hour < 6
        room_ids = {r["id"] for r in building["building"]["rooms"]}
        recent = repo.get_events(ago(now, 120), now, now)
        # Guard even a nonconforming gateway at the boundary.
        recent = [r for r in recent if r.get("timestamp_utc") and
                  instant(ago(now, 120)) <= instant(r["timestamp_utc"]) <= instant(now)]
        cleaning_rooms = {r.get("room_id") for r in recent
                          if r.get("type") in {"cleaning_started", "cleaning_completed", "robot_floor_cleaning_started", "robot_floor_cleaning_completed"}}
        deltas = {}
        for room in sorted(room_ids):
            readings = groups.get(room, [])
            flow = meter_deltas(readings)
            key = "water:" + room
            self._observe_low_rate(result, repo, room, readings, now, recent, cleaning_rooms, work_orders)
            if (flow is None or len(flow) < self.config.water_window_minutes // 15
                    or instant(flow[-1][0]) != now_dt):
                self.healthy_counts[key] = 0
                self.diagnostics["room_invalid_windows"] += 1
                self.diagnostics["main_blocked_room:" + room] += 1
                continue
            deltas[room] = dict(flow)
            recovery_limit = min(self.config.water_min_l_per_interval,
                                 getattr(self.config, "water_low_min_l_per_interval", 1.0)/2) if room in self._low_active else self.config.water_min_l_per_interval
            if all(v < recovery_limit for _, v in flow):
                self.healthy_counts[key] += 1
                if self.healthy_counts[key] >= 3:
                    result.resolved.add(key)
                    self._low_active.discard(room)
            else:
                self.healthy_counts[key] = 0
            if not night or room in cleaning_rooms:
                continue
            if any(not 0 <= instant(r["timestamp_utc"]).astimezone(VIENNA).hour < 6 for r in readings):
                continue
            values = [v for _, v in flow]
            if not all(self.config.water_min_l_per_interval <= v <= self.config.water_max_l_per_interval for v in values):
                continue
            if max(values) > min(values) * 2:
                continue  # variable guest/operational consumption is not persistent base flow
            if self._known_work(room, now, work_orders):
                continue
            if any(room in {r.get("room_id"), r.get("from_room"), r.get("to_room")} and instant(start) <= instant(r["timestamp_utc"]) and
                   r.get("type") in {"checkin", "checkin_early", "checkin_after_wait", "checkout", "checkout_early", "room_reassigned", "stay_in_progress"}
                   for r in recent):
                continue
            states = [room_state(repo, room, row["timestamp_utc"]) for row in readings]
            if any(s.get("room_id") != room or s.get("occupancy") != "vacant" or s.get("housekeeping_state") != "clean"
                   or not s.get("last_observed_utc")
                   or not instant(r["timestamp_utc"])-timedelta(minutes=self.config.telemetry_max_age_minutes) <= instant(s["last_observed_utc"]) <= instant(r["timestamp_utc"])
                   for r, s in zip(readings, states)):
                continue
            evidence = [f"room_water.csv:{room}:{readings[0]['timestamp_utc']}..{now}"]
            for s in states:
                evidence.extend(s.get("evidence", []))
            result.candidates.append(Candidate(key, room, "water_leak", "request_inspection",
                f"Vacant, observed-clean room has sustained night flow {min(values):.1f}–{max(values):.1f} L/15 min for {self.config.water_window_minutes} min. Possible leak; no recent cleaning or known flushing. Meter model and occupancy remain uncertain.",
                evidence, "Confirm meter continuity and inspect fixture; verify subsequent flow stops, otherwise seek technician approval.",
                "Queue a vacant-room inspection; do not shut off water or claim a repaired leak.",
                priority="high", context={"room": states[-1], "entry_required": True}))
        # Residual uses matched intervals from every room; partial sums are not evidence.
        main = observations(repo, "water_main.csv", start, now)
        main_flow = meter_deltas(main, max_delta=50000)
        self.diagnostics["main_windows_evaluated"] += 1
        if not main_flow or instant(main_flow[-1][0]) != now_dt:
            self.diagnostics["main_invalid_or_missing_windows"] += 1
        if room_ids-deltas.keys():
            self.diagnostics["main_incomplete_room_windows"] += 1
        if main_flow and instant(main_flow[-1][0]) == instant(now) and room_ids <= deltas.keys():
            t, total = main_flow[-1]
            if all(t in deltas[room] for room in room_ids):
                self.diagnostics["main_complete_windows"] += 1
                residual = total - sum(deltas[room][t] for room in room_ids)
                cutoff = now_dt-timedelta(minutes=60)
                baseline = [value for stamp, value in self.residual_history if stamp < cutoff]
                base = median(baseline) if len(baseline) >= 24 else None
                if base is None:
                    self.diagnostics["main_baseline_unavailable_windows"] += 1
                mad = median([abs(v - base) for v in baseline]) if base is not None else None
                high = (night and base is not None and residual >
                        max(self.config.main_residual_min_l_per_interval, base + max(60, 6 * mad)))
                self.residual_run.append((now, residual, high))
                if len(self.residual_run) == 4 and all(x[2] for x in self.residual_run) and all(
                    instant(b[0]) - instant(a[0]) == timedelta(minutes=15)
                    for a, b in zip(self.residual_run, list(self.residual_run)[1:])):
                    result.candidates.append(Candidate("water:WM_MAIN", "WM_MAIN", "water_balance", "flag_anomaly",
                        f"Main-minus-all-room flow elevated for one hour; latest {residual:.1f} L/15 min, preceding median {base:.1f}. Common-area use, irrigation, losses and metering offsets are alternative explanations; not proof of a leak.",
                        [f"water_main.csv:{ago(now, 60)}..{now}", f"room_water.csv:all_rooms:{ago(now, 60)}..{now}", f"water_main.csv:baseline:{utc(self.residual_history[0][0])}..{ago(now, 60)} (minus matched room deltas)"],
                        "Compare common-area/irrigation schedules and meter calibration before any service or shutoff.",
                        "Flag unexplained main-meter residual for review only."))
                if not high and base is not None and abs(residual - base) <= max(30, 3 * mad):
                    self.healthy_counts["water:WM_MAIN"] += 1
                    if self.healthy_counts["water:WM_MAIN"] >= 3:
                        result.resolved.add("water:WM_MAIN")
                else:
                    self.healthy_counts["water:WM_MAIN"] = 0
                self.residual_history.append((now_dt, residual))
        else:
            self.residual_run.clear()
            self.healthy_counts["water:WM_MAIN"] = 0
        return result

    def _observe_low_rate(self, result, repo, room, readings, now, recent, cleaning_rooms, orders):
        """Retain causal eligible intervals; prior nights are comparison, never future labels."""
        now_dt = instant(now)
        history = self._room_history[room]
        while history and history[0][0] < now_dt-timedelta(days=7):
            history.popleft()
        pair = readings[-2:]
        pair_flow = meter_deltas(pair)
        delta = pair_flow[-1][1] if pair_flow and instant(pair_flow[-1][0]) == now_dt else None
        # Daytime/invalid observations still break the continuous low-rate run.
        eligible = False
        states = []
        if delta is not None and all(0 <= instant(r["timestamp_utc"]).astimezone(VIENNA).hour < 6 for r in pair):
            start_dt = instant(pair[0]["timestamp_utc"])
            transitioning = any(room in {r.get("room_id"), r.get("from_room"), r.get("to_room")}
                                and start_dt < instant(r["timestamp_utc"]) <= now_dt
                                and r.get("type") in {"checkin", "checkin_early", "checkin_after_wait", "checkout", "checkout_early", "room_reassigned", "stay_in_progress"}
                                for r in recent)
            if room not in cleaning_rooms and not transitioning and not self._known_work(room, now, orders):
                states = [room_state(repo, room, r["timestamp_utc"]) for r in pair]
                eligible = all(s.get("room_id") == room and s.get("occupancy") == "vacant"
                               and s.get("housekeeping_state") == "clean" and s.get("last_observed_utc")
                               and instant(r["timestamp_utc"])-timedelta(minutes=self.config.telemetry_max_age_minutes) <= instant(s["last_observed_utc"]) <= instant(r["timestamp_utc"])
                               for r, s in zip(pair, states))
        history.append((now_dt, delta, eligible))
        if eligible:
            self.diagnostics["low_rate_eligible_intervals"] += 1
        minimum = getattr(self.config, "water_low_min_l_per_interval", 1.0)
        # Quiet eligible intervals remain in history for later independent baselines,
        # but cannot finish a current elevated-flow window. Avoid copying/scanning
        # the week of history on the overwhelmingly common quiet/daytime ticks.
        if not eligible or delta is None or not minimum <= delta < self.config.water_min_l_per_interval:
            return
        window_minutes = getattr(self.config, "water_low_window_minutes", 180)
        count = window_minutes//15
        if len(history) < count:
            return
        window = list(history)[-count:]
        if not all(ok and value is not None for _, value, ok in window):
            return
        if any(b[0]-a[0] != timedelta(minutes=15) for a, b in zip(window, window[1:])):
            return
        values = [value for _, value, _ in window]
        if not all(minimum <= value < self.config.water_min_l_per_interval for value in values):
            return
        if max(values)/min(values) > 1.5 or max(values)-min(values) > max(0.5, median(values)*0.5):
            self.diagnostics["low_rate_variable_windows"] += 1
            return
        self.diagnostics["low_rate_candidate_windows"] += 1
        night = now_dt.astimezone(VIENNA).date()
        baseline = [(stamp, value) for stamp, value, ok in history if ok and value is not None
                    and stamp < window[0][0]-timedelta(minutes=15)
                    and stamp.astimezone(VIENNA).date() < night]
        needed = getattr(self.config, "water_low_baseline_min_intervals", 12)
        if len(baseline) < needed:
            self.diagnostics["low_rate_baseline_unavailable_windows"] += 1
            self.diagnostics["low_rate_no_baseline_room:"+room] += 1
            return
        base_values = sorted(value for _, value in baseline)
        base = median(base_values)
        p90 = base_values[min(len(base_values)-1, int(len(base_values)*0.9))]
        if base > minimum*0.25 or p90 > minimum*0.5:
            self.diagnostics["low_rate_baseline_not_quiet_windows"] += 1
            return
        if min(values) < max(minimum, base+0.75*minimum, p90+0.5*minimum):
            return
        self._low_active.add(room)
        self.healthy_counts["water:"+room] = 0
        self.diagnostics["low_rate_review_windows"] += 1
        start = utc(window[0][0]-timedelta(minutes=15))
        result.candidates.append(Candidate("water:"+room, room, "water_low_rate_review", "flag_anomaly",
            f"Vacant, observed-clean room has stable low-rate night flow {min(values):.2f}–{max(values):.2f} L/15 min for {window_minutes} min; independent earlier-night baseline median {base:.2f}, p90 {p90:.2f} over {len(baseline)} eligible intervals. This is an uncertain review candidate, not a confirmed leak; meter bias and unrecorded activity remain alternatives.",
            [f"room_water.csv:{room}:{start}..{now}",
             f"room_water.csv:{room}:earlier_night_baseline:{utc(baseline[0][0])}..{utc(baseline[-1][0])}",
             f"room_telemetry.csv:{room}:{start}..{now} (vacant/clean historical checks)",
             f"events.jsonl:{room}:{start}..{now} (lifecycle/operation checks)"],
            "Review meter resolution, verify independent fixture flow and unrecorded operations before arranging any entry or water intervention.",
            "Flag a long-duration low-rate anomaly for review only; no inspection, shutoff, repair or statistical confidence is asserted.",
            context={"room": states[-1]} if states else {}))

    @staticmethod
    def _known_work(room: str, now: str, orders: list[dict]) -> bool:
        for order in orders:
            if not order.get("created_utc") or instant(order["created_utc"]) > instant(now):
                continue
            scheduled = order.get("scheduled_utc")
            summary = str(order.get("summary") or "").casefold()
            applies = order.get("asset_id") in {room, "WM_" + room} or room.lower() in summary
            if (applies and scheduled and 0 <= (instant(now) - instant(scheduled)).total_seconds() <= 7200
                    and any(word in summary for word in ("flush", "meter", "legionella", "tap"))):
                return True
        return False
