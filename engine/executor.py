"""Simulation audit only. Commands are immutable, scoped proposals, not real effects."""
from copy import deepcopy
from datetime import timedelta
import json

from engine.core import instant, utc
from common.contracts.schemas import DecisionRecordV2
from engine.safety import SafetyViolation, validate_safety


class MockExecutor:
    def __init__(self):
        self.audit: list[dict] = []
        self._completed: dict[str, dict] = {}
        self._submitted: dict[str, tuple[str, str]] = {}
        self._submitted_calendars: dict[str, list[object]] = {}

    @staticmethod
    def _parameters(decision: DecisionRecordV2, context: dict) -> dict:
        parameters = {k: deepcopy(context[k]) for k in (
            "start_utc", "end_utc", "setpoint_c", "task", "battery_power_kw",
            "duration_minutes", "expected_soc_kwh", "transfer_liters", "service",
            "lead_hours", "weak_water_metrics", "container_max_fill_pct", "water_shutoff", "safety_critical", "cancelled_decision_id",
            "notable_cost", "common_area_route", "oncall", "waiting_guest",
        ) if k in context}
        parameters["room_id"] = context.get("room", {}).get("room_id")
        rooms = {r["id"] for r in context.get("building", {}).get("building", {}).get("rooms", [])}
        room_action = bool(rooms & set(decision.affected_assets))
        parameters["entry_required"] = bool(
            room_action and decision.action in {"schedule_cleaning", "request_inspection", "dispatch_robot", "dispatch_technician"}
            or context.get("entry_required") is True)
        for key in ("source_bin", "destination_bin"):
            if key in context:
                parameters[key + "_id"] = context[key].get("id")
        return parameters

    def _reject_withdrawn(self, decision: DecisionRecordV2, context: dict) -> None:
        """A known withdrawal also invalidates an otherwise idempotent retry."""
        try:
            calendars = [*self._submitted_calendars.get(decision.event_id, []),
                         context.get("calendar")]
            checked = []
            for calendar in calendars:
                if calendar is None or any(calendar is prior for prior in checked):
                    continue
                checked.append(calendar)
                cancellations = calendar.cancellations
                if not isinstance(cancellations, list):
                    raise TypeError("Calendar cancellations must be a list")
                for entry in cancellations:
                    if (not isinstance(entry, dict)
                            or not isinstance(entry.get("decision_id"), str)
                            or not entry["decision_id"]):
                        raise ValueError("Invalid calendar cancellation record")
                    if entry["decision_id"] == decision.event_id:
                        raise SafetyViolation("Cancelled decision cannot be resubmitted; replan with a fresh ID")
        except SafetyViolation:
            raise
        except (KeyError, TypeError, ValueError, OverflowError, AttributeError) as exc:
            raise SafetyViolation(f"Invalid safety context: {exc}") from exc

    def execute(self, decision: DecisionRecordV2, context: dict) -> dict:
        try:
            parameters = self._parameters(decision, context)
            encoded = (decision.model_dump_json(warnings=False), json.dumps(parameters, sort_keys=True, allow_nan=False))
        except (TypeError, ValueError, AttributeError, KeyError) as exc:
            raise SafetyViolation(f"Invalid execution parameters: {exc}") from exc
        self._reject_withdrawn(decision, context)
        if decision.event_id in self._submitted and self._submitted[decision.event_id] != encoded:
            raise SafetyViolation("Conflicting decision or command parameters for reused ID")
        if decision.event_id in self._completed:
            return deepcopy(self._completed[decision.event_id])
        # Safety checks can prune expired holds, and an accepted resource hold can
        # precede a failed volume hold. Roll back only planning state, never the
        # calendar object or its observed work/cancellation history.
        reservation_state = []
        try:
            calendar = context.get("calendar")
            if calendar is not None:
                for name in ("reservations", "room_reservations", "tentative_reservations",
                             "tentative_room_reservations", "volume_reservations"):
                    collection = getattr(calendar, name)
                    if not isinstance(collection, dict) or any(
                            not isinstance(rows, list) for rows in collection.values()):
                        raise TypeError("Calendar capacity ledgers must contain lists")
                    reservation_state.append((collection, {
                        key: (rows, list(rows)) for key, rows in collection.items()
                    }))
        except (KeyError, TypeError, ValueError, OverflowError, AttributeError) as exc:
            raise SafetyViolation(f"Invalid safety context: {exc}") from exc
        audit_count = len(self.audit)
        previous_submitted = self._submitted.get(decision.event_id)
        previous_calendars = self._submitted_calendars.get(decision.event_id)
        calendar_count = len(previous_calendars) if previous_calendars is not None else 0
        try:
            return self._execute(decision, context, parameters, encoded)
        except BaseException as exc:
            for collection, snapshot in reservation_state:
                collection.clear()
                for key, (rows, contents) in snapshot.items():
                    rows[:] = contents
                    collection[key] = rows
            del self.audit[audit_count:]
            self._completed.pop(decision.event_id, None)
            if previous_submitted is None:
                self._submitted.pop(decision.event_id, None)
            else:
                self._submitted[decision.event_id] = previous_submitted
            if previous_calendars is None:
                self._submitted_calendars.pop(decision.event_id, None)
            else:
                del previous_calendars[calendar_count:]
                self._submitted_calendars[decision.event_id] = previous_calendars
            if isinstance(exc, SafetyViolation):
                raise
            if isinstance(exc, (KeyError, TypeError, ValueError, OverflowError, AttributeError)):
                raise SafetyViolation(f"Invalid execution context: {exc}") from exc
            raise

    def _execute(self, decision: DecisionRecordV2, context: dict, parameters: dict,
                 encoded: tuple[str, str]) -> dict:
        validate_safety(decision, context)
        execution_utc = context.get("execution_utc", decision.timestamp_utc)
        if instant(execution_utc) != instant(decision.timestamp_utc):
            raise SafetyViolation("A deferred command must be replanned with fresh evidence at execution time")
        if decision.requires_human_approval and context.get("human_approval_granted") is not True:
            status, phase = "pending_human_approval", "approval_pending"
        elif decision.requires_guest_consent and context.get("guest_consent_granted") is not True:
            status, phase = "pending_guest_consent", "consent_pending"
        else:
            if decision.action == "dispatch_technician" and not decision.resource_id:
                raise SafetyViolation("Dispatch execution needs a verified resource assignment and shift")
            status = "simulated_message_proposed" if decision.action == "notify_guest" else "simulated"
            if decision.resource_id:
                context["calendar"].reserve(
                    decision.resource_id, context["start_utc"], context["end_utc"],
                    decision.event_id, decision.timestamp_utc,
                    room=parameters["room_id"] if parameters["entry_required"] else None,
                )
                phase = "scheduled" if instant(context["start_utc"]) > instant(decision.timestamp_utc) else "task_proposed"
            elif decision.action in {"request_inspection", "order_service", "dispatch_technician"}:
                phase = "request_queued"
            elif decision.action == "notify_guest":
                phase = "message_proposed"
            else:
                phase = "control_proposed"
            if decision.action == "optimize_energy":
                end = utc(instant(decision.timestamp_utc) + timedelta(minutes=context["duration_minutes"]))
                context["calendar"].reserve("asset:BAT1", decision.timestamp_utc, end, decision.event_id, decision.timestamp_utc)
            if decision.action == "dispatch_robot" and context.get("task") == "carry_bin":
                context["calendar"].reserve_volume(context["destination_bin"]["id"], context["transfer_liters"], decision.event_id)
        if status.startswith("pending_") and decision.resource_id:
            context["calendar"].reserve_tentative(
                decision.resource_id, context["start_utc"], context["end_utc"],
                decision.event_id, decision.timestamp_utc,
                room=parameters["room_id"] if parameters["entry_required"] else None,
            )
        record = {
            "decision_id": decision.event_id, "timestamp_utc": decision.timestamp_utc,
            "status": status, "phase": phase, "action": decision.action, "simulated": True,
            "parameters": parameters, "effects_applied": False, "observed_history_changed": False,
        }
        self._submitted[decision.event_id] = encoded
        calendar = context.get("calendar")
        if calendar is not None:
            submitted_calendars = self._submitted_calendars.setdefault(decision.event_id, [])
            if not any(calendar is prior for prior in submitted_calendars):
                submitted_calendars.append(calendar)
        self.audit.append(record)
        # Approval requirements are not grants. Revisiting a pending proposal must
        # retain exactly the command that was proposed and pass current checks again.
        if not status.startswith("pending_"):
            self._completed[decision.event_id] = deepcopy(record)
        return deepcopy(record)
