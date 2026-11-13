"""Independent output-validator tests: file fixtures, no planner/ingestion imports."""
from copy import deepcopy
import csv
import json
from pathlib import Path
import subprocess
import sys

import pytest

from engine.validation import validate_outputs

NOW = "2026-01-06T09:00:00Z"


def write_jsonl(path, rows):
    path.write_text("".join(json.dumps(row)+"\n" for row in rows), encoding="utf-8")


def write_csv(path, fields, rows):
    with path.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


@pytest.fixture
def files(tmp_path):
    root = tmp_path/"data"
    root.mkdir()
    (root/"building.json").write_text(json.dumps({"building": {
        "rooms": [{"id": "R101"}, {"id": "R102"}], "assets": [{"id": "BAT1"}]},
        "staff": {"resources": [{"resource_id": "CLEANER_1", "role": "cleaner"},
                                 {"resource_id": "CLEANER_2", "role": "cleaner"},
                                 {"resource_id": "ROBOT_1", "role": "robot"},
                                 {"resource_id": "ONCALL_1", "role": "on_call_housekeeping"}]}}))
    (root/"manifest.json").write_text(json.dumps({"profile": "fixture", "start_utc": "2026-01-06T00:00:00Z", "end_utc": "2026-01-07T00:00:00Z"}))
    write_jsonl(root/"events.jsonl", [])
    write_csv(root/"guest_requests.csv", ["request_id", "booking_id", "room_id"], [
        {"request_id": "G1", "booking_id": "B1", "room_id": "R101"},
        {"request_id": "G2", "booking_id": "B1", "room_id": "R101"},
        {"request_id": "G3", "booking_id": "B2", "room_id": "R101"}])
    write_csv(root/"bookings.csv", ["booking_id", "room_id"], [{"booking_id": "B1", "room_id": "R101"}, {"booking_id": "B2", "room_id": "R101"}])
    write_csv(root/"staff_roster.csv", ["shift_id", "resource_id"], [])
    return root, tmp_path/"decisions.jsonl", tmp_path/"audit.jsonl"


def decision(identifier="D1", when=NOW, action="schedule_cleaning", resource="CLEANER_1", room="R101", human=False, reason="Observed room needs cleaning", **extra):
    return {"event_id": identifier, "timestamp_utc": when, "affected_assets": [room], "issue_type": "room_turnaround",
            "action": action, "priority": "medium", "reason": reason, "requires_human_approval": human,
            "autonomy": "human_approval" if human else "autonomous", "evidence": ["guest_requests.csv:G1"],
            "expected_outcome": "SIMULATED proposal", "verification_plan": "Observe completion",
            **({"resource_id": resource} if resource else {}), **extra}


def audit(record, start=NOW, end="2026-01-06T09:45:00Z", **overrides):
    has_job = bool(record.get("resource_id"))
    params = {"room_id": record["affected_assets"][0] if has_job else None, "entry_required": has_job}
    if has_job:
        params.update(start_utc=start, end_utc=end)
    result = {"decision_id": record["event_id"], "timestamp_utc": record["timestamp_utc"], "action": record["action"],
              "status": "pending_human_approval" if record["requires_human_approval"] else "simulated_message_proposed" if record["action"] == "notify_guest" else "simulated",
              "phase": "approval_pending" if record["requires_human_approval"] else "scheduled",
              "simulated": True, "effects_applied": False, "observed_history_changed": False, "parameters": params}
    result.update(overrides)
    return result


def validate(files, records, audits, events=()):
    root, decisions_path, audit_path = files
    write_jsonl(decisions_path, sorted(records, key=lambda r: (r["timestamp_utc"], r["event_id"])))
    write_jsonl(audit_path, audits)
    write_jsonl(root/"events.jsonl", events)
    return validate_outputs(root, decisions_path, audit_path)


def source(identifier, kind, when, resource="CLEANER_1", room="R102", **extra):
    return {"event_id": identifier, "timestamp_utc": when, "type": kind, "room_id": room, **({"resource_id": resource} if resource else {}), **extra}


def test_valid_official_job_has_one_audit_and_input_hashes(files):
    row = decision()
    result = validate(files, [row], [audit(row)])
    assert result["valid"] and result["error_count"] == 0
    assert result["stats"]["joined_decisions"] == 1
    assert result["stats"]["live_intervals"] == 1
    assert all(len(value) == 64 for value in result["hashes_sha256"].values())
    assert "no ground-truth precision" in result["scope"]


@pytest.mark.parametrize("change,code", [({"requires_human_approval": "false"}, "decision_boolean_type"),
    ({"affected_assets": ["INVENTED"]}, "unknown_affected_asset"), ({"resource_id": "INVENTED"}, "unknown_resource"),
    ({"priority": "urgent"}, "decision_schema")])
def test_schema_and_building_identity_errors_are_reported(files, change, code):
    row = decision(**change)
    result = validate(files, [row], [audit(row)])
    assert not result["valid"] and code in result["issue_counts"]


def test_duplicate_ids_missing_audit_and_duplicate_audit_are_errors(files):
    row = decision()
    assert "duplicate_decision_id" in validate(files, [row, row], [audit(row)])["issue_counts"]
    assert "audit_join_cardinality" in validate(files, [row], [])["issue_counts"]
    assert "audit_join_cardinality" in validate(files, [row], [audit(row), audit(row)])["issue_counts"]


@pytest.mark.parametrize("change", [{"action": "release_room"}, {"timestamp_utc": "2026-01-06T10:00:00Z"}])
def test_action_and_timestamp_join_must_match(files, change):
    row = decision()
    assert "audit_action_or_timestamp_mismatch" in validate(files, [row], [audit(row, **change)])["issue_counts"]


@pytest.mark.parametrize("change", [{"simulated": False}, {"effects_applied": True}, {"observed_history_changed": True}])
def test_real_effect_markers_cannot_pass(files, change):
    row = decision()
    assert "audit_effect_boundary" in validate(files, [row], [audit(row, **change)])["issue_counts"]


def test_approval_required_is_pending_in_default_no_grant_pipeline(files):
    row = decision(human=True)
    result = validate(files, [row], [audit(row)])
    assert result["valid"] and result["stats"]["live_pending_intervals"] == 1
    result = validate(files, [row], [audit(row, status="simulated")])
    assert "required_approval_not_pending" in result["issue_counts"]


def test_live_pending_jobs_cannot_double_book_a_resource(files):
    a, b = decision("A", human=True), decision("B", room="R102", human=True)
    result = validate(files, [a,b], [audit(a), audit(b)])
    assert result["issue_counts"]["live_resource_overlap"] == 1
    assert result["stats"]["live_pending_intervals"] == 2


def test_different_workers_cannot_double_book_same_entry_room(files):
    a, b = decision("A"), decision("B", resource="CLEANER_2")
    result = validate(files, [a,b], [audit(a), audit(b)])
    assert result["issue_counts"]["live_room_overlap"] == 1


def test_cancellation_truncates_interval_and_replacement_can_start_at_boundary(files):
    a = decision("A")
    cancel = decision("CANCEL", when="2026-01-06T09:10:00Z", action="defer_action", resource=None, dependencies=["A"])
    b = decision("B", when="2026-01-06T09:10:00Z")
    result = validate(files, [a,cancel,b], [audit(a), audit(cancel, parameters={"cancelled_decision_id": "A", "entry_required": False}),
                                        audit(b,start="2026-01-06T09:10:00Z",end="2026-01-06T09:55:00Z")])
    assert result["valid"] and result["stats"]["cancellation_targets"] == 1
    assert result["stats"]["resource_overlap_pairs"] == 0


def test_cancel_before_start_is_zero_execution_time(files):
    a = decision("A", when="2026-01-06T08:00:00Z")
    cancel = decision("CANCEL", action="defer_action", resource=None, dependencies=["A"])
    b = decision("B")
    result = validate(files, [a,cancel,b], [audit(a,start="2026-01-06T10:00:00Z",end="2026-01-06T11:00:00Z"),
        audit(cancel,parameters={"cancelled_decision_id": "A", "entry_required": False}),
        audit(b,start="2026-01-06T10:00:00Z",end="2026-01-06T11:00:00Z")])
    assert result["valid"] and result["stats"]["jobs_cancelled_before_start"] == 1


def test_cancellation_is_structured_not_inferred_from_prose(files):
    a, b = decision("A"), decision("B")
    prose = decision("CANCEL", action="defer_action", resource=None, reason="Cancel/supersede A", dependencies=["A"])
    result = validate(files, [a,b,prose], [audit(a), audit(b), audit(prose)])
    assert result["stats"]["cancellation_targets"] == 0
    assert "live_resource_overlap" in result["issue_counts"]
    invalid = audit(prose,parameters={"cancelled_decision_id": "A"})
    prose["dependencies"] = []
    assert "cancellation_structure" in validate(files,[a,prose],[audit(a),invalid])["issue_counts"]


def test_observed_strictly_prior_busy_worker_blocks_even_future_proposal(files):
    row = decision()
    events = [source("START", "cleaning_started", "2026-01-06T08:30:00Z"), source("END", "cleaning_completed", "2026-01-06T10:30:00Z")]
    result = validate(files,[row],[audit(row,start="2026-01-06T11:00:00Z",end="2026-01-06T11:45:00Z")],events)
    assert result["issue_counts"]["observed_work_known_busy"] == 1


def test_unannounced_future_work_does_not_retroactively_accuse_an_earlier_plan(files):
    row = decision()
    events = [source("FUTURE_START", "cleaning_started", "2026-01-06T09:30:00Z")]
    result = validate(files,[row],[audit(row,start="2026-01-06T10:00:00Z",end="2026-01-06T10:45:00Z")],events)
    assert result["valid"]


def test_equal_time_source_ordering_is_counted_separately(files):
    row = decision()
    result = validate(files,[row],[audit(row)],[source("TIE", "cleaning_started",NOW)])
    assert result["valid"] and result["stats"]["observed_equal_time_ties"] == 1
    assert result["warnings"]


def test_equal_time_completion_does_not_false_flag_a_known_busy_job(files):
    row = decision()
    events = [source("START", "cleaning_started", "2026-01-06T08:30:00Z"), source("END", "cleaning_completed", NOW)]
    result = validate(files,[row],[audit(row)],events)
    assert result["valid"] and result["stats"]["observed_equal_time_ties"] == 1


def test_wrong_role_completion_cannot_clear_observed_robot_work_in_room(files):
    row = decision(resource="CLEANER_2")
    events = [source("ROBOT_START", "robot_floor_cleaning_started", "2026-01-06T08:30:00Z",resource="ROBOT_1",room="R101"),
              source("HUMAN_END", "cleaning_completed", "2026-01-06T08:45:00Z",room="R101")]
    result = validate(files,[row],[audit(row)],events)
    assert "observed_work_known_busy" in result["issue_counts"]


def test_missing_performer_is_not_invented_spare_crew_capacity(files):
    row = decision()
    result = validate(files,[row],[audit(row)],[source("UNKNOWN", "cleaning_started", "2026-01-06T08:30:00Z",resource=None)])
    assert "observed_work_known_busy" in result["issue_counts"]


def test_exact_notifications_for_same_booking_derived_from_different_requests_are_duplicates(files):
    a = decision("A",action="notify_guest",resource=None,guest_request_ids=["G1"],reason="Readiness remains unconfirmed")
    b = decision("B",action="notify_guest",resource=None,guest_request_ids=["G2"],reason=a["reason"])
    result = validate(files,[a,b],[audit(a),audit(b)])
    assert result["stats"]["duplicate_exact_notifications"] == 1
    assert result["issue_counts"]["duplicate_exact_notification"] == 1


def test_distinct_bookings_and_updated_messages_are_not_duplicate_notifications(files):
    a = decision("A",action="notify_guest",resource=None,guest_request_ids=["G1"],reason="Readiness remains unconfirmed")
    b = decision("B",action="notify_guest",resource=None,guest_request_ids=["G3"],reason=a["reason"])
    c = decision("C",action="notify_guest",resource=None,guest_request_ids=["G2"],reason="Updated estimate is 10:30 subject to inspection")
    assert validate(files,[a,b,c],[audit(a),audit(b),audit(c)])["valid"]


def test_waiting_event_references_derive_same_booking_scope(files):
    a = decision("A",action="notify_guest",resource=None,evidence=["events.jsonl:WAIT1"],reason="No readiness yet")
    b = decision("B",action="notify_guest",resource=None,evidence=["events.jsonl:WAIT2"],reason=a["reason"])
    events = [source(identifier,"arrival_waiting_room",NOW,resource=None,room="R101",booking_id="B1") for identifier in ["WAIT1","WAIT2"]]
    result = validate(files,[a,b],[audit(a),audit(b)],events)
    assert result["stats"]["duplicate_exact_notifications"] == 1


def test_cli_reports_json_and_nonzero_on_errors_without_real_dataset_dependency(files,tmp_path):
    root, decisions_path, audit_path = files
    row = decision()
    validate(files,[row],[audit(row,effects_applied=True)])
    output = tmp_path/"report.json"
    command = [sys.executable,"-m","engine.validation","--data-dir",str(root),"--decisions",str(decisions_path),"--audit",str(audit_path),"--out",str(output)]
    result = subprocess.run(command,capture_output=True,text=True)
    assert result.returncode == 1
    report = json.loads(output.read_text())
    assert report["issue_counts"]["audit_effect_boundary"] == 1
    assert report["hashes_sha256"]
    assert json.loads(result.stdout)["valid"] is False


def test_late_known_completion_of_an_older_job_cannot_release_newer_busy_work(files):
    row = decision(when="2026-01-06T09:30:00Z")
    events = [source("START", "cleaning_started", NOW),
              source("OLD_END", "cleaning_completed", "2026-01-06T08:00:00Z", available_at_utc="2026-01-06T09:15:00Z")]
    result = validate(files,[row],[audit(row,start="2026-01-06T10:00:00Z",end="2026-01-06T10:45:00Z")],events)
    assert "observed_work_known_busy" in result["issue_counts"]


def test_resource_capability_is_checked_independently_of_its_id_existence(files):
    row = decision(resource="ROBOT_1")
    assert "resource_capability_mismatch" in validate(files,[row],[audit(row)])["issue_counts"]


def test_deleting_entry_marker_cannot_hide_room_conflicts(files):
    a, b = decision("A"), decision("B",resource="CLEANER_2")
    altered = audit(b)
    altered["parameters"].pop("entry_required")
    result = validate(files,[a,b],[audit(a),altered])
    assert "missing_job_entry_marker" in result["issue_counts"]
    assert "live_room_overlap" in result["issue_counts"]


def test_malformed_audit_status_and_source_fields_are_reported_without_crashing(files):
    row = decision()
    result = validate(files,[row],[audit(row,status={"not":"a string"})],
                      [{"event_id":"BAD","type":["cleaning_started"],"timestamp_utc":NOW}])
    assert "audit_status" in result["issue_counts"]
    assert "source_event_schema" in result["issue_counts"]


def test_late_start_does_not_resurrect_a_job_with_an_already_known_later_completion(files):
    row = decision(when="2026-01-06T10:30:00Z")
    events = [source("LATE_START","cleaning_started","2026-01-06T08:00:00Z",available_at_utc="2026-01-06T10:00:00Z"),
              source("END","cleaning_completed",NOW)]
    result = validate(files,[row],[audit(row,start="2026-01-06T11:00:00Z",end="2026-01-06T11:45:00Z")],events)
    assert result["valid"]


def test_explicit_guest_request_ids_take_priority_over_unrelated_evidence_recipients(files):
    a = decision("A",action="notify_guest",resource=None,guest_request_ids=["G3"],evidence=["guest_requests.csv:G1"],reason="Same message")
    b = decision("B",action="notify_guest",resource=None,guest_request_ids=["G3"],evidence=["guest_requests.csv:G3"],reason=a["reason"])
    result = validate(files,[a,b],[audit(a),audit(b)])
    assert result["stats"]["duplicate_exact_notifications"] == 1


def test_joint_notification_does_not_hide_duplicate_for_one_recipient(files):
    a = decision("A",action="notify_guest",resource=None,guest_request_ids=["G1","G3"],reason="Same message")
    b = decision("B",action="notify_guest",resource=None,guest_request_ids=["G1"],reason=a["reason"])
    result = validate(files,[a,b],[audit(a),audit(b)])
    assert result["stats"]["duplicate_exact_notifications"] == 1
