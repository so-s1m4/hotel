from datetime import datetime
import json
from pathlib import Path
import pytest
from pydantic import ValidationError
from common.contracts.schemas import DecisionRecordV2, NormalizedEventV1

HERE = Path(__file__).resolve().parents[1]


def test_example_event():
    e = json.loads((HERE / 'examples' / 'normalized_event_early_checkin.json').read_text())
    obj = NormalizedEventV1.model_validate(e)
    assert obj.entities.booking_id == 'BK000173'
    assert obj.payload['request_type'] == 'early_checkin'
    assert obj.payload['intents'][0]['requested_time_utc'] == '2026-01-06T12:00:00Z'


def test_example_decision():
    d = json.loads((HERE / 'examples' / 'decision_example.json').read_text())
    obj = DecisionRecordV2.model_validate(d)
    assert obj.action == 'schedule_cleaning'
    assert obj.requires_human_approval is False


def test_invalid_utc():
    d = json.loads((HERE / 'examples' / 'decision_example.json').read_text())
    d['timestamp_utc'] = '2026-01-06T13:00:00+01:00'
    with pytest.raises(ValidationError):
        DecisionRecordV2.model_validate(d)


def test_guest_consent_also_human_approval():
    d = json.loads((HERE / 'examples' / 'decision_example.json').read_text())
    d['requires_guest_consent'] = True
    with pytest.raises(ValidationError):
        DecisionRecordV2.model_validate(d)


def test_no_unknown_decision_fields():
    d = json.loads((HERE / 'examples' / 'decision_example.json').read_text())
    d['unrecognized_field'] = 'foo'
    with pytest.raises(ValidationError):
        DecisionRecordV2.model_validate(d)
