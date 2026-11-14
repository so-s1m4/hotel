"""End-to-end import-failure boundary controls; no model/network."""
import builtins
import csv

import pytest

from engine.planner import DecisionEngine
from ingestion.events import iter_normalized_events
from ingestion.replay import ReplayData
from tests.ingestion.test_local_parser_end_to_end import local_dataset


@pytest.mark.parametrize('use_llm', [True, None, False])
@pytest.mark.parametrize('refusal', [False, True])
def test_parser_import_failure_does_not_impersonate_explicit_offline_policy(local_dataset, monkeypatch, use_llm, refusal):
    root, building = local_dataset
    if refusal:
        path = root / 'guest_requests.csv'
        with path.open(newline='') as stream:
            reader = csv.DictReader(stream)
            fields, rows = reader.fieldnames, list(reader)
        for row in rows:
            if row['request_id'] == 'WAIT':
                row['text'] = 'We do not want an early check-in. Keep our standard arrival arrangements.'
        with path.open('w', newline='') as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
    original_import = builtins.__import__
    attempts = []

    def fail_guest_parser(name, globals=None, locals=None, fromlist=(), level=0):
        if level == 1 and 'guest_parser' in (fromlist or ()) and (globals or {}).get('__package__') == 'ingestion':
            attempts.append((name, level, tuple(fromlist)))
            raise ImportError('injected unavailable optional guest parser')
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, '__import__', fail_guest_parser)
    repo = ReplayData(root, use_llm=use_llm)
    events = list(iter_normalized_events(repo))
    assert attempts
    guests = {event.source.record_id: event for event in events if event.event_type == 'guest_request'}
    assert set(guests) == {'WAIT', 'OCCUPIED', 'NO_DATE'}
    expected_method = 'source_label' if use_llm is False else 'fallback'
    assert all(event.extraction.method == expected_method and event.extraction.needs_review for event in guests.values())
    assert all(item['requested_time_utc'] is None for event in guests.values() for item in event.payload['intents'])
    if refusal:
        assert 'semantic_negation' in guests['WAIT'].extraction.uncertain_fields
    subject = DecisionEngine(repo, building)
    decisions = subject.run()
    if use_llm is False and not refusal:
        assert any(row.action == 'schedule_cleaning' for row in decisions)
        assert any(subject.calendar.tentative_reservations.values())
    else:
        assert not any(row.action in {'schedule_cleaning', 'request_inspection', 'dispatch_robot', 'release_room'} for row in decisions)
        assert not any(subject.calendar.reservations.values())
        assert not any(subject.calendar.tentative_reservations.values())
        assert any(row.action == 'notify_guest' and 'unverified' in row.reason for row in decisions)
    assert not subject.rejections
    assert all(not row['effects_applied'] for row in subject.executor.audit)
