"""Simulation audit only. Never writes to a gateway or infers consent from a request."""
from copy import deepcopy

from common.contracts.schemas import DecisionRecordV2
from engine.safety import SafetyViolation, validate_safety


class MockExecutor:
    def __init__(self):
        self.audit: list[dict] = []
        self._completed: dict[str, dict] = {}
        self._submitted: dict[str, str] = {}

    def execute(self, decision: DecisionRecordV2, context: dict) -> dict:
        encoded = decision.model_dump_json()
        if decision.event_id in self._submitted and self._submitted[decision.event_id] != encoded:
            raise SafetyViolation("Conflicting decision content for reused ID")
        if decision.event_id in self._completed:
            return deepcopy(self._completed[decision.event_id])
        validate_safety(decision, context)
        if decision.requires_human_approval and context.get("human_approval_granted") is not True:
            status = "pending_human_approval"
        elif decision.requires_guest_consent and context.get("guest_consent_granted") is not True:
            status = "pending_guest_consent"
        else:
            status = "simulated_message_proposed" if decision.action == "notify_guest" else "simulated"
            if decision.resource_id:
                context["calendar"].reserve(decision.resource_id, context["start_utc"], context["end_utc"],
                                            decision.event_id, decision.timestamp_utc, room=context.get("room", {}).get("room_id")
                                            if context.get("entry_required") else None)
        record = {"decision_id": decision.event_id, "timestamp_utc": decision.timestamp_utc,
                  "status": status, "action": decision.action, "simulated": True,
                  "parameters": {k: context[k] for k in ("start_utc", "end_utc", "setpoint_c", "task") if k in context},
                  "observed_history_changed": False}
        self._submitted[decision.event_id] = encoded
        self.audit.append(record)
        # Pending proposals can be revisited with explicit grants; no re-validation bypass.
        if not status.startswith("pending_"):
            self._completed[decision.event_id] = deepcopy(record)
        return deepcopy(record)
