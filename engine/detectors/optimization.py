"""Causal bounded battery dispatch and vacant-room preheating proposals."""
from datetime import timedelta
from math import sqrt

from engine.core import Candidate, DetectionResult, ago, instant, number, observations, room_state, utc


def known_prices(repo, now, until):
    latest = {}
    for row in repo.get_tariffs(now, until):
        if (not row.get("published_utc") or instant(row["published_utc"]) > instant(now)
                or not row.get("timestamp_utc")
                or not instant(now).replace(minute=0, second=0, microsecond=0) <= instant(row["timestamp_utc"]) <= instant(until)):
            continue
        t = row["timestamp_utc"]
        if t not in latest or instant(row["published_utc"]) > instant(latest[t]["published_utc"]):
            latest[t] = row
    return sorted((r for r in latest.values() if number(r.get("price_eur_kwh")) is not None),
                  key=lambda r: instant(r["timestamp_utc"]))


class OptimizationDetector:
    def __init__(self, config):
        self.config = config
        self.day = None

    def detect(self, event, repo, building):
        result = DetectionResult()
        if event.event_type != "clock_tick" or instant(event.available_at_utc).minute != 0:
            return result
        now = event.available_at_utc
        until = utc(instant(now) + timedelta(hours=24))
        prices = known_prices(repo, now, until)
        current = next((r for r in prices if instant(r["timestamp_utc"]) <= instant(now) < instant(r["timestamp_utc"]) + timedelta(hours=1)), None)
        future = [r for r in prices if instant(r["timestamp_utc"]) >= instant(now)]
        self._preheat(result, repo, building, now, current, future)
        assets = {a["id"]: a for a in building["building"].get("assets", [])}
        battery = assets.get("BAT1")
        rows = observations(repo, "energy.csv", ago(now, 15), now)
        row = rows[-1] if rows else None
        if not battery or not row or instant(row["timestamp_utc"]) != instant(now):
            return result
        soc = number(row.get("battery_soc_kwh"))
        load, pv = number(row.get("building_load_kw")), number(row.get("pv_kw"))
        charge, discharge = number(battery.get("max_charge_kw")), number(battery.get("max_discharge_kw"))
        efficiency = number(battery.get("round_trip_efficiency"))
        bounds = battery.get("soc_bounds_kwh", [])
        if (soc is None or load is None or pv is None or min(load, pv) < 0 or not charge or not discharge
                or not efficiency or not 0 < efficiency <= 1 or len(bounds) != 2
                or not bounds[0] <= soc <= bounds[1]):
            return result
        eta = sqrt(efficiency)
        interval_h = 0.25
        mode = None
        power = 0.0
        reason = ""
        if pv - load >= 1:
            mode = "pv_charge"
            power = -min(charge, pv - load, (bounds[1] - soc) / (eta * interval_h))
            reason = f"Observed PV surplus {pv-load:.2f} kW; absorb within battery limits."
        elif current and len(future) >= 3:
            values = [float(r["price_eur_kwh"]) for r in future]
            price = float(current["price_eur_kwh"])
            low, high = min(values), max(values)
            if high - low >= self.config.battery_price_spread:
                if price <= low + (high-low) * 0.25 and high * efficiency > price + 0.03:
                    mode = "cheap_charge"
                    power = -min(charge, (bounds[1] - soc) / (eta * interval_h))
                    reason = f"Known current price {price:.3f} EUR/kWh vs published range {low:.3f}..{high:.3f}; round-trip losses included in price-spread screen."
                elif price >= high - (high-low) * 0.25 and price * efficiency > low + 0.03:
                    mode = "peak_discharge"
                    power = min(discharge, max(0, load-pv), (soc-bounds[0]) * eta / interval_h)
                    reason = f"Known current price {price:.3f} EUR/kWh; discharge to cover observed net load without reducing HVAC."
        if not mode or abs(power) < 1:
            return result
        expected = soc - power * interval_h / eta if power >= 0 else soc - power * interval_h * eta
        day = instant(now).date().isoformat()
        if self.day and self.day != day:
            result.resolved.add("battery:" + self.day)
        self.day = day
        evidence = [f"energy.csv:{row['timestamp_utc']}"] + [f"tariff.csv:{r['timestamp_utc']}:published={r['published_utc']}" for r in prices]
        result.candidates.append(Candidate("battery:" + day, "BAT1", "energy_optimization", "optimize_energy",
            reason + f" Propose signed battery power {power:.2f} kW for 15 min; simulated SOC {soc:.2f}→{expected:.2f} kWh. Positive means discharge. This is a one-step heuristic, not a verified saving or globally optimal schedule.",
            evidence, "Verify actual battery power/SOC and occupied comfort on the next tick; discard the proposal if any bound or sensor changes.",
            "Shift battery power for one bounded interval; leave HVAC and observed history unchanged.",
            context={"battery": battery, "observed_energy": row, "battery_power_kw": power,
                     "duration_minutes": 15, "expected_soc_kwh": expected, "strategy": mode},
            variant=now))
        return result

    def _preheat(self, result, repo, building, now, current, future):
        values = [float(r["price_eur_kwh"]) for r in future]
        if current and len(values) >= 3 and max(values)-min(values) >= self.config.battery_price_spread and float(current["price_eur_kwh"]) <= min(values)+(max(values)-min(values))*0.25:
            near = utc(instant(now) + timedelta(hours=2))
            known = [r for r in repo.get_weather_forecast(now, near) if r.get("issued_utc")
                         and instant(r["issued_utc"]) <= instant(now) and r.get("target_utc")
                         and instant(now) <= instant(r["target_utc"]) <= instant(near)]
            latest = {}
            for row in known:
                if row["target_utc"] not in latest or instant(row["issued_utc"]) > instant(latest[row["target_utc"]]["issued_utc"]):
                    latest[row["target_utc"]] = row
            forecasts = [r for r in latest.values() if number(r.get("outdoor_c_forecast")) is not None
                         and -60 <= number(r["outdoor_c_forecast"]) <= self.config.cold_forecast_c]
            if forecasts:
                for room in building["building"]["rooms"]:
                    bookings = [b for b in repo.get_bookings_for_room(room["id"], now, near, now)
                                if b.get("created_utc") and instant(b["created_utc"]) <= instant(now)
                                and b.get("arrival_utc") and instant(now) < instant(b["arrival_utc"]) <= instant(near)
                                and b.get("observed_state") not in {"checked_in", "checked_out", "no_show"}
                                and b.get("observed_room_id", room["id"]) == room["id"]]
                    state = room_state(repo, room["id"], now)
                    setpoint = number(state.get("setpoint_c"))
                    temperature = number(state.get("temperature_c"))
                    if (bookings and state.get("room_id") == room["id"] and state.get("occupancy") == "vacant"
                            and state.get("last_observed_utc")
                            and instant(now)-timedelta(minutes=self.config.telemetry_max_age_minutes) <= instant(state["last_observed_utc"]) <= instant(now)
                            and temperature is not None and -10 <= temperature < 21
                            and number(state.get("window_open")) == 0
                            and setpoint is not None and 5 <= setpoint < 21):
                        result.candidates.append(Candidate("preheat:" + bookings[0]["booking_id"], room["id"], "energy_optimization", "adjust_room_setpoint",
                            "Known near-term booked arrival and issued cold forecast during a cheap published hour; propose vacant-room preheating to 21 °C, conditional on continued vacancy.",
                            [f"bookings.csv:{bookings[0]['booking_id']}", *state.get("evidence", []),
                             f"weather_forecast.csv:issued={forecasts[0]['issued_utc']}:target={forecasts[0]['target_utc']}",
                             f"tariff.csv:{current['timestamp_utc']}:published={current['published_utc']}"],
                            "Observe temperature before arrival and stop/reassess if occupancy or booking facts change.",
                            "Propose heating within safe bounds; no physical readiness or savings are claimed.",
                            context={"room": state, "setpoint_c": 21}, variant="room:" + room["id"]))
