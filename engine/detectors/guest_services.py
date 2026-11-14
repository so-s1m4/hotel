"""Optional, grounded guest-service routing through the normal engine pipeline.

Queue-only tasks never grant access, dispatch a worker, change billing, or control
equipment. Room inspection requests retain the shared occupancy/consent checks.
"""
from datetime import timedelta

from common.contracts.schemas import GuestIntentV1
from engine.core import Candidate, DetectionResult, instant, room_state
from engine.safety import SERVICE_TASK_TEAMS


TASK_REASONS = {
    "wifi_help": "Queue verified Wi-Fi assistance with the front desk; never invent or disclose a password.",
    "extra_towels": "Queue a towel handoff with housekeeping; this does not authorize entering the room.",
    "access_issue": "Queue identity and booking verification with the front desk; no door is unlocked or key issued.",
    "invoice": "Queue a billing-information review; no invoice, charge or refund is issued automatically.",
    "parking": "Queue parking-information assistance; availability and any fee still require verification.",
    "luggage_storage": "Queue a luggage-storage handoff; no availability or completed transfer is assumed.",
    "arrival_delay": "Queue the guest's arrival update for the front desk; no booking is changed automatically.",
    "late_checkout": "Queue a late-checkout review against bookings and housekeeping capacity; no extension is granted.",
    "noise_complaint": "Queue a noise follow-up without assuming the source of the disturbance.",
    "cleaning_request": "Queue housekeeping coordination; no cleaner is reserved, dispatched or permitted to enter.",
}
INSPECTION_INTENTS = {"maintenance_issue", "water_issue", "air_quality", "temperature_hot", "temperature_cold"}


def verified_guest_intents(event):
    """Return validated intentions only when the complete extraction is usable."""
    if event.event_type != "guest_request":
        return []
    meta = event.extraction
    text = event.payload.get("original_text")
    raw = event.payload.get("intents")
    if (not meta or meta.method != "llm" or meta.needs_review or meta.uncertain_fields
            or not isinstance(text, str) or not text or not isinstance(raw, list) or not raw):
        return []
    try:
        items = [GuestIntentV1.model_validate(item, strict=True).model_dump(mode="json") for item in raw]
    except (TypeError, ValueError):
        return []
    if any(item["intent"] == "unknown" or item["needs_review"] or not item["evidence_quote"]
           or item["evidence_quote"] not in text
           or (item["requested_time_text"] is not None and
               (not item["requested_time_text"] or item["requested_time_text"] not in text)) for item in items):
        return []
    source = event.payload.get("request_type")
    if source is not None and source not in {item["intent"] for item in items}:
        return []
    return items


class GuestServicesDetector:
    def __init__(self, config):
        self.config = config

    def detect(self, event, repo, building):
        result = DetectionResult()
        if event.event_type != "guest_request":
            return result
        source_ref = f"{event.source.file}:{event.source.record_id}"
        room = event.entities.room_id
        known_rooms = {item["id"] for item in building["building"]["rooms"]}
        asset = room if room in known_rooms else "BUILDING"
        request_ids = [event.entities.guest_request_id] if event.entities.guest_request_id else []
        items = verified_guest_intents(event)
        if not items:
            fields = sorted(set(event.extraction.uncertain_fields)) if event.extraction else ["extraction"]
            detail = ", ".join(fields) if fields else "unverified or ambiguous extraction"
            result.candidates.append(Candidate(
                key="guest-service:" + event.event_id, asset=asset, issue="guest_request_clarification",
                action="defer_action", reason="The message cannot yet support an operational request (" + detail +
                    "). Ask the guest to clarify; no manager authorization or physical action is inferred.",
                evidence=[source_ref], verification="Obtain a new, clear guest message and validate its grounded extraction.",
                outcome="A clarification is recorded; no task is dispatched and no authority is granted.",
                request_ids=request_ids, context={"guest_intent": "unknown", "service_team": "Front desk",
                                                 "clarification_required": True}, variant="clarify"))
            return result
        seen = set()
        for item in items:
            intent = item["intent"]
            if intent in seen:
                continue
            seen.add(intent)
            if intent in {"early_checkin", "waiting_for_room"}:
                # The actual turnaround detector handles readiness, staff and
                # resource reservations. Do not manufacture a source label.
                continue
            key = "guest-service:" + event.event_id + ":" + intent
            quoted = f'The guest wrote: “{item["evidence_quote"]}”. '
            context = {"guest_intent": intent, "service_team": SERVICE_TASK_TEAMS.get(intent, "Maintenance")}
            if intent in SERVICE_TASK_TEAMS:
                result.candidates.append(Candidate(
                    key=key, asset=asset, issue="guest_service_" + intent, action="create_service_task",
                    reason=quoted + TASK_REASONS[intent], evidence=[source_ref],
                    verification="Staff must verify the request and any identity, availability or access requirements before carrying it out.",
                    outcome="A service task is queued; no room entry, physical dispatch, financial change or delivery is claimed.",
                    request_ids=request_ids, context={**context, "service_task": intent, "entry_required": False}))
                continue
            if intent in INSPECTION_INTENTS:
                state = room_state(repo, room, event.available_at_utc) if room in known_rooms else {}
                stamp = state.get("last_observed_utc")
                fresh = bool(stamp and state.get("room_id") == room and
                             instant(event.available_at_utc) - timedelta(minutes=self.config.telemetry_max_age_minutes)
                             <= instant(stamp) <= instant(event.available_at_utc))
                if not fresh:
                    result.candidates.append(Candidate(
                        key=key, asset=asset, issue="guest_service_" + intent, action="defer_action",
                        reason=quoted + "Fresh room identity and occupancy evidence are unavailable; verify the room before requesting an inspection.",
                        evidence=[source_ref, *state.get("evidence", [])],
                        verification="Obtain a current, causally available room observation before planning a visit.",
                        outcome="An evidence check is queued; no entry or inspection is authorized.",
                        request_ids=request_ids, context={**context, "clarification_required": True}, variant="missing_room_evidence"))
                    continue
                consent = state.get("occupancy") != "vacant"
                result.candidates.append(Candidate(
                    key=key, asset=room, issue="guest_service_" + intent, action="request_inspection",
                    reason=quoted + ("The room is occupied or occupancy is unknown, so manager approval and guest consent are required for entry. "
                                     if consent else "The room is freshly observed vacant, so an inspection request can be queued autonomously. ") +
                           "This requests an assessment only; no technician dispatch, repair, water shutoff or setpoint change is performed.",
                    evidence=[source_ref, *state.get("evidence", [])],
                    verification="Arrange an eligible inspector and verify the observation and any entry permissions before a real visit.",
                    outcome="An inspection request only; no room entry or completed assessment is claimed.",
                    human=consent, consent=consent, request_ids=request_ids,
                    context={**context, "room": state, "entry_required": True}))
        return result
