"""Regressions grounded in the supplied independent review, not incident labels."""
from types import SimpleNamespace

from ingestion.events import _work_order_events
from ingestion.replay import work_order_view
from tests.engine.fixtures import FakeGateway, state, request
from engine.planner import DecisionEngine
from tests.engine.fixtures import BUILDING


def test_f1_future_work_order_outcome_does_not_change_historical_creation():
    row = {"work_order_id": "WO_REVIEW", "asset_id": "WM_R105", "type": "metering", "status": "completed",
           "created_utc": "2026-07-01T21:00:00Z", "scheduled_utc": "2026-07-04T21:00:00Z",
           "completed_utc": "2026-07-04T21:15:00Z", "summary": "Counter exchanged after later diagnosis", "assigned_to": "TECH_1"}
    alternate = {**row, "status": "postponed", "completed_utc": None, "summary": "Later contractor failure"}
    assert work_order_view(row, row["created_utc"]) == work_order_view(alternate, row["created_utc"])
    def creation(value):
        repo = SimpleNamespace(ds=SimpleNamespace(room_ids={"R105"}, work_orders=[value]))
        return next(e for e in _work_order_events(repo) if e.event_type == "work_order_created")
    assert creation(row).model_dump_json() == creation(alternate).model_dump_json()


def test_f3_independent_explicit_guest_queries_are_acknowledged_not_blanket_suppressed():
    repo = FakeGateway()
    repo.states["R101"] = [state(occupancy="occupied")]
    engine = DecisionEngine(repo, BUILDING)
    first = request(rid="G1")
    second = request(t="2026-01-06T09:01:00Z", rid="G2")
    rows = engine.process(first)+engine.process(second)
    assert sum(r.action == "notify_guest" for r in rows) == 2
