from copy import deepcopy
from datetime import timedelta

import pytest

from engine.core import EngineConfig, instant, utc
from engine.planner import DecisionEngine
from tests.engine.fixtures import BUILDING, FakeGateway, state, slot, tick, request, water_repo, energy_repo


def test_s01_conflict_schedules_human_and_conditional_message():
    repo = FakeGateway()
    repo.states["R101"] = [state()]
    repo.slots = [slot()]
    engine = DecisionEngine(repo, BUILDING)
    decisions = engine.process(request())
    assert {d.action for d in decisions} == {"schedule_cleaning", "notify_guest"}
    cleaning = next(d for d in decisions if d.action == "schedule_cleaning")
    assert cleaning.resource_id == "CLEANER_1"
    assert "45 min" in cleaning.reason and "SIMULATED" in cleaning.expected_outcome
    assert engine.executor.audit[0]["parameters"]["end_utc"] == "2026-01-06T09:45:00Z"
    assert not engine.rejections
    assert all(d.action != "release_room" for d in decisions)


def test_s01_clean_vacant_room_has_no_conflict_or_release():
    repo = FakeGateway()
    repo.states["R101"] = [state(cleaning="clean")]
    assert DecisionEngine(repo, BUILDING).process(request()) == []


@pytest.mark.parametrize("status", ["sick", "vacation", "out_of_service", "unknown"])
def test_s01_unavailable_cleaner_never_scheduled(status):
    repo = FakeGateway()
    repo.states["R101"] = [state()]
    repo.slots = [slot(status=status)]
    decisions = DecisionEngine(repo, BUILDING).process(request())
    assert [d.action for d in decisions] == ["notify_guest"]
    assert "No eligible" in decisions[0].reason


@pytest.mark.parametrize("occupancy", ["occupied", "unknown"])
def test_s01_occupied_or_unknown_room_does_not_get_cleaning(occupancy):
    repo = FakeGateway()
    repo.states["R101"] = [state(occupancy=occupancy)]
    repo.slots = [slot()]
    decisions = DecisionEngine(repo, BUILDING).process(request())
    assert [d.action for d in decisions] == ["notify_guest"]


def test_s01_no_double_booking_and_no_repeated_estimates():
    repo = FakeGateway()
    repo.states = {r: [state(room=r)] for r in ["R101", "R102"]}
    repo.slots = [slot(end="2026-01-06T10:30:00Z")]
    engine = DecisionEngine(repo, BUILDING)
    first = engine.process(request(rid="G1"))
    second = engine.process(request(room="R102", rid="G2"))
    reservations = engine.calendar.reservations["CLEANER_1"]
    assert len(reservations) == 2
    assert reservations[0][1] <= reservations[1][0]
    assert sum(d.action == "schedule_cleaning" for d in first + second) == 2
    assert engine.process(tick("2026-01-06T09:15:00Z")) == []


def test_s01_shift_too_short_cannot_invent_capacity():
    repo = FakeGateway()
    repo.states["R101"] = [state()]
    repo.slots = [slot(end="2026-01-06T09:20:00Z")]
    assert all(d.resource_id is None for d in DecisionEngine(repo, BUILDING).process(request()))


def test_s01_unknown_requested_time_asks_clarification():
    repo = FakeGateway()
    repo.states["R101"] = [state()]
    event = request()
    event.payload["intents"][0]["requested_time_utc"] = None
    rows = DecisionEngine(repo, BUILDING).process(event)
    assert [d.action for d in rows] == ["notify_guest"]
    assert "unverified" in rows[0].reason


def test_s01_oncall_has_lead_time_window_and_pending_approval():
    now = "2026-01-06T15:00:00Z"  # 16:00 Vienna
    repo = FakeGateway()
    repo.states["R101"] = [state(t=now)]
    engine = DecisionEngine(repo, BUILDING)
    rows = engine.process(request(t=now, waiting=True))
    assert len(rows) == 1
    assert rows[0].resource_id == "ONCALL_1" and rows[0].requires_human_approval
    assert engine.executor.audit[0]["status"] == "pending_human_approval"
    assert engine.executor.audit[0]["parameters"]["start_utc"] == "2026-01-06T15:45:00Z"
    assert not engine.calendar.reservations.get("ONCALL_1")


def test_s01_oncall_never_offered_too_late():
    now = "2026-01-06T20:30:00Z"
    repo = FakeGateway()
    repo.states["R101"] = [state(t=now)]
    rows = DecisionEngine(repo, BUILDING).process(request(t=now, waiting=True))
    assert all(r.resource_id != "ONCALL_1" for r in rows)


def test_s02_sustained_vacant_night_flow_proposes_inspection():
    engine = DecisionEngine(water_repo(), BUILDING)
    rows = engine.process(tick("2026-01-06T02:00:00Z"))
    assert len(rows) == 1 and rows[0].issue_type == "water_leak"
    assert rows[0].action == "request_inspection" and not rows[0].requires_guest_consent
    assert not engine.rejections


@pytest.mark.parametrize("occupancy,cleaning", [("occupied", "clean"), ("unknown", "clean"), ("vacant", "in_progress"), ("vacant", "dirty")])
def test_s02_guest_cleaning_or_unknown_state_is_not_confirmed_leak(occupancy, cleaning):
    assert DecisionEngine(water_repo(occupancy=occupancy, cleaning=cleaning), BUILDING).process(tick("2026-01-06T02:00:00Z")) == []


@pytest.mark.parametrize("problem", ["reset", "missing", "gap", "spike", "variable", "duplicate_conflict"])
def test_s02_meter_artifacts_do_not_create_leaks(problem):
    repo = water_repo()
    rows = repo.measurements["room_water.csv"]
    if problem == "reset": rows[2]["meter_total_l"] = 0
    if problem == "missing": rows[2]["meter_total_l"] = None
    if problem == "gap": rows.pop(2)
    if problem == "spike": rows[-1]["meter_total_l"] = 5000
    if problem == "variable": rows[2]["meter_total_l"] += 4
    if problem == "duplicate_conflict": rows.append({**rows[2], "meter_total_l": 999})
    assert DecisionEngine(repo, BUILDING).process(tick("2026-01-06T02:00:00Z")) == []


def test_s02_recent_cleaning_and_known_flushing_suppress_false_positive():
    repo = water_repo()
    repo.events = [{"timestamp_utc": "2026-01-06T01:30:00Z", "type": "cleaning_completed", "room_id": "R101"}]
    assert DecisionEngine(repo, BUILDING).process(tick("2026-01-06T02:00:00Z")) == []
    repo.events = []
    engine = DecisionEngine(repo, BUILDING)
    engine.work_orders = [{"created_utc": "2026-01-05T00:00:00Z", "scheduled_utc": "2026-01-06T01:30:00Z", "asset_id": "BUILDING", "summary": "Taps flushed in R101"}]
    assert engine.process(tick("2026-01-06T02:00:00Z")) == []


def test_s02_future_flushing_is_not_used_to_explain_current_flow():
    engine = DecisionEngine(water_repo(), BUILDING)
    engine.work_orders = [{"created_utc": "2026-01-07T00:00:00Z", "scheduled_utc": "2026-01-06T01:30:00Z", "asset_id": "R101", "summary": "meter flush"}]
    assert engine.process(tick("2026-01-06T02:00:00Z"))[0].issue_type == "water_leak"


def test_s02_bms_alarm_alone_is_not_evidence_of_leak():
    event = tick()
    event.event_type = "bms_alarm"
    event.payload = {"type": "bms_alarm", "alarm_code": "water_high", "asset_id": "R101"}
    assert DecisionEngine(FakeGateway(), BUILDING).process(event) == []


def test_s03_comfort_wins_over_expensive_published_tariff():
    repo = energy_repo()
    repo.tariffs = [{"timestamp_utc": "2026-01-06T09:00:00Z", "published_utc": "2026-01-05T12:00:00Z", "price_eur_kwh": 1.5}]
    repo.forecasts = [{"issued_utc": "2026-01-06T05:00:00Z", "target_utc": "2026-01-06T12:00:00Z", "outdoor_c_forecast": -10}]
    engine = DecisionEngine(repo, BUILDING)
    rows = engine.process(tick())
    comfort = next(r for r in rows if r.issue_type == "heating_comfort")
    assert comfort.action == "adjust_room_setpoint"
    assert "does not override" in comfort.reason and "forecast -10" in comfort.reason
    assert next(r for r in engine.executor.audit if r["action"] == "adjust_room_setpoint")["parameters"]["setpoint_c"] == 22
    assert any(r.action == "dispatch_technician" and r.requires_human_approval for r in rows)


@pytest.mark.parametrize("occupancy,temp", [("vacant", 18), ("unknown", 18), ("occupied", 21), ("occupied", None)])
def test_s03_no_comfort_alarm_for_vacancy_normal_or_missing(occupancy, temp):
    rows = DecisionEngine(energy_repo(occupancy=occupancy, temperature=temp), BUILDING).process(tick())
    assert all(r.issue_type != "heating_comfort" for r in rows)


@pytest.mark.parametrize("status", ["defrost", "standby", None])
def test_s03_defrost_standby_missing_status_not_plant_degradation(status):
    repo = energy_repo()
    for row in repo.measurements["equipment.csv"]:
        if row["metric"] == "status": row["value"] = status
    rows = DecisionEngine(repo, BUILDING).process(tick())
    assert all(r.issue_type != "heating_performance" for r in rows)


def test_s03_high_setpoint_inspection_requires_human_and_guest_consent():
    engine = DecisionEngine(energy_repo(setpoint=23), BUILDING)
    row = next(r for r in engine.process(tick()) if r.issue_type == "heating_comfort")
    assert row.action == "request_inspection"
    assert row.requires_human_approval and row.requires_guest_consent
    assert next(a for a in engine.executor.audit if a["decision_id"] == row.event_id)["status"] == "pending_human_approval"


def test_s03_future_forecast_tariff_and_telemetry_never_used():
    repo = energy_repo(temperature=21)
    repo.leak_future_rows = True
    for row in deepcopy(repo.measurements["room_telemetry.csv"]):
        repo.measurements["room_telemetry.csv"].append({**row, "timestamp_utc": "2026-01-07T09:00:00Z", "temperature_c": 1})
    repo.tariffs = [{"timestamp_utc": "2026-01-06T10:00:00Z", "published_utc": "2026-01-07T12:00:00Z", "price_eur_kwh": 999}]
    repo.forecasts = [{"issued_utc": "2026-01-07T05:00:00Z", "target_utc": "2026-01-07T12:00:00Z", "outdoor_c_forecast": -99}]
    rows = DecisionEngine(repo, BUILDING).process(tick())
    assert all(r.issue_type != "heating_comfort" for r in rows)
    assert all("999" not in r.reason and "-99" not in r.reason for r in rows)
    assert all(instant(t) <= instant(tick().available_at_utc) for _, t in repo.calls)


def test_s03_open_window_suggests_guest_check_not_more_heating():
    repo = energy_repo()
    for r in repo.measurements["room_telemetry.csv"]: r["window_open"] = 1
    rows = DecisionEngine(repo, BUILDING).process(tick())
    assert next(r for r in rows if r.issue_type == "heating_comfort").action == "notify_guest"
    assert all(r.action != "dispatch_technician" for r in rows)


def test_s03_single_cold_reading_or_gap_not_sustained():
    repo = energy_repo()
    repo.measurements["room_telemetry.csv"][3]["temperature_c"] = 21
    rows = DecisionEngine(repo, BUILDING).process(tick())
    assert all(r.issue_type != "heating_comfort" for r in rows)


@pytest.mark.parametrize("setpoint,action", [(24, "adjust_room_setpoint"), (21, "notify_guest")])
def test_s03_overheating_never_claims_heating_only_plant_can_cool(setpoint, action):
    repo = energy_repo(temperature=26, setpoint=setpoint)
    engine = DecisionEngine(repo, BUILDING)
    rows = engine.process(tick())
    row = next(r for r in rows if r.issue_type == "heating_comfort")
    assert row.action == action
    assert "cool" in row.reason
    if action == "adjust_room_setpoint":
        assert next(a for a in engine.executor.audit if a["decision_id"] == row.event_id)["parameters"]["setpoint_c"] == 23
