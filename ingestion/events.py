"""NormalizedEventV1 emitters.

Streams (all deterministic except the optional LLM intent extraction):
  guest_requests.csv -> guest_request        id guest:<request_id>
  events.jsonl       -> operational_event /
                        bms_alarm            id raw:<event_id>
  bookings.csv       -> booking_created      id booking:<booking_id>   (at created_utc)
  staff_roster.csv   -> roster_update        id roster:<shift_id>:<status>
  work_orders.csv    -> work_order_created   id wo:<id>                (at created_utc)
                        operational_event    id wo:<id>:resolved       (at completion)
  generated          -> clock_tick           id tick:<UTC timestamp>   (every interval)
Sorted by (available_at_utc, event_id); ids are unique.
"""
from __future__ import annotations

import logging
import os
from datetime import timedelta
from typing import TYPE_CHECKING, Any, Iterator

from common.contracts.schemas import (EventEntitiesV1, ExtractionMetaV1,
                                      GuestIntentV1, NormalizedEventV1, SourceRefV1)

from .loader import fmt_ts, norm_ts, parse_ts, shift_ts

if TYPE_CHECKING:  # pragma: no cover
    from .replay import ReplayData

log = logging.getLogger("ingestion")

CLOCK_SOURCE = "__simulation_clock__"
KNOWN_INTENTS = set(GuestIntentV1.model_fields["intent"].annotation.__args__)
PRIORITIES = {"critical", "high", "medium", "low"}


def iter_normalized_events(repo: "ReplayData") -> Iterator[NormalizedEventV1]:
    """Public entry point: same stream as ``repo.iter_events()``."""
    return repo.iter_events()


# ------------------------------------------------------------ guest requests
def source_label_intents(record: dict, method: str = "source_label",
                         model_name: str | None = None
                         ) -> tuple[list[dict[str, Any]], ExtractionMetaV1]:
    """No-model extraction: the dataset label only, no invented time or details."""
    label = record.get("request_type")
    known = label in KNOWN_INTENTS
    intent = GuestIntentV1(intent=label if known else "unknown", needs_review=True,
                           details={"source": "request_type"})
    uncertain = ["requested_time_utc", "details"] + ([] if known else ["intent"])
    meta = ExtractionMetaV1(method=method, model_name=model_name, needs_review=True,
                            uncertain_fields=uncertain)
    return [intent.model_dump()], meta


def _load_dotenv(path: str = ".env") -> None:
    """Minimal KEY=VALUE loader; never overrides variables that are already set."""
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key, value = key.strip(), value.strip().strip("'\"")
                if key and value and key not in os.environ:
                    os.environ[key] = value
    except OSError:
        pass


def _parse_guest_requests(repo: "ReplayData", records: list[dict]
                          ) -> list[tuple[list[dict], ExtractionMetaV1]]:
    contexts = []
    for rec in records:
        ts = norm_ts(rec["timestamp_utc"])
        booking = repo.get_booking(rec["booking_id"], ts) if rec.get("booking_id") else None
        contexts.append({
            "booking": booking, "timezone": repo.ds.local_tz_name,
            "message_local_date": parse_ts(ts).astimezone(repo.tz).date().isoformat(),
            "as_of_utc": ts, "use_llm": repo.use_llm,
        })
    try:
        from . import guest_parser
    except Exception as exc:  # parser module missing/broken must not stop replay
        log.warning("guest_parser unavailable (%s); using source labels", exc)
        return [source_label_intents(r) for r in records]
    results: list[Any] | None = None
    try:
        if repo.use_llm is not False:
            _load_dotenv()
        if hasattr(guest_parser, "parse_requests_full_batch"):
            results = list(guest_parser.parse_requests_full_batch(records, contexts))
        elif repo.use_llm is False or len(records) < 2:
            results = [guest_parser.parse_request_full(r, c) for r, c in zip(records, contexts)]
        else:  # network-bound model calls: a small pool; results keep input order
            from concurrent.futures import ThreadPoolExecutor
            workers = max(1, min(16, int(os.environ.get("GUEST_PARSER_WORKERS", "6"))))
            with ThreadPoolExecutor(max_workers=workers) as pool:
                results = list(pool.map(guest_parser.parse_request_full, records, contexts))
    except Exception as exc:
        log.warning("guest parser failed (%s); using source labels", exc)
    out = []
    for i, rec in enumerate(records):
        try:
            intents, meta = results[i]  # type: ignore[index]
            intents = [GuestIntentV1.model_validate(
                x.model_dump() if isinstance(x, GuestIntentV1) else x).model_dump()
                for x in intents]
            meta = ExtractionMetaV1.model_validate(
                meta.model_dump() if isinstance(meta, ExtractionMetaV1) else meta)
            text = rec.get("text") or ""
            for intent in intents:  # evidence must be verbatim source text
                quote = intent.get("evidence_quote")
                if quote is not None and quote not in text:
                    intent["evidence_quote"] = None
                    intent["needs_review"] = True
            if not intents:
                raise ValueError("no intents")
            out.append((intents, meta))
        except Exception:
            out.append(source_label_intents(rec, method="fallback"))
    return out


def _guest_events(repo: "ReplayData") -> list[NormalizedEventV1]:
    records = [r for r in repo.ds.guest_requests if r.get("timestamp_utc")]
    parsed = _parse_guest_requests(repo, records)
    out = []
    for rec, (intents, meta) in zip(records, parsed):
        ts = norm_ts(rec["timestamp_utc"])
        label = rec.get("request_type")
        extracted = {i["intent"] for i in intents}
        if meta.method == "llm" and label in KNOWN_INTENTS and label not in extracted:
            meta = meta.model_copy(update={
                "needs_review": True,
                "uncertain_fields": sorted(set(meta.uncertain_fields) | {"intent"})})
        payload = {
            "request_type": label, "original_text": rec.get("text") or "",
            "language": rec.get("language"), "channel": rec.get("channel"),
            "response_due_utc": rec.get("response_due_utc"),
            "requires_guest_consent": rec.get("requires_guest_consent"),
            "source_priority": rec.get("priority"),
            "intents": intents,
        }
        room = rec.get("room_id")
        if room is None and rec.get("booking_id"):
            # Blank room cell: use the room the booking was KNOWN to hold at this
            # instant (causal view, reassignments up to ts). Marked as derived.
            booking = repo.get_booking(rec["booking_id"], ts)
            if booking is not None and booking.get("observed_room_id"):
                room = booking["observed_room_id"]
                payload["room_id_source"] = "derived_from_booking_asof"
        out.append(NormalizedEventV1(
            event_id=f"guest:{rec['request_id']}", timestamp_utc=ts, available_at_utc=ts,
            event_type="guest_request",
            source=SourceRefV1(file="guest_requests.csv", record_id=rec["request_id"],
                               timestamp_utc=ts),
            entities=EventEntitiesV1(room_id=room,
                                     booking_id=rec.get("booking_id"),
                                     guest_request_id=rec["request_id"]),
            priority_hint=rec.get("priority") if rec.get("priority") in PRIORITIES else None,
            payload=payload, extraction=meta))
    return out


# ------------------------------------------------------- deterministic streams
RESERVED_PAYLOAD_KEYS = ("intents", "request_type", "original_text")


def _raw_payload(event: dict) -> dict:
    """events.jsonl fields are kept, but keys that consumers read as guest-request
    semantics are moved under "source_fields" so a raw record cannot pose as one."""
    clash = {k: event.pop(k) for k in RESERVED_PAYLOAD_KEYS if k in event}
    if clash:
        existing = event.get("source_fields")
        if "source_fields" in event and not isinstance(existing, dict):
            clash["source_fields"] = existing
            existing = None
        event["source_fields"] = {**(existing or {}), **clash}
    return event

def _raw_events(repo: "ReplayData") -> list[NormalizedEventV1]:
    from .replay import public_event
    rooms = set(repo.ds.room_ids)
    out = []
    for ev in repo.ds.raw_events:
        occurred = ev.get("_source_ts") or ev["timestamp_utc"]
        ts = ev.get("_available_ts", ev["timestamp_utc"])  # publication grid, never before occurrence
        asset = ev.get("asset_id")
        room = ev.get("room_id") or ev.get("to_room")
        if room is None and isinstance(asset, str):
            if asset in rooms:
                room = asset
            elif asset.startswith("WM_") and asset[3:] in rooms:
                room = asset[3:]
        out.append(NormalizedEventV1(
            event_id=f"raw:{ev['event_id']}", timestamp_utc=occurred, available_at_utc=ts,
            event_type="bms_alarm" if ev.get("type") == "bms_alarm" else "operational_event",
            source=SourceRefV1(file="events.jsonl", record_id=str(ev["event_id"]),
                               timestamp_utc=occurred),
            entities=EventEntitiesV1(room_id=room, booking_id=ev.get("booking_id"),
                                     asset_id=asset, resource_id=ev.get("resource_id")),
            payload=_raw_payload(public_event(ev))))
    return out


def _booking_events(repo: "ReplayData") -> list[NormalizedEventV1]:
    out = []
    for row in repo.ds.bookings.values():
        if not row.get("created_utc"):
            continue
        ts = norm_ts(row["created_utc"])
        payload = {k: v for k, v in row.items() if k != "status"}
        payload["status"] = "unresolved"  # final status has no timestamp: withheld
        out.append(NormalizedEventV1(
            event_id=f"booking:{row['booking_id']}", timestamp_utc=ts, available_at_utc=ts,
            event_type="booking_created",
            source=SourceRefV1(file="bookings.csv", record_id=row["booking_id"],
                               timestamp_utc=ts),
            entities=EventEntitiesV1(room_id=row.get("room_id"), booking_id=row["booking_id"]),
            payload=payload))
    return out


def _roster_events(repo: "ReplayData") -> list[NormalizedEventV1]:
    from .replay import ROSTER_PUBLICATION_LEAD_DAYS
    out = []

    def emit(row: dict, status: str, ts: str, assumed: bool) -> None:
        payload = dict(row)
        payload["status"] = status
        payload["status_known_utc"] = ts
        payload["effective_status_asof"] = status
        # NB: no marker distinguishing these reconstructed plans from ordinary
        # scheduled rows - that would reveal the later absence.
        out.append(NormalizedEventV1(
            event_id=f"roster:{row['shift_id']}:{status}", timestamp_utc=ts,
            available_at_utc=ts, event_type="roster_update",
            source=SourceRefV1(file="staff_roster.csv", record_id=row["shift_id"],
                               timestamp_utc=ts),
            entities=EventEntitiesV1(resource_id=row.get("resource_id")),
            payload=payload))

    for row in repo.ds.roster:
        status, known, start = row.get("status"), row.get("status_known_utc"), row.get("start_utc")
        if not known or not status:
            continue
        known = norm_ts(known)
        if status != "scheduled" and start:
            published = shift_ts(start, days=-ROSTER_PUBLICATION_LEAD_DAYS)
            if published < known:
                emit(row, "scheduled", published, assumed=True)
        emit(row, status, known, assumed=False)
    return out


def _work_order_events(repo: "ReplayData") -> list[NormalizedEventV1]:
    from .replay import work_order_resolved_at, work_order_view
    rooms = set(repo.ds.room_ids)
    out = []
    for row in repo.ds.work_orders:
        if not row.get("created_utc"):
            continue
        ts = norm_ts(row["created_utc"])
        asset = row.get("asset_id")
        room = asset if asset in rooms else (
            asset[3:] if isinstance(asset, str) and asset.startswith("WM_") and asset[3:] in rooms
            else None)
        entities = EventEntitiesV1(room_id=room, asset_id=asset,
                                   resource_id=row.get("assigned_to"))
        out.append(NormalizedEventV1(
            event_id=f"wo:{row['work_order_id']}", timestamp_utc=ts, available_at_utc=ts,
            event_type="work_order_created",
            source=SourceRefV1(file="work_orders.csv", record_id=row["work_order_id"],
                               timestamp_utc=ts),
            entities=entities, payload=work_order_view(row, ts)))
        done = work_order_resolved_at(row)
        if done:
            done = norm_ts(done)
            payload = work_order_view(row, done)
            payload["type_of_update"] = "work_order_" + (row.get("status") or "resolved")
            payload["work_order_type"] = row.get("type")
            payload["type"] = "work_order_" + (row.get("status") or "resolved")
            out.append(NormalizedEventV1(
                event_id=f"wo:{row['work_order_id']}:resolved", timestamp_utc=done,
                available_at_utc=done, event_type="operational_event",
                source=SourceRefV1(file="work_orders.csv", record_id=row["work_order_id"],
                                   timestamp_utc=done),
                entities=entities, payload=payload))
    return out


def _tick_events(repo: "ReplayData") -> list[NormalizedEventV1]:
    manifest = repo.ds.manifest
    start, end = manifest.get("start_utc"), manifest.get("end_utc")
    step = repo.ds.interval_seconds
    if not start or not end:  # fall back to the observed range of a dense series
        try:
            ts_list = next(iter(repo.ds.series("water_main.csv").groups.values()))[0]
            start, end = ts_list[0], shift_ts(ts_list[-1], seconds=step)
        except Exception:
            log.warning("no manifest time range and no water_main.csv: no clock ticks")
            return []
    cur, stop = parse_ts(start), parse_ts(end)
    delta = timedelta(seconds=step)
    empty = EventEntitiesV1()
    out = []
    while cur < stop:
        ts = fmt_ts(cur)
        out.append(NormalizedEventV1(
            event_id=f"tick:{ts}", timestamp_utc=ts, available_at_utc=ts,
            event_type="clock_tick",
            source=SourceRefV1(file=CLOCK_SOURCE, record_id=f"tick:{ts}", timestamp_utc=ts),
            entities=empty,
            payload={"generated": True, "interval_seconds": step,
                     "local_time": cur.astimezone(repo.tz).isoformat()}))
        cur += delta
    return out


def build_normalized_events(repo: "ReplayData") -> list[NormalizedEventV1]:
    events: list[NormalizedEventV1] = []
    events += _guest_events(repo)
    events += _raw_events(repo)
    events += _booking_events(repo)
    events += _roster_events(repo)
    events += _work_order_events(repo)
    if repo.include_ticks:
        events += _tick_events(repo)
    unique: dict[str, NormalizedEventV1] = {}
    for ev in events:
        if ev.event_id in unique:
            repo.ds.audit["events:duplicate_ids_dropped"] = (
                repo.ds.audit.get("events:duplicate_ids_dropped", 0) + 1)
            continue
        unique[ev.event_id] = ev
    return sorted(unique.values(), key=lambda e: (e.available_at_utc, e.event_id))
