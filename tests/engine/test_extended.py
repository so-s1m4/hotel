from copy import deepcopy
from datetime import timedelta
import json

import pytest

from engine.core import Candidate, instant, utc
from engine.planner import DecisionEngine
from engine.resources import ResourceCalendar
from engine.safety import SafetyViolation, validate_safety
from engine.detectors.logistics import next_regular
from tests.engine.fixtures import BUILDING, FakeGateway, state, slot, tick

NOW = "2026-01-06T09:00:00Z"


def full_building():
    b = deepcopy(BUILDING)
    b["building"]["assets"] = [a for a in b["building"]["assets"] if a["id"] != "BAT1"]
    b["building"]["assets"] += [
        {"id": "AHU1", "nominal_airflow_m3h": 4200},
        {"id": "BAT1", "max_charge_kw": 40, "max_discharge_kw": 40,
         "soc_bounds_kwh": [10, 95], "round_trip_efficiency": 0.9},
        {"id": "PV1", "peak_kwp": 60},
        {"id": "WASTE_F1", "volume_l": 240}, {"id": "WASTE_F2", "volume_l": 240},
        {"id": "WASTE_MAIN", "volume_l": 1100},
        {"id": "VEND1", "capacity": {"drinks": 100, "snacks": 60}},
    ]
    b["staff"]["resources"].append({"resource_id": "CLEANER_2", "role": "cleaner"})
    return b


def ahu_repo(dp=300, flow=2500):
    repo = FakeGateway()
    for i in range(5):
        t = utc(instant(NOW)-timedelta(minutes=15*(4-i)))
        for metric, value in [("filter_dp_pa", dp), ("airflow_m3h", flow), ("fan_kw", 1.2)]:
            repo.measurements.setdefault("equipment.csv", []).append({"timestamp_utc": t, "asset_id": "AHU1", "metric": metric, "value": value})
    return repo


def surge_repo():
    repo = FakeGateway()
    for i, room in enumerate(["R101", "R102"]):
        repo.states[room] = [state(room=room)]
        repo.bookings.append({"booking_id": "B"+str(i), "room_id": room,
                              "created_utc": "2026-01-05T00:00:00Z", "arrival_utc": "2026-01-06T12:00:00Z", "departure_utc": "2026-01-07T10:00:00Z"})
    repo.slots = [slot(), slot(resource="CLEANER_2", status="sick", shift_id="SH2"),
                  slot(resource="ROBOT_1", role="robot", shift_id="SHR")]
    return repo


def battery_repo(price=0.1, future=(0.3, 0.4), soc=50, pv=0, load=30):
    repo = FakeGateway()
    repo.measurements["energy.csv"] = [{"timestamp_utc": NOW, "building_load_kw": load,
        "hvac_electric_kw": 10, "pv_kw": pv, "battery_discharge_kw_signed": 0,
        "battery_soc_kwh": soc, "grid_import_kw": max(0, load-pv), "grid_export_kw": max(0, pv-load)}]
    repo.tariffs = [{"timestamp_utc": utc(instant(NOW)+timedelta(hours=i)), "price_eur_kwh": value,
                     "published_utc": "2026-01-05T12:00:00Z"} for i, value in enumerate([price, *future])]
    return repo


def logistics_repo(main=30, floor=90):
    repo = FakeGateway()
    repo.measurements["logistics.csv"] = [{"timestamp_utc": NOW, "asset_id": asset,
        "metric": "fill_pct", "value": value} for asset, value in [("WASTE_F1", floor), ("WASTE_MAIN", main)]]
    repo.slots = [slot(resource="ROBOT_1", role="robot", shift_id="SHR")]
    return repo


def test_s04_high_pressure_and_low_airflow_with_real_technician_slot():
    repo = ahu_repo()
    repo.slots = [slot(resource="TECH_1", role="technician", shift_id="SHT")]
    engine = DecisionEngine(repo, full_building())
    rows = engine.process(tick())
    service = next(r for r in rows if r.issue_type == "ventilation_maintenance")
    assert service.action == "dispatch_technician" and service.resource_id == "TECH_1"
    assert service.requires_human_approval
    assert engine.executor.audit[0]["status"] == "pending_human_approval"
    assert not engine.rejections


def test_s04_unavailable_technician_means_approved_external_request_not_fake_capacity():
    repo = ahu_repo()
    repo.slots = [slot(resource="TECH_1", role="technician", status="sick")]
    rows = DecisionEngine(repo, full_building()).process(tick())
    assert any(r.action == "order_service" and r.requires_human_approval and r.resource_id is None for r in rows)


def test_s04_pressure_alone_is_not_a_confirmed_cascade():
    rows = DecisionEngine(ahu_repo(flow=4200), full_building()).process(tick())
    assert [r.action for r in rows] == ["flag_anomaly"]


def test_s04_missing_readings_are_data_quality_not_plant_fault():
    repo = ahu_repo()
    for row in repo.measurements["equipment.csv"]:
        if row["metric"] == "filter_dp_pa": row["value"] = None
    rows = DecisionEngine(repo, full_building()).process(tick())
    assert len(rows) == 1 and rows[0].issue_type == "data_quality"
    assert rows[0].action == "flag_anomaly"


def test_s04_known_future_service_avoids_duplicate_order_and_ignores_completed_status():
    engine = DecisionEngine(ahu_repo(), full_building())
    engine.work_orders = [{"work_order_id": "WO1", "asset_id": "AHU1", "created_utc": "2026-01-05T09:00:00Z", "scheduled_utc": "2026-01-06T12:00:00Z", "status": "completed", "completed_utc": "2026-01-06T13:00:00Z"}]
    rows = engine.process(tick())
    assert len(rows) == 1 and rows[0].action == "defer_action"
    assert "known schedule" in rows[0].evidence[-1]


def test_s04_single_high_sample_or_bad_airflow_does_not_trigger_service():
    for variant in ["single", "implausible"]:
        repo = ahu_repo(dp=100)
        if variant == "single": repo.measurements["equipment.csv"][-3]["value"] = 300
        else:
            for row in repo.measurements["equipment.csv"]:
                if row["metric"] == "airflow_m3h": row["value"] = -10
        assert not DecisionEngine(repo, full_building()).process(tick())


def test_s05_known_arrival_rush_absence_and_robots_share_resources():
    engine = DecisionEngine(surge_repo(), full_building())
    rows = engine.process(tick())
    assert sum(r.action == "schedule_cleaning" for r in rows) == 2
    assert sum(r.action == "dispatch_robot" for r in rows) == 2
    assert any(r.issue_type == "occupancy_surge" and r.action == "flag_anomaly" for r in rows)
    assert all(r.resource_id != "CLEANER_2" for r in rows)
    assert not engine.rejections
    for reservations in engine.calendar.reservations.values():
        for a, b in zip(sorted(reservations), sorted(reservations)[1:]):
            assert a[1] <= b[0]
    assert all(r.action != "release_room" for r in rows)


def test_s05_future_bookings_and_absence_not_used_before_known():
    repo = surge_repo()
    for b in repo.bookings: b["created_utc"] = "2026-01-07T00:00:00Z"
    assert DecisionEngine(repo, full_building()).process(tick()) == []
    repo = surge_repo()
    repo.slots[1].update(status_known_utc="2026-01-07T00:00:00Z", effective_status_asof="scheduled")
    assert DecisionEngine(repo, full_building()).process(tick()) == []


def test_s05_no_false_rush_when_staff_capacity_is_sufficient():
    repo = surge_repo()
    repo.slots = [s for s in repo.slots if s["effective_status_asof"] == "scheduled"]
    assert DecisionEngine(repo, full_building()).process(tick()) == []


def test_s05_never_cleans_occupied_rooms_or_uses_robot_as_human():
    repo = surge_repo()
    repo.states["R101"] = [state(occupancy="occupied")]
    repo.slots = [s for s in repo.slots if s["role"] != "cleaner" or s["status"] == "sick"]
    rows = DecisionEngine(repo, full_building()).process(tick())
    assert all(r.action not in {"schedule_cleaning", "dispatch_robot"} for r in rows)
    assert any(r.action == "notify_guest" and r.affected_assets == ["R102"] for r in rows)


@pytest.mark.parametrize("price,future,expected_sign", [(0.1, (0.3, 0.4), -1), (0.5, (0.1, 0.2), 1)])
def test_battery_dispatch_respects_bounds_and_keeps_hvac_unchanged(price, future, expected_sign):
    repo = battery_repo(price=price, future=future)
    before = deepcopy(repo.measurements)
    engine = DecisionEngine(repo, full_building())
    rows = engine.process(tick())
    row = next(r for r in rows if r.action == "optimize_energy")
    assert not row.requires_human_approval
    parameters = engine.executor.audit[0]["parameters"]
    assert parameters["battery_power_kw"]*expected_sign > 0
    assert 10 <= parameters["expected_soc_kwh"] <= 95
    assert parameters["duration_minutes"] == 15
    assert repo.measurements == before
    assert not engine.rejections


def test_pv_surplus_charges_battery_without_assuming_future_sun():
    repo = battery_repo(pv=30, load=10)
    repo.tariffs = []
    engine = DecisionEngine(repo, full_building())
    assert engine.process(tick())[0].action == "optimize_energy"
    assert engine.executor.audit[0]["parameters"]["battery_power_kw"] == -20


@pytest.mark.parametrize("soc,price,future", [(95, 0.1, (0.3, 0.4)), (10, 0.5, (0.1, 0.2))])
def test_battery_boundaries_do_not_create_impossible_dispatch(soc, price, future):
    assert DecisionEngine(battery_repo(soc=soc, price=price, future=future), full_building()).process(tick()) == []


def test_battery_future_price_publication_cannot_drive_plan():
    repo = battery_repo()
    for r in repo.tariffs: r["published_utc"] = "2026-01-07T00:00:00Z"
    assert DecisionEngine(repo, full_building()).process(tick()) == []


def test_battery_safety_rejects_bad_balance_and_overpowered_control():
    repo = battery_repo()
    repo.measurements["energy.csv"][0]["grid_import_kw"] = 999
    engine = DecisionEngine(repo, full_building())
    assert engine.process(tick()) == []
    assert any("balance" in r["reason"] for r in engine.rejections)
    battery = next(a for a in full_building()["building"]["assets"] if a["id"] == "BAT1")
    row = Candidate("battery", "BAT1", "energy_optimization", "optimize_energy", "reason", ["energy.csv:t"], "Verify SOC", "Bounded dispatch").record(NOW, "E")
    ctx = {"building": full_building(), "battery": battery, "observed_energy": battery_repo().measurements["energy.csv"][0], "battery_power_kw": 50, "duration_minutes": 15, "expected_soc_kwh": 20, "strategy": "peak_discharge"}
    with pytest.raises(SafetyViolation, match="power"):
        validate_safety(row, ctx)


def test_cheap_known_cold_arrival_preheats_vacant_room_only():
    repo = battery_repo()
    repo.forecasts = [{"issued_utc": "2026-01-06T05:00:00Z", "target_utc": "2026-01-06T10:00:00Z", "outdoor_c_forecast": -5}]
    repo.bookings = [{"booking_id": "B1", "room_id": "R101", "created_utc": "2026-01-05T00:00:00Z", "arrival_utc": "2026-01-06T10:00:00Z", "departure_utc": "2026-01-07T10:00:00Z"}]
    repo.states["R101"] = [state(setpoint_c=18)]
    rows = DecisionEngine(repo, full_building()).process(tick())
    assert any(r.action == "adjust_room_setpoint" for r in rows)
    repo.states["R101"] = [state(occupancy="occupied", setpoint_c=18)]
    assert all(r.action != "adjust_room_setpoint" for r in DecisionEngine(repo, full_building()).process(tick()))


def test_bin_transport_reserves_robot_and_container_without_faking_measurements():
    repo = logistics_repo()
    before = deepcopy(repo.measurements)
    engine = DecisionEngine(repo, full_building())
    rows = engine.process(tick())
    assert any(r.action == "dispatch_robot" and r.resource_id == "ROBOT_1" for r in rows)
    assert engine.calendar.volume_reservations["WASTE_MAIN"][0][1] == 216
    assert repo.measurements == before
    assert not engine.rejections


def test_full_container_and_absent_robot_never_get_transport():
    repo = logistics_repo(main=95)
    rows = DecisionEngine(repo, full_building()).process(tick())
    assert all(r.action != "dispatch_robot" for r in rows)
    assert any(r.action == "order_service" and r.requires_human_approval for r in rows)
    repo = logistics_repo()
    repo.slots[0]["effective_status_asof"] = "out_of_service"
    assert all(r.action != "dispatch_robot" for r in DecisionEngine(repo, full_building()).process(tick()))


def test_two_bin_transfers_cannot_overfill_central_capacity():
    repo = logistics_repo(main=60)
    repo.measurements["logistics.csv"].append({"timestamp_utc": NOW, "asset_id": "WASTE_F2", "metric": "fill_pct", "value": 90})
    engine = DecisionEngine(repo, full_building())
    rows = engine.process(tick())
    assert sum(r.action == "dispatch_robot" for r in rows) == 1
    assert sum(amount for _, amount in engine.calendar.volume_reservations["WASTE_MAIN"]) == 216
    assert not engine.rejections


def test_vending_forecast_orders_early_but_prefers_regular_restock_when_timely():
    repo = FakeGateway()
    for i, stock in enumerate([35, 25, 15]):
        repo.measurements.setdefault("logistics.csv", []).append({"timestamp_utc": utc(instant(NOW)-timedelta(hours=2-i)), "asset_id": "VEND1", "metric": "stock_drinks", "value": stock})
    rows = DecisionEngine(repo, full_building()).process(tick())
    assert len(rows) == 1 and rows[0].action == "order_service" and rows[0].requires_human_approval
    # Monday 09:00 local; next regular restock is 10:00, before slow predicted depletion.
    now = "2026-01-05T08:00:00Z"
    repo.measurements["logistics.csv"] = [{"timestamp_utc": utc(instant(now)-timedelta(hours=2-i)), "asset_id": "VEND1", "metric": "stock_drinks", "value": stock} for i, stock in enumerate([19, 18, 17])]
    rows = DecisionEngine(repo, full_building()).process(tick(now))
    assert len(rows) == 1 and rows[0].action == "defer_action"


def test_regular_service_hours_are_local_across_dst():
    assert next_regular("2026-07-06T06:00:00Z", {0, 3}, 10) == "2026-07-06T08:00:00Z"
    assert next_regular("2026-01-05T07:00:00Z", {0, 3}, 10) == "2026-01-05T09:00:00Z"


def test_new_domains_repeat_suppress_with_same_input_and_no_future_sensor_leak():
    repo = ahu_repo()
    repo.leak_future_rows = True
    for r in deepcopy(repo.measurements["equipment.csv"]):
        repo.measurements["equipment.csv"].append({**r, "timestamp_utc": "2026-01-07T09:00:00Z", "value": 999})
    a, b = DecisionEngine(repo, full_building()), DecisionEngine(repo, full_building())
    assert a.process(tick()) == b.process(tick())
    assert a.process(tick(event_id="tick:z")) == []


def test_preheating_is_independent_of_battery_headroom_and_uses_latest_known_forecast():
    repo = battery_repo(soc=95)
    repo.states["R101"] = [state(setpoint_c=18)]
    repo.bookings = [{"booking_id": "B1", "room_id": "R101", "created_utc": "2026-01-05T00:00:00Z", "arrival_utc": "2026-01-06T10:00:00Z", "departure_utc": "2026-01-07T10:00:00Z"}]
    repo.forecasts = [{"issued_utc": "2026-01-06T05:00:00Z", "target_utc": "2026-01-06T10:00:00Z", "outdoor_c_forecast": -10}]
    rows = DecisionEngine(repo, full_building()).process(tick())
    assert [r.action for r in rows] == ["adjust_room_setpoint"]
    repo.forecasts.append({"issued_utc": "2026-01-06T08:00:00Z", "target_utc": "2026-01-06T10:00:00Z", "outdoor_c_forecast": 10})
    assert DecisionEngine(repo, full_building()).process(tick()) == []


def test_cross_domain_plans_share_robot_time_and_emit_explicit_dependencies():
    repo = surge_repo()
    repo.measurements.update(ahu_repo().measurements)
    repo.measurements.update(battery_repo().measurements)
    repo.tariffs = battery_repo().tariffs
    repo.measurements.update(logistics_repo().measurements)
    engine = DecisionEngine(repo, full_building())
    rows = engine.process(tick())
    assert {"occupancy_surge", "ventilation_maintenance", "logistics", "energy_optimization"} <= {r.issue_type for r in rows}
    assert not engine.rejections
    assert len({r.event_id for r in rows}) == len(rows)
    reservations = sorted(engine.calendar.reservations["ROBOT_1"])
    assert len(reservations) == 3
    assert all(a[1] <= b[0] for a, b in zip(reservations, reservations[1:]))
    robot_room_jobs = [r for r in rows if r.action == "dispatch_robot" and r.affected_assets[0].startswith("R")]
    assert all(r.dependencies and r.dependencies[0] in {s.event_id for s in rows if s.action == "schedule_cleaning"} for r in robot_room_jobs)


def test_old_pickup_does_not_clear_capacity_reserved_after_the_pickup():
    repo = logistics_repo()
    repo.events = [{"timestamp_utc": "2026-01-06T08:30:00Z", "type": "waste_pickup", "asset_id": "WASTE_MAIN"}]
    engine = DecisionEngine(repo, full_building())
    engine.process(tick())
    assert engine.calendar.volume_reservations["WASTE_MAIN"]
    for row in repo.measurements["logistics.csv"]: row["timestamp_utc"] = "2026-01-06T10:00:00Z"
    engine.process(tick("2026-01-06T10:00:00Z"))
    assert engine.calendar.volume_reservations["WASTE_MAIN"]
    repo.events.append({"timestamp_utc": "2026-01-06T10:30:00Z", "type": "waste_pickup", "asset_id": "WASTE_MAIN"})
    for row in repo.measurements["logistics.csv"]: row["timestamp_utc"] = "2026-01-06T11:00:00Z"
    engine.process(tick("2026-01-06T11:00:00Z"))
    assert not engine.calendar.volume_reservations["WASTE_MAIN"]


def test_vending_implausible_history_cannot_create_a_depletion_forecast():
    repo = FakeGateway()
    repo.measurements["logistics.csv"] = [{"timestamp_utc": utc(instant(NOW)-timedelta(hours=2-i)), "asset_id": "VEND1", "metric": "stock_drinks", "value": stock} for i, stock in enumerate([1000, 70, 50])]
    assert DecisionEngine(repo, full_building()).process(tick()) == []


@pytest.mark.parametrize("soc", [10, 10.1, 50, 94.9, 95])
def test_projected_battery_soc_stays_bounded_at_capacity_edges(soc):
    for price, future in [(0.1, (0.3, 0.4)), (0.5, (0.1, 0.2))]:
        engine = DecisionEngine(battery_repo(soc=soc, price=price, future=future), full_building())
        engine.process(tick())
        assert not engine.rejections
        assert all(10-1e-8 <= row["parameters"]["expected_soc_kwh"] <= 95+1e-8 for row in engine.executor.audit)
