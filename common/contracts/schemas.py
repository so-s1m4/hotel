"""PLATO-40 v2: team internal event contract and official decision output contract.

Internal NormalizedEventV1 is TEAM-DESIGNED and is NOT the organizer's output format.
DecisionRecordV2 follows the supplied DECISION_CONTRACT.md.
Python 3.11+, pydantic >= 2.  No API calls or file I/O at import time.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SCHEMA_VERSION = "1.0"


def _validate_utc_z(value: str) -> str:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError("Timestamp must be ISO 8601 UTC ending in Z")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except (TypeError, ValueError) as exc:
        raise ValueError("Invalid ISO 8601 UTC timestamp") from exc
    if parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ValueError("Timestamp must be UTC")
    return value


class SourceRefV1(BaseModel):
    """Stable pointer to an actual source row, never an LLM-generated identifier."""
    model_config = ConfigDict(extra="forbid")
    file: str = Field(min_length=1)
    record_id: str = Field(min_length=1)
    timestamp_utc: str | None = None

    @field_validator("timestamp_utc")
    @classmethod
    def valid_timestamp(cls, value: str | None) -> str | None:
        return _validate_utc_z(value) if value is not None else None


class EventEntitiesV1(BaseModel):
    model_config = ConfigDict(extra="forbid")
    room_id: str | None = None  # Can be BUILDING (from guest_requests.csv)
    booking_id: str | None = None
    guest_request_id: str | None = None
    asset_id: str | None = None
    resource_id: str | None = None


class GuestIntentV1(BaseModel):
    """LLM extracts semantics. Dataset's request_type is always kept separately."""
    model_config = ConfigDict(extra="forbid")
    intent: Literal[
        "early_checkin", "late_checkout", "waiting_for_room", "arrival_delay",
        "temperature_hot", "temperature_cold", "water_issue", "air_quality",
        "cleaning_request", "maintenance_issue", "access_issue", "noise_complaint",
        "extra_towels", "wifi_help", "luggage_storage", "parking", "invoice", "unknown"
    ]
    requested_time_utc: str | None = None
    requested_time_text: str | None = None
    evidence_quote: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)
    needs_review: bool = False

    @field_validator("requested_time_utc")
    @classmethod
    def valid_utc(cls, value: str | None) -> str | None:
        return _validate_utc_z(value) if value is not None else None


class ExtractionMetaV1(BaseModel):
    model_config = ConfigDict(extra="forbid")
    method: Literal["llm", "source_label", "fallback", "none"]
    model_name: str | None = None
    needs_review: bool = False
    uncertain_fields: list[str] = Field(default_factory=list)


class NormalizedEventV1(BaseModel):
    """Team-internal event envelope; use it in ingestion->engine handoff."""
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["1.0"] = "1.0"
    event_id: str = Field(min_length=1)
    timestamp_utc: str
    available_at_utc: str
    event_type: Literal[
        "guest_request", "operational_event", "bms_alarm", "booking_created",
        "roster_update", "work_order_created", "clock_tick", "unknown"
    ]
    source: SourceRefV1
    entities: EventEntitiesV1
    priority_hint: Literal["critical", "high", "medium", "low"] | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    extraction: ExtractionMetaV1 | None = None

    @field_validator("timestamp_utc", "available_at_utc")
    @classmethod
    def valid_utc(cls, value: str) -> str:
        return _validate_utc_z(value)

    @model_validator(mode="after")
    def check_guest_identity(self) -> "NormalizedEventV1":
        if self.event_type == "guest_request":
            if not self.entities.guest_request_id:
                raise ValueError("guest_request event needs entities.guest_request_id")
            if "request_type" not in self.payload or "original_text" not in self.payload:
                raise ValueError("guest_request payload needs request_type and original_text")
            if "intents" not in self.payload or not isinstance(self.payload["intents"], list):
                raise ValueError("guest_request payload needs intents array")
            for intent in self.payload["intents"]:
                GuestIntentV1.model_validate(intent)
        return self


class DecisionRecordV2(BaseModel):
    """Output specified by organizers (dataset DECISION_CONTRACT.md)."""
    model_config = ConfigDict(extra="forbid")
    event_id: str = Field(min_length=1)  # Unique DECISION ID, not source event ID
    timestamp_utc: str
    affected_assets: list[str] = Field(min_length=1)
    issue_type: str = Field(min_length=1)
    action: str = Field(min_length=1)
    priority: Literal["critical", "high", "medium", "low"]
    reason: str = Field(min_length=1)
    requires_human_approval: bool
    autonomy: Literal["autonomous", "human_approval", "approval_and_consent"] | None = None
    episode_id: str | None = None
    evidence: list[str] = Field(default_factory=list)
    expected_outcome: str | None = None
    verification_plan: str | None = None
    dependencies: list[str] = Field(default_factory=list)
    guest_request_ids: list[str] = Field(default_factory=list)
    requires_guest_consent: bool = False
    resource_id: str | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)

    @field_validator("timestamp_utc")
    @classmethod
    def valid_timestamp(cls, value: str) -> str:
        return _validate_utc_z(value)

    @field_validator("affected_assets")
    @classmethod
    def unique_assets(cls, value: list[str]) -> list[str]:
        if any(not v.strip() for v in value):
            raise ValueError("affected_assets cannot include blank IDs")
        return value

    @model_validator(mode="after")
    def check_approval_levels(self) -> "DecisionRecordV2":
        if self.requires_guest_consent and not self.requires_human_approval:
            raise ValueError("Guest consent also requires human approval")
        if self.autonomy is not None:
            expected = (
                "approval_and_consent" if self.requires_guest_consent else
                "human_approval" if self.requires_human_approval else "autonomous"
            )
            if self.autonomy != expected:
                raise ValueError(f"autonomy must be {expected} for the approval flags")
        return self
