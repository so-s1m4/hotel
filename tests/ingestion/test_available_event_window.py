"""Optional stock capability distinguishes availability from occurrence windows."""
from copy import deepcopy
from datetime import datetime
import json

import pytest

from ingestion.replay import ReplayData


@pytest.fixture
def repo_factory(tmp_path):
    def build(events):
        directory=tmp_path/str(len(list(tmp_path.iterdir())))
        directory.mkdir()
        (directory/'events.jsonl').write_text(''.join(json.dumps(event)+'\n' for event in events))
        return ReplayData(directory,use_llm=False)
    return build


def event(ident,occurred,available=None,**fields):
    result={'event_id':ident,'timestamp_utc':occurred,'type':'cleaning_completed','room_id':'R101',**fields}
    if available is not None:
        result['available_at_utc']=available
    return result


def ids(rows):return [row['event_id'] for row in rows]


EARLY='2026-01-06T08:30:00Z'
CURSOR='2026-01-06T08:45:00Z'
NOW='2026-01-06T09:00:00Z'
LATER='2026-01-06T09:15:00Z'


def test_late_known_old_occurrence_is_returned_after_the_availability_cursor(repo_factory):
    repo=repo_factory([event('LATE',EARLY,NOW)])
    assert repo.get_events(CURSOR,NOW,NOW)==[]  # occurrence-window API stays intact
    rows=repo.get_events_available_between(CURSOR,NOW,NOW)
    assert ids(rows)==['LATE']
    assert rows[0]['timestamp_utc']==EARLY
    assert rows[0]['available_at_utc']==NOW
    assert repo.get_events_available_between(CURSOR,NOW,CURSOR)==[]


def test_window_is_inclusive_in_availability_and_clipped_by_asof(repo_factory):
    repo=repo_factory([event('BEFORE',EARLY),event('START',EARLY,CURSOR),event('END',EARLY,NOW),event('FUTURE',EARLY,LATER)])
    assert ids(repo.get_events_available_between(CURSOR,LATER,NOW))==['START','END']
    assert ids(repo.get_events_available_between(NOW,NOW,NOW))==['END']
    assert repo.get_events_available_between(LATER,NOW,NOW)==[]
    assert repo.get_events_available_between(LATER,LATER,NOW)==[]


def test_default_availability_is_loader_grid_but_occurrence_precision_survives(repo_factory):
    precise='2026-01-06T09:00:00.500Z'
    repo=repo_factory([event('PRECISE',precise)])
    assert repo.get_events_available_between(CURSOR,LATER,'2026-01-06T09:00:00.999999Z')==[]
    rows=repo.get_events_available_between(CURSOR,LATER,'2026-01-06T09:00:01Z')
    assert datetime.fromisoformat(rows[0]['timestamp_utc'].replace('Z','+00:00'))==datetime.fromisoformat(precise.replace('Z','+00:00'))
    assert rows[0]['available_at_utc']=='2026-01-06T09:00:01Z'


def test_explicit_precise_future_availability_is_not_replaced_by_old_occurrence(repo_factory):
    repo=repo_factory([event('PRECISE_LATE',EARLY,'2026-01-06T09:00:00.250Z')])
    assert repo.get_events_available_between(CURSOR,LATER,'2026-01-06T09:00:00.750Z')==[]
    rows=repo.get_events_available_between(CURSOR,LATER,'2026-01-06T09:00:01Z')
    assert ids(rows)==['PRECISE_LATE']
    assert rows[0]['available_at_utc']=='2026-01-06T09:00:01Z'


def test_precise_start_bound_does_not_reinclude_earlier_whole_second(repo_factory):
    repo=repo_factory([event('BEFORE',EARLY,NOW),event('AFTER',EARLY,'2026-01-06T09:00:01Z')])
    assert ids(repo.get_events_available_between('2026-01-06T09:00:00.500Z',LATER,LATER))==['AFTER']


def test_same_instant_offset_bounds_are_supported_without_lexicographic_leakage(repo_factory):
    repo=repo_factory([event('NOW',EARLY,NOW)])
    assert ids(repo.get_events_available_between('2026-01-06T10:00:00+01:00','2026-01-06T10:00:00+01:00',NOW))==['NOW']


def test_future_occurrence_cannot_be_exposed_by_an_earlier_claimed_availability(repo_factory):
    repo=repo_factory([event('NOT_OCCURRED',LATER,EARLY)])
    assert repo.get_events_available_between(EARLY,NOW,NOW)==[]
    rows=repo.get_events_available_between(EARLY,LATER,LATER)
    assert rows[0]['timestamp_utc']==LATER
    assert rows[0]['available_at_utc']==LATER


@pytest.mark.parametrize('invalid',[None,'','not-a-timestamp',123,False])
def test_explicit_unknown_or_invalid_availability_is_not_silently_treated_as_occurrence(repo_factory,invalid):
    raw=event('UNKNOWN',EARLY)
    raw['available_at_utc']=invalid
    repo=repo_factory([raw])
    assert repo.get_events_available_between(EARLY,LATER,LATER)==[]
    assert ids(repo.get_events(EARLY,LATER,LATER))==['UNKNOWN']


def test_results_are_independent_deep_copies_even_for_arbitrary_nested_source_fields(repo_factory):
    raw=event('NESTED',EARLY,NOW,details={'jobs':[{'id':'original'}]},source_fields={'original':['kept']})
    repo=repo_factory([raw])
    first=repo.get_events_available_between(CURSOR,NOW,NOW)
    expected=deepcopy(first)
    first[0]['details']['jobs'][0]['id']='mutated'
    first[0]['source_fields']['original'].append('injected')
    first[0]['available_at_utc']=LATER
    assert repo.get_events_available_between(CURSOR,NOW,NOW)==expected
    assert repo.get_events(EARLY,NOW,NOW)[0]['details']['jobs'][0]['id']=='original'


def test_equal_availability_order_is_deterministic_and_index_reused_without_full_rescan(repo_factory):
    repo=repo_factory([event('Z',EARLY,NOW),event('A',CURSOR,NOW),event('OTHER',EARLY,LATER)])
    assert ids(repo.get_events_available_between(CURSOR,NOW,NOW))==['A','Z']
    index=repo._event_index()
    class NoIteration(list):
        def __iter__(self):
            raise AssertionError('An incremental query must not rescan all cached facts')
        def __getitem__(self,key):
            if isinstance(key,slice):
                raise AssertionError('An incremental query must not copy the full history')
            return super().__getitem__(key)
    # The query may index only the requested available rows, not walk either
    # original occurrence history or the complete availability index.
    index['all']=NoIteration(index['all'])
    index['available']=NoIteration(index['available'])
    assert ids(repo.get_events_available_between(CURSOR,NOW,NOW))==['A','Z']
    assert repo._event_index() is index
