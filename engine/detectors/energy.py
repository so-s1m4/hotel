"""S03: sustained occupied discomfort; contextual, status-aware HP1 checks."""
from collections import defaultdict
from datetime import timedelta
from statistics import median

from engine.core import (Candidate, DetectionResult, EngineConfig, ago, instant,
                         number, observations, room_state, stable_id, utc)


class EnergyDetector:
    def __init__(self, config: EngineConfig):
        self.config = config
        self.healthy_counts = defaultdict(int)
        self._notice_priorities = defaultdict(dict)
        self._last_tick = None

    def detect(self, event, repo, building: dict) -> DetectionResult:
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
        self._last_tick = now_dt
        start = ago(now, self.config.cold_window_minutes)
        groups = defaultdict(list)
        for row in observations(repo, "room_telemetry.csv", start, now):
            if row.get("room_id"):
                groups[row["room_id"]].append(row)
        low, high = building["constraints"]["occupied_comfort_c"]
        recovery_margin = getattr(self.config, "comfort_recovery_margin_c", 0.25)
        if not 0 <= recovery_margin < (high-low)/2:
            raise ValueError("Comfort recovery margin must be smaller than half the building comfort band")
        cold_rooms = []
        until = utc(instant(now) + timedelta(hours=12))
        # Price/forecast enrichment is only needed for an actual occupied-cold finding.
        cold_forecasts = None
        tariffs = None
        transitions = None
        for room in sorted(r["id"] for r in building["building"]["rooms"]):
            rows = groups.get(room, [])
            key = "comfort:" + room
            if not self._continuous(rows, now):
                self.healthy_counts[key] = 0
                continue
            values = [number(r.get("temperature_c")) for r in rows]
            if any(v is None or v < -10 or v > 50 for v in values):
                self.healthy_counts[key] = 0
                continue
            if all(low+recovery_margin <= v <= high-recovery_margin for v in values):
                self.healthy_counts[key] += 1
                if self.healthy_counts[key] >= 3:
                    result.resolved.add(key)
                    self._notice_priorities.pop(room, None)
            else:
                self.healthy_counts[key] = 0
            if all(low <= v <= high for v in values):
                continue
            if transitions is None:
                transitions = [r for r in repo.get_events(start, now, now) if r.get("timestamp_utc")
                               and instant(start) < instant(r["timestamp_utc"]) <= now_dt
                               and r.get("type") in {"checkin", "checkin_early", "checkin_after_wait", "checkout", "checkout_early", "room_reassigned"}]
            if any(room in {r.get("room_id"), r.get("from_room"), r.get("to_room")} for r in transitions):
                continue
            if all(v > high for v in values):
                states = [room_state(repo, room, r["timestamp_utc"]) for r in rows]
                if not all(s.get("occupancy") == "occupied" for s in states):
                    continue
                setpoint = number(rows[-1].get("setpoint_c"))
                reduce = setpoint is not None and setpoint > high
                context = {"room": states[-1]}
                if reduce:
                    context["setpoint_c"] = high
                result.candidates.append(self._scope_comfort_notice(Candidate(key, room, "heating_comfort", "adjust_room_setpoint" if reduce else "notify_guest",
                    f"Occupied temperature above {high} °C for {self.config.cold_window_minutes} min ({min(values):.2f}–{max(values):.2f} °C). " +
                    (f"Reduce excessive heating setpoint to {high} °C within comfort limits; this is not active cooling." if reduce else
                     "Heating-only plant cannot actively cool; propose a ventilation/window check and investigate persistent overheating."),
                    [f"room_telemetry.csv:{room}:{rows[0]['timestamp_utc']}..{now}", *states[-1].get("evidence", [])],
                    "Verify observed temperature and guest comfort; escalate persistent overheating without claiming mechanical cooling.",
                    "Reduce further heat input or propose guest assistance; no simulated cooling is reported as measured.",
                    priority="high" if max(values) > high+2 else "medium", context=context, variant="overheating")))
                continue
            if not all(v < low for v in values):
                continue
            states = [room_state(repo, room, r["timestamp_utc"]) for r in rows]
            if any(s.get("occupancy") != "occupied" for s in states):
                continue
            state = states[-1]
            if cold_forecasts is None:
                forecasts = [r for r in repo.get_weather_forecast(now, until)
                             if r.get("issued_utc") and instant(r["issued_utc"]) <= instant(now)
                             and r.get("target_utc") and instant(now) <= instant(r["target_utc"]) <= instant(until)]
                # Keep the latest known issue per target; do not borrow a later forecast.
                latest = {}
                for row in forecasts:
                    target = row["target_utc"]
                    if target not in latest or instant(row["issued_utc"]) > instant(latest[target]["issued_utc"]):
                        latest[target] = row
                tariffs = [r for r in repo.get_tariffs(now, until)
                           if r.get("published_utc") and instant(r["published_utc"]) <= instant(now)
                           and r.get("timestamp_utc") and instant(now) <= instant(r["timestamp_utc"]) <= instant(until)]
                cold_forecasts = [r for r in latest.values()
                                  if number(r.get("outdoor_c_forecast")) is not None and -60 <= number(r["outdoor_c_forecast"]) <= self.config.cold_forecast_c]
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
            windows_known_closed = all(number(r.get("window_open")) == 0 for r in rows)
            if windows_known_closed:
                cold_rooms.append(room)
            setpoint = number(rows[-1].get("setpoint_c"))
            if window:
                action = "notify_guest"
                reason = " Window was observed open; propose checking it before diagnosing plant failure."
                context = {"room": state}
            elif not windows_known_closed:
                action = "notify_guest"
                reason = " Window observations are incomplete/unknown; verify the contact and room conditions before changing heat or attributing cold to the plant."
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
            result.candidates.append(self._scope_comfort_notice(Candidate(**common, action=action,
                reason=f"Observed occupied room below {low} °C for {self.config.cold_window_minutes} min ({min(values):.2f}–{max(values):.2f} °C)." + reason + details,
                outcome="Propose comfort recovery; future telemetry remains observed history.",
                human=action == "request_inspection", consent=action == "request_inspection")))
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
        valid_weather = [(r["timestamp_utc"], number(r.get("outdoor_c"))) for r in weather
                         if number(r.get("outdoor_c")) is not None and -30 <= number(r["outdoor_c"]) <= 45]
        temperatures = [v for _, v in valid_weather]
        continuous = (len(cop) >= 4 and instant(cop[-1][0]) == instant(now)
                      and all(instant(b[0]) - instant(a[0]) == timedelta(minutes=15)
                              for a, b in zip(cop, cop[1:])))
        if continuous and all(v < self.config.low_cop for _, v in cop):
            plausible = bool(temperatures and min(temperatures) > -7 and valid_weather and instant(valid_weather[-1][0]) >= instant(ago(now, 30)))
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

    def _scope_comfort_notice(self, candidate: Candidate) -> Candidate:
        """Separate recipient updates without reopening the physical incident."""
        if candidate.action != "notify_guest":
            return candidate
        booking = candidate.context.get("room", {}).get("current_booking_id")
        scope = "booking:" + booking if booking else "asset:" + candidate.asset
        notices = self._notice_priorities[candidate.asset]
        key = (scope, candidate.variant)
        priorities = {"low": 0, "medium": 1, "high": 2, "critical": 3}
        highest = max(notices.get(key, candidate.priority), candidate.priority, key=priorities.__getitem__)
        notices[key] = highest
        # Keep the thermal prefix for the notification ledger. A downgrade, data
        # gap or returning booking cannot rearm an already proposed severity.
        candidate.variant = stable_id(candidate.variant, scope, highest)
        return candidate

    def _continuous(self, rows, now):
        return (len(rows) >= self.config.cold_window_minutes // 15 + 1
                and instant(rows[-1]["timestamp_utc"]) == instant(now)
                and all(instant(b["timestamp_utc"]) - instant(a["timestamp_utc"]) == timedelta(minutes=15)
                        for a, b in zip(rows, rows[1:])))
