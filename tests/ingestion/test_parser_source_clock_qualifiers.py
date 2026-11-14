"""Source grammar must survive narrow and full model quotations."""
from datetime import datetime,timezone
from zoneinfo import ZoneInfo

import pytest

from ingestion import guest_parser as parser


def interpret(text,quote,language='de'):
    record={'text':text,'language':language,'timestamp_utc':'2028-02-04T10:00:00Z','request_type':'early_checkin'}
    raw=[dict(intent='early_checkin',requested_time_utc=None,requested_time_text=quote,evidence_quote=text,details={},needs_review=False)]
    return parser._validated(raw,record,{},set())


@pytest.mark.parametrize('clock,source_date,expected_local',[
    ('14:45','08.03.2028',datetime(2028,3,8,14,45)),
    ('12:05','05.02.2028',datetime(2028,2,5,12,5)),
    ('15:10','2028-07-18',datetime(2028,7,18,15,10)),
    ('01:40','11/02/2028',datetime(2028,2,11,1,40)),
])
@pytest.mark.parametrize('narrow',[True,False])
def test_german_am_before_a_date_does_not_become_an_am_meridiem(clock,source_date,expected_local,narrow):
    text=f'Könnten wir um {clock} am {source_date} früher einchecken?'
    rows,uncertain=interpret(text,clock if narrow else f'{clock} am {source_date}')
    expected=expected_local.replace(tzinfo=ZoneInfo('Europe/Vienna')).astimezone(timezone.utc).isoformat().replace('+00:00','Z')
    assert rows[0]['requested_time_utc']==expected
    assert not rows[0]['needs_review'] and not uncertain


@pytest.mark.parametrize('suffix,hour',[('AM',0),('a.m.',0),('PM',12),('p.m.',12)])
@pytest.mark.parametrize('narrow',[True,False])
def test_genuine_clock_meridiem_before_a_german_date_is_still_honored(suffix,hour,narrow):
    text=f'Früher Check-in um 12:05 {suffix} am 05.02.2028?'
    rows,uncertain=interpret(text,'12:05' if narrow else f'12:05 {suffix} am 05.02.2028')
    expected=datetime(2028,2,5,hour,5,tzinfo=ZoneInfo('Europe/Vienna')).astimezone(timezone.utc).isoformat().replace('+00:00','Z')
    assert rows[0]['requested_time_utc']==expected
    assert not rows[0]['needs_review'] and not uncertain


@pytest.mark.parametrize('clock,offset',[
    ('06:45','+ 06:45'),('05:30','- 05:30'),('13:30','+ 05:30'),
    ('08:10','+08:10'),('07:20','− 07:20'),('06:45','+ 06 : 45'),
])
@pytest.mark.parametrize('narrow',[True,False])
def test_offset_rejection_does_not_depend_on_whether_offset_equals_clock(clock,offset,narrow):
    text=f'We request early check-in tomorrow at {clock} {offset}.'
    rows,uncertain=interpret(text,clock if narrow else f'{clock} {offset}',language='en')
    assert rows[0]['requested_time_utc'] is None and rows[0]['needs_review']
    assert 'requested_time_utc' in uncertain


@pytest.mark.parametrize('clock,suffix,hour',[('1:35','am',1),('1:35','pm',13),('12:05','am',0)])
def test_english_lowercase_am_and_pm_remain_clock_suffixes(clock,suffix,hour):
    rows,uncertain=interpret(f'We request early check-in tomorrow at {clock} {suffix}.',clock,language='en')
    expected=datetime(2028,2,5,hour,int(clock.split(':')[1]),tzinfo=ZoneInfo('Europe/Vienna')).astimezone(timezone.utc).isoformat().replace('+00:00','Z')
    assert rows[0]['requested_time_utc']==expected
    assert not rows[0]['needs_review'] and not uncertain


def spanish(text,quote):
    record={'text':text,'language':'es','timestamp_utc':'2028-07-04T10:00:00Z','request_type':'early_checkin'}
    context={'booking':{'booking_id':'BOOKING','created_utc':'2028-07-01T10:00:00Z','arrival_utc':'2028-07-06T13:00:00Z'}}
    safe,uncertain=parser._known_context(record,context)
    raw=[dict(intent='early_checkin',requested_time_utc=None,requested_time_text=quote,evidence_quote=text,details={},needs_review=False)]
    return parser._validated(raw,record,safe,uncertain)


@pytest.mark.parametrize('quote',['10:20','a las 10:20'])
def test_spanish_early_checkin_adjective_does_not_become_a_before_threshold(quote):
    rows,uncertain=spanish('¿Podemos hacer el check-in antes a las 10:20 en punto? Esa es nuestra hora exacta.',quote)
    assert rows[0]['requested_time_utc']=='2028-07-06T08:20:00Z'
    assert not rows[0]['needs_review'] and not uncertain


@pytest.mark.parametrize('quote',['12:40','de las 12:40','antes de las 12:40'])
def test_spanish_before_threshold_remains_unknown_for_every_quote_boundary(quote):
    rows,uncertain=spanish('Necesitamos terminar el registro antes de las 12:40; esa es la hora límite.',quote)
    assert rows[0]['requested_time_utc'] is None and rows[0]['needs_review']
    assert 'requested_time_utc' in uncertain


@pytest.mark.parametrize('quote',['12:40','de las 12:40','después de las 12:40'])
def test_spanish_after_threshold_is_not_an_exact_appointment(quote):
    rows,uncertain=spanish('Podemos completar el registro después de las 12:40, no antes.',quote)
    assert rows[0]['requested_time_utc'] is None and rows[0]['needs_review']
    assert 'requested_time_utc' in uncertain


@pytest.mark.parametrize('word',['antes','después'])
@pytest.mark.parametrize('quote',['12:40','a las 12:40'])
def test_spanish_bare_before_after_is_not_globally_exempted(word,quote):
    actual_quote='12:40' if quote=='12:40' else f'{word} 12:40'
    rows,uncertain=spanish(f'Debemos completar el registro {word} 12:40; no es una cita exacta.',actual_quote)
    assert rows[0]['requested_time_utc'] is None and rows[0]['needs_review']
    assert 'requested_time_utc' in uncertain


@pytest.mark.parametrize('qualifier',['hoy','mañana','de mañana','aproximadamente'])
@pytest.mark.parametrize('quote',['12:40','a las 12:40'])
def test_spanish_intervening_date_or_qualifier_does_not_turn_a_threshold_into_an_exact_clock(qualifier,quote):
    rows,uncertain=spanish(f'Hay que terminar el registro antes {qualifier} a las 12:40; ese es el límite.',quote)
    assert rows[0]['requested_time_utc'] is None and rows[0]['needs_review']
    assert 'requested_time_utc' in uncertain


@pytest.mark.parametrize('quote',['10:20','a las 10:20'])
def test_spanish_date_before_earlier_checkin_still_keeps_explicit_at_clock_grammar(quote):
    rows,uncertain=spanish('Mañana quisiéramos hacer el check-in antes a las 10:20 en punto.',quote)
    assert rows[0]['requested_time_utc']=='2028-07-05T08:20:00Z'
    assert not rows[0]['needs_review'] and not uncertain
