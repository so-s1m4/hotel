"""Parser-level credential routing through the documented optional transport."""
import pytest

from tests.ingestion.test_guest_parser import context, intent, llm, parser, record


@pytest.mark.parametrize('endpoint,expected_calls', [
    ('http://api.openai.com/v1', 0),
    ('http://api.openai.com:8080/v1', 0),
    ('https://api.openai.com:444/v1', 0),
    ('https://api.openai.com:443/v1', 1),
    ('https://api.openai.com/v1', 1),
])
def test_provider_alias_requires_exact_origin(parser, record, context, llm, monkeypatch,
                                             endpoint, expected_calls):
    calls = llm({'intents': [intent()]})
    monkeypatch.delenv('LLM_API_KEY')
    monkeypatch.setenv('LLM_BASE_URL', endpoint)
    monkeypatch.setenv('OPENAI_API_KEY', 'synthetic-provider-key')
    monkeypatch.setenv('OPENAI_MODEL', 'synthetic-provider-model')
    _, meta = parser.parse_request_full(record, context)
    assert len(calls) == expected_calls
    assert meta.method == ('llm' if expected_calls else 'source_label')
    if calls:
        assert calls[0][1]['api_key'] == 'synthetic-provider-key'


def test_endpoint_userinfo_is_rejected_before_transport(parser, record, context, llm, monkeypatch):
    calls = llm({'intents': [intent()]})
    monkeypatch.setenv('LLM_BASE_URL', 'http://user:synthetic-secret@localhost:11434/v1')
    _, meta = parser.parse_request_full(record, context)
    assert not calls and meta.method == 'fallback' and meta.needs_review
