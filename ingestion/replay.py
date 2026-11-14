"""ReplayData: time-causal implementation of common.contracts.api.ReplayDataGateway.

Every getter takes ``as_of_utc`` and returns only what the hotel systems could
have known at that instant. Returned dicts are fresh copies.
"""
import os
from copy import deepcopy
import re
from bisect import bisect_left, bisect_right
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterator
from zoneinfo import ZoneInfo

from common.contracts.schemas import NormalizedEventV1

from .loader import Dataset, fmt_ts, norm_ts, parse_ts, shift_ts

ROSTER_PUBLICATION_LEAD_DAYS = 14  # observed: every 'scheduled' row is known start-14d
CHECKIN_TYPES = {"checkin", "checkin_early", "checkin_after_wait", "stay_in_progress"}
CHECKOUT_TYPES = {"checkout", "checkout_early"}
ROOM_STATE_FIELDS = ("temperature_c", "setpoint_c", "humidity_pct", "co2_ppm",
                     "presence", "window_open", "housekeeping_state")
EFFECTIVE_STATUSES = {"scheduled", "sick", "vacation", "out_of_service"}


def _occurrence_order(event: dict) -> tuple[datetime, str]:
    # Ceil-to-second availability can tie two different source instants. Keep
    # original chronology for lifecycle inference; equal instants use stable IDs.
    return parse_ts(event.get("_source_ts") or event["timestamp_utc"]), event["event_id"]


class ReplayData:
    """Historical, leak-free view of one dataset directory.

    ``use_llm``: None = decide from environment (INGESTION_USE_LLM / API key
    presence), False = never call a model, True = try the configured model.
    ``include_ticks``: emit the generated 15-minute ``clock_tick`` events.
    """

    def __init__(self, data_dir: str | Path, *, use_llm: bool | None = None,
                 include_ticks: bool = True):
        self.data_dir = Path(data_dir)
        self.ds = Dataset(self.data_dir)
        self.tz = ZoneInfo(self.ds.local_tz_name)
        self.use_llm = use_llm
        self.include_ticks = include_ticks
        self._events_cache: list[NormalizedEventV1] | None = None
        self._idx: dict[str, Any] = {}

    # ------------------------------------------------------------ indexes
    def _event_index(self) -> dict[str, Any]:
        if "events" not in self._idx:
            events = sorted(self.ds.raw_events, key=_occurrence_order)
            by_room: dict[str, list[dict]] = {}
            by_booking: dict[str, list[dict]] = {}
            available = []
            for ev in events:
                available.append((parse_ts(ev.get("_available_ts", ev["timestamp_utc"])), ev))
                rooms = {ev.get("room_id"), ev.get("from_room"), ev.get("to_room")}
                asset = ev.get("asset_id")
                if isinstance(asset, str):
                    rooms.add(asset)
                    if asset.startswith("WM_R"):
                        rooms.add(asset[3:])
                for room in rooms:
                    if isinstance(room, str):
                        by_room.setdefault(room, []).append(ev)
                if ev.get("booking_id"):
                    by_booking.setdefault(ev["booking_id"], []).append(ev)
            available.sort(key=lambda item: (item[0], item[1]["event_id"]))
            self._idx["events"] = {
                "available": [event for _, event in available],
                "available_ts": [stamp for stamp, _ in available],
                "all": events, "all_ts": [e["timestamp_utc"] for e in events],
                "by_room": by_room,
                "by_room_ts": {r: [e["timestamp_utc"] for e in v] for r, v in by_room.items()},
                "by_booking": by_booking,
            }
        return self._idx["events"]

    def _bookings_by_room(self) -> dict[str, list[dict]]:
        if "bk_room" not in self._idx:
            out: dict[str, list[dict]] = {}
            for row in self.ds.bookings.values():
                if row.get("room_id"):
                    out.setdefault(row["room_id"], []).append(row)
            # bookings moved INTO a room by an observed room_reassigned event
            for ev in self.ds.raw_events:
                if ev.get("type") == "room_reassigned" and ev.get("to_room"):
                    row = self.ds.bookings.get(ev.get("booking_id"))
                    if row is not None and row not in out.setdefault(ev["to_room"], []):
                        out[ev["to_room"]].append(row)
            self._idx["bk_room"] = out
        return self._idx["bk_room"]

    # ------------------------------------------------------------- events
    def iter_events(self) -> Iterator[NormalizedEventV1]:
        """Independent normalized snapshots sorted by (available_at_utc, event_id)."""
        if self._events_cache is None:
            from .events import build_normalized_events
            self._events_cache = build_normalized_events(self)
        return (event.model_copy(deep=True) for event in self._events_cache)

    def get_events(self, start_utc: str, end_utc: str, as_of_utc: str,
                   room_id: str | None = None,
                   event_type: str | None = None) -> list[dict[str, Any]]:
        """Raw events.jsonl records with start <= timestamp <= min(end, as_of)."""
        as_of = norm_ts(as_of_utc)
        start, end = norm_ts(start_utc), min(norm_ts(end_utc), as_of)
        idx = self._event_index()
        if room_id is None:
            rows, ts = idx["all"], idx["all_ts"]
        else:
            rows, ts = idx["by_room"].get(room_id, []), idx["by_room_ts"].get(room_id, [])
        lo, hi = bisect_left(ts, start), bisect_right(ts, end)
        return [public_event(ev) for ev in rows[lo:hi]
                if ev.get("_available_ts", ev["timestamp_utc"]) <= as_of
                and (event_type is None or ev.get("type") == event_type)]

    def get_events_available_between(self, start_utc: str, end_utc: str,
                                     as_of_utc: str) -> list[dict[str, Any]]:
        """Optional incremental raw-fact query by inclusive availability window.

        Occurrence timestamps remain original source instants. Availability uses
        the loader's conservative whole-second grid, raised further by explicit
        publication timestamps. Precise query bounds are never rounded down at
        the start. The cached index gives O(log n + returned facts) per query.
        """
        start = parse_ts(start_utc)
        end = min(parse_ts(end_utc), parse_ts(as_of_utc))
        if start > end:
            return []
        idx = self._event_index()
        lo, hi = bisect_left(idx["available_ts"], start), bisect_right(idx["available_ts"], end)
        result = []
        for position in range(lo, hi):
            out = public_event(idx["available"][position])
            out["available_at_utc"] = fmt_ts(idx["available_ts"][position])
            result.append(out)
        return result

    # -------------------------------------------------------------- rooms
    def _occupancy(self, room_id: str, as_of: str) -> tuple[str, str | None, list[str]]:
        idx = self._event_index()
        rows = idx["by_room"].get(room_id, [])
        hi = bisect_right(idx["by_room_ts"].get(room_id, []), as_of)
        moved_out: dict[str, str] = {}  # booking_id -> evidence of a later move away
        for ev in reversed(rows[:hi]):
            if ev.get("_available_ts", ev["timestamp_utc"]) > as_of:
                continue
            kind = ev.get("type")
            occurred = ev.get("_source_ts") or ev["timestamp_utc"]
            ref = f"events.jsonl:{ev['event_id']}:{kind}:{occurred}"
            booking = ev.get("booking_id")
            if kind in CHECKIN_TYPES and ev.get("room_id") == room_id:
                if booking in moved_out:  # guest was moved to another room afterwards
                    return "vacant", None, [moved_out[booking], ref]
                return "occupied", booking, [ref]
            if kind in CHECKOUT_TYPES and ev.get("room_id") == room_id:
                return "vacant", None, [ref]
            if kind == "room_reassigned" and ev.get("from_room") == room_id and booking:
                moved_out.setdefault(booking, ref)  # keep looking further back
            elif kind == "room_reassigned" and ev.get("to_room") == room_id and booking:
                if booking in moved_out:
                    continue
                # an in-house guest moved INTO this room (check-in observed earlier)
                state = None
                for prior in idx["by_booking"].get(booking, []):
                    if _occurrence_order(prior) >= _occurrence_order(ev):
                        break
                    if prior.get("_available_ts", prior["timestamp_utc"]) > as_of:
                        continue
                    if prior.get("type") in CHECKIN_TYPES:
                        state = "in"
                    elif prior.get("type") in CHECKOUT_TYPES:
                        state = "out"
                if state == "in":
                    return "occupied", booking, [ref]
        # No observed lifecycle event for this room: absence of events does not
        # certify vacancy (the start snapshot is not guaranteed to be a full census).
        return "unknown", None, []

    def get_room_state(self, room_id: str, as_of_utc: str) -> dict[str, Any] | None:
        """Latest telemetry row <= as_of plus occupancy from guest lifecycle events."""
        if room_id not in self.ds.room_ids:
            return None
        as_of = norm_ts(as_of_utc)
        row = self.ds.series("room_telemetry.csv").latest(as_of, entity_id=room_id)
        occupancy, booking_id, evidence = self._occupancy(room_id, as_of)
        state: dict[str, Any] = {
            "room_id": room_id, "as_of_utc": as_of,
            "last_observed_utc": row["timestamp_utc"] if row else None,
        }
        for field in ROOM_STATE_FIELDS:
            state[field] = row.get(field) if row else None
        state["occupancy"] = occupancy
        state["current_booking_id"] = booking_id
        state["evidence"] = (
            [f"room_telemetry.csv:{room_id}:{row['timestamp_utc']}"] if row else []) + evidence
        return state

    # ----------------------------------------------------------- bookings
    def _booking_view(self, row: dict, as_of: str) -> dict[str, Any]:
        out = dict(row)
        # Final status ('cancelled') carries no timestamp -> never knowable as-of.
        out["status"] = "unresolved"
        out["status_note"] = ("bookings.csv final status withheld (no cancellation "
                              "timestamp); use observed_state from events.jsonl")
        observed, room, checkin, checkout = "not_arrived", row.get("room_id"), None, None
        refs: list[str] = []
        for ev in self._event_index()["by_booking"].get(row["booking_id"], []):
            if ev["timestamp_utc"] > as_of:
                break
            if ev.get("_available_ts", ev["timestamp_utc"]) > as_of:
                continue
            occurred = ev.get("_source_ts") or ev["timestamp_utc"]
            kind = ev.get("type")
            refs.append(f"events.jsonl:{ev['event_id']}:{kind}:{occurred}")
            if kind in CHECKIN_TYPES:
                observed, checkin = "checked_in", occurred
                room = ev.get("room_id") or room
            elif kind in CHECKOUT_TYPES:
                observed, checkout = "checked_out", occurred
            elif kind == "arrival_waiting_room":
                observed = "waiting_for_room"
            elif kind == "no_show":
                observed = "no_show"
            elif kind == "room_reassigned":
                room = ev.get("to_room") or room
        out.update({"observed_state": observed, "observed_room_id": room,
                    "observed_checkin_utc": checkin, "observed_checkout_utc": checkout,
                    "evidence": refs})
        return out

    def get_booking(self, booking_id: str, as_of_utc: str) -> dict[str, Any] | None:
        """Booking plan if created_utc <= as_of; final status is never leaked."""
        as_of = norm_ts(as_of_utc)
        row = self.ds.bookings.get(booking_id)
        if row is None or row.get("created_utc") is None or row["created_utc"] > as_of:
            return None
        return self._booking_view(row, as_of)

    def get_bookings_for_room(self, room_id: str, start_utc: str, end_utc: str,
                              as_of_utc: str) -> list[dict[str, Any]]:
        """Known bookings whose planned [arrival, departure] overlaps [start, end]."""
        start, end, as_of = norm_ts(start_utc), norm_ts(end_utc), norm_ts(as_of_utc)
        out = []
        for row in self._bookings_by_room().get(room_id, []):
            if row.get("created_utc") is None or row["created_utc"] > as_of:
                continue
            arr, dep = row.get("arrival_utc"), row.get("departure_utc")
            if arr is None or dep is None or arr > end or dep < start:
                continue
            view = self._booking_view(row, as_of)
            if view["observed_room_id"] != room_id and row.get("room_id") != room_id:
                continue  # reassignment into this room not yet observed
            out.append(view)
        out.sort(key=lambda b: (b["arrival_utc"], b["booking_id"]))
        return out

    # -------------------------------------------------------------- staff
    def _oncall_rule(self) -> tuple[tuple[int, int], tuple[int, int], int]:
        notes = ((self.ds.building.get("staff") or {}).get("notes") or {})
        text = notes.get("on_call_housekeeping") or ""
        window = re.search(r"(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})", text)
        lead = re.search(r"(\d+)\s*min", text)
        if window:
            h1, m1, h2, m2 = (int(x) for x in window.groups())
        else:
            h1, m1, h2, m2 = 15, 30, 22, 0
        return (h1, m1), (h2, m2), int(lead.group(1)) if lead else 45

    def _oncall_slots(self, start: str, end: str) -> list[dict[str, Any]]:
        resources = (self.ds.building.get("staff") or {}).get("resources") or []
        ids = [r["resource_id"] for r in resources if r.get("role") == "on_call_housekeeping"]
        if not ids:
            return []
        (h1, m1), (h2, m2), lead = self._oncall_rule()
        first = parse_ts(start).astimezone(self.tz).date() - timedelta(days=1)
        last = parse_ts(end).astimezone(self.tz).date()
        out = []
        day = first
        while day <= last:
            s = fmt_ts(datetime(day.year, day.month, day.day, h1, m1, tzinfo=self.tz))
            e = fmt_ts(datetime(day.year, day.month, day.day, h2, m2, tzinfo=self.tz))
            if s <= end and e >= start:
                for rid in ids:
                    out.append({
                        "shift_id": f"ONCALL:{rid}:{day.isoformat()}", "resource_id": rid,
                        "role": "on_call_housekeeping", "start_utc": s, "end_utc": e,
                        "status": "scheduled", "status_known_utc": None,
                        "effective_status_asof": "scheduled",
                        "simulated_assumption": True, "lead_time_min": lead,
                        "source": "building.json:staff.notes.on_call_housekeeping",
                        "note": "Derived from building.json rule, not from the roster; "
                                "actual availability is NOT observed. One room at a "
                                "time, costly, for waiting guests only.",
                    })
            day += timedelta(days=1)
        return out

    def _shift_view(self, row: dict, as_of: str) -> dict[str, Any] | None:
        status, known = row.get("status"), row.get("status_known_utc")
        start = row.get("start_utc")
        # A scheduled row without a recorded publication timestamp is not
        # verified capacity. Do not manufacture a timestamp from the usual
        # 14-day planning pattern; only absence updates use that overwrite rule.
        if status == "scheduled" and known is None:
            return None
        published = known
        if status != "scheduled" and start is not None:
            # absence rows overwrite a shift that was published like all the others
            lead = shift_ts(start, days=-ROSTER_PUBLICATION_LEAD_DAYS)
            published = min(known, lead) if known is not None else lead
        if published is not None and published > as_of:
            return None  # roster line not published yet
        out = dict(row)
        if status != "scheduled" and (known is None or known > as_of):
            out["status"] = "scheduled"          # absence not yet known: do not leak it
            out["status_known_utc"] = published  # same shape as any planned shift
            out["effective_status_asof"] = "scheduled"
        else:
            out["effective_status_asof"] = status if status in EFFECTIVE_STATUSES else "unknown"
        return out

    def get_staff_slots(self, start_utc: str, end_utc: str, as_of_utc: str,
                        role: str | None = None) -> list[dict[str, Any]]:
        """Shifts overlapping [start, end] as known at as_of (plus derived ONCALL_1)."""
        start, end, as_of = norm_ts(start_utc), norm_ts(end_utc), norm_ts(as_of_utc)
        out = []
        for row in self.ds.roster:
            if role is not None and row.get("role") != role:
                continue
            s, e = row.get("start_utc"), row.get("end_utc")
            if s is None or e is None or s > end or e < start:
                continue
            view = self._shift_view(row, as_of)
            if view is not None:
                out.append(view)
        if role in (None, "on_call_housekeeping"):
            out.extend(self._oncall_slots(start, end))
        out.sort(key=lambda r: (r["start_utc"], r["resource_id"], r["shift_id"]))
        return out

    def _tariff_index(self):
        """Static sorted target timestamps; availability is still checked per query."""
        if "tariff_targets" not in self._idx:
            rows = self.ds.tariffs  # sorted by (timestamp_utc, published_utc)
            self._idx["tariff_targets"] = (rows, [r["timestamp_utc"] for r in rows])
        return self._idx["tariff_targets"]

    def _forecast_index(self):
        """Targets are indexed once; issue timestamps are built lazily per target."""
        if "forecast_targets" not in self._idx:
            by_target = self.ds.forecasts
            self._idx["forecast_targets"] = (by_target, list(by_target), {})
        return self._idx["forecast_targets"]

    # ------------------------------------------------------- measurements
    def get_measurements(self, filename: str, start_utc: str, end_utc: str,
                         as_of_utc: str, entity_id: str | None = None,
                         metric: str | None = None) -> list[dict[str, Any]]:
        """Rows with start <= timestamp_utc <= min(end, as_of); blanks stay None."""
        filename = os.path.basename(filename)
        start, as_of = norm_ts(start_utc), norm_ts(as_of_utc)
        end = min(norm_ts(end_utc), as_of)
        if filename == "tariff.csv":  # price history; future hours stay hidden here
            return self._known_tariffs(start, end, as_of)
        series = self.ds.series(filename)
        if entity_id is not None and filename == "room_water.csv" and entity_id.startswith("WM_R"):
            entity_id = entity_id[3:]
        return series.query(start, end, entity_id=entity_id, metric=metric)

    def get_weather_forecast(self, as_of_utc: str, until_utc: str) -> list[dict[str, Any]]:
        """Per target hour in [hour(as_of), until]: latest forecast issued <= as_of."""
        as_of, until = norm_ts(as_of_utc), norm_ts(until_utc)
        first = as_of[:13] + ":00:00Z"
        by_target, targets, issue_index = self._forecast_index()
        lo, hi = bisect_left(targets, first), bisect_right(targets, until)
        out = []
        for target in targets[lo:hi]:
            rows = by_target[target]
            issued = issue_index.get(target)
            if issued is None:
                issued = issue_index[target] = [r["issued_utc"] for r in rows]
            known = bisect_right(issued, as_of)
            if known:
                out.append(dict(rows[known - 1]))
        return out

    def get_tariffs(self, as_of_utc: str, until_utc: str) -> list[dict[str, Any]]:
        """Hourly prices from hour(as_of) to until that were published <= as_of."""
        as_of, until = norm_ts(as_of_utc), norm_ts(until_utc)
        first = as_of[:13] + ":00:00Z"
        return self._known_tariffs(first, until, as_of)

    def _known_tariffs(self, first: str, last: str, as_of: str) -> list[dict[str, Any]]:
        """Per target hour the most recently published price known at as_of."""
        rows, targets = self._tariff_index()
        lo, hi = bisect_left(targets, first), bisect_right(targets, last)
        best: dict[str, dict] = {}
        for row in rows[lo:hi]:  # sorted by (timestamp, published): later revision wins
            if row.get("published_utc") is not None and row["published_utc"] <= as_of:
                best[row["timestamp_utc"]] = row
        return [dict(best[key]) for key in sorted(best)]

    # ------------------------------------------- extras (not in the Protocol)
    def get_work_orders(self, as_of_utc: str, asset_id: str | None = None) -> list[dict[str, Any]]:
        """Work orders created <= as_of. Outcome fields appear only once they happened."""
        as_of = norm_ts(as_of_utc)
        out = []
        for row in self.ds.work_orders:
            if row.get("created_utc") is None or row["created_utc"] > as_of:
                continue
            if asset_id is not None and row.get("asset_id") != asset_id:
                continue
            out.append(work_order_view(row, as_of))
        return out

    @property
    def audit(self) -> dict[str, Any]:
        """Row counts and de-duplication counters for files loaded so far."""
        return dict(self.ds.audit)


def public_event(ev: dict) -> dict[str, Any]:
    """Deep copy of a raw event with its original source instant (sub-second precision)."""
    out = deepcopy(ev)
    available = out.pop("_available_ts", None)
    if available is not None:
        out["available_at_utc"] = available
    precise = out.pop("_source_ts", None)
    if precise is not None:
        out["timestamp_utc"] = precise
    return out


def work_order_view(row: dict, as_of: str) -> dict[str, Any]:
    """Causal view of a work order: final status/summary only after completion."""
    out = dict(row)
    done = work_order_resolved_at(row)
    if done is not None and done <= as_of:
        out["status_asof"] = row.get("status") or "unknown"
        return out
    out["completed_utc"] = None
    out["status"] = "open"
    out["status_asof"] = "open"
    # Summaries are written by people after the fact and no source field proves
    # what was known at creation, so the text is withheld for ALL order types until
    # an observed completion. Always a string, so consumers can call str methods.
    out["summary"] = ""
    return out


def work_order_resolved_at(row: dict) -> str | None:
    """When the final status became observable: only a real completed_utc counts.

    'postponed' rows have no timestamp for the status change, so their final
    status and summary are never revealed (they stay "open").
    """
    return row.get("completed_utc")
