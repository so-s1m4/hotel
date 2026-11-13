"""Independent bounded negative publication gates; never touches real run outputs."""
import hashlib
import json
import os
from pathlib import Path

import pytest

import scripts.refresh_f5_artifacts as refresh
from tests.engine.test_f5_artifact_refresh import artifacts, json_file


def old_publication(review):
    review.mkdir()
    sentinel = review/"f5_artifact_checks.json"
    sentinel.write_text("previous verified publication",encoding="utf-8")
    return sentinel


def assert_old_only(review,sentinel):
    assert sentinel.read_text()=="previous verified publication"
    assert list(review.iterdir())==[sentinel]


def test_planner_snapshot_drift_refuses_publication_before_any_replacement(artifacts):
    data,output,review=artifacts
    sentinel=old_publication(review)
    paired_path=output/"f5_paired_evaluation.json"
    paired=json.loads(paired_path.read_text())
    paired["profiles"][0]["rule_provenance"]["shared_planner_source_sha256"]="0"*64
    json_file(paired_path,paired)
    with pytest.raises(refresh.ArtifactCheckError,match="shared_planner_source_hash_matches_current"):
        refresh.refresh_artifacts(data,output,review)
    assert_old_only(review,sentinel)


def test_config_drift_cannot_be_hidden_by_changing_both_run_and_paired_metadata(artifacts):
    data,output,review=artifacts
    sentinel=old_publication(review)
    paired_path=output/"f5_paired_evaluation.json"
    paired=json.loads(paired_path.read_text())
    paired["profiles"][0]["config"]["water_low_min_l_per_interval"]=0.9
    run_path=output/"f5_quickstart_run.json"
    run=json.loads(run_path.read_text())
    run["config"]["water_low_min_l_per_interval"]=0.9
    json_file(paired_path,paired);json_file(run_path,run)
    with pytest.raises(refresh.ArtifactCheckError,match="full_run_config_equals_current_defaults"):
        refresh.refresh_artifacts(data,output,review)
    assert_old_only(review,sentinel)


def test_late_safety_rejection_after_preflight_blocks_publication(artifacts,monkeypatch):
    data,output,review=artifacts
    sentinel=old_publication(review)
    original=refresh.raw_completeness
    changed=False
    def mutate_after_preflight(root):
        nonlocal changed
        result=original(root)
        if not changed:
            changed=True
            (output/"f5_quickstart_decisions_rejections.jsonl").write_text('{"reason":"late rejection"}\n')
        return result
    monkeypatch.setattr(refresh,"raw_completeness",mutate_after_preflight)
    with pytest.raises(refresh.ArtifactCheckError,match="input_changed"):
        refresh.refresh_artifacts(data,output,review)
    assert changed
    assert_old_only(review,sentinel)


def test_same_size_source_change_with_restored_mtime_is_not_silently_rehashed(artifacts,monkeypatch):
    data,output,review=artifacts
    sentinel=old_publication(review)
    path=data/"PLATO40_v2_quickstart/work_orders.csv"
    before=path.stat();old=path.read_bytes()
    replacement=bytes([old[0]^32])+old[1:]
    assert len(replacement)==len(old) and replacement!=old
    original=refresh.raw_completeness
    changed=False
    def mutate_after_preflight(root):
        nonlocal changed
        result=original(root)
        if not changed:
            changed=True
            path.write_bytes(replacement)
            os.utime(path,ns=(before.st_atime_ns,before.st_mtime_ns))
        return result
    monkeypatch.setattr(refresh,"raw_completeness",mutate_after_preflight)
    with pytest.raises(refresh.ArtifactCheckError,match="input_changed"):
        refresh.refresh_artifacts(data,output,review)
    assert changed
    assert_old_only(review,sentinel)


def test_output_drift_after_independent_validation_does_not_publish_stale_hashes(artifacts,monkeypatch):
    data,output,review=artifacts
    sentinel=old_publication(review)
    path=output/"f5_quickstart_decisions.jsonl"
    original=refresh.raw_completeness
    changed=False
    def mutate_after_validation(root):
        nonlocal changed
        result=original(root)
        if not changed:
            changed=True
            # Even a semantically harmless byte change must invalidate its pinned
            # output digest, not get silently adopted as the same verified run.
            path.write_bytes(path.read_bytes()+b"\n")
        return result
    monkeypatch.setattr(refresh,"raw_completeness",mutate_after_validation)
    with pytest.raises(refresh.ArtifactCheckError,match="input_changed"):
        refresh.refresh_artifacts(data,output,review)
    assert changed
    assert_old_only(review,sentinel)


def test_testlog_cannot_attach_old_success_summary_to_new_failed_log(tmp_path):
    path=tmp_path/"tests.txt"
    path.write_text("17 passed in 0.10s\n")
    class ChangeAtSnapshot(refresh._Hashes):
        changed=False
        def __call__(self,target):
            if Path(target)==path and not self.changed:
                self.changed=True
                path.write_text("1 failed, 16 passed in 0.10s\n")
            return super().__call__(target)
    hashes=ChangeAtSnapshot()
    report=refresh.read_test_log(path,hashes)
    assert report["status"]=="failed"
    assert report["passed_count"]==16
    assert report["sha256"]==hashlib.sha256(path.read_bytes()).hexdigest()


def test_read_snapshot_refuses_midread_replacement_instead_of_pinning_changed_json(tmp_path,monkeypatch):
    path=tmp_path/"metadata.json";path.write_text('{"version":1}')
    hashes=refresh._Hashes()
    hashes(path)
    original=Path.read_bytes
    def changed_read(target):
        if target==path:
            target.write_text('{"version":2}')
        return original(target)
    monkeypatch.setattr(Path,"read_bytes",changed_read)
    with pytest.raises(refresh.ArtifactCheckError,match="input_changed"):
        hashes.read_bytes(path)


def test_final_verification_checks_bytes_even_if_stat_identity_appears_unchanged(tmp_path,monkeypatch):
    path=tmp_path/"source.csv";path.write_bytes(b"first-value")
    hashes=refresh._Hashes()
    hashes(path)
    stamp=path.stat()
    path.write_bytes(b"other-value")
    # File identity/clock metadata is an aid, not a substitute for comparing the
    # cryptographic snapshot. Mock identical stats to test the independent guard.
    original=Path.stat
    def same_stat(target,*args,**kwargs):
        return stamp if target==path else original(target,*args,**kwargs)
    monkeypatch.setattr(Path,"stat",same_stat)
    with pytest.raises(refresh.ArtifactCheckError,match="input_changed"):
        hashes.ensure_stable()
