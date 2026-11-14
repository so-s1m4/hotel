"""Retrospective measured analysis. Never used as information for replay decisions."""
from __future__ import annotations

import argparse
from bisect import bisect_right
from collections import Counter, defaultdict
import csv
import hashlib
import json
from pathlib import Path
from statistics import median

from engine.core import instant, number, VIENNA


def rows(root, name):
    with (root/name).open(encoding="utf-8-sig", newline="") as stream:
        yield from csv.DictReader(stream)


def analyze_dataset(data_dir: str | Path) -> dict:
    """Use strict UTC instants for ordering and joins, preserving source bytes.

    The existing instant helper accepts equivalent zero-offset spellings and
    rejects invalid, naive or non-UTC timestamps when used in these calculations.
    """
    root = Path(data_dir)
    manifest = json.loads((root/"manifest.json").read_text(encoding="utf-8-sig"))
    building = json.loads((root/"building.json").read_text(encoding="utf-8-sig"))
    rooms = {r["id"]: r for r in building["building"]["rooms"]}
    low, high = building["constraints"]["occupied_comfort_c"]
    timelines = defaultdict(list)
    event_counts = Counter()
    checkout = {}
    active_bookings = {}
    turnaround = []
    with (root/"events.jsonl").open(encoding="utf-8-sig") as stream:
        events = sorted((json.loads(line) for line in stream if line.strip()), key=lambda e: (instant(e["timestamp_utc"]), e["event_id"]))
    for e in events:
        event_counts[e["type"]] += 1
        t, room = instant(e["timestamp_utc"]), e.get("room_id")
        if e["type"] in {"checkin", "checkin_early", "checkin_after_wait", "stay_in_progress"} and room in rooms:
            timelines[room].append((t, "occupied"))
            active_bookings[room] = e.get("booking_id")
        elif e["type"] in {"checkout", "checkout_early"} and room in rooms:
            if room not in active_bookings or active_bookings[room] == e.get("booking_id"):
                timelines[room].append((t, "vacant"))
                active_bookings.pop(room, None)
                checkout[room] = t
        elif e["type"] == "room_reassigned":
            origin, destination = e.get("from_room"), e.get("to_room")
            if (origin in rooms and destination in rooms and origin in active_bookings
                    and active_bookings[origin] == e.get("booking_id") and destination not in active_bookings):
                timelines[origin].append((t, "vacant"))
                timelines[destination].append((t, "occupied"))
                active_bookings.pop(origin)
                active_bookings[destination] = e.get("booking_id")
        elif e["type"] == "cleaning_completed" and room in checkout:
            minutes = (t-checkout.pop(room)).total_seconds()/60
            if 0 <= minutes <= 1440:
                turnaround.append(minutes)
    times = {room: [t for t, _ in entries] for room, entries in timelines.items()}

    def occupancy(room, t):
        idx = bisect_right(times.get(room, []), t)-1
        return timelines[room][idx][1] if idx >= 0 else "unknown"

    temperature = {room: Counter() for room in rooms}
    duplicate_rows = 0
    out_of_order = 0
    last = {}
    for row in rows(root, "room_telemetry.csv"):
        room, t = row["room_id"], row["timestamp_utc"]
        if room not in temperature:
            continue
        t = instant(t)
        if room in last and t <= last[room]:
            duplicate_rows += t == last[room]
            out_of_order += t < last[room]
            continue
        last[room] = t
        count = temperature[room]
        count["samples"] += 1
        value = number(row.get("temperature_c"))
        if value is None:
            count["missing_temperature"] += 1
            continue
        if not -10 <= value <= 50:
            count["implausible_temperature"] += 1
            continue
        occ = occupancy(room, t)
        count[occ+"_samples"] += 1
        if occ == "occupied":
            count["cold_samples"] += value < low
            count["hot_samples"] += value > high
            count["cold_closed_window_samples"] += value < low and row.get("window_open") == "0"
    total_temperature = Counter()
    for c in temperature.values():
        total_temperature.update(c)
    orientations = defaultdict(Counter)
    for room, c in temperature.items():
        orientations[str(rooms[room].get("orientation", "unknown"))].update(c)
    water = Counter()
    previous = {}
    per_interval = defaultdict(lambda: [0, 0.0])
    local_hour = {}
    for row in rows(root, "room_water.csv"):
        room, t = row["room_id"], row["timestamp_utc"]
        if room not in rooms:
            continue
        value = number(row.get("meter_total_l"))
        water["room_samples"] += 1
        if value is None or value < 0:
            water["missing_or_invalid_room_samples"] += 1
            previous.pop(room, None)
            continue
        t = instant(t)
        if room in previous:
            old_t, old = previous[room]
            if t <= old_t:
                water["duplicate_or_out_of_order_room_samples"] += 1
                continue
            delta = value-old
            if delta < 0:
                water["room_negative_deltas"] += 1
                water["room_counter_drops_ge100l"] += abs(delta) >= 100
            elif (t-old_t).total_seconds() != 900:
                water["noncontiguous_room_intervals"] += 1
            elif delta <= 2000:
                water["valid_room_intervals"] += 1
                water["valid_room_interval_liters"] += delta
                per_interval[t][0] += 1
                per_interval[t][1] += delta
                if t not in local_hour:
                    local_hour[t] = t.astimezone(VIENNA).hour
                if 0 <= local_hour[t] < 6 and occupancy(room, t) == "vacant" and occupancy(room, old_t) == "vacant" and delta >= 3:
                    water["vacant_night_flow_intervals_ge_3l"] += 1
                    water["vacant_night_flow_liters_ge_3l"] += delta
            else:
                water["implausible_room_deltas"] += 1
        previous[room] = (t, value)
    residuals = []
    prev = None
    for row in rows(root, "water_main.csv"):
        t, value = row["timestamp_utc"], number(row.get("meter_total_l"))
        if value is None or value < 0:
            water["missing_or_invalid_main_samples"] += 1
            prev = None
            continue
        t = instant(t)
        if prev:
            old_t, old = prev
            delta = value-old
            if delta < 0:
                water["main_negative_deltas"] += 1
            elif 0 <= delta <= 50000 and (t-old_t).total_seconds() == 900:
                water["valid_main_intervals"] += 1
                water["valid_main_interval_liters"] += delta
                if per_interval[t][0] == len(rooms):
                    residuals.append(delta-per_interval[t][1])
        prev = (t, value)
    water["complete_main_room_intervals"] = len(residuals)
    water["complete_interval_residual_liters"] = sum(residuals)
    water["median_complete_interval_residual_liters"] = median(residuals) if residuals else None
    tariffs = {instant(r["timestamp_utc"]): number(r.get("price_eur_kwh")) for r in rows(root, "tariff.csv")}
    energy = Counter()
    for row in rows(root, "energy.csv"):
        energy["samples"] += 1
        values = {k: number(row.get(k)) for k in ("building_load_kw", "hvac_electric_kw", "pv_kw", "battery_discharge_kw_signed", "battery_soc_kwh", "grid_import_kw", "grid_export_kw")}
        if any(v is None for v in values.values()):
            energy["missing_samples"] += 1
            continue
        if any(values[k] < 0 for k in values if k != "battery_discharge_kw_signed"):
            energy["invalid_samples"] += 1
            continue
        energy["valid_samples"] += 1
        for key in ("building_load_kw", "hvac_electric_kw", "pv_kw", "grid_import_kw", "grid_export_kw"):
            energy[key.replace("_kw", "_kwh")] += values[key]*0.25
        energy["battery_charge_kwh"] += max(0, -values["battery_discharge_kw_signed"])*0.25
        energy["battery_discharge_kwh"] += max(0, values["battery_discharge_kw_signed"])*0.25
        balance = values["grid_import_kw"]+values["pv_kw"]+values["battery_discharge_kw_signed"]-values["building_load_kw"]-values["grid_export_kw"]
        energy["balance_deviation_samples"] += abs(balance) > max(1, values["building_load_kw"]*0.05)
        hour = instant(row["timestamp_utc"]).replace(minute=0, second=0, microsecond=0)
        price = tariffs.get(hour)
        if price is not None:
            energy["grid_import_cost_eur_observed"] += values["grid_import_kw"]*0.25*price
            energy["cost_covered_samples"] += 1
    equipment = Counter()
    for row in rows(root, "equipment.csv"):
        key = row["asset_id"]+":"+row["metric"]
        equipment[key+":samples"] += 1
        value = number(row.get("value"))
        if row.get("value", "") == "":
            equipment[key+":missing"] += 1
        if row["asset_id"] == "AHU1" and row["metric"] == "filter_dp_pa" and value is not None:
            equipment["AHU1:filter_dp_pa:ge250"] += value >= 250
    logistics = Counter()
    for row in rows(root, "logistics.csv"):
        value = number(row.get("value"))
        if value is None:
            logistics["missing_samples"] += 1
        elif row["metric"] == "fill_pct":
            logistics[row["asset_id"]+":recorded_full_samples"] += value >= 100
            logistics[row["asset_id"]+":recorded_ge80_samples"] += value >= 80
        elif row["metric"].startswith("stock_"):
            logistics[row["asset_id"]+":"+row["metric"]+":recorded_empty_samples"] += value == 0
    staffing = Counter()
    for row in rows(root, "staff_roster.csv"):
        staffing[row["role"]+":"+row["status"]+":shifts"] += 1
    sources = {}
    for name in ["manifest.json", "building.json", "events.jsonl", "room_telemetry.csv", "room_water.csv", "water_main.csv", "energy.csv", "tariff.csv", "equipment.csv", "logistics.csv", "staff_roster.csv"]:
        with (root/name).open("rb") as stream:
            sources[name] = hashlib.file_digest(stream, "sha256").hexdigest()
    return {"analysis_version": "1.0", "retrospective_only": True,
            "metadata": {k: manifest[k] for k in ("profile", "model_id", "start_utc", "end_utc", "days")},
            "comfort_band_c": [low, high], "temperature": dict(total_temperature),
            "temperature_by_room": {r: dict(c) for r, c in temperature.items()},
            "temperature_by_orientation": {r: dict(c) for r, c in orientations.items()},
            "telemetry_duplicates_skipped": duplicate_rows, "telemetry_out_of_order_skipped": out_of_order,
            "water": dict(water), "energy": dict(energy), "equipment": dict(equipment),
            "logistics": dict(logistics), "staffing": dict(staffing), "event_counts": dict(event_counts),
            "observed_checkout_to_cleaning_minutes_median": median(turnaround) if turnaround else None,
            "checkout_cleaning_pairs": len(turnaround), "source_sha256": sources}


def render_report(a: dict) -> str:
    m, t, w, e = (a[k] for k in ("metadata", "temperature", "water", "energy"))
    occupied = t.get("occupied_samples", 0)
    cold, hot = t.get("cold_samples", 0), t.get("hot_samples", 0)
    cold_rate = f"{100*cold/occupied:.2f}%" if occupied > 0 else "rate unavailable"
    temperature_samples, missing_temperature = t.get("samples", 0), t.get("missing_temperature", 0)
    matched_water = w.get("complete_main_room_intervals", 0)
    residual = w.get("median_complete_interval_residual_liters")
    water_supported = matched_water > 0 and residual is not None
    water_value = f"{residual:.2f} L/15 min" if water_supported else "unavailable"
    energy_samples, valid_energy = e.get("samples", 0), e.get("valid_samples", 0)
    priced_energy = e.get("cost_covered_samples", 0)
    energy_values = (f"observed building load {e['building_load_kwh']:,.1f} kWh, HVAC {e['hvac_electric_kwh']:,.1f} kWh, PV {e['pv_kwh']:,.1f} kWh"
                     if valid_energy > 0 else "observed energy totals unavailable")
    cost = e.get("grid_import_cost_eur_observed")
    cost_value = f"{cost:,.2f} EUR" if priced_energy > 0 and cost is not None else "unavailable"
    if priced_energy > 0 and priced_energy < valid_energy and cost is not None:
        cost_value += " (covered subtotal)"
    pairs, turnaround = a["checkout_cleaning_pairs"], a["observed_checkout_to_cleaning_minutes_median"]
    turnaround_supported = pairs > 0 and turnaround is not None
    turnaround_value = f"{turnaround} minutes" if turnaround_supported else "unavailable"
    comfort_review = (f"Use the {cold:,} cold samples as a review pool, not ground truth."
                      if occupied > 0 else "No valid occupied-temperature samples support a discomfort review pool in this dataset.")
    latency_review = (f"measure checkout→human-cleaning latency against the observed {turnaround} minute median"
                      if turnaround_supported else "collect paired checkout/cleaning observations before defining a latency baseline")
    water_evidence = (f"The {matched_water:,} matched main/room intervals have a signed median residual of {residual:.2f} L/15 min; this aggregate does not identify usage or loss causes."
                      if water_supported else "No matched main/room residual observations are available, so these data establish no residual or attribution finding.")
    if missing_temperature > 0:
        temperature_evidence = f"The {missing_temperature:,} missing temperature records among {temperature_samples:,} included samples provide an observed telemetry-quality review pool."
    elif temperature_samples > 0:
        temperature_evidence = f"No missing temperature values were recorded among {temperature_samples:,} included samples; this does not establish complete telemetry coverage."
    else:
        temperature_evidence = "No temperature samples are available to assess missing-record coverage."
    lines = ["# PLATO-40: evidence-based operating and design review", "",
        f"Dataset: **{m['profile']}**, {m['days']} days, `{m['start_utc']}..{m['end_utc']}`.",
        "Retrospective analysis of the fictional synthetic property. This is not a claim about real PLATO buildings or current CLARA capability.",
        "Data credit: PLATO-40 synthetic dataset, Proximata Hackathon '26 – Rhomberg track (CC BY 4.0).", "",
        "## Measured findings", "",
        f"- Comfort: {occupied:,} valid temperature samples with event-derived occupied state; {cold:,} below the comfort band ({cold_rate}), {hot:,} above. Each sample represents a 15-minute observation; these counts are not independent guests or labelled incidents.",
        f"- Data quality: {missing_temperature:,} missing temperature samples among {temperature_samples:,} included rows; {a['telemetry_duplicates_skipped']} duplicate timestamp/room readings skipped; {a['telemetry_out_of_order_skipped']} out-of-order readings excluded. Unknown occupancy is retained, never inferred from motion.",
        f"- Water: {matched_water:,} intervals have valid matched readings from the main meter and every room. Median main-minus-room residual is {water_value}. Common areas, irrigation and losses are included; this is **not** measured leakage.",
        f"- Water quality: {w.get('room_negative_deltas', 0)} negative room-meter deltas (possible resets or artefacts; not confirmed exchanges) and {w.get('missing_or_invalid_room_samples', 0):,} missing/invalid room-meter samples; negative deltas and gaps are excluded from consumption.",
        f"- Energy: {valid_energy:,} of {energy_samples:,} samples are valid; {energy_values}. Observed import cost {cost_value}; {priced_energy:,} of {valid_energy:,} valid energy samples have price coverage. Missing intervals and prices are not imputed; export revenue is not assumed.",
        f"- Actual checkout→observed cleaning-completed median: {turnaround_value} over {pairs:,} paired events. This is not a room-release or service-level guarantee.", "",
        "### Logistics and staffing observations", ""]
    lines += [f"- `{key}`: {value:,} recorded samples" for key, value in sorted(a["logistics"].items()) if value]
    lines += [f"- `{key}`: {value:,}" for key, value in sorted(a["staffing"].items()) if not key.endswith(":scheduled:shifts")]
    lines += ["", "## CLARA: operating recommendations", "",
        f"1. Prioritise sustained discomfort in observed occupied rooms. {comfort_review} Separate open windows, defrost and sensor gaps before dispatch. Validate thresholds on held-out data and report false positives; never lower occupied comfort to exploit a cheap/expensive tariff.",
        f"2. Retain availability timestamps, verify staff absence before reallocating jobs, and {latency_review}. Add an explicit inspection/release event rather than treating clean telemetry as authorisation.",
        "3. Trigger lead-time-aware waste/restock forecasts, reusing scheduled visits when they precede predicted shortage. Verify actual collection/delivery instead of treating an order or simulated robot task as a physical state change.", "",
        "## PLATO: future-building design recommendations", "",
        f"1. Consider common-area water submeters and meter-replacement records if attribution is needed. {water_evidence} Evaluate instrumentation against matched evidence before committing to a retrofit; no quantified leak saving is claimed.",
        f"2. Provide explicit room inspection/release signals and resilient room/gateway telemetry. {temperature_evidence} Test store-and-forward and outage reporting before deciding on a hardware retrofit.",
        "3. Investigate thermal variation by orientation with matched area, floor, window and occupancy controls before changing envelope/HVAC design. The exposure table below is descriptive and does not establish a design defect.", "",
        "| Orientation | Occupied samples | Below-band samples | Below-band rate |",
        "|---|---:|---:|---:|"]
    for orientation, counts in sorted(a["temperature_by_orientation"].items()):
        n = counts.get("occupied_samples", 0)
        k = counts.get("cold_samples", 0)
        rate = f"{100*k/n:.2f}%" if n > 0 else "unavailable"
        lines.append(f"| {orientation} | {n:,} | {k:,} | {rate} |")
    lines += ["", "## Evidence and reproducibility", "",
        "Source windows are the dataset coverage above. Metrics are in the companion analysis JSON, including per-room exposure and SHA-256 hashes. References:", "",
        "- `room_telemetry.csv` + `events.jsonl`: temperature/occupancy counts, orientation exposure, observed cleaning pairs.",
        "- `room_water.csv` + `water_main.csv`: contiguous positive deltas and strictly matched residual intervals.",
        "- `energy.csv` + `tariff.csv`: observed energy and import-only expense, not counterfactual savings.",
        "- `logistics.csv` + `staff_roster.csv`: recorded capacity/stock states and final retrospective shift counts.", "",
        "Reproduce with `python -m engine.annual --data-dir PATH --out insights.md --json analysis.json`.", "",
        "## Target architecture", "",
        "Time-stamped source adapters and durable event store → causal indexed gateway → guest/housekeeping, water, maintenance, energy and logistics agents → shared resource/capacity planner → central safety gate → authenticated human approval and guest consent → robot/BMS/vendor adapters (mock-only here) → observed verification → annual operating/design feedback. In a real system, commands need acknowledgement, durable idempotency, consent scope/expiry and conflict recovery.", "",
        "## Limits and uncertainty", "",
        "No participant ground truth exists. Findings describe synthetic observations, not real-world savings, true fault counts or causal design effects. Missing intervals are not imputed as zero. Occupancy before the first lifecycle event stays unknown. Room reassignment changes occupancy only for a booking already observed in that source room; a booking reassignment alone does not prove entry. Source ordering and 15-minute semantics are assumptions; inconsistent telemetry is excluded and counted. Water flushing/cleaning may explain vacant-night use. Full-bin readings are capped and do not quantify overflow. Annual analysis may use the whole historical dataset only after replay; it must never feed future knowledge back into decisions.", ""]
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--json", type=Path, required=True)
    args = parser.parse_args()
    if args.out.resolve() == args.json.resolve() or any(p.resolve().is_relative_to(args.data_dir.resolve()) for p in [args.out, args.json]):
        parser.error("Use distinct output paths outside source data")
    analysis = analyze_dataset(args.data_dir)
    for target in [args.out, args.json]:
        target.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(render_report(analysis), encoding="utf-8")
    args.json.write_text(json.dumps(analysis, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    print(json.dumps({"status": "retrospective_analysis_complete", "coverage": analysis["metadata"], "out": str(args.out)}))


if __name__ == "__main__":
    main()
