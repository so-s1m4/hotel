"""Cerebras intake through the real DecisionEngine, with durable explanations.

The property is an explicit fixture; engine execution remains simulated.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
import re
from threading import Event, RLock
from uuid import uuid4

from common.contracts.schemas import (EventEntitiesV1, ExtractionMetaV1, GuestIntentV1,
                                      NormalizedEventV1, SourceRefV1)
from engine.planner import DecisionEngine
from engine.safety import SafetyViolation, validate_safety
from hotel_bot.demo import demo_property
from hotel_bot.gateway import DemoGateway
from hotel_bot.journal import JournalCorruptionError, SessionJournal
from ingestion.guest_parser import parse_request_full


MAX_MESSAGES = 200


class ValidationError(ValueError):
    status_code = 400


class ConflictError(ValueError):
    status_code = 409


class NotFoundError(ValueError):
    status_code = 404


class ServiceUnavailableError(ValueError):
    status_code = 503


ACTION_TITLES = {
    "create_service_task": "Service task queued", "request_inspection": "Inspection requested",
    "schedule_cleaning": "Cleaning scheduled", "notify_guest": "Guest update prepared",
    "defer_action": "Clarification needed", "request_human_approval": "Approval required",
}


def _stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="auto").replace("+00:00", "Z")


class HotelService:
    """One process-local demo session; a lock serializes model calls and approvals."""

    def __init__(self, parser=None, clock=None, journal=None):
        self._lock = RLock()
        self._stopping = Event()
        self._parser = parser or parse_request_full
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._fixture = demo_property(self._now())
        self._rooms = {room["id"]: room for room in self._fixture["rooms"]}
        self._messages: dict[str, dict] = {}
        self._fingerprints: dict[str, str] = {}
        self._proposals: dict[str, dict] = {}
        self._activity: list[dict] = []
        self._gateway = DemoGateway(self._fixture, self._now())
        self._engine = DecisionEngine(self._gateway, self._fixture["building"], enable_guest_services=True)
        self._executor = self._engine.executor
        self._decisions: dict[str, dict] = {}
        self._journal = journal or SessionJournal(clock=self._clock)
        self._pending_journal: list[dict] = []
        self._journal_cursor = self._journal.total
        self._journal_confirmed_operations: set[str] = set()
        self._version = 0
        self._model = {"provider": "Cerebras", "name": os.environ.get("CEREBRAS_MODEL") or os.environ.get("LLM_MODEL") or "gpt-oss-120b",
                       "status": "not_checked", "last_error": None}

    def stop_accepting(self):
        """Reject queued actions without waiting for the active parser's lock."""
        self._stopping.set()

    def _require_accepting(self):
        if self._stopping.is_set():
            raise ServiceUnavailableError("The Hotel server is shutting down.")

    def _now(self):
        now = self._clock()
        if not isinstance(now, datetime) or now.tzinfo is None:
            raise ValidationError("The service clock must have a timezone.")
        return now.astimezone(timezone.utc)

    def _log(self, title, detail, kind, now):
        self._activity.append({"id": f"activity-{len(self._activity)+1}", "time": _stamp(now),
                               "title": title, "detail": detail, "kind": kind})

    def journal(self, after=0, limit=100):
        with self._lock:
            self._flush_journal()
            return self._journal.read(after=after, limit=limit)

    def _queue_journal(self, kind, title, reason, **fields):
        """Retain immutable operations until durable publication is acknowledged."""
        operation = {"kind": kind, "title": title, "reason": reason, **deepcopy(fields)}
        operation["details"] = {**operation.get("details", {}), "journal_operation_id": uuid4().hex}
        self._pending_journal.append(operation)

    def _refresh_journal_operations(self):
        while True:
            page = self._journal.read(after=self._journal_cursor, limit=500)
            if page["total"] < self._journal_cursor:
                raise JournalCorruptionError("The action journal was truncated after acknowledged entries.")
            for entry in page["entries"]:
                operation = entry["details"].get("journal_operation_id")
                if isinstance(operation, str):
                    self._journal_confirmed_operations.add(operation)
            self._journal_cursor = page["next_cursor"]
            if self._journal_cursor >= page["total"]:
                return

    def _flush_journal(self):
        # An append can reach disk and still raise (for example during fsync).
        # Reconcile its operation ID before retrying, never publish it twice.
        self._refresh_journal_operations()
        while self._pending_journal:
            operation = self._pending_journal[0]
            identity = operation["details"]["journal_operation_id"]
            if identity in self._journal_confirmed_operations:
                self._journal.sync()
            else:
                entry = self._journal.append(**operation)
                self._journal_confirmed_operations.add(identity)
                self._journal_cursor = entry["id"]
            self._pending_journal.pop(0)

    def snapshot(self) -> dict:
        with self._lock:
            self._flush_journal()
            rooms = deepcopy(list(self._rooms.values()))
            active = {"pending_approval", "needs_review", "pending_guest_consent"}
            for room in rooms:
                room["active_request_count"] = sum(p["room_id"] == room["id"] and p["status"] in active
                                                   for p in self._proposals.values())
            counts = {status: sum(r["status"] == status for r in rooms)
                      for status in ("occupied", "ready", "attention", "cleaning")}
            pending = sum(p["status"] in active for p in self._proposals.values())
            return deepcopy({
                "property": self._fixture["property"], "model": self._model, "rooms": rooms,
                "people": self._fixture["people"], "messages": list(self._messages.values()),
                "proposals": list(self._proposals.values()), "decisions": list(self._decisions.values()),
                "activity": self._activity[-100:], "journal": {"session_id": self._journal.session_id,
                    "total": self._journal.total}, "source": "synthetic_demo",
                "stats": {"rooms": len(rooms), **counts, "occupancy": counts["occupied"],
                          "occupancy_pct": round(100 * counts["occupied"] / len(rooms)),
                          "pending": pending, "requests": len(self._messages),
                          "autonomous": sum(not d["decision_record"]["requires_human_approval"] for d in self._decisions.values()),
                          "approved": sum(p["status"] == "approved_simulated" for p in self._proposals.values()),
                          "rejected": sum(p["status"] == "rejected" for p in self._proposals.values())},
                "version": self._version,
            })

    def _input(self, payload):
        if not isinstance(payload, dict) or set(payload) - {"text", "language", "room_id", "request_id"}:
            raise ValidationError("Use only text, language, room_id and request_id.")
        text, room = payload.get("text"), payload.get("room_id")
        language = payload.get("language", "en")
        ident = payload["request_id"] if "request_id" in payload else str(uuid4())
        if not isinstance(text, str) or not text.strip() or len(text) > 4000:
            raise ValidationError("Enter a message between 1 and 4,000 characters.")
        if not isinstance(room, str) or room not in self._rooms:
            raise ValidationError("Choose one of the 40 demo rooms.")
        if not isinstance(language, str) or language not in {"en", "de", "it", "fr", "es", "nl"}:
            raise ValidationError("Choose English, German, Italian, French, Spanish or Dutch.")
        if not isinstance(ident, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,100}", ident):
            raise ValidationError("request_id must contain 1–100 letters, digits, dots, colons, underscores or hyphens.")
        return {"request_id": ident, "text": text, "language": language, "room_id": room}

    def _receipt(self, ident, replayed=False):
        return {"request_id": ident, "replayed": replayed,
                "request": deepcopy(self._messages[ident]), "snapshot": self.snapshot()}

    def submit_message(self, payload) -> dict:
        with self._lock:
            self._require_accepting()
            # Pending output must become durable before another API call or an
            # idempotent success reply can be issued.
            self._flush_journal()
            values = self._input(payload)
            ident = values["request_id"]
            fingerprint = sha256(json.dumps(values, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            if ident in self._messages:
                if self._fingerprints[ident] != fingerprint:
                    raise ConflictError("This request ID already belongs to a different message.")
                return self._receipt(ident, replayed=True)
            if len(self._messages) >= MAX_MESSAGES:
                raise ConflictError("This demo session is full. Restart the local server to begin a new session.")
            now = self._now()
            room = self._rooms[values["room_id"]]
            self._gateway.set_now(now)
            booking = self._gateway.get_booking("DEMO_BOOKING_" + room["id"], _stamp(now))
            record = {**values, "booking_id": booking["booking_id"], "timestamp_utc": _stamp(now)}
            context = {"use_llm": True, "as_of_utc": _stamp(now), "booking": booking,
                       "room_ids": list(self._rooms), "source_label_policy": "optional"}
            self._queue_journal("message", "Guest message received", "The guest submitted this message for interpretation and an engine decision.",
                                 timestamp=now, room_id=room["id"], request_id=ident,
                                 evidence=["demo_guest_messages.jsonl:" + ident], details={"text": values["text"], "language": values["language"]})
            try:
                items, metadata = self._parser(deepcopy(record), deepcopy(context))
                meta = ExtractionMetaV1.model_validate(metadata.model_dump() if hasattr(metadata, "model_dump") else metadata)
                intents = [GuestIntentV1.model_validate(item.model_dump() if hasattr(item, "model_dump") else item,
                                                       strict=True).model_dump(mode="json") for item in items]
                if not intents or len(intents) > 32:
                    raise ValueError("Invalid intent count")
                # Protect the service seam too: injected/broken parsers cannot turn
                # ungrounded evidence into apparently verified operator requests.
                for item in intents:
                    if not item["evidence_quote"] or item["evidence_quote"] not in values["text"]:
                        item["needs_review"] = True
                        item["evidence_quote"] = None
                        item["requested_time_utc"] = None
                        item["requested_time_text"] = None
                        meta.needs_review = True
                        meta.uncertain_fields = sorted(set(meta.uncertain_fields) | {"evidence_quote"})
            except Exception:
                # Never serialize exception text, which may contain headers/data.
                intents = [GuestIntentV1(intent="unknown", needs_review=True).model_dump(mode="json")]
                meta = ExtractionMetaV1(method="fallback", model_name=self._model["name"], needs_review=True,
                                        uncertain_fields=["model_or_validation_error"])
            is_live = meta.method == "llm"
            # The parser may return a validated cached model result. This status
            # must not claim that a fresh HTTP connection was observed.
            self._model["status"] = "result_available" if is_live else "degraded"
            self._model["last_error"] = None if is_live else "The model could not provide a verified result. This message needs manual review."
            if is_live and meta.model_name:
                self._model["name"] = meta.model_name
            needs_review = meta.needs_review or any(item["needs_review"] for item in intents) or not is_live
            event = NormalizedEventV1(
                event_id="demo-guest:" + ident, timestamp_utc=_stamp(now), available_at_utc=_stamp(now),
                event_type="guest_request", source=SourceRefV1(file="demo_guest_messages.jsonl", record_id=ident,
                                                               timestamp_utc=_stamp(now)),
                entities=EventEntitiesV1(room_id=room["id"], booking_id=booking["booking_id"], guest_request_id=ident),
                payload={"request_type": None, "original_text": values["text"], "language": values["language"],
                         "intents": intents, "channel": "presentation_demo", "synthetic_property": True}, extraction=meta)
            message = {"id": ident, "room_id": room["id"], "text": values["text"], "language": values["language"],
                       "created_at": _stamp(now), "intents": intents, "needs_review": needs_review,
                       "status": "degraded" if not is_live else "needs_review" if needs_review else "classified",
                       "extraction": meta.model_dump(mode="json"), "proposal_ids": [],
                       "normalized_event": event.model_dump(mode="json")}
            self._queue_journal("extraction", "Message interpreted", "The model result passed the parser checks." if not needs_review else
                                 "The extraction is uncertain; the engine must defer unsupported actions.", timestamp=now,
                                 room_id=room["id"], request_id=ident, status=message["status"],
                                 details={"intents": intents, "extraction": meta.model_dump(mode="json")})
            rejection_start = len(self._engine.rejections)
            records = self._engine.process(event)
            self._messages[ident], self._fingerprints[ident] = message, fingerprint
            message["decision_ids"] = []
            message["triggered_decision_ids"] = []
            owned_views = []
            for decision in records:
                view = self._decision_view(decision, ident, room["id"])
                self._decisions[view["id"]] = view
                message["triggered_decision_ids"].append(view["id"])
                if ident in view["request_ids"]:
                    message["decision_ids"].append(view["id"])
                    owned_views.append(view)
                if decision.requires_human_approval:
                    self._proposals[view["id"]] = view
                    if ident in view["request_ids"]:
                        message["proposal_ids"].append(view["id"])
                self._journal_decision(view, now)
            for rejection in self._engine.rejections[rejection_start:]:
                self._queue_journal("safety", "Action blocked", rejection["reason"], timestamp=now,
                                     room_id=room["id"], request_id=ident, status="blocked", details=rejection)
            if not records:
                self._queue_journal("decision", "No new action", "No new safe action was available, or an existing engine decision already covers this event.",
                                     timestamp=now, room_id=room["id"], request_id=ident, status="no_action")
            if not needs_review:
                message["status"] = "approval_required" if message["proposal_ids"] else (
                    "clarification_needed" if any(view["decision_record"]["action"] == "defer_action" for view in owned_views) else
                    "handled_autonomously" if owned_views else "no_action")
            self._version += 1
            automatic = sum(not view["decision_record"]["requires_human_approval"] for view in owned_views)
            self._log("Engine decision", f"{room['id']}: {automatic} autonomous decision(s), {len(message['proposal_ids'])} approval exception(s).",
                      "warning" if needs_review else "request", now)
            self._flush_journal()
            return self._receipt(ident)

    def _decision_view(self, decision, request_id, room_id):
        candidate = self._engine._proposals.get(decision.event_id)
        audit = next(row for row in reversed(self._executor.audit) if row["decision_id"] == decision.event_id)
        context = candidate.context if candidate else {}
        room_id = next((asset for asset in decision.affected_assets if asset in self._rooms), None) or (decision.affected_assets[0] if decision.affected_assets else room_id)
        owner = self._decisions.get(audit.get("parameters", {}).get("cancelled_decision_id"))
        if owner is None:
            owner = next((self._decisions[parent] for parent in decision.dependencies if parent in self._decisions), None)
        inherited_ids = (owner.get("request_ids") or [owner["request_id"]]) if owner else [request_id]
        request_ids = list(dict.fromkeys(decision.guest_request_ids or inherited_ids))
        request_id = request_ids[0]
        intent = context.get("guest_intent") or ("early_checkin" if decision.issue_type == "room_turnaround" else decision.issue_type)
        human = decision.requires_human_approval
        clarification = decision.action == "defer_action"
        return {"id": decision.event_id, "request_id": request_id, "request_ids": request_ids,
                "room_id": room_id, "intent": intent,
                "title": ACTION_TITLES.get(decision.action, decision.action.replace("_", " ").capitalize()),
                "reason": decision.reason, "team": context.get("service_team", "Hotel operations"),
                "status": "pending_approval" if human else "clarification_needed" if clarification else "autonomous_simulated",
                "requires_consent": decision.requires_guest_consent, "consent_granted": False,
                "manager_approved": False, "entry_required": audit["parameters"]["entry_required"],
                "review_required": False, "review_reasons": [], "requested_time_utc": context.get("start_utc"),
                "created_at": decision.timestamp_utc, "decision_at": None, "simulated": True,
                "effects_applied": False, "execution_count": 1, "decision_record": decision.model_dump(mode="json"),
                "audit": deepcopy(audit), "original_decision_id": decision.event_id,
                "status_text": "Approval required before this simulated action." if human else
                    "The engine requested clarification; no manager approval is needed." if clarification else
                    "The engine handled this step autonomously in simulation.", "engine": "DecisionEngine"}

    def _journal_decision(self, view, now):
        record = view["decision_record"]
        self._queue_journal("decision", view["title"], view["reason"], timestamp=now, room_id=view["room_id"],
                             request_id=view["request_id"], proposal_id=view["id"], action=record["action"],
                             status=view["status"], evidence=record["evidence"],
                             details={"decision": record, "audit": view["audit"], "engine": "DecisionEngine"})

    def _approval_conflict(self, proposal, reason):
        self._queue_journal("approval_blocked", "Approval blocked", reason, timestamp=self._now(),
                             room_id=proposal["room_id"], request_id=proposal["request_id"],
                             proposal_id=proposal["id"], action=proposal["decision_record"]["action"], status="blocked")
        self._flush_journal()
        raise ConflictError(reason)

    def _approve_inspection(self, proposal, now, consent):
        """Replan a queue-only inspection using current engine evidence and safety.

        Resource-bearing commands cannot use this path: they need a fresh full
        planning event. A human click cannot bypass capacity or dispatch rules.
        """
        original = self._engine._proposals[proposal["original_decision_id"]]
        if original.action != "request_inspection" or original.resource is not None:
            self._approval_conflict(proposal, "This action needs a fresh engine plan before approval.")
        self._gateway.set_now(now)
        event = NormalizedEventV1.model_validate(self._messages[proposal["request_id"]]["normalized_event"])
        event = event.model_copy(update={"available_at_utc": _stamp(now)})
        candidates = self._engine.guest_services.detect(event, self._gateway, self._fixture["building"]).candidates
        candidate = next((c for c in candidates if c.action == original.action and c.asset == original.asset
                          and c.context.get("guest_intent") == original.context.get("guest_intent")), None)
        if candidate is None:
            self._approval_conflict(proposal, "Current room evidence no longer supports this inspection. Submit a fresh request.")
        candidate.human = original.human or candidate.human
        candidate.consent = original.consent or candidate.consent
        candidate.variant = "approval-" + str(proposal["execution_count"])
        decision = candidate.record(_stamp(now), proposal["decision_record"]["episode_id"])
        context = {**candidate.context, "building": self._fixture["building"], "calendar": self._engine.calendar,
                   "execution_utc": _stamp(now), "max_age_minutes": self._engine.config.telemetry_max_age_minutes,
                   "human_approval_granted": True, "guest_consent_granted": consent}
        try:
            validate_safety(decision, context)
            audit = self._executor.execute(decision, context)
        except SafetyViolation:
            self._approval_conflict(proposal, "The current safety check rejected this approval; the action needs a fresh plan.")
        proposal["execution_count"] += 1
        proposal["decision_record"] = decision.model_dump(mode="json")
        proposal["audit"] = audit

    def approve(self, proposal_id, decision, consent=False) -> dict:
        with self._lock:
            self._require_accepting()
            self._flush_journal()
            if not isinstance(proposal_id, str) or proposal_id not in self._proposals:
                raise NotFoundError("This approval request does not exist.")
            if not isinstance(decision, str) or decision not in {"approve", "reject"} or type(consent) is not bool:
                raise ValidationError("Use approve or reject, with a boolean consent value.")
            proposal = self._proposals[proposal_id]
            if proposal["status"] in {"approved_simulated", "rejected"}:
                expected = "approve" if proposal["status"] == "approved_simulated" else "reject"
                if decision != expected:
                    self._approval_conflict(proposal, "This request already has a final decision. Submit a new request to change the plan.")
                return {"proposal_id": proposal_id, "replayed": True, "proposal": deepcopy(proposal), "snapshot": self.snapshot()}
            if decision == "approve" and proposal["review_required"]:
                self._approval_conflict(proposal, "Clarify the guest's message and submit a new request before approval.")
            if proposal["status"] == "pending_guest_consent" and decision == "approve" and not consent:
                return {"proposal_id": proposal_id, "replayed": True, "proposal": deepcopy(proposal), "snapshot": self.snapshot()}
            now = self._now()
            if decision == "reject":
                proposal["status"] = "rejected"
                proposal["manager_approved"] = False
                proposal["consent_granted"] = False
                proposal["status_text"] = "Rejected by the manager. No task was dispatched."
            else:
                self._approve_inspection(proposal, now, consent=consent)
                proposal["manager_approved"] = True
                proposal["consent_granted"] = consent if proposal["requires_consent"] else False
                proposal["status"] = "pending_guest_consent" if proposal["requires_consent"] and not consent else "approved_simulated"
                proposal["status_text"] = (
                    "Manager approved. Waiting for explicit guest consent before any room-entry request."
                    if proposal["status"] == "pending_guest_consent" else
                    "Approved in this demo. Staff ticket prepared; no person was dispatched and no physical action occurred.")
            proposal["decision_at"] = _stamp(now)
            self._queue_journal("approval", "Manager approved" if decision == "approve" else "Manager declined",
                                 proposal["status_text"], timestamp=now, room_id=proposal["room_id"],
                                 request_id=proposal["request_id"], proposal_id=proposal_id,
                                 action=proposal["decision_record"]["action"], status=proposal["status"],
                                 evidence=proposal["decision_record"]["evidence"], details={"original_decision_id": proposal["original_decision_id"],
                                     "manager_approved": proposal["manager_approved"], "guest_consent_granted": proposal["consent_granted"],
                                     "decision": proposal["decision_record"], "audit": proposal["audit"]})
            self._version += 1
            self._log("Manager decision", f"{proposal['room_id']}: {proposal['status_text']}",
                      "approval" if decision == "approve" else "rejection", now)
            self._flush_journal()
            return {"proposal_id": proposal_id, "replayed": False, "proposal": deepcopy(proposal), "snapshot": self.snapshot()}
