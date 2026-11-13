"""Injected HTTP integration checks: no real network, providers or local model."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import importlib
import json
import logging
import os
import socket
from threading import Barrier,Event,Lock
from urllib.error import HTTPError

import pytest


RECORD={'request_id':'LOCAL-TRANSPORT-1','timestamp_utc':'2026-01-06T09:00:00Z',
        'room_id':'R101','language':'en','request_type':'water_issue','text':'Water is dripping.'}
CONTEXT={'as_of_utc':RECORD['timestamp_utc'],'use_llm':True,'room_ids':['R101']}
ABSENT=object()


def prediction(record=RECORD):
    return {'intents':[{'intent':record['request_type'],'requested_time_utc':None,
        'requested_time_text':None,'evidence_quote':record['text'],'details':{},'needs_review':False}]}


def envelope(content=None,finish='stop'):
    choice={'message':{'content':json.dumps(prediction()) if content is None else content}}
    if finish is not ABSENT:
        choice['finish_reason']=finish
    return json.dumps({'choices':[choice]}).encode()


class Response:
    def __init__(self,body):
        self.body=body
        self.read_limits=[]
    def __enter__(self):
        return self
    def __exit__(self,*args):
        return False
    def read(self,limit):
        self.read_limits.append(limit)
        return self.body[:limit]


@pytest.fixture
def parser(monkeypatch,tmp_path):
    for name in tuple(os.environ):
        if name.startswith(('LLM_','OPENAI_','OPENROUTER_','CEREBRAS_','GUEST_PARSER_')):
            monkeypatch.delenv(name,raising=False)
    monkeypatch.setenv('LLM_API_KEY','synthetic-local-key')
    monkeypatch.setenv('LLM_MODEL','synthetic-local-model')
    monkeypatch.setenv('LLM_BASE_URL','http://127.0.0.1:11434/v1')
    monkeypatch.setenv('GUEST_PARSER_CACHE_DIR',str(tmp_path/'cache'))
    module=importlib.import_module('ingestion.guest_parser')
    attempted=[]
    def block_network(*args,**kwargs):
        attempted.append(True)
        raise AssertionError('Transport tests must never open a real connection')
    monkeypatch.setattr(module,'_open_llm',block_network)
    monkeypatch.setattr(socket,'create_connection',block_network)
    monkeypatch.setattr(socket.socket,'connect',block_network)
    monkeypatch.setattr(socket.socket,'connect_ex',block_network)
    yield module
    assert not attempted


def install_transport(parser,monkeypatch,body=None):
    calls=[]
    responses=[]
    def transport(request,timeout):
        calls.append({'url':request.full_url,'headers':dict(request.header_items()),
                      'payload':json.loads(request.data),'timeout':timeout})
        response=Response(envelope() if body is None else body)
        responses.append(response)
        return response
    monkeypatch.setattr(parser,'_open_llm',transport)
    return calls,responses


def cached_files(tmp_path):
    return list((tmp_path/'cache').glob('*.json'))


def test_default_transport_keeps_json_object_no_output_cap_and_explicit_timeout(parser,monkeypatch):
    calls,responses=install_transport(parser,monkeypatch)
    result,meta=parser.parse_request_full(deepcopy(RECORD),deepcopy(CONTEXT))
    assert meta.method=='llm' and result[0]['evidence_quote']==RECORD['text']
    assert len(calls)==1
    request=calls[0]
    assert request['url']=='http://127.0.0.1:11434/v1/chat/completions'
    assert request['payload']['response_format']=={'type':'json_object'}
    assert 'max_tokens' not in request['payload']
    assert request['payload']['model']=='synthetic-local-model'
    assert request['payload']['temperature']==0
    assert request['headers']['Authorization']=='Bearer synthetic-local-key'
    assert request['timeout']==15
    assert responses[0].read_limits==[parser.MAX_RESPONSE_BYTES+1]


def test_extraction_profile_leaves_utc_to_host_and_cannot_reuse_contract_cache(parser,monkeypatch):
    record={**RECORD,'request_type':'early_checkin','text':'May I check in tomorrow at 13:00?'}
    reply={'intents':[{'intent':'early_checkin','requested_time_utc':None,
        'requested_time_text':'tomorrow at 13:00','evidence_quote':record['text'],
        'details':{},'needs_review':False}]}
    calls,_=install_transport(parser,monkeypatch,envelope(json.dumps(reply)))
    contract,meta=parser.parse_request_full(record,CONTEXT)
    assert meta.method=='llm' and contract[0]['requested_time_utc']=='2026-01-07T12:00:00Z'
    monkeypatch.setenv('LLM_GENERATION_PROFILE','extraction')
    monkeypatch.setenv('LLM_RESPONSE_FORMAT','json_schema')
    result,meta=parser.parse_request_full(record,CONTEXT)
    assert len(calls)==2 and meta.method=='llm' and not meta.needs_review
    assert result==contract
    schema=calls[-1]['payload']['response_format']['json_schema']['schema']['$defs']['intent']['properties']
    assert schema['requested_time_utc']=={'type':'null'}
    assert schema['details']=={'type':'object','properties':{},'required':[],'additionalProperties':False}
    assert 'Generation profile: extraction only.' in calls[-1]['payload']['messages'][0]['content']
    parser.parse_request_full(record,CONTEXT)
    assert len(calls)==2  # the new profile's own valid cache is reused


def test_schema_generation_is_explicit_opt_in_and_preserves_validated_host_contract(parser,monkeypatch):
    monkeypatch.setenv('LLM_RESPONSE_FORMAT','json_schema')
    monkeypatch.setenv('LLM_MAX_TOKENS','512')
    calls,_=install_transport(parser,monkeypatch)
    result,meta=parser.parse_request_full(RECORD,CONTEXT)
    assert meta.method=='llm'
    wire=calls[0]['payload']
    assert wire['response_format']['type']=='json_schema'
    schema=wire['response_format']['json_schema']
    assert schema['strict'] is True
    assert schema['schema']['type']=='object'
    assert schema['schema']['properties']['intents']['type']=='array'
    assert wire['max_tokens']==512 and type(wire['max_tokens']) is int
    assert result[0]['intent']=='water_issue'
    assert result[0]['evidence_quote']==RECORD['text']


@pytest.mark.parametrize('cap',['1','512','16384'])
def test_positive_bounded_output_caps_reach_actual_http_payload(parser,monkeypatch,cap):
    monkeypatch.setenv('LLM_MAX_TOKENS',cap)
    calls,_=install_transport(parser,monkeypatch)
    _,meta=parser.parse_request_full(RECORD,CONTEXT)
    assert meta.method=='llm'
    assert calls[0]['payload']['max_tokens']==int(cap)


@pytest.mark.parametrize('cap',['0','-1','16385','1.5','NaN','True'])
def test_invalid_cap_falls_back_without_any_http_or_cache_write(parser,monkeypatch,tmp_path,cap):
    monkeypatch.setenv('LLM_MAX_TOKENS',cap)
    result,meta=parser.parse_request_full(RECORD,CONTEXT)
    assert meta.method=='fallback'
    assert result[0]['needs_review'] is True
    assert not cached_files(tmp_path)


def test_invalid_format_falls_back_without_any_http_or_cache_write(parser,monkeypatch,tmp_path):
    monkeypatch.setenv('LLM_RESPONSE_FORMAT','yaml')
    _,meta=parser.parse_request_full(RECORD,CONTEXT)
    assert meta.method=='fallback'
    assert not cached_files(tmp_path)


def test_cache_distinguishes_format_and_cap_but_not_key_or_timeout(parser,monkeypatch,tmp_path):
    calls,_=install_transport(parser,monkeypatch)
    modes=[('json_object',''),('json_schema',''),('json_schema','512'),('json_object','512')]
    for mode,cap in modes:
        monkeypatch.setenv('LLM_RESPONSE_FORMAT',mode)
        monkeypatch.setenv('LLM_MAX_TOKENS',cap)
        _,meta=parser.parse_request_full(RECORD,CONTEXT)
        assert meta.method=='llm'
    assert len(calls)==4
    assert len(cached_files(tmp_path))==4
    monkeypatch.setenv('LLM_API_KEY','different-synthetic-key')
    monkeypatch.setenv('LLM_TIMEOUT_SECONDS','30')
    for mode,cap in modes:
        monkeypatch.setenv('LLM_RESPONSE_FORMAT',mode)
        monkeypatch.setenv('LLM_MAX_TOKENS',cap)
        _,meta=parser.parse_request_full(RECORD,CONTEXT)
        assert meta.method=='llm'
    assert len(calls)==4
    assert len(cached_files(tmp_path))==4
    assert all('synthetic-key' not in path.read_text() for path in cached_files(tmp_path))


@pytest.mark.parametrize('reason',['length','content_filter','tool_calls','function_call','unknown',None])
def test_present_non_stop_finish_is_not_cached_even_when_content_is_valid_json(parser,monkeypatch,tmp_path,reason):
    calls,_=install_transport(parser,monkeypatch,envelope(finish=reason))
    result,meta=parser.parse_request_full(RECORD,CONTEXT)
    assert len(calls)==1
    assert meta.method=='fallback' and result[0]['needs_review']
    assert not cached_files(tmp_path)
    retry_calls,_=install_transport(parser,monkeypatch,envelope(finish='stop'))
    result,meta=parser.parse_request_full(RECORD,CONTEXT)
    assert meta.method=='llm' and result[0]['evidence_quote']==RECORD['text']
    assert len(retry_calls)==1 and len(cached_files(tmp_path))==1


def test_absent_finish_reason_retains_backend_compatibility(parser,monkeypatch):
    calls,_=install_transport(parser,monkeypatch,envelope(finish=ABSENT))
    _,meta=parser.parse_request_full(RECORD,CONTEXT)
    assert meta.method=='llm' and len(calls)==1


@pytest.mark.parametrize('body',[
    b'{',b'{}',b'{"choices":[]}',b'{"choices":[{"message":{"content":null}}]}',
    b'{"choices":[{"message":{"content":[{"type":"image_url","image_url":"ignored"}]}}]}',
])
def test_malformed_envelopes_are_fallbacks_not_successful_cache_entries(parser,monkeypatch,tmp_path,body):
    calls,_=install_transport(parser,monkeypatch,body)
    result,meta=parser.parse_request_full(RECORD,CONTEXT)
    assert len(calls)==1
    assert meta.method=='fallback' and result[0]['needs_review']
    assert not cached_files(tmp_path)


def test_oversized_http_body_stops_at_bounded_read_without_cache(parser,monkeypatch,tmp_path):
    monkeypatch.setattr(parser,'MAX_RESPONSE_BYTES',128)
    calls,responses=install_transport(parser,monkeypatch,b' '*129)
    _,meta=parser.parse_request_full(RECORD,CONTEXT)
    assert len(calls)==1 and meta.method=='fallback'
    assert responses[0].read_limits==[129]
    assert not cached_files(tmp_path)


def test_transport_error_logging_does_not_include_url_credentials_body_or_guest_text(parser,monkeypatch,tmp_path,caplog):
    secret='SYNTHETIC-URL-CREDENTIAL'
    sensitive_guest='SYNTHETIC-GUEST-PRIVATE-TEXT'
    monkeypatch.setenv('LLM_BASE_URL','http://127.0.0.1:11434/v1/'+secret)
    record={**RECORD,'text':sensitive_guest}
    calls=[]
    def fail(request,timeout):
        calls.append(request)
        raise HTTPError(request.full_url,500,'synthetic-local-key '+sensitive_guest,None,None)
    monkeypatch.setattr(parser,'_open_llm',fail)
    with caplog.at_level(logging.DEBUG,logger=parser.__name__):
        _,meta=parser.parse_request_full(record,CONTEXT)
    assert len(calls)==1 and meta.method=='fallback'
    for value in (secret,sensitive_guest,'synthetic-local-key',calls[0].full_url):
        assert value not in caplog.text
    assert not cached_files(tmp_path)


def test_same_key_concurrent_timeouts_cannot_poison_a_successful_atomic_cache(parser,monkeypatch,tmp_path):
    arrived=Barrier(6)
    saved=Event()
    lock=Lock()
    attempts=[]
    def transport(request,timeout):
        with lock:
            index=len(attempts)
            attempts.append(index)
        arrived.wait(timeout=5)
        if index==0:
            return Response(envelope())
        assert saved.wait(5),'successful cache writer must be able to finish independently'
        raise TimeoutError('injected local model queue timeout')
    monkeypatch.setattr(parser,'_open_llm',transport)
    def run(_):
        result=parser.parse_request_full(deepcopy(RECORD),deepcopy(CONTEXT))
        if result[1].method=='llm':
            saved.set()
        return result
    with ThreadPoolExecutor(max_workers=6) as pool:
        results=list(pool.map(run,range(6)))
    assert len(attempts)==6
    assert sum(meta.method=='llm' for _,meta in results)==1
    assert sum(meta.method=='fallback' for _,meta in results)==5
    files=cached_files(tmp_path)
    assert len(files)==1
    assert json.loads(files[0].read_text())['extraction']['method']=='llm'
    assert not list((tmp_path/'cache').glob('*.tmp'))
    intents,meta=parser.parse_request_full(RECORD,CONTEXT)
    assert meta.method=='llm' and intents[0]['evidence_quote']==RECORD['text']
    assert len(attempts)==6
