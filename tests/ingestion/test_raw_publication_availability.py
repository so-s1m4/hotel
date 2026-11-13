"""Raw publication is causal across normalization, event views and lifecycle state."""
from datetime import datetime

import pytest

from ingestion.events import _raw_events
from ingestion.replay import ReplayData
from tests.ingestion.test_causality_astra import make_dataset  # shared tiny CSV fixture

OCCURRED='2026-01-05T00:10:00Z'
BEFORE='2026-01-05T00:15:00Z'
PUBLISHED='2026-01-05T00:30:00Z'
AFTER='2026-01-05T00:45:00Z'


def fact(ident='LATE',kind='checkin',occurred=OCCURRED,published=PUBLISHED,**fields):
    return {'event_id':ident,'timestamp_utc':occurred,'available_at_utc':published,
            'type':kind,'room_id':'R101','booking_id':'BK_PRESENT',**fields}


def test_raw_envelope_uses_publication_while_all_public_occurrence_fields_remain_original(make_dataset):
    repo=ReplayData(make_dataset(events=[fact()]),use_llm=False)
    raw=_raw_events(repo)
    assert len(raw)==1
    assert raw[0].timestamp_utc==raw[0].source.timestamp_utc==OCCURRED
    assert raw[0].available_at_utc==PUBLISHED
    assert raw[0].payload['timestamp_utc']==OCCURRED
    assert raw[0].payload['available_at_utc']==PUBLISHED
    assert not {'_source_ts','_available_ts'} & raw[0].payload.keys()
    assert [row.event_id for row in repo.iter_events() if row.event_id=='raw:LATE']==['raw:LATE']
    assert not any(row.event_id=='raw:LATE' and row.available_at_utc<PUBLISHED for row in repo.iter_events())


def test_occurrence_query_end_does_not_act_as_a_publication_cutoff(make_dataset):
    repo=ReplayData(make_dataset(events=[fact()]),use_llm=False)
    assert repo.get_events(OCCURRED,BEFORE,BEFORE)==[]
    # Publication is later than the requested OCCURRENCE window end, but it is
    # already known at as_of and the old occurrence belongs in that window.
    assert [row['event_id'] for row in repo.get_events(OCCURRED,BEFORE,PUBLISHED)]==['LATE']
    assert repo.get_events(PUBLISHED,AFTER,AFTER)==[]
    assert [row['event_id'] for row in repo.get_events_available_between(BEFORE,PUBLISHED,PUBLISHED)]==['LATE']


def test_late_checkin_is_absent_from_room_booking_and_evidence_before_publication(make_dataset):
    repo=ReplayData(make_dataset(events=[fact()]),use_llm=False)
    assert repo.get_room_state('R101',BEFORE)['occupancy']=='unknown'
    before=repo.get_booking('BK_PRESENT',BEFORE)
    assert before['observed_state']=='not_arrived' and before['observed_checkin_utc'] is None
    assert all('LATE' not in ref for ref in before['evidence'])
    state=repo.get_room_state('R101',PUBLISHED)
    booking=repo.get_booking('BK_PRESENT',PUBLISHED)
    assert state['occupancy']=='occupied' and state['current_booking_id']=='BK_PRESENT'
    assert booking['observed_state']=='checked_in' and booking['observed_checkin_utc']==OCCURRED
    assert any(OCCURRED in ref and 'LATE' in ref for ref in state['evidence'])
    assert any(OCCURRED in ref and 'LATE' in ref for ref in booking['evidence'])


def test_late_checkout_does_not_regress_a_more_recent_known_checkin(make_dataset):
    rows=[fact('FIRST','checkin','2026-01-05T00:01:00Z','2026-01-05T00:01:00Z'),
          fact('OLD_OUT','checkout','2026-01-05T00:05:00Z',PUBLISHED),
          fact('NEW_IN','checkin','2026-01-05T00:12:00Z','2026-01-05T00:12:00Z')]
    repo=ReplayData(make_dataset(events=rows),use_llm=False)
    for at in (BEFORE,PUBLISHED):
        assert repo.get_room_state('R101',at)['occupancy']=='occupied'
        booking=repo.get_booking('BK_PRESENT',at)
        assert booking['observed_state']=='checked_in'
        assert booking['observed_checkin_utc']=='2026-01-05T00:12:00Z'
    assert not any('OLD_OUT' in ref for ref in repo.get_booking('BK_PRESENT',BEFORE)['evidence'])
    assert any('OLD_OUT' in ref for ref in repo.get_booking('BK_PRESENT',PUBLISHED)['evidence'])


def test_move_cannot_borrow_an_unpublished_prior_checkin(make_dataset):
    repo=ReplayData(make_dataset(events=[fact('IN','checkin','2026-01-05T00:01:00Z',AFTER),
        fact('MOVE','room_reassigned',OCCURRED,PUBLISHED,from_room='R101',to_room='R102')]),use_llm=False)
    assert repo.get_room_state('R102',PUBLISHED)['occupancy']=='unknown'
    assert repo.get_booking('BK_PRESENT',PUBLISHED)['observed_state']=='not_arrived'
    assert repo.get_room_state('R102',AFTER)['occupancy']=='occupied'
    booking=repo.get_booking('BK_PRESENT',AFTER)
    assert booking['observed_room_id']=='R102'
    assert booking['observed_checkin_utc']=='2026-01-05T00:01:00Z'


def test_unpublished_move_does_not_change_room_or_booking_assignment(make_dataset):
    repo=ReplayData(make_dataset(events=[fact('IN','checkin','2026-01-05T00:01:00Z','2026-01-05T00:01:00Z'),
        fact('MOVE','room_reassigned',OCCURRED,PUBLISHED,from_room='R101',to_room='R102')]),use_llm=False)
    assert repo.get_room_state('R101',BEFORE)['occupancy']=='occupied'
    assert repo.get_room_state('R102',BEFORE)['occupancy']=='unknown'
    assert repo.get_booking('BK_PRESENT',BEFORE)['observed_room_id']=='R101'
    assert repo.get_bookings_for_room('R102',OCCURRED,AFTER,BEFORE)==[]
    assert repo.get_room_state('R101',PUBLISHED)['occupancy']=='vacant'
    assert repo.get_room_state('R102',PUBLISHED)['occupancy']=='occupied'
    assert repo.get_booking('BK_PRESENT',PUBLISHED)['observed_room_id']=='R102'
    assert len(repo.get_bookings_for_room('R102',OCCURRED,AFTER,PUBLISHED))==1


def test_fractional_occurrence_and_publication_keep_separate_instants(make_dataset):
    occurred='2026-01-05T00:10:00.500Z'
    repo=ReplayData(make_dataset(events=[fact(occurred=occurred,published='2026-01-05T00:30:00.250Z')]),use_llm=False)
    assert repo.get_events(OCCURRED,AFTER,'2026-01-05T00:30:00.999Z')==[]
    assert repo.get_room_state('R101','2026-01-05T00:30:00.999Z')['occupancy']=='unknown'
    after='2026-01-05T00:30:01Z'
    raw=_raw_events(repo)[0]
    instant=lambda value:datetime.fromisoformat(value.replace('Z','+00:00'))
    assert instant(raw.timestamp_utc)==instant(raw.source.timestamp_utc)==instant(occurred)
    assert raw.available_at_utc==after
    assert instant(repo.get_booking('BK_PRESENT',after)['observed_checkin_utc'])==instant(occurred)
    public=repo.get_events(OCCURRED,AFTER,after)[0]
    assert instant(public['timestamp_utc'])==instant(occurred)
    assert public['available_at_utc']==after
    assert not {'_source_ts','_available_ts'} & public.keys()


@pytest.mark.parametrize('publication',[None,'','nonsense',123,False])
def test_malformed_publication_is_skipped_with_visible_data_quality_diagnostic(make_dataset,publication):
    repo=ReplayData(make_dataset(events=[fact(published=publication)]),use_llm=False)
    assert _raw_events(repo)==[]
    assert repo.get_events(OCCURRED,AFTER,AFTER)==[]
    assert repo.get_events_available_between(OCCURRED,AFTER,AFTER)==[]
    assert repo.get_room_state('R101',AFTER)['occupancy']=='unknown'
    assert repo.get_booking('BK_PRESENT',AFTER)['observed_state']=='not_arrived'
    assert repo.audit['events.jsonl:invalid_publication_timestamps']==1


def test_publication_before_occurrence_never_makes_a_future_event_observed(make_dataset):
    repo=ReplayData(make_dataset(events=[fact(occurred=PUBLISHED,published=OCCURRED)]),use_llm=False)
    assert repo.get_events(OCCURRED,AFTER,BEFORE)==[]
    assert repo.get_room_state('R101',BEFORE)['occupancy']=='unknown'
    raw=_raw_events(repo)[0]
    assert raw.timestamp_utc==raw.available_at_utc==PUBLISHED


def test_untrusted_private_availability_metadata_cannot_override_a_real_publication(make_dataset):
    repo=ReplayData(make_dataset(events=[fact(_available_ts=OCCURRED,_source_ts=AFTER)]),use_llm=False)
    assert repo.get_events(OCCURRED,AFTER,BEFORE)==[]
    raw=_raw_events(repo)[0]
    assert raw.timestamp_utc==OCCURRED
    assert raw.available_at_utc==PUBLISHED


def test_late_older_fractional_checkout_does_not_regress_actual_occurrence_order(make_dataset):
    # Both source instants ceil to00:10:01; event IDs deliberately sort opposite
    # to actual chronology. Publication order must not change the latest fact.
    repo=ReplayData(make_dataset(events=[
        fact('A_NEW_IN','checkin','2026-01-05T00:10:00.900Z',PUBLISHED),
        fact('Z_OLD_OUT','checkout','2026-01-05T00:10:00.100Z',AFTER),
    ]),use_llm=False)
    assert repo.get_room_state('R101',PUBLISHED)['occupancy']=='occupied'
    assert repo.get_room_state('R101',AFTER)['occupancy']=='occupied'
    booking=repo.get_booking('BK_PRESENT',AFTER)
    assert booking['observed_state']=='checked_in'
    assert booking['observed_checkin_utc']=='2026-01-05T00:10:00.9Z'


def test_fractional_move_can_use_the_truly_earlier_known_checkin_in_the_same_grid_second(make_dataset):
    repo=ReplayData(make_dataset(events=[
        fact('Z_FIRST_IN','checkin','2026-01-05T00:10:00.100Z',PUBLISHED),
        fact('A_LATER_MOVE','room_reassigned','2026-01-05T00:10:00.900Z',PUBLISHED,
             from_room='R101',to_room='R102'),
    ]),use_llm=False)
    assert repo.get_room_state('R101',PUBLISHED)['occupancy']=='vacant'
    assert repo.get_room_state('R102',PUBLISHED)['occupancy']=='occupied'
    booking=repo.get_booking('BK_PRESENT',PUBLISHED)
    assert booking['observed_room_id']=='R102'
    assert booking['observed_checkin_utc']=='2026-01-05T00:10:00.1Z'
