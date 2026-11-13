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
import io
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
PARSE_MEMO_ROWS = 150_000  # per file; keeps Dev memory bounded
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


# Columns without which a file cannot be replayed causally. A zero-row file with
# a header is a valid empty stream; a file without these columns is schema loss.
REQUIRED_COLUMNS: dict[str, tuple[str, ...]] = {
    "bookings.csv": ("booking_id", "room_id", "created_utc", "arrival_utc", "departure_utc"),
    "access_log.csv": ("timestamp_utc", "door_id"),
    "room_telemetry.csv": ("timestamp_utc", "room_id"),
    "room_water.csv": ("timestamp_utc", "room_id", "meter_total_l"),
    "water_main.csv": ("timestamp_utc", "meter_total_l"),
    "energy.csv": ("timestamp_utc",),
    "weather.csv": ("timestamp_utc",),
    "weather_forecast.csv": ("issued_utc", "target_utc"),
    "tariff.csv": ("timestamp_utc", "published_utc"),
    "equipment.csv": ("timestamp_utc", "asset_id", "metric", "value"),
    "logistics.csv": ("timestamp_utc", "asset_id", "metric", "value"),
    "staff_roster.csv": ("shift_id", "resource_id", "start_utc", "end_utc", "status",
                         "status_known_utc"),
    "work_orders.csv": ("work_order_id", "created_utc"),
    "guest_requests.csv": ("request_id", "timestamp_utc", "request_type", "text"),
}


def read_header(path: Path) -> list[str]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [c.strip() for c in next(csv.reader(handle), [])]


def check_headers(root: Path) -> list[str]:
    """Problems with required CSV headers (empty list = fine). Missing files are skipped."""
    problems = []
    for name, required in REQUIRED_COLUMNS.items():
        path = root / name
        if not path.is_file():
            continue
        header = read_header(path)
        if not header:
            # zero-byte file: tolerated as an empty stream (existing fixtures rely on
            # it) but reported loudly through Dataset.audit / logs, never silently
            continue
        lacking = [c for c in required if c not in header]
        if lacking:
            problems.append(f"{name}: missing column(s) {', '.join(lacking)}")
    return problems


def inspect_dataset(data_dir: str | Path) -> dict:
    """Preflight: required files and CSV headers; returns dataset metadata."""
    root = Path(data_dir)
    missing = [name for name in REQUIRED_FILES if not (root / name).is_file()]
    if missing:
        raise ValueError(f"Missing dataset files in {root}: {', '.join(missing)}")
    problems = check_headers(root)
    if problems:
        raise ValueError(f"Unusable dataset schema in {root}: {'; '.join(problems)}")
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8-sig"))
    building = json.loads((root / "building.json").read_text(encoding="utf-8-sig"))
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


def data_ts(value: str) -> str:
    """Canonical timestamp of a SOURCE fact, rounded UP to the next full second.

    Availability keys use a whole-second grid (an implementation choice, not a
    contract requirement). A fact stamped 00:15:00.500Z must not be
    visible to a query as of 00:15:00Z, so sub-second source times round up
    (as-of/query bounds use ``norm_ts`` and round down). Never early, at most
    one second late.
    """
    parsed = parse_ts(value)
    if parsed.microsecond:
        parsed = parsed.replace(microsecond=0) + timedelta(seconds=1)
    return fmt_ts(parsed)


def source_ts(value: str) -> str:
    """Source instant in UTC with its ORIGINAL precision (fractions kept)."""
    parsed = parse_ts(value)
    if not parsed.microsecond:
        return fmt_ts(parsed)
    return parsed.strftime("%Y-%m-%dT%H:%M:%S.%f").rstrip("0") + "Z"


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
        self._memo: dict[str, dict[str, Any]] = {}
        self.bad_rows = 0
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            text = handle.read()
        # Fast path keeps raw lines; files with quoted cells are parsed by the csv
        # module (embedded commas/newlines) and kept as cell tuples instead.
        rows: list[Any]
        if '"' in text:
            parsed = [tuple(r) for r in csv.reader(io.StringIO(text, newline=""))]
            self.columns = [c.strip() for c in parsed[0]] if parsed else []
            rows = parsed[1:]
        else:
            lines = text.splitlines()
            self.columns = [c.strip() for c in lines[0].split(",")] if lines else []
            rows = lines[1:]
        ts_i = self.columns.index("timestamp_utc") if "timestamp_utc" in self.columns else 0
        ent_i = (self.columns.index(self.entity_col)
                 if self.entity_col in self.columns else None)
        met_i = (self.columns.index(self.metric_col)
                 if self.metric_col in self.columns else None)
        nsplit = max(i for i in (ent_i, met_i, ts_i) if i is not None) + 1
        groups: dict[tuple[str, str], tuple[list[str], list[Any]]] = {}
        unsorted_groups: set[tuple[str, str]] = set()
        for line in rows:
            if not line or (isinstance(line, tuple) and not any(line)):
                continue
            self.rows_read += 1
            head = line if isinstance(line, tuple) else line.split(",", nsplit)
            if len(head) < nsplit:
                self.bad_rows += 1
                continue
            ts = head[ts_i]
            if not (len(ts) == 20 and ts[19] == "Z" and ts[10] == "T"):
                try:  # +00:00, fractional seconds, lowercase z, local offsets ...
                    raw_ts = ts
                    ts = data_ts(raw_ts)       # lookup key: rounded UP, never early
                    shown = source_ts(raw_ts)  # returned value keeps source precision
                except (ValueError, TypeError):
                    self.bad_rows += 1
                    continue
                cells = list(line) if isinstance(line, tuple) else line.split(",")
                cells[ts_i] = shown
                line = tuple(cells)
            key = (head[ent_i].strip() if ent_i is not None else "",
                   head[met_i].strip() if met_i is not None else "")
            group = groups.get(key)
            if group is None:
                group = groups[key] = ([], [])
            ts_list, line_list = group
            if ts_list:
                if ts == ts_list[-1]:
                    # same key second: a duplicate unless the source instants differ
                    # (distinct sub-second readings are both kept) or it is a door log
                    prev = line_list[-1]
                    distinct = line != prev and (
                        (line[ts_i] if isinstance(line, tuple) else ts)
                        != (prev[ts_i] if isinstance(prev, tuple) else ts))
                    if not distinct and (filename != "access_log.csv" or line == line_list[-1]):
                        self.duplicates_dropped += 1
                        continue
                    if distinct:
                        unsorted_groups.add(key)  # order the bucket by original instant
                elif ts < ts_list[-1]:
                    unsorted_groups.add(key)
            ts_list.append(ts)
            line_list.append(line)
        for key in unsorted_groups:  # out-of-order rows or several readings in one second
            ts_list, line_list = groups[key]

            def shown(i: int) -> datetime:  # original (possibly sub-second) instant
                row = line_list[i]
                return parse_ts(row[ts_i] if isinstance(row, tuple) else ts_list[i])

            order = sorted(range(len(ts_list)), key=lambda i: (ts_list[i], shown(i)))
            seen: set[tuple[str, str]] = set()
            new_ts, new_lines = [], []
            for i in order:
                ident = (ts_list[i], shown(i))
                if ident in seen and filename != "access_log.csv":
                    self.duplicates_dropped += 1
                    continue
                seen.add(ident)
                new_ts.append(ts_list[i])
                new_lines.append(line_list[i])
            groups[key] = (new_ts, new_lines)
        self.groups = groups
        # Group membership is immutable after loading. Preserve the original sorted
        # key order once, rather than rescanning/sorting every entity lookup.
        self._all_keys = tuple(sorted(groups))
        by_entity: dict[str, list[tuple[str, str]]] = {}
        by_metric: dict[str, list[tuple[str, str]]] = {}
        for key in self._all_keys:
            by_entity.setdefault(key[0], []).append(key)
            by_metric.setdefault(key[1], []).append(key)
        self._entity_keys = {name: tuple(keys) for name, keys in by_entity.items()}
        self._metric_keys = {name: tuple(keys) for name, keys in by_metric.items()}
        self.entities = sorted({k[0] for k in groups if k[0]})
        if self.duplicates_dropped:
            log.info("%s: dropped %d duplicate rows (same timestamp+key)",
                     filename, self.duplicates_dropped)

    def _parse(self, line: Any) -> dict[str, Any]:
        """Parsed copy of one raw line; a bounded memo serves overlapping windows."""
        cached = self._memo.get(line)
        if cached is None:
            if len(self._memo) >= PARSE_MEMO_ROWS:
                self._memo.clear()
            cached = self._memo[line] = self._parse_line(line)
        return dict(cached)

    def _parse_line(self, line: Any) -> dict[str, Any]:
        cells = list(line) if isinstance(line, tuple) else line.split(",")
        cols = self.columns
        if len(cells) < len(cols):
            cells = cells + [""] * (len(cols) - len(cells))
        return {c: parse_value(c, cells[i].strip()) for i, c in enumerate(cols)}

    def _keys(self, entity_id: str | None, metric: str | None) -> list[tuple[str, str]]:
        entity_filter = entity_id is not None and bool(self.entity_col)
        metric_filter = metric is not None and bool(self.metric_col)
        if entity_filter and metric_filter:
            key = (entity_id, metric)
            return [key] if key in self.groups else []
        if entity_filter:
            return list(self._entity_keys.get(entity_id, ()))
        if metric_filter:
            return list(self._metric_keys.get(metric, ()))
        return list(self._all_keys)

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
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = [{k.strip(): none_if_blank(v) for k, v in row.items()
                 if k is not None and isinstance(v, str)}
                for row in csv.DictReader(handle)]
    for row in rows:  # canonical UTC strings so string comparison stays chronological
        for key, value in row.items():
            if key.endswith("_utc") and value is not None and not (
                    len(value) == 20 and value[19] == "Z" and value[10] == "T"):
                try:
                    row[key] = data_ts(value)
                except (ValueError, TypeError):
                    log.warning("%s: unparseable %s=%r set to None", path.name, key, value)
                    row[key] = None
    return rows


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


# events.jsonl fields that consumers use as identifiers/keys. A non-string value
# (list, dict, number, bool) cannot be a valid id and would be unhashable or
# ambiguous downstream; the original is preserved under "source_fields".
IDENTITY_FIELDS = ("type", "room_id", "booking_id", "asset_id", "resource_id",
                   "from_room", "to_room", "alarm_code", "state", "product")


def _sanitize_identity_fields(event: dict) -> None:
    invalid = {}
    for key in IDENTITY_FIELDS:
        if key in event and event[key] is not None and not isinstance(event[key], str):
            invalid[key] = event[key]
            event[key] = "unknown" if key == "type" else None
    if "type" not in event or event.get("type") in (None, ""):
        if "type" in event:
            invalid.setdefault("type", event["type"])
        event["type"] = "unknown"
    if invalid:
        existing = event.get("source_fields")
        if "source_fields" in event and not isinstance(existing, dict):
            # a raw "source_fields" value that is not a mapping is itself kept
            invalid["source_fields"] = existing
            existing = None
        event["source_fields"] = {**(existing or {}), **invalid}


class Dataset:
    """Lazy, cached access to the raw files of ONE dataset directory."""

    def __init__(self, data_dir: str | Path):
        self.dir = Path(data_dir)
        if not self.dir.is_dir():
            raise FileNotFoundError(f"data directory not found: {self.dir}")
        self.audit: dict[str, Any] = {}
        self._series: dict[str, Series] = {}
        self._cache: dict[str, Any] = {}
        self._note_empty_files()

    # -- generic helpers
    def _note_empty_files(self) -> None:
        for name in REQUIRED_COLUMNS:
            path = self.dir / name
            if path.is_file() and path.stat().st_size == 0:
                self.audit[name + ":empty_no_header"] = True
                log.warning("%s is a zero-byte file (no header): treated as an empty stream", name)
            elif not path.is_file():
                self.audit[name + ":missing"] = True

    def _memo(self, name: str, build):
        if name not in self._cache:
            self._cache[name] = build()
        return self._cache[name]

    def _json(self, name: str) -> dict:
        path = self.dir / name
        if not path.exists():
            log.warning("missing file %s", path)
            return {}
        return json.loads(path.read_text(encoding="utf-8-sig"))

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
            if ser.bad_rows:
                self.audit[filename + ":unparseable_rows"] = ser.bad_rows
                log.warning("%s: skipped %d rows without a usable timestamp/key",
                            filename, ser.bad_rows)
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
        """All price rows sorted by (timestamp_utc, published_utc); revisions are kept."""
        def build():
            out, seen = [], set()
            for row in read_csv_dicts(self.dir / "tariff.csv"):
                if row.get("timestamp_utc") is None:
                    continue
                ident = (row["timestamp_utc"], row.get("published_utc"), row.get("price_eur_kwh"))
                if ident in seen:
                    continue
                seen.add(ident)
                try:
                    row["price_eur_kwh"] = (float(row["price_eur_kwh"])
                                            if row.get("price_eur_kwh") is not None else None)
                except ValueError:
                    row["price_eur_kwh"] = None
                out.append(row)
            out.sort(key=lambda r: (r["timestamp_utc"], r.get("published_utc") or ""))
            return out
        return self._memo("tariffs", build)

    @property
    def interval_seconds(self) -> int:
        """Manifest sampling interval; anything unusable falls back to 900 s."""
        raw = self.manifest.get("interval_seconds")
        if isinstance(raw, bool) or not isinstance(raw, (int, float)) or raw != int(raw) \
                or not 60 <= int(raw) <= 86400:
            if raw is not None:
                log.warning("manifest interval_seconds=%r is not usable; using 900", raw)
                self.audit["manifest:interval_seconds_invalid"] = repr(raw)
            return 900
        return int(raw)

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
            with path.open("r", encoding="utf-8-sig") as handle:
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
                    if not isinstance(event, dict):
                        bad += 1
                        continue
                    try:
                        raw_ts = ts
                        ts = data_ts(raw_ts)
                        if not (len(raw_ts) == 20 and raw_ts.endswith("Z")):
                            precise = source_ts(raw_ts)
                            if precise != ts:
                                event["_source_ts"] = precise  # original instant, kept
                    except (ValueError, TypeError, AttributeError):
                        bad += 1
                        continue
                    ident = str(ident)
                    event["event_id"] = ident
                    _sanitize_identity_fields(event)
                    if ident in seen:
                        dropped += 1
                        continue
                    seen.add(ident)
                    event["timestamp_utc"] = ts
                    out.append(event)
            out.sort(key=lambda e: (e["timestamp_utc"], e["event_id"]))
            if dropped or bad:
                self.audit["events.jsonl:duplicates_dropped"] = dropped
                self.audit["events.jsonl:unparseable_lines"] = bad
                log.info("events.jsonl: dropped %d duplicates, %d bad lines", dropped, bad)
            return out
        return self._memo("raw_events", build)
