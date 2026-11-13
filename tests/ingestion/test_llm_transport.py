"""No real sockets: origin isolation and private no-redirect opener proofs."""
from io import BytesIO
from unittest.mock import Mock
from urllib import request as urllib_request
from urllib.error import HTTPError

import pytest

from ingestion import llm_transport as transport


@pytest.mark.parametrize('url,expected',[
    ('https://API.OPENAI.COM/v1',('https','api.openai.com',443)),
    ('https://api.openai.com:443/other?q=ignored#ignored',('https','api.openai.com',443)),
    ('http://127.0.0.1:11434/v1',('http','127.0.0.1',11434)),
    ('http://localhost/v1',('http','localhost',80)),
    ('http://[0:0:0:0:0:0:0:1]:11434/v1',('http','::1',11434)),
    ('https://bücher.invalid/v1',('https','xn--bcher-kva.invalid',443)),
])
def test_origin_normalizes_authority_but_ignores_path_query_fragment(url,expected):
    assert transport.origin(url)==expected


@pytest.mark.parametrize('other',[
    'http://api.openai.com/v1','https://api.openai.com:8080/v1',
    'https://api.openai.com.attacker.invalid/v1','https://api.openai.com./v1',
])
def test_same_hostname_is_not_sufficient_when_scheme_port_or_authority_changes(other):
    assert transport.origin('https://api.openai.com/v1')!=transport.origin(other)


@pytest.mark.parametrize('url',[
    '',None,'/v1','ftp://example.invalid/v1','file:///tmp/model',
    'https:///v1','https://:443/v1','https://user:synthetic-secret@example.invalid/v1',
    'https://@example.invalid/v1','https://example.invalid:0/v1',
    'https://example.invalid:65536/v1','https://example.invalid:bad/v1',
    'https://[broken/v1','https://example.invalid\\other/v1',
    'https://exa mple.invalid/v1',' https://example.invalid/v1',
    'https://example.invalid/v1\n','https://example.invalid/\x00v1',
    'https://%65xample.invalid/v1',
])
def test_invalid_or_credential_bearing_origins_fail_without_echoing_endpoint(url):
    with pytest.raises(ValueError) as exc:
        transport.origin(url)
    assert 'synthetic-secret' not in str(exc.value)


@pytest.mark.parametrize('code',[301,302,303,307,308])
@pytest.mark.parametrize('target',[
    'https://other.invalid/landing','https://origin.invalid/another-path','/same-origin-path',
])
def test_redirect_handler_never_builds_or_follows_authenticated_redirects(code,target):
    request=urllib_request.Request('https://origin.invalid/v1/chat/completions',data=b'{}',
        headers={'Authorization':'Bearer synthetic-secret','Content-Type':'application/json'})
    handler=transport.NoRedirectHandler()
    parent=Mock()
    handler.add_parent(parent)
    body=BytesIO(b'body must not be consumed to follow redirect')
    headers={'location':target}
    with pytest.raises(HTTPError) as exc:
        getattr(handler,'http_error_'+str(code))(request,body,code,'Redirect',headers)
    assert exc.value.code==code
    assert request.get_header('Authorization')=='Bearer synthetic-secret'
    parent.open.assert_not_called()
    assert body.tell()==0


def test_direct_redirect_request_hook_also_rejects_cross_origin_forwarding():
    request=urllib_request.Request('https://origin.invalid/v1',headers={'Authorization':'Bearer synthetic-secret'})
    with pytest.raises(HTTPError):
        transport.NoRedirectHandler().redirect_request(request,None,302,'Redirect',{},'https://other.invalid/')


def test_open_llm_uses_a_private_opener_preserving_request_timeout_and_global_state(monkeypatch):
    request=urllib_request.Request('http://127.0.0.1:11434/v1/chat/completions',data=b'{"input":"synthetic"}',
                                  headers={'Authorization':'Bearer local-dummy'})
    original_global=urllib_request._opener
    opener=Mock()
    result=object()
    opener.open.return_value=result
    captured=[]
    def build(*handlers):
        captured.extend(handlers)
        return opener
    monkeypatch.setattr(transport.urllib_request,'build_opener',build)
    monkeypatch.setattr(transport.urllib_request,'install_opener',Mock(side_effect=AssertionError('No global opener mutation')))
    assert transport.open_llm(request,timeout=15) is result
    assert len(captured)==1 and isinstance(captured[0],transport.NoRedirectHandler)
    opener.open.assert_called_once_with(request,timeout=15)
    assert request.data==b'{"input":"synthetic"}'
    assert request.get_header('Authorization')=='Bearer local-dummy'
    assert urllib_request._opener is original_global


def test_invalid_original_endpoint_is_rejected_before_building_or_opening_any_transport(monkeypatch):
    build=Mock(side_effect=AssertionError('Invalid URL must not open transport'))
    monkeypatch.setattr(transport.urllib_request,'build_opener',build)
    request=urllib_request.Request('https://user:synthetic-secret@example.invalid/v1')
    with pytest.raises(ValueError):
        transport.open_llm(request,timeout=15)
    build.assert_not_called()


def test_private_opener_keeps_default_tls_handler_and_has_one_no_redirect_handler():
    # Constructing urllib handlers performs no I/O and uses the default verified
    # HTTPS context; no replacement certificate policy is installed.
    opener=urllib_request.build_opener(transport.NoRedirectHandler())
    redirects=[handler for handler in opener.handlers if isinstance(handler,urllib_request.HTTPRedirectHandler)]
    https=[handler for handler in opener.handlers if isinstance(handler,urllib_request.HTTPSHandler)]
    assert len(redirects)==1 and isinstance(redirects[0],transport.NoRedirectHandler)
    assert len(https)==1
    assert https[0]._context is None  # urllib creates the default verified context
