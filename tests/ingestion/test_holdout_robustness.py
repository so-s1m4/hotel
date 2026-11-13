"""L5 holdout robustness regressions and controls.

Run from a hotel checkout with pytest and the project's dependencies installed::

    GUEST_PARSER_OFFLINE=1 PLATO_FUZZ_QUICKSTART=/path/to/PLATO40_v2_quickstart \
        python -m pytest -q tests/ingestion/test_holdout_robustness.py

All mutations use pytest temporary copies or small fixtures matching Quickstart's
source schemas. No actual holdout, production code changes, or model calls.
Defect assertions intentionally fail on affected revisions and pass after repairs.
The original discovery revision was db9abf1; results are recorded per tested SHA.
Optional HOLDOUT_FUZZ_BASELINE_DIR reuses events/decisions named baseline_*.jsonl;
without it the integration controls generate a clean baseline once per session.
"""
from __future__ import annotations


# LANE_CSV

import csv

import json

import os

import shutil

from pathlib import Path

import pytest

from ingestion.loader import Dataset, inspect_dataset

from ingestion.replay import ReplayData

@pytest.fixture
def csv_qs_copy(tmp_path):
    source = Path(os.environ.get("PLATO_FUZZ_QUICKSTART", "data/PLATO40_v2_quickstart"))
    if not (source / "manifest.json").is_file():
        pytest.skip("Set PLATO_FUZZ_QUICKSTART to the unmodified public Quickstart")
    target = tmp_path / "quickstart"
    shutil.copytree(source, target)
    return target

def csv_read_rows(path):
    with Path(path).open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        return list(reader.fieldnames), list(reader)

def csv_write_rows(path, columns, rows, *, quoting=csv.QUOTE_MINIMAL, newline="\n"):
    with Path(path).open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, quoting=quoting, lineterminator=newline)
        writer.writeheader()
        writer.writerows(rows)

def csv_measurement_witness(data_dir):
    columns, rows = csv_read_rows(Path(data_dir) / "room_telemetry.csv")
    row = rows[0]
    timestamp, room = row["timestamp_utc"], row["room_id"]
    expected = ReplayData(data_dir, use_llm=False).get_measurements(
        "room_telemetry.csv", timestamp, timestamp, timestamp, room)
    assert expected, "Public Quickstart witness must contain a telemetry row"
    return columns, rows, timestamp, room, expected

def csv_get_witness(data_dir, timestamp, room):
    return ReplayData(data_dir, use_llm=False).get_measurements(
        "room_telemetry.csv", timestamp, timestamp, timestamp, room)

def csv_guest_records(data_dir):
    return {e.event_id: e.model_dump() for e in ReplayData(data_dir, use_llm=False).iter_events()
            if e.event_type == "guest_request"}

def test_csv_crlf_preserves_measurements(csv_qs_copy):
    columns, rows, timestamp, room, expected = csv_measurement_witness(csv_qs_copy)
    csv_write_rows(csv_qs_copy / "room_telemetry.csv", columns, rows, newline="\r\n")
    assert csv_get_witness(csv_qs_copy, timestamp, room) == expected

def test_csv_unsorted_rows_preserve_measurements(csv_qs_copy):
    columns, rows, timestamp, room, expected = csv_measurement_witness(csv_qs_copy)
    csv_write_rows(csv_qs_copy / "room_telemetry.csv", columns, list(reversed(rows)))
    assert csv_get_witness(csv_qs_copy, timestamp, room) == expected

def test_csv_reordered_series_columns_preserve_measurements(csv_qs_copy):
    columns, rows, timestamp, room, expected = csv_measurement_witness(csv_qs_copy)
    reordered = columns[1:] + columns[:1]
    csv_write_rows(csv_qs_copy / "room_telemetry.csv", reordered, rows)
    assert csv_get_witness(csv_qs_copy, timestamp, room) == expected

def test_csv_quote_all_preserves_measurements(csv_qs_copy):
    columns, rows, timestamp, room, expected = csv_measurement_witness(csv_qs_copy)
    csv_write_rows(csv_qs_copy / "room_telemetry.csv", columns, rows, quoting=csv.QUOTE_ALL)
    assert csv_get_witness(csv_qs_copy, timestamp, room) == expected

def test_csv_bom_preserves_measurement_schema(csv_qs_copy):
    _, _, timestamp, room, expected = csv_measurement_witness(csv_qs_copy)
    path = csv_qs_copy / "room_telemetry.csv"
    path.write_bytes(b"\xef\xbb\xbf" + path.read_bytes())
    assert csv_get_witness(csv_qs_copy, timestamp, room) == expected

def test_csv_bom_preserves_guest_requests(csv_qs_copy):
    expected = csv_guest_records(csv_qs_copy)
    path = csv_qs_copy / "guest_requests.csv"
    path.write_bytes(b"\xef\xbb\xbf" + path.read_bytes())
    assert csv_guest_records(csv_qs_copy) == expected

def test_csv_relational_column_order_and_quoted_multiline_text(csv_qs_copy):
    path = csv_qs_copy / "guest_requests.csv"
    columns, rows = csv_read_rows(path)
    request_id = rows[0]["request_id"]
    text = 'Please check, "thank you".\nSecond line of the same request.'
    rows[0]["text"] = text
    csv_write_rows(path, list(reversed(columns)), rows, newline="\r\n")
    result = csv_guest_records(csv_qs_copy)
    assert result[f"guest:{request_id}"]["payload"]["original_text"] == text

def test_csv_extra_quoted_comma_column_preserved(csv_qs_copy):
    columns, rows, timestamp, room, expected = csv_measurement_witness(csv_qs_copy)
    for row in rows:
        row["source_note"] = 'sensor, "verified"'
    csv_write_rows(csv_qs_copy / "room_telemetry.csv", columns + ["source_note"], rows)
    actual = csv_get_witness(csv_qs_copy, timestamp, room)
    assert actual == [{**row, "source_note": 'sensor, "verified"'} for row in expected]

def test_csv_extra_quoted_newline_column_preserved(csv_qs_copy):
    columns, rows, timestamp, room, expected = csv_measurement_witness(csv_qs_copy)
    for row in rows:
        row["source_note"] = "sensor line one\nline two"
    csv_write_rows(csv_qs_copy / "room_telemetry.csv", columns + ["source_note"], rows)
    actual = csv_get_witness(csv_qs_copy, timestamp, room)
    assert actual == [{**row, "source_note": "sensor line one\nline two"} for row in expected]

def test_csv_missing_request_id_is_rejected_by_preflight(csv_qs_copy):
    path = csv_qs_copy / "guest_requests.csv"
    columns, rows = csv_read_rows(path)
    for row in rows:
        del row["request_id"]
    csv_write_rows(path, [c for c in columns if c != "request_id"], rows)
    with pytest.raises((ValueError, KeyError)):
        inspect_dataset(csv_qs_copy)

def test_csv_exact_guest_duplicate_is_audited_and_coalesced(csv_qs_copy):
    path = csv_qs_copy / "guest_requests.csv"
    columns, rows = csv_read_rows(path)
    baseline = Dataset(csv_qs_copy)
    expected = baseline.guest_requests
    before = baseline.audit.get("guest_requests.csv:duplicates_dropped", 0)
    csv_write_rows(path, columns, rows + [dict(rows[0])])
    after = Dataset(csv_qs_copy)
    assert after.guest_requests == expected
    assert after.audit["guest_requests.csv:duplicates_dropped"] == before + 1

@pytest.mark.parametrize("prepend", [False, True])
def test_csv_conflicting_guest_duplicate_documented_first_wins(csv_qs_copy, prepend):
    path = csv_qs_copy / "guest_requests.csv"
    columns, rows = csv_read_rows(path)
    duplicate = {**rows[0], "text": "Conflicting same-ID source content"}
    source = [duplicate] + rows if prepend else rows + [duplicate]
    csv_write_rows(path, columns, source)
    dataset = Dataset(csv_qs_copy)
    row = next(r for r in dataset.guest_requests if r["request_id"] == rows[0]["request_id"])
    assert row["text"] == (duplicate["text"] if prepend else rows[0]["text"])
    assert dataset.audit["guest_requests.csv:duplicates_dropped"] >= 1

@pytest.mark.parametrize("prepend", [False, True])
def test_csv_conflicting_raw_event_documented_first_wins(csv_qs_copy, prepend):
    path = csv_qs_copy / "events.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    duplicate = {**rows[0], "csv_fuzz_note": "conflicting same-ID content"}
    source = [duplicate] + rows if prepend else rows + [duplicate]
    path.write_text("".join(json.dumps(row) + "\n" for row in source), encoding="utf-8")
    dataset = Dataset(csv_qs_copy)
    row = next(r for r in dataset.raw_events if r["event_id"] == rows[0]["event_id"])
    assert ("csv_fuzz_note" in row) is prepend
    assert dataset.audit["events.jsonl:duplicates_dropped"] >= 1


# LANE_TEMPORAL

import subprocess

import sys

from datetime import datetime, timedelta

from common.contracts.schemas import SourceRefV1

TEMPORAL_START = "2026-01-05T00:00:00Z"

TEMPORAL_CUTOFF = "2026-01-05T00:15:00Z"

TEMPORAL_FUTURE = "2026-01-05T00:15:00.500Z"

TEMPORAL_END = "2026-01-05T00:30:00Z"

TEMPORAL_HEADERS = {
    "bookings.csv": "booking_id,room_id,created_utc,arrival_utc,departure_utc,guest_count,channel,status",
    "access_log.csv": "timestamp_utc,door_id,credential,result",
    "room_telemetry.csv": "timestamp_utc,room_id,temperature_c,setpoint_c,humidity_pct,co2_ppm,presence,window_open,housekeeping_state",
    "room_water.csv": "timestamp_utc,room_id,meter_total_l",
    "water_main.csv": "timestamp_utc,meter_total_l",
    "energy.csv": "timestamp_utc,building_load_kw,hvac_electric_kw,pv_kw,battery_discharge_kw_signed,battery_soc_kwh,grid_import_kw,grid_export_kw",
    "weather.csv": "timestamp_utc,outdoor_c,cloud_cover_pct",
    "weather_forecast.csv": "issued_utc,target_utc,outdoor_c_forecast",
    "tariff.csv": "timestamp_utc,price_eur_kwh,published_utc",
    "equipment.csv": "timestamp_utc,asset_id,metric,value",
    "logistics.csv": "timestamp_utc,asset_id,metric,value",
    "staff_roster.csv": "shift_id,resource_id,role,start_utc,end_utc,status,status_known_utc",
    "work_orders.csv": "work_order_id,created_utc,asset_id,type,status,scheduled_utc,completed_utc,assigned_to,summary",
    "guest_requests.csv": "request_id,timestamp_utc,room_id,booking_id,channel,language,request_type,text,priority,response_due_utc,requires_guest_consent",
}

def temporal_instant(value):
    return datetime.fromisoformat(value.removesuffix("Z").removesuffix("z") +
                                 ("+00:00" if value.endswith(("Z", "z")) else ""))

def temporal_write_rows(directory, filename, rows):
    with (directory / filename).open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=TEMPORAL_HEADERS[filename].split(","))
        writer.writeheader()
        writer.writerows(rows)

def temporal_telemetry(timestamp, temperature=21):
    return dict(timestamp_utc=timestamp, room_id="R101", temperature_c=temperature,
                setpoint_c=21, humidity_pct=45, co2_ppm=650, presence=0,
                window_open=0, housekeeping_state="clean")

@pytest.fixture
def temporal_dataset(tmp_path, monkeypatch):
    monkeypatch.setenv("GUEST_PARSER_OFFLINE", "1")
    monkeypatch.setenv("INGESTION_USE_LLM", "0")
    monkeypatch.setenv("GUEST_PARSER_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    count = 0

    def make(*, manifest_updates=None, missing=(), rows=None, events=()):
        nonlocal count
        count += 1
        directory = tmp_path / f"temporal_case_{count}"
        directory.mkdir()
        tables = {
            "room_telemetry.csv": [temporal_telemetry(TEMPORAL_START),
                                   temporal_telemetry(TEMPORAL_CUTOFF)],
            "water_main.csv": [dict(timestamp_utc=TEMPORAL_START, meter_total_l=100),
                               dict(timestamp_utc=TEMPORAL_CUTOFF, meter_total_l=101)],
        }
        tables.update(rows or {})
        for filename in TEMPORAL_HEADERS:
            temporal_write_rows(directory, filename, tables.get(filename, []))
        manifest = dict(model_id="PLATO-40", profile="synthetic-temporal-fuzz",
                        synthetic=True, start_utc=TEMPORAL_START,
                        end_utc=TEMPORAL_END, interval_seconds=900)
        manifest.update(manifest_updates or {})
        for field in missing:
            manifest.pop(field, None)
        (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        building = {
            "time": {"data_timezone": "UTC", "local_timezone": "Europe/Vienna"},
            "building": {"rooms": [{"id": "R101", "floor": 1, "max_guests": 2}],
                         "assets": [{"id": "HP1", "type": "heat_pump"}]},
            "staff": {"resources": [{"resource_id": "ONCALL_1", "role": "on_call_housekeeping"}],
                      "notes": {"on_call_housekeeping": "15:30-22:00 local; 45 min lead"}},
            "operations": {"check_in_from_local": "15:00", "check_out_until_local": "11:00"},
            "constraints": {"occupied_comfort_c": [20, 23], "room_release_requires":
                            ["vacant", "clean", "not_blocked", "inspection_passed_if_required"]},
        }
        (directory / "building.json").write_text(json.dumps(building), encoding="utf-8")
        (directory / "events.jsonl").write_text("".join(json.dumps(x) + "\n" for x in events), encoding="utf-8")
        return directory

    return make

@pytest.mark.parametrize("cutoff", [TEMPORAL_CUTOFF, "2026-01-05T00:15:00+00:00",
                                     "2026-01-05T00:15:00z", "2026-01-05T01:15:00+01:00"])
def test_temporal_query_formats_on_canonical_source(temporal_dataset, cutoff):
    repo = ReplayData(temporal_dataset(), use_llm=False)
    assert repo.get_room_state("R101", cutoff) == repo.get_room_state("R101", TEMPORAL_CUTOFF)

@pytest.mark.parametrize("timestamp", ["2026-01-05T00:15:00+00:00", "2026-01-05T00:15:00z"])
def test_temporal_noncanonical_source_exact_boundary_robustness(temporal_dataset, timestamp):
    """Outside canonical Z requirement: rejection or correct normalization is safe."""
    path = temporal_dataset(rows={"room_telemetry.csv": [temporal_telemetry(timestamp)]})
    try:
        rows = ReplayData(path, use_llm=False).get_measurements(
            "room_telemetry.csv", TEMPORAL_CUTOFF, TEMPORAL_CUTOFF, TEMPORAL_CUTOFF, "R101")
    except (ValueError, TypeError):
        return
    assert len(rows) == 1, "Accepted equivalent noncanonical source instant silently omitted at exact boundary"

def test_temporal_fractional_z_is_valid_frozen_schema():
    source = SourceRefV1(file="probe.csv", record_id="probe", timestamp_utc=TEMPORAL_FUTURE)
    assert temporal_instant(source.timestamp_utc) == temporal_instant(TEMPORAL_FUTURE)

@pytest.mark.parametrize("getter", ["measurements", "room_state", "events", "booking", "roster", "work_order", "forecast", "tariff"])
def test_temporal_fractional_future_is_not_visible(temporal_dataset, getter):
    """All source times end Z; future facts are 500 ms after the cutoff."""
    rows = {
        "room_telemetry.csv": [temporal_telemetry(TEMPORAL_START), temporal_telemetry(TEMPORAL_FUTURE, 99)],
        "bookings.csv": [dict(booking_id="BK_FUTURE", room_id="R101", created_utc=TEMPORAL_FUTURE,
                              arrival_utc=TEMPORAL_END, departure_utc="2026-01-06T10:00:00Z", status="confirmed")],
        "staff_roster.csv": [dict(shift_id="SH_FUTURE", resource_id="CLEANER_1", role="cleaner",
                                  start_utc=TEMPORAL_START, end_utc=TEMPORAL_END,
                                  status="sick", status_known_utc=TEMPORAL_FUTURE)],
        "work_orders.csv": [dict(work_order_id="WO_FUTURE", created_utc=TEMPORAL_START, asset_id="HP1",
                                 type="corrective", status="completed", completed_utc=TEMPORAL_FUTURE,
                                 scheduled_utc=TEMPORAL_CUTOFF, summary="FUTURE OUTCOME")],
        "weather_forecast.csv": [dict(issued_utc=TEMPORAL_FUTURE, target_utc=TEMPORAL_END,
                                      outdoor_c_forecast=99)],
        "tariff.csv": [dict(timestamp_utc=TEMPORAL_END, price_eur_kwh=9,
                            published_utc=TEMPORAL_FUTURE)],
    }
    events = [dict(event_id="E_FUTURE", timestamp_utc=TEMPORAL_FUTURE, type="checkin", room_id="R101")]
    repo = ReplayData(temporal_dataset(rows=rows, events=events), use_llm=False)
    if getter == "measurements":
        result = repo.get_measurements("room_telemetry.csv", TEMPORAL_START, TEMPORAL_END, TEMPORAL_CUTOFF, "R101")
        assert len(result) == 1 and result[0]["temperature_c"] == 21, result
        assert all(temporal_instant(r["timestamp_utc"]) <= temporal_instant(TEMPORAL_CUTOFF) for r in result), result
    elif getter == "room_state":
        result = repo.get_room_state("R101", TEMPORAL_CUTOFF)
        assert result["temperature_c"] == 21 and result["occupancy"] == "unknown", result
    elif getter == "events":
        assert repo.get_events(TEMPORAL_START, TEMPORAL_END, TEMPORAL_CUTOFF) == []
    elif getter == "booking":
        assert repo.get_booking("BK_FUTURE", TEMPORAL_CUTOFF) is None
    elif getter == "roster":
        result = repo.get_staff_slots(TEMPORAL_START, TEMPORAL_END, TEMPORAL_CUTOFF, role="cleaner")
        assert len(result) == 1 and result[0]["effective_status_asof"] == "scheduled", result
    elif getter == "work_order":
        result = repo.get_work_orders(TEMPORAL_CUTOFF)[0]
        assert result["completed_utc"] is None and result["summary"] == "", result
    elif getter == "forecast":
        assert repo.get_weather_forecast(TEMPORAL_CUTOFF, TEMPORAL_END) == []
    else:
        assert repo.get_tariffs(TEMPORAL_CUTOFF, TEMPORAL_END) == []

@pytest.mark.xfail(strict=True, reason="Documented policy: availability uses a whole-second "
                   "grid (source instants round UP, never early); within one second the "
                   "contract sort key (available_at_utc, event_id) decides. The original "
                   "instant is preserved in timestamp_utc/payload.")
def test_temporal_fractional_events_keep_chronological_order(temporal_dataset):
    events = [dict(event_id="A_LATE", timestamp_utc="2026-01-05T00:15:00.900Z", type="probe"),
              dict(event_id="Z_EARLY", timestamp_utc="2026-01-05T00:15:00.100Z", type="probe")]
    repo = ReplayData(temporal_dataset(events=events), use_llm=False, include_ticks=False)
    normalized = list(repo.iter_events())
    assert [e.source.record_id for e in normalized] == ["Z_EARLY", "A_LATE"]
    assert [temporal_instant(e.available_at_utc) for e in normalized] == [temporal_instant(events[1]["timestamp_utc"]), temporal_instant(events[0]["timestamp_utc"])]

def test_temporal_subsecond_measurements_preserve_distinct_source_rows(temporal_dataset):
    early = "2026-01-05T00:15:00.100Z"
    late = "2026-01-05T00:15:00.900Z"
    path = temporal_dataset(rows={"room_telemetry.csv": [
        temporal_telemetry(early, 21), temporal_telemetry(late, 22)]})
    result = ReplayData(path, use_llm=False).get_measurements(
        "room_telemetry.csv", TEMPORAL_START, TEMPORAL_END, TEMPORAL_END, "R101")
    assert [r["temperature_c"] for r in result] == [21, 22], result
    assert [temporal_instant(r["timestamp_utc"]) for r in result] == [
        temporal_instant(early), temporal_instant(late)]


@pytest.mark.parametrize("step", [300, 600, 900, 1800])
def test_temporal_positive_manifest_intervals(temporal_dataset, step):
    repo = ReplayData(temporal_dataset(manifest_updates={"interval_seconds": step}), use_llm=False)
    ticks = [e for e in repo.iter_events() if e.event_type == "clock_tick"]
    assert len(ticks) == 1800 // step
    assert all(temporal_instant(b.available_at_utc) - temporal_instant(a.available_at_utc) == timedelta(seconds=step)
               for a, b in zip(ticks, ticks[1:]))

def temporal_subprocess_environment():
    import ingestion
    env = dict(os.environ)
    root = Path(ingestion.__file__).resolve().parent.parent
    env.update(PYTHONPATH=str(root), GUEST_PARSER_OFFLINE="1", INGESTION_USE_LLM="0", PYTHONDONTWRITEBYTECODE="1")
    return root, env

@pytest.mark.parametrize("step", [-900, 0, "0", 0.5, "invalid"])
def test_temporal_invalid_interval_fails_bounded_and_cleanly(temporal_dataset, step):
    """Invalid clocks reject; a positive fractional step may be explicitly supported."""
    path = temporal_dataset(manifest_updates={"interval_seconds": step})
    root, env = temporal_subprocess_environment()
    code = """import json,sys
# The parent wall timeout always applies. Optional child-side guards avoid a
# preexec import failure and macOS virtual-address-space assumptions.
try:
    import resource
    resource.setrlimit(resource.RLIMIT_CPU, (5, 5))
    if sys.platform.startswith('linux'):
        resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024))
except (ImportError, AttributeError, OSError, ValueError):
    pass
from ingestion.replay import ReplayData
try:
    n = len(list(ReplayData(sys.argv[1], use_llm=False).iter_events()))
except (ValueError, TypeError) as e:
    print(json.dumps({'rejected': True, 'error': str(e)}))
else:
    print(json.dumps({'rejected': False, 'events': n}))
"""
    try:
        result = subprocess.run([sys.executable, "-c", code, str(path)], cwd=root, env=env,
                                text=True, capture_output=True, timeout=10)
    except subprocess.TimeoutExpired:
        pytest.fail(f"interval_seconds={step!r}: exceeded 10-second subprocess timeout")
    assert result.returncode == 0, f"interval_seconds={step!r}: no clean rejection; rc={result.returncode}; {result.stderr[-250:]}"
    outcome = json.loads(result.stdout)
    if True:
        # Policy (owner/coordinator): an unusable interval must not abort a holdout
        # run. It is replaced by 900 s with a warning and an audit entry, never silently.
        assert outcome["rejected"] or "interval_seconds" in result.stderr, (
            f"interval_seconds={step!r}: silently accepted: {result.stdout}")

@pytest.mark.parametrize("missing", [("start_utc",), ("end_utc",), ("start_utc", "end_utc")])
def test_temporal_missing_manifest_range_fallback(temporal_dataset, missing):
    repo = ReplayData(temporal_dataset(missing=missing), use_llm=False)
    ticks = [e.available_at_utc for e in repo.iter_events() if e.event_type == "clock_tick"]
    assert ticks == [TEMPORAL_START, TEMPORAL_CUTOFF]

@pytest.mark.parametrize("missing", [("start_utc",), ("end_utc",), ("start_utc", "end_utc")])
def test_temporal_run_preflight_rejects_missing_required_range(temporal_dataset, missing):
    """Preflight rejection is appropriate; direct ingestion has a broader fallback."""
    path = temporal_dataset(missing=missing)
    root, env = temporal_subprocess_environment()
    result = subprocess.run([sys.executable, str(root / "run.py"), "--data-dir", str(path), "--check"],
                            cwd=root, env=env, text=True, capture_output=True, timeout=5)
    assert result.returncode == 2 and "Input error" in result.stderr

@pytest.mark.parametrize("start,end,count", [
    ("2026-03-28T23:00:00Z", "2026-03-29T22:00:00Z", 92),
    ("2026-10-24T22:00:00Z", "2026-10-25T23:00:00Z", 100),
    ("2026-12-31T23:30:00Z", "2027-01-01T00:30:00Z", 4),
])
def test_temporal_dst_and_year_boundary_ticks(temporal_dataset, start, end, count):
    repo = ReplayData(temporal_dataset(manifest_updates={"start_utc": start, "end_utc": end}), use_llm=False)
    ticks = [e for e in repo.iter_events() if e.event_type == "clock_tick"]
    assert len(ticks) == count and len({e.event_id for e in ticks}) == count
    assert all(temporal_instant(b.available_at_utc) - temporal_instant(a.available_at_utc) == timedelta(minutes=15)
               for a, b in zip(ticks, ticks[1:]))
    local = [datetime.fromisoformat(e.payload["local_time"]) for e in ticks]
    if count == 92:
        assert not any(t.hour == 2 for t in local)
    elif count == 100:
        repeated = [t for t in local if t.hour == 2]
        assert len(repeated) == 8 and {t.utcoffset() for t in repeated} == {timedelta(hours=1), timedelta(hours=2)}

@pytest.mark.parametrize("start,end,expected", [
    ("2026-03-28T00:00:00Z", "2026-03-29T23:00:00Z", ["2026-03-28T14:30:00Z", "2026-03-29T13:30:00Z"]),
    ("2026-10-24T00:00:00Z", "2026-10-25T23:00:00Z", ["2026-10-24T13:30:00Z", "2026-10-25T14:30:00Z"]),
])
def test_temporal_oncall_local_hours_across_dst(temporal_dataset, start, end, expected):
    repo = ReplayData(temporal_dataset(), use_llm=False)
    slots = repo.get_staff_slots(start, end, start, role="on_call_housekeeping")
    assert [row["start_utc"] for row in slots] == expected
    assert all(row["simulated_assumption"] is True for row in slots)


# LANE_UNKNOWNS

import hashlib

_UNKNOWN_RUNS: list[dict] = []

@pytest.fixture(scope="session")
def unknown_paths(tmp_path_factory):
    import ingestion

    repo = Path(ingestion.__file__).resolve().parents[1]
    data = Path(os.environ.get("PLATO_FUZZ_QUICKSTART") or
                os.environ.get("HOLDOUT_FUZZ_DATA") or repo / "data/PLATO40_v2_quickstart")
    output = (Path(os.environ["HOLDOUT_FUZZ_OUTPUT"]) if os.environ.get("HOLDOUT_FUZZ_OUTPUT")
              else tmp_path_factory.mktemp("unknown-results"))
    baseline = os.environ.get("HOLDOUT_FUZZ_BASELINE_DIR")
    assert (repo / "run.py").is_file(), repo
    if not (data / "manifest.json").is_file():
        pytest.skip("Independent Quickstart required; set PLATO_FUZZ_QUICKSTART or HOLDOUT_FUZZ_DATA")
    return {"repo": repo, "data": data, "output": output,
            "baseline": Path(baseline) if baseline else output}

@pytest.fixture(scope="session", autouse=True)
def unknown_results_report(unknown_paths):
    yield
    dest = unknown_paths["output"] / "unknown_results.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps({
        "lane": "optional-empty-unknowns", "reviewed_commit": os.environ.get("HOLDOUT_FUZZ_COMMIT"),
        "source_data": str(unknown_paths["data"]), "model_calls": 0,
        "runs": _UNKNOWN_RUNS,
    }, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

@pytest.fixture
def unknown_data(tmp_path, unknown_paths):
    target = tmp_path / "data"
    shutil.copytree(unknown_paths["data"], target)
    return target

def unknown_jsonl(path):
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]

def unknown_append_csv(path, rows):
    with path.open(encoding="utf-8", newline="") as stream:
        fields = next(csv.reader(stream))
    with path.open("a", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writerows(rows)

def unknown_header_only(path):
    with path.open(encoding="utf-8", newline="") as stream:
        fields = next(csv.reader(stream))
    with path.open("w", encoding="utf-8", newline="") as stream:
        csv.writer(stream).writerow(fields)

def unknown_invoke(paths, data, kind, outdir, test_id):
    outdir.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.update(GUEST_PARSER_OFFLINE="1", INGESTION_USE_LLM="0",
               PYTHONDONTWRITEBYTECODE="1")
    if kind == "ingestion":
        out = outdir / "events.jsonl"
        args = [sys.executable, "-m", "ingestion.cli", "--data-dir", str(data),
                "--out", str(out), "--no-llm"]
    else:
        out = outdir / "decisions.jsonl"
        args = [sys.executable, "run.py", "--data-dir", str(data), "--out", str(out)]
        if kind == "check":
            args.append("--check")
    proc = subprocess.run(args, cwd=paths["repo"], env=env,
                          capture_output=True, text=True, timeout=55)
    rows = unknown_jsonl(out)
    _UNKNOWN_RUNS.append({
        "test": test_id, "kind": kind, "argv": args,
        "returncode": proc.returncode, "stdout": proc.stdout,
        "stderr": proc.stderr, "output_rows": len(rows),
        "output_path": str(out),
        "output_sha256": hashlib.sha256(out.read_bytes()).hexdigest() if out.is_file() else None,
    })
    return proc, rows, out

@pytest.fixture
def unknown_cli(unknown_paths, tmp_path, request):
    def invoke(data, kind):
        return unknown_invoke(unknown_paths, data, kind, tmp_path / ("outputs_" + kind),
                              request.node.nodeid)
    return invoke

@pytest.fixture(scope="session")
def unknown_baseline(unknown_paths, tmp_path_factory):
    cached = {"ingestion": unknown_paths["baseline"] / "baseline_events.jsonl",
              "engine": unknown_paths["baseline"] / "baseline_decisions.jsonl"}
    if all(p.is_file() for p in cached.values()):
        return {kind: unknown_jsonl(path) for kind, path in cached.items()}
    root = tmp_path_factory.mktemp("unknown-clean-baseline")
    result = {}
    for kind in ("ingestion", "engine"):
        proc, rows, _ = unknown_invoke(unknown_paths, unknown_paths["data"], kind,
                                      root / kind, "session:unknown_baseline")
        unknown_require_success(proc)
        result[kind] = rows
    return result

def unknown_require_success(proc):
    assert proc.returncode == 0, proc.stderr[-6000:]
    assert "Traceback (most recent call last)" not in proc.stderr

def test_unknowns_optional_docs_and_samples_do_not_change_outputs(
        unknown_data, unknown_baseline, unknown_cli):
    """Organizer documentation/examples are optional, unlike the 17 inputs."""
    for name in ("README.md", "CHALLENGE.md", "DECISION_CONTRACT.md",
                 "decisions_sample.jsonl", "insights_sample.jsonl"):
        (unknown_data / name).unlink(missing_ok=True)
    for kind in ("ingestion", "engine"):
        proc, rows, out = unknown_cli(unknown_data, kind)
        unknown_require_success(proc)
        assert rows == unknown_baseline[kind]

@pytest.mark.parametrize("filename", [
    "bookings.csv", "guest_requests.csv", "staff_roster.csv", "work_orders.csv",
    "weather_forecast.csv", "tariff.csv",
])
def test_unknowns_missing_relational_gateway_inputs_are_rejected_by_full_preflight(
        unknown_data, unknown_cli, filename):
    """Gateway tolerance does not make a required submission file optional."""
    (unknown_data / filename).unlink()
    proc, _, _ = unknown_cli(unknown_data, "ingestion")
    unknown_require_success(proc)
    proc, _, out = unknown_cli(unknown_data, "check")
    assert proc.returncode == 2
    assert filename in proc.stderr and "Missing dataset files" in proc.stderr
    assert "Traceback" not in proc.stderr
    assert not out.exists()

@pytest.mark.parametrize("family", ["discrete", "observations"])
def test_unknowns_valid_empty_streams_remain_unknown_and_pipeline_completes(
        unknown_data, unknown_cli, family):
    from ingestion.replay import ReplayData

    if family == "discrete":
        names = ["bookings.csv", "guest_requests.csv", "staff_roster.csv",
                 "work_orders.csv", "weather_forecast.csv", "tariff.csv"]
        (unknown_data / "events.jsonl").write_text("", encoding="utf-8")
    else:
        names = ["access_log.csv", "room_telemetry.csv", "room_water.csv",
                 "water_main.csv", "energy.csv", "weather.csv", "equipment.csv",
                 "logistics.csv"]
    for name in names:
        unknown_header_only(unknown_data / name)
    proc, events, _ = unknown_cli(unknown_data, "ingestion")
    unknown_require_success(proc)
    repo = ReplayData(unknown_data, use_llm=False)
    state = repo.get_room_state("R102", "2026-01-05T00:15:00Z")
    if family == "discrete":
        assert len(events) == 2016
        assert all(row["event_type"] == "clock_tick" for row in events)
        assert state["occupancy"] == "unknown"
    else:
        assert state["last_observed_utc"] is None
        assert state["temperature_c"] is None and state["housekeeping_state"] is None
        assert state["occupancy"] == "occupied"  # observed stay, independent of telemetry
    proc, decisions, _ = unknown_cli(unknown_data, "engine")
    unknown_require_success(proc)
    if family == "discrete":
        # Equipment performance can still be assessed from retained measurements.
        # Missing lifecycle observations must not fabricate room occupancy/vacancy.
        room_ids = set(repo.ds.room_ids)
        assert all(not room_ids.intersection(d["affected_assets"]) for d in decisions)
        assert all(d["issue_type"] not in {"room_turnaround", "heating_comfort"}
                   for d in decisions)
    else:
        assert all(d["action"] in {"notify_guest", "defer_action"} for d in decisions)

def test_unknowns_future_values_preserved_without_future_outcome_or_staff_availability(
        unknown_data, unknown_cli):
    from ingestion.replay import ReplayData

    known = "2026-01-05T00:15:00Z"
    completed = "2026-01-05T00:30:00Z"
    unknown_append_csv(unknown_data / "guest_requests.csv", [{
        "request_id": "FUZZ_UNKNOWN_GUEST", "timestamp_utc": known, "room_id": "R102",
        "booking_id": "BK000014", "channel": "future_channel", "language": "zu-ZA",
        "request_type": "baggage_drone_vnext", "text": "Ngicela usizo — 未知の依頼",
        "priority": "informational", "response_due_utc": "", "requires_guest_consent": "undetermined",
    }])
    raw = {"event_id": "FUZZ_UNKNOWN_EVENT", "timestamp_utc": known,
           "type": "checkout_pending_review_vnext", "room_id": "R102",
           "booking_id": "BK000014", "proposed_occupancy": "vacant",
           "details": {"unrecognized_source_field": ["preserve", None, 7]}}
    with (unknown_data / "events.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(raw, ensure_ascii=False) + "\n")
    unknown_append_csv(unknown_data / "bookings.csv", [{
        "booking_id": "FUZZ_UNKNOWN_BOOKING", "room_id": "R101", "created_utc": known,
        "arrival_utc": "2026-01-06T14:00:00Z", "departure_utc": "2026-01-07T10:00:00Z",
        "guest_count": "1", "channel": "future_channel", "status": "awaiting_validation_vnext",
    }])
    unknown_append_csv(unknown_data / "work_orders.csv", [{
        "work_order_id": "FUZZ_UNKNOWN_ORDER", "created_utc": known, "asset_id": "HP1",
        "type": "sensor_calibration_vnext", "status": "resolved_with_followup_vnext",
        "scheduled_utc": known, "completed_utc": completed, "assigned_to": "TECH_1",
        "summary": "FUTURE_OUTCOME_SENTINEL",
    }])
    building_path = unknown_data / "building.json"
    building = json.loads(building_path.read_text(encoding="utf-8"))
    building["staff"]["resources"].extend([
        {"resource_id": "FUZZ_UNKNOWN_STATUS", "role": "cleaner"},
        {"resource_id": "FUZZ_UNKNOWN_ROLE", "role": "concierge_vnext"},
    ])
    building_path.write_text(json.dumps(building), encoding="utf-8")
    unknown_append_csv(unknown_data / "staff_roster.csv", [
        {"shift_id": "FUZZ_UNKNOWN_STATUS", "resource_id": "FUZZ_UNKNOWN_STATUS",
         "role": "cleaner", "start_utc": known, "end_utc": "2026-01-05T20:00:00Z",
         "status": "training_vnext", "status_known_utc": known},
        {"shift_id": "FUZZ_UNKNOWN_ROLE", "resource_id": "FUZZ_UNKNOWN_ROLE",
         "role": "concierge_vnext", "start_utc": known, "end_utc": "2026-01-05T20:00:00Z",
         "status": "scheduled", "status_known_utc": known},
    ])
    proc, events, _ = unknown_cli(unknown_data, "ingestion")
    unknown_require_success(proc)
    by_id = {e["event_id"]: e for e in events}
    guest = by_id["guest:FUZZ_UNKNOWN_GUEST"]
    assert guest["payload"]["request_type"] == "baggage_drone_vnext"
    assert guest["payload"]["language"] == "zu-ZA"
    assert guest["payload"]["source_priority"] == "informational"
    assert guest["priority_hint"] is None
    assert guest["extraction"]["needs_review"] is True
    assert guest["payload"]["intents"][0]["intent"] == "unknown"
    assert guest["payload"]["intents"][0]["requested_time_utc"] is None
    assert by_id["raw:FUZZ_UNKNOWN_EVENT"]["payload"] == raw
    assert by_id["booking:FUZZ_UNKNOWN_BOOKING"]["payload"]["status"] == "unresolved"
    assert by_id["wo:FUZZ_UNKNOWN_ORDER"]["payload"]["summary"] == ""
    assert by_id["wo:FUZZ_UNKNOWN_ORDER"]["payload"]["status"] == "open"
    resolved = by_id["wo:FUZZ_UNKNOWN_ORDER:resolved"]
    assert resolved["available_at_utc"] == completed
    assert resolved["payload"]["summary"] == "FUTURE_OUTCOME_SENTINEL"
    repo = ReplayData(unknown_data, use_llm=False)
    assert repo.get_room_state("R102", known)["occupancy"] == "occupied"
    slots = {r["shift_id"]: r for r in repo.get_staff_slots(known, completed, known)}
    assert slots["FUZZ_UNKNOWN_STATUS"]["status"] == "training_vnext"
    assert slots["FUZZ_UNKNOWN_STATUS"]["effective_status_asof"] == "unknown"
    assert slots["FUZZ_UNKNOWN_ROLE"]["role"] == "concierge_vnext"
    proc, decisions, _ = unknown_cli(unknown_data, "engine")
    unknown_require_success(proc)
    assert not any(d.get("resource_id", "").startswith("FUZZ_UNKNOWN_")
                   for d in decisions if d.get("resource_id"))

def test_unknowns_undeclared_room_remains_traceable_but_is_not_actioned(
        unknown_data, unknown_cli):
    from ingestion.replay import ReplayData

    unknown_append_csv(unknown_data / "guest_requests.csv", [{
        "request_id": "FUZZ_UNDECLARED_ROOM", "timestamp_utc": "2026-01-05T09:00:00Z",
        "room_id": "R999", "booking_id": "", "channel": "app_chat", "language": "en",
        "request_type": "waiting_for_room", "text": "I am waiting for my room.",
        "priority": "high", "response_due_utc": "", "requires_guest_consent": "not_required",
    }])
    proc, events, _ = unknown_cli(unknown_data, "ingestion")
    unknown_require_success(proc)
    row = next(e for e in events if e["event_id"] == "guest:FUZZ_UNDECLARED_ROOM")
    assert row["entities"]["room_id"] == "R999"
    assert row["source"]["record_id"] == "FUZZ_UNDECLARED_ROOM"
    assert ReplayData(unknown_data, use_llm=False).get_room_state("R999", row["available_at_utc"]) is None
    proc, decisions, out = unknown_cli(unknown_data, "engine")
    unknown_require_success(proc)
    assert all("R999" not in d["affected_assets"] for d in decisions)
    rejected = unknown_jsonl(out.with_name("decisions_rejections.jsonl"))
    assert any(r.get("reason") == "Unknown candidate asset" for r in rejected)

@pytest.mark.xfail(strict=True, reason="Zero-byte required CSVs are tolerated as empty "
                   "streams because the engine owner's fixtures (tests/engine/test_cli.py) "
                   "use zero-byte placeholders with inspect_dataset. They are reported in the "
                   "audit ('<file>:empty_no_header') and logged; rejecting them in run.py "
                   "preflight is the engine owner's decision. Headers that lack required "
                   "columns ARE rejected.")
@pytest.mark.parametrize("filename", ["guest_requests.csv", "room_telemetry.csv"])
def test_unknowns_headerless_required_csv_is_not_silently_accepted(
        unknown_data, unknown_cli, filename):
    """Malformed required schema: a zero-byte CSV has no required header.

    A valid zero-row CSV is tested separately above. Strict rejection is needed
    here to distinguish missing/corrupt input from an intentionally empty stream.
    Both actual CLIs are exercised before the regression assertion.
    """
    (unknown_data / filename).write_bytes(b"")
    ingestion, _, _ = unknown_cli(unknown_data, "ingestion")
    preflight, _, _ = unknown_cli(unknown_data, "check")
    engine, decisions, _ = unknown_cli(unknown_data, "engine")
    assert preflight.returncode != 0 and engine.returncode != 0, (
        f"Headerless required {filename} was accepted: ingestion={ingestion.returncode}, "
        f"preflight={preflight.returncode}, engine={engine.returncode}, "
        f"decisions={len(decisions)}. Header-only controls are valid; no header is schema loss."
    )
