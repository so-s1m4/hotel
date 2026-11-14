"""Unseen combinations: source clock qualifiers cannot vanish in a narrow quote."""
from copy import deepcopy
from datetime import datetime,timezone
from zoneinfo import ZoneInfo

import pytest

from ingestion.guest_parser import _validated


def expected(hour,minute=0,*,utc=False):
    zone=timezone.utc if utc else ZoneInfo('Europe/Vienna')
    return datetime(2027,2,5,hour,minute,tzinfo=zone).astimezone(timezone.utc).isoformat().replace('+00:00','Z')


def interpret(text,quote,*,evidence=None,language='en'):
    record={'text':text,'language':language,'timestamp_utc':'2027-02-04T10:00:00Z','request_type':'early_checkin'}
    raw=[{'intent':'early_checkin','requested_time_utc':None,'requested_time_text':quote,
          'evidence_quote':evidence or text,'details':{},'needs_review':False}]
    before=deepcopy((record,raw))
    rows,uncertain=_validated(raw,record,{},set())
    assert (record,raw)==before
    return rows[0],uncertain


@pytest.mark.parametrize('clock,quote,hour,minute',[
    ('1:30 pm','1:30',13,30),('1:30 p.m.','1:30',13,30),
    ('12:05 am','12:05',0,5),('12:05 pm','12:05',12,5),
    ('1 pm','1',13,0),('1:30 p.m.','1:30 p.m.',13,30),
])
def test_adjacent_source_meridiem_is_not_dropped_when_resolving_a_narrow_clock(clock,quote,hour,minute):
    row,uncertain=interpret(f'We request early check-in tomorrow at {clock}.',quote)
    assert row['requested_time_utc']==expected(hour,minute)
    assert not row['needs_review'] and not uncertain


@pytest.mark.parametrize('clock',['13:10 pm','00:10 am'])
def test_invalid_meridiem_in_source_cannot_be_hidden_by_dropping_it_from_quote(clock):
    quote=clock.split()[0]
    row,uncertain=interpret(f'We request early check-in tomorrow at {clock}.',quote)
    assert row['requested_time_utc'] is None and row['needs_review']
    assert 'requested_time_utc' in uncertain


@pytest.mark.parametrize('clock,quote',[
    ('noon or midnight','noon'),('noon or midnight','midnight'),
    ('13h or 14h','13h'),('13h or 14h','14h'),
    ('1 pm or 2 pm','1 pm'),('1 pm or 2 pm','2 pm'),
    ('noon or at 13:45','noon'),('noon or at 13:45','13:45'),
])
def test_alternative_source_clocks_remain_unknown_for_named_and_hour_only_forms(clock,quote):
    row,uncertain=interpret(f'We request early check-in tomorrow at {clock}.',quote)
    assert row['requested_time_utc'] is None and row['needs_review']
    assert 'requested_time_utc' in uncertain


@pytest.mark.parametrize('clock,quote,hour,minute',[
    ('noon','noon',12,0),('midnight','midnight',0,0),
    ('13:45','13:45',13,45),('13h45','13h45',13,45),('13h','13h',13,0),
])
def test_supported_unique_clocks_keep_exact_minute_resolution(clock,quote,hour,minute):
    row,uncertain=interpret(f'We request early check-in tomorrow at {clock}.',quote)
    assert row['requested_time_utc']==expected(hour,minute)
    assert not row['needs_review'] and not uncertain


@pytest.mark.parametrize('zone',['JST','KST','AEST','Asia/Tokyo','+5:30','+05:30','+ 05:30'])
@pytest.mark.parametrize('narrow',[True,False])
def test_explicit_unsupported_source_zone_or_offset_cannot_be_silently_treated_as_vienna(zone,narrow):
    text=f'We request early check-in tomorrow at 13:00 {zone}.'
    row,uncertain=interpret(text,'13:00' if narrow else f'13:00 {zone}')
    assert row['requested_time_utc'] is None and row['needs_review']
    assert 'requested_time_utc' in uncertain


@pytest.mark.parametrize('zone,expected_hour',[('UTC',13),('CET',12)])
def test_adjacent_supported_zone_and_meridiem_can_both_be_inherited(zone,expected_hour):
    row,uncertain=interpret(f'We request early check-in tomorrow at 1:30 pm {zone}.','1:30')
    assert row['requested_time_utc']==expected(expected_hour,30,utc=True)
    assert not row['needs_review'] and not uncertain


def test_timezone_and_meridiem_from_an_unrelated_clause_do_not_contaminate_an_exact_request():
    first='We request early check-in tomorrow at 1:30 pm'
    second='Our colleague will call the day after tomorrow at 1:30 am JST'
    row,uncertain=interpret(first+'; '+second+'.','1:30',evidence=first)
    assert row['requested_time_utc']==expected(13,30)
    assert not row['needs_review'] and not uncertain


def test_identical_narrow_quote_with_different_source_meridiems_is_ambiguous_without_evidence_scope():
    text='We request early check-in tomorrow at 1:30 am or 1:30 pm.'
    row,uncertain=interpret(text,'1:30',evidence='1:30')
    assert row['requested_time_utc'] is None and row['needs_review']
    assert 'requested_time_utc' in uncertain


@pytest.mark.parametrize('quote',['1:30','2:30','1:30 p.m.','2:30 p.m.'])
def test_dotted_meridiem_does_not_sever_an_alternative_from_its_source_clock(quote):
    row,uncertain=interpret('We request early check-in tomorrow at 1:30 p.m. or 2:30 p.m.',quote)
    assert row['requested_time_utc'] is None and row['needs_review']
    assert 'requested_time_utc' in uncertain


@pytest.mark.parametrize('clock,quote', [('1:30 p.m.','1:30 p'),('13h45','13h'),('noon','no')])
def test_partial_meridiem_unit_or_named_clock_cannot_be_certified_as_a_complete_clock(clock,quote):
    row,uncertain=interpret(f'We request early check-in tomorrow at {clock}.',quote)
    assert row['requested_time_utc'] is None and row['needs_review']
    assert 'requested_time_utc' in uncertain


def test_dotted_meridiem_sentence_end_still_separates_unrelated_clock_context():
    first='We request early check-in tomorrow at 1:30 p.m.'
    second='Our colleague calls the day after tomorrow at 1:30 a.m. JST.'
    row,uncertain=interpret(first+' '+second,'1:30',evidence=first)
    assert row['requested_time_utc']==expected(13,30)
    assert not row['needs_review'] and not uncertain


@pytest.mark.parametrize('quote',['1:30','2:30'])
def test_attached_dotted_meridiem_keeps_alternative_clocks_connected(quote):
    row,uncertain=interpret('We request early check-in tomorrow at 1:30p.m. or 2:30p.m.',quote)
    assert row['requested_time_utc'] is None and row['needs_review']
    assert 'requested_time_utc' in uncertain


@pytest.mark.parametrize('zone',['asia/tokyo','Europe/London'])
@pytest.mark.parametrize('narrow',[False,True])
def test_explicit_iana_zone_syntax_is_not_silently_lost_even_when_case_varies(zone,narrow):
    text=f'We request early check-in tomorrow at 13:00 {zone}.'
    row,uncertain=interpret(text,'13:00' if narrow else f'13:00 {zone}')
    assert row['requested_time_utc'] is None and row['needs_review']
    assert 'requested_time_utc' in uncertain


def test_seconds_separated_by_whitespace_cannot_be_hidden_as_an_exact_minute_request():
    row,uncertain=interpret('We request early check-in tomorrow at 13:00 : 59.','13:00')
    assert row['requested_time_utc'] is None and row['needs_review']
    assert 'requested_time_utc' in uncertain


# The named-zone class was disclosed by an external review. These are informed
# host diagnostics with independently written examples, not untouched blind gold.
@pytest.mark.parametrize('language,text',[
    ('en','We request early check-in tomorrow at 14:00 local time in Nairobi.'),
    ('de','Könnten wir morgen um 14:00 Ortszeit in Bangkok früher einchecken?'),
    ('fr','Un check-in anticipé demain à 14:00, heure de Rabat, serait-il possible ?'),
    ('it','Possiamo fare il check-in anticipato domani alle 14:00 ora di Seul?'),
    ('es','¿Podemos hacer el check-in temprano mañana a las 14:00, hora de Bogotá?'),
    ('nl','Kunnen we morgen om 14:00 lokale tijd in Toronto vroeg inchecken?'),
])
@pytest.mark.parametrize('narrow',[True,False])
def test_explicit_natural_language_zone_marker_does_not_certify_vienna(language,text,narrow):
    row,uncertain=interpret(text,'14:00' if narrow else text,language=language)
    assert row['requested_time_utc'] is None and row['needs_review']
    assert 'requested_time_utc' in uncertain


@pytest.mark.parametrize('place',['Tokyo','New York','São Paulo'])
def test_english_proper_name_time_suffix_is_unknown_without_a_city_lookup(place):
    row,uncertain=interpret(f'We request early check-in tomorrow at 14:00 {place} time.','14:00')
    assert row['requested_time_utc'] is None and row['needs_review']
    assert 'requested_time_utc' in uncertain


@pytest.mark.parametrize('label',['arrival time','Arrival time','local time','hotel time'])
def test_ordinary_time_labels_after_the_clock_are_not_assumed_to_name_a_zone(label):
    row,uncertain=interpret(f'We request early check-in tomorrow at 14:00, {label}.','14:00')
    assert row['requested_time_utc']==expected(14)
    assert not row['needs_review'] and not uncertain


@pytest.mark.parametrize('language,phrase,day',[
    ('es','hora de llegada','mañana'),('es','hora de salida','mañana'),
    ('it','ora di arrivo','domani'),('fr',"heure d'arrivée",'demain'),
    ('nl','tijd van aankomst','morgen'),
])
def test_event_time_objects_are_not_place_names(language,phrase,day):
    row,uncertain=interpret(f'{day} 14:00, {phrase}.','14:00',language=language)
    assert row['requested_time_utc']==expected(14)
    assert not row['needs_review'] and not uncertain


def test_portuguese_arrival_label_does_not_add_spurious_timezone_uncertainty():
    row,uncertain=interpret('2027-02-05 14:00, hora de chegada.','14:00',language='pt')
    assert row['requested_time_utc']==expected(14)
    assert row['needs_review'] and uncertain=={'language'}  # Portuguese is not a supported source language.


@pytest.mark.parametrize('separator',['; ','. '])
def test_named_zone_on_an_unrelated_clock_does_not_change_the_requested_clock(separator):
    first='We request early check-in tomorrow at 14:00'
    second='Our colleague arrives the day after tomorrow at 16:00 local time in Nairobi'
    row,uncertain=interpret(first+separator+second+'.','14:00',evidence=first)
    assert row['requested_time_utc']==expected(14)
    assert not row['needs_review'] and not uncertain


@pytest.mark.parametrize('language,phrase,day',[
    ('es','hora de la llegada','mañana'),('fr',"heure de l'arrivée",'demain'),
    ('nl','tijd van de aankomst','morgen'),
])
def test_ordinary_event_time_label_with_an_article_is_not_a_named_zone(language,phrase,day):
    row,uncertain=interpret(f'{day} 14:00, {phrase}.','14:00',language=language)
    assert row['requested_time_utc']==expected(14)
    assert not row['needs_review'] and not uncertain
