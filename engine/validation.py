"""Independent file-level consistency audit; no detector or planner imports.

This checks provenance, simulation boundaries and scheduling consistency. It does
not estimate detection precision/recall or certify the policy's real-world safety.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import csv
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sys

from common.contracts.schemas import DecisionRecordV2

VALIDATOR_VERSION = "1.0"
START_ROLES = {"cleaning_started": "cleaner", "robot_floor_cleaning_started": "robot"}
END_ROLES = {"cleaning_completed": "cleaner", "robot_floor_cleaning_completed": "robot"}
ALLOWED_STATUSES = {"simulated", "simulated_message_proposed", "pending_human_approval", "pending_guest_consent"}


def _time(value):
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError("UTC timestamp must be a string ending in Z")
    parsed = datetime.fromisoformat(value[:-1]+"+00:00")
    if parsed.utcoffset() != timedelta(0):
        raise ValueError("UTC timestamp required")
    return parsed


def _stamp(value):
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _role(role):
    return "cleaner" if role == "on_call_housekeeping" else role


class _Issues:
    def __init__(self):
        self.counts = Counter()
        self.examples = []

    def add(self, code, *, decision_id=None, **details):
        self.counts[code] += 1
        if len(self.examples) < 2000:
            self.examples.append({"code": code, **({"decision_id": decision_id} if decision_id else {}), **details})


def _jsonl(path, issues, label):
    result = []
    with path.open(encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                issues.add(label+"_blank_line", line=line_number)
                continue
            try:
                item = json.loads(line)
                if not isinstance(item, dict):
                    raise ValueError("Expected a JSON object")
                result.append(item)
            except (ValueError, TypeError) as exc:
                issues.add(label+"_json", line=line_number, detail=str(exc))
    return result


def _csv(path):
    if not path.is_file():
        return []
    with path.open(encoding="utf-8", newline="") as stream:
        return list(csv.DictReader(stream))


def _hash(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _match_end(start, end, resources):
    if start["occurrence"] > end["_occurrence"]:
        return False
    if start["role"] != END_ROLES.get(end.get("type")):
        return False
    room = end.get("room_id")
    if room is not None and start["room"] != room:
        return False
    performer = end.get("resource_id")
    known = performer if performer in resources else None
    if known is not None and start["resource"] is not None and start["resource"] != known:
        return False
    if known is None and room is None:
        return False
    return True


def _related(activity, job, roles):
    resource_match = job["resource"] == activity["resource"] and activity["resource"] is not None
    uncertain_role = activity["resource"] is None and _role(roles.get(job["resource"])) == activity["role"]
    room_match = job["room"] is not None and activity["room"] == job["room"]
    return resource_match or uncertain_role or room_match


def _source_activities(raw_events, roles, issues):
    events = []
    for index, event in enumerate(raw_events):
        if event.get("type") not in START_ROLES and event.get("type") not in END_ROLES:
            continue
        try:
            occurrence = _time(event.get("timestamp_utc"))
            available = _time(event.get("available_at_utc", event.get("timestamp_utc")))
        except (ValueError, TypeError) as exc:
            issues.add("source_activity_timestamp", source_event=event.get("event_id"), detail=str(exc))
            continue
        event = dict(event)
        event["_known"] = max(occurrence, available)
        event["_occurrence"] = occurrence
        event["_index"] = index
        events.append(event)
    return sorted(events, key=lambda e: (e["_known"], e["_occurrence"], str(e.get("event_id", ""))))


def _busy_checks(jobs, raw_events, roles, issues):
    sources = _source_activities(raw_events, roles, issues)
    active = {}
    cursor = 0
    ties = set()
    conflicts = set()
    known_ends = []

    def apply(event, state, remember_end=True):
        kind = event["type"]
        if kind in START_ROLES:
            key = event.get("event_id") or "source-line:"+str(event["_index"])
            resource = event.get("resource_id")
            start = {"event_id": key, "resource": resource if resource in roles else None,
                          "source_performer": resource, "room": event.get("room_id"),
                          "role": START_ROLES[kind], "known": event["_known"], "occurrence": event["_occurrence"]}
            # A delayed start can arrive after its matching completion was
            # already known. Do not resurrect an already-completed old job.
            if event["_known"] > event["_occurrence"] and any(_match_end(start, end, roles) for end in known_ends):
                return
            state[key] = start
        else:
            if remember_end:
                known_ends.append(event)
            for key, start in list(state.items()):
                if _match_end(start, event, roles):
                    state.pop(key)

    for job in sorted(jobs, key=lambda j: (j["decided"], j["id"])):
        when = job["decided"]
        while cursor < len(sources) and sources[cursor]["_known"] < when:
            apply(sources[cursor], active)
            cursor += 1
        equal = []
        pointer = cursor
        while pointer < len(sources) and sources[pointer]["_known"] == when:
            equal.append(sources[pointer])
            pointer += 1
        # A completion at exactly this instant may have been consumed first.
        # Count the tie; do not manufacture a strictly-prior conflict from it.
        known_busy = dict(active)
        for event in equal:
            if event["type"] in END_ROLES:
                for start in known_busy.values():
                    if _match_end(start, event, roles) and _related(start, job, roles):
                        ties.add((job["id"], event.get("event_id", event["_index"])))
                apply(event, known_busy, remember_end=False)
            else:
                activity = {"resource": event.get("resource_id") if event.get("resource_id") in roles else None,
                            "room": event.get("room_id"), "role": START_ROLES[event["type"]]}
                if _related(activity, job, roles):
                    ties.add((job["id"], event.get("event_id", event["_index"])))
        for activity in known_busy.values():
            if _related(activity, job, roles):
                pair = (job["id"], activity["event_id"])
                if pair not in conflicts:
                    conflicts.add(pair)
                    issues.add("observed_work_known_busy", decision_id=job["id"],
                               observed_event=activity["event_id"], resource_id=job["resource"], room_id=job["room"],
                               known_since=_stamp(activity["known"]), proposed_start=_stamp(job["start"]),
                               detail="Observed work was already known and had no known matching completion at decision time")
    return len(ties)


def _overlaps(jobs, field, issues):
    groups = defaultdict(list)
    for job in jobs:
        if job[field] is not None:
            groups[job[field]].append(job)
    pairs = 0
    for key, entries in groups.items():
        active = []
        for job in sorted(entries, key=lambda j: (j["start"], j["end"], j["id"])):
            active = [old for old in active if old["end"] > job["start"]]
            for old in active:
                if old["id"] == job["id"]:
                    continue
                pairs += 1
                issues.add("live_"+field+"_overlap", decision_id=job["id"], other_decision_id=old["id"],
                           **{field+"_id": key}, overlap_start=_stamp(job["start"]),
                           overlap_end=_stamp(min(job["end"], old["end"])))
            active.append(job)
    return pairs


def _notification_scope(record, decisions, requests, events_by_id, bookings, lifecycle, seen=None):
    seen = set() if seen is None else seen
    if record.event_id in seen:
        return None
    seen.add(record.event_id)
    ids = list(record.guest_request_ids)
    primary = sorted({requests[r].get("booking_id") for r in ids if r in requests and requests[r].get("booking_id")})
    if primary:
        return "booking:"+",".join(primary)
    refs = record.evidence
    for ref in refs:
        if ref.startswith("guest_requests.csv:"):
            ids.append(ref[len("guest_requests.csv:"):].split(":", 1)[0].split(" ", 1)[0])
    direct = sorted({requests[r].get("booking_id") for r in ids if r in requests and requests[r].get("booking_id")})
    if direct:
        return "booking:"+",".join(direct)
    relevant = [events_by_id.get(ref[len("events.jsonl:"):].split(":", 1)[0])
                for ref in refs if ref.startswith("events.jsonl:")]
    waiting = sorted({e.get("booking_id") for e in relevant if e and e.get("type") == "arrival_waiting_room" and e.get("booking_id")})
    if waiting:
        return "booking:"+",".join(waiting)
    # Corrections follow the cancellation -> original proposal chain, not the
    # unrelated source event that happened to trigger resource revalidation.
    for identifier in record.dependencies:
        if identifier in decisions:
            scope = _notification_scope(decisions[identifier], decisions, requests, events_by_id, bookings, lifecycle, seen)
            if scope and not scope.startswith("unknown_stay:"):
                return scope
    explicit = sorted({ref[len("bookings.csv:"):].split(":", 1)[0].split(" ", 1)[0]
                       for ref in refs if ref.startswith("bookings.csv:")
                       and ref[len("bookings.csv:"):].split(":", 1)[0].split(" ", 1)[0] in bookings})
    if len(explicit) == 1:
        return "booking:"+explicit[0]
    guest = sorted({e.get("booking_id") for e in relevant if e and e.get("type") in {"checkin", "checkin_early", "checkin_after_wait", "stay_in_progress"} and e.get("booking_id")})
    if len(guest) == 1:
        return "booking:"+guest[0]
    room = record.affected_assets[0]
    prior = [e for e in lifecycle.get(room, []) if _time(e["timestamp_utc"]) <= _time(record.timestamp_utc)]
    if prior:
        last = prior[-1]
        if last.get("booking_id") and last.get("type") not in {"checkout", "checkout_early", "no_show"}:
            return "booking:"+last["booking_id"]
        return "unknown_stay:"+room+":"+str(last.get("event_id", last["timestamp_utc"]))
    return "unknown_stay:"+room+":initial"


def validate_outputs(data_dir: str | Path, decisions_path: str | Path, audit_path: str | Path) -> dict:
    root, decisions_path, audit_path = Path(data_dir), Path(decisions_path), Path(audit_path)
    issues = _Issues()
    building = json.loads((root/"building.json").read_text(encoding="utf-8"))
    manifest = json.loads((root/"manifest.json").read_text(encoding="utf-8")) if (root/"manifest.json").is_file() else {}
    rooms = {r["id"] for r in building["building"].get("rooms", [])}
    roles = {r["resource_id"]: r["role"] for r in building.get("staff", {}).get("resources", [])}
    asset_ids = {"BUILDING", "MAIN_ENTRANCE", *rooms, *roles}
    for section in ("assets", "sensors", "gateways"):
        asset_ids.update(r["id"] for r in building["building"].get(section, []))
    raw_decisions = _jsonl(decisions_path, issues, "decision")
    raw_audits = _jsonl(audit_path, issues, "audit")
    raw_events = _jsonl(root/"events.jsonl", issues, "source_event")
    checked_events = []
    for index, event in enumerate(raw_events, 1):
        invalid = [key for key in ("event_id", "type", "timestamp_utc") if not isinstance(event.get(key), str)]
        invalid += [key for key in ("room_id", "resource_id", "booking_id", "from_room", "to_room")
                    if event.get(key) is not None and not isinstance(event[key], str)]
        try:
            _time(event.get("timestamp_utc"))
            if "available_at_utc" in event:
                _time(event["available_at_utc"])
        except (ValueError, TypeError):
            invalid.append("timestamp")
        if invalid:
            issues.add("source_event_schema", line=index, fields=invalid)
        else:
            checked_events.append(event)
    raw_events = checked_events
    requests = {r["request_id"]: r for r in _csv(root/"guest_requests.csv") if r.get("request_id")}
    bookings = {r["booking_id"]: r for r in _csv(root/"bookings.csv") if r.get("booking_id")}
    events_by_id = {r["event_id"]: r for r in raw_events if r.get("event_id")}
    lifecycle = defaultdict(list)
    for event in raw_events:
        if event.get("room_id") and event.get("timestamp_utc") and event.get("type") in {
                "checkin", "checkin_early", "checkin_after_wait", "stay_in_progress", "checkout", "checkout_early", "no_show"}:
            try:
                _time(event["timestamp_utc"])
                lifecycle[event["room_id"]].append(event)
            except ValueError:
                issues.add("source_lifecycle_timestamp", source_event=event.get("event_id"))
    for entries in lifecycle.values():
        entries.sort(key=lambda e: (_time(e["timestamp_utc"]), str(e.get("event_id", ""))))
    decisions = {}
    previous = None
    for index, raw in enumerate(raw_decisions, 1):
        if not isinstance(raw.get("requires_human_approval"), bool) or ("requires_guest_consent" in raw and not isinstance(raw["requires_guest_consent"], bool)):
            issues.add("decision_boolean_type", decision_id=raw.get("event_id"), line=index)
        try:
            record = DecisionRecordV2.model_validate(raw)
            order = (_time(record.timestamp_utc), record.event_id)
        except (ValueError, TypeError) as exc:
            issues.add("decision_schema", decision_id=raw.get("event_id"), line=index, detail=str(exc))
            continue
        if previous and order < previous:
            issues.add("decision_order", decision_id=record.event_id)
        previous = order
        if record.event_id in decisions:
            issues.add("duplicate_decision_id", decision_id=record.event_id)
            continue
        decisions[record.event_id] = record
        unknown = set(record.affected_assets)-asset_ids
        if unknown:
            issues.add("unknown_affected_asset", decision_id=record.event_id, assets=sorted(unknown))
        if record.resource_id and record.resource_id not in roles:
            issues.add("unknown_resource", decision_id=record.event_id, resource_id=record.resource_id)
        allowed_role = {"schedule_cleaning": "cleaner", "dispatch_robot": "robot", "dispatch_technician": "technician"}.get(record.action)
        if record.resource_id in roles and allowed_role and _role(roles[record.resource_id]) != allowed_role:
            issues.add("resource_capability_mismatch", decision_id=record.event_id, resource_id=record.resource_id, action=record.action)
        if record.action == "release_room" and record.resource_id and roles.get(record.resource_id) == "robot":
            issues.add("robot_cannot_certify_release", decision_id=record.event_id)
        if record.event_id in events_by_id or record.event_id.removeprefix("raw:") in events_by_id:
            issues.add("decision_id_reuses_source_event", decision_id=record.event_id)
        for request_id in record.guest_request_ids:
            if request_id not in requests:
                issues.add("unknown_guest_request", decision_id=record.event_id, guest_request_id=request_id)
    joined = defaultdict(list)
    for audit in raw_audits:
        identifier = audit.get("decision_id")
        if not isinstance(identifier, str):
            issues.add("audit_missing_decision_id")
            continue
        joined[identifier].append(audit)
        if identifier not in decisions:
            issues.add("audit_without_decision", decision_id=identifier)
    audits = {}
    for identifier, record in decisions.items():
        matches = joined.get(identifier, [])
        if len(matches) != 1:
            issues.add("audit_join_cardinality", decision_id=identifier, audit_rows=len(matches))
            continue
        audit = matches[0]
        audits[identifier] = audit
        try:
            same_time = _time(audit.get("timestamp_utc")) == _time(record.timestamp_utc)
        except (ValueError, TypeError):
            same_time = False
        if not same_time or audit.get("action") != record.action:
            issues.add("audit_action_or_timestamp_mismatch", decision_id=identifier)
        if audit.get("simulated") is not True or audit.get("effects_applied") is not False or audit.get("observed_history_changed") is not False:
            issues.add("audit_effect_boundary", decision_id=identifier)
        status = audit.get("status")
        if not isinstance(status, str) or status not in ALLOWED_STATUSES:
            issues.add("audit_status", decision_id=identifier, status=status)
        if record.requires_human_approval:
            if status != "pending_human_approval":
                issues.add("required_approval_not_pending", decision_id=identifier, status=status)
        elif record.requires_guest_consent:
            issues.add("consent_without_human_approval", decision_id=identifier)
        elif isinstance(status, str) and status.startswith("pending_"):
            issues.add("unexpected_pending_status", decision_id=identifier)
        if record.action == "notify_guest" and status != "simulated_message_proposed":
            issues.add("notification_not_proposed_only", decision_id=identifier, status=status)
        parameters = audit.get("parameters")
        if not isinstance(parameters, dict):
            issues.add("audit_parameters", decision_id=identifier)
            continue
        for flag in ("entry_required", "notable_cost", "oncall", "waiting_guest", "water_shutoff", "safety_critical", "common_area_route"):
            if flag in parameters and not isinstance(parameters[flag], bool):
                issues.add("audit_parameter_boolean_type", decision_id=identifier, parameter=flag)
        needs_human = record.action in {"dispatch_technician", "block_room", "reassign_room", "request_human_approval"} or parameters.get("notable_cost") is True or record.resource_id == "ONCALL_1"
        if needs_human and not record.requires_human_approval:
            issues.add("missing_required_human_flag", decision_id=identifier)
        if (parameters.get("water_shutoff") is True or parameters.get("safety_critical") is True) and not (record.requires_human_approval and record.requires_guest_consent):
            issues.add("missing_safety_consent_flags", decision_id=identifier)
    cancellations = {}
    for identifier, audit in audits.items():
        params = audit.get("parameters")
        if not isinstance(params, dict) or "cancelled_decision_id" not in params:
            continue
        target = params["cancelled_decision_id"]
        record = decisions[identifier]
        if not isinstance(target, str) or target not in decisions or record.action != "defer_action" or target not in record.dependencies:
            issues.add("cancellation_structure", decision_id=identifier, cancelled_decision_id=target)
            continue
        when = _time(record.timestamp_utc)
        if when < _time(decisions[target].timestamp_utc):
            issues.add("cancellation_before_proposal", decision_id=identifier, cancelled_decision_id=target)
            continue
        cancellations[target] = min(cancellations.get(target, when), when)
    jobs = []
    cancelled_before_start = 0
    for identifier, audit in audits.items():
        record = decisions[identifier]
        params = audit.get("parameters")
        if not isinstance(params, dict):
            continue
        resource = record.resource_id
        try:
            if resource:
                start, end = _time(params.get("start_utc")), _time(params.get("end_utc"))
            elif record.action == "optimize_energy":
                duration = params.get("duration_minutes")
                if isinstance(duration, bool) or not isinstance(duration, (int, float)) or not 0 < duration <= 15:
                    raise ValueError("Invalid bounded control duration")
                resource = "asset:BAT1"
                start = _time(record.timestamp_utc)
                end = start+timedelta(minutes=duration)
            else:
                continue
            if not _time(record.timestamp_utc) <= start < end:
                raise ValueError("Task must be a nonempty interval starting no earlier than decision")
        except (ValueError, TypeError) as exc:
            issues.add("job_interval", decision_id=identifier, detail=str(exc))
            continue
        effective_end = min(end, cancellations.get(identifier, end))
        if effective_end <= start:
            cancelled_before_start += 1
            continue
        room_assets = rooms & set(record.affected_assets)
        implicit_entry = bool(room_assets and record.action in {"schedule_cleaning", "dispatch_robot", "request_inspection", "dispatch_technician"})
        if implicit_entry and params.get("entry_required") is not True:
            issues.add("missing_job_entry_marker", decision_id=identifier)
        room = params.get("room_id") if params.get("entry_required") is True or implicit_entry else None
        if implicit_entry and room is None:
            issues.add("missing_job_room", decision_id=identifier)
            room = next(iter(room_assets)) if len(room_assets) == 1 else None
        if room is not None and room not in record.affected_assets:
            issues.add("job_room_asset_mismatch", decision_id=identifier, room_id=room)
        if room is not None and room not in rooms:
            issues.add("job_unknown_room", decision_id=identifier, room_id=room)
        jobs.append({"id": identifier, "resource": resource, "room": room,
                     "start": start, "end": effective_end, "decided": _time(record.timestamp_utc),
                     "pending": str(audit.get("status", "")).startswith("pending_")})
    resource_pairs = _overlaps(jobs, "resource", issues)
    room_pairs = _overlaps(jobs, "room", issues)
    equal_ties = _busy_checks(jobs, raw_events, roles, issues)
    notification_groups = defaultdict(list)
    for record in decisions.values():
        if record.action == "notify_guest":
            scope = _notification_scope(record, decisions, requests, events_by_id, bookings, lifecycle)
            room = tuple(sorted(r for r in record.affected_assets if r in rooms)) or tuple(record.affected_assets)
            recipients = ["booking:"+identifier for identifier in scope[len("booking:"):].split(",")] if scope and scope.startswith("booking:") else [scope]
            for recipient in recipients:
                notification_groups[(recipient, room, _time(record.timestamp_utc), record.reason)].append(record.event_id)
    duplicate_notifications = 0
    for (scope, room, when, reason), ids in notification_groups.items():
        if len(ids) > 1:
            duplicate_notifications += len(ids)-1
            issues.add("duplicate_exact_notification", booking_or_stay=scope, rooms=list(room), timestamp_utc=_stamp(when), decision_ids=ids, reason=reason)
    input_paths = {"decisions": decisions_path, "audit": audit_path,
                   "building.json": root/"building.json", "events.jsonl": root/"events.jsonl"}
    for name in ("manifest.json", "guest_requests.csv", "bookings.csv", "staff_roster.csv"):
        if (root/name).is_file():
            input_paths[name] = root/name
    return {"validator_version": VALIDATOR_VERSION, "valid": not issues.counts,
            "scope": "File-level consistency/scheduling audit only; no ground-truth precision, recall, savings or real-world safety certification.",
            "dataset": {k: manifest.get(k) for k in ("profile", "start_utc", "end_utc")},
            "hashes_sha256": {name: _hash(path) for name, path in input_paths.items()},
            "issue_counts": dict(sorted(issues.counts.items())), "error_count": sum(issues.counts.values()),
            "warnings": [{"code": "observed_equal_time_ties", "count": equal_ties,
                          "detail": "Equal-time source/decision ordering is ambiguous; these are counted separately, not reported as strictly-prior conflicts."}] if equal_ties else [],
            "issues": issues.examples, "examples_truncated": sum(issues.counts.values()) > len(issues.examples),
            "stats": {"decision_rows": len(raw_decisions), "valid_unique_decisions": len(decisions), "audit_rows": len(raw_audits),
                      "joined_decisions": len(audits), "cancellation_targets": len(cancellations),
                      "jobs_cancelled_before_start": cancelled_before_start, "live_intervals": len(jobs),
                      "live_pending_intervals": sum(j["pending"] for j in jobs),
                      "resource_overlap_pairs": resource_pairs, "room_overlap_pairs": room_pairs,
                      "observed_equal_time_ties": equal_ties, "duplicate_exact_notifications": duplicate_notifications}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--decisions", type=Path, required=True)
    parser.add_argument("--audit", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if args.out.resolve() in {args.decisions.resolve(), args.audit.resolve()} or args.out.resolve().is_relative_to(args.data_dir.resolve()):
        parser.error("Validation report must not overwrite source inputs")
    try:
        result = validate_outputs(args.data_dir, args.decisions, args.audit)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        result = {"validator_version": VALIDATOR_VERSION, "valid": False, "error_count": 1,
                  "issue_counts": {"validation_input_error": 1}, "issues": [{"code": "validation_input_error", "detail": str(exc)}]}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    print(json.dumps({"valid": result["valid"], "error_count": result["error_count"], "out": str(args.out)}))
    return 0 if result["valid"] else 1


if __name__ == "__main__":
    sys.exit(main())
