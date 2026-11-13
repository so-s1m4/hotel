"""Independent regressions for concrete S04/S05/logistics audit findings."""
from copy import deepcopy
from datetime import timedelta

from engine.core import EngineConfig, instant, utc
from engine.detectors.maintenance import MaintenanceDetector
from engine.detectors.surge import SurgeDetector
from engine.detectors.logistics import LogisticsDetector
from engine.planner import DecisionEngine
from engine.resources import ResourceCalendar
from tests.engine.fixtures import FakeGateway, state, slot, tick
from tests.engine.test_extended import full_building, ahu_repo, logistics_repo

NOW = "2026-01-06T09:00:00Z"


def booking(identifier, room, arrival="2026-01-05T14:00:00Z", departure="2026-01-07T10:00:00Z", **extra):
    return {"booking_id": identifier, "room_id": room, "created_utc": "2026-01-01T00:00:00Z",
            "arrival_utc": arrival, "departure_utc": departure, **extra}


def busy_existing_repo(observed=True):
    repo = FakeGateway()
    repo.slots = [slot(resource="CLEANER_2", status="sick")]
    repo.bookings = [booking("B1", "R101"), booking("B2", "R102")]
    if observed:
        repo.states = {room: [state(room=room, occupancy="occupied", cleaning="clean")]
                       for room in ["R101", "R102"]}
    return repo


def test_busy_existing_stays_plus_absence_do_not_require_new_arrivals():
    repo = busy_existing_repo()
    rows = SurgeDetector(EngineConfig()).detect(tick(), repo, full_building(), ResourceCalendar()).candidates
    assert len(rows) == 1
    assert rows[0].issue == "occupancy_surge"
    assert "0 known planned arrivals" in rows[0].reason
    assert "occupied 2/2" in rows[0].reason
    assert all(row.action not in {"schedule_cleaning", "dispatch_robot"} for row in rows)


def test_known_existing_booking_load_is_a_plan_not_a_guest_fact():
    repo = busy_existing_repo(observed=False)
    rows = SurgeDetector(EngineConfig()).detect(tick(), repo, full_building(), ResourceCalendar()).candidates
    assert len(rows) == 1
    assert "peak simultaneous known booking load 2/2" in rows[0].reason
    assert "currently observed occupied 0/2" in rows[0].reason
    assert "Bookings are plans" in rows[0].reason


def test_future_created_and_observed_finished_bookings_do_not_fill_house():
    for extra in [{"created_utc": "2026-01-07T00:00:00Z"}, {"observed_state": "checked_out"}, {"observed_state": "no_show"}]:
        repo = busy_existing_repo(observed=False)
        for b in repo.bookings:
            b.update(extra)
        assert not SurgeDetector(EngineConfig()).detect(tick(), repo, full_building(), ResourceCalendar()).candidates


def test_same_room_overlapping_bookings_do_not_invent_physical_room_load():
    repo = busy_existing_repo(observed=False)
    repo.bookings[1]["room_id"] = "R101"
    assert not SurgeDetector(EngineConfig()).detect(tick(), repo, full_building(), ResourceCalendar()).candidates


def test_planned_load_is_peak_concurrency_not_a_turnover_day_union():
    b = full_building()
    b["building"]["rooms"] = [{"id": f"R{i:03d}"} for i in range(101, 111)]
    repo = FakeGateway()
    repo.slots = [slot(status="sick")]
    for i in range(10):
        room = f"R{101+i:03d}"
        repo.bookings.append(booking(str(i), room,
            arrival="2026-01-05T14:00:00Z" if i < 7 else "2026-01-07T02:00:00Z",
            departure="2026-01-06T09:30:00Z" if i < 7 else "2026-01-07T10:00:00Z"))
    assert not SurgeDetector(EngineConfig()).detect(tick(), repo, b, ResourceCalendar()).candidates


def test_mixed_pressure_windows_do_not_falsely_close_plant_incident():
    repo = ahu_repo()
    detector = MaintenanceDetector(EngineConfig())
    assert detector.detect(tick(), repo, full_building(), ResourceCalendar(), []).candidates
    for i in [1, 2, 3]:
        now = utc(instant(NOW)+timedelta(minutes=15*i))
        for metric, value in [("filter_dp_pa", 100), ("airflow_m3h", 2500)]:
            repo.measurements["equipment.csv"].append({"timestamp_utc": now, "asset_id": "AHU1", "metric": metric, "value": value})
        result = detector.detect(tick(now), repo, full_building(), ResourceCalendar(), [])
        assert "plant:AHU1" not in result.resolved


def test_repeated_same_time_scan_cannot_manufacture_three_recovery_windows():
    repo = ahu_repo(dp=100)
    detector = MaintenanceDetector(EngineConfig())
    for _ in range(3):
        result = detector.detect(tick(), repo, full_building(), ResourceCalendar(), [])
        assert "plant:AHU1" not in result.resolved
        assert "data:AHU1" not in result.resolved


def test_overdue_unverified_order_is_not_duplicated_using_untimestamped_final_status():
    order = {"work_order_id": "WO1", "asset_id": "AHU1", "created_utc": "2026-01-05T09:00:00Z",
             "scheduled_utc": "2026-01-06T08:30:00Z", "status": "completed",
             "completed_utc": "2026-01-06T10:00:00Z"}
    result = MaintenanceDetector(EngineConfig()).detect(tick(), ahu_repo(), full_building(), ResourceCalendar(), [order])
    assert [c.action for c in result.candidates] == ["defer_action"]
    assert "overdue" in result.candidates[0].reason
    # Only an observed completion by this time retires the old order.
    order["observed_completed_utc"] = "2026-01-06T08:45:00Z"
    result = MaintenanceDetector(EngineConfig()).detect(tick(), ahu_repo(), full_building(), ResourceCalendar(), [order])
    assert [c.action for c in result.candidates] == ["order_service"]


def test_main_bin_recovery_closes_service_episode_and_allows_next_real_incident():
    repo = logistics_repo(main=95, floor=0)
    engine = DecisionEngine(repo, full_building())
    first = engine.process(tick())
    initial = next(r for r in first if r.action == "order_service")
    for row in repo.measurements["logistics.csv"]:
        row.update(timestamp_utc="2026-01-06T10:00:00Z", value=10 if row["asset_id"] == "WASTE_MAIN" else 0)
    engine.process(tick("2026-01-06T10:00:00Z"))
    for row in repo.measurements["logistics.csv"]:
        row.update(timestamp_utc="2026-01-06T11:00:00Z", value=95 if row["asset_id"] == "WASTE_MAIN" else 0)
    third = engine.process(tick("2026-01-06T11:00:00Z"))
    renewed = next(r for r in third if r.action == "order_service")
    assert initial.episode_id != renewed.episode_id


def test_unrelated_pickup_does_not_release_reserved_central_volume():
    repo = logistics_repo()
    engine = DecisionEngine(repo, full_building())
    engine.process(tick())
    assert engine.calendar.volume_reservations["WASTE_MAIN"]
    repo.events = [{"timestamp_utc": "2026-01-06T10:00:00Z", "type": "waste_pickup", "asset_id": "OTHER_CONTAINER"}]
    for row in repo.measurements["logistics.csv"]:
        row["timestamp_utc"] = "2026-01-06T11:00:00Z"
    engine.process(tick("2026-01-06T11:00:00Z"))
    assert engine.calendar.volume_reservations["WASTE_MAIN"]


def test_observed_empty_bin_cancels_unneeded_future_transfer_before_planned_end():
    repo = logistics_repo(floor=0)
    for row in repo.measurements["logistics.csv"]:
        row["timestamp_utc"] = "2026-01-06T10:00:00Z"
    detector = LogisticsDetector(EngineConfig())
    detector.open_transfers["WASTE_F1"] = {"decision_id": "D1", "end_utc": "2026-01-06T10:20:00Z"}
    calendar = ResourceCalendar()
    calendar.reserve("ROBOT_1", "2026-01-06T10:00:00Z", "2026-01-06T10:20:00Z", "D1", NOW)
    calendar.reserve_volume("WASTE_MAIN", 216, "D1")
    result = detector.detect(tick("2026-01-06T10:00:00Z"), repo, full_building(), calendar)
    assert not calendar.volume_reservations["WASTE_MAIN"]
    assert not calendar.reservations["ROBOT_1"]
    assert "logistics:WASTE_F1" in result.resolved


def test_forecast_evidence_contains_history_not_only_latest_point():
    repo = FakeGateway()
    repo.measurements["logistics.csv"] = [{"timestamp_utc": utc(instant(NOW)-timedelta(hours=2-i)), "asset_id": "VEND1", "metric": "stock_drinks", "value": stock} for i, stock in enumerate([35, 25, 15])]
    result = LogisticsDetector(EngineConfig()).detect(tick(), repo, full_building(), ResourceCalendar())
    assert result.candidates
    assert any("2026-01-06T07:00:00Z..2026-01-06T09:00:00Z" in e for e in result.candidates[0].evidence)


def test_observed_pickup_after_old_full_reading_prevents_duplicate_service():
    repo = logistics_repo(main=100, floor=0)
    repo.events = [{"timestamp_utc": "2026-01-06T09:30:00Z", "type": "waste_pickup", "asset_id": "WASTE_MAIN"}]
    # The latest available 09:00 level is stale even though it is within 60 min.
    result = LogisticsDetector(EngineConfig()).detect(tick("2026-01-06T10:00:00Z"), repo, full_building(), ResourceCalendar())
    assert not result.candidates


def test_observed_restock_after_old_low_stock_prevents_duplicate_order():
    repo = FakeGateway()
    repo.measurements["logistics.csv"] = [{"timestamp_utc": NOW, "asset_id": "VEND1", "metric": "stock_drinks", "value": 0}]
    repo.events = [{"timestamp_utc": "2026-01-06T09:30:00Z", "type": "vending_restocked", "asset_id": "VEND1"}]
    result = LogisticsDetector(EngineConfig()).detect(tick("2026-01-06T10:00:00Z"), repo, full_building(), ResourceCalendar())
    assert not result.candidates


def test_observed_emptying_after_old_full_bin_prevents_duplicate_robot_dispatch():
    repo = logistics_repo()
    repo.events = [{"timestamp_utc": "2026-01-06T09:30:00Z", "type": "floor_bin_emptied", "asset_id": "WASTE_F1"}]
    result = LogisticsDetector(EngineConfig()).detect(tick("2026-01-06T10:00:00Z"), repo, full_building(), ResourceCalendar())
    assert not result.candidates


def test_implausible_sensor_pairs_do_not_masquerade_as_data_recovery():
    repo = ahu_repo(dp=-10)
    result = MaintenanceDetector(EngineConfig()).detect(tick(), repo, full_building(), ResourceCalendar(), [])
    assert [c.issue for c in result.candidates] == ["data_quality"]
    assert not result.resolved


def test_pressure_recovery_without_airflow_recovery_does_not_close_plant():
    detector = MaintenanceDetector(EngineConfig())
    repo = ahu_repo(dp=100, flow=2500)
    for i in range(3):
        now = utc(instant(NOW)+timedelta(minutes=15*i))
        if i:
            for metric, value in [("filter_dp_pa", 100), ("airflow_m3h", 2500)]:
                repo.measurements["equipment.csv"].append({"timestamp_utc": now, "asset_id": "AHU1", "metric": metric, "value": value})
        result = detector.detect(tick(now), repo, full_building(), ResourceCalendar(), [])
        assert "plant:AHU1" not in result.resolved


def test_robot_only_reservation_does_not_replace_required_human_cleaning():
    from tests.engine.test_extended import surge_repo
    repo = surge_repo()
    calendar = ResourceCalendar()
    calendar.reserve("ROBOT_1", NOW, "2026-01-06T09:20:00Z", "ROBOT_ONLY", NOW, room="R101")
    result = SurgeDetector(EngineConfig()).detect(tick(), repo, full_building(), calendar)
    assert any(c.action == "schedule_cleaning" and c.asset == "R101" for c in result.candidates)
    assert not any(c.action == "dispatch_robot" and c.asset == "R101" for c in result.candidates)


def test_late_human_reservation_does_not_masquerade_as_deadline_coverage():
    from tests.engine.test_extended import surge_repo
    repo = surge_repo()
    repo.bookings[0]["arrival_utc"] = "2026-01-06T10:00:00Z"
    calendar = ResourceCalendar()
    calendar.reserve("CLEANER_1", "2026-01-06T11:00:00Z", "2026-01-06T11:45:00Z", "LATE_JOB", NOW, room="R101")
    result = SurgeDetector(EngineConfig()).detect(tick(), repo, full_building(), calendar)
    human = next(c for c in result.candidates if c.action == "schedule_cleaning" and c.asset == "R101")
    assert instant(human.context["end_utc"]) <= instant("2026-01-06T10:00:00Z")


def test_old_still_unverified_order_requires_status_review_not_duplicate_contract():
    order = {"work_order_id": "OLD", "asset_id": "AHU1", "created_utc": "2025-12-01T09:00:00Z",
             "scheduled_utc": "2025-12-02T08:30:00Z"}
    result = MaintenanceDetector(EngineConfig()).detect(tick(), ahu_repo(), full_building(), ResourceCalendar(), [order])
    assert [c.action for c in result.candidates] == ["defer_action"]
    assert "overdue" in result.candidates[0].reason
