from copy import deepcopy
from datetime import timedelta
import json

import pytest

from common.contracts.schemas import DecisionRecordV2
from engine.core import EngineConfig, instant, utc
from engine.decisions import write_decisions
from engine.planner import DecisionEngine
from engine.resources import oncall_slot, ResourceCalendar
from tests.engine.fixtures import BUILDING, FakeGateway, energy_repo, request, slot, state, tick, water_repo


def test_same_input_same_decisions_and_audit_and_no_observation_mutation():
    repo = energy_repo()
    before = deepcopy(repo.measurements)
    first, second = DecisionEngine(repo, BUILDING), DecisionEngine(repo, BUILDING)
    assert first.process(tick()) == second.process(tick())
    assert first.executor.audit == second.executor.audit
    assert repo.measurements == before


def test_repeat_suppression_then_verified_recovery_opens_new_episode():
    repo = energy_repo()
    engine = DecisionEngine(repo, BUILDING)
    initial = engine.process(tick())
    first_episode = next(r.episode_id for r in initial if r.issue_type == "heating_comfort")
    for minute in [15, 30, 45]:
        t = utc(instant(tick().available_at_utc) + timedelta(minutes=minute))
        repo.measurements["room_telemetry.csv"].append({"timestamp_utc": t, "room_id": "R101", "temperature_c": 18.5, "setpoint_c": 21, "window_open": 0})
        repo.states["R101"].append(state(t=t, occupancy="occupied", cleaning="clean"))
        assert engine.process(tick(t)) == []
    for minute in range(60, 135, 15):
        t = utc(instant(tick().available_at_utc) + timedelta(minutes=minute))
        repo.measurements["room_telemetry.csv"].append({"timestamp_utc": t, "room_id": "R101", "temperature_c": 21, "setpoint_c": 21, "window_open": 0})
        repo.states["R101"].append(state(t=t, occupancy="occupied", cleaning="clean"))
        engine.process(tick(t))
    new_rows = []
    for minute in [135, 150, 165]:
        t = utc(instant(tick().available_at_utc) + timedelta(minutes=minute))
        repo.measurements["room_telemetry.csv"].append({"timestamp_utc": t, "room_id": "R101", "temperature_c": 18, "setpoint_c": 21, "window_open": 0})
        repo.states["R101"].append(state(t=t, occupancy="occupied", cleaning="clean"))
        new_rows += engine.process(tick(t))
    assert any(r.issue_type == "heating_comfort" and r.episode_id != first_episode for r in new_rows)


def test_sick_update_withdraws_existing_estimate_without_rebooking_absent_cleaner():
    repo = FakeGateway()
    repo.states["R101"] = [state()]
    repo.slots = [slot(start="2026-01-06T10:00:00Z")]
    engine = DecisionEngine(repo, BUILDING)
    assert any(d.action == "schedule_cleaning" for d in engine.process(request(deadline="2026-01-06T12:00:00Z")))
    repo.slots[0].update(effective_status_asof="sick", status="sick", status_known_utc="2026-01-06T09:15:00Z")
    repo.states["R101"].append(state(t="2026-01-06T09:15:00Z"))
    rows = engine.process(tick("2026-01-06T09:15:00Z"))
    assert any("withdraw" in d.reason for d in rows)
    assert not any(d.action == "schedule_cleaning" for d in rows)
    assert not engine.calendar.reservations["CLEANER_1"]


def test_sick_update_can_replan_with_another_available_cleaner():
    repo = FakeGateway()
    repo.states["R101"] = [state()]
    repo.slots = [slot(start="2026-01-06T10:00:00Z")]
    building = deepcopy(BUILDING)
    building["staff"]["resources"].append({"resource_id": "CLEANER_2", "role": "cleaner"})
    engine = DecisionEngine(repo, building)
    engine.process(request(deadline="2026-01-06T12:00:00Z"))
    repo.slots[0].update(effective_status_asof="sick", status="sick")
    repo.slots.append(slot(resource="CLEANER_2", shift_id="SH2", start="2026-01-06T10:00:00Z"))
    repo.states["R101"].append(state(t="2026-01-06T09:15:00Z"))
    rows = engine.process(tick("2026-01-06T09:15:00Z"))
    assert any(r.action == "schedule_cleaning" and r.resource_id == "CLEANER_2" for r in rows)


def test_future_absence_is_not_used_before_gateway_effective_update():
    repo = FakeGateway()
    repo.states["R101"] = [state()]
    repo.slots = [slot(status="sick", effective_status_asof="scheduled", status_known_utc="2026-01-06T12:00:00Z")]
    assert any(r.action == "schedule_cleaning" for r in DecisionEngine(repo, BUILDING).process(request()))


def test_oncall_uses_vienna_dst_summer_and_winter():
    calendar = ResourceCalendar()
    # 15:00 local, earliest arrival 15:45 local in both seasons.
    assert oncall_slot("2026-01-06T14:00:00Z", "2026-01-06T20:00:00Z", 45, calendar)[0] == "2026-01-06T14:45:00Z"
    assert oncall_slot("2026-07-06T13:00:00Z", "2026-07-06T19:00:00Z", 45, calendar)[0] == "2026-07-06T13:45:00Z"


def test_sorted_stream_enforced_and_duplicates_ignored():
    engine = DecisionEngine(FakeGateway(), BUILDING)
    assert engine.process(tick()) == []
    assert engine.process(tick()) == []
    assert engine.event_count == 1
    with pytest.raises(ValueError, match="sorted"):
        engine.process(tick("2026-01-06T08:45:00Z"))


def test_event_with_future_occurrence_is_rejected():
    engine = DecisionEngine(FakeGateway(), BUILDING)
    row = tick()
    row.timestamp_utc = "2026-01-07T09:00:00Z"
    with pytest.raises(ValueError, match="Future"):
        engine.process(row)


def test_official_jsonl_valid_sorted_deterministic_and_duplicate_failure_is_atomic(tmp_path):
    rows = DecisionEngine(energy_repo(), BUILDING).process(tick())
    output = tmp_path / "decisions.jsonl"
    write_decisions(reversed(rows), output)
    raw = output.read_text()
    parsed = [DecisionRecordV2.model_validate_json(line) for line in raw.splitlines()]
    assert parsed == sorted(rows, key=lambda r: (instant(r.timestamp_utc), r.event_id))
    write_decisions(rows, output)
    assert output.read_text() == raw
    with pytest.raises(ValueError, match="Duplicate"):
        write_decisions(rows + rows, output)
    assert output.read_text() == raw


def test_main_residual_requires_all_room_meters_and_baseline():
    repo = FakeGateway()
    start = instant("2026-01-05T18:00:00Z")
    main = 1000
    for i in range(37):
        t = utc(start + timedelta(minutes=15 * i))
        main += 300 if i >= 33 else 10
        repo.measurements.setdefault("water_main.csv", []).append({"timestamp_utc": t, "meter_total_l": main})
        for room in ["R101", "R102"]:
            repo.measurements.setdefault("room_water.csv", []).append({"timestamp_utc": t, "room_id": room, "meter_total_l": 100})
    engine = DecisionEngine(repo, BUILDING)
    rows = []
    for i in range(4, 37):
        rows += engine.process(tick(utc(start + timedelta(minutes=15 * i))))
    assert sum(r.issue_type == "water_balance" for r in rows) == 1
    assert "not proof" in rows[-1].reason
    # A missing room means the residual cannot be computed from a partial sum.
    repo.measurements["room_water.csv"] = [r for r in repo.measurements["room_water.csv"] if r["room_id"] == "R101"]
    engine = DecisionEngine(repo, BUILDING)
    assert all(not engine.process(tick(utc(start + timedelta(minutes=15 * i)))) for i in range(4, 37))


def test_occupancy_transition_within_meter_interval_suppresses_leak_claim():
    repo = water_repo()
    repo.events = [{"timestamp_utc": "2026-01-06T01:05:00Z", "type": "checkout_early", "room_id": "R101"}]
    assert DecisionEngine(repo, BUILDING).process(tick("2026-01-06T02:00:00Z")) == []


@pytest.mark.parametrize("override", [{"cleaning_minutes": -1}, {"water_window_minutes": 20}, {"low_cop": 0}, {"water_min_l_per_interval": float("nan")}])
def test_invalid_config_rejected(override):
    with pytest.raises(ValueError):
        EngineConfig(**override)


def test_multiple_requests_for_same_room_do_not_schedule_duplicate_cleaning():
    repo = FakeGateway()
    repo.states["R101"] = [state()]
    repo.slots = [slot(), slot(resource="CLEANER_2", shift_id="SH2")]
    building = deepcopy(BUILDING)
    building["staff"]["resources"].append({"resource_id": "CLEANER_2", "role": "cleaner"})
    engine = DecisionEngine(repo, building)
    rows = engine.process(request(rid="G1")) + engine.process(request(rid="G2"))
    assert sum(r.action == "schedule_cleaning" for r in rows) == 1
    assert len(engine.calendar.room_reservations["R101"]) == 1
    assert not engine.rejections


def test_capacity_becomes_available_for_two_pending_rooms_on_same_tick():
    repo = FakeGateway()
    repo.states = {room: [state(room=room)] for room in ["R101", "R102"]}
    engine = DecisionEngine(repo, BUILDING)
    engine.process(request(rid="G1"))
    engine.process(request(rid="G2", room="R102"))
    repo.slots = [slot()]
    now = "2026-01-06T09:15:00Z"
    for room in ["R101", "R102"]:
        repo.states[room].append(state(room=room, t=now))
    rows = engine.process(tick(now))
    assert sum(r.action == "schedule_cleaning" for r in rows) == 2
    a, b = engine.calendar.reservations["CLEANER_1"]
    assert a[1] <= b[0]
    assert not engine.rejections


def test_duplicate_water_readings_do_not_double_consumption():
    repo = water_repo()
    original = DecisionEngine(repo, BUILDING).process(tick("2026-01-06T02:00:00Z"))
    repo.measurements["room_water.csv"] *= 2
    duplicate = DecisionEngine(repo, BUILDING).process(tick("2026-01-06T02:00:00Z"))
    assert duplicate == original
