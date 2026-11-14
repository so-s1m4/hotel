from copy import deepcopy

import pytest

from common.contracts.schemas import ExtractionMetaV1, GuestIntentV1, NormalizedEventV1
from engine.core import Candidate
from engine.planner import DecisionEngine
from engine.safety import SafetyViolation, validate_safety
from tests.engine.fixtures import BUILDING, FakeGateway, slot, state


NOW = "2026-01-06T09:00:00Z"


def event(intent="wifi_help", *, review=False, method="llm", uncertain=(), text="Please help with the Wi-Fi.",
          quote=None, source=None, room="R101", rid="new-guest", requested_time=None):
    return NormalizedEventV1(
        event_id="guest:" + rid, timestamp_utc=NOW, available_at_utc=NOW, event_type="guest_request",
        source={"file": "demo_guest_messages.jsonl", "record_id": rid, "timestamp_utc": NOW},
        entities={"room_id": room, "guest_request_id": rid},
        payload={"request_type": source, "original_text": text,
                 "intents": [GuestIntentV1(intent=intent, needs_review=review,
                    evidence_quote=text if quote is None else quote, requested_time_utc=requested_time,
                    requested_time_text="11:00" if requested_time else None).model_dump()]},
        extraction=ExtractionMetaV1(method=method, model_name="test", needs_review=review,
                                    uncertain_fields=list(uncertain)))


def setup(occupancy="occupied", *, stamp=NOW, enable=True):
    repo = FakeGateway()
    repo.states["R101"] = [state(t=stamp, occupancy=occupancy)]
    return repo, DecisionEngine(repo, BUILDING, enable_guest_services=enable)


@pytest.mark.parametrize("intent", ["wifi_help", "extra_towels", "access_issue", "invoice", "parking",
                                    "luggage_storage", "arrival_delay", "late_checkout", "noise_complaint",
                                    "cleaning_request"])
def test_low_risk_tasks_are_autonomous_queue_only(intent):
    _, engine = setup()
    records = engine.process(event(intent))
    assert len(records) == 1
    decision = records[0]
    assert decision.action == "create_service_task"
    assert decision.autonomy == "autonomous"
    assert not decision.requires_human_approval and not decision.requires_guest_consent
    assert decision.resource_id is None
    audit = engine.executor.audit[0]
    assert audit["status"] == "simulated" and audit["phase"] == "task_queued"
    assert not audit["effects_applied"] and not audit["observed_history_changed"]
    assert audit["parameters"]["service_task"] == intent
    assert audit["parameters"]["guest_intent"] == intent
    assert audit["parameters"]["entry_required"] is False


@pytest.mark.parametrize("occupancy", ["occupied", "unknown"])
@pytest.mark.parametrize("intent", ["maintenance_issue", "water_issue", "air_quality"])
def test_occupied_or_unknown_inspection_requires_approval_and_separate_consent(occupancy, intent):
    _, engine = setup(occupancy)
    decisions = engine.process(event(intent))
    assert len(decisions) == 1
    decision = decisions[0]
    assert decision.action == "request_inspection"
    assert decision.requires_human_approval and decision.requires_guest_consent
    assert decision.autonomy == "approval_and_consent"
    assert decision.resource_id is None
    assert engine.executor.audit[0]["status"] == "pending_human_approval"
    assert engine.executor.audit[0]["parameters"]["entry_required"] is True


def test_vacant_inspection_is_autonomous_request_not_unassigned_technician_dispatch():
    _, engine = setup("vacant")
    records = engine.process(event("maintenance_issue"))
    assert records[0].action == "request_inspection"
    assert records[0].autonomy == "autonomous"
    assert not records[0].requires_guest_consent
    assert records[0].resource_id is None
    assert engine.executor.audit[0]["phase"] == "request_queued"
    assert not engine.executor.audit[0]["effects_applied"]


@pytest.mark.parametrize("changes", [
    {"intent": "unknown"}, {"review": True}, {"method": "fallback"}, {"quote": "invented quote"},
    {"uncertain": ["untrusted_instructions"]}, {"uncertain": ["semantic_negation"]},
    {"source": "water_issue"},
])
def test_uncertain_extraction_defers_without_manager_approval_spam(changes):
    _, engine = setup()
    decisions = engine.process(event(**changes))
    assert len(decisions) == 1
    assert decisions[0].action == "defer_action"
    assert decisions[0].autonomy == "autonomous"
    assert not decisions[0].requires_human_approval
    assert engine.executor.audit[0]["parameters"]["clarification_required"] is True


@pytest.mark.parametrize("stamp", ["2026-01-06T08:00:00Z", "2026-01-06T10:00:00Z"])
def test_inspection_needs_fresh_causal_room_evidence(stamp):
    _, engine = setup("vacant", stamp=stamp)
    records = engine.process(event("water_issue"))
    assert records[0].action == "defer_action"
    assert not records[0].requires_human_approval
    assert "Fresh room" in records[0].reason


def test_unknown_room_does_not_become_an_actionable_asset():
    _, engine = setup()
    assert engine.process(event("water_issue", room="R999")) == []
    assert engine.rejections
    assert not engine.executor.audit


def test_duplicate_source_id_has_one_task_and_audit():
    _, engine = setup()
    request = event()
    first = engine.process(request)
    assert len(first) == 1
    assert engine.process(request) == []
    assert len(engine.executor.audit) == 1
    different = request.model_copy(deep=True)
    different.payload["original_text"] += " Changed."
    with pytest.raises(ValueError, match="Conflicting"):
        engine.process(different)


def test_duplicate_same_intent_does_not_duplicate_service_task():
    _, engine = setup()
    request = event("extra_towels")
    request.payload["intents"].append(deepcopy(request.payload["intents"][0]))
    assert len(engine.process(request)) == 1
    assert len(engine.executor.audit) == 1


def test_multi_intent_request_keeps_autonomous_and_approval_actions_separate():
    _, engine = setup("occupied")
    request = event("extra_towels", text="Please bring towels and check the television.")
    request.payload["intents"].append(GuestIntentV1(intent="maintenance_issue",
                                                  evidence_quote="check the television").model_dump())
    decisions = engine.process(request)
    assert {r.action for r in decisions} == {"create_service_task", "request_inspection"}
    assert sum(r.requires_human_approval for r in decisions) == 1
    assert len(engine.executor.audit) == 2


def test_source_less_early_request_uses_actual_turnaround_resource_calendar():
    repo, engine = setup("vacant")
    repo.slots = [slot()]
    request = event("early_checkin", text="Could I check in today at 11:00?", requested_time="2026-01-06T10:00:00Z")
    decisions = engine.process(request)
    assert any(row.action == "schedule_cleaning" for row in decisions)
    assert any(row.resource_id == "CLEANER_1" for row in decisions)
    assert request.payload["request_type"] is None
    assert not any(row.action == "create_service_task" for row in decisions)


def test_source_less_waiting_request_uses_turnaround_without_inventing_source_label():
    repo, engine = setup("vacant")
    repo.slots = [slot()]
    request = event("waiting_for_room", text="I am in the lobby waiting for my room.")
    decisions = engine.process(request)
    assert any(row.action == "schedule_cleaning" for row in decisions)
    assert request.payload["request_type"] is None


def test_new_guest_service_path_is_disabled_by_default():
    repo, _ = setup()
    engine = DecisionEngine(repo, BUILDING)
    assert engine.process(event()) == []
    assert engine.process(event("waiting_for_room", rid="another")) == []


def test_queued_task_safety_rejects_entry_resource_and_control_smuggling():
    decision = Candidate("key", "R101", "guest_service_wifi_help", "create_service_task", "Guest asked for help",
                         ["demo_guest_messages.jsonl:r1"], "Verify the request", "Queue only").record(NOW, "episode")
    context = {"building": BUILDING, "service_task": "wifi_help", "guest_intent": "wifi_help",
               "service_team": "Front desk", "entry_required": False}
    validate_safety(decision, context)
    for extra in ({"entry_required": True}, {"service_task": "unlock_door"}, {"service_team": "Housekeeping"},
                  {"setpoint_c": 21}, {"water_shutoff": True}, {"notable_cost": True}):
        with pytest.raises(SafetyViolation):
            validate_safety(decision, {**context, **extra})
    with pytest.raises(SafetyViolation):
        validate_safety(decision.model_copy(update={"resource_id": "TECH_1"}), context)
