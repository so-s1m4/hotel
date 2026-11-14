"""Regressions for public event snapshots and roster publication bounds."""
from __future__ import annotations

import csv
import json

from ingestion.replay import ReplayData


START = "2026-01-05T00:00:00Z"
END = "2026-01-05T02:00:00Z"
BEFORE_LEAD = "2025-12-22T09:59:59Z"
LEAD = "2025-12-22T10:00:00Z"
KNOWN = "2025-12-20T00:00:00Z"


def _dataset(tmp_path, *, roster_rows=()):
    root = tmp_path / "fixture"
    root.mkdir()
    (root / "manifest.json").write_text(json.dumps({
        "model_id": "PLATO-40", "profile": "test", "synthetic": True,
        "start_utc": START, "end_utc": END, "interval_seconds": 900,
    }))
    (root / "building.json").write_text(json.dumps({
        "schema_version": "2.0", "model_id": "PLATO-40", "profile": "test",
        "time": {"data_timezone": "UTC", "local_timezone": "UTC", "interval_seconds": 900},
        "building": {"rooms": [{"id": "R101"}], "assets": []},
        "staff": {"resources": [{"resource_id": "C1", "role": "cleaner"}]},
    }))
    headers = {
        "bookings.csv": "booking_id,room_id,created_utc,arrival_utc,departure_utc\n",
        "access_log.csv": "timestamp_utc,door_id\n",
        "room_telemetry.csv": "timestamp_utc,room_id\n",
        "room_water.csv": "timestamp_utc,room_id,meter_total_l\n",
        "water_main.csv": "timestamp_utc,meter_total_l\n",
        "energy.csv": "timestamp_utc\n", "weather.csv": "timestamp_utc\n",
        "weather_forecast.csv": "issued_utc,target_utc\n",
        "tariff.csv": "timestamp_utc,published_utc\n",
        "equipment.csv": "timestamp_utc,asset_id,metric,value\n",
        "logistics.csv": "timestamp_utc,asset_id,metric,value\n",
        "work_orders.csv": "work_order_id,created_utc\n",
        "guest_requests.csv": "request_id,timestamp_utc,request_type,text\n",
    }
    for filename, header in headers.items():
        (root / filename).write_text(header)
    roster_header = "shift_id,resource_id,role,start_utc,end_utc,status,status_known_utc\n"
    with (root / "staff_roster.csv").open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=roster_header.strip().split(","))
        writer.writeheader()
        writer.writerows(roster_rows)
    (root / "events.jsonl").write_text(json.dumps({
        "event_id": "E1", "timestamp_utc": START, "type": "bms_alarm", "room_id": "R101",
        "details": {"nested": ["source"]},
        "source_fields": {"vendor": {"flags": ["source"]}},
    }) + "\n")
    return root


def _window(repo):
    return repo.get_events(START, END, END)


def test_raw_event_queries_return_deep_independent_snapshots(tmp_path):
    repo = ReplayData(_dataset(tmp_path), use_llm=False, include_ticks=False)
    first = _window(repo)[0]
    first["details"]["nested"].append("caller")
    first["source_fields"]["vendor"]["flags"].append("caller")

    second = _window(repo)[0]
    assert second["details"]["nested"] == ["source"]
    assert second["source_fields"]["vendor"]["flags"] == ["source"]
    assert repo.ds.raw_events[0]["details"]["nested"] == ["source"]
    assert repo.ds.raw_events[0]["source_fields"]["vendor"]["flags"] == ["source"]

    available = repo.get_events_available_between(START, END, END)[0]
    available["details"]["nested"].append("incremental-caller")
    assert repo.get_events_available_between(START, END, END)[0]["details"]["nested"] == ["source"]


def test_normalized_event_iterations_are_independent_of_each_other_and_raw_cache(tmp_path):
    repo = ReplayData(_dataset(tmp_path), use_llm=False, include_ticks=False)
    first = next(event for event in repo.iter_events() if event.event_id == "raw:E1")
    first.payload["details"]["nested"].append("consumer")
    first.payload["source_fields"]["vendor"]["flags"].append("consumer")

    second = next(event for event in repo.iter_events() if event.event_id == "raw:E1")
    assert second is not first
    assert second.payload["details"]["nested"] == ["source"]
    assert second.payload["source_fields"]["vendor"]["flags"] == ["source"]
    assert repo.ds.raw_events[0]["details"]["nested"] == ["source"]


def test_scheduled_roster_without_publication_time_is_never_counted(tmp_path):
    row = {"shift_id": "S_UNKNOWN", "resource_id": "C1", "role": "cleaner",
           "start_utc": "2026-01-05T10:00:00Z", "end_utc": "2026-01-05T18:00:00Z",
           "status": "scheduled", "status_known_utc": ""}
    repo = ReplayData(_dataset(tmp_path, roster_rows=[row]), use_llm=False, include_ticks=False)
    for as_of in ("2025-12-01T00:00:00Z", BEFORE_LEAD, LEAD, "2026-01-05T12:00:00Z"):
        assert repo.get_staff_slots("2026-01-05T10:00:00Z", "2026-01-05T18:00:00Z",
                                    as_of, role="cleaner") == []
    assert repo.ds.roster[0]["status_known_utc"] is None
    assert (repo.data_dir / "staff_roster.csv").read_text().endswith(",scheduled,\n")


def test_scheduled_roster_exact_known_cutoff_is_visible(tmp_path):
    row = {"shift_id": "S_KNOWN", "resource_id": "C1", "role": "cleaner",
           "start_utc": "2026-01-05T10:00:00Z", "end_utc": "2026-01-05T18:00:00Z",
           "status": "scheduled", "status_known_utc": KNOWN}
    repo = ReplayData(_dataset(tmp_path, roster_rows=[row]), use_llm=False, include_ticks=False)
    args = ("2026-01-05T10:00:00Z", "2026-01-05T18:00:00Z")
    assert repo.get_staff_slots(*args, "2025-12-19T23:59:59Z", role="cleaner") == []
    visible = repo.get_staff_slots(*args, KNOWN, role="cleaner")
    assert len(visible) == 1 and visible[0]["shift_id"] == "S_KNOWN"
    assert visible[0]["status_known_utc"] == KNOWN
    assert visible[0]["effective_status_asof"] == "scheduled"


def test_absence_overwrite_remains_hidden_until_exact_status_knowledge(tmp_path):
    row = {"shift_id": "S_SICK", "resource_id": "C1", "role": "cleaner",
           "start_utc": "2026-01-05T10:00:00Z", "end_utc": "2026-01-05T18:00:00Z",
           "status": "sick", "status_known_utc": "2026-01-05T12:00:00Z"}
    repo = ReplayData(_dataset(tmp_path, roster_rows=[row]), use_llm=False, include_ticks=False)
    args = ("2026-01-05T10:00:00Z", "2026-01-05T18:00:00Z")
    before = repo.get_staff_slots(*args, BEFORE_LEAD, role="cleaner")
    assert before == []
    at_lead = repo.get_staff_slots(*args, LEAD, role="cleaner")
    assert len(at_lead) == 1 and at_lead[0]["status"] == "scheduled"
    assert at_lead[0]["status_known_utc"] == LEAD
    after = repo.get_staff_slots(*args, "2026-01-05T11:59:59Z", role="cleaner")
    assert after[0]["status"] == "scheduled"
    known = repo.get_staff_slots(*args, "2026-01-05T12:00:00Z", role="cleaner")
    assert known[0]["status"] == "sick"
    assert known[0]["effective_status_asof"] == "sick"
    assert known[0]["status_known_utc"] == "2026-01-05T12:00:00Z"
