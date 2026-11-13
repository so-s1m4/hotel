"""Physical readers and in-memory indexes for one PLATO-40 dataset directory.

Design notes
- One ``Dataset`` object per data directory; Quickstart and Dev are never merged.
- Stdlib only. Every file is read at most once per process (lazy, cached).
- All timestamps are fixed-width ``YYYY-MM-DDTHH:MM:SSZ`` strings, so string
  comparison equals chronological comparison and ``bisect`` works on them.
- Time-series CSVs are kept as raw lines grouped per entity and parsed only for
  the rows a query returns (Dev: 1.4M telemetry rows, ~1 s to index).
- Empty cell -> ``None``. Never zero.
- Exact/keyed duplicates are dropped (first row wins) and counted in
  ``Dataset.audit``.
"""
from __future__ import annotations

import csv
import json
import logging
from bisect import bisect_left, bisect_right
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

log = logging.getLogger("ingestion")

UTC = timezone.utc
TS_FMT = "%Y-%m-%dT%H:%M:%SZ"

# filename -> (entity column or None, metric column or None)
SERIES_FILES: dict[str, tuple[str | None, str | None]] = {
    "room_telemetry.csv": ("room_id", None),
    "room_water.csv": ("room_id", None),
    "water_main.csv": (None, None),
    "energy.csv": (None, None),
    "weather.csv": (None, None),
    "equipment.csv": ("asset_id", "metric"),
    "logistics.csv": ("asset_id", "metric"),
    "access_log.csv": ("door_id", None),
}
STRING_COLUMNS = {
    "timestamp_utc", "room_id", "asset_id", "metric", "housekeeping_state",
    "door_id", "credential", "result",
}


REQUIRED_FILES = (
    "building.json", "manifest.json", "bookings.csv", "access_log.csv",
    "room_telemetry.csv", "room_water.csv", "water_main.csv", "energy.csv",
    "weather.csv", "weather_forecast.csv", "tariff.csv", "equipment.csv",
    "logistics.csv", "staff_roster.csv", "work_orders.csv", "guest_requests.csv",
    "events.jsonl",
)


def inspect_dataset(data_dir: str | Path) -> dict:
    """Preflight: check required inputs and return dataset metadata."""
    root = Path(data_dir)
    missing = [name for name in REQUIRED_FILES if not (root / name).is_file()]
    if missing:
        raise ValueError(f"Missing dataset files in {root}: {', '.join(missing)}")
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    building = json.loads((root / "building.json").read_text(encoding="utf-8"))
    return {
        "model_id": manifest["model_id"], "profile": manifest["profile"],
        "start_utc": manifest["start_utc"], "end_utc": manifest["end_utc"],
        "rooms": len(building["building"]["rooms"]),
        "required_files": len(REQUIRED_FILES),
    }


# ---------------------------------------------------------------- time helpers
def parse_ts(value: str) -> datetime:
    """Parse an ISO 8601 timestamp; naive input is taken as UTC."""
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def fmt_ts(value: datetime) -> str:
    return value.astimezone(UTC).strftime(TS_FMT)


def norm_ts(value: str | datetime) -> str:
    """Canonical ``...Z`` string (seconds resolution) for comparisons."""
    if isinstance(value, datetime):
        return fmt_ts(value if value.tzinfo else value.replace(tzinfo=UTC))
    if len(value) == 20 and value[-1] == "Z" and value[10] == "T":
        return value
    return fmt_ts(parse_ts(value))


def shift_ts(value: str, **delta: float) -> str:
    return fmt_ts(parse_ts(value) + timedelta(**delta))


def none_if_blank(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value if value != "" else None


def parse_value(column: str, raw: str) -> Any:
    """CSV cell -> python value. Blank -> None; numbers -> int/float."""
    if raw == "":
        return None
    if column in STRING_COLUMNS or column.endswith("_utc"):
        return raw
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        return raw


# ------------------------------------------------------------------- series
class Series:
    """One time-series CSV, indexed by entity (and metric) with sorted timestamps."""

    def __init__(self, path: Path, filename: str):
        self.filename = filename
        self.entity_col, self.metric_col = SERIES_FILES[filename]
        self.duplicates_dropped = 0
        self.rows_read = 0
        with path.open("r", encoding="utf-8", newline="") as handle:
            lines = handle.read().splitlines()
        self.columns = lines[0].split(",") if lines else []
        ent_i = (self.columns.index(self.entity_col)
                 if self.entity_col in self.columns else None)
        met_i = (self.columns.index(self.metric_col)
                 if self.metric_col in self.columns else None)
        nsplit = max(i for i in (ent_i, met_i, 0) if i is not None) + 1
        groups: dict[tuple[str, str], tuple[list[str], list[str]]] = {}
        unsorted_groups: set[tuple[str, str]] = set()
        for line in lines[1:]:
            if not line:
                continue
            self.rows_read += 1
            head = line.split(",", nsplit)
            key = (head[ent_i] if ent_i is not None else "",
                   head[met_i] if met_i is not None else "")
            group = groups.get(key)
            if group is None:
                group = groups[key] = ([], [])
            ts_list, line_list = group
            ts = head[0]
            if ts_list:
                if ts == ts_list[-1]:
                    if filename != "access_log.csv" or line == line_list[-1]:
                        self.duplicates_dropped += 1
                        continue
                elif ts < ts_list[-1]:
                    unsorted_groups.add(key)
            ts_list.append(ts)
            line_list.append(line)
        for key in unsorted_groups:  # not seen in supplied data, but be safe
            ts_list, line_list = groups[key]
            order = sorted(range(len(ts_list)), key=lambda i: ts_list[i])
            seen: set[str] = set()
            new_ts, new_lines = [], []
            for i in order:
                if ts_list[i] in seen and filename != "access_log.csv":
                    self.duplicates_dropped += 1
                    continue
                seen.add(ts_list[i])
                new_ts.append(ts_list[i])
                new_lines.append(line_list[i])
            groups[key] = (new_ts, new_lines)
        self.groups = groups
        self.entities = sorted({k[0] for k in groups if k[0]})
        if self.duplicates_dropped:
            log.info("%s: dropped %d duplicate rows (same timestamp+key)",
                     filename, self.duplicates_dropped)

    def _parse(self, line: str) -> dict[str, Any]:
        if '"' in line:
            cells = next(csv.reader([line]))
        else:
            cells = line.split(",")
        cols = self.columns
        if len(cells) < len(cols):
            cells = cells + [""] * (len(cols) - len(cells))
        return {c: parse_value(c, cells[i]) for i, c in enumerate(cols)}

    def _keys(self, entity_id: str | None, metric: str | None) -> list[tuple[str, str]]:
        return sorted(k for k in self.groups
                      if (entity_id is None or not self.entity_col or k[0] == entity_id)
                      and (metric is None or not self.metric_col or k[1] == metric))

    def query(self, start: str, end: str, entity_id: str | None = None,
              metric: str | None = None) -> list[dict[str, Any]]:
        """Rows with start <= timestamp_utc <= end, sorted by time."""
        if end < start:
            return []
        picked: list[tuple[str, int, str]] = []
        for order, key in enumerate(self._keys(entity_id, metric)):
            ts_list, line_list = self.groups[key]
            lo, hi = bisect_left(ts_list, start), bisect_right(ts_list, end)
            picked.extend((ts_list[i], order, line_list[i]) for i in range(lo, hi))
        picked.sort(key=lambda item: (item[0], item[1]))
        return [self._parse(item[2]) for item in picked]

    def latest(self, as_of: str, entity_id: str | None = None,
               metric: str | None = None) -> dict[str, Any] | None:
        """Most recent row with timestamp_utc <= as_of."""
        best: tuple[str, str] | None = None
        for key in self._keys(entity_id, metric):
            ts_list, line_list = self.groups[key]
            i = bisect_right(ts_list, as_of)
            if i and (best is None or ts_list[i - 1] > best[0]):
                best = (ts_list[i - 1], line_list[i - 1])
        return self._parse(best[1]) if best else None


# ------------------------------------------------------------------ dataset
def read_csv_dicts(path: Path) -> list[dict[str, str | None]]:
    """Small relational CSVs: all cells stripped, blank -> None."""
    if not path.exists():
        log.warning("missing file %s", path)
        return []
    with path.open("r", encoding="utf-8", newline="") as handle:
        return [{k: none_if_blank(v) for k, v in row.items() if k is not None}
                for row in csv.DictReader(handle)]


def _dedup(rows: Iterable[dict], key: str, name: str, audit: dict) -> list[dict]:
    seen: set[str] = set()
    out = []
    dropped = 0
    for row in rows:
        ident = row.get(key)
        if ident is None or ident in seen:
            dropped += 1
            continue
        seen.add(ident)
        out.append(row)
    if dropped:
        audit[name + ":duplicates_dropped"] = dropped
        log.info("%s: dropped %d rows with duplicate/missing %s", name, dropped, key)
    return out


class Dataset:
    """Lazy, cached access to the raw files of ONE dataset directory."""

    def __init__(self, data_dir: str | Path):
        self.dir = Path(data_dir)
        if not self.dir.is_dir():
            raise FileNotFoundError(f"data directory not found: {self.dir}")
        self.audit: dict[str, Any] = {}
        self._series: dict[str, Series] = {}
        self._cache: dict[str, Any] = {}

    # -- generic helpers
    def _memo(self, name: str, build):
        if name not in self._cache:
            self._cache[name] = build()
        return self._cache[name]

    def _json(self, name: str) -> dict:
        path = self.dir / name
        if not path.exists():
            log.warning("missing file %s", path)
            return {}
        return json.loads(path.read_text(encoding="utf-8"))

    # -- static documents
    @property
    def manifest(self) -> dict:
        return self._memo("manifest", lambda: self._json("manifest.json"))

    @property
    def building(self) -> dict:
        return self._memo("building", lambda: self._json("building.json"))

    @property
    def local_tz_name(self) -> str:
        return (self.building.get("time") or {}).get("local_timezone") or "Europe/Vienna"

    @property
    def room_ids(self) -> list[str]:
        rooms = (self.building.get("building") or {}).get("rooms") or []
        return [r["id"] for r in rooms if "id" in r]

    # -- series
    def series(self, filename: str) -> Series:
        if filename not in SERIES_FILES:
            raise ValueError(
                f"unsupported measurement file {filename!r}; expected one of "
                f"{sorted(SERIES_FILES) + ['tariff.csv']}")
        if filename not in self._series:
            path = self.dir / filename
            if not path.exists():
                raise FileNotFoundError(path)
            ser = Series(path, filename)
            self._series[filename] = ser
            self.audit[filename + ":rows_read"] = ser.rows_read
            self.audit[filename + ":duplicates_dropped"] = ser.duplicates_dropped
        return self._series[filename]

    # -- relational tables
    @property
    def bookings(self) -> dict[str, dict]:
        def build():
            rows = _dedup(read_csv_dicts(self.dir / "bookings.csv"), "booking_id",
                          "bookings.csv", self.audit)
            for row in rows:
                if row.get("guest_count") is not None:
                    try:
                        row["guest_count"] = int(row["guest_count"])
                    except ValueError:
                        pass
            return {r["booking_id"]: r for r in rows}
        return self._memo("bookings", build)

    @property
    def roster(self) -> list[dict]:
        return self._memo("roster", lambda: _dedup(
            read_csv_dicts(self.dir / "staff_roster.csv"), "shift_id",
            "staff_roster.csv", self.audit))

    @property
    def work_orders(self) -> list[dict]:
        return self._memo("work_orders", lambda: _dedup(
            read_csv_dicts(self.dir / "work_orders.csv"), "work_order_id",
            "work_orders.csv", self.audit))

    @property
    def guest_requests(self) -> list[dict]:
        return self._memo("guest_requests", lambda: _dedup(
            read_csv_dicts(self.dir / "guest_requests.csv"), "request_id",
            "guest_requests.csv", self.audit))

    @property
    def tariffs(self) -> list[dict]:
        def build():
            rows = read_csv_dicts(self.dir / "tariff.csv")
            out, seen = [], set()
            for row in rows:
                if row.get("timestamp_utc") is None or row["timestamp_utc"] in seen:
                    continue
                seen.add(row["timestamp_utc"])
                row["price_eur_kwh"] = (float(row["price_eur_kwh"])
                                        if row.get("price_eur_kwh") is not None else None)
                out.append(row)
            out.sort(key=lambda r: r["timestamp_utc"])
            return out
        return self._memo("tariffs", build)

    @property
    def forecasts(self) -> dict[str, list[dict]]:
        """target_utc -> rows sorted by issued_utc."""
        def build():
            by_target: dict[str, list[dict]] = {}
            for row in read_csv_dicts(self.dir / "weather_forecast.csv"):
                if row.get("issued_utc") is None or row.get("target_utc") is None:
                    continue
                row["outdoor_c_forecast"] = (float(row["outdoor_c_forecast"])
                                             if row.get("outdoor_c_forecast") is not None else None)
                by_target.setdefault(row["target_utc"], []).append(row)
            for rows in by_target.values():
                rows.sort(key=lambda r: r["issued_utc"])
            return dict(sorted(by_target.items()))
        return self._memo("forecasts", build)

    @property
    def raw_events(self) -> list[dict]:
        """events.jsonl sorted by (timestamp_utc, event_id), de-duplicated by event_id."""
        def build():
            path = self.dir / "events.jsonl"
            out, seen, dropped, bad = [], set(), 0, 0
            if not path.exists():
                log.warning("missing file %s", path)
                return out
            with path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                        ident, ts = event["event_id"], event["timestamp_utc"]
                    except (ValueError, KeyError, TypeError):
                        bad += 1
                        continue
                    if ident in seen:
                        dropped += 1
                        continue
                    seen.add(ident)
                    event["timestamp_utc"] = norm_ts(ts)
                    out.append(event)
            out.sort(key=lambda e: (e["timestamp_utc"], e["event_id"]))
            if dropped or bad:
                self.audit["events.jsonl:duplicates_dropped"] = dropped
                self.audit["events.jsonl:unparseable_lines"] = bad
                log.info("events.jsonl: dropped %d duplicates, %d bad lines", dropped, bad)
            return out
        return self._memo("raw_events", build)
