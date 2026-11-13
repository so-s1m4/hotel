"""Injected structured replies through real ingestion and engine; no model/network."""
from copy import deepcopy
import csv
import json
import os
import socket

import pytest

from engine.planner import DecisionEngine
from ingestion import guest_parser
from ingestion.events import iter_normalized_events
from ingestion.loader import REQUIRED_COLUMNS
from ingestion.replay import ReplayData


def write_csv(path,rows):
    with path.open('w',newline='') as stream:
        writer=csv.DictWriter(stream,fieldnames=list(rows[0]))
        writer.writeheader();writer.writerows(rows)


@pytest.fixture
def local_dataset(tmp_path,monkeypatch):
    for name in tuple(os.environ):
        if name.startswith(('LLM_','OPENAI_','OPENROUTER_','CEREBRAS_','GUEST_PARSER_')):
            monkeypatch.delenv(name,raising=False)
    monkeypatch.setenv('LLM_API_KEY','fixture-placeholder-not-a-credential')
    monkeypatch.setenv('LLM_MODEL','injected-local-structured')
    monkeypatch.setenv('LLM_BASE_URL','http://127.0.0.1:1/v1')
    monkeypatch.setenv('LLM_RESPONSE_FORMAT','json_schema')
    monkeypatch.setenv('GUEST_PARSER_WORKERS','1')
    monkeypatch.setenv('GUEST_PARSER_CACHE_DIR',str(tmp_path/'cache'))
    # Isolate optional dotenv/network side effects; all source loading and host
    # validation below remain production implementations.
    monkeypatch.setattr('ingestion.events._load_dotenv',lambda:None)
    def block_network(*args,**kwargs):raise AssertionError('Network is forbidden in injected integration controls')
    monkeypatch.setattr(socket,'create_connection',block_network)
    monkeypatch.setattr(socket.socket,'connect',block_network)
    root=tmp_path/'data';root.mkdir()
    for filename,columns in REQUIRED_COLUMNS.items():
        (root/filename).write_text(','.join(columns)+'\n')
    building={'time':{'local_timezone':'Europe/Vienna'},'constraints':{'occupied_comfort_c':[20,23]},
        'building':{'rooms':[{'id':room} for room in ['R101','R102','R103']],'assets':[]},
        'staff':{'resources':[{'resource_id':'ONCALL_1','role':'on_call_housekeeping'}]}}
    (root/'building.json').write_text(json.dumps(building))
    (root/'manifest.json').write_text(json.dumps({'start_utc':'2026-01-06T15:00:00Z',
        'end_utc':'2026-01-06T15:15:00Z','interval_seconds':900}))
    write_csv(root/'room_telemetry.csv',[{'timestamp_utc':'2026-01-06T15:00:00Z','room_id':room,
        'temperature_c':21,'setpoint_c':21,'window_open':0,'presence':1 if room=='R102' else 0,
        'housekeeping_state':'dirty'} for room in ['R101','R102','R103']])
    write_csv(root/'bookings.csv',[{'booking_id':bid,'room_id':room,'created_utc':created,
        'arrival_utc':'2026-01-06T18:00:00Z','departure_utc':'2026-01-07T10:00:00Z'}
        for bid,room,created in [('B1','R101','2026-01-01T00:00:00Z'),
                                 ('B2','R102','2026-01-01T00:00:00Z'),
                                 ('B3','R103','2026-01-08T00:00:00Z')]])
    write_csv(root/'guest_requests.csv',[{'request_id':rid,'timestamp_utc':'2026-01-06T15:00:00Z',
        'room_id':room,'booking_id':bid,'channel':'app_chat','language':'en','request_type':label,
        'text':text,'priority':'medium','requires_guest_consent':'required'}
        for rid,room,bid,label,text in [
            ('WAIT','R101','B1','waiting_for_room','I am waiting for my room; please clean now.'),
            ('OCCUPIED','R102','B2','early_checkin','Please let me check in today at 18:00 and clean now.'),
            ('NO_DATE','R103','B3','early_checkin','May I check in at 18:00?')]])
    raw=[{'event_id':'CHECKOUT1','timestamp_utc':'2026-01-06T14:45:00Z','type':'checkout','room_id':'R101'},
         {'event_id':'CHECKOUT3','timestamp_utc':'2026-01-06T14:45:00Z','type':'checkout','room_id':'R103'},
         {'event_id':'OTHER_GUEST','timestamp_utc':'2026-01-06T14:30:00Z','type':'checkin','room_id':'R102','booking_id':'OLD'},
         {'event_id':'SOURCE_NOTE','timestamp_utc':'2026-01-06T15:10:00Z','type':'note','room_id':'R101'}]
    (root/'events.jsonl').write_text(''.join(json.dumps(row)+'\n' for row in raw))
    return root,building


def extracted(intent,quote,*,time_text=None,time_utc=None,details=None):
    return {'intent':intent,'requested_time_utc':time_utc,'requested_time_text':time_text,
            'evidence_quote':quote,'details':details or {},'needs_review':False}


def test_structured_local_semantics_do_not_grant_consent_approval_or_room_release(local_dataset,monkeypatch):
    root,building=local_dataset
    prompts={}
    def reply(messages,config):
        assert config['response_format_mode']=='json_schema'
        source=json.loads(messages[-1]['content'])
        rid=source['guest_record']['request_id'];prompts[rid]=deepcopy(source)
        if rid=='WAIT':
            intents=[extracted('waiting_for_room','I am waiting for my room'),
                     extracted('cleaning_request','please clean now',details={'assistance_requested':'please clean now'})]
        elif rid=='OCCUPIED':
            intents=[extracted('early_checkin','Please let me check in today at 18:00',
                               time_text='today at 18:00',time_utc='2026-01-06T17:00:00Z'),
                     extracted('cleaning_request','clean now',details={'assistance_requested':'clean now'})]
        else:
            intents=[extracted('early_checkin','May I check in at 18:00?',time_text='at 18:00',
                               time_utc='2026-01-08T17:00:00Z')]
        return json.dumps({'intents':intents})
    monkeypatch.setattr(guest_parser,'_call_llm',reply)
    repo=ReplayData(root,use_llm=True)
    events=list(iter_normalized_events(repo))
    guests={event.source.record_id:event for event in events if event.event_type=='guest_request'}
    assert set(prompts)==set(guests)=={'WAIT','OCCUPIED','NO_DATE'}
    assert guests['WAIT'].extraction.method=='llm' and not guests['WAIT'].extraction.needs_review
    assert guests['OCCUPIED'].extraction.method=='llm' and not guests['OCCUPIED'].extraction.needs_review
    assert all(event.payload['requires_guest_consent']=='required' for event in guests.values())
    # The future booking's planned arrival is not a usable date anchor at15:00.
    assert 'booking' not in prompts['NO_DATE']['known_context']
    unknown=guests['NO_DATE'].payload['intents'][0]
    assert unknown['requested_time_utc'] is None and unknown['needs_review']
    assert guests['NO_DATE'].extraction.needs_review
    assert all('consent_granted' not in event.payload and 'action' not in event.payload for event in guests.values())
    engine=DecisionEngine(repo,building)
    decisions=engine.run()
    job=next(row for row in decisions if row.action=='schedule_cleaning')
    assert job.affected_assets==['R101'] and job.resource_id=='ONCALL_1' and job.requires_human_approval
    assert not any(row.action in {'release_room','schedule_cleaning','request_inspection','dispatch_robot'}
                   and bool(set(row.affected_assets)&{'R102','R103'}) for row in decisions)
    audit=next(row for row in engine.executor.audit if row['decision_id']==job.event_id)
    assert audit['status']=='pending_human_approval'
    assert any(job.event_id==ident for _,_,ident in engine.calendar.tentative_reservations['ONCALL_1'])
    assert not any(engine.calendar.reservations.values())
    assert all(not row['effects_applied'] and not row['observed_history_changed'] for row in engine.executor.audit)
    assert not any(row.action=='release_room' for row in decisions)
    assert repo.get_room_state('R101','2026-01-06T15:00:00Z')['housekeeping_state']=='dirty'
    assert repo.get_room_state('R102','2026-01-06T15:00:00Z')['occupancy']=='occupied'
    assert not engine.rejections


@pytest.mark.parametrize('failure',['timeout','invalid_json'])
def test_optional_model_failure_preserves_full_real_normalized_stream_and_replay(local_dataset,monkeypatch,failure):
    root,building=local_dataset
    baseline_repo=ReplayData(root,use_llm=False)
    baseline=list(iter_normalized_events(baseline_repo))
    attempts=[]
    def fail(messages,config):
        attempts.append(json.loads(messages[-1]['content'])['guest_record']['request_id'])
        if failure=='timeout':raise TimeoutError('injected local runtime timeout')
        return '{"intents": [not valid JSON]}'
    monkeypatch.setattr(guest_parser,'_call_llm',fail)
    repo=ReplayData(root,use_llm=True)
    events=list(iter_normalized_events(repo))
    assert set(attempts)=={'WAIT','OCCUPIED','NO_DATE'} and len(attempts)==3
    assert [e.event_id for e in events]==[e.event_id for e in baseline]
    assert any(e.event_type=='clock_tick' for e in events)
    assert any(e.event_id=='raw:SOURCE_NOTE' for e in events)
    assert any(e.event_id=='booking:B3' for e in events)
    assert [e.model_dump() for e in events if e.event_type!='guest_request']==[
        e.model_dump() for e in baseline if e.event_type!='guest_request']
    for event in events:
        if event.event_type=='guest_request':
            assert event.extraction.method=='fallback' and event.extraction.needs_review
            assert event.payload['intents'][0]['intent']==event.payload['request_type']
            assert event.payload['intents'][0]['requested_time_utc'] is None
            assert event.payload['requires_guest_consent']=='required'
    engine=DecisionEngine(repo,building)
    actual=engine.run()
    expected=DecisionEngine(baseline_repo,building).run()
    assert [row.model_dump() for row in actual]==[row.model_dump() for row in expected]
    assert engine.event_count==len(events)
    assert not engine.rejections
    assert all(not row['effects_applied'] for row in engine.executor.audit)
