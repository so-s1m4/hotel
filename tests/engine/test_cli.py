import json
from pathlib import Path
import sys

from common.contracts.schemas import DecisionRecordV2
from ingestion.loader import REQUIRED_FILES
import ingestion.replay
from engine.core import instant
from run import main
from tests.engine.fixtures import BUILDING, energy_repo, water_repo, tick, request, state, slot


def test_one_command_pipeline_on_test_gateway_covers_all_three_scenarios(tmp_path, monkeypatch, capsys):
    source = tmp_path / "source"
    source.mkdir()
    for name in REQUIRED_FILES:
        (source / name).touch()
    metadata = {"profile": "test_fixture", "model_id": "PLATO-40", "start_utc": "2026-01-06T00:00:00Z", "end_utc": "2026-01-07T00:00:00Z"}
    (source / "manifest.json").write_text(json.dumps(metadata))
    (source / "building.json").write_text(json.dumps(BUILDING))
    repo = energy_repo()
    water = water_repo()
    repo.measurements.update(water.measurements)
    repo.states["R101"] = water.states["R101"] + repo.states["R101"]
    repo.states["R102"] = [state(room="R102")]
    repo.slots = [slot()]
    repo.stream = [tick("2026-01-06T02:00:00Z"), request(room="R102"), tick()]
    monkeypatch.setattr(ingestion.replay, "ReplayData", lambda _, **kwargs: repo)
    out, insights = tmp_path / "decisions.jsonl", tmp_path / "insights.md"
    monkeypatch.setattr(sys, "argv", ["run.py", "--data-dir", str(source), "--out", str(out), "--insights", str(insights)])
    assert main() == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["status"] == "replay_complete" and summary["safety_rejections"] == 0
    rows = [DecisionRecordV2.model_validate_json(line) for line in out.read_text().splitlines()]
    assert {"room_turnaround", "water_leak", "heating_comfort"} <= {r.issue_type for r in rows}
    assert [(instant(r.timestamp_utc), r.event_id) for r in rows] == sorted((instant(r.timestamp_utc), r.event_id) for r in rows)
    assert all(r.event_id not in {e.event_id for e in repo.stream} for r in rows)
    audit = [json.loads(line) for line in (tmp_path / "decisions_audit.jsonl").read_text().splitlines()]
    assert len(audit) == len(rows) == 5
    assert all(a["simulated"] and not a["observed_history_changed"] for a in audit)
    assert any(a["status"] == "pending_human_approval" for a in audit)
    assert "test_fixture" in insights.read_text()
    assert "not ground-truth" in insights.read_text()
