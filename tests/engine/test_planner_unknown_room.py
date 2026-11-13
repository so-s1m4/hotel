"""Explicit undeclared identities are rejected; missing identities stay causal."""
from copy import deepcopy

import pytest

from engine.planner import DecisionEngine
from tests.engine.fixtures import BUILDING, FakeGateway, request, slot, state, tick


def setup_repo(with_booking=True):
    repo=FakeGateway()
    repo.states['R101']=[state()]
    repo.slots=[slot()]
    if with_booking:
        repo.bookings=[{'booking_id':'B1','room_id':'R101','observed_room_id':'R101',
            'observed_state':'not_arrived','created_utc':'2026-01-01T00:00:00Z',
            'arrival_utc':'2026-01-06T10:00:00Z','departure_utc':'2026-01-07T10:00:00Z'}]
    return repo


@pytest.mark.parametrize('with_booking',[False,True])
def test_explicit_undeclared_room_is_rejected_without_retarget_or_pending_workflow(with_booking):
    repo=setup_repo(with_booking)
    engine=DecisionEngine(repo,BUILDING)
    event=request(room='R999',rid='explicit:unknown:room')
    before=deepcopy(event.model_dump())
    assert engine.process(event)==[]
    assert event.model_dump()==before
    assert engine.rejections==[{'event_id':event.event_id,'reason':'Unknown candidate asset','asset':'R999'}]
    assert not engine.turnaround.pending
    assert not engine.turnaround.target_rooms
    assert not engine.executor.audit
    assert not engine.calendar.reservations
    assert not engine.calendar.tentative_reservations
    assert not any(name=='booking' for name,_ in repo.calls)
    # Stable rejection identity is idempotent, including when a valid booking
    # could otherwise tempt the detector to silently substitute its room.
    assert engine.process(event.model_copy(deep=True))==[]
    assert len(engine.rejections)==1
    assert engine.event_count==1
    assert not engine.process(tick('2026-01-08T09:00:00Z'))
    assert not engine.turnaround.pending


def test_none_room_still_resolves_from_booking_known_at_request_time():
    engine=DecisionEngine(setup_repo(),BUILDING)
    rows=engine.process(request(room=None))
    cleaning=next(row for row in rows if row.action=='schedule_cleaning')
    assert cleaning.affected_assets==['R101']
    assert 'bookings.csv:B1' in cleaning.evidence
    assert not engine.rejections


def test_none_room_unknown_identity_is_reviewed_then_closed_at_48_hours():
    engine=DecisionEngine(setup_repo(False),BUILDING)
    event=request(room=None)
    first=engine.process(event)
    assert first and all(row.action=='notify_guest' and row.affected_assets==['BUILDING'] for row in first)
    assert all(row.guest_request_ids==['G1'] for row in first)
    assert event.event_id in engine.turnaround.pending
    closed=engine.process(tick('2026-01-08T09:00:00Z'))
    assert not engine.turnaround.pending
    assert any('48-hour' in row.reason for row in closed)
    assert all(row.action=='notify_guest' for row in closed)
    assert not engine.rejections
    assert not engine.calendar.reservations
    assert not engine.calendar.tentative_reservations
def test_rejected_guest_identity_still_advances_known_time_for_pending_approval():
    import pytest
    from engine.planner import DecisionEngine
    from engine.safety import SafetyViolation
    from tests.engine.fixtures import BUILDING, FakeGateway, request
    from tests.engine.test_safety_audit import oncall_proposal

    engine = DecisionEngine(FakeGateway(), BUILDING)
    proposal, context = oncall_proposal("R101")
    context["calendar"] = engine.calendar
    engine.calendar.current_time = proposal.timestamp_utc
    assert engine.executor.execute(proposal, context)["status"] == "pending_human_approval"
    later = request(t="2026-01-06T15:30:00Z", room="R999", rid="INVALID_ROOM")
    assert engine.process(later) == []
    assert engine.calendar.current_time == later.available_at_utc
    with pytest.raises(SafetyViolation):
        engine.executor.execute(proposal, {**context, "human_approval_granted": True})
    assert len(engine.executor.audit) == 1
