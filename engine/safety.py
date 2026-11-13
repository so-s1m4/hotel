"""Fail-closed safety checks shared by the planner and the mock executor."""
from datetime import timedelta
from math import sqrt, isclose

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


def _validate_safety(decision: DecisionRecordV2, context: dict) -> None:
    """Validate proposal, not grant approval. Missing action preconditions fail closed."""
    def require(condition, message):
        if not condition:
            raise SafetyViolation(message)

    try:
        DecisionRecordV2.model_validate(decision.model_dump(warnings=False), strict=True)
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
    affected_rooms = rooms & set(decision.affected_assets)
    room_action = bool(affected_rooms)
    room_controls = {"release_room", "schedule_cleaning", "adjust_room_setpoint"}
    if decision.action in room_controls:
        require(len(decision.affected_assets) == 1 and len(affected_rooms) == 1,
                "Room controls require exactly one affected room")
    if room_action and decision.action in {"dispatch_robot", "request_inspection", "dispatch_technician"}:
        require(len(decision.affected_assets) == 1 and len(affected_rooms) == 1,
                "A room intervention requires exactly one affected room")
    if decision.action in {"schedule_cleaning", "dispatch_robot"}:
        require(bool(decision.resource_id), "A verified resource assignment is required")
    if room_action and decision.action in {"release_room", "schedule_cleaning", "dispatch_robot", "adjust_room_setpoint", "request_inspection", "dispatch_technician"}:
        require(room.get("room_id") in decision.affected_assets, "Room preconditions do not match affected asset")
    stamp = room.get("last_observed_utc")
    if stamp:
        require(instant(stamp) <= instant(decision.timestamp_utc), "Future room observation")
    human_needed = decision.action in HUMAN_ACTIONS or context.get("notable_cost", False) or decision.resource_id == "ONCALL_1"
    # Entry is a property of a room intervention, not an override controlled by a detector.
    mandatory_entry = room_action and decision.action in {"schedule_cleaning", "request_inspection", "dispatch_robot", "dispatch_technician"}
    entry = mandatory_entry or context.get("entry_required") is True
    consent_needed = bool(context.get("water_shutoff") or context.get("safety_critical") or (entry and occupancy != "vacant"))
    require(not human_needed or decision.requires_human_approval, "Human approval is mandatory")
    require(not consent_needed or (decision.requires_human_approval and decision.requires_guest_consent), "Human approval and guest consent are mandatory for entry/safety interventions")
    # Never infer that flags are permissions. Executor checks grants separately.
    if room_action and decision.action in {"release_room", "schedule_cleaning", "dispatch_robot", "adjust_room_setpoint", "request_inspection", "dispatch_technician"}:
        require(stamp is not None and
                instant(stamp) >= instant(decision.timestamp_utc) - timedelta(minutes=context.get("max_age_minutes", 30)),
                "Fresh room observations are required")
    if decision.action == "release_room":
        require(occupancy == "vacant" and room.get("housekeeping_state") == "clean", "Room must be observed vacant and clean")
        require(room.get("blocked") is False, "No block must be explicitly verified")
        require(room.get("inspection_required") is False or room.get("inspection_passed") is True, "Inspection requirement must be resolved")
        require(not decision.resource_id or not decision.resource_id.startswith("ROBOT"), "Robots cannot certify release")
    if room_action and decision.action in {"schedule_cleaning", "dispatch_robot"}:
        require(occupancy == "vacant", "Do not schedule cleaning/robots into occupied or unknown rooms")
    if decision.action == "adjust_room_setpoint":
        target = number(context.get("setpoint_c"))
        require(target is not None, "A numerical setpoint is required")
        low, high = building["constraints"]["occupied_comfort_c"]
        require(occupancy in {"occupied", "vacant"}, "Occupancy must be verified before setpoint control")
        require((low <= target <= high) if occupancy == "occupied" else (18 <= target <= high), "Setpoint violates comfort/safe setback limits")
    if decision.action == "optimize_energy":
        battery = context.get("battery", {})
        catalog = {a["id"]: a for a in building["building"].get("assets", [])}
        require(decision.affected_assets == ["BAT1"] and battery == catalog.get("BAT1"), "Use actual battery capabilities")
        row = context.get("observed_energy", {})
        power = number(context.get("battery_power_kw"))
        duration = number(context.get("duration_minutes"))
        soc = number(row.get("battery_soc_kwh"))
        require(row.get("timestamp_utc") is not None and instant(decision.timestamp_utc) == instant(row["timestamp_utc"]), "Fresh observed battery state is required")
        require(power is not None and duration is not None and 0 < duration <= 15 and soc is not None, "Invalid battery action parameters")
        eta = number(battery.get("round_trip_efficiency"))
        bounds = battery.get("soc_bounds_kwh", [])
        charge, discharge = number(battery.get("max_charge_kw")), number(battery.get("max_discharge_kw"))
        require(eta is not None and 0 < eta <= 1 and charge is not None and charge > 0 and discharge is not None and discharge > 0 and len(bounds) == 2 and 0 <= bounds[0] < bounds[1], "Battery limits are incomplete")
        require(-charge <= power <= discharge and bounds[0] <= soc <= bounds[1], "Battery power or initial SOC out of bounds")
        expected = soc - power*duration/60/sqrt(eta) if power >= 0 else soc - power*duration/60*sqrt(eta)
        supplied = number(context.get("expected_soc_kwh"))
        require(bounds[0]-1e-8 <= expected <= bounds[1]+1e-8 and supplied is not None and isclose(expected, supplied, abs_tol=1e-6), "Projected SOC violates bounds or accounting")
        fields = {k: number(row.get(k)) for k in ("building_load_kw", "hvac_electric_kw", "pv_kw", "battery_discharge_kw_signed", "grid_import_kw", "grid_export_kw")}
        require(all(v is not None for v in fields.values()), "Energy balance observations are missing")
        require(all(fields[k] >= 0 for k in ("building_load_kw", "hvac_electric_kw", "pv_kw", "grid_import_kw", "grid_export_kw")), "Invalid energy measurements")
        require(fields["hvac_electric_kw"] <= fields["building_load_kw"] + 0.1, "HVAC exceeds whole-building load")
        balance = fields["grid_import_kw"] + fields["pv_kw"] + fields["battery_discharge_kw_signed"] - fields["building_load_kw"] - fields["grid_export_kw"]
        require(abs(balance) <= max(1, fields["building_load_kw"]*0.05), "Observed energy balance is inconsistent")
        require(context.get("strategy") in {"pv_charge", "cheap_charge", "peak_discharge"}, "Unknown battery strategy")
        calendar = context.get("calendar")
        end = instant(decision.timestamp_utc) + timedelta(minutes=duration)
        require(calendar is not None and calendar.available("asset:BAT1", decision.timestamp_utc, end.isoformat().replace("+00:00", "Z"), decision.timestamp_utc, exclude_decision_id=decision.event_id), "Battery already has a dispatch for this interval")
    if decision.action == "dispatch_robot":
        require(context.get("task") in {"floor_cleaning", "carry_bin"}, "Robot capability exceeded")
        require((context.get("task") == "floor_cleaning" and room_action)
                or (context.get("task") == "carry_bin" and not room_action),
                "Robot task must match its room/bin asset")
        if not room_action:
            require(context.get("task") == "carry_bin" and context.get("common_area_route") is True, "Only verified common-area bin routes are supported")
            source, destination = context.get("source_bin", {}), context.get("destination_bin", {})
            catalog = {a["id"]: a for a in building["building"].get("assets", [])}
            require(source == catalog.get(source.get("id")) and destination == catalog.get(destination.get("id")) and decision.affected_assets == [source.get("id")] and source.get("id") != destination.get("id"), "Unknown bin capabilities")
            src, dst = context.get("source_observation", {}), context.get("destination_observation", {})
            for obs, asset in ((src, source), (dst, destination)):
                require(obs.get("asset_id") == asset.get("id") and obs.get("metric") == "fill_pct", "Bin observation mismatch")
                require(obs.get("timestamp_utc") is not None and instant(decision.timestamp_utc)-timedelta(minutes=60) <= instant(obs["timestamp_utc"]) <= instant(decision.timestamp_utc), "Stale bin observation")
                value = number(obs.get("value"))
                require(value is not None and 0 <= value <= 100, "Invalid bin fill")
            volume, capacity = number(source.get("volume_l")), number(destination.get("volume_l"))
            transfer, limit_pct = number(context.get("transfer_liters")), number(context.get("container_max_fill_pct"))
            require(volume is not None and volume > 0 and capacity is not None and capacity > 0 and transfer is not None and transfer > 0 and limit_pct is not None and 0 < limit_pct <= 100, "Bin capacities missing")
            require(isclose(transfer, volume*float(src["value"])/100, abs_tol=1e-6), "Incorrect transfer volume")
            calendar = context.get("calendar")
            require(calendar is not None and calendar.capacity_available(destination["id"], capacity*float(dst["value"])/100, capacity*limit_pct/100, transfer), "Central container capacity exceeded")
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
                    local_start >= local_start.replace(hour=15, minute=30, second=0, microsecond=0) and
                    local_end <= local_start.replace(hour=22, minute=0, second=0, microsecond=0), "Outside on-call window")
            require(instant(start) >= instant(decision.timestamp_utc) + timedelta(minutes=45), "On-call needs 45-minute travel")
        else:
            slot = context.get("slot", {})
            require(slot.get("resource_id") == decision.resource_id and scheduled(slot, decision.timestamp_utc), "Resource absent or availability unknown")
            require(slot.get("role") == resources[decision.resource_id], "Roster and resource role disagree")
            require(instant(slot["start_utc"]) <= instant(start) and instant(end) <= instant(slot["end_utc"]), "Reservation outside actual shift")
            if decision.action in {"schedule_cleaning", "request_inspection"}:
                require(resources[decision.resource_id] == "cleaner", "Only human cleaners may certify housekeeping")
            if decision.action == "dispatch_technician":
                require(resources[decision.resource_id] == "technician", "Technician role required")
            if decision.action == "dispatch_robot":
                require(resources[decision.resource_id] == "robot", "Robot action requires robot resource")
        calendar = context.get("calendar")
        require(calendar is not None and calendar.available(decision.resource_id, start, end, decision.timestamp_utc, exclude_decision_id=decision.event_id), "Resource already reserved or calendar missing")
        if room_action and entry:
            require(calendar.room_available(room["room_id"], start, end, decision.timestamp_utc, exclude_decision_id=decision.event_id), "Room already reserved for another job")


def validate_safety(decision: DecisionRecordV2, context: dict) -> None:
    """Turn malformed/incomplete context into a rejected proposal, never a pipeline crash."""
    try:
        _validate_safety(decision, context)
    except SafetyViolation:
        raise
    except (KeyError, TypeError, ValueError, OverflowError, AttributeError) as exc:
        raise SafetyViolation(f"Invalid safety context: {exc}") from exc
