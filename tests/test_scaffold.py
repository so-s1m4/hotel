"""Scaffold checks run offline and do not require participant datasets."""
import inspect
import json
from pathlib import Path
import subprocess
import sys
import zipfile

import pytest

from common.contracts.api import ReplayDataGateway
from common.contracts.schemas import NormalizedEventV1
from ingestion.events import iter_normalized_events
from ingestion.loader import REQUIRED_FILES, inspect_dataset
from ingestion.replay import ReplayData
from scripts.prepare_data import prepare

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def dataset(tmp_path):
    for name in REQUIRED_FILES:
        (tmp_path / name).touch()
    (tmp_path / "manifest.json").write_text(json.dumps({
        "model_id": "TEST", "profile": "fixture", "start_utc": "2026-01-01T00:00:00Z",
        "end_utc": "2026-01-02T00:00:00Z"}))
    (tmp_path / "building.json").write_text('{"building":{"rooms":[]}}')
    return tmp_path


def test_public_gateway_signatures_are_preserved():
    for name, method in inspect.getmembers(ReplayDataGateway, inspect.isfunction):
        if name.startswith("_"):
            continue
        assert inspect.signature(getattr(ReplayData, name)) == inspect.signature(method)


def test_missing_inputs_report_filenames(tmp_path):
    with pytest.raises(ValueError, match="guest_requests.csv"):
        inspect_dataset(tmp_path)


def test_preflight_does_not_create_decisions(dataset):
    output = dataset / "decisions.jsonl"
    result = subprocess.run([sys.executable, str(ROOT / "run.py"), "--data-dir", str(dataset),
                             "--check", "--out", str(output)], capture_output=True, text=True)
    assert result.returncode == 0
    assert json.loads(result.stdout)["status"] == "preflight_ok"
    assert not output.exists()


def test_unimplemented_pipeline_fails_without_touching_output(dataset, tmp_path_factory):
    output = tmp_path_factory.mktemp("pipeline_output") / "decisions.jsonl"
    output.write_text("existing result\n")
    result = subprocess.run([sys.executable, str(ROOT / "run.py"), "--data-dir", str(dataset),
                             "--out", str(output)], capture_output=True, text=True)
    assert result.returncode == 2
    assert "not implemented" in result.stderr
    assert output.read_text() == "existing result\n"


def test_engine_can_consume_contract_fixture_without_ingestion():
    event = NormalizedEventV1.model_validate_json(
        (ROOT / "common/examples/normalized_event_early_checkin.json").read_text())

    class FixtureGateway:
        def iter_events(self):
            return iter([event])

    assert list(iter_normalized_events(FixtureGateway())) == [event]


def test_archive_paths_cannot_escape_destination(tmp_path):
    with zipfile.ZipFile(tmp_path / "PLATO40_v2_quickstart.zip", "w") as archive:
        archive.writestr("PLATO40_v2_quickstart/../../escaped.txt", "bad")
    with pytest.raises(ValueError, match="Unsafe"):
        prepare(tmp_path, tmp_path / "data", "quickstart")
    assert not (tmp_path / "escaped.txt").exists()


def test_extraction_is_idempotent_and_preserves_modified_inputs(tmp_path):
    with zipfile.ZipFile(tmp_path / "PLATO40_v2_quickstart.zip", "w") as archive:
        archive.writestr("PLATO40_v2_quickstart/manifest.json", "original")
    result = prepare(tmp_path, tmp_path / "data", "quickstart")
    assert prepare(tmp_path, tmp_path / "data", "quickstart") == result
    (result / "manifest.json").write_text("edited")
    with pytest.raises(ValueError, match="Refusing to overwrite"):
        prepare(tmp_path, tmp_path / "data", "quickstart")
    assert (result / "manifest.json").read_text() == "edited"
