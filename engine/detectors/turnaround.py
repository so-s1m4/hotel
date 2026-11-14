"""S01: retain requests and reconsider when observations/staff availability change."""
from datetime import timedelta
from copy import deepcopy

from engine.core import Candidate, DetectionResult, EngineConfig, instant, utc, room_state, ago
from engine.resources import ResourceCalendar, oncall_slot, scheduled


class TurnaroundDetector:
    def __init__(self, config: EngineConfig, *, allow_model_intents: bool = False):
        self.config = config
        self.allow_model_intents = allow_model_intents
        self.pending: dict[str, object] = {}
        self.scheduled_jobs: dict[str, dict] = {}
        self.revisions: dict[str, int] = {}
        self.target_rooms: dict[str, str] = {}
        self.target_evidence: dict[str, list[str]] = {}

    def detect(self, event, repo, calendar: ResourceCalendar, building: dict) -> DetectionResult:
        result = DetectionResult()
        now = event.available_at_utc
        raw_type = event.payload.get("type")
        if event.event_type == "guest_request":
            # Source labels decide scope; arbitrary non-guest payloads are not
            # guest extraction envelopes and need not have iterable intents.
            source_type = event.payload.get("request_type")
            wanted = isinstance(source_type, str) and source_type in {"early_checkin", "waiting_for_room"}
            if self.allow_model_intents and source_type is None:
                from engine.detectors.guest_services import verified_guest_intents
                wanted = any(item["intent"] in {"early_checkin", "waiting_for_room"}
                             for item in verified_guest_intents(event))
            if wanted:
                self.pending.setdefault(event.event_id, event)
        if raw_type == "arrival_waiting_room":
            self.pending.setdefault(event.event_id, event)
        if event.event_type not in {"guest_request", "clock_tick", "operational_event", "roster_update"}:
            return result
        planning = deepcopy(calendar)
        planned_rooms = set()
        known_rooms = {r["id"] for r in building["building"]["rooms"]}
        for request_id, request in list(self.pending.items()):
            key = "turnaround:" + request_id
            room = self.target_rooms.get(request_id, request.entities.room_id)
            source_refs = [f"{request.source.file}:{request.source.record_id}"]
            request_time = instant(request.available_at_utc)
            if instant(now) - request_time >= timedelta(hours=48):
                self._retire(request_id, key, calendar, planning,
                    f"Request {request.source.record_id} is older than the 48-hour planning horizon as of {now}", source_refs)
                result.resolved.add(key)
                if room not in known_rooms:
                    result.candidates.append(Candidate(key, "BUILDING", "room_turnaround", "notify_guest",
                        "The 48-hour arrival planning horizon expired without a verified room identity; request closed for manual booking/room clarification.",
                        source_refs, "Confirm the booking and room with operations before opening a new readiness request.",
                        "Propose manual identity review; no delivery, room entry or readiness is assumed.",
                        request_ids=[request.entities.guest_request_id] if request.entities.guest_request_id else [],
                        context={"notification_scope":"booking:"+request.entities.booking_id} if request.entities.booking_id else {},
                        variant="identity_expired"))
                continue
            booking_id = request.entities.booking_id
            booking = repo.get_booking(booking_id, now) if booking_id else None
            if booking and (not booking.get("created_utc") or instant(booking["created_utc"]) > instant(now)):
                booking = None
            assignment_refs = list(self.target_evidence.get(request_id, []))
            if booking:
                booking_refs = [f"bookings.csv:{booking_id}"] + [ref for ref in booking.get("evidence", []) if isinstance(ref, str)]
                observed = booking.get("observed_state")
                lifecycle_stamp = booking.get("observed_checkin_utc") if observed == "checked_in" else booking.get("observed_checkout_utc") if observed == "checked_out" else None
                future_lifecycle = lifecycle_stamp and instant(lifecycle_stamp) > instant(now)
                if observed in {"checked_in", "checked_out", "no_show"} and not future_lifecycle:
                    self._retire(request_id, key, calendar, planning,
                        f"Booking {booking_id} is observed {observed} in room {booking.get('observed_room_id') or booking.get('room_id')}; the earlier arrival-readiness request is no longer active as of {now}",
                        source_refs + booking_refs)
                    result.resolved.add(key)
                    continue
                target = booking.get("observed_room_id") or booking.get("room_id")
                if room not in known_rooms and not future_lifecycle and target in known_rooms:
                    room = target
                    assignment_refs = booking_refs
                    self.target_rooms[request_id] = room
                    self.target_evidence[request_id] = assignment_refs
                if room not in known_rooms:
                    # The booking exists but its assignment is not yet causal or
                    # valid; leave identity handling below to request review.
                    target = None
                if room in known_rooms and (future_lifecycle or target and target != room):
                    moves = repo.get_events(booking["created_utc"], now, now, event_type="room_reassigned")
                    moves = [move for move in moves if move.get("booking_id") == booking_id and move.get("timestamp_utc")
                             and instant(move["timestamp_utc"]) <= instant(now)
                             and (not move.get("available_at_utc") or instant(move["available_at_utc"]) <= instant(now))]
                    latest = max(moves, key=lambda m: (instant(m["timestamp_utc"]), str(m.get("event_id", "")))) if moves else None
                    justified = (not future_lifecycle and observed in {"not_arrived", "waiting_for_room"}
                                 and target in {r["id"] for r in building["building"]["rooms"]}
                                 and latest and latest.get("to_room") == target)
                    if not justified:
                        self._cancel_job(key, calendar, planning,
                            f"Booking {booking_id} and request {request.source.record_id} do not have a verified current room assignment as of {now}", source_refs + booking_refs)
                        result.candidates.append(Candidate(key, room, "room_turnaround", "notify_guest",
                            "Booking lifecycle or room assignment is inconsistent with the request; confirm the current assignment before promising readiness or scheduling work.",
                            source_refs + booking_refs, "Verify the booking's observed lifecycle and room assignment with operations.",
                            "Propose an assignment clarification; no room entry or readiness is assumed.",
                            request_ids=[request.entities.guest_request_id] if request.entities.guest_request_id else [], variant="assignment_unverified"))
                        continue
                    assignment_refs = [f"events.jsonl:{latest.get('event_id', 'room_reassigned')}:{latest['timestamp_utc']}"]
                    self._cancel_job(key, calendar, planning,
                        f"Observed room reassignment for booking {booking_id} from {room} to {target} at {latest['timestamp_utc']} supersedes the old-room plan",
                        source_refs + booking_refs + assignment_refs)
                    room = target
                    self.target_rooms[request_id] = room
                    self.target_evidence[request_id] = assignment_refs
            if room not in known_rooms:
                result.candidates.append(Candidate(key, "BUILDING", "room_turnaround", "notify_guest",
                    "Room identity is unverified; confirm the booking and assigned room before estimating readiness or scheduling work.",
                    source_refs, "Obtain a causally known booking-to-room assignment from operations.",
                    "Propose identity clarification; no delivery, room entry or readiness is assumed.",
                    request_ids=[request.entities.guest_request_id] if request.entities.guest_request_id else [], variant="identity_unverified"))
                continue
            state = room_state(repo, room, now)
            if booking_id and state.get("current_booking_id") == booking_id and state.get("occupancy") == "occupied":
                self._retire(request_id, key, calendar, planning,
                    f"Room {room} is observed occupied by booking {booking_id} as of {now}; the arrival-readiness request is already satisfied",
                    source_refs + state.get("evidence", []))
                result.resolved.add(key)
                continue
            observed_waiting = request.event_type == "operational_event" and request.payload.get("type") == "arrival_waiting_room"
            extraction = request.extraction
            uncertainty = set(extraction.uncertain_fields) if extraction else set()
            # Offline source labels retain their explicit metadata-priority
            # policy; they are not evidence that the guest is observed waiting.
            # A failed model call or semantic contradiction cannot use it.
            offline_label = bool(extraction and extraction.method == "source_label" and (
                uncertainty <= {"requested_time_utc", "details"}
                or "offline_mode" in uncertainty and uncertainty <= {
                    "requested_time_utc", "semantic_details", "offline_mode"}))
            extracted_intents = request.payload.get("intents")
            verified_extraction = bool(extraction and extraction.method == "llm" and not extraction.needs_review
                and isinstance(extracted_intents, list) and extracted_intents
                and all(isinstance(i, dict) and i.get("needs_review") is False for i in extracted_intents))
            waiting = observed_waiting or (request.event_type == "guest_request"
                and request.payload.get("request_type") == "waiting_for_room"
                and (verified_extraction or offline_label))
            if self.allow_model_intents and request.payload.get("request_type") is None:
                from engine.detectors.guest_services import verified_guest_intents
                model_intents = verified_guest_intents(request)
                waiting = waiting or any(item["intent"] == "waiting_for_room" for item in model_intents)
            desired = None
            intents = request.payload.get("intents")
            text = request.payload.get("original_text")
            for intent in intents if isinstance(intents, list) else []:
                if not isinstance(intent, dict):
                    continue
                quote = intent.get("evidence_quote")
                requested_time = intent.get("requested_time_utc")
                if (intent.get("intent") == "early_checkin" and not intent.get("needs_review")
                        and isinstance(requested_time, str) and requested_time and isinstance(quote, str) and quote
                        and isinstance(text, str) and quote in text
                        and not (request.extraction and request.extraction.needs_review)):
                    try:
                        instant(requested_time)
                    except (ValueError, TypeError, OverflowError):
                        continue
                    desired = requested_time
                    break
            if waiting:
                desired = now
            evidence = [*source_refs, *assignment_refs, *state.get("evidence", [])]
            # A checkout after the clean sample invalidates that readiness
            # evidence; the old clean value cannot resolve a newer dirty episode.
            dirty_trigger = getattr(calendar, "last_dirty_trigger", {}).get(room)
            clean_stamp = state.get("last_observed_utc")
            clean_after_dirty = not dirty_trigger or clean_stamp and instant(clean_stamp) >= instant(dirty_trigger)
            fresh_clean = clean_stamp and instant(ago(now, self.config.telemetry_max_age_minutes)) <= instant(clean_stamp) <= instant(now)
            # A request alone is neither proof of a conflict nor readiness.
            if state.get("occupancy") == "vacant" and state.get("housekeeping_state") == "clean" and clean_after_dirty and fresh_clean:
                result.resolved.add(key)
                for completed_key, completed in list(self.scheduled_jobs.items()):
                    if completed["room_id"] == room:
                        self._cancel_job(completed_key, calendar, planning,
                            f"Room {room} is now observed vacant and clean as of {now}; its proposed housekeeping task is no longer required",
                            evidence)
                continue
            observed_completion = getattr(calendar, "last_human_completion", {}).get(room)
            dirty_trigger = getattr(calendar, "last_dirty_trigger", {}).get(room)
            stamp = state.get("last_observed_utc")
            old_dirty = (state.get("housekeeping_state") in {"dirty", "in_progress"} and stamp and observed_completion
                         and instant(stamp) < instant(observed_completion) <= instant(now)
                         and not (dirty_trigger and instant(observed_completion) < instant(dirty_trigger) <= instant(now)))
            if old_dirty:
                observed_ref = f"events.jsonl:{room}:cleaning_completed:{observed_completion}"
                self._cancel_job(key, calendar, planning,
                    f"Observed human cleaning of {room} completed at {observed_completion}, after housekeeping telemetry {stamp}; wait for verification instead of duplicating the job",
                    evidence + [observed_ref])
                result.candidates.append(Candidate(key, room, "room_turnaround", "defer_action",
                    "Human cleaning completed after the last dirty/in-progress telemetry; obtain fresh readiness/inspection evidence instead of scheduling duplicate cleaning.",
                    evidence + [observed_ref], "Verify fresh housekeeping state, vacancy and required inspection before offering access.",
                    "Await observed readiness verification without changing telemetry.", variant="post_completion_verification"))
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
                roster_refs = [f"staff_roster.csv:{s.get('shift_id')}:known={s.get('status_known_utc')}:effective={s.get('effective_status_asof', s.get('status'))}"
                               for s in known_slots if s.get("resource_id") == job["resource_id"]]
                if not roster_refs:
                    roster_refs = [f"staff_roster.csv:{job['resource_id']}:queried={job['start_utc']}..{job['end_utc']}:as_of={now}"]
                self._cancel_job(key, calendar, planning,
                    f"No eligible known shift remains for {job['resource_id']} during {job['start_utc']}..{job['end_utc']} as of {now}", roster_refs)
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
                    if not planning.room_available(room, start, end, now):
                        observed = [job for jobs in planning.observed_busy.values() for job in jobs.values() if job.get("room_id") == room]
                        evidence.extend(f"events.jsonl:{job['event_id']}:{job['start_utc']}" for job in observed)
                        evidence.extend(f"decisions.jsonl:{ident}" for a, z, ident in planning.room_reservations.get(room, [])
                                        if instant(start) < instant(z) and instant(end) > instant(a))
                        result.candidates.append(Candidate(**common, action="notify_guest", priority="high",
                            reason="The room has observed work in progress or a conflicting planned job; its future availability is not verified. Confirm completion before proposing an on-call entry or a readiness time.",
                            variant="oncall_room_unverified"))
                        continue
                    # This batch's proposals consume local planning capacity,
                    # while actual pending holds remain executor-owned.
                    planning.reserve_tentative("ONCALL_1", start, end, "planned:" + key, now, room=room)
                    planned_rooms.add(room)
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
        for candidate in result.candidates:
            original = self.pending.get(candidate.key.removeprefix("turnaround:"))
            if original and original.entities.booking_id:
                candidate.context.setdefault("notification_scope", "booking:" + original.entities.booking_id)
        return result

    def _cancel_job(self, key, calendar, planning, reason, evidence):
        job = self.scheduled_jobs.pop(key, None)
        if not job:
            return
        identifier = job["decision_id"]
        calendar.cancel(identifier, reason=reason)
        planning.cancel(identifier, reason=reason)
        for entry in reversed(calendar.cancellations):
            if entry["decision_id"] == identifier:
                entry["evidence"] = list(dict.fromkeys([*entry.get("evidence", []), *evidence]))
                break
        self.revisions[key] = self.revisions.get(key, 0) + 1

    def _retire(self, request_id, key, calendar, planning, reason, evidence):
        self._cancel_job(key, calendar, planning, reason, evidence)
        self.pending.pop(request_id, None)
        self.target_rooms.pop(request_id, None)
        self.target_evidence.pop(request_id, None)
