"""Independent cancellation/observed-work regressions from external repros."""
import pytest

from engine.resources import ResourceCalendar
from tests.engine.test_output_validation import files, decision, audit, validate, source

NOW = "2026-01-06T09:00:00Z"


def overlapping_jobs():
    first = decision("A")
    second = decision("B", when="2026-01-06T09:10:00Z", room="R102")
    return first, second, audit(first), audit(second,start="2026-01-06T09:10:00Z",end="2026-01-06T09:55:00Z")


def cancellation(identifier="CANCEL", when="2026-01-06T09:10:00Z", *, human=False, **fields):
    row = decision(identifier,when=when,action="defer_action",resource=None,human=human,dependencies=["A"],**fields)
    trace = audit(row,parameters={"cancelled_decision_id":"A","entry_required":False})
    return row,trace


def test_pending_human_cancellation_does_not_hide_resource_overlap(files):
    first,second,a,b = overlapping_jobs()
    cancel,trace = cancellation(human=True)
    result = validate(files,[first,cancel,second],[a,trace,b])
    assert trace["status"] == "pending_human_approval"
    assert not result["valid"]
    assert result["stats"]["resource_overlap_pairs"] == 1
    assert result["issue_counts"]["live_resource_overlap"] == 1
    assert result["stats"]["cancellation_targets"] == 0


def test_pending_cancellation_itself_is_a_valid_proposal_without_effect(files):
    first = decision("A")
    cancel,trace = cancellation(human=True)
    result = validate(files,[first,cancel],[audit(first),trace])
    assert result["valid"]
    assert result["stats"]["cancellation_targets"] == 0
    assert result["stats"]["live_intervals"] == 1


def test_effective_autonomous_cancellation_releases_capacity_at_its_boundary(files):
    first,second,a,b = overlapping_jobs()
    cancel,trace = cancellation()
    result = validate(files,[first,cancel,second],[a,trace,b])
    assert result["valid"]
    assert result["stats"]["cancellation_targets"] == 1
    assert result["stats"]["resource_overlap_pairs"] == 0


@pytest.mark.parametrize("invalid", [
    {"status":"pending_guest_consent"},
    {"status":"not_a_real_status"},
    {"effects_applied":True},
    {"simulated":False},
    {"action":"flag_anomaly"},
    {"timestamp_utc":"2026-01-06T09:11:00Z"},
])
def test_non_effective_or_invalid_cancellation_audits_do_not_rewrite_capacity(files,invalid):
    first,second,a,b = overlapping_jobs()
    cancel,trace = cancellation()
    trace.update(invalid)
    result = validate(files,[first,cancel,second],[a,trace,b])
    assert not result["valid"]
    assert result["stats"]["resource_overlap_pairs"] == 1
    assert result["stats"]["cancellation_targets"] == 0


def test_pending_earlier_cancellation_cannot_backdate_a_later_effective_one(files):
    first = decision("A")
    second = decision("B",when="2026-01-06T09:15:00Z",room="R102")
    pending,pending_trace = cancellation("C1",human=True)
    effective,effective_trace = cancellation("C2",when="2026-01-06T09:20:00Z")
    result = validate(files,[first,pending,second,effective],[audit(first),pending_trace,
        audit(second,start="2026-01-06T09:15:00Z",end="2026-01-06T10:00:00Z"),effective_trace])
    assert not result["valid"] and result["stats"]["cancellation_targets"] == 1
    overlap = next(row for row in result["issues"] if row["code"] == "live_resource_overlap")
    assert overlap["overlap_start"] == "2026-01-06T09:15:00Z"
    assert overlap["overlap_end"] == "2026-01-06T09:20:00Z"


def test_known_worker_roomless_completion_cannot_clear_unknown_worker_in_other_room(files):
    proposed = decision(resource="CLEANER_2",room="R102")
    start = source("UNKNOWN_START","cleaning_started","2026-01-06T08:30:00Z",resource=None,room="R101")
    finish = source("ROOMLESS_END","cleaning_completed","2026-01-06T08:45:00Z",resource="CLEANER_1",room=None)
    result = validate(files,[proposed],[audit(proposed)],[start,finish])
    assert not result["valid"] and result["issue_counts"]["observed_work_known_busy"] == 1
    calendar = ResourceCalendar()
    calendar.resource_roles = {"CLEANER_1":"cleaner","CLEANER_2":"cleaner"}
    calendar.observe_start(None,"R101","UNKNOWN_START",start["timestamp_utc"],"cleaner")
    calendar.observe_end("CLEANER_1",None,finish["timestamp_utc"],"cleaner")
    assert not calendar.available("CLEANER_2",NOW,"2026-01-06T09:45:00Z",NOW)


def test_known_worker_same_room_completion_can_clear_uncertain_assignment(files):
    proposed = decision(resource="CLEANER_2",room="R102")
    events = [source("UNKNOWN_START","cleaning_started","2026-01-06T08:30:00Z",resource=None,room="R101"),
              source("MATCHING_END","cleaning_completed","2026-01-06T08:45:00Z",resource="CLEANER_1",room="R101")]
    assert validate(files,[proposed],[audit(proposed)],events)["valid"]


def test_roomless_completion_still_clears_its_own_known_performer(files):
    proposed = decision(resource="CLEANER_1",room="R102")
    events = [source("KNOWN_START","cleaning_started","2026-01-06T08:30:00Z",resource="CLEANER_1",room="R101"),
              source("OWN_END","cleaning_completed","2026-01-06T08:45:00Z",resource="CLEANER_1",room=None)]
    assert validate(files,[proposed],[audit(proposed)],events)["valid"]


def test_wrong_room_or_role_cannot_clear_an_unknown_cleaner(files):
    proposed = decision(resource="CLEANER_2",room="R102")
    start = source("UNKNOWN_START","cleaning_started","2026-01-06T08:30:00Z",resource=None,room="R101")
    endings = [source("WRONG_ROOM","cleaning_completed","2026-01-06T08:45:00Z",resource="CLEANER_1",room="R102"),
               source("WRONG_ROLE","robot_floor_cleaning_completed","2026-01-06T08:45:00Z",resource="ROBOT_1",room="R101")]
    for ending in endings:
        result = validate(files,[proposed],[audit(proposed)],[start,ending])
        assert not result["valid"] and "observed_work_known_busy" in result["issue_counts"]


def test_late_start_is_not_suppressed_by_an_unrelated_previously_known_roomless_end(files):
    proposed = decision(when="2026-01-06T09:15:00Z",resource="CLEANER_2",room="R102")
    start = source("LATE_UNKNOWN_START","cleaning_started","2026-01-06T08:30:00Z",resource=None,room="R101",available_at_utc="2026-01-06T09:00:00Z")
    end = source("ROOMLESS_END","cleaning_completed","2026-01-06T08:45:00Z",resource="CLEANER_1",room=None)
    result = validate(files,[proposed],[audit(proposed,start=proposed["timestamp_utc"],end="2026-01-06T10:00:00Z")],[start,end])
    assert not result["valid"] and "observed_work_known_busy" in result["issue_counts"]


def test_late_start_with_matching_previously_known_room_end_stays_completed(files):
    proposed = decision(when="2026-01-06T09:15:00Z",resource="CLEANER_2",room="R102")
    start = source("LATE_UNKNOWN_START","cleaning_started","2026-01-06T08:30:00Z",resource=None,room="R101",available_at_utc="2026-01-06T09:00:00Z")
    end = source("MATCHING_END","cleaning_completed","2026-01-06T08:45:00Z",resource="CLEANER_1",room="R101")
    result = validate(files,[proposed],[audit(proposed,start=proposed["timestamp_utc"],end="2026-01-06T10:00:00Z")],[start,end])
    assert result["valid"]
