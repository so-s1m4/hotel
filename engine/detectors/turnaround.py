"""S01: retain requests and reconsider when observations/staff availability change."""
from datetime import timedelta
from copy import deepcopy

from engine.core import Candidate, DetectionResult, EngineConfig, instant, utc, room_state, ago
from engine.resources import ResourceCalendar, oncall_slot, scheduled


class TurnaroundDetector:
    def __init__(self, config: EngineConfig):
        self.config = config
        self.pending: dict[str, object] = {}
        self.scheduled_jobs: dict[str, dict] = {}
        self.revisions: dict[str, int] = {}

    def detect(self, event, repo, calendar: ResourceCalendar, building: dict) -> DetectionResult:
        result = DetectionResult()
        now = event.available_at_utc
        raw_type = event.payload.get("type")
        labels = {i.get("intent") for i in event.payload.get("intents", [])}
        wanted = event.payload.get("request_type") in {"early_checkin", "waiting_for_room"}
        if event.event_type == "guest_request" and (wanted or labels & {"early_checkin", "waiting_for_room"}):
            # Do not trust an LLM disagreement over the original source label.
            if wanted:
                self.pending.setdefault(event.event_id, event)
        if raw_type == "arrival_waiting_room":
            self.pending.setdefault(event.event_id, event)
        if event.event_type not in {"guest_request", "clock_tick", "operational_event", "roster_update"}:
            return result
        planning = deepcopy(calendar)
        planned_rooms = set()
        for request_id, request in list(self.pending.items()):
            key = "turnaround:" + request_id
            room = request.entities.room_id
            if not room or room == "BUILDING":
                continue
            request_time = instant(request.available_at_utc)
            if instant(now) - request_time > timedelta(hours=48):
                self.pending.pop(request_id)
                result.resolved.add(key)
                continue
            booking_id = request.entities.booking_id
            booking = repo.get_booking(booking_id, now) if booking_id else None
            if booking and (not booking.get("created_utc") or instant(booking["created_utc"]) > instant(now)):
                booking = None
            state = room_state(repo, room, now)
            if booking_id and state.get("current_booking_id") == booking_id and state.get("occupancy") == "occupied":
                self.pending.pop(request_id)
                result.resolved.add(key)
                continue
            waiting = request.payload.get("type") == "arrival_waiting_room" or request.payload.get("request_type") == "waiting_for_room"
            desired = None
            for intent in request.payload.get("intents", []):
                quote = intent.get("evidence_quote")
                if (intent.get("intent") == "early_checkin" and not intent.get("needs_review")
                        and intent.get("requested_time_utc") and quote
                        and quote in request.payload.get("original_text", "")
                        and not (request.extraction and request.extraction.needs_review)):
                    desired = intent["requested_time_utc"]
                    break
            if waiting:
                desired = now
            evidence = [f"{request.source.file}:{request.source.record_id}", *state.get("evidence", [])]
            # A request alone is neither proof of a conflict nor readiness.
            if state.get("occupancy") == "vacant" and state.get("housekeeping_state") == "clean":
                result.resolved.add(key)
                for completed_key, completed in list(self.scheduled_jobs.items()):
                    if completed["room_id"] == room:
                        calendar.cancel(completed["decision_id"])
                        planning.cancel(completed["decision_id"])
                        self.scheduled_jobs.pop(completed_key)
                continue
            if not desired:
                if state.get("housekeeping_state") in {"dirty", "inspection_required", "in_progress"} or state.get("occupancy") == "occupied":
                    result.candidates.append(Candidate(key, room, "room_turnaround", "notify_guest",
                        "Requested arrival time is unverified; readiness cannot be promised. Ask for arrival time and verify room status.",
                        evidence, "Obtain a verified arrival time and observe vacancy, cleaning and required inspection.",
                        "Propose a clarification message; no delivery or readiness is claimed.",
                        request_ids=[request.entities.guest_request_id] if request.entities.guest_request_id else [], variant="clarify"))
                continue
            # Never use a stale overnight dirty state to schedule next-day work hours ahead.
            horizon = utc(instant(now) + timedelta(hours=12))
            if instant(desired) > instant(horizon):
                continue
            end_limit = utc(max(instant(desired), instant(now) + timedelta(hours=4)))
            occupancy = state.get("occupancy", "unknown")
            cleaning = state.get("housekeeping_state")
            request_ids = [request.entities.guest_request_id] if request.entities.guest_request_id else []
            common = dict(key=key, asset=room, issue="room_turnaround", evidence=evidence,
                          verification="Observe vacancy, completed human cleaning, no block and required inspection; otherwise escalate.",
                          outcome="Propose readiness coordination; no room release or message delivery is asserted.",
                          request_ids=request_ids)
            if occupancy != "vacant":
                # If an occupied room has a known late planned departure, explain the conflict;
                # the planned departure never authorizes entry or proves future vacancy.
                overlaps = repo.get_bookings_for_room(room, now, end_limit, now)
                for known in overlaps:
                    if known.get("created_utc") and instant(known["created_utc"]) <= instant(now):
                        evidence.append(f"bookings.csv:{known['booking_id']} (plan only)")
                result.candidates.append(Candidate(**common, action="notify_guest", priority="high" if waiting else "medium",
                    reason=f"Requested readiness by {desired}; occupancy is {occupancy}. A checkout and release checks are still required; no confirmed readiness time.",
                    variant="occupancy"))
                continue
            if cleaning not in {"dirty", "inspection_required"}:
                if cleaning == "in_progress":
                    reason = "Human cleaning is observed in progress; completion/inspection time is not yet confirmed."
                else:
                    reason = "Room readiness data is incomplete; obtain inspection before promising access."
                result.candidates.append(Candidate(**common, action="notify_guest", reason=reason, variant="unconfirmed"))
                continue
            stamp = state.get("last_observed_utc")
            if not stamp or instant(stamp) < instant(ago(now, self.config.telemetry_max_age_minutes)):
                result.candidates.append(Candidate(**common, action="defer_action", reason="Housekeeping telemetry is stale; verify vacancy and condition before scheduling.", variant="stale"))
                continue
            if room in planned_rooms:
                continue
            other_job = next((j for k, j in self.scheduled_jobs.items() if k != key and j["room_id"] == room), None)
            if other_job:
                result.candidates.append(Candidate(**common, action="notify_guest",
                    reason=f"An existing simulated human job for this room ends at {other_job['end_utc']}; readiness still requires observed completion and release checks. No duplicate cleaning is reserved.", variant="existing_job"))
                continue
            job = self.scheduled_jobs.get(key)
            if job:
                if instant(job["end_utc"]) <= instant(now):
                    result.candidates.append(Candidate(**common, action="defer_action",
                        reason="Simulated cleaning reservation ended, but no observed readiness confirmation exists; verify before further scheduling or release.", variant="verify_job"))
                    continue
                known_slots = repo.get_staff_slots(job["start_utc"], job["end_utc"], now, role="cleaner")
                still_valid = any(s.get("resource_id") == job["resource_id"] and scheduled(s, now) for s in known_slots)
                if still_valid:
                    continue
                calendar.cancel(job["decision_id"])
                planning.cancel(job["decision_id"])
                self.scheduled_jobs.pop(key)
                self.revisions[key] = self.revisions.get(key, 0) + 1
                result.candidates.append(Candidate(**common, action="notify_guest", priority="high",
                    reason="Previously planned cleaner is now known unavailable; withdraw the estimate and seek a new slot.", variant="withdraw_estimate"))
            duration = self.config.inspection_minutes if cleaning == "inspection_required" else self.config.cleaning_minutes
            # Roster rows are authoritative for actual shifts, not nominal building hours.
            slots = repo.get_staff_slots(now, end_limit, now, role="cleaner")
            choice = planning.first_slot(slots, now, end_limit, duration, room=room)
            if choice:
                start, end, resource, slot = choice
                planning.reserve(resource, start, end, "planned:" + key, now, room=room)
                planned_rooms.add(room)
                evidence.append(f"staff_roster.csv:{slot['shift_id']}")
                late = instant(end) > instant(desired)
                action = "request_inspection" if cleaning == "inspection_required" else "schedule_cleaning"
                context = {"room": state, "slot": slot, "start_utc": start, "end_utc": end,
                           "entry_required": True, "estimated_minutes": duration}
                result.candidates.append(Candidate(**common, action=action, resource=resource, context=context,
                    reason=f"Vacant room {cleaning}; reserve {resource} {start}..{end}. Duration {duration} min is a simulated planning assumption; requested deadline {'missed' if late else 'feasible'}, subject to inspection.", variant=f"staff:{self.revisions.get(key, 0)}"))
                result.candidates.append(Candidate(**common, action="notify_guest", priority="high" if late else "medium",
                    reason=f"Proposed readiness estimate {end}, conditional on human completion and release checks; requested {desired}. No access is promised.", variant=f"estimate:{self.revisions.get(key, 0)}",
                    context={"after_action": action}))
            elif waiting and any(r.get("resource_id") == "ONCALL_1" for r in building.get("staff", {}).get("resources", [])):
                call = oncall_slot(now, end_limit, duration, planning)
                if call:
                    start, end = call
                    result.candidates.append(Candidate(**common, action="schedule_cleaning", resource="ONCALL_1", human=True,
                        reason=f"Waiting guest, no regular crew capacity. Propose costly ONCALL_1 {start}..{end}; 45-minute travel and availability are simulated assumptions requiring confirmation.",
                        context={"room": state, "start_utc": start, "end_utc": end, "waiting_guest": True,
                                 "oncall": True, "entry_required": True, "notable_cost": True}, variant="oncall"))
                else:
                    result.candidates.append(Candidate(**common, action="notify_guest", priority="high",
                        reason="No feasible known staff slot or on-call window; propose delay and human assistance, with no invented readiness time.", variant="no_capacity"))
            else:
                result.candidates.append(Candidate(**common, action="notify_guest", priority="high",
                    reason="No eligible known cleaner can complete the job in the planning window; propose delay and escalation instead of inventing capacity.", variant="no_capacity"))
        return result
