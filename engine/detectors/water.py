"""S02: sustained vacant-room night flow and conservative main-meter residuals."""
from collections import defaultdict, deque
from datetime import timedelta
from statistics import median

from engine.core import (Candidate, DetectionResult, EngineConfig, VIENNA, ago,
                         instant, number, observations, room_state)


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

    def detect(self, event, repo, building: dict, work_orders: list[dict]) -> DetectionResult:
        result = DetectionResult()
        if event.event_type != "clock_tick":
            return result
        now = event.available_at_utc
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
            if (flow is None or len(flow) < self.config.water_window_minutes // 15
                    or instant(flow[-1][0]) != instant(now)):
                continue
            deltas[room] = dict(flow)
            key = "water:" + room
            if all(v < self.config.water_min_l_per_interval for _, v in flow):
                self.healthy_counts[key] += 1
                if self.healthy_counts[key] >= 3:
                    result.resolved.add(key)
            else:
                self.healthy_counts[key] = 0
            if not night or room in cleaning_rooms:
                continue
            if any(not 0 <= instant(t).astimezone(VIENNA).hour < 6 for t, _ in flow):
                continue
            values = [v for _, v in flow]
            if not all(self.config.water_min_l_per_interval <= v <= self.config.water_max_l_per_interval for v in values):
                continue
            if max(values) > min(values) * 2:
                continue  # variable guest/operational consumption is not persistent base flow
            if self._known_work(room, now, work_orders):
                continue
            if any(r.get("room_id") == room and instant(start) <= instant(r["timestamp_utc"]) and
                   r.get("type") in {"checkin", "checkin_early", "checkin_after_wait", "checkout", "checkout_early", "room_reassigned", "stay_in_progress"}
                   for r in recent):
                continue
            states = [room_state(repo, room, row["timestamp_utc"]) for row in readings]
            if any(s.get("occupancy") != "vacant" or s.get("housekeeping_state") != "clean" for s in states):
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
        if main_flow and instant(main_flow[-1][0]) == instant(now) and room_ids <= deltas.keys():
            t, total = main_flow[-1]
            if all(t in deltas[room] for room in room_ids):
                residual = total - sum(deltas[room][t] for room in room_ids)
                baseline = [value for stamp, value in self.residual_history
                            if instant(stamp) < instant(ago(now, 60))]
                base = median(baseline) if len(baseline) >= 24 else None
                mad = median([abs(v - base) for v in baseline]) if base is not None else None
                high = (night and base is not None and residual >
                        max(self.config.main_residual_min_l_per_interval, base + max(60, 6 * mad)))
                self.residual_run.append((now, residual, high))
                if len(self.residual_run) == 4 and all(x[2] for x in self.residual_run) and all(
                    instant(b[0]) - instant(a[0]) == timedelta(minutes=15)
                    for a, b in zip(self.residual_run, list(self.residual_run)[1:])):
                    result.candidates.append(Candidate("water:WM_MAIN", "WM_MAIN", "water_balance", "flag_anomaly",
                        f"Main-minus-all-room flow elevated for one hour; latest {residual:.1f} L/15 min, preceding median {base:.1f}. Common-area use, irrigation, losses and metering offsets are alternative explanations; not proof of a leak.",
                        [f"water_main.csv:{ago(now, 60)}..{now}", f"room_water.csv:all_rooms:{ago(now, 60)}..{now}", f"water_main.csv:baseline:{self.residual_history[0][0]}..{ago(now, 60)} (minus matched room deltas)"],
                        "Compare common-area/irrigation schedules and meter calibration before any service or shutoff.",
                        "Flag unexplained main-meter residual for review only."))
                if not high and base is not None and abs(residual - base) <= max(30, 3 * mad):
                    self.healthy_counts["water:WM_MAIN"] += 1
                    if self.healthy_counts["water:WM_MAIN"] >= 3:
                        result.resolved.add("water:WM_MAIN")
                else:
                    self.healthy_counts["water:WM_MAIN"] = 0
                self.residual_history.append((now, residual))
        return result

    @staticmethod
    def _known_work(room: str, now: str, orders: list[dict]) -> bool:
        for order in orders:
            if not order.get("created_utc") or instant(order["created_utc"]) > instant(now):
                continue
            scheduled = order.get("scheduled_utc")
            summary = order.get("summary", "").lower()
            applies = order.get("asset_id") in {room, "WM_" + room} or room.lower() in summary
            if (applies and scheduled and abs((instant(now) - instant(scheduled)).total_seconds()) <= 7200
                    and any(word in summary for word in ("flush", "meter", "legionella", "tap"))):
                return True
        return False
