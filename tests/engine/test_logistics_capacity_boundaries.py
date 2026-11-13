"""Causal transfer capacity and service discontinuities through DecisionEngine."""
from copy import deepcopy
import pytest
from common.contracts.schemas import NormalizedEventV1
from engine.planner import DecisionEngine
from tests.engine.fixtures import FakeGateway,slot,tick
from tests.engine.test_extended import full_building,logistics_repo


def observation(repo,asset,value,at,metric='fill_pct'):
    repo.measurements.setdefault('logistics.csv',[]).append({'asset_id':asset,'metric':metric,'value':value,'timestamp_utc':at})


def roster(at='2026-01-06T09:15:00Z'):
    return NormalizedEventV1(event_id='roster:'+at,timestamp_utc=at,available_at_utc=at,
        event_type='roster_update',source={'file':'staff_roster.csv','record_id':'SHR'},entities={'resource_id':'ROBOT_1'})


def setup(start='2026-01-06T10:30:00Z',main=30):
    repo=logistics_repo(main=main)
    repo.slots=[slot(resource='ROBOT_1',role='robot',shift_id='SHR',start=start)]
    engine=DecisionEngine(repo,full_building())
    first=next(row for row in engine.process(tick()) if row.action=='dispatch_robot')
    return repo,engine,first


def test_external_future_cancellation_retires_marker_and_restored_capacity_retries():
    repo,engine,first=setup()
    repo.slots[0].update(status='out_of_service',effective_status_asof='out_of_service',status_known_utc='2026-01-06T09:15:00Z')
    cancelled=engine.process(roster())
    assert 'WASTE_F1' not in engine.logistics.open_transfers
    assert not engine.calendar.volume_reservations['WASTE_MAIN']
    assert any(row.action=='defer_action' and first.event_id in row.dependencies for row in cancelled)
    assert all(row.action!='dispatch_robot' for row in cancelled)
    repo.slots[0].update(status='scheduled',effective_status_asof='scheduled',status_known_utc='2026-01-06T09:30:00Z')
    for asset,value in [('WASTE_F1',90),('WASTE_MAIN',30)]:
        observation(repo,asset,value,'2026-01-06T10:00:00Z')
    retried=next(row for row in engine.process(tick('2026-01-06T10:00:00Z')) if row.action=='dispatch_robot')
    assert retried.event_id != first.event_id
    assert engine.calendar.volume_reservations['WASTE_MAIN']==[(retried.event_id,216)]
    assert not engine.rejections


def test_fresh_destination_overflow_withdraws_future_job_on_quarter_hour_tick():
    repo,engine,first=setup()
    observation(repo,'WASTE_MAIN',90,'2026-01-06T09:15:00Z')
    rows=engine.process(tick('2026-01-06T09:15:00Z'))
    assert first.event_id in engine.calendar._cancelled_ids
    assert not engine.calendar.volume_reservations['WASTE_MAIN']
    assert not engine.calendar.reservations['ROBOT_1']
    assert not engine.logistics.open_transfers
    cancellation=next(row for row in rows if row.action=='defer_action' and first.event_id in row.dependencies)
    assert 'logistics.csv:WASTE_MAIN:fill_pct:2026-01-06T09:15:00Z' in cancellation.evidence
    assert all(row.action!='dispatch_robot' for row in rows)


@pytest.mark.parametrize('destination_liters,withdraw',[(829,False),(829.01,True)])
def test_capacity_policy_boundary_includes_every_existing_volume_commitment(destination_liters,withdraw):
    repo,engine,first=setup(main=60)
    observation(repo,'WASTE_MAIN',destination_liters/1100*100,'2026-01-06T09:15:00Z')
    engine.process(tick('2026-01-06T09:15:00Z'))
    assert (first.event_id in engine.calendar._cancelled_ids) is withdraw


def test_elapsed_mock_and_empty_source_do_not_free_stale_destination_capacity():
    repo,engine,first=setup(start='2026-01-06T08:00:00Z',main=60)
    observation(repo,'WASTE_F1',0,'2026-01-06T10:00:00Z')
    observation(repo,'WASTE_F2',90,'2026-01-06T10:00:00Z')
    rows=engine.process(tick('2026-01-06T10:00:00Z'))
    assert 'WASTE_F1' not in engine.logistics.open_transfers
    assert first.event_id not in engine.calendar._cancelled_ids
    assert engine.calendar.volume_reservations['WASTE_MAIN']==[(first.event_id,216)]
    assert first.event_id in engine.logistics.destination_commitments
    assert not any(row.action=='dispatch_robot' and 'WASTE_F2' in row.affected_assets for row in rows)
    assert any(row.action=='flag_anomaly' and 'WASTE_F2' in row.affected_assets for row in rows)
    # 660 observed + 216 uncertain + 216 new would exceed the 1045 L policy.
    assert 660+216+216 > 1100*.95
    observation(repo,'WASTE_MAIN',60,'2026-01-06T10:15:00Z')
    engine.process(tick('2026-01-06T10:15:00Z'))
    assert not engine.calendar.volume_reservations['WASTE_MAIN']
    for asset,value in [('WASTE_F1',0),('WASTE_F2',90),('WASTE_MAIN',60)]:
        observation(repo,asset,value,'2026-01-06T11:00:00Z')
    next_rows=engine.process(tick('2026-01-06T11:00:00Z'))
    assert any(row.action=='dispatch_robot' and row.affected_assets==['WASTE_F2'] for row in next_rows)


def test_fresh_main_alone_does_not_verify_an_unobserved_full_source_task():
    repo,engine,first=setup(start='2026-01-06T08:00:00Z',main=60)
    observation(repo,'WASTE_MAIN',30,'2026-01-06T10:00:00Z')
    observation(repo,'WASTE_F1',90,'2026-01-06T10:00:00Z')
    engine.process(tick('2026-01-06T10:00:00Z'))
    assert engine.calendar.volume_reservations['WASTE_MAIN']==[(first.event_id,216)]
    assert engine.logistics.open_transfers['WASTE_F1']['decision_id']==first.event_id


@pytest.mark.parametrize('pickup_asset,pickup_time,released',[
    ('WASTE_MAIN','2026-01-06T08:30:00Z',False),
    ('OTHER_CONTAINER','2026-01-06T09:30:00Z',False),
    ('WASTE_MAIN','2026-01-06T09:30:00Z',True),
])
def test_only_matching_post_end_pickup_clears_uncertain_commitment(pickup_asset,pickup_time,released):
    repo,engine,first=setup(start='2026-01-06T08:00:00Z')
    repo.events.append({'event_id':'PICKUP','type':'waste_pickup','asset_id':pickup_asset,'timestamp_utc':pickup_time})
    observation(repo,'WASTE_F1',0,'2026-01-06T10:00:00Z')
    engine.process(tick('2026-01-06T10:00:00Z'))
    assert (not engine.calendar.volume_reservations['WASTE_MAIN']) is released


def test_fresh_empty_source_before_start_cancels_unnecessary_future_transport():
    repo,engine,first=setup()
    observation(repo,'WASTE_F1',0,'2026-01-06T09:15:00Z')
    rows=engine.process(tick('2026-01-06T09:15:00Z'))
    assert first.event_id in engine.calendar._cancelled_ids
    assert not engine.calendar.volume_reservations['WASTE_MAIN']
    assert any(row.action=='defer_action' and first.event_id in row.dependencies for row in rows)


def vending(repo,stocks=(70,60,50)):
    for hour,value in zip((7,8,9),stocks):
        observation(repo,'VEND1',value,f'2026-01-06T{hour:02}:00:00Z','stock_drinks')


@pytest.mark.parametrize('event_asset,event_known,has_forecast',[
    ('VEND1','2026-01-06T08:30:00Z',False),
    ('OTHER_VENDOR','2026-01-06T08:30:00Z',True),
    ('VEND1','2026-01-06T09:30:00Z',True),
])
def test_known_restock_cuts_pre_event_trend_without_reading_future_restock(event_asset,event_known,has_forecast):
    repo=FakeGateway()
    vending(repo)
    repo.events=[{'event_id':'RESTOCK','type':'vending_restocked','asset_id':event_asset,
                  'timestamp_utc':'2026-01-06T08:30:00Z','available_at_utc':event_known}]
    rows=DecisionEngine(repo,full_building()).process(tick())
    assert any(row.affected_assets==['VEND1'] and 'trend implies' in row.reason for row in rows) is has_forecast


def test_low_current_stock_remains_actionable_without_claiming_single_sample_trend():
    repo=FakeGateway()
    vending(repo,stocks=(30,20,10))
    repo.events=[{'event_id':'RESTOCK','type':'vending_restocked','asset_id':'VEND1','timestamp_utc':'2026-01-06T08:30:00Z'}]
    rows=DecisionEngine(repo,full_building()).process(tick())
    service=next(row for row in rows if row.affected_assets==['VEND1'])
    assert 'time is uncertain' in service.reason
    assert 'trend implies' not in service.reason
    assert service.action=='order_service'


def test_pickup_before_future_transfer_end_does_not_release_its_volume_hold():
    repo,engine,first=setup()
    repo.events.append({'event_id':'PICKUP','type':'waste_pickup','asset_id':'WASTE_MAIN',
                        'timestamp_utc':'2026-01-06T09:15:00Z'})
    rows=engine.process(tick('2026-01-06T09:15:00Z'))
    assert first.event_id not in engine.calendar._cancelled_ids
    assert engine.calendar.volume_reservations['WASTE_MAIN']==[(first.event_id,216)]
    assert not any(row.action=='dispatch_robot' for row in rows)


def test_pre_recovery_destination_sample_does_not_clear_uncertain_volume():
    repo,engine,first=setup(start='2026-01-06T08:00:00Z',main=60)
    observation(repo,'WASTE_MAIN',60,'2026-01-06T09:45:00Z')
    observation(repo,'WASTE_F1',0,'2026-01-06T10:00:00Z')
    engine.process(tick('2026-01-06T10:00:00Z'))
    assert engine.calendar.volume_reservations['WASTE_MAIN']==[(first.event_id,216)]


def test_cancellation_reconciliation_is_rolled_back_if_later_detector_fails(monkeypatch):
    from tests.engine.test_planner_transactions import frozen
    repo,engine,first=setup()
    repo.slots[0].update(status='out_of_service',effective_status_asof='out_of_service',status_known_utc='2026-01-06T09:15:00Z')
    before=frozen(engine)
    original=engine.optimization.detect
    def fail_after_marker_cleanup(*args,**kwargs):
        assert 'WASTE_F1' not in engine.logistics.open_transfers
        raise OSError('after marker reconciliation')
    monkeypatch.setattr(engine.optimization,'detect',fail_after_marker_cleanup)
    with pytest.raises(OSError,match='marker reconciliation'):
        engine.process(roster())
    assert frozen(engine)==before
    assert engine.logistics.open_transfers['WASTE_F1']['decision_id']==first.event_id
    monkeypatch.setattr(engine.optimization,'detect',original)
    engine.process(roster())
    assert 'WASTE_F1' not in engine.logistics.open_transfers
    assert first.event_id in engine.calendar._cancelled_ids


def test_capacity_withdrawal_is_rolled_back_if_later_detector_fails(monkeypatch):
    from tests.engine.test_planner_transactions import frozen
    repo,engine,first=setup()
    observation(repo,'WASTE_MAIN',90,'2026-01-06T09:15:00Z')
    before=frozen(engine)
    original=engine.optimization.detect
    def fail_after_capacity_withdrawal(*args,**kwargs):
        assert first.event_id in engine.calendar._cancelled_ids
        raise OSError('after capacity withdrawal')
    monkeypatch.setattr(engine.optimization,'detect',fail_after_capacity_withdrawal)
    with pytest.raises(OSError,match='capacity withdrawal'):
        engine.process(tick('2026-01-06T09:15:00Z'))
    assert frozen(engine)==before
    monkeypatch.setattr(engine.optimization,'detect',original)
    rows=engine.process(tick('2026-01-06T09:15:00Z'))
    assert sum(row.action=='defer_action' and first.event_id in row.dependencies for row in rows)==1


@pytest.mark.parametrize('policy_pct,main_liters',[(95,825),(100,880)])
def test_fresh_source_growth_withdraws_future_whole_bin_command_before_overflow(policy_pct,main_liters):
    from engine.core import EngineConfig
    repo=logistics_repo(main=main_liters/1100*100)
    repo.slots=[slot(resource='ROBOT_1',role='robot',shift_id='SHR',start='2026-01-06T10:30:00Z')]
    engine=DecisionEngine(repo,full_building(),EngineConfig(container_max_fill_pct=policy_pct))
    first=next(row for row in engine.process(tick()) if row.action=='dispatch_robot')
    observation(repo,'WASTE_F1',100,'2026-01-06T09:15:00Z')
    rows=engine.process(tick('2026-01-06T09:15:00Z'))
    assert main_liters+216 <= 1100*policy_pct/100
    assert main_liters+240 > 1100*policy_pct/100
    assert first.event_id in engine.calendar._cancelled_ids
    assert not engine.calendar.reservations['ROBOT_1']
    assert not engine.calendar.volume_reservations['WASTE_MAIN']
    assert 'WASTE_F1' not in engine.logistics.open_transfers
    assert first.event_id not in engine.logistics.destination_commitments
    correction=next(row for row in rows if row.action=='defer_action' and first.event_id in row.dependencies)
    assert 'logistics.csv:WASTE_F1:fill_pct:2026-01-06T09:15:00Z' in correction.evidence
    assert all(row.action!='dispatch_robot' for row in rows)
    original_audit=next(row for row in engine.executor.audit if row['decision_id']==first.event_id)
    assert original_audit['parameters']['transfer_liters']==216


def test_source_growth_with_headroom_requires_new_id_and_retains_enlarged_uncertain_commitment():
    repo,engine,first=setup(main=600/1100*100)
    observation(repo,'WASTE_F1',100,'2026-01-06T09:15:00Z')
    quarter_rows=engine.process(tick('2026-01-06T09:15:00Z'))
    assert first.event_id in engine.calendar._cancelled_ids
    assert all(row.action!='dispatch_robot' for row in quarter_rows)
    assert not engine.calendar.volume_reservations['WASTE_MAIN']
    observation(repo,'WASTE_F1',100,'2026-01-06T10:00:00Z')
    observation(repo,'WASTE_MAIN',600/1100*100,'2026-01-06T10:00:00Z')
    second=next(row for row in engine.process(tick('2026-01-06T10:00:00Z')) if row.action=='dispatch_robot')
    assert second.event_id!=first.event_id
    assert engine.calendar.volume_reservations['WASTE_MAIN']==[(second.event_id,240)]
    assert engine._proposals[first.event_id].context['transfer_liters']==216
    assert engine._proposals[second.event_id].context['transfer_liters']==240
    # The second task's scheduled end is 10:50. Source empty at11:00 does
    # not prove destination capacity, whose last observation remains10:00.
    observation(repo,'WASTE_F1',0,'2026-01-06T11:00:00Z')
    observation(repo,'WASTE_F2',90,'2026-01-06T11:00:00Z')
    later_rows=engine.process(tick('2026-01-06T11:00:00Z'))
    assert second.event_id not in engine.calendar._cancelled_ids
    assert (second.event_id,240) in engine.calendar.volume_reservations['WASTE_MAIN']
    assert first.event_id not in dict(engine.calendar.volume_reservations['WASTE_MAIN'])
    assert second.event_id in engine.logistics.destination_commitments
    # The obsolete216L amount would permit a second216L transfer; the
    # actual240L uncertainty must block it against the stale600L destination.
    assert 600+216+216 <= 1045 < 600+240+216
    assert not any(row.action=='dispatch_robot' and row.affected_assets==['WASTE_F2'] for row in later_rows)
    assert any(row.action=='flag_anomaly' and row.affected_assets==['WASTE_F2'] for row in later_rows)


@pytest.mark.parametrize('value',[None,-1,101,float('nan')])
def test_invalid_latest_source_does_not_zero_or_resize_future_volume_commitment(value):
    repo,engine,first=setup()
    observation(repo,'WASTE_F1',value,'2026-01-06T09:15:00Z')
    rows=engine.process(tick('2026-01-06T09:15:00Z'))
    assert first.event_id not in engine.calendar._cancelled_ids
    assert engine.calendar.volume_reservations['WASTE_MAIN']==[(first.event_id,216)]
    assert all(row.action!='dispatch_robot' for row in rows)


def test_future_source_growth_cannot_change_a_present_reservation():
    repo,engine,first=setup()
    observation(repo,'WASTE_F1',100,'2026-01-06T09:30:00Z')
    engine.process(tick('2026-01-06T09:15:00Z'))
    assert first.event_id not in engine.calendar._cancelled_ids
    assert engine.calendar.volume_reservations['WASTE_MAIN']==[(first.event_id,216)]


def test_stale_source_growth_does_not_infer_zero_or_rewrite_the_old_command():
    repo,engine,first=setup(start='2026-01-06T10:35:00Z')
    # No scan disclosed this reading while fresh. At10:30 it is75min old.
    observation(repo,'WASTE_F1',100,'2026-01-06T09:15:00Z')
    observation(repo,'WASTE_MAIN',30,'2026-01-06T10:30:00Z')
    engine.process(tick('2026-01-06T10:30:00Z'))
    assert first.event_id not in engine.calendar._cancelled_ids
    assert engine.calendar.volume_reservations['WASTE_MAIN']==[(first.event_id,216)]
    assert engine._proposals[first.event_id].context['transfer_liters']==216


def test_post_start_source_growth_preserves_the_unverified_destination_commitment():
    repo,engine,first=setup(start='2026-01-06T08:00:00Z')
    observation(repo,'WASTE_F1',100,'2026-01-06T10:00:00Z')
    rows=engine.process(tick('2026-01-06T10:00:00Z'))
    assert first.event_id not in engine.calendar._cancelled_ids
    assert engine.calendar.volume_reservations['WASTE_MAIN']==[(first.event_id,216)]
    assert engine.logistics.open_transfers['WASTE_F1']['decision_id']==first.event_id
    assert all(row.action!='dispatch_robot' for row in rows)


def test_growth_at_the_planned_start_boundary_withdraws_before_assuming_execution():
    repo,engine,first=setup()
    observation(repo,'WASTE_F1',100,'2026-01-06T10:30:00Z')
    observation(repo,'WASTE_MAIN',30,'2026-01-06T10:30:00Z')
    rows=engine.process(tick('2026-01-06T10:30:00Z'))
    assert first.event_id in engine.calendar._cancelled_ids
    assert not engine.calendar.volume_reservations['WASTE_MAIN']
    assert any(row.action=='defer_action' and first.event_id in row.dependencies for row in rows)


def test_fresh_source_reduction_also_requires_replanning_the_whole_bin_command():
    repo,engine,first=setup()
    observation(repo,'WASTE_F1',80,'2026-01-06T09:15:00Z')
    engine.process(tick('2026-01-06T09:15:00Z'))
    assert first.event_id in engine.calendar._cancelled_ids
    assert not engine.calendar.volume_reservations['WASTE_MAIN']
    assert engine._proposals[first.event_id].context['transfer_liters']==216


def test_source_volume_replan_is_atomic_when_a_later_detector_read_fails(monkeypatch):
    from tests.engine.test_planner_transactions import frozen
    repo,engine,first=setup()
    observation(repo,'WASTE_F1',100,'2026-01-06T09:15:00Z')
    before=frozen(engine)
    original=engine.optimization.detect
    def fail_after_growth_withdrawal(*args,**kwargs):
        assert first.event_id in engine.calendar._cancelled_ids
        assert not engine.calendar.volume_reservations['WASTE_MAIN']
        raise OSError('after whole-bin command withdrawal')
    monkeypatch.setattr(engine.optimization,'detect',fail_after_growth_withdrawal)
    with pytest.raises(OSError,match='whole-bin command withdrawal'):
        engine.process(tick('2026-01-06T09:15:00Z'))
    assert frozen(engine)==before
    monkeypatch.setattr(engine.optimization,'detect',original)
    rows=engine.process(tick('2026-01-06T09:15:00Z'))
    assert sum(row.action=='defer_action' and first.event_id in row.dependencies for row in rows)==1
