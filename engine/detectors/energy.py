"""S03: sustained occupied discomfort; contextual, status-aware HP1 checks."""
from collections import defaultdict
from datetime import timedelta
from statistics import median

from engine.core import (Candidate, DetectionResult, EngineConfig, ago, instant,
                         number, observations, room_state, utc)


class EnergyDetector:
    def __init__(self, config: EngineConfig):
        self.config = config
        self.healthy_counts = defaultdict(int)

    def detect(self, event, repo, building: dict) -> DetectionResult:
        result = DetectionResult()
        if event.event_type != "clock_tick":
            return result
        now = event.available_at_utc
        start = ago(now, self.config.cold_window_minutes)
        groups = defaultdict(list)
        for row in observations(repo, "room_telemetry.csv", start, now):
            if row.get("room_id"):
                groups[row["room_id"]].append(row)
        low, high = building["constraints"]["occupied_comfort_c"]
        cold_rooms = []
        until = utc(instant(now) + timedelta(hours=12))
        forecasts = [r for r in repo.get_weather_forecast(now, until)
                     if r.get("issued_utc") and instant(r["issued_utc"]) <= instant(now)
                     and r.get("target_utc") and instant(now) <= instant(r["target_utc"]) <= instant(until)]
        # Keep the latest known issue per target; do not borrow a later forecast.
        latest = {}
        for row in forecasts:
            key = row["target_utc"]
            if key not in latest or instant(row["issued_utc"]) > instant(latest[key]["issued_utc"]):
                latest[key] = row
        tariffs = [r for r in repo.get_tariffs(now, until)
                   if r.get("published_utc") and instant(r["published_utc"]) <= instant(now)
                   and r.get("timestamp_utc") and instant(now) <= instant(r["timestamp_utc"]) <= instant(until)]
        # Verify actual column names against dataset README; no artificial future weather.
        cold_forecasts = [r for r in latest.values()
                          if number(r.get("outdoor_c_forecast")) is not None and number(r["outdoor_c_forecast"]) <= self.config.cold_forecast_c]
        for room in sorted(r["id"] for r in building["building"]["rooms"]):
            rows = groups.get(room, [])
            if not self._continuous(rows, now):
                continue
            values = [number(r.get("temperature_c")) for r in rows]
            if any(v is None or v < -10 or v > 50 for v in values):
                continue
            key = "comfort:" + room
            if all(low <= v <= high for v in values):
                self.healthy_counts[key] += 1
                if self.healthy_counts[key] >= 3:
                    result.resolved.add(key)
            else:
                self.healthy_counts[key] = 0
            if not all(v < low for v in values):
                continue
            states = [room_state(repo, room, r["timestamp_utc"]) for r in rows]
            if any(s.get("occupancy") != "occupied" for s in states):
                continue
            state = states[-1]
            evidence = [f"room_telemetry.csv:{room}:{rows[0]['timestamp_utc']}..{now}", *state.get("evidence", [])]
            details = ""
            if cold_forecasts:
                forecast = min(cold_forecasts, key=lambda r: float(r["outdoor_c_forecast"]))
                details += f" Known forecast {forecast['outdoor_c_forecast']} °C at {forecast['target_utc']}."
                evidence.append(f"weather_forecast.csv:issued={forecast['issued_utc']}:target={forecast['target_utc']}")
            if tariffs:
                prices = [number(r.get("price_eur_kwh")) for r in tariffs]
                prices = [p for p in prices if p is not None]
                if prices:
                    details += f" Published price up to {max(prices):.3f} EUR/kWh does not override occupied comfort."
                    evidence.extend(f"tariff.csv:{r['timestamp_utc']}:published={r['published_utc']}" for r in tariffs)
            common = dict(key=key, asset=room, issue="heating_comfort", evidence=evidence,
                          priority="high" if min(values) < low - 1 else "medium",
                          verification=f"Check occupied room temperature returns to {low}–{high} °C within 60 min; escalate persistent cold.",
                          context={"room": state})
            window = any(number(r.get("window_open")) == 1 for r in rows)
            if not window:
                cold_rooms.append(room)
            setpoint = number(rows[-1].get("setpoint_c"))
            if window:
                action = "notify_guest"
                reason = " Window was observed open; propose checking it before diagnosing plant failure."
                context = {"room": state}
            elif setpoint is not None and setpoint < min(high, 22):
                action = "adjust_room_setpoint"
                target = min(high, max(low, 22))
                context = {"room": state, "setpoint_c": target}
                reason = f" Propose heating setpoint {target} °C within occupied comfort limits."
            else:
                action = "request_inspection"
                context = {"room": state, "entry_required": True}
                reason = " Heating demand is already raised or setpoint is unknown; request consent-based inspection rather than assume a control failure."
            common["context"] = context
            result.candidates.append(Candidate(**common, action=action,
                reason=f"Observed occupied room below {low} °C for {self.config.cold_window_minutes} min ({min(values):.2f}–{max(values):.2f} °C)." + reason + details,
                outcome="Propose comfort recovery; future telemetry remains observed history.",
                human=action == "request_inspection", consent=action == "request_inspection"))
        # COP needs simultaneous non-defrost running samples and plausible outdoor context.
        equipment = observations(repo, "equipment.csv", ago(now, 60), now, entity="HP1")
        by_time = defaultdict(dict)
        for row in equipment:
            by_time[row["timestamp_utc"]][row.get("metric")] = row.get("value")
        cop = []
        for t, row in sorted(by_time.items()):
            heat, electric = number(row.get("heat_output_kw")), number(row.get("electric_kw"))
            if row.get("status") == "running" and heat is not None and electric is not None and 0 < heat <= 200 and 2 <= electric <= 100:
                cop.append((t, heat / electric))
        weather = observations(repo, "weather.csv", ago(now, 60), now)
        temperatures = [number(r.get("outdoor_c")) for r in weather]
        temperatures = [v for v in temperatures if v is not None and -30 <= v <= 45]
        continuous = (len(cop) >= 4 and instant(cop[-1][0]) == instant(now)
                      and all(instant(b[0]) - instant(a[0]) == timedelta(minutes=15)
                              for a, b in zip(cop, cop[1:])))
        if continuous and all(v < self.config.low_cop for _, v in cop):
            plausible = bool(temperatures and min(temperatures) > -7 and weather and instant(weather[-1]["timestamp_utc"]) >= instant(ago(now, 30)))
            action = "dispatch_technician" if cold_rooms and plausible else "flag_anomaly"
            result.candidates.append(Candidate("plant:HP1", "HP1", "heating_performance", action,
                f"HP1 running-only COP median {median(v for _, v in cop):.2f} over {len(cop)} contiguous samples, threshold {self.config.low_cop}; defrost/standby excluded. {len(cold_rooms)} persistently cold occupied rooms. Outdoor conditions {'support service review' if plausible else 'are missing/extreme; efficiency interpretation uncertain'}.",
                [f"equipment.csv:HP1:{cop[0][0]}..{now}"] + ([f"weather.csv:{ago(now, 60)}..{now}"] if weather else []) +
                [f"room_telemetry.csv:{room}:{start}..{now}" for room in cold_rooms],
                "Review defrost cycle and heat/electric sensors; confirm sustained comfort recovery and plant performance after approved service.",
                "Propose plant service, never a measured repair or cooling capability.",
                priority="high" if cold_rooms else "medium", human=action == "dispatch_technician"))
        elif continuous and all(v >= self.config.low_cop for _, v in cop):
            result.resolved.add("plant:HP1")
        return result

    def _continuous(self, rows, now):
        return (len(rows) >= self.config.cold_window_minutes // 15 + 1
                and instant(rows[-1]["timestamp_utc"]) == instant(now)
                and all(instant(b["timestamp_utc"]) - instant(a["timestamp_utc"]) == timedelta(minutes=15)
                        for a, b in zip(rows, rows[1:])))
