"""Refresh F5 review artifacts from completed, frozen runs; never runs replay.

After root has produced outputs/f5_{quickstart,dev}_{decisions,audit,run}.json[l]
and outputs/f5_paired_evaluation.json:

  python -m scripts.refresh_f5_artifacts --preflight-only
  python -m scripts.refresh_f5_artifacts

The command independently rereads raw CSV interval availability and validates the
existing outputs. It refuses to publish if source/config/data hashes drift, tests
report failure, or any of the 44 per-profile checks fail. The manifest is replaced
last. Consumers must verify every artifact hash against that manifest; an I/O
failure during replacement can leave an incomplete generation. No decisions,
audit logs, detector code or primary submission are changed.
"""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

from engine.core import EngineConfig
from engine.validation import validate_outputs
from scripts.evaluate_water import REPO_ROOT, raw_completeness

PROFILES = ("quickstart", "dev")
REQUIRED_CODE = {"water.py":"engine/detectors/water.py", "weak_water.py":"engine/detectors/weak_water.py", "core.py":"engine/core.py", "planner.py":"engine/planner.py"}
REQUIRED_SOURCES = {"manifest.json","building.json","room_water.csv","water_main.csv","room_telemetry.csv","events.jsonl","work_orders.csv"}


class ArtifactCheckError(ValueError):
    def __init__(self, failures):
        self.failures = failures
        super().__init__("F5 artifact refresh refused: "+json.dumps(failures,ensure_ascii=False))


class _Hashes:
    """Pin the first observed bytes; never accept a newer input mid-refresh."""
    def __init__(self):
        self.snapshots = {}

    @staticmethod
    def _stamp(stat):
        return (stat.st_dev,stat.st_ino,stat.st_size,stat.st_mtime_ns,stat.st_ctime_ns)

    def __call__(self,path):
        path = Path(path).resolve()
        stamp = self._stamp(path.stat())
        old = self.snapshots.get(path)
        if old:
            if old[0] != stamp:
                raise ArtifactCheckError([{"check":"input_changed_during_refresh","path":str(path)}])
            return old[1]
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream,"sha256").hexdigest()
        if stamp != self._stamp(path.stat()):
            raise ArtifactCheckError([{"check":"input_changed_during_hash","path":str(path)}])
        self.snapshots[path] = (stamp,digest)
        return digest

    def read_bytes(self,path):
        expected = self(path)
        content = Path(path).read_bytes()
        if hashlib.sha256(content).hexdigest() != expected:
            raise ArtifactCheckError([{"check":"input_changed_during_read","path":str(path)}])
        self(path)
        return content

    def ensure_stable(self):
        drift = []
        for path,(stamp,digest) in self.snapshots.items():
            try:
                before = self._stamp(path.stat())
                with path.open("rb") as stream:
                    actual = hashlib.file_digest(stream,"sha256").hexdigest()
                after = self._stamp(path.stat())
                stable = before == stamp == after and actual == digest
            except OSError:
                stable = False
            if not stable:
                drift.append({"check":"input_changed_during_refresh","path":str(path)})
        if drift:
            raise ArtifactCheckError(drift)


def _json_bytes(value):
    return (json.dumps(value,ensure_ascii=False,indent=2)+"\n").encode("utf-8")


def _json(path,hashes):
    return json.loads(hashes.read_bytes(path).decode("utf-8"))


def _baseline_blob(revision):
    if not isinstance(revision,str) or re.fullmatch(r"[0-9a-f]{40}",revision) is None:
        raise ArtifactCheckError([{"check":"baseline_commit_format"}])
    result = subprocess.run(["git","show",revision+":engine/detectors/water.py"],cwd=REPO_ROOT,capture_output=True)
    if result.returncode:
        raise ArtifactCheckError([{"check":"baseline_commit_unavailable","commit":revision,
                                   "detail":"Fetch the pinned baseline history before refreshing."}])
    return result.stdout


def read_test_log(path,hashes=None):
    """Report only counts actually present in the final pytest summary line."""
    path = Path(path)
    if not path.is_file():
        return {"present":False,"status":"not_available","passed_count":None,"summary":None}
    hashes = hashes or _Hashes()
    text = hashes.read_bytes(path).decode("utf-8",errors="replace")
    text = re.sub(r"\x1b\[[0-9;]*[A-Za-z]","",text)
    summaries = [line.strip().strip("= ") for line in text.splitlines()
                 if re.search(r"(?:\d+\s+(?:passed|failed|errors?|skipped|xfailed|xpassed|warnings?)|no tests ran).*\bin\s+\d",line)]
    summary = summaries[-1] if summaries else None
    match = re.search(r"\b(\d+)\s+passed\b",summary or "")
    failed = bool(re.search(r"\b\d+\s+(?:failed|errors?)\b",summary or ""))
    return {"present":True,"path":str(path),"sha256":hashes(path),"summary":summary,
            "passed_count":int(match.group(1)) if match else None,
            "status":"failed" if failed else "reported_pass" if match else "unrecognised_summary"}


def _preflight(data_root,output_dir,paired_path,hashes):
    paired = _json(paired_path,hashes)
    profiles = paired.get("profiles",[])
    names = [p.get("dataset",{}).get("profile") for p in profiles]
    failures = []
    if sorted(names) != sorted(PROFILES):
        failures.append({"check":"exactly_two_independent_profiles","observed":names})
    if paired.get("profiles_are_independent") is not True:
        failures.append({"check":"profiles_independent_marker"})
    evaluator = REPO_ROOT/"scripts/evaluate_water.py"
    if paired.get("evaluator_source_sha256") != hashes(evaluator):
        failures.append({"check":"paired_evaluator_source_hash_matches_current"})
    current_code = {name:hashes(REPO_ROOT/path) for name,path in REQUIRED_CODE.items()}
    current_detectors = {p.name:hashes(p) for p in (REPO_ROOT/"engine/detectors").glob("*.py")}
    inputs = {}
    for evaluation in profiles:
        name = evaluation.get("dataset",{}).get("profile")
        if name not in PROFILES or name in inputs:
            continue
        root = data_root/("PLATO40_v2_"+name)
        source = evaluation["rule_provenance"]
        if source.get("shared_planner_source_sha256") != current_code["planner.py"]:
            failures.append({"profile":name,"check":"shared_planner_source_hash_matches_current"})
        run_path = output_dir/("f5_"+name+"_run.json")
        run = _json(run_path,hashes)
        rejections_path = output_dir/("f5_"+name+"_decisions_rejections.jsonl")
        rejection_bytes = hashes.read_bytes(rejections_path)
        relevant = {
            "water_source_matches":source.get("candidate_water_source_sha256") == current_code["water.py"],
            "weak_source_matches":source.get("candidate_detector_sources_sha256",{}).get("weak_water.py") == current_code["weak_water.py"],
            "core_source_matches":source.get("config_source_sha256") == current_code["core.py"],
            "all_detector_source_hashes_match":source.get("candidate_detector_sources_sha256") == current_detectors,
            "shared_episode_tracker_matches":source.get("shared_episode_tracker_source_sha256") == hashes(REPO_ROOT/"engine/decisions.py"),
            "paired_raw_source_hashes_match":set(evaluation.get("source_sha256",{})) == REQUIRED_SOURCES
                and all(hashes(root/filename) == digest for filename,digest in evaluation["source_sha256"].items()),
            "full_run_config_equals_paired_config":run.get("config") == evaluation.get("config"),
            "full_run_config_equals_current_defaults":run.get("config") == asdict(EngineConfig()),
            "full_run_water_diagnostics_equal_paired_candidate":run.get("detector_diagnostics",{}).get("water") == evaluation.get("rules",{}).get("candidate",{}).get("diagnostics",{}).get("detector"),
            "full_run_no_safety_rejections":run.get("safety_rejections") == 0,
            "rejections_file_empty":not rejection_bytes,
            "paired_run_not_partial":evaluation.get("partial_run") is False,
            "baseline_blob_hash_matches_pinned_git":hashlib.sha256(_baseline_blob(source["baseline_commit"])).hexdigest() == source.get("baseline_water_source_sha256"),
        }
        for check,passed in relevant.items():
            if not passed:
                failures.append({"profile":name,"check":check})
        inputs[name] = {"evaluation":evaluation,"data_dir":root,"run":run,"run_path":run_path,"checks":relevant}
    commits = {p.get("rule_provenance",{}).get("baseline_commit") for p in profiles}
    if len(commits) != 1:
        failures.append({"check":"shared_pinned_baseline_commit"})
    test_info = read_test_log(output_dir/"f5_tests.txt",hashes)
    if test_info["present"] and test_info["status"] != "reported_pass":
        failures.append({"check":"test_log_final_summary","status":test_info["status"]})
    if failures:
        raise ArtifactCheckError(failures)
    return paired,inputs,current_code,test_info


def refresh_artifacts(data_root=Path("data"),output_dir=Path("outputs"),review_dir=Path("docs/reviews"),paired_path=None,preflight_only=False):
    data_root,output_dir,review_dir = Path(data_root),Path(output_dir),Path(review_dir)
    paired_path = Path(paired_path) if paired_path else output_dir/"f5_paired_evaluation.json"
    if review_dir.resolve().is_relative_to(data_root.resolve()):
        raise ValueError("Review output directory must be outside source datasets")
    hashes = _Hashes()
    paired,inputs,current_code,test_info = _preflight(data_root,output_dir,paired_path,hashes)
    if preflight_only:
        hashes.ensure_stable()
        return {"preflight_passed":True,"full_checks_not_run":True,"artifacts_written":False,
                "test_log":test_info,"scope":"No raw re-analysis, output validation or replay was run."}
    reports = {};raw_profiles=[];files={};checks=[]
    for name in PROFILES:
        item = inputs[name]
        evaluation,root,run = item["evaluation"],item["data_dir"],item["run"]
        decisions_path,audit_path = output_dir/("f5_"+name+"_decisions.jsonl"),output_dir/("f5_"+name+"_audit.jsonl")
        validation_paths = {"decisions":decisions_path,"audit":audit_path,
                            **{filename:root/filename for filename in ("building.json","events.jsonl","manifest.json","guest_requests.csv","bookings.csv","staff_roster.csv") if (root/filename).is_file()}}
        for path in validation_paths.values():
            hashes(path)
        validation = validate_outputs(root,decisions_path,audit_path)
        validation_bytes = _json_bytes(validation)
        files["f5_"+name+"_validation.json"] = validation_bytes
        raw = raw_completeness(root)
        raw_profiles.append(raw)
        relevant = dict(item["checks"])
        relevant.update({
            "independent_raw_source_hashes_match":all(hashes(root/filename) == digest for filename,digest in raw["source_sha256"].items()),
            "full_run_decision_count_equals_validated_rows":run.get("decisions") == validation.get("stats",{}).get("decision_rows"),
            "independent_output_validator_passed":validation.get("valid") is True and validation.get("error_count") == 0,
            "validated_outputs_match_current_files":all(validation.get("hashes_sha256",{}).get(key) == hashes(path)
                for key,path in validation_paths.items()),
        })
        a,b = evaluation["exposure"]["counts"],raw["counts"]
        for left,right in [("main_latest_pair_complete_ticks","latest_pair_complete_ticks"),("main_strict_1h_complete_ticks","strict_1h_complete_ticks"),
                           ("main_latest_pair_available_but_1h_blocked_ticks","latest_pair_available_but_1h_blocked_ticks"),
                           ("room_negative_delta","room_negative_intervals"),("room_missing_meter_value","room_intervals_touching_missing_value")]:
            relevant["raw_crosscheck_"+left] = a.get(left,0) == b.get(right,0)
        for check,passed in relevant.items():
            checks.append({"profile":name,"check":check,"passed":bool(passed)})
        issues,actions = Counter(),Counter()
        with decisions_path.open(encoding="utf-8") as stream:
            for line in stream:
                row=json.loads(line);issues[row["issue_type"]]+=1;actions[row["action"]]+=1
        reports[name] = {"checks":relevant,"run":{k:run.get(k) for k in ("events","decisions","safety_rejections","elapsed_seconds")},
            "output_validation":validation.get("stats",{}),"issue_type_counts":dict(issues),"action_counts":dict(actions),
            "paired_baseline_emissions":evaluation["rules"]["baseline"]["episode_emissions_by_path"],
            "paired_candidate_emissions":evaluation["rules"]["candidate"]["episode_emissions_by_path"],
            "paired_window_change":evaluation["paired_comparison"],"main_interval_availability":{k:v for k,v in a.items() if k.startswith("main_")},
            "artifact_sha256":{"decisions":hashes(decisions_path),"audit":hashes(audit_path),"run_metadata":hashes(item["run_path"]),
                               "validation":hashlib.sha256(validation_bytes).hexdigest()}}
    failed = [check for check in checks if not check["passed"]]
    if failed:
        raise ArtifactCheckError(failed)
    raw_report = {"evaluation_version":"1.0","evaluation_mode":"raw_csv_crosscheck",
        "scope":"Independent raw CSV interval availability and signed-quality counts; no leak truth, ground-truth precision/recall or measured savings.",
        "evaluator_source_sha256":hashes(REPO_ROOT/"scripts/evaluate_water.py"),"profiles_are_independent":True,"profiles":raw_profiles}
    raw_bytes = _json_bytes(raw_report)
    paired_bytes = hashes.read_bytes(paired_path)
    if hashlib.sha256(paired_bytes).hexdigest() != hashes(paired_path):
        raise ArtifactCheckError([{"check":"paired_file_changed_before_copy"}])
    files["f5_evaluation.json"] = paired_bytes
    files["f5_completeness.json"] = raw_bytes
    result = {"verification_version":"1.1","checked_at_utc":datetime.now(timezone.utc).isoformat().replace("+00:00","Z"),
        "all_checks_passed":True,"check_count":len(checks),"source_hash_drift":False,
        "scope":"Independent F5 output consistency and unlabelled empirical/provenance checks, not ground-truth recall/precision, confirmed-leak truth or real savings.",
        "refresh_command":"python -m scripts.refresh_f5_artifacts","no_full_replay_performed":True,
        "current_required_code_sha256":current_code,"baseline_commit":next(iter(inputs.values()))["evaluation"]["rule_provenance"]["baseline_commit"],
        "full_run_source_provenance_limit":"Full-run JSON does not emit runtime source hashes. Evaluated water/core/weak snapshots and the full-pipeline planner snapshot match the current tree; full-run config and every water diagnostic counter match the paired candidate. This is cross-artifact consistency evidence, not signed runtime build attestation.",
        "current_verifier_sha256":{"validation.py":hashes(REPO_ROOT/"engine/validation.py"),"evaluate_water.py":hashes(REPO_ROOT/"scripts/evaluate_water.py"),
                                   "refresh_f5_artifacts.py":hashes(Path(__file__))},
        "test_log":test_info,"evaluation_copies":{"paired_sha256":hashlib.sha256(paired_bytes).hexdigest(),
                "raw_sha256":hashlib.sha256(raw_bytes).hexdigest(),"paired_byte_identical":True,"raw_generated_independently_during_refresh":True},
        "profiles":reports,"checks":checks}
    files["f5_artifact_checks.json"] = _json_bytes(result)
    hashes.ensure_stable()
    # Stage everything, then replace the manifest last as the publication marker.
    review_dir.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".f5-refresh-",dir=review_dir) as temporary:
        staging=Path(temporary)
        for name,content in files.items():
            (staging/name).write_bytes(content)
        for name in files:
            if name != "f5_artifact_checks.json":
                os.replace(staging/name,review_dir/name)
        os.replace(staging/"f5_artifact_checks.json",review_dir/"f5_artifact_checks.json")
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root",type=Path,default=Path("data"))
    parser.add_argument("--output-dir",type=Path,default=Path("outputs"))
    parser.add_argument("--review-dir",type=Path,default=Path("docs/reviews"))
    parser.add_argument("--paired",type=Path)
    parser.add_argument("--preflight-only",action="store_true")
    args=parser.parse_args()
    try:
        report=refresh_artifacts(args.data_root,args.output_dir,args.review_dir,args.paired,args.preflight_only)
    except (ArtifactCheckError,OSError,ValueError,KeyError,TypeError) as exc:
        print(json.dumps({"all_checks_passed":False,"publication_complete":False,
                          "artifacts_may_have_changed":isinstance(exc,OSError),"error":str(exc)},ensure_ascii=False),file=sys.stderr)
        return 1
    print(json.dumps({"preflight_only":args.preflight_only,"checks":report.get("check_count"),
                      "passed":report.get("all_checks_passed",report.get("preflight_passed")),
                      "no_full_replay_performed":True,"review_dir":str(args.review_dir)},ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
