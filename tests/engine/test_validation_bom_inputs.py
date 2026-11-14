"""Dataset BOMs preserve validation semantics; generated artifacts stay strict."""
from codecs import BOM_UTF8
from hashlib import sha256

import pytest

from engine.validation import validate_outputs
from tests.engine.test_output_validation import audit, decision, files, source, validate


def populated_report(files):
    # Losing the bookings header would merge distinct recipients; losing the
    # requests header would make the third notification's request ID unknown.
    records = [
        decision("BOOKING_A", action="notify_guest", resource=None,
                 evidence=["bookings.csv:B1"], reason="A status update is pending"),
        decision("BOOKING_B", action="notify_guest", resource=None,
                 evidence=["bookings.csv:B2"], reason="A status update is pending"),
        decision("REQUEST", action="notify_guest", resource=None,
                 guest_request_ids=["G1"], reason="Your request awaits clarification"),
    ]
    result = validate(files, records, [audit(row) for row in records], [
        source("OBSERVED", "checkout", "2026-01-06T08:00:00Z",
               resource=None, room="R102"),
    ])
    assert result["valid"] and result["error_count"] == 0
    assert result["stats"]["joined_decisions"] == 3
    assert result["stats"]["duplicate_exact_notifications"] == 0
    return result


def input_paths(files):
    root, decisions_path, audit_path = files
    return {"decisions": decisions_path, "audit": audit_path,
            **{path.name: path for path in root.iterdir()}}


@pytest.mark.parametrize("filename", [
    "building.json", "manifest.json", "events.jsonl",
    "guest_requests.csv", "bookings.csv",
])
def test_source_leading_bom_preserves_validation_and_original_bytes(files, filename):
    plain = populated_report(files)
    path = files[0] / filename
    path.write_bytes(BOM_UTF8 + path.read_bytes())
    paths = input_paths(files)
    before = {name: path.read_bytes() for name, path in paths.items()}

    result = validate_outputs(*files)

    assert {key: value for key, value in result.items() if key != "hashes_sha256"} == {
        key: value for key, value in plain.items() if key != "hashes_sha256"}
    assert result["hashes_sha256"] == {
        name: sha256(raw).hexdigest() for name, raw in before.items()}
    assert {name: path.read_bytes() for name, path in paths.items()} == before


@pytest.mark.parametrize("artifact, issue", [
    ("decisions", "decision_json"), ("audit", "audit_json"),
])
def test_generated_artifact_leading_bom_is_still_rejected(files, artifact, issue):
    populated_report(files)
    paths = input_paths(files)
    path = paths[artifact]
    path.write_bytes(BOM_UTF8 + path.read_bytes())
    before = {name: path.read_bytes() for name, path in paths.items()}

    result = validate_outputs(*files)

    assert not result["valid"]
    assert result["issue_counts"][issue] == 1
    assert result["hashes_sha256"] == {
        name: sha256(raw).hexdigest() for name, raw in before.items()}
    assert {name: path.read_bytes() for name, path in paths.items()} == before
