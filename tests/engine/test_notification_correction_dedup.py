"""A new cancellation remains visible when merged with an unchanged estimate."""
from engine.core import Candidate
from engine.planner import DecisionEngine
from tests.engine.fixtures import BUILDING, FakeGateway, tick


def notice(key,reason,variant,*,accepted=False):
    return Candidate(key,'R101','room_turnaround','notify_guest',reason,
        ['guest_requests.csv:G1'],'Verify the current plan before access.',
        'Propose a message without claiming delivery.',priority='high',variant=variant,
        request_ids=['G1'],context={'accepted_plan':accepted})


def enqueue(engine,cancellation,now,*,merged=True,estimate='Proposed readiness at 10:00, conditional on observed completion.',episode='episode:readiness',variant='estimate:0'):
    correction=notice('cancel_notice:'+cancellation,'Withdraw the superseded conditional readiness estimate.',
                      'cancelled_plan:'+cancellation)
    record=correction.record(now,'episode:correction:'+cancellation)
    record.dependencies=[cancellation]
    engine._outbox=[(correction,record,'booking:B1')]
    if merged:
        candidate=notice('turnaround:guest:G1',estimate,variant,accepted=True)
        decision=candidate.record(now,episode)
        decision.dependencies=['accepted:new-job']
        engine._outbox.append((candidate,decision,'booking:B1'))


def test_new_cancellation_is_not_lost_when_the_accepted_estimate_text_repeats():
    engine=DecisionEngine(FakeGateway(),BUILDING)
    enqueue(engine,'cancel:A','2026-01-06T09:00:00Z')
    assert len(engine._flush_notifications(tick()))==1
    enqueue(engine,'cancel:B','2026-01-06T09:01:00Z',episode='episode:next-readiness')
    rows=engine._flush_notifications(tick('2026-01-06T09:01:00Z'))
    assert len(rows)==1
    assert set(rows[0].dependencies)=={'accepted:new-job','cancel:B'}
    assert not engine._outbox


def test_same_cancellation_and_identical_merged_estimate_still_do_not_spam():
    engine=DecisionEngine(FakeGateway(),BUILDING)
    enqueue(engine,'cancel:A','2026-01-06T09:00:00Z')
    assert len(engine._flush_notifications(tick()))==1
    enqueue(engine,'cancel:A','2026-01-06T09:01:00Z')
    assert engine._flush_notifications(tick('2026-01-06T09:01:00Z'))==[]
    assert not engine._outbox
    assert len(engine.executor.audit)==1


def test_standalone_new_correction_control_is_not_subject_to_estimate_dedup():
    engine=DecisionEngine(FakeGateway(),BUILDING)
    enqueue(engine,'cancel:A','2026-01-06T09:00:00Z')
    engine._flush_notifications(tick())
    enqueue(engine,'cancel:B','2026-01-06T09:01:00Z',merged=False)
    rows=engine._flush_notifications(tick('2026-01-06T09:01:00Z'))
    assert len(rows)==1 and rows[0].dependencies==['cancel:B']


def test_distinct_merged_cancellations_have_distinct_official_ids_even_in_one_episode():
    engine=DecisionEngine(FakeGateway(),BUILDING)
    enqueue(engine,'cancel:A','2026-01-06T09:00:00Z')
    first=engine._flush_notifications(tick())[0]
    enqueue(engine,'cancel:B','2026-01-06T09:00:00Z')
    second=engine._flush_notifications(tick())
    assert len(second)==1 and second[0].event_id!=first.event_id
    assert 'cancel:B' in second[0].dependencies


def test_updated_estimate_with_same_correction_is_still_a_legitimate_new_plan():
    engine=DecisionEngine(FakeGateway(),BUILDING)
    enqueue(engine,'cancel:A','2026-01-06T09:00:00Z')
    engine._flush_notifications(tick())
    enqueue(engine,'cancel:A','2026-01-06T09:01:00Z',estimate='Proposed readiness at 10:45, subject to observed release checks.',variant='estimate:1')
    rows=engine._flush_notifications(tick('2026-01-06T09:01:00Z'))
    assert len(rows)==1 and '10:45' in rows[0].reason
    assert set(rows[0].dependencies)=={'accepted:new-job','cancel:A'}
