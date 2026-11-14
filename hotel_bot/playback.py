"""Read-only, seekable presentation of a committed replay's original events.

The model and DecisionEngine are never invoked here. Source emitters provide the
original clock/order; saved extraction, decision and audit records supply results.
"""
from __future__ import annotations

from bisect import bisect_right
from copy import deepcopy

from common.contracts.schemas import DecisionRecordV2, NormalizedEventV1
from engine.core import instant
from ingestion import events as source_events
from ingestion.replay import ReplayData, ROOM_STATE_FIELDS

from .replay import (ARTIFACTS, ReplayConflictError, ReplayValidationError,
                     _artifact_rows, _source_hashes)


class PlaybackTimeline:
    """Immutable source index with ordinal-exact snapshots and paginated events."""

    def __init__(self, job):
        public = job.public
        if public.get("complete") is not True or public.get("status") not in {"completed", "degraded"}:
            raise ReplayConflictError("Finish preparing the whole dataset before playback")
        if _source_hashes(job.dataset_path) != public.get("input_hashes_sha256"):
            raise ReplayConflictError("The original dataset changed; prepare a new replay")
        self.repo = ReplayData(job.dataset_path, use_llm=False)
        self.job_id, self.dataset_id = public["id"], public["dataset_id"]
        self.final_progress = deepcopy(public["progress"])
        count = self.final_progress
        extracts = _artifact_rows(job.root / ARTIFACTS["extractions"], count["guest_processed"])
        decisions = _artifact_rows(job.root / ARTIFACTS["decisions"], count["decisions"])
        audits = _artifact_rows(job.root / ARTIFACTS["audit"], count["audit_rows"])
        journal = _artifact_rows(job.root / ARTIFACTS["journal"], 500_000)
        if (len(extracts) != count["guest_processed"] or len(decisions) != count["decisions"]
                or len(audits) != count["audit_rows"]):
            raise ReplayValidationError("Saved replay output counts differ")
        self.extractions = {}
        guest_events = []
        sources = {str(row["request_id"]): row for row in self.repo.ds.guest_requests if row.get("timestamp_utc")}
        for row in extracts:
            event = NormalizedEventV1.model_validate(row["normalized_event"])
            source = sources.get(event.source.record_id)
            if (event.event_type != "guest_request" or source is None or row["source_record"] != source
                    or event.event_id != "guest:" + event.source.record_id
                    or instant(event.available_at_utc) != instant(source["timestamp_utc"])
                    or event.event_id in self.extractions):
                raise ReplayValidationError("Saved guest extraction differs from its source")
            self.extractions[event.event_id] = row
            guest_events.append(event)
        if len(guest_events) != len(sources):
            raise ReplayValidationError("Saved replay does not cover every guest source event")

        # Match build_normalized_events without its guest parser entry point.
        events = list(guest_events)
        for emit in (source_events._raw_events, source_events._booking_events,
                     source_events._roster_events, source_events._work_order_events,
                     source_events._tick_events):
            events.extend(emit(self.repo))
        unique = {}
        for event in events:
            unique.setdefault(event.event_id, event)
        self.events = sorted(unique.values(), key=lambda e: (
            instant(e.available_at_utc), e.event_type == "clock_tick", e.event_id))
        if len(self.events) != count["total_events"] or count["events_processed"] != len(self.events):
            raise ReplayValidationError("Saved replay does not cover the exact original event stream")
        if not self.events:
            raise ReplayValidationError("The source event stream is empty")
        self.times = [instant(event.available_at_utc) for event in self.events]
        self.ordinals = {event.event_id: index for index, event in enumerate(self.events, 1)}
        self.rooms = {r["id"]: r for r in self.repo.ds.building.get("building", {}).get("rooms", []) if r.get("id")}
        self._manifest = {"job_id": self.job_id, "dataset_id": self.dataset_id,
            "start_utc": self.events[0].available_at_utc, "end_utc": self.events[-1].available_at_utc,
            "total_events": len(self.events), "total_guests": len(guest_events),
            "source_order": "available_at_utc,clock_last,event_id", "mode": "archived_read_only",
            "observation_boundary": "all_source_facts_available_at_frame_timestamp",
            "effects_applied": False, "model_calls": 0, "engine_rerun": False}
        operational_start = self.repo.ds.manifest.get("start_utc")
        try:
            if self.times[0] <= instant(operational_start) <= self.times[-1]:
                self._manifest["operational_start_utc"] = operational_start
        except (ValueError, TypeError, AttributeError):
            pass

        by_id = {row["event_id"]: DecisionRecordV2.model_validate(row).model_dump(mode="json") for row in decisions}
        audit_by_id = {row["decision_id"]: row for row in audits}
        if len(by_id) != len(decisions) or len(audit_by_id) != len(audits) or set(by_id) != set(audit_by_id):
            raise ReplayValidationError("Saved decisions and audits do not join")
        self.decisions, seen_decisions, journal_ordinals = [], set(), []
        for entry in journal:
            kind, detail = entry["kind"], entry["details"]
            if kind == "engine_decision":
                record = detail["decision_record"]
                identifier = record["event_id"]
                ordinal = self.ordinals.get(detail["trigger"]["event_id"])
                if (ordinal is None or identifier in seen_decisions or by_id.get(identifier) != record
                        or audit_by_id.get(identifier) != detail["audit"]
                        or instant(record["timestamp_utc"]) > self.times[ordinal - 1]):
                    raise ReplayValidationError("Saved decision has no matching causal source trigger")
                seen_decisions.add(identifier)
                self.decisions.append((ordinal, {"decision": record, "audit": detail["audit"],
                    "autonomous": not record["requires_human_approval"] and not record["requires_guest_consent"]}))
            elif kind == "guest_extraction":
                event_id = detail["normalized_event"]["event_id"]
                ordinal = self.ordinals.get(event_id)
                if ordinal is None or self.extractions.get(event_id) != detail:
                    raise ReplayValidationError("Saved extraction journal differs")
            elif kind == "replay_started":
                ordinal = 1
            elif kind in {"replay_finished", "replay_failed"}:
                ordinal = len(self.events)
            else:
                # Unanchored safety/finalization notes may be delayed until the
                # end of their equal-time batch, never revealed ahead of it.
                ordinal = max(1, bisect_right(self.times, instant(entry["timestamp"])))
            journal_ordinals.append(max(ordinal, journal_ordinals[-1] if journal_ordinals else 0))
        if seen_decisions != set(by_id):
            raise ReplayValidationError("Saved decision journal is incomplete")
        self.journal_ordinals = journal_ordinals
        self.decisions.sort(key=lambda item: item[0])
        self.decision_ordinals = [row[0] for row in self.decisions]
        self.guest_ordinals = [index for index, event in enumerate(self.events, 1) if event.event_type == "guest_request"]
        self.roster = {}
        for ordinal, event in enumerate(self.events, 1):
            if event.event_type == "roster_update":
                self.roster.setdefault(event.source.record_id, []).append((ordinal, event.payload))
        self._prefix = []
        running = {key: 0 for key in ("guest_processed", "llm_count", "fallback_count", "source_label_count",
                   "parser_errors", "source_errors", "needs_review_count", "decisions", "audit_rows",
                   "autonomous_count", "pending_approval_count", "safety_rejections")}
        self._prefix.append(dict(running))
        by_ordinal = {}
        for ordinal, row in self.decisions:
            by_ordinal.setdefault(ordinal, []).append(row)
        safety_counts = {}
        for ordinal, entry in zip(journal_ordinals, journal):
            if entry["kind"] == "safety_rejection":
                safety_counts[ordinal] = safety_counts.get(ordinal, 0) + 1
        for ordinal, event in enumerate(self.events, 1):
            if event.event_type == "guest_request":
                row = self.extractions[event.event_id]
                running["guest_processed"] += 1
                method = event.extraction.method
                if method + "_count" in running:
                    running[method + "_count"] += 1
                running["parser_errors"] += int(row["parser_error"])
                running["source_errors"] += int(row["source_error"])
                running["needs_review_count"] += int(event.extraction.needs_review)
            for row in by_ordinal.get(ordinal, []):
                running["decisions"] += 1
                running["audit_rows"] += 1
                running["autonomous_count"] += int(row["autonomous"])
                running["pending_approval_count"] += int(row["decision"]["requires_human_approval"]
                                                          and row["audit"]["status"].startswith("pending_"))
            running["safety_rejections"] += safety_counts.get(ordinal, 0)
            self._prefix.append(dict(running))
        # First-frame latency must not include parsing a year's telemetry. Build
        # read-only indexes while the UI is still preparing the saved timeline.
        self.repo.ds.series("room_telemetry.csv")
        self.repo._event_index()

    def manifest(self):
        return deepcopy(self._manifest)

    def _event(self, ordinal):
        if not 1 <= ordinal <= len(self.events):
            return None
        event = self.events[ordinal - 1]
        return {"ordinal": ordinal, "event_id": event.event_id, "event_type": event.event_type,
            "available_at_utc": event.available_at_utc, "timestamp_utc": event.timestamp_utc,
            "room_id": event.entities.room_id, "source": event.source.model_dump(mode="json")}

    def timeline(self, after=0, limit=1000):
        if type(after) is not int or not 0 <= after <= len(self.events) or type(limit) is not int or not 1 <= limit <= 2000:
            raise ReplayValidationError("Invalid timeline page")
        end = min(len(self.events), after + limit)
        return {"manifest": self.manifest(), "events": [self._event(i) for i in range(after + 1, end + 1)],
                "next_cursor": end, "total": len(self.events), "has_more": end < len(self.events)}

    def _room_view(self, cursor, now):
        # DecisionEngine hydrates every fact available at the current timestamp
        # before processing a source event. Match that same observation boundary;
        # decisions, inbox, journal and counters still use exact source ordinals.
        rooms = []
        for room_id in self.repo.ds.room_ids:
            if cursor:
                state = deepcopy(self.repo.get_room_state(room_id, now) or {})
            else:
                state = {field: None for field in ROOM_STATE_FIELDS}
                state.update(room_id=room_id, occupancy="unknown", current_booking_id=None,
                             last_observed_utc=None, as_of_utc=None, evidence=[])
            occupancy, cleaning = state.get("occupancy"), state.get("housekeeping_state")
            status = ("occupied" if occupancy == "occupied" else "cleaning" if cleaning in {"cleaning", "in_progress"}
                      else "attention" if cleaning == "dirty" else "vacant" if occupancy == "vacant" else "unknown")
            rooms.append({**state, "id": room_id, "floor": self.rooms.get(room_id, {}).get("floor"),
                          "status": status, "source": "historical_dataset", "fixture": False,
                          "release_confirmed": False})
        return rooms

    def frame(self, *, cursor=None, at=None):
        if cursor is not None and at is not None:
            raise ReplayValidationError("Choose a source cursor or a timestamp")
        if at is not None:
            try:
                selected_time = instant(at)
            except (ValueError, TypeError, AttributeError):
                raise ReplayValidationError("Invalid playback time") from None
            cursor = bisect_right(self.times, selected_time)
        else:
            cursor = 0 if cursor is None else cursor
        if type(cursor) is not int or not 0 <= cursor <= len(self.events):
            raise ReplayValidationError("Invalid playback cursor")
        now = self.events[cursor - 1].available_at_utc if cursor else None
        included = self.decisions[:bisect_right(self.decision_ordinals, cursor)]
        cancelled = {row["audit"].get("parameters", {}).get("cancelled_decision_id") for _, row in included}
        pending = [row for _, row in included if row["decision"]["requires_human_approval"]
                   and row["decision"]["event_id"] not in cancelled and row["audit"]["status"].startswith("pending_")
                   and (not row["audit"].get("parameters", {}).get("end_utc")
                        or instant(row["audit"]["parameters"]["end_utc"]) > instant(now))]
        guest_positions = self.guest_ordinals[:bisect_right(self.guest_ordinals, cursor)]
        messages = []
        for ordinal in guest_positions[-10:]:
            event = self.events[ordinal - 1]
            source = self.extractions[event.event_id]["source_record"]
            messages.append({"id": event.source.record_id, "room_id": event.entities.room_id,
                "text": source.get("text"), "language": source.get("language"), "created_at": event.timestamp_utc,
                "available_at_utc": event.available_at_utc, "source_ordinal": ordinal,
                "intents": event.payload["intents"], "needs_review": event.extraction.needs_review,
                "status": "needs_review" if event.extraction.needs_review else "processed",
                "extraction_method": event.extraction.method, "normalized_event": event.model_dump(mode="json")})
        people = []
        if cursor:
            for history in self.roster.values():
                position = bisect_right([row[0] for row in history], cursor) - 1
                if position < 0:
                    continue
                row = history[position][1]
                if row.get("start_utc") and row.get("end_utc") and instant(row["start_utc"]) <= instant(now) < instant(row["end_utc"]):
                    people.append({"id": row.get("resource_id"), "role": row.get("role"),
                        "status": row.get("effective_status_asof", "unknown"), "shift": row,
                        "fixture": False, "source": "staff_roster.csv", "position_observed": False})
        progress = {**self._prefix[cursor], "events_processed": cursor, "total_events": len(self.events),
                    "total_guests": len(self.guest_ordinals), "as_of_utc": now}
        if cursor == len(self.events):
            progress = deepcopy(self.final_progress)
        return deepcopy({"manifest": self.manifest(), "events_consumed": cursor, "source_ordinal": cursor,
            "as_of_utc": now, "current_event": self._event(cursor), "next_event": self._event(cursor + 1),
            "progress": progress, "journal_total": bisect_right(self.journal_ordinals, cursor),
            "snapshot": {"source": "historical_dataset", "dataset_id": self.dataset_id, "as_of_utc": now,
                "rooms": self._room_view(cursor, now), "people": people, "recent_messages": messages,
                "decisions": [row for _, row in included[-20:]], "pending_approvals": pending[-200:],
                "pending_approvals_total": len(pending), "pending_approvals_truncated": len(pending) > 200,
                "synthetic": True, "effects_applied": False, "approval_mode": "historical_read_only"}})
