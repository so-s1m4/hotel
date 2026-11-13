"""Generation grammar constraints do not replace host semantic validation."""
import hashlib
import json
import re
from typing import get_args

import pytest
from pydantic import ValidationError

from common.contracts.schemas import GuestIntentV1
from ingestion.guest_generation import (
    MAX_INTENTS, generation_schema, generation_schema_sha256,
    response_format, response_format_sha256,
)


def intent_schema():
    return generation_schema()['$defs']['intent']


def test_generation_envelope_requires_one_to_32_objects_not_json_strings():
    schema=generation_schema()
    assert schema['type']=='object'
    assert schema['required']==['intents'] and schema['additionalProperties'] is False
    intents=schema['properties']['intents']
    assert intents['type']=='array' and intents['minItems']==1 and intents['maxItems']==MAX_INTENTS==32
    assert intents['items']=={'$ref':'#/$defs/intent'}
    assert intent_schema()['type']=='object'


def test_generation_fields_match_frozen_intent_shape_with_explicit_required_nullables():
    intent=intent_schema();properties=intent['properties']
    assert set(properties)==set(GuestIntentV1.model_fields)
    assert set(intent['required'])==set(properties)
    assert properties['intent']['enum']==list(get_args(GuestIntentV1.model_fields['intent'].annotation))
    for field in ['requested_time_utc','requested_time_text']:
        assert {alternative['type'] for alternative in properties[field]['anyOf']}=={'string','null'}
    assert properties['evidence_quote']['type']=='string'
    assert properties['evidence_quote']['minLength']==1
    assert 'anyOf' not in properties['evidence_quote']
    assert properties['needs_review']['type']=='boolean'
    assert intent['additionalProperties'] is False


def test_every_generation_object_is_closed_and_all_declared_fields_are_required():
    objects=[]
    def visit(node):
        if isinstance(node,dict):
            if node.get('type')=='object':
                objects.append(node)
                assert node['additionalProperties'] is False
                assert set(node['required'])==set(node['properties'])
            for value in node.values():visit(value)
        elif isinstance(node,list):
            for value in node:visit(value)
    visit(generation_schema())
    assert len(objects)==6  # envelope, intent and four details variants


def test_details_support_only_documented_keys_without_invented_null_placeholders():
    variants=intent_schema()['properties']['details']['anyOf']
    assert {frozenset(v['properties']) for v in variants}=={
        frozenset(),frozenset({'approximate_time'}),frozenset({'assistance_requested'}),
        frozenset({'approximate_time','assistance_requested'})}
    for variant in variants:
        props=variant['properties']
        if 'approximate_time' in props:assert props['approximate_time']['type']=='boolean'
        if 'assistance_requested' in props:
            assistance=props['assistance_requested']['anyOf']
            assert assistance[0]=={'type':'string','minLength':1}
            assert assistance[1]=={'type':'array','minItems':1,'items':{'type':'string','minLength':1}}


def test_utc_pattern_constrains_shape_but_host_rejects_impossible_calendar_dates():
    pattern=intent_schema()['properties']['requested_time_utc']['anyOf'][0]['pattern']
    for value in ['2026-01-06T09:00:00Z','2026-01-06T09:00:00.500Z']:
        assert re.fullmatch(pattern,value)
        GuestIntentV1(intent='early_checkin',requested_time_utc=value)
    for value in ['2026-01-06T09:00:00+00:00','2026-01-06 09:00:00Z','09:00','2026-01-06T09:00:00Zjunk']:
        assert not re.fullmatch(pattern,value)
    impossible='2026-02-31T09:00:00Z'
    assert re.fullmatch(pattern,impossible)  # deliberately no fake date/grounding authority
    with pytest.raises(ValidationError):GuestIntentV1(intent='early_checkin',requested_time_utc=impossible)


def test_host_grounding_remains_authoritative_for_well_shaped_generated_quotes():
    from ingestion.guest_parser import _validated
    raw={'intent':'extra_towels','requested_time_utc':None,'requested_time_text':None,
         'evidence_quote':'Please send flowers','details':{'assistance_requested':'send flowers'},'needs_review':False}
    result,uncertain=_validated([raw],{'text':'Could I have towels?','language':'en','request_type':'extra_towels'}, {},set())
    assert result[0]['evidence_quote'] is None and result[0]['details']=={}
    assert result[0]['needs_review'] and 'evidence_quote' in uncertain


def test_response_format_is_backwards_compatible_by_default_and_strict_only_when_selected():
    assert response_format()==response_format('json_object')=={'type':'json_object'}
    strict=response_format('json_schema')
    assert strict=={'type':'json_schema','json_schema':{
        'name':'guest_intents_v1','strict':True,'schema':generation_schema()}}
    json.dumps(strict,allow_nan=False)


@pytest.mark.parametrize('mode',[None,'','json','JSON_SCHEMA',[],{}])
def test_unknown_mode_never_silently_changes_or_disables_constraints(mode):
    with pytest.raises(ValueError):response_format(mode)
    with pytest.raises(ValueError):response_format_sha256(mode)


def test_schema_and_format_results_cannot_contaminate_subsequent_calls():
    original=generation_schema_sha256()
    schema=generation_schema();schema['properties']['intents']['maxItems']=1
    schema['$defs']['intent']['properties']['intent']['enum'].append('release_room')
    fmt=response_format('json_schema');fmt['json_schema']['schema']['additionalProperties']=True
    default=response_format();default['type']='text'
    assert generation_schema_sha256()==original
    assert response_format()=={'type':'json_object'}
    assert response_format('json_schema')['json_schema']['schema']['additionalProperties'] is False


def test_hashes_identify_exact_canonical_schema_and_provider_payload():
    def digest(value):
        return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()
    assert generation_schema_sha256()==digest(generation_schema())
    assert response_format_sha256()==digest({'type':'json_object'})
    assert response_format_sha256('json_schema')==digest(response_format('json_schema'))
    assert response_format_sha256()!=response_format_sha256('json_schema')
    assert len({generation_schema_sha256(),response_format_sha256(),response_format_sha256('json_schema')})==3


def test_generation_schema_does_not_tighten_the_frozen_host_fallback_contract():
    # Source-label fallback legitimately carries no model evidence. It remains
    # valid under common even though generation itself requires quoted evidence.
    fallback=GuestIntentV1(intent='unknown',evidence_quote=None,details={},needs_review=True)
    assert fallback.evidence_quote is None
    assert GuestIntentV1.model_fields['evidence_quote'].default is None


def test_default_contract_profile_preserves_exact_1647ebb_schema_and_format_hashes():
    assert generation_schema()==generation_schema('contract')
    assert generation_schema_sha256()==generation_schema_sha256('contract')=='271937dd7e1736c0e63260d63163c2687e1fb342e7e9fff3e5e37d7b32f1c5f2'
    assert response_format_sha256('json_schema','contract')=='bbc089482ac6dcd5a1ad23d51ca74abcf9461ba7d3564875f531dc2d27a41823'
    assert response_format_sha256()=='376bde7986cc2f8fd182f04e745c607933760bea7c306ea6e8e992b7f70be25a'


def test_extraction_profile_changes_only_computed_utc_and_semantic_details():
    contract=generation_schema('contract')
    extraction=generation_schema('extraction')
    properties=extraction['$defs']['intent']['properties']
    assert properties['requested_time_utc']=={'type':'null'}
    assert properties['details']=={'type':'object','properties':{},'required':[],'additionalProperties':False}
    required=extraction['$defs']['intent']['required']
    assert {'requested_time_utc','details'} <= set(required)
    expected=generation_schema('contract')
    expected['$defs']['intent']['properties']['requested_time_utc']={'type':'null'}
    expected['$defs']['intent']['properties']['details']={'type':'object','properties':{},'required':[],'additionalProperties':False}
    assert extraction==expected
    assert set(properties)==set(GuestIntentV1.model_fields)
    assert extraction['properties']['intents']==contract['properties']['intents']
    assert response_format('json_schema','extraction')['json_schema']['schema']==extraction


@pytest.mark.parametrize('profile',[None,'','CONTRACT','other',False,[],{}])
def test_invalid_profile_fails_in_every_helper_even_json_object_mode(profile):
    with pytest.raises(ValueError):generation_schema(profile)
    with pytest.raises(ValueError):generation_schema_sha256(profile)
    for mode in ['json_object','json_schema']:
        with pytest.raises(ValueError):response_format(mode,profile)
        with pytest.raises(ValueError):response_format_sha256(mode,profile)


def test_profile_schema_identity_distinguishes_identical_json_object_wire_format():
    assert response_format('json_object','contract')==response_format('json_object','extraction')
    assert response_format_sha256('json_object','contract')==response_format_sha256('json_object','extraction')
    assert generation_schema_sha256('contract')!=generation_schema_sha256('extraction')
    assert response_format_sha256('json_schema','contract')!=response_format_sha256('json_schema','extraction')


def test_extraction_mutations_cannot_change_contract_or_another_profile_call():
    contract_hash=generation_schema_sha256('contract')
    extraction_hash=generation_schema_sha256('extraction')
    extraction=generation_schema('extraction')
    extraction['$defs']['intent']['properties']['details']['additionalProperties']=True
    extraction['$defs']['intent']['properties']['requested_time_utc']['type']='string'
    response=response_format('json_schema','extraction')
    response['json_schema']['schema']['$defs']['intent']['required'].remove('details')
    assert generation_schema_sha256('contract')==contract_hash
    assert generation_schema_sha256('extraction')==extraction_hash


def test_profile_helpers_do_not_open_files_or_connections(monkeypatch):
    import builtins
    import socket
    def forbidden(*args,**kwargs):raise AssertionError('Generation helpers must not perform I/O')
    monkeypatch.setattr(builtins,'open',forbidden)
    monkeypatch.setattr(socket,'create_connection',forbidden)
    monkeypatch.setattr(socket.socket,'connect',forbidden)
    for profile in ['contract','extraction']:
        generation_schema(profile)
        generation_schema_sha256(profile)
        for mode in ['json_object','json_schema']:
            response_format(mode,profile)
            response_format_sha256(mode,profile)
