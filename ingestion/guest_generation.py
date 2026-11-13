"""Provider generation constraints, separate from the frozen host contract.

These constraints guide optional structured generation; they do not establish
that quotations are grounded, a calendar date exists, a local time is unambiguous,
or a request is authorized. guest_parser._validated remains the authority for
source quotation, details grounding, date/DST resolution and uncertainty.

The default response format stays json_object. json_schema is an explicit opt-in
for compatible backends. Its details object permits only approximate_time (bool)
and assistance_requested (nonempty exact substring or list of such substrings).
Four closed object variants allow either, both or neither key without requiring
null placeholder details or optional properties unsupported by strict providers.
Importing this module performs no I/O and never changes the common contract.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from typing import get_args

from common.contracts.schemas import GuestIntentV1

__all__ = [
    'GENERATION_SCHEMA_VERSION', 'MAX_INTENTS', 'generation_schema',
    'response_format', 'generation_schema_sha256', 'response_format_sha256',
]

GENERATION_SCHEMA_VERSION = 'guest-generation-v1'
MAX_INTENTS = 32
# Shape only: calendar validity, UTC semantics and date grounding are host checks.
_UTC_Z_PATTERN = r'^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?Z$'


def _object(properties):
    return {'type':'object', 'properties':properties,
            'required':list(properties), 'additionalProperties':False}


def _build_schema():
    assistance = {
        'description':'Requested help, copied exactly from the guest text; host validates every substring.',
        'anyOf':[
            {'type':'string','minLength':1},
            {'type':'array','minItems':1,'items':{'type':'string','minLength':1}},
        ],
    }
    detail_properties = {
        'approximate_time':{'type':'boolean','description':'True when the quoted clock time is approximate.'},
        'assistance_requested':assistance,
    }
    details = {'anyOf':[
        _object({}),
        _object({'approximate_time':deepcopy(detail_properties['approximate_time'])}),
        _object({'assistance_requested':deepcopy(assistance)}),
        _object(deepcopy(detail_properties)),
    ]}
    intent = _object({
        'intent':{'type':'string','enum':list(get_args(GuestIntentV1.model_fields['intent'].annotation))},
        'requested_time_utc':{'anyOf':[{'type':'string','pattern':_UTC_Z_PATTERN},{'type':'null'}]},
        'requested_time_text':{'anyOf':[{'type':'string','minLength':1},{'type':'null'}],
                               'description':'Exact guest-text substring including relative date words, or null.'},
        'evidence_quote':{'type':'string','minLength':1,
                          'description':'Nonempty exact substring of the original guest text; host validates grounding.'},
        'details':details,
        'needs_review':{'type':'boolean'},
    })
    envelope = _object({'intents':{'type':'array','minItems':1,'maxItems':MAX_INTENTS,
                                  'items':{'$ref':'#/$defs/intent'}}})
    envelope['$defs'] = {'intent':intent}
    return envelope


_SCHEMA = _build_schema()


def generation_schema() -> dict:
    """Return an independent JSON-serializable generation envelope schema."""
    return deepcopy(_SCHEMA)


def response_format(mode: str = 'json_object') -> dict:
    """Return provider response_format, preserving the historical default."""
    if mode == 'json_object':
        return {'type':'json_object'}
    if mode == 'json_schema':
        return {'type':'json_schema','json_schema':{
            'name':'guest_intents_v1','strict':True,'schema':generation_schema(),
        }}
    raise ValueError('Response format must be json_object or json_schema')


def _sha256(value) -> str:
    encoded = json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


def generation_schema_sha256() -> str:
    """Hash exact generation constraints, independently of provider mode."""
    return _sha256(generation_schema())


def response_format_sha256(mode: str = 'json_object') -> str:
    """Hash the exact provider format, including its schema in opt-in mode."""
    return _sha256(response_format(mode))
