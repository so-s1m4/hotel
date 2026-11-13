"""Stateful S01–S05 and cross-domain orchestration over the frozen ReplayDataGateway."""
from copy import deepcopy, copy
from collections import deque
import json
from hashlib import sha256
from itertools import groupby

from common.contracts.api import ReplayDataGateway
from common.contracts.schemas import NormalizedEventV1, DecisionRecordV2
from engine.core import Candidate, EngineConfig, instant, stable_id
from engine.decisions import EpisodeTracker
from engine.detectors.turnaround import TurnaroundDetector
from engine.detectors.water import WaterDetector
from engine.detectors.energy import EnergyDetector
from engine.detectors.maintenance import MaintenanceDetector
from engine.detectors.surge import SurgeDetector
from engine.detectors.logistics import LogisticsDetector
from engine.detectors.optimization import OptimizationDetector
from engine.executor import MockExecutor
from engine.resources import ResourceCalendar
from engine.notifications import NotificationLedger
from engine.safety import SafetyViolation, asset_ids, validate_safety


_MISSING = object()


class _ShallowListCheckpoint:
    def __init__(self, value):
        self.original = value
        self.items = list(value)


class _UndoDict(dict):
    """Record touched keys, never copy a growing history for each input event."""
    def __init__(self, values=(), *, mutable_values=False):
        super().__init__(values)
        self.mutable_values = mutable_values
        self.shallow_list_values = False
        self.undo = None

    def __deepcopy__(self, memo):
        return deepcopy(dict(self), memo)

    def touch(self, key):
        if self.undo is not None and key not in self.undo:
            value = dict.get(self, key, _MISSING)
            if self.shallow_list_values and isinstance(value, list):
                # Original-calendar metadata borrows live calendars; only the
                # list membership is transactional, never calendar identity.
                self.undo[key] = _ShallowListCheckpoint(value)
            else:
                self.undo[key] = deepcopy(value) if self.mutable_values and value is not _MISSING else value

    def __getitem__(self, key):
        if self.mutable_values or self.shallow_list_values:
            self.touch(key)
        return dict.__getitem__(self, key)

    def get(self, key, default=None):
        if self.mutable_values or self.shallow_list_values:
            self.touch(key)
        return dict.get(self, key, default)

    def __setitem__(self, key, value):
        self.touch(key)
        dict.__setitem__(self, key, value)

    def setdefault(self, key, default=None):
        self.touch(key)
        return dict.setdefault(self, key, default)

    def pop(self, key, *default):
        self.touch(key)
        return dict.pop(self, key, *default)

    def clear(self):
        for key in self:
            self.touch(key)
        dict.clear(self)

    def update(self, *args, **kwargs):
        for key, value in dict(*args, **kwargs).items():
            self[key] = value

    def finish(self, rollback=False):
        changes, self.undo = self.undo, None
        if rollback:
            for key, old in changes.items():
                if old is _MISSING:
                    dict.pop(self, key, None)
                else:
                    if isinstance(old, _ShallowListCheckpoint):
                        old.original[:] = old.items
                        old = old.original
                    dict.__setitem__(self, key, old)


class _UndoAddSet(set):
    """The observed/query/cancelled ID ledgers only ever add identities."""
    def add(self, value):
        if self.added is not None and value not in self:
            self.added.add(value)
        set.add(self, value)

    added = None


def _runtime_copy(value):
    # Detector history deques contain immutable tuples or frozen intervals.
    # Copy their pointers once, not every timestamp and evidence string.
    if isinstance(value, deque):
        return value.copy()
    if isinstance(value, dict):
        result = copy(value)
        for key, item in value.items():
            result[key] = _runtime_copy(item)
        return result
    if isinstance(value, list):
        return [_runtime_copy(item) for item in value]
    if isinstance(value, set):
        return value.copy()
    return value


class _EventTransaction:
    """Rollback engine-owned mock state; gateway reads remain external."""
    def __init__(self, engine):
        self.engine = engine
        self.maps, self.sets, self.snapshots = [], [], []
        self.logs = [(value, len(value)) for value in
                     (engine.executor.audit, engine.rejections, engine.calendar.cancellations, engine.work_orders)]
        self.touched = {}
        self.scalars = {key: getattr(engine, key) for key in
                        ("_observed_hydrated", "_observed_query_utc", "_cancellation_cursor", "_outbox")}
        self.calendar_scalars = {key: value for key, value in vars(engine.calendar).items()
                                 if not isinstance(value, (dict, set, list))}
        self.notification_suppressed = engine.notifications.suppressed
        for owner, keys, mutable in (
            (engine, ("_proposals", "_decisions"), False),
            (engine.executor, tuple(key for key in ("_submitted", "_completed", "_submitted_calendars")
                                    if hasattr(engine.executor, key)), False),
            (engine.episodes, ("_generations",), False),
            (engine.episodes, ("active",), True),
            (engine.notifications, ("history",), False),
            (engine, ("_observed_completions",), False),
            (engine.calendar, tuple(key for key, value in vars(engine.calendar).items() if isinstance(value, dict)), True),
        ):
            for key in keys:
                value = getattr(owner, key)
                if not isinstance(value, _UndoDict):
                    value = _UndoDict(value, mutable_values=mutable)
                    setattr(owner, key, value)
                if owner is engine.executor and key == "_submitted_calendars":
                    value.shallow_list_values = True
                value.undo = {}
                self.maps.append(value)
        for owner, key in ((engine, "_observed_event_ids"), (engine.calendar, "_cancelled_ids"),
                           (engine.notifications, "explicit_queries")):
            value = getattr(owner, key)
            if not isinstance(value, _UndoAddSet):
                value = _UndoAddSet(value)
                setattr(owner, key, value)
            value.added = set()
            self.sets.append(value)

    def detector(self, detector):
        self.snapshots.append((detector, {key: _runtime_copy(value) for key, value in vars(detector).items()
                                          if key not in {"config", "weak"}}))
        if hasattr(detector, "weak"):
            self.detector(detector.weak)

    def touch(self, value):
        self.touched.setdefault(id(value), (value, deepcopy(value)))

    def finish(self, rollback=False):
        for value in self.maps:
            value.finish(rollback)
        for value in self.sets:
            if rollback:
                value.difference_update(value.added)
            value.added = None
        if rollback:
            for owner, snapshot in reversed(self.snapshots):
                for key in set(vars(owner))-set(snapshot)-{"config", "weak"}:
                    delattr(owner, key)
                for key, value in snapshot.items():
                    setattr(owner, key, value)
            for value, size in self.logs:
                del value[size:]
            for value, old in self.touched.values():
                value.clear()
                value.update(old)
            for key, value in self.scalars.items():
                setattr(self.engine, key, value)
            for key, value in self.calendar_scalars.items():
                setattr(self.engine.calendar, key, value)
            self.engine.notifications.suppressed = self.notification_suppressed


class DecisionEngine:
    def __init__(self, repo: ReplayDataGateway, building: dict, config: EngineConfig | None = None):
        self.repo = repo
        self.building = deepcopy(building)
        self.config = config or EngineConfig()
        self.calendar = ResourceCalendar()
        self.calendar.resource_roles = {r["resource_id"]: r["role"] for r in building.get("staff", {}).get("resources", [])}
        self.notifications = NotificationLedger()
        self._observed_event_ids = set()
        self._observed_hydrated = False
        self._observed_query_utc = None
        self._observed_completions = {}
        self._cancellation_cursor = 0
        self._outbox = []
        self.calendar.last_human_completion = {}
        self.calendar.last_dirty_trigger = {}
        self._proposals = {}
        self._decisions = {}
        self._event_fingerprints = {}
        self.episodes = EpisodeTracker()
        self.executor = MockExecutor()
        self.turnaround = TurnaroundDetector(self.config)
        self.water = WaterDetector(self.config)
        self.energy = EnergyDetector(self.config)
        self.maintenance = MaintenanceDetector(self.config)
        self.surge = SurgeDetector(self.config)
        self.logistics = LogisticsDetector(self.config)
        self.optimization = OptimizationDetector(self.config)
        self.work_orders: list[dict] = []
        self.rejections: list[dict] = []
        self.event_count = 0
        self._last_order = None
        self._seen_ids: set[str] = set()
        self._allowed_ids = asset_ids(building)

    def process(self, event: NormalizedEventV1) -> list[DecisionRecordV2]:
        raw = event.model_dump() if isinstance(event, NormalizedEventV1) else event
        event = NormalizedEventV1.model_validate(deepcopy(raw))
        fingerprint = sha256(json.dumps(event.model_dump(), sort_keys=True).encode()).hexdigest()
        if event.event_id in self._event_fingerprints and self._event_fingerprints[event.event_id] != fingerprint:
            raise ValueError("Conflicting content for duplicate input event ID")
        if event.event_id in self._seen_ids:
            return []
        order = instant(event.available_at_utc)
        if self._last_order is not None and order < self._last_order:
            raise ValueError("Event stream must be sorted by available_at_utc")
        if instant(event.timestamp_utc) > instant(event.available_at_utc):
            raise ValueError("Future event occurrence cannot be used as observed evidence")
        if event.source.timestamp_utc and instant(event.source.timestamp_utc) > instant(event.available_at_utc):
            raise ValueError("Future source timestamp")
        transaction = _EventTransaction(self)
        self._transaction = transaction
        # Turnaround can be changed by cancellation before its detector runs.
        transaction.detector(self.turnaround)
        try:
            records = self._process_event(event)
        except BaseException:
            transaction.finish(rollback=True)
            raise
        else:
            transaction.finish()
            self._last_order = order
            self._seen_ids.add(event.event_id)
            self._event_fingerprints[event.event_id] = fingerprint
            self.event_count += 1
            return records
        finally:
            self._transaction = None

    def _process_event(self, event):
        now = event.available_at_utc
        self.calendar.current_time = now
        self._outbox = []
        declared_room = event.entities.room_id
        if (event.event_type == "guest_request" and declared_room not in {None, "BUILDING"}
                and declared_room not in {room["id"] for room in self.building["building"]["rooms"]}):
            # Missing identity can be clarified or resolved from known booking
            # facts. An explicit undeclared room is contradictory input, and
            # must not be silently retargeted into an actionable room workflow.
            self.rejections.append({"event_id": event.event_id,
                                    "reason": "Unknown candidate asset", "asset": declared_room})
            return []
        self._observe_activity(event)
        self._revalidate_plans(event)
        if event.event_type == "work_order_created":
            # Retain only scheduling information already known, not final outcome/status.
            self.work_orders.append({k: event.payload.get(k) for k in
                                     ("work_order_id", "created_utc", "asset_id", "scheduled_utc", "summary", "assigned_to", "type")})
        if event.event_type == "operational_event" and event.payload.get("type") == "work_order_completed":
            for order in self.work_orders:
                if order.get("work_order_id") == event.payload.get("work_order_id", event.source.record_id):
                    self._transaction.touch(order)
                    order["observed_completed_utc"] = now
                    # Completion narratives become usable only with this
                    # observed event, never from the original creation view.
                    for field in ("summary", "scheduled_utc", "asset_id", "assigned_to"):
                        if event.payload.get(field) is not None:
                            order[field] = deepcopy(event.payload[field])
        # Evaluate lazily: later planners see earlier accepted resource/capacity reservations.
        detectors = [lambda: self.turnaround.detect(event, self.repo, self.calendar, self.building),
                     lambda: self.water.detect(event, self.repo, self.building, self.work_orders),
                     lambda: self.energy.detect(event, self.repo, self.building),
                     lambda: self.maintenance.detect(event, self.repo, self.building, self.calendar, self.work_orders,
                         pending_plans=self._pending_plans(event.available_at_utc)),
                     lambda: self.surge.detect(event, self.repo, self.building, self.calendar),
                     lambda: self.logistics.detect(event, self.repo, self.building, self.calendar),
                     lambda: self.optimization.detect(event, self.repo, self.building)]
        records = self._drain_cancellations(event)
        accepted_actions = {}
        for index, detect in enumerate(detectors):
            detector = (self.turnaround, self.water, self.energy, self.maintenance,
                        self.surge, self.logistics, self.optimization)[index]
            if detector is not self.turnaround and not (
                detector in (self.water, self.energy, self.optimization)
                and event.event_type != "clock_tick"
            ):
                self._transaction.detector(detector)
            result = detect()
            records.extend(self._drain_cancellations(event))
            for key in result.resolved:
                for metadata in self._pending_plans(now):
                    if metadata["key"] == key:
                        self._cancel_tree(metadata["decision_id"], f"Observed recovery resolves {key}; withdraw the unexecuted pending proposal")
                self.episodes.close(key)
            records.extend(self._drain_cancellations(event))
            # Detector order puts resource reservations before dependent messages.
            for candidate in result.candidates:
                if candidate.asset not in self._allowed_ids:
                    self.rejections.append({"event_id": event.event_id, "reason": "Unknown candidate asset"})
                    continue
                if self.episodes.emitted(candidate.key, candidate.action, candidate.variant):
                    continue
                if candidate.context.get("after_action") and (candidate.key, candidate.context["after_action"]) not in accepted_actions:
                    continue
                scope = self._notification_scope(candidate, event)
                episode = self.episodes.episode(candidate.key, now)
                decision = candidate.record(now, episode)
                if candidate.context.get("after_action"):
                    decision.dependencies = [accepted_actions[(candidate.key, candidate.context["after_action"])]]
                if candidate.action == "notify_guest":
                    if event.event_type == "guest_request" and candidate.key == "turnaround:"+event.event_id:
                        candidate.context["explicit_query_id"] = event.entities.guest_request_id
                    if candidate.context.get("after_action"):
                        candidate.context["accepted_plan"] = True
                    self._outbox.append((deepcopy(candidate), decision, scope))
                    continue
                context = {**candidate.context, "building": self.building, "calendar": self.calendar, "max_age_minutes": self.config.telemetry_max_age_minutes}
                try:
                    validate_safety(decision, context)
                    audit = self.executor.execute(decision, context)
                except SafetyViolation as exc:
                    self.rejections.append({"event_id": event.event_id, "action": candidate.action, "reason": str(exc),
                                            "asset": candidate.asset, "resource_id": candidate.resource,
                                            "start_utc": context.get("start_utc"), "end_utc": context.get("end_utc"),
                                            "resource_reservations": deepcopy(self.calendar.reservations_for(candidate.resource)),
                                            "room_reservations": deepcopy(self.calendar.room_jobs(candidate.asset))})
                    continue
                self._proposals[decision.event_id] = deepcopy(candidate)
                self._decisions[decision.event_id] = decision.model_copy(deep=True)
                if candidate.action == "notify_guest":
                    self.notifications.mark(candidate, scope, now)
                self.episodes.mark(candidate.key, candidate.action, candidate.variant)
                accepted_actions[(candidate.key, candidate.action)] = decision.event_id
                if candidate.resource and audit["status"] == "simulated" and candidate.issue == "room_turnaround":
                    self.turnaround.scheduled_jobs[candidate.key] = {
                        "room_id": candidate.asset, "resource_id": candidate.resource, "start_utc": context["start_utc"],
                        "end_utc": context["end_utc"], "decision_id": decision.event_id,
                    }
                if candidate.action == "dispatch_robot" and candidate.context.get("task") == "carry_bin" and audit["status"] == "simulated":
                    self.logistics.open_transfers[candidate.asset] = {"decision_id": decision.event_id, "end_utc": context["end_utc"]}
                records.append(decision)
        records.extend(self._flush_notifications(event))
        return sorted(records, key=lambda r: r.event_id)

    def _pending_plans(self, now):
        ids = {ident for jobs in self.calendar.tentative_reservations.values()
               for start, end, ident in jobs if instant(end) > instant(now)}
        return [{"decision_id": ident, "key": self._proposals[ident].key,
                 "action": self._proposals[ident].action, "asset_id": self._proposals[ident].asset}
                for ident in sorted(ids) if ident in self._proposals]

    def _notification_scope(self, candidate, event):
        if candidate.context.get("notification_scope"):
            return candidate.context["notification_scope"]
        if candidate.key.startswith("turnaround:"):
            request = self.turnaround.pending.get(candidate.key[len("turnaround:"):])
            if request and request.entities.booking_id:
                return "booking:" + request.entities.booking_id
        if candidate.key.startswith("surge_job:"):
            return "booking:" + candidate.key[len("surge_job:"):]
        booking = candidate.context.get("room", {}).get("current_booking_id")
        if booking:
            return "booking:" + booking
        if candidate.request_ids:
            return "request:" + ",".join(sorted(candidate.request_ids))
        return "asset:" + candidate.asset

    def _observe_activity(self, event):
        now = event.available_at_utc
        raw_events = []
        if (not self._observed_hydrated or self._observed_query_utc is None
                or instant(now) > instant(self._observed_query_utc)):
            # Gateway room state already includes every fact queryable as of
            # now. Hydrate the calendar against that same knowledge boundary,
            # not just the prefix of equal-time normalized envelopes delivered.
            begin = self._observed_query_utc if self._observed_hydrated and self._observed_query_utc else "1970-01-01T00:00:00Z"
            raw_events.extend(self.repo.get_events(begin, now, now))
            self._observed_hydrated = True
            self._observed_query_utc = now
        if event.event_type == "operational_event":
            raw_events.append({**event.payload, "event_id": event.source.record_id,
                               "timestamp_utc": event.timestamp_utc,
                               "available_at_utc": now,
                               "room_id": event.payload.get("room_id") or event.entities.room_id,
                               "resource_id": event.payload.get("resource_id") or event.entities.resource_id})
        starts = {"cleaning_started": "cleaner", "robot_floor_cleaning_started": "robot"}
        ends = {"cleaning_completed", "robot_floor_cleaning_completed"}
        checkins = {"checkin", "checkin_early", "checkin_after_wait", "stay_in_progress"}
        checkouts = {"checkout", "checkout_early"}
        activity_types = checkins | checkouts | starts.keys() | ends
        for raw in sorted(raw_events, key=lambda r: (r.get("timestamp_utc", ""), r.get("event_id", ""))):
            stamp = raw.get("timestamp_utc")
            known = raw.get("available_at_utc") or stamp
            if not stamp or instant(stamp) > instant(now) or (known and instant(known) > instant(now)):
                continue
            kind = raw.get("type")
            if not isinstance(kind, str):
                continue  # Unknown operational payload cannot establish observed work.
            if kind not in activity_types:
                continue
            ident = raw.get("event_id") or stable_id("observed", stamp, kind, str(raw.get("room_id")), str(raw.get("resource_id")))
            if ident in self._observed_event_ids:
                continue
            self._observed_event_ids.add(ident)
            if kind in checkouts and raw.get("room_id"):
                self._advance_occurrence(self.calendar.last_dirty_trigger, raw["room_id"], stamp)
            if kind in checkins and raw.get("room_id"):
                for start, end, decision_id in self.calendar.room_jobs(raw["room_id"]):
                    if instant(end) > instant(now):
                        self._cancel_tree(decision_id, f"Observed guest occupancy {raw.get('event_id')} at {stamp}; room-entry proposal must be withdrawn")
            if kind not in starts and kind not in ends:
                continue
            resource, room = raw.get("resource_id"), raw.get("room_id")
            resource = resource if resource in self.calendar.resource_roles else None
            role = "robot" if kind.startswith("robot_") else "cleaner"
            if resource and self.calendar.resource_roles[resource] == "on_call_housekeeping" and role == "cleaner":
                role = "on_call_housekeeping"
            if not resource and not room:
                continue
            if kind in starts:
                # Availability may lag occurrence. A previously known matching
                # completion must also cover a start that arrives afterwards.
                completed = any(
                    instant(end) >= instant(stamp)
                    and (worker is None or resource == worker
                         or (resource is None and target is not None and completed_role == role))
                    and (target is None or room == target)
                    and (completed_role is None or role == completed_role)
                    for (worker, target, completed_role), end in self._observed_completions.items())
                if completed:
                    continue
                self.calendar.observe_start(resource, room, ident, stamp, role=role)
                # New actual work supersedes incompatible proposed jobs, with an explicit trace.
                conflicting = set()
                for worker, reservations in self.calendar.iter_reservations():
                    if worker == resource or (resource is None and self.calendar.resource_roles.get(worker) == starts[kind]):
                        conflicting.update(r[2] for r in reservations if instant(r[1]) > instant(now))
                conflicting.update(r[2] for r in self.calendar.room_jobs(room) if instant(r[1]) > instant(now))
                for decision_id in sorted(conflicting):
                    self._cancel_tree(decision_id, f"Observed {kind} {ident} at {stamp}; proposed resource/room availability is no longer verified")
            else:
                self._advance_occurrence(self._observed_completions, (resource, room, role), stamp)
                self.calendar.observe_end(resource, room, stamp, role=role)
                if kind == "cleaning_completed" and room:
                    self._advance_occurrence(self.calendar.last_human_completion, room, stamp)
                    dirty = self.calendar.last_dirty_trigger.get(room)
                    for start, end, decision_id in list(self.calendar.room_jobs(room)):
                        if instant(end) > instant(now) and (not dirty or instant(stamp) >= instant(dirty)):
                            self._cancel_tree(decision_id, f"Observed human cleaning_completed {ident} at {stamp}; verify readiness instead of duplicating work")

    @staticmethod
    def _advance_occurrence(history, key, stamp):
        if key not in history or instant(stamp) > instant(history[key]):
            history[key] = stamp

    def _drain_cancellations(self, event):
        records = []
        while self._cancellation_cursor < len(self.calendar.cancellations):
            cancelled = self.calendar.cancellations[self._cancellation_cursor]
            self._cancellation_cursor += 1
            ident = cancelled["decision_id"]
            old = self._decisions.get(ident)
            proposal = self._proposals.get(ident)
            if not old or not proposal:
                continue
            for dependant_id, dependant in list(self._decisions.items()):
                if ident in dependant.dependencies:
                    self._cancel_tree(dependant_id, f"Prerequisite proposed job {ident} was cancelled")
            self.episodes.close(proposal.key)
            for key, job in list(self.turnaround.scheduled_jobs.items()):
                if job["decision_id"] == ident:
                    self.turnaround.scheduled_jobs.pop(key)
                    self.turnaround.revisions[key] = self.turnaround.revisions.get(key, 0)+1
            candidate = Candidate("cancel:"+ident, old.affected_assets[0], old.issue_type, "defer_action",
                f"Cancel/supersede proposed job {ident}: {cancelled['reason']}. No historical work is undone.",
                list(dict.fromkeys([f"{event.source.file}:{event.source.record_id}", f"decisions.jsonl:{ident}",
                                    *cancelled.get("evidence", []), *self._cancellation_evidence(old, event)])),
                "Replan only against fresh observed room/resource state; withdraw any prior readiness estimate.",
                "Cancel a simulation reservation only.")
            decision = candidate.record(event.available_at_utc, stable_id("episode", "cancel", ident))
            decision.dependencies = [ident]
            context = {"building": self.building, "calendar": self.calendar, "cancelled_decision_id": ident}
            self.executor.execute(decision, context)
            records.append(decision)
            self._decisions[decision.event_id] = decision.model_copy(deep=True)
            if any(d.action == "notify_guest" and ident in d.dependencies for d in self._decisions.values()):
                notice = Candidate("cancel_notice:"+ident, old.affected_assets[0], old.issue_type, "notify_guest",
                    f"Please withdraw the earlier conditional readiness estimate linked to {ident}; its simulated resource plan was superseded. A new readiness time requires fresh verification.",
                    candidate.evidence, "Confirm a new feasible plan and observed readiness before offering access.",
                    "Propose a correction message, without asserting delivery.", priority="high", variant="cancelled_plan:"+ident,
                    request_ids=old.guest_request_ids, context={"notification_scope": self._notification_scope(proposal, event)})
                scope = self._notification_scope(notice, event)
                correction = notice.record(event.available_at_utc, stable_id("episode", "cancel_notice", ident))
                correction.dependencies = [decision.event_id]
                self._outbox.append((notice, correction, scope))
        self._cancellation_cursor = len(self.calendar.cancellations)
        return records

    def _cancel_tree(self, decision_id, reason, evidence=None):
        self.calendar.cancel(decision_id, reason=reason)
        if evidence:
            for entry in self.calendar.cancellations:
                if entry["decision_id"] == decision_id:
                    self._transaction.touch(entry)
                    entry["evidence"] = list(dict.fromkeys([*entry.get("evidence", []), *evidence]))
        for ident, record in list(self._decisions.items()):
            if decision_id in record.dependencies:
                self.calendar.cancel(ident, reason=f"Prerequisite {decision_id} cancelled: {reason}")

    def _cancellation_evidence(self, old, event):
        refs = []
        room_ids = {r["id"] for r in self.building["building"]["rooms"]}
        if old.affected_assets[0] in room_ids:
            state = self.repo.get_room_state(old.affected_assets[0], event.available_at_utc) or {}
            refs.extend(state.get("evidence", []))
        if old.resource_id:
            proposal = self._proposals[old.event_id]
            end = proposal.context.get("end_utc")
            if end and instant(end) > instant(event.available_at_utc):
                role = self.calendar.resource_roles.get(old.resource_id)
                for row in self.repo.get_staff_slots(event.available_at_utc, end, event.available_at_utc, role=role):
                    if row.get("resource_id") == old.resource_id:
                        refs.append(f"staff_roster.csv:{row['shift_id']}:effective={row.get('effective_status_asof')}:known={row.get('status_known_utc')}")
        return refs

    def _revalidate_plans(self, event):
        if event.event_type not in {"roster_update", "clock_tick", "operational_event"}:
            return
        from engine.resources import scheduled
        now = event.available_at_utc
        active = {ident for _, jobs in self.calendar.iter_reservations() for start, end, ident in jobs if instant(end) > instant(now)}
        for ident in sorted(active):
            record = self._decisions.get(ident)
            proposal = self._proposals.get(ident)
            if not record or not proposal or not record.resource_id:
                continue
            rooms = {room["id"] for room in self.building["building"]["rooms"]}
            state = (self.repo.get_room_state(proposal.asset, now) or {}) if proposal.asset in rooms else {}
            move_into_room = (event.event_type == "operational_event"
                              and event.payload.get("type") == "room_reassigned"
                              and event.payload.get("to_room") == proposal.asset)
            if move_into_room and state.get("occupancy") == "occupied":
                self._cancel_tree(ident, f"Observed incoming room reassignment occupies {proposal.asset} as of {now}",
                                  evidence=[f"{event.source.file}:{event.source.record_id}", *state.get("evidence", [])])
                continue
            observed = state.get("last_observed_utc")
            dirty = self.calendar.last_dirty_trigger.get(proposal.asset)
            clean = (state.get("housekeeping_state") == "clean" and observed
                     and (not dirty or instant(observed) >= instant(dirty))
                     and instant(record.timestamp_utc) <= instant(observed) <= instant(now)
                     and (instant(now)-instant(observed)).total_seconds() <= self.config.telemetry_max_age_minutes*60)
            if clean and (proposal.action == "schedule_cleaning"
                          or proposal.action == "dispatch_robot" and proposal.context.get("task") == "floor_cleaning"):
                self._cancel_tree(ident, f"Fresh clean telemetry makes proposed cleaning of {proposal.asset} unnecessary as of {now}",
                                  evidence=state.get("evidence", []))
                continue
            scope = self._notification_scope(proposal, event)
            if scope.startswith("booking:"):
                booking = self.repo.get_booking(scope[len("booking:"):], now)
                if booking and booking.get("created_utc") and instant(booking["created_utc"]) <= instant(now):
                    moved = booking.get("observed_room_id") and booking["observed_room_id"] != proposal.asset
                    if booking.get("observed_state") in {"checked_in", "checked_out", "no_show"} or moved:
                        self._cancel_tree(ident, f"Booking {booking['booking_id']} observed {booking.get('observed_state')} in room {booking.get('observed_room_id')}; supersedes proposed room job as of {now}",
                                          evidence=[f"bookings.csv:{booking['booking_id']}", *booking.get("evidence", [])])
                        continue
            if record.resource_id == "ONCALL_1":
                continue
            if event.event_type == "operational_event":
                continue  # Shift changes have their own roster events; observed work is handled separately.
            role = self.calendar.resource_roles.get(record.resource_id)
            start, end = proposal.context["start_utc"], proposal.context["end_utc"]
            slots = self.repo.get_staff_slots(now, end, now, role=role)
            valid = any(s.get("resource_id") == record.resource_id and scheduled(s, now)
                        and instant(s["start_utc"]) <= instant(start) and instant(s["end_utc"]) >= instant(end) for s in slots)
            if not valid:
                self._cancel_tree(ident, f"Resource {record.resource_id} has no eligible known shift for its proposed interval as of {now}")

    def _flush_notifications(self, event):
        groups = {}
        for candidate, decision, scope in self._outbox:
            topic = "readiness" if candidate.issue in {"room_turnaround", "occupancy_surge"} else candidate.issue
            groups.setdefault((candidate.asset, scope, topic), []).append((candidate, decision, scope))
        records = []
        priorities = {"low": 0, "medium": 1, "high": 2, "critical": 3}
        for items in groups.values():
            def rank(item):
                c = item[0]
                correction = c.variant.split(":", 1)[0] in {"cancelled_plan", "withdraw_estimate"}
                return (bool(c.context.get("accepted_plan")), correction, bool(c.context.get("explicit_query_id")), priorities[c.priority])
            candidate, decision, scope = max(items, key=rank)
            decision.evidence = sorted({ref for c, d, scope in items for ref in d.evidence})
            decision.guest_request_ids = sorted({request_id for c, d, scope in items for request_id in d.guest_request_ids})
            corrections = [d for c, d, _ in items if c.variant.split(":", 1)[0] in {"cancelled_plan", "withdraw_estimate"}]
            if candidate.context.get("accepted_plan") and corrections:
                decision.reason = "Earlier simulated plan/estimate is superseded. " + decision.reason
                decision.dependencies = list(dict.fromkeys(decision.dependencies + [dep for d in corrections for dep in d.dependencies]))
                candidate.reason = decision.reason
            if self.notifications.allow(candidate, scope, event.available_at_utc):
                context = {**candidate.context, "building": self.building, "calendar": self.calendar}
                self.executor.execute(decision, context)
                self.notifications.mark(candidate, scope, event.available_at_utc)
                self._proposals[decision.event_id] = deepcopy(candidate)
                self._decisions[decision.event_id] = decision.model_copy(deep=True)
                records.append(decision)
            for c, d, _ in items:
                if c.key in self.episodes.active:
                    self.episodes.mark(c.key, c.action, c.variant)
        self._outbox = []
        return records

    def run(self) -> list[DecisionRecordV2]:
        records = []
        # Every fact available at a clock instant precedes its periodic scan.
        # Only equal-time envelopes are buffered; later facts stay unavailable.
        snapshots = (deepcopy(event) for event in self.repo.iter_events())
        for _, events in groupby(snapshots, key=lambda e: instant(e.available_at_utc)):
            batch = sorted(events, key=lambda e: (e.event_type == "clock_tick", e.event_id))
            for event in batch:
                records.extend(self.process(event))
        return records
