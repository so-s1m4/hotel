"""S04: distinguish AHU filter/airflow degradation from missing telemetry."""
from collections import defaultdict
from datetime import timedelta

from engine.core import Candidate, DetectionResult, ago, instant, number, observations, utc


class MaintenanceDetector:
    def __init__(self, config):
        self.config = config
        self.seen = False
        self.healthy = defaultdict(int)

    def detect(self, event, repo, building, calendar, work_orders):
        result = DetectionResult()
        if event.event_type != "clock_tick":
            return result
        assets = {a["id"]: a for a in building["building"].get("assets", [])}
        if "AHU1" not in assets:
            return result
        now = event.available_at_utc
        rows = observations(repo, "equipment.csv", ago(now, 120), now, entity="AHU1")
        self.seen = self.seen or bool(rows)
        by_time = defaultdict(dict)
        for row in rows:
            by_time[row["timestamp_utc"]][row.get("metric")] = row.get("value")
        valid = [(t, number(r.get("filter_dp_pa")), number(r.get("airflow_m3h")))
                 for t, r in sorted(by_time.items())
                 if number(r.get("filter_dp_pa")) is not None and number(r.get("airflow_m3h")) is not None]
        fresh = [r for r in valid if instant(r[0]) >= instant(ago(now, 30))]
        if self.seen and not fresh:
            self.healthy["data:AHU1"] = 0
            result.candidates.append(Candidate("data:AHU1", "AHU1", "data_quality", "flag_anomaly",
                "AHU filter/airflow evidence is missing or stale; plant degradation cannot be inferred from absent measurements.",
                [f"equipment.csv:AHU1:queried={ago(now, 120)}..{now} (missing/stale expected metrics)"],
                "Restore sensor/gateway observations and verify timestamps before any plant diagnosis.",
                "Queue a telemetry investigation, not a filter repair."))
            return result
        if fresh:
            self.healthy["data:AHU1"] += 1
            if self.healthy["data:AHU1"] >= 3:
                result.resolved.add("data:AHU1")
        else:
            self.healthy["data:AHU1"] = 0
        window = [r for r in valid if instant(r[0]) >= instant(ago(now, 60))]
        contiguous = (len(window) >= 5 and instant(window[-1][0]) == instant(now)
                      and all(instant(b[0]) - instant(a[0]) == timedelta(minutes=15)
                              for a, b in zip(window, window[1:])))
        nominal = number(assets["AHU1"].get("nominal_airflow_m3h"))
        if not contiguous or not nominal or nominal <= 0:
            return result
        plausible = all(0 <= dp <= 2000 and 0 < flow <= nominal * 2 for _, dp, flow in window)
        if not plausible:
            return result
        high = all(dp >= self.config.filter_dp_pa for _, dp, _ in window)
        low_flow = all(flow <= nominal * self.config.ahu_flow_fraction for _, _, flow in window)
        if not high:
            self.healthy["plant:AHU1"] += 1
            if self.healthy["plant:AHU1"] >= 3:
                result.resolved.add("plant:AHU1")
            return result
        self.healthy["plant:AHU1"] = 0
        evidence = [f"equipment.csv:AHU1:{window[0][0]}..{now}"]
        common = dict(key="plant:AHU1", asset="AHU1", issue="ventilation_maintenance", evidence=evidence,
                      verification="Verify filter pressure and airflow under comparable operating demand after approved inspection; check sensors first.",
                      outcome="Propose a maintenance workflow, without asserting a measured repair.")
        reason = (f"AHU filter differential pressure ≥{self.config.filter_dp_pa} Pa for 60 min; "
                  f"airflow {'also below' if low_flow else 'not consistently below'} {self.config.ahu_flow_fraction:.0%} of nominal {nominal:.0f} m³/h. "
                  "Thresholds are prototype assumptions; operating demand and calibration remain alternatives.")
        if not low_flow:
            result.candidates.append(Candidate(**common, action="flag_anomaly", reason=reason))
            return result
        until = utc(instant(now) + timedelta(hours=24))
        known = [o for o in work_orders if o.get("asset_id") == "AHU1" and o.get("created_utc")
                 and instant(o["created_utc"]) <= instant(now) and o.get("scheduled_utc")
                 and instant(now) <= instant(o["scheduled_utc"]) <= instant(until)]
        if known:
            evidence.extend(f"work_orders.csv:{o.get('work_order_id')} (known schedule only)" for o in known)
            result.candidates.append(Candidate(**common, action="defer_action", reason=reason + " Existing known service plan; confirm it rather than duplicate a contractor call.", variant="known_order"))
            return result
        slots = repo.get_staff_slots(now, until, now, role="technician")
        choice = calendar.first_slot(slots, now, until, self.config.technician_minutes, role="technician")
        if choice:
            start, end, resource, slot = choice
            evidence.append(f"staff_roster.csv:{slot['shift_id']}")
            result.candidates.append(Candidate(**common, action="dispatch_technician", human=True,
                resource=resource, priority="high", reason=reason + f" Propose {resource} {start}..{end}, subject to approval.",
                context={"slot": slot, "start_utc": start, "end_utc": end}, variant="inhouse"))
        else:
            result.candidates.append(Candidate(**common, action="order_service", human=True, priority="high",
                reason=reason + " No eligible known technician slot in the next 24 h; request external service and confirm availability/cost.",
                context={"notable_cost": True, "service": "ventilation"}, variant="external"))
        return result
