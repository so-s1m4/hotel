"""A failed composite reservation leaves the proposal safe to retry unchanged."""
from copy import deepcopy

import pytest

from engine.core import Candidate
from engine.executor import MockExecutor
from engine.resources import ResourceCalendar
from engine.safety import SafetyViolation
from tests.engine.fixtures import BUILDING, slot, state


NOW = "2026-01-06T09:00:00Z"
END = "2026-01-06T09:20:00Z"
CAPACITY_FIELDS = (
    "reservations", "room_reservations", "tentative_reservations",
    "tentative_room_reservations", "volume_reservations",
)
OBSERVED_FIELDS = ("observed_busy", "resource_roles", "current_time", "cancellations", "_cancelled_ids")


def _command(*, human=False, room=False):
    building = deepcopy(BUILDING)
    source = {"id": "WASTE_F1", "volume_l": 240}
    destination = {"id": "WASTE_MAIN", "volume_l": 1100}
    building["building"]["assets"].extend([source, destination])
    calendar, executor = ResourceCalendar(), MockExecutor()
    calendar.current_time = NOW
    calendar.resource_roles = {r["resource_id"]: r["role"] for r in building["staff"]["resources"]}
    decision = Candidate(
        "atomic:robot", "R101" if room else "WASTE_F1", "room_turnaround" if room else "logistics", "dispatch_robot",
        "Observed work with a verified robot slot",
        [("room_telemetry.csv:R101:" if room else "logistics.csv:WASTE_F1:") + NOW],
        "Check the observed task outcome", "Propose one robot task", resource="ROBOT_1", human=human,
    ).record(NOW, "atomic:robot")
    context = {
        "building": building, "calendar": calendar, "start_utc": NOW, "end_utc": END,
        "slot": slot(resource="ROBOT_1", role="robot"),
        "task": "floor_cleaning" if room else "carry_bin",
    }
    if room:
        context["room"] = state()
    else:
        context.update(
            common_area_route=True, source_bin=source, destination_bin=destination,
            source_observation={"asset_id": source["id"], "timestamp_utc": NOW, "metric": "fill_pct", "value": 90},
            destination_observation={"asset_id": destination["id"], "timestamp_utc": NOW, "metric": "fill_pct", "value": 30},
            transfer_liters=216, container_max_fill_pct=95,
        )
    # Existing work exercises preservation of every capacity ledger and nonempty
    # executor history; the failed command must not clear somebody else's work.
    prior = Candidate(
        "prior", "R102", "coordination", "notify_guest", "Known request", ["events.jsonl:prior"],
        "Check actual delivery", "Propose a message",
    ).record(NOW, "atomic:prior")
    executor.execute(prior, {"building": building, "calendar": calendar})
    calendar.reserve("ROBOT_1", "2026-01-06T10:00:00Z", "2026-01-06T10:20:00Z",
                     "unrelated:accepted", NOW, room="R102")
    calendar.reserve_tentative("CLEANER_1", "2026-01-06T11:00:00Z", "2026-01-06T11:45:00Z",
                               "unrelated:pending", NOW, room="R101")
    calendar.reserve_volume("WASTE_MAIN", 25, "unrelated:volume")
    calendar.reserve_volume("WASTE_MAIN", 1, "withdrawn:other")
    calendar.cancel("withdrawn:other")
    # available() can prune this during validation. A failed execution must still
    # restore its complete entry state, including preexisting expired capacity.
    calendar.reserve("ROBOT_1", "2026-01-06T08:00:00Z", "2026-01-06T08:20:00Z",
                     "expired:robot", "2026-01-06T08:00:00Z", room="R101")
    calendar.observe_start("TECH_1", None, "observed:maintenance", NOW, role="technician")
    return executor, calendar, decision, context


def _calendar_state(calendar):
    return deepcopy({name: getattr(calendar, name) for name in (*CAPACITY_FIELDS, *OBSERVED_FIELDS)})


def _executor_state(executor):
    return {
        "audit": deepcopy(executor.audit),
        "submitted": deepcopy(executor._submitted),
        "completed": deepcopy(executor._completed),
        # Preserve the actual originating calendars; deepcopy would hide a
        # replacement calendar introduced while rolling back the failed command.
        "submitted_calendars": {key: tuple(values) for key, values in executor._submitted_calendars.items()},
    }


def _assert_healthy_retry(executor, calendar, decision, context):
    result = executor.execute(decision, context)
    assert result["status"] == "simulated"
    row = (NOW, END, decision.event_id)
    assert [r for r in calendar.reservations_for("ROBOT_1", include_tentative=False)
            if r[2] == decision.event_id] == [row]
    assert not any(r[2] == decision.event_id for r in calendar.tentative_reservations.get("ROBOT_1", []))
    if context["task"] == "carry_bin":
        assert [r for r in calendar.volume_reservations["WASTE_MAIN"] if r[0] == decision.event_id] == [
            (decision.event_id, 216),
        ]
    else:
        assert [r for r in calendar.room_jobs("R101", include_tentative=False) if r[2] == decision.event_id] == [row]
    assert executor._submitted_calendars[decision.event_id] == [calendar]
    assert all(not row["effects_applied"] and not row["observed_history_changed"] for row in executor.audit)
    after = (_calendar_state(calendar), _executor_state(executor))
    assert executor.execute(decision, context) == result
    assert (_calendar_state(calendar), _executor_state(executor)) == after


@pytest.mark.parametrize("error_type", [SafetyViolation, ValueError], ids=["safety-violation", "value-error"])
@pytest.mark.parametrize("write_volume", [False, True], ids=["before-volume-write", "after-volume-write"])
def test_failed_bin_transfer_restores_state_and_allows_identical_retry(monkeypatch, error_type, write_volume):
    executor, calendar, decision, context = _command()
    calendar_before, executor_before = _calendar_state(calendar), _executor_state(executor)
    ledger_refs = {name: getattr(calendar, name) for name in CAPACITY_FIELDS}
    observed_ref = calendar.observed_busy
    reserve_volume = calendar.reserve_volume
    failure = error_type("Injected destination reservation failure")
    calls = []

    def fail_volume(destination, amount, decision_id):
        calls.append(decision_id)
        assert (NOW, END, decision_id) in calendar.reservations["ROBOT_1"]
        if write_volume:
            reserve_volume(destination, amount, decision_id)
        raise failure

    with monkeypatch.context() as injected:
        injected.setattr(calendar, "reserve_volume", fail_volume)
        with pytest.raises(SafetyViolation, match="Injected destination reservation failure"):
            executor.execute(decision, context)

    assert calls == [decision.event_id]
    assert context["calendar"] is calendar
    assert all(getattr(calendar, name) is value for name, value in ledger_refs.items())
    assert calendar.observed_busy is observed_ref
    assert _calendar_state(calendar) == calendar_before
    assert _executor_state(executor) == executor_before
    _assert_healthy_retry(executor, calendar, decision, context)


@pytest.mark.parametrize("error_type", [SafetyViolation, ValueError], ids=["safety-violation", "value-error"])
def test_failed_bin_approval_restores_its_existing_pending_hold(monkeypatch, error_type):
    executor, calendar, decision, context = _command(human=True)
    pending = executor.execute(decision, context)
    assert pending["status"] == "pending_human_approval"
    assert (NOW, END, decision.event_id) in calendar.tentative_reservations["ROBOT_1"]
    calendar_before, executor_before = _calendar_state(calendar), _executor_state(executor)
    reserve_volume = calendar.reserve_volume
    approved_context = {**context, "human_approval_granted": True}

    def fail_volume(destination, amount, decision_id):
        assert (NOW, END, decision_id) in calendar.reservations["ROBOT_1"]
        assert all(r[2] != decision_id for r in calendar.tentative_reservations["ROBOT_1"])
        reserve_volume(destination, amount, decision_id)
        raise error_type("Injected approval reservation failure")

    with monkeypatch.context() as injected:
        injected.setattr(calendar, "reserve_volume", fail_volume)
        with pytest.raises(SafetyViolation, match="Injected approval reservation failure"):
            executor.execute(decision, approved_context)

    assert _calendar_state(calendar) == calendar_before
    assert _executor_state(executor) == executor_before
    assert executor.audit[-1] == pending
    _assert_healthy_retry(executor, calendar, decision, approved_context)


def test_failed_room_approval_restores_both_worker_and_room_pending_holds(monkeypatch):
    executor, calendar, decision, context = _command(human=True, room=True)
    assert executor.execute(decision, context)["status"] == "pending_human_approval"
    row = (NOW, END, decision.event_id)
    assert row in calendar.tentative_reservations["ROBOT_1"]
    assert row in calendar.tentative_room_reservations["R101"]
    calendar_before, executor_before = _calendar_state(calendar), _executor_state(executor)
    reserve = calendar.reserve
    approved_context = {**context, "human_approval_granted": True}

    def fail_after_reserve(*args, **kwargs):
        reserve(*args, **kwargs)
        assert row in calendar.reservations["ROBOT_1"]
        assert row in calendar.room_reservations["R101"]
        raise SafetyViolation("Injected failure after worker and room upgrade")

    with monkeypatch.context() as injected:
        injected.setattr(calendar, "reserve", fail_after_reserve)
        with pytest.raises(SafetyViolation, match="Injected failure after worker and room upgrade"):
            executor.execute(decision, approved_context)

    assert _calendar_state(calendar) == calendar_before
    assert _executor_state(executor) == executor_before
    _assert_healthy_retry(executor, calendar, decision, approved_context)


def test_failed_audit_append_rolls_back_reservations_and_submission_bookkeeping():
    executor, calendar, decision, context = _command()

    class FailOnceAfterAppend(list):
        failed = False

        def append(self, record):
            super().append(record)
            if record["decision_id"] == decision.event_id and not self.failed:
                self.failed = True
                assert decision.event_id in executor._submitted
                assert executor._submitted_calendars[decision.event_id] == [calendar]
                assert (decision.event_id, 216) in calendar.volume_reservations["WASTE_MAIN"]
                raise SafetyViolation("Injected audit append failure")

    executor.audit = FailOnceAfterAppend(executor.audit)
    calendar_before, executor_before = _calendar_state(calendar), _executor_state(executor)
    audit_ref = executor.audit

    with pytest.raises(SafetyViolation, match="Injected audit append failure"):
        executor.execute(decision, context)

    assert executor.audit is audit_ref and executor.audit.failed
    assert _calendar_state(calendar) == calendar_before
    assert _executor_state(executor) == executor_before
    _assert_healthy_retry(executor, calendar, decision, context)
