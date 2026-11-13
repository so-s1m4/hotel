"""Internal engine types. The frozen common contracts remain unchanged."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from functools import lru_cache
import math
from zoneinfo import ZoneInfo

from common.contracts.schemas import DecisionRecordV2

VIENNA = ZoneInfo("Europe/Vienna")


@lru_cache(maxsize=65536)
def instant(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ValueError(f"Expected UTC timestamp: {value}")
    return parsed


def utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="auto").replace("+00:00", "Z")


def ago(now: str, minutes: int) -> str:
    return utc(instant(now) - timedelta(minutes=minutes))


def number(value) -> float | None:
    if value is None or isinstance(value, bool) or value == "":
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (ValueError, TypeError):
        return None


def stable_id(prefix: str, *parts: str) -> str:
    return prefix + ":" + sha256("\x00".join(parts).encode()).hexdigest()[:20]


def observations(repo, filename: str, start: str, now: str,
                 entity: str | None = None, metric: str | None = None) -> list[dict]:
    """Bound again at the engine boundary, deduplicate without filling gaps."""
    rows = repo.get_measurements(filename, start, now, now, entity_id=entity, metric=metric)
    unique = {}
    start_at, end_at = instant(start), instant(now)
    for row in rows:
        t = row.get("timestamp_utc")
        if not t or not start_at <= instant(t) <= end_at:
            continue
        if entity and entity not in (row.get("room_id"), row.get("asset_id")):
            continue
        if metric and row.get("metric") != metric:
            continue
        key = (t, row.get("room_id"), row.get("asset_id"), row.get("metric"))
        # Conflicting duplicates are unknown, never select whichever supports an alarm.
        if key in unique and unique[key] != row:
            unique[key] = {"timestamp_utc": t, "room_id": row.get("room_id"),
                           "asset_id": row.get("asset_id"), "metric": row.get("metric")}
        else:
            unique.setdefault(key, dict(row))
    return sorted(unique.values(), key=lambda r: instant(r["timestamp_utc"]))


def room_state(repo, room: str, now: str) -> dict:
    state = repo.get_room_state(room, now) or {}
    stamp = state.get("last_observed_utc")
    if stamp and instant(stamp) > instant(now):
        return {"room_id": room, "occupancy": "unknown"}
    return state


@dataclass(frozen=True)
class EngineConfig:
    """Conservative prototype thresholds; not calibrated performance estimates."""
    cleaning_minutes: int = 45
    inspection_minutes: int = 15
    telemetry_max_age_minutes: int = 30
    water_window_minutes: int = 60
    water_min_l_per_interval: float = 3.0
    water_low_window_minutes: int = 180
    water_weak_persistent_window_minutes: int = 240
    water_weak_max_backstep_l: float = 0.5
    water_low_min_l_per_interval: float = 1.0
    water_low_baseline_min_intervals: int = 12
    water_max_l_per_interval: float = 100.0
    main_residual_min_l_per_interval: float = 120.0
    cold_window_minutes: int = 30
    comfort_recovery_margin_c: float = 0.25
    cold_forecast_c: float = 0.0
    low_cop: float = 1.7
    filter_dp_pa: float = 250
    ahu_flow_fraction: float = 0.75
    technician_minutes: int = 60
    robot_minutes: int = 20
    surge_arrival_fraction: float = 0.4
    surge_occupancy_fraction: float = 0.8
    waste_trigger_pct: float = 80
    container_max_fill_pct: float = 95
    vending_low_fraction: float = 0.2
    battery_price_spread: float = 0.08

    def __post_init__(self):
        for name, value in self.__dict__.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)) or number(value) is None:
                raise ValueError(f"{name} must be a finite number")
        for name in ("cleaning_minutes", "inspection_minutes", "telemetry_max_age_minutes", "water_window_minutes", "water_low_window_minutes", "water_weak_persistent_window_minutes", "water_low_baseline_min_intervals", "cold_window_minutes", "technician_minutes", "robot_minutes"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if self.water_window_minutes < 60 or self.water_window_minutes % 15:
            raise ValueError("water_window_minutes must be at least 60 and a multiple of 15")
        if self.water_low_window_minutes < 180 or self.water_low_window_minutes % 15 or self.water_low_baseline_min_intervals < 12:
            raise ValueError("Low-rate water review requires at least 3 h and 12 independent baseline intervals")
        if self.water_weak_persistent_window_minutes not in {240, 300}:
            raise ValueError("Persistent weak-flow review requires four or five complete hours within the local night")
        if not 0 <= self.water_weak_max_backstep_l <= 1:
            raise ValueError("Weak-flow small-backstep allowance must be within 0..1 L")
        if not 0 < self.water_low_min_l_per_interval < self.water_min_l_per_interval:
            raise ValueError("Low-rate floor must be positive and below the sustained-flow threshold")
        if not 0 <= self.comfort_recovery_margin_c < 1.5:
            raise ValueError("Comfort recovery margin must fit within half the comfort band")
        if self.cold_window_minutes < 30 or self.cold_window_minutes % 15:
            raise ValueError("cold_window_minutes must be at least 30 and a multiple of 15")
        if not 0 < self.water_min_l_per_interval < self.water_max_l_per_interval <= 2000:
            raise ValueError("Invalid room-water thresholds")
        if self.main_residual_min_l_per_interval <= 0 or self.low_cop <= 0:
            raise ValueError("Residual and COP thresholds must be positive")
        if self.filter_dp_pa <= 0 or not 0 < self.ahu_flow_fraction < 1:
            raise ValueError("Invalid AHU thresholds")
        if not 0 < self.surge_arrival_fraction <= 1 or not 0 < self.surge_occupancy_fraction <= 1 or not 0 < self.vending_low_fraction < 1:
            raise ValueError("Invalid demand fractions")
        if not 0 < self.waste_trigger_pct < self.container_max_fill_pct <= 100:
            raise ValueError("Invalid waste thresholds")
        if self.battery_price_spread <= 0:
            raise ValueError("Battery spread must be positive")


@dataclass
class Candidate:
    key: str
    asset: str
    issue: str
    action: str
    reason: str
    evidence: list[str]
    verification: str
    outcome: str
    priority: str = "medium"
    human: bool = False
    consent: bool = False
    resource: str | None = None
    request_ids: list[str] = field(default_factory=list)
    context: dict = field(default_factory=dict)
    variant: str = "initial"

    def record(self, now: str, episode: str) -> DecisionRecordV2:
        return DecisionRecordV2(
            event_id=stable_id("decision", episode, self.action, self.variant),
            timestamp_utc=now, affected_assets=[self.asset], issue_type=self.issue,
            action=self.action, priority=self.priority, reason=self.reason,
            requires_human_approval=self.human, requires_guest_consent=self.consent,
            autonomy="approval_and_consent" if self.consent else "human_approval" if self.human else "autonomous",
            episode_id=episode, evidence=sorted(set(self.evidence)),
            expected_outcome="SIMULATED proposal: " + self.outcome,
            verification_plan=self.verification, resource_id=self.resource,
            guest_request_ids=self.request_ids,
        )


@dataclass
class DetectionResult:
    candidates: list[Candidate] = field(default_factory=list)
    resolved: set[str] = field(default_factory=set)
