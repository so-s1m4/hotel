"""Fail-closed safety checks shared by the planner and the mock executor."""
from datetime import timedelta

from common.contracts.schemas import DecisionRecordV2
from engine.core import instant, number, VIENNA
from engine.resources import scheduled

ACTIONS = {
    "schedule_cleaning", "dispatch_robot", "adjust_room_setpoint", "optimize_energy",
    "block_room", "release_room", "reassign_room", "request_inspection",
    "dispatch_technician", "order_service", "notify_guest", "flag_anomaly",
    "defer_action", "request_human_approval",
}
HUMAN_ACTIONS = {"dispatch_technician", "order_service", "block_room", "reassign_room", "request_human_approval"}


class SafetyViolation(ValueError):
    pass


def asset_ids(building: dict) -> set[str]:
    details = building["building"]
    result = {"BUILDING", "MAIN_ENTRANCE"}
    for section in ("rooms", "assets", "sensors", "gateways"):
        result.update(row["id"] for row in details.get(section, []))
    result.update(r["resource_id"] for r in building.get("staff", {}).get("resources", []))
    return result


def validate_safety(decision: DecisionRecordV2, context: dict) -> None:
    """Validate proposal, not grant approval. Missing action preconditions fail closed."""
    def require(condition, message):
        if not condition:
            raise SafetyViolation(message)

    try:
        DecisionRecordV2.model_validate(decision.model_dump())
    except ValueError as exc:
        raise SafetyViolation(f"Invalid decision contract: {exc}") from exc
    building = context.get("building")
    require(bool(building), "Building configuration is required")
    require(decision.action in ACTIONS, "Unknown action")
    require(set(decision.affected_assets) <= asset_ids(building), "Unknown affected asset")
    require(bool(decision.evidence) and bool(decision.verification_plan), "Evidence and verification are required")
    require(bool(decision.expected_outcome) and decision.expected_outcome.startswith("SIMULATED"), "Label simulation explicitly")
    room = context.get("room", {})
    occupancy = room.get("occupancy", "unknown")
    rooms = {r["id"] for r in building["building"]["rooms"]}
    room_action = bool(rooms & set(decision.affected_assets))
    if room_action and decision.action in {"release_room", "schedule_cleaning", "dispatch_robot", "adjust_room_setpoint", "request_inspection"}:
        require(room.get("room_id") in decision.affected_assets, "Room preconditions do not match affected asset")
    stamp = room.get("last_observed_utc")
    if stamp:
        require(instant(stamp) <= instant(decision.timestamp_utc), "Future room observation")
    human_needed = decision.action in HUMAN_ACTIONS or context.get("notable_cost", False) or decision.resource_id == "ONCALL_1"
    entry = context.get("entry_required", room_action and decision.action in {"schedule_cleaning", "request_inspection", "dispatch_robot"})
    consent_needed = bool(context.get("water_shutoff") or context.get("safety_critical") or (entry and occupancy != "vacant"))
    require(not human_needed or decision.requires_human_approval, "Human approval is mandatory")
    require(not consent_needed or (decision.requires_human_approval and decision.requires_guest_consent), "Human approval and guest consent are mandatory for entry/safety interventions")
    # Never infer that flags are permissions. Executor checks grants separately.
    if room_action and decision.action in {"release_room", "schedule_cleaning", "dispatch_robot", "adjust_room_setpoint", "request_inspection"}:
        require(stamp is not None and
                instant(stamp) >= instant(decision.timestamp_utc) - timedelta(minutes=context.get("max_age_minutes", 30)),
                "Fresh room observations are required")
    if decision.action == "release_room":
        require(occupancy == "vacant" and room.get("housekeeping_state") == "clean", "Room must be observed vacant and clean")
        require(room.get("blocked") is False, "No block must be explicitly verified")
        require(room.get("inspection_required") is False or room.get("inspection_passed") is True, "Inspection requirement must be resolved")
        require(not decision.resource_id or not decision.resource_id.startswith("ROBOT"), "Robots cannot certify release")
    if decision.action in {"schedule_cleaning", "dispatch_robot"}:
        require(occupancy == "vacant", "Do not schedule cleaning/robots into occupied or unknown rooms")
    if decision.action == "adjust_room_setpoint":
        target = number(context.get("setpoint_c"))
        require(target is not None, "A numerical setpoint is required")
        low, high = building["constraints"]["occupied_comfort_c"]
        require(occupancy in {"occupied", "vacant"}, "Occupancy must be verified before setpoint control")
        require((low <= target <= high) if occupancy == "occupied" else (18 <= target <= high), "Setpoint violates comfort/safe setback limits")
    if decision.action == "optimize_energy":
        # The mandatory scenarios do not implement a battery/load optimiser.
        require(False, "Energy optimisation has no implemented constraint solver")
    if decision.action == "dispatch_robot":
        require(context.get("task") in {"floor_cleaning", "carry_bin"}, "Robot capability exceeded")
    if decision.resource_id:
        resources = {r["resource_id"]: r["role"] for r in building.get("staff", {}).get("resources", [])}
        require(decision.resource_id in resources, "Unknown resource")
        start, end = context.get("start_utc"), context.get("end_utc")
        require(start is not None and end is not None, "Resource reservation interval is required")
        require(instant(decision.timestamp_utc) <= instant(start) < instant(end), "Invalid reservation interval")
        if decision.resource_id == "ONCALL_1":
            require(context.get("oncall") and context.get("waiting_guest"), "On-call is only for waiting guests")
            local_start, local_end = instant(start).astimezone(VIENNA), instant(end).astimezone(VIENNA)
            require(local_start.date() == local_end.date() and
                    (local_start.hour, local_start.minute) >= (15, 30) and
                    (local_end.hour, local_end.minute) <= (22, 0), "Outside on-call window")
            require(instant(start) >= instant(decision.timestamp_utc) + timedelta(minutes=45), "On-call needs 45-minute travel")
        else:
            slot = context.get("slot", {})
            require(slot.get("resource_id") == decision.resource_id and scheduled(slot, decision.timestamp_utc), "Resource absent or availability unknown")
            require(instant(slot["start_utc"]) <= instant(start) and instant(end) <= instant(slot["end_utc"]), "Reservation outside actual shift")
            if decision.action in {"schedule_cleaning", "request_inspection"}:
                require(resources[decision.resource_id] == "cleaner", "Only human cleaners may certify housekeeping")
            if decision.action == "dispatch_robot":
                require(resources[decision.resource_id] == "robot", "Robot action requires robot resource")
        calendar = context.get("calendar")
        require(calendar is not None and calendar.available(decision.resource_id, start, end, decision.timestamp_utc), "Resource already reserved or calendar missing")
        if room_action and entry:
            require(calendar.room_available(room["room_id"], start, end, decision.timestamp_utc), "Room already reserved for another job")
