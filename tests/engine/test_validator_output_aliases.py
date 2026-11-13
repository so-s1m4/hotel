"""Validator CLI must not truncate an input through an external inode alias."""
import os
import subprocess
import sys
import pytest
from tests.engine.test_output_validation import files, write_jsonl


@pytest.mark.parametrize('target', ['decisions', 'audit', 'building.json', 'events.jsonl', 'manifest.json', 'guest_requests.csv', 'bookings.csv', 'staff_roster.csv'])
def test_hardlinked_report_preserves_every_source_byte(files, tmp_path, target):
    root, decisions, audit = files
    write_jsonl(decisions, [])
    write_jsonl(audit, [])
    source = decisions if target == 'decisions' else audit if target == 'audit' else root/target
    output = tmp_path/'report.json'
    os.link(source, output)
    inputs = [decisions, audit, *root.iterdir()]
    before = {path: path.read_bytes() for path in inputs}
    report_before = output.read_bytes()
    result = subprocess.run([sys.executable, '-m', 'engine.validation', '--data-dir', str(root), '--decisions', str(decisions), '--audit', str(audit), '--out', str(output)], capture_output=True, text=True)
    assert result.returncode != 0
    assert 'must not overwrite' in result.stderr
    assert {path: path.read_bytes() for path in inputs} == before
    assert output.read_bytes() == report_before
    assert '"valid": true' not in result.stdout


def test_existing_separate_report_can_be_refreshed(files, tmp_path):
    root, decisions, audit = files
    write_jsonl(decisions, [])
    write_jsonl(audit, [])
    output = tmp_path/'report.json'
    output.write_text('old report')
    result = subprocess.run([sys.executable, '-m', 'engine.validation', '--data-dir', str(root), '--decisions', str(decisions), '--audit', str(audit), '--out', str(output)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    import json
    assert json.loads(output.read_text())['valid'] is True
    assert decisions.read_bytes() == audit.read_bytes() == b''
