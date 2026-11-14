"""Failure of an optional semantic helper must remain a conservative fallback."""
import pytest

from tests.ingestion.test_guest_parser import parser


@pytest.mark.parametrize('method,reason', [
    ('source_label','offline_mode'), ('source_label','missing_api_key'),
    ('fallback','model_or_validation_error'),
])
def test_semantic_helper_failure_cannot_escape_or_certify_fallback(parser, monkeypatch, method, reason):
    def broken(*args):
        raise RuntimeError('injected semantic helper failure')
    monkeypatch.setattr(parser, 'declined_intents', broken)
    items, meta = parser._fallback({'request_type':'waiting_for_room', 'language':'en',
                                  'text':'Please clarify the arrival arrangements.'}, method, reason)
    assert len(items)==1 and items[0]['intent']=='waiting_for_room'
    assert items[0]['needs_review'] and items[0]['requested_time_utc'] is None
    assert meta.method==method and meta.needs_review
    assert 'semantic_validation_unavailable' in meta.uncertain_fields
    assert reason in meta.uncertain_fields
