"""Withdrawal is terminal even for direct calendar callers."""
import pytest

from engine.resources import ResourceCalendar


def test_withdrawn_job_cannot_retake_worker_room_or_container_under_its_old_id():
    calendar = ResourceCalendar()
    now, end = "2026-01-06T09:00:00Z", "2026-01-06T10:00:00Z"
    calendar.current_time = now
    calendar.reserve_tentative("ROBOT_1", now, end, "old", now, room="R101")
    calendar.reserve_volume("WASTE_MAIN", 100, "old")
    calendar.cancel("old")
    for reserve in (calendar.reserve, calendar.reserve_tentative):
        with pytest.raises(ValueError, match="Cancelled"):
            reserve("ROBOT_1", now, end, "old", now, room="R101")
    with pytest.raises(ValueError, match="Cancelled"):
        calendar.reserve_volume("WASTE_MAIN", 100, "old")
    assert calendar.reservations_for("ROBOT_1") == []
    assert calendar.room_jobs("R101") == []
    assert calendar.volume_reservations["WASTE_MAIN"] == []
    calendar.reserve("ROBOT_1", now, end, "new", now, room="R101")
    calendar.reserve_volume("WASTE_MAIN", 100, "new")
    assert calendar.room_jobs("R101") == [(now, end, "new")]
    assert calendar.volume_reservations["WASTE_MAIN"] == [("new", 100)]
