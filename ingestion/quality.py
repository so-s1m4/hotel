"""Deterministic data-quality report for one dataset directory.

    python -m ingestion.quality --data-dir data/PLATO40_v2_dev --out outputs/quality_dev.json

This is a RETROSPECTIVE whole-dataset analysis for insights and robustness
evidence (sensor gaps, frozen or implausible values, meter resets, duplicates).
It is not time-causal and must never feed a decision at replay time.
Every finding carries source references so it can be cited as evidence.
"""
from __future__ import annotations

import argparse
import collections
import json
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any

from .loader import SERIES_FILES, Dataset, fmt_ts, parse_ts

FROZEN_MIN_INTERVALS = 12          # identical analogue reading for >= 3 h
GAP_MIN_INTERVALS = 2              # >= 2 consecutive missing samples
PLAUSIBLE = {                      # generous physical bounds, not comfort limits
    "temperature_c": (5.0, 40.0), "setpoint_c": (10.0, 30.0),
    "humidity_pct": (0.0, 100.0), "co2_ppm": (300.0, 5000.0),
}
TOP = 15


def _expected_grid(ds: Dataset) -> tuple[list[str], int]:
    manifest = ds.manifest
    step = ds.interval_seconds
    if not manifest.get("start_utc") or not manifest.get("end_utc"):
        return [], step
    cur, stop = parse_ts(manifest["start_utc"]), parse_ts(manifest["end_utc"])
    grid = []
    while cur < stop:
        grid.append(fmt_ts(cur))
        cur += timedelta(seconds=step)
    return grid, step


def _runs(flags: list[bool]) -> list[tuple[int, int]]:
    """(start_index, length) of consecutive True runs."""
    out, start = [], None
    for i, flag in enumerate(flags):
        if flag and start is None:
            start = i
        elif not flag and start is not None:
            out.append((start, i - start))
            start = None
    if start is not None:
        out.append((start, len(flags) - start))
    return out


def _num(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def telemetry_quality(ds: Dataset) -> dict[str, Any]:
    series = ds.series("room_telemetry.csv")
    grid, step = _expected_grid(ds)
    index = {ts: i for i, ts in enumerate(grid)}
    missing_cells: collections.Counter[str] = collections.Counter()
    implausible: list[dict] = []
    gaps: list[dict] = []
    frozen: list[dict] = []
    rows_total = 0
    for (room, _), (ts_list, lines) in sorted(series.groups.items()):
        rows = [series._parse(line) for line in lines]
        rows_total += len(rows)
        present = [False] * len(grid)
        for row in rows:
            pos = index.get(row["timestamp_utc"])
            if pos is not None:
                present[pos] = True
            for col in series.columns:
                if col not in ("timestamp_utc", "room_id") and row.get(col) is None:
                    missing_cells[col] += 1
            for col, (low, high) in PLAUSIBLE.items():
                value = _num(row.get(col))
                if value is not None and not low <= value <= high:
                    implausible.append({"room_id": room, "timestamp_utc": row["timestamp_utc"],
                                        "column": col, "value": value})
        for start, length in _runs([not p for p in present]):
            if length >= GAP_MIN_INTERVALS:
                gaps.append({"room_id": room, "kind": "row_missing", "from_utc": grid[start],
                             "to_utc": grid[start + length - 1], "missing_samples": length,
                             "minutes": length * step // 60})
        # communication loss usually shows as rows whose sensor cells are all blank
        blank = [all(r.get(c) is None for c in ("temperature_c", "humidity_pct", "co2_ppm"))
                 for r in rows]
        for start, length in _runs(blank):
            if length >= GAP_MIN_INTERVALS:
                gaps.append({"room_id": room, "kind": "sensors_blank",
                             "from_utc": rows[start]["timestamp_utc"],
                             "to_utc": rows[start + length - 1]["timestamp_utc"],
                             "missing_samples": length, "minutes": length * step // 60})
        # frozen analogue sensor: the same temperature for many consecutive samples
        temps = [_num(r.get("temperature_c")) for r in rows]
        run_start = 0
        for i in range(1, len(temps) + 1):
            if i == len(temps) or temps[i] is None or temps[i] != temps[run_start]:
                length = i - run_start
                if temps[run_start] is not None and length >= FROZEN_MIN_INTERVALS:
                    frozen.append({"room_id": room, "column": "temperature_c",
                                   "value": temps[run_start],
                                   "from_utc": rows[run_start]["timestamp_utc"],
                                   "to_utc": rows[i - 1]["timestamp_utc"], "samples": length})
                run_start = i
    gaps.sort(key=lambda g: -g["missing_samples"])
    frozen.sort(key=lambda f: -f["samples"])
    by_floor: collections.Counter[str] = collections.Counter()
    for gap in gaps:
        by_floor["floor_" + gap["room_id"][1:2]] += gap["missing_samples"]
    # simultaneous outages across a floor point at the gateway, not a single sensor
    starts: dict[tuple[str, str], set[str]] = collections.defaultdict(set)
    for gap in gaps:
        starts[(gap["from_utc"], gap["room_id"][1:2])].add(gap["room_id"])
    floor_outages = sorted(({"from_utc": k[0], "floor": int(k[1]) if k[1].isdigit() else k[1],
                             "rooms_affected": len(v)} for k, v in starts.items() if len(v) >= 5),
                           key=lambda o: o["from_utc"])
    declared = ds.room_ids or sorted(k[0] for k in series.groups)
    silent_rooms = sorted(set(declared) - {k[0] for k in series.groups})
    undeclared_rooms = sorted({k[0] for k in series.groups} - set(declared)) if ds.room_ids else []
    expected = len(grid) * len(declared)
    return {
        "rows_after_dedup": rows_total, "expected_rows": expected,
        "missing_rows": max(expected - rows_total, 0),
        "declared_rooms": len(declared), "rooms_without_any_rows": silent_rooms,
        "rooms_not_in_building_json": undeclared_rooms,
        "duplicates_dropped": series.duplicates_dropped,
        "missing_cells_by_column": dict(missing_cells.most_common()),
        "gap_count": len(gaps), "missing_samples_by_floor": dict(sorted(by_floor.items())),
        "floor_wide_outages": {"count": len(floor_outages), "examples": floor_outages[:TOP]},
        "longest_gaps": gaps[:TOP],
        "frozen_temperature_runs": {"count": len(frozen), "longest": frozen[:TOP]},
        "implausible_values": {"count": len(implausible), "examples": implausible[:TOP]},
    }


def water_quality(ds: Dataset) -> dict[str, Any]:
    metering = [w for w in ds.work_orders if w.get("type") == "metering"]
    out: dict[str, Any] = {}
    for filename, entity_label in (("room_water.csv", "room_id"), ("water_main.csv", None)):
        series = ds.series(filename)
        resets, missing, negative_small = [], 0, 0
        small_sizes: collections.Counter[int] = collections.Counter()
        for (entity, _), (ts_list, lines) in sorted(series.groups.items()):
            previous: tuple[str, float] | None = None
            for line in lines:
                row = series._parse(line)
                value = _num(row.get("meter_total_l"))
                if value is None:
                    missing += 1
                    continue
                if previous is not None and value < previous[1]:
                    drop = round(previous[1] - value, 2)
                    asset = f"WM_{entity}" if entity_label else "WM_MAIN"
                    explained = [w["work_order_id"] for w in metering if w.get("asset_id") == asset
                                 and w.get("completed_utc")
                                 and abs((parse_ts(w["completed_utc"]) - parse_ts(row["timestamp_utc"]))
                                         .total_seconds()) <= 6 * 3600]
                    resets.append({"asset_id": asset, "timestamp_utc": row["timestamp_utc"],
                                   "before_l": previous[1], "after_l": value, "drop_l": drop,
                                   "metering_work_orders": explained})
                previous = (row["timestamp_utc"], value)
        resets.sort(key=lambda r: -r["drop_l"])
        claimed: set[str] = set()
        for reset in resets:  # largest first: each work order explains ONE decrease
            keep = [w for w in reset["metering_work_orders"] if w not in claimed]
            claimed.update(keep[:1])
            reset["metering_work_orders"] = keep[:1]
        negative_small = sum(1 for r in resets if r["drop_l"] < 5 and not r["metering_work_orders"])
        small_sizes = collections.Counter(min(int(r["drop_l"]), 4) for r in resets
                                          if r["drop_l"] < 5 and not r["metering_work_orders"])
        out[filename] = {
            "missing_readings": missing, "duplicates_dropped": series.duplicates_dropped,
            "counter_decreases": len(resets), "small_unexplained_decreases": negative_small,
            "small_decrease_size_histogram_l": {f"{k}-{k + 1}": v
                                                for k, v in sorted(small_sizes.items())},
            "large_unexplained_decreases": [r for r in resets if r["drop_l"] >= 5
                                            and not r["metering_work_orders"]][:TOP],
            "decreases_matched_to_metering_work_order":
                sum(1 for r in resets if r["metering_work_orders"]),
            "largest_decreases": resets[:TOP],
        }
    return out


def long_format_quality(ds: Dataset, filename: str) -> dict[str, Any]:
    series = ds.series(filename)
    missing: collections.Counter[str] = collections.Counter()
    text_values: dict[str, collections.Counter[str]] = collections.defaultdict(collections.Counter)
    for (asset, metric), (ts_list, lines) in sorted(series.groups.items()):
        for line in lines:
            value = series._parse(line).get("value")
            if value is None:
                missing[f"{asset}.{metric}"] += 1
            elif isinstance(value, str):
                text_values[f"{asset}.{metric}"][value] += 1
    return {"rows_read": series.rows_read, "duplicates_dropped": series.duplicates_dropped,
            "missing_values_by_metric": dict(missing.most_common()),
            "text_value_counts": {k: dict(v.most_common()) for k, v in text_values.items()}}


def build_report(data_dir: str | Path) -> dict[str, Any]:
    ds = Dataset(data_dir)
    report: dict[str, Any] = {
        "data_dir": str(data_dir), "profile": ds.manifest.get("profile"),
        "range_utc": [ds.manifest.get("start_utc"), ds.manifest.get("end_utc")],
        "note": "Retrospective whole-dataset quality analysis. Not time-causal; "
                "never use at decision time. Synthetic data: no real-world claims.",
        "thresholds": {"frozen_min_intervals": FROZEN_MIN_INTERVALS,
                       "gap_min_intervals": GAP_MIN_INTERVALS, "plausible_bounds": PLAUSIBLE},
        "room_telemetry": telemetry_quality(ds), "water": water_quality(ds),
    }
    for filename in ("equipment.csv", "logistics.csv"):
        if (Path(data_dir) / filename).exists():
            report[filename] = long_format_quality(ds, filename)
    roster = collections.Counter((r.get("role"), r.get("status")) for r in ds.roster)
    report["staff_roster"] = {f"{role}:{status}": n for (role, status), n in sorted(
        roster.items(), key=lambda kv: (str(kv[0][0]), str(kv[0][1])))}
    orders = collections.Counter((w.get("type"), w.get("status")) for w in ds.work_orders)
    report["work_orders"] = {f"{kind}:{status}": n for (kind, status), n in sorted(
        orders.items(), key=lambda kv: (str(kv[0][0]), str(kv[0][1])))}
    alarms = collections.Counter(e.get("alarm_code") for e in ds.raw_events
                                 if e.get("type") == "bms_alarm" and e.get("state") == "raised")
    report["bms_alarms_raised"] = dict(alarms.most_common())
    for name in SERIES_FILES:
        if name in ds._series and ds._series[name].bad_rows:
            report.setdefault("unparseable_rows", {})[name] = ds._series[name].bad_rows
    return report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--data-dir", required=True)
    ap.add_argument("--out", default="-", help="output JSON ('-' = stdout)")
    args = ap.parse_args(argv)
    text = json.dumps(build_report(args.data_dir), indent=2, ensure_ascii=False)
    if args.out == "-":
        sys.stdout.write(text + "\n")
    else:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
