"""Bounded refresh tests: tiny file fixtures, no replay or real dataset dependency."""
from dataclasses import asdict
import csv
import hashlib
import json
from pathlib import Path

import pytest

from engine.core import EngineConfig
import scripts.refresh_f5_artifacts as refresh
from scripts.evaluate_water import raw_completeness


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def json_file(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(value),encoding="utf-8")


def csv_file(path,fields,rows=()):
    with path.open("w",newline="") as stream:
        writer=csv.DictWriter(stream,fieldnames=fields);writer.writeheader();writer.writerows(rows)


@pytest.fixture
def artifacts(tmp_path,monkeypatch):
    data,output,review=tmp_path/"data",tmp_path/"outputs",tmp_path/"review"
    output.mkdir()
    baseline=b"fixture trusted baseline blob"
    monkeypatch.setattr(refresh,"_baseline_blob",lambda revision:baseline)
    code=refresh.REPO_ROOT
    config=asdict(EngineConfig());profiles=[]
    sources={p.name:digest(p) for p in (code/"engine/detectors").glob("*.py")}
    for name in refresh.PROFILES:
        root=data/("PLATO40_v2_"+name);root.mkdir(parents=True)
        manifest={"profile":name,"start_utc":"2026-01-01T00:00:00Z","end_utc":"2026-01-01T00:30:00Z","days":1,"interval_seconds":900}
        json_file(root/"manifest.json",manifest)
        json_file(root/"building.json",{"building":{"rooms":[{"id":"R101"}],"assets":[]},"staff":{"resources":[]}})
        (root/"events.jsonl").write_text("")
        csv_file(root/"room_water.csv",["timestamp_utc","room_id","meter_total_l"],[
            {"timestamp_utc":"2026-01-01T00:00:00Z","room_id":"R101","meter_total_l":100},
            {"timestamp_utc":"2026-01-01T00:15:00Z","room_id":"R101","meter_total_l":101}])
        csv_file(root/"water_main.csv",["timestamp_utc","meter_total_l"],[
            {"timestamp_utc":"2026-01-01T00:00:00Z","meter_total_l":200},
            {"timestamp_utc":"2026-01-01T00:15:00Z","meter_total_l":202}])
        for filename,fields in [("room_telemetry.csv",["timestamp_utc","room_id"]),("work_orders.csv",["work_order_id"]),
                                ("guest_requests.csv",["request_id","booking_id"]),("bookings.csv",["booking_id"]),("staff_roster.csv",["shift_id"])]:
            csv_file(root/filename,fields)
        decision={"event_id":"D_"+name,"timestamp_utc":"2026-01-01T00:15:00Z","affected_assets":["BUILDING"],"issue_type":"fixture",
                  "action":"flag_anomaly","priority":"low","reason":"Fixture consistency review","requires_human_approval":False,
                  "expected_outcome":"SIMULATED review","verification_plan":"Check source","evidence":["water_main.csv:source"]}
        audit={"decision_id":decision["event_id"],"timestamp_utc":decision["timestamp_utc"],"action":"flag_anomaly","status":"simulated",
               "simulated":True,"effects_applied":False,"observed_history_changed":False,"parameters":{"entry_required":False,"room_id":None}}
        (output/("f5_"+name+"_decisions.jsonl")).write_text(json.dumps(decision)+"\n")
        (output/("f5_"+name+"_audit.jsonl")).write_text(json.dumps(audit)+"\n")
        (output/("f5_"+name+"_decisions_rejections.jsonl")).write_text("")
        diagnostics={"ticks_processed":2,"main_complete_intervals":1}
        json_file(output/("f5_"+name+"_run.json"),{"events":2,"decisions":1,"safety_rejections":0,"elapsed_seconds":.001,"config":config,"detector_diagnostics":{"water":diagnostics}})
        raw=raw_completeness(root);counts=raw["counts"]
        exposure={"main_latest_pair_complete_ticks":counts["latest_pair_complete_ticks"],"main_strict_1h_complete_ticks":counts["strict_1h_complete_ticks"],
                  "main_latest_pair_available_but_1h_blocked_ticks":counts["latest_pair_available_but_1h_blocked_ticks"],"room_negative_delta":0,"room_missing_meter_value":0}
        profiles.append({"dataset":manifest,"config":config,"partial_run":False,"rule_provenance":{
            "baseline_commit":"a"*40,"baseline_water_source_sha256":hashlib.sha256(baseline).hexdigest(),
            "candidate_water_source_sha256":digest(code/"engine/detectors/water.py"),"candidate_detector_sources_sha256":sources,
            "config_source_sha256":digest(code/"engine/core.py"),"shared_episode_tracker_source_sha256":digest(code/"engine/decisions.py"),"shared_planner_source_sha256":digest(code/"engine/planner.py")},
            "source_sha256":{filename:digest(root/filename) for filename in refresh.REQUIRED_SOURCES},"exposure":{"counts":exposure},
            "rules":{"baseline":{"episode_emissions_by_path":{}},"candidate":{"episode_emissions_by_path":{},"diagnostics":{"detector":diagnostics}}},"paired_comparison":{}})
    json_file(output/"f5_paired_evaluation.json",{"profiles_are_independent":True,"evaluator_source_sha256":digest(code/"scripts/evaluate_water.py"),"profiles":profiles})
    return data,output,review


def test_refresh_reproduces_44_checks_without_running_replay(artifacts):
    data,output,review=artifacts
    result=refresh.refresh_artifacts(data,output,review)
    assert result["all_checks_passed"] and result["check_count"]==44
    assert result["no_full_replay_performed"] is True
    assert result["test_log"]["passed_count"] is None
    assert (review/"f5_evaluation.json").read_bytes()==(output/"f5_paired_evaluation.json").read_bytes()
    assert json.loads((review/"f5_completeness.json").read_text())["profiles_are_independent"] is True
    assert all(json.loads((review/("f5_"+name+"_validation.json")).read_text())["valid"] for name in refresh.PROFILES)


def test_code_drift_refuses_publication_and_leaves_old_review_unchanged(artifacts):
    data,output,review=artifacts;review.mkdir();sentinel=review/"f5_artifact_checks.json";sentinel.write_text("old publication")
    paired=json.loads((output/"f5_paired_evaluation.json").read_text())
    paired["profiles"][0]["rule_provenance"]["candidate_water_source_sha256"]="0"*64
    json_file(output/"f5_paired_evaluation.json",paired)
    with pytest.raises(refresh.ArtifactCheckError,match="water_source_matches"):
        refresh.refresh_artifacts(data,output,review)
    assert sentinel.read_text()=="old publication"
    assert list(review.iterdir())==[sentinel]


def test_failed_independent_validation_cannot_publish_partial_review(artifacts):
    data,output,review=artifacts
    path=output/"f5_dev_audit.jsonl";audit=json.loads(path.read_text());audit["effects_applied"]=True;path.write_text(json.dumps(audit)+"\n")
    with pytest.raises(refresh.ArtifactCheckError,match="independent_output_validator_passed"):
        refresh.refresh_artifacts(data,output,review)
    assert not review.exists()


def test_preflight_only_does_not_reanalyse_raw_csv_or_write_anything(artifacts,monkeypatch):
    data,output,review=artifacts
    monkeypatch.setattr(refresh,"raw_completeness",lambda root:(_ for _ in ()).throw(AssertionError("must not run")))
    result=refresh.refresh_artifacts(data,output,review,preflight_only=True)
    assert result["preflight_passed"] and result["full_checks_not_run"]
    assert not review.exists()


def test_testlog_only_reports_the_actual_final_summary(tmp_path):
    path=tmp_path/"tests.txt"
    assert refresh.read_test_log(path)["passed_count"] is None
    path.write_text("earlier 3 passed in0.2s\n3 failed, 12 passed in 0.4s\n")
    report=refresh.read_test_log(path)
    assert report["status"]=="failed" and report["passed_count"]==12
    path.write_text("..................\n17 passed in 0.10s\n")
    report=refresh.read_test_log(path)
    assert report["status"]=="reported_pass" and report["passed_count"]==17


def test_planner_source_drift_refuses_publication(artifacts):
    data,output,review=artifacts
    paired=json.loads((output/"f5_paired_evaluation.json").read_text())
    paired["profiles"][0]["rule_provenance"]["shared_planner_source_sha256"]="0"*64
    json_file(output/"f5_paired_evaluation.json",paired)
    with pytest.raises(refresh.ArtifactCheckError,match="shared_planner_source_hash_matches_current"):
        refresh.refresh_artifacts(data,output,review)
    assert not review.exists()
