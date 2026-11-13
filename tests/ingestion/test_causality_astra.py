"""Independent, contract-based adversarial tests for historical replay.

The tiny synthetic dataset deliberately includes edge cases absent from the
provided snapshots.  All assertions use the frozen public API; no production
implementation is imported or patched at collection time.
"""
from __future__ import annotations

import csv
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from common.contracts.schemas import NormalizedEventV1


START = "2026-01-05T00:00:00Z"
END = "2026-01-05T02:00:00Z"
BEFORE = "2026-01-05T00:29:59Z"
CUTOFF = "2026-01-05T00:30:00Z"
LATER = "2026-01-05T00:45:00Z"
PREVIOUS_DAY = "2026-01-04T00:00:00Z"

HEADERS = {
    "bookings.csv": "booking_id,room_id,created_utc,arrival_utc,departure_utc,guest_count,channel,status",
    "access_log.csv": "timestamp_utc,door_id,credential,result",
    "room_telemetry.csv": "timestamp_utc,room_id,temperature_c,setpoint_c,humidity_pct,co2_ppm,presence,window_open,housekeeping_state",
    "room_water.csv": "timestamp_utc,room_id,meter_total_l",
    "water_main.csv": "timestamp_utc,meter_total_l",
    "energy.csv": "timestamp_utc,building_load_kw,hvac_electric_kw,pv_kw,battery_discharge_kw_signed,battery_soc_kwh,grid_import_kw,grid_export_kw",
    "equipment.csv": "timestamp_utc,asset_id,metric,value",
    "logistics.csv": "timestamp_utc,asset_id,metric,value",
    "weather.csv": "timestamp_utc,outdoor_c,cloud_cover_pct",
    "weather_forecast.csv": "issued_utc,target_utc,outdoor_c_forecast",
    "tariff.csv": "timestamp_utc,price_eur_kwh,published_utc",
    "staff_roster.csv": "shift_id,resource_id,role,start_utc,end_utc,status,status_known_utc",
    "work_orders.csv": "work_order_id,created_utc,asset_id,type,status,scheduled_utc,completed_utc,assigned_to,summary",
    "guest_requests.csv": "request_id,timestamp_utc,room_id,booking_id,channel,language,request_type,text,priority,response_due_utc,requires_guest_consent",
}


def telemetry(timestamp, room="R101", temperature=21, presence=0):
    return dict(timestamp_utc=timestamp, room_id=room, temperature_c=temperature,
                setpoint_c=21, humidity_pct=45, co2_ppm=650, presence=presence,
                window_open=0, housekeeping_state="clean")


def booking(identifier, room="R101", created=PREVIOUS_DAY, status="confirmed"):
    return dict(booking_id=identifier, room_id=room, created_utc=created,
                arrival_utc=PREVIOUS_DAY, departure_utc="2026-01-06T10:00:00Z",
                guest_count=1, channel="direct", status=status)


def default_rows():
    return {
        "room_telemetry.csv": [
            telemetry(START), telemetry("2026-01-05T00:15:00Z", temperature=""),
            telemetry(CUTOFF, temperature=22), telemetry(CUTOFF, temperature=22),
            telemetry(LATER, temperature=99), telemetry(START, room="R102"),
            telemetry(START, room="R103"), telemetry(START, room="R104"),
        ],
        "bookings.csv": [booking("BK_PRESENT"),
                         booking("BK_CANCELLED", status="cancelled"),
                         booking("BK_FUTURE", created=LATER),
                         booking("BK_PLAN_ONLY", room="R103")],
        "staff_roster.csv": [
            dict(shift_id="SH_SICK", resource_id="CLEANER_1", role="cleaner",
                 start_utc=START, end_utc=END, status="sick", status_known_utc=CUTOFF),
            dict(shift_id="SH_READY", resource_id="ROBOT_1", role="robot",
                 start_utc=START, end_utc=END, status="scheduled", status_known_utc=PREVIOUS_DAY),
        ],
        "work_orders.csv": [
            dict(work_order_id="WO_FUTURE_DONE", created_utc=START, asset_id="HP1",
                 type="corrective", status="completed", scheduled_utc=LATER,
                 completed_utc="2026-01-05T01:00:00Z", assigned_to="TECH_1",
                 summary="Heat pump service"),
        ],
        "weather.csv": [
            dict(timestamp_utc=START, outdoor_c=-1, cloud_cover_pct=20),
            dict(timestamp_utc=CUTOFF, outdoor_c="", cloud_cover_pct=30),
            dict(timestamp_utc=LATER, outdoor_c=88, cloud_cover_pct=99),
        ],
        "weather_forecast.csv": [
            dict(issued_utc=START, target_utc="2026-01-05T01:00:00Z", outdoor_c_forecast=-1),
            dict(issued_utc=CUTOFF, target_utc="2026-01-05T01:00:00Z", outdoor_c_forecast=-2),
            dict(issued_utc=LATER, target_utc="2026-01-05T01:00:00Z", outdoor_c_forecast=99),
            dict(issued_utc=START, target_utc="2026-01-05T01:30:00Z", outdoor_c_forecast=-3),
        ],
        "tariff.csv": [
            dict(timestamp_utc="2026-01-05T01:00:00Z", price_eur_kwh=0.2, published_utc=CUTOFF),
            dict(timestamp_utc="2026-01-05T01:30:00Z", price_eur_kwh=0.9, published_utc=LATER),
        ],
        "equipment.csv": [
            dict(timestamp_utc=START, asset_id="HP1", metric="electric_kw", value=3),
            dict(timestamp_utc=CUTOFF, asset_id="HP1", metric="electric_kw", value=4),
            dict(timestamp_utc=CUTOFF, asset_id="HP1", metric="status", value="running"),
            dict(timestamp_utc=CUTOFF, asset_id="AHU1", metric="electric_kw", value=8),
            dict(timestamp_utc=LATER, asset_id="HP1", metric="electric_kw", value=99),
        ],
        "room_water.csv": [
            dict(timestamp_utc=START, room_id="R101", meter_total_l=100),
            dict(timestamp_utc=CUTOFF, room_id="R101", meter_total_l=""),
            dict(timestamp_utc=LATER, room_id="R101", meter_total_l=4),
        ],
        "guest_requests.csv": [
            dict(request_id="GR_FUTURE_BOOKING", timestamp_utc=CUTOFF, room_id="R101",
                 booking_id="BK_FUTURE", channel="chat", language="de",
                 request_type="early_checkin", text="Können wir schon um 13:00 einchecken?",
                 priority="medium", response_due_utc=END, requires_guest_consent="not_required"),
        ],
    }


def default_events():
    return [
        dict(event_id="E_IN", timestamp_utc=START, type="checkin",
             room_id="R101", booking_id="BK_PRESENT", guests=1),
        dict(event_id="E_ALARM", timestamp_utc=CUTOFF, type="bms_alarm",
             asset_id="HP1", alarm_code="ROOM_TEMP_LOW", state="raised", value=17),
        dict(event_id="E_OUT", timestamp_utc="2026-01-05T01:00:00Z", type="checkout",
             room_id="R101", booking_id="BK_PRESENT"),
    ]


@pytest.fixture
def make_dataset(tmp_path, monkeypatch):
    # Public tests must always work without an LLM service or credentials.
    for key in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "LLM_API_KEY",
                "INGESTION_LLM_API_KEY", "GUEST_PARSER_API_KEY",
                "OPENROUTER_API_KEY", "CEREBRAS_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("GUEST_PARSER_OFFLINE", "1")
    monkeypatch.setenv("GUEST_PARSER_CACHE_DIR", str(tmp_path / "parser_cache"))
    counter = 0

    def make(overrides=None, events=None):
        nonlocal counter
        counter += 1
        path = tmp_path / f"causal_fixture_{counter}"
        path.mkdir()
        rows = default_rows()
        rows.update(overrides or {})
        for filename, header in HEADERS.items():
            with (path / filename).open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=header.split(","))
                writer.writeheader()
                writer.writerows(rows.get(filename, []))
        (path / "manifest.json").write_text(json.dumps({
            "model_id": "PLATO-40", "dataset_version": "2.0.0-dev",
            "profile": "causality-test", "synthetic": True,
            "start_utc": START, "end_utc": END, "interval_seconds": 900,
        }), encoding="utf-8")
        (path / "building.json").write_text(json.dumps({
            "schema_version": "2.0", "model_id": "PLATO-40", "profile": "causality-test",
            "time": {"data_timezone": "UTC", "local_timezone": "Europe/Vienna", "interval_seconds": 900},
            "building": {"rooms": [{"id": f"R10{i}", "floor": 1, "max_guests": 2}
                                     for i in range(1, 5)],
                         "assets": [{"id": "HP1", "type": "heat_pump"},
                                    {"id": "AHU1", "type": "air_handling_unit"}]},
            "staff": {"resources": [{"resource_id": "CLEANER_1", "role": "cleaner"},
                                    {"resource_id": "TECH_1", "role": "technician"},
                                    {"resource_id": "ROBOT_1", "role": "robot"}]},
            "operations": {"check_in_from_local": "15:00", "check_out_until_local": "11:00"},
            "constraints": {"occupied_comfort_c": [20, 23], "room_release_requires":
                            ["vacant", "clean", "not_blocked", "inspection_passed_if_required"]},
        }), encoding="utf-8")
        (path / "events.jsonl").write_text("".join(
            json.dumps(row) + "\n" for row in (default_events() if events is None else events)
        ), encoding="utf-8")
        return path

    return make


@pytest.fixture
def repo(make_dataset):
    from ingestion.replay import ReplayData
    return ReplayData(make_dataset())


def test_room_state_uses_latest_observation_no_later_than_cutoff(repo):
    before = repo.get_room_state("R101", BEFORE)
    assert before["last_observed_utc"] == "2026-01-05T00:15:00Z"
    assert before["temperature_c"] is None, "Missing latest reading must not become zero or future data"
    at = repo.get_room_state("R101", CUTOFF)
    assert at["last_observed_utc"] == CUTOFF
    assert float(at["temperature_c"]) == 22
    assert at["as_of_utc"] == CUTOFF


def test_motion_zero_and_clean_room_do_not_cancel_observed_stay(repo):
    state = repo.get_room_state("R101", CUTOFF)
    assert state["presence"] in (0, "0", False)
    assert state["housekeeping_state"] == "clean"
    assert state["occupancy"] == "occupied"
    assert state["current_booking_id"] == "BK_PRESENT"
    assert state.get("released") is not True


def test_booking_plan_without_checkin_does_not_establish_occupancy(repo):
    assert repo.get_room_state("R103", CUTOFF)["occupancy"] == "unknown"
    assert repo.get_room_state("R102", CUTOFF)["occupancy"] == "unknown"


def test_checkout_applies_at_exact_event_time_only(repo):
    assert repo.get_room_state("R101", "2026-01-05T00:59:59Z")["occupancy"] == "occupied"
    after = repo.get_room_state("R101", "2026-01-05T01:00:00Z")
    assert after["occupancy"] == "vacant"
    assert after["current_booking_id"] is None


def test_planned_departure_does_not_replace_observed_checkout(make_dataset):
    from ingestion.replay import ReplayData
    planned = booking("BK_PRESENT")
    planned["departure_utc"] = "2026-01-05T00:15:00Z"
    repo = ReplayData(make_dataset(overrides={"bookings.csv": [planned]}))
    assert repo.get_room_state("R101", CUTOFF)["occupancy"] == "occupied"
    assert repo.get_room_state("R101", "2026-01-05T01:00:00Z")["occupancy"] == "vacant"


def test_unknown_room_is_not_fabricated(repo):
    state = repo.get_room_state("NOT_A_ROOM", CUTOFF)
    # The contract permits None and does not prescribe an unknown-ID exception.
    # An explicit unknown state is also acceptable, but measured facts are not.
    if state is not None:
        assert state["occupancy"] == "unknown"
        assert state["last_observed_utc"] is None
        assert state["temperature_c"] is None
        assert state["presence"] is None
        assert state["current_booking_id"] is None


def test_measurement_range_inclusive_asof_bound_and_deduplicated(repo):
    rows = repo.get_measurements("room_telemetry.csv", START, END, CUTOFF, entity_id="R101")
    assert sorted(r["timestamp_utc"] for r in rows) == [START, "2026-01-05T00:15:00Z", CUTOFF]
    assert all(r["room_id"] == "R101" for r in rows)
    exact = repo.get_measurements("room_telemetry.csv", CUTOFF, CUTOFF, CUTOFF, entity_id="R101")
    assert len(exact) == 1 and exact[0]["timestamp_utc"] == CUTOFF


def test_measurements_empty_for_wholly_future_interval(repo):
    assert repo.get_measurements("room_telemetry.csv", LATER, END, CUTOFF, entity_id="R101") == []


def test_equipment_filter_preserves_source_fields(repo):
    rows = repo.get_measurements("equipment.csv", START, END, CUTOFF,
                                 entity_id="HP1", metric="electric_kw")
    assert sorted(float(r["value"]) for r in rows) == [3, 4]
    assert all(r["asset_id"] == "HP1" and r["metric"] == "electric_kw" for r in rows)
    assert all(set(HEADERS["equipment.csv"].split(",")) <= r.keys() for r in rows)


def test_future_actual_weather_is_hidden_and_missing_stays_null(repo):
    rows = repo.get_measurements("weather.csv", START, END, CUTOFF)
    assert sorted(r["timestamp_utc"] for r in rows) == [START, CUTOFF]
    assert next(r for r in rows if r["timestamp_utc"] == CUTOFF)["outdoor_c"] is None


def test_water_meter_reset_is_preserved_as_raw_counter_not_rewritten(repo):
    rows = repo.get_measurements("room_water.csv", START, END, LATER, entity_id="R101")
    by_time = sorted(rows, key=lambda r: r["timestamp_utc"])
    assert [None if r["meter_total_l"] is None else float(r["meter_total_l"]) for r in by_time] == [100, None, 4]


def test_booking_creation_is_inclusive_and_unknown_booking_is_none(repo):
    assert repo.get_booking("BK_FUTURE", CUTOFF) is None
    assert repo.get_booking("BK_FUTURE", LATER)["booking_id"] == "BK_FUTURE"
    assert repo.get_booking("DOES_NOT_EXIST", END) is None


def test_cancelled_final_status_not_presented_as_known_at_creation(repo):
    row = repo.get_booking("BK_CANCELLED", START)
    assert row is not None, "A final cancellation cannot silently erase a historical booking"
    assert row.get("status") != "cancelled", "No timestamp exists to make cancellation historically known"


def test_room_bookings_keep_unresolved_cancellation_and_hide_uncreated_booking(repo):
    rows = repo.get_bookings_for_room("R101", START, END, CUTOFF)
    ids = {r["booking_id"] for r in rows}
    assert {"BK_PRESENT", "BK_CANCELLED"} <= ids
    assert "BK_FUTURE" not in ids
    assert all(r["room_id"] == "R101" for r in rows)


def test_sick_shift_effective_status_changes_only_when_known(repo):
    before = repo.get_staff_slots(START, END, BEFORE, role="cleaner")
    at = repo.get_staff_slots(START, END, CUTOFF, role="cleaner")
    assert len(before) == len(at) == 1
    assert before[0]["effective_status_asof"] in {"scheduled", "unknown"}
    assert at[0]["effective_status_asof"] == "sick"
    # Preserve field names without exposing the time of an unobserved change.
    assert set(HEADERS["staff_roster.csv"].split(",")) <= before[0].keys()
    assert before[0]["status_known_utc"] is None or before[0]["status_known_utc"] <= BEFORE
    assert before[0]["status"] in {"scheduled", "unknown", None}
    assert at[0]["status_known_utc"] == CUTOFF
    assert at[0]["status"] == "sick"


def test_future_absence_does_not_change_past_roster_view_or_event_prefix(make_dataset):
    from ingestion.replay import ReplayData
    # Actual normal rows in both archives use start minus 14 days. L1 models
    # overwritten plans with that same uniform publication assumption.
    published = "2025-12-22T00:00:00Z"
    scheduled = dict(shift_id="SH_COUNTERFACTUAL", resource_id="CLEANER_1",
                     role="cleaner", start_utc=START, end_utc=END,
                     status="scheduled", status_known_utc=published)
    later_sick = {**scheduled, "status": "sick", "status_known_utc": CUTOFF}
    normal_repo = ReplayData(make_dataset(overrides={"staff_roster.csv": [scheduled]}))
    sick_repo = ReplayData(make_dataset(overrides={"staff_roster.csv": [later_sick]}))
    normal_view = normal_repo.get_staff_slots(START, END, BEFORE, role="cleaner")
    sick_view = sick_repo.get_staff_slots(START, END, BEFORE, role="cleaner")
    assert normal_view == sick_view, "Past getter output must not identify a future absence"

    def past_roster(repo):
        return [event.model_dump(mode="json") for event in repo.iter_events()
                if event.source.file == "staff_roster.csv" and event.available_at_utc <= BEFORE]

    normal_events, sick_events = past_roster(normal_repo), past_roster(sick_repo)
    assert len(normal_events) == len(sick_events) == 1
    assert normal_events == sick_events, (
        "Past event provenance, including assumed_publication markers, must not identify future sickness"
    )
    assert sick_repo.get_staff_slots(START, END, CUTOFF, role="cleaner")[0]["status"] == "sick"
    assert normal_repo.get_staff_slots(START, END, CUTOFF, role="cleaner")[0]["status"] == "scheduled"


def test_known_future_roster_schedule_can_be_queried(repo):
    rows = repo.get_staff_slots(START, END, PREVIOUS_DAY, role="robot")
    assert len(rows) == 1
    assert rows[0]["effective_status_asof"] == "scheduled"


def test_forecast_selects_latest_known_issue_for_each_future_target(repo):
    before = repo.get_weather_forecast(BEFORE, END)
    at = repo.get_weather_forecast(CUTOFF, END)
    target = "2026-01-05T01:00:00Z"
    before_target = [r for r in before if r["target_utc"] == target]
    at_target = [r for r in at if r["target_utc"] == target]
    assert len(before_target) == len(at_target) == 1
    assert before_target[0]["issued_utc"] == START
    assert at_target[0]["issued_utc"] == CUTOFF
    assert float(at_target[0]["outdoor_c_forecast"]) == -2
    assert all(r["issued_utc"] <= CUTOFF and r["target_utc"] <= END for r in at)


def test_forecast_before_first_issue_is_empty(repo):
    assert repo.get_weather_forecast(PREVIOUS_DAY, END) == []


def test_tariff_published_at_cutoff_is_visible_for_future_hour(repo):
    assert repo.get_tariffs(BEFORE, END) == []
    at = repo.get_tariffs(CUTOFF, END)
    assert len(at) == 1
    assert at[0]["timestamp_utc"] == "2026-01-05T01:00:00Z"
    assert at[0]["published_utc"] == CUTOFF
    assert float(at[0]["price_eur_kwh"]) == 0.2


def test_missing_tariff_publication_is_unknown_through_both_access_paths(make_dataset):
    from ingestion.replay import ReplayData
    unknown_price = dict(timestamp_utc=START, price_eur_kwh=0.7, published_utc="")
    repo = ReplayData(make_dataset(overrides={"tariff.csv": [unknown_price]}))
    assert repo.get_tariffs(CUTOFF, END) == []
    assert repo.get_measurements("tariff.csv", START, END, CUTOFF) == [], (
        "Missing publication time cannot fall back to the price's target time"
    )


def test_raw_events_cutoff_is_inclusive_and_future_checkout_hidden(repo):
    before = repo.get_events(START, END, BEFORE)
    at = repo.get_events(START, END, CUTOFF)
    assert {r["event_id"] for r in before} == {"E_IN"}
    assert {r["event_id"] for r in at} == {"E_IN", "E_ALARM"}
    filtered = repo.get_events(START, END, END, room_id="R101", event_type="checkout")
    assert [r["event_id"] for r in filtered] == ["E_OUT"]


def test_normalized_events_sorted_unique_and_repeatable(repo):
    first = list(repo.iter_events())
    second = list(repo.iter_events())
    assert all(isinstance(event, NormalizedEventV1) for event in first)
    keys = [(e.available_at_utc, e.event_id) for e in first]
    assert keys == sorted(keys)
    assert len({e.event_id for e in first}) == len(first)
    assert [e.model_dump() for e in first] == [e.model_dump() for e in second]
    assert not any(e.source.file == "room_telemetry.csv" for e in first)


def test_clock_ticks_cover_manifest_start_inclusive_end_exclusive(repo):
    events = list(repo.iter_events())
    ticks = [e for e in events if e.event_type == "clock_tick"]
    start = datetime(2026, 1, 5, tzinfo=timezone.utc)
    expected = [(start + timedelta(minutes=15 * n)).isoformat().replace("+00:00", "Z") for n in range(8)]
    assert [e.available_at_utc for e in ticks] == expected
    assert all(e.timestamp_utc == e.available_at_utc for e in ticks)
    assert all(e.source.file not in HEADERS and e.source.file != "events.jsonl" for e in ticks)


def test_work_order_creation_event_does_not_reveal_future_completion(repo):
    order = next(e for e in repo.iter_events() if e.source.record_id == "WO_FUTURE_DONE")
    assert order.event_type == "work_order_created"
    assert order.available_at_utc == START
    assert order.payload.get("completed_utc") is None
    assert order.payload.get("status") != "completed"
    assert order.payload.get("scheduled_utc") == LATER, "Known future plans are permitted"


def test_untimed_postponement_is_not_revealed_when_scheduled_time_passes(make_dataset):
    from ingestion.replay import ReplayData
    order = dict(work_order_id="WO_UNTIMED_POSTPONEMENT", created_utc=START,
                 asset_id="HP1", type="preventive", status="postponed",
                 scheduled_utc=LATER, completed_utc="", assigned_to="TECH_1",
                 summary="Heat-pump service postponed because the replacement part was unavailable")
    repo = ReplayData(make_dataset(overrides={"work_orders.csv": [order]}))
    events = [event for event in repo.iter_events()
              if event.source.file == "work_orders.csv"
              and event.source.record_id == order["work_order_id"]]
    assert len(events) == 1, "A planned slot is not evidence of a final-status update"
    assert events[0].event_type == "work_order_created"
    assert events[0].available_at_utc == START
    assert events[0].payload.get("status") != "postponed"
    assert not events[0].payload.get("summary")  # withheld (empty string, engine-safe)
    assert events[0].payload.get("completed_utc") is None

    # No work-order getter is required by the frozen API. If an implementation
    # offers one, it must not bypass the same knowledge boundary.
    getter = getattr(repo, "get_work_orders", None)
    if callable(getter):
        for as_of in (CUTOFF, LATER, END, "2026-01-15T00:00:00Z"):
            rows = getter(as_of)
            visible = next(row for row in rows if row["work_order_id"] == order["work_order_id"])
            assert visible.get("status") != "postponed"
            assert visible.get("status_asof") != "postponed"
            assert not visible.get("summary")
            assert visible.get("completed_utc") is None


def test_booking_creation_event_does_not_reveal_untimed_cancellation(repo):
    event = next(e for e in repo.iter_events() if e.source.record_id == "BK_CANCELLED")
    assert event.event_type == "booking_created"
    assert event.available_at_utc == PREVIOUS_DAY
    assert event.payload.get("status") != "cancelled"


def test_guest_wrapper_retains_source_identity_without_invented_booking_facts(repo):
    event = next(e for e in repo.iter_events() if e.event_id == "guest:GR_FUTURE_BOOKING")
    assert event.entities.booking_id == "BK_FUTURE", "The source reference itself is already known"
    assert event.source.file == "guest_requests.csv"
    assert event.source.record_id == "GR_FUTURE_BOOKING"
    assert event.timestamp_utc == event.available_at_utc == CUTOFF
    assert event.payload["request_type"] == "early_checkin"
    assert event.payload["requires_guest_consent"] == "not_required"
    assert event.extraction.method in {"source_label", "fallback"}
    assert all(intent.get("requested_time_utc") is None for intent in event.payload["intents"])
    assert repo.get_booking(event.entities.booking_id, event.available_at_utc) is None


def test_getters_return_copies_not_mutable_internal_records(repo):
    row = repo.get_booking("BK_PRESENT", CUTOFF)
    row["room_id"] = "CORRUPTED"
    assert repo.get_booking("BK_PRESENT", CUTOFF)["room_id"] == "R101"
    state = repo.get_room_state("R101", CUTOFF)
    state["evidence"].append("CORRUPTED")
    assert "CORRUPTED" not in repo.get_room_state("R101", CUTOFF)["evidence"]
    records = repo.get_measurements("room_telemetry.csv", START, START, CUTOFF, entity_id="R101")
    records[0]["temperature_c"] = 999
    assert float(repo.get_measurements("room_telemetry.csv", START, START, CUTOFF, entity_id="R101")[0]["temperature_c"]) == 21


def test_room_reassignment_moves_observed_stay_only_at_event_time(make_dataset):
    from ingestion.replay import ReplayData
    raw = default_events()[:1] + [
        dict(event_id="E_MOVE", timestamp_utc=LATER, type="room_reassigned",
             booking_id="BK_PRESENT", from_room="R101", to_room="R104"),
    ]
    repo = ReplayData(make_dataset(events=raw))
    assert repo.get_room_state("R101", CUTOFF)["occupancy"] == "occupied"
    assert repo.get_room_state("R104", CUTOFF)["occupancy"] == "unknown"
    assert repo.get_room_state("R101", LATER)["occupancy"] == "vacant"
    moved = repo.get_room_state("R104", LATER)
    assert moved["occupancy"] == "occupied"
    assert moved["current_booking_id"] == "BK_PRESENT"


def test_identical_raw_event_duplicates_do_not_double_emit(make_dataset):
    from ingestion.replay import ReplayData
    raw = default_events()
    repo = ReplayData(make_dataset(events=raw + [raw[0].copy()]))
    wrapped = [e for e in repo.iter_events() if e.source.file == "events.jsonl"]
    assert len(wrapped) == len(raw)
    assert len({e.event_id for e in wrapped}) == len(wrapped)

