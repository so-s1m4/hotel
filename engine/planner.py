"""Stateful S01–S03 orchestration over the frozen ReplayDataGateway."""
from copy import deepcopy

from common.contracts.api import ReplayDataGateway
from common.contracts.schemas import NormalizedEventV1, DecisionRecordV2
from engine.core import EngineConfig, instant
from engine.decisions import EpisodeTracker
from engine.detectors.turnaround import TurnaroundDetector
from engine.detectors.water import WaterDetector
from engine.detectors.energy import EnergyDetector
from engine.executor import MockExecutor
from engine.resources import ResourceCalendar
from engine.safety import SafetyViolation, asset_ids, validate_safety


class DecisionEngine:
    def __init__(self, repo: ReplayDataGateway, building: dict, config: EngineConfig | None = None):
        self.repo = repo
        self.building = deepcopy(building)
        self.config = config or EngineConfig()
        self.calendar = ResourceCalendar()
        self.episodes = EpisodeTracker()
        self.executor = MockExecutor()
        self.turnaround = TurnaroundDetector(self.config)
        self.water = WaterDetector(self.config)
        self.energy = EnergyDetector(self.config)
        self.work_orders: list[dict] = []
        self.rejections: list[dict] = []
        self.event_count = 0
        self._last_order = None
        self._seen_ids: set[str] = set()
        self._allowed_ids = asset_ids(building)

    def process(self, event: NormalizedEventV1) -> list[DecisionRecordV2]:
        event = NormalizedEventV1.model_validate(event)
        order = (instant(event.available_at_utc), event.event_id)
        if self._last_order is not None and order < self._last_order:
            raise ValueError("Event stream must be sorted by (available_at_utc, event_id)")
        if instant(event.timestamp_utc) > instant(event.available_at_utc):
            raise ValueError("Future event occurrence cannot be used as observed evidence")
        if event.source.timestamp_utc and instant(event.source.timestamp_utc) > instant(event.available_at_utc):
            raise ValueError("Future source timestamp")
        self._last_order = order
        if event.event_id in self._seen_ids:
            return []
        self._seen_ids.add(event.event_id)
        self.event_count += 1
        now = event.available_at_utc
        if event.event_type == "work_order_created":
            # Retain only scheduling information already known, not final outcome/status.
            self.work_orders.append({k: event.payload.get(k) for k in
                                     ("work_order_id", "created_utc", "asset_id", "scheduled_utc", "summary")})
        results = [self.turnaround.detect(event, self.repo, self.calendar, self.building),
                   self.water.detect(event, self.repo, self.building, self.work_orders),
                   self.energy.detect(event, self.repo, self.building)]
        records = []
        accepted_actions = set()
        for result in results:
            for key in result.resolved:
                self.episodes.close(key)
            # Detector order puts resource reservations before dependent messages.
            for candidate in result.candidates:
                if candidate.asset not in self._allowed_ids:
                    self.rejections.append({"event_id": event.event_id, "reason": "Unknown candidate asset"})
                    continue
                if self.episodes.emitted(candidate.key, candidate.action, candidate.variant):
                    continue
                if candidate.context.get("after_action") and (candidate.key, candidate.context["after_action"]) not in accepted_actions:
                    continue
                episode = self.episodes.episode(candidate.key, now)
                decision = candidate.record(now, episode)
                context = {**candidate.context, "building": self.building, "calendar": self.calendar, "max_age_minutes": self.config.telemetry_max_age_minutes}
                try:
                    validate_safety(decision, context)
                    audit = self.executor.execute(decision, context)
                except SafetyViolation as exc:
                    self.rejections.append({"event_id": event.event_id, "action": candidate.action, "reason": str(exc)})
                    continue
                self.episodes.mark(candidate.key, candidate.action, candidate.variant)
                accepted_actions.add((candidate.key, candidate.action))
                if candidate.resource and audit["status"] == "simulated":
                    self.turnaround.scheduled_jobs[candidate.key] = {
                        "room_id": candidate.asset, "resource_id": candidate.resource, "start_utc": context["start_utc"],
                        "end_utc": context["end_utc"], "decision_id": decision.event_id,
                    }
                records.append(decision)
        return sorted(records, key=lambda r: r.event_id)

    def run(self) -> list[DecisionRecordV2]:
        records = []
        for event in self.repo.iter_events():
            records.extend(self.process(event))
        return records
