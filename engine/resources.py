"""Feasible half-open resource intervals shared by planning and mock execution."""
from datetime import timedelta

from engine.core import instant, utc, VIENNA


def scheduled(slot: dict, now: str) -> bool:
    effective = slot.get("effective_status_asof")
    if effective is not None:
        return effective == "scheduled"
    known = slot.get("status_known_utc")
    return bool(known and instant(known) <= instant(now) and slot.get("status") == "scheduled")


class ResourceCalendar:
    def __init__(self):
        self.reservations: dict[str, list[tuple[str, str, str]]] = {}
        self.room_reservations: dict[str, list[tuple[str, str, str]]] = {}

    def available(self, resource: str, start: str, end: str, now: str) -> bool:
        self.reservations[resource] = [r for r in self.reservations.get(resource, [])
                                       if instant(r[1]) > instant(now)]
        return all(instant(end) <= instant(a) or instant(start) >= instant(b)
                   for a, b, _ in self.reservations[resource])

    def room_available(self, room: str, start: str, end: str, now: str) -> bool:
        self.room_reservations[room] = [r for r in self.room_reservations.get(room, []) if instant(r[1]) > instant(now)]
        return all(instant(end) <= instant(a) or instant(start) >= instant(b)
                   for a, b, _ in self.room_reservations[room])

    def cancel(self, decision_id: str):
        for collection in (self.reservations, self.room_reservations):
            for key in collection:
                collection[key] = [r for r in collection[key] if r[2] != decision_id]

    def reserve(self, resource: str, start: str, end: str, decision_id: str, now: str, room: str | None = None):
        if not self.available(resource, start, end, now):
            raise ValueError(f"Resource already reserved: {resource}")
        if room and not self.room_available(room, start, end, now):
            raise ValueError(f"Room already reserved for a job: {room}")
        self.reservations[resource].append((start, end, decision_id))
        if room:
            self.room_reservations.setdefault(room, []).append((start, end, decision_id))

    def first_slot(self, slots: list[dict], now: str, until: str, duration: int, room: str | None = None):
        choices = []
        for slot in slots:
            if slot.get("role") != "cleaner" or not scheduled(slot, now):
                continue
            resource = slot["resource_id"]
            begin = max(instant(now), instant(slot["start_utc"]))
            latest = min(instant(until), instant(slot["end_utc"]))
            # Try shift start and ends of already-reserved intervals.
            starts = [begin] + [instant(b) for a, b, _ in self.reservations.get(resource, [])
                                if instant(b) >= begin]
            if room:
                starts += [instant(b) for a, b, _ in self.room_reservations.get(room, []) if instant(b) >= begin]
            for start in sorted(starts):
                end = start + timedelta(minutes=duration)
                if (end <= latest and self.available(resource, utc(start), utc(end), now)
                        and (not room or self.room_available(room, utc(start), utc(end), now))):
                    choices.append((utc(start), utc(end), resource, slot))
                    break
        return min(choices, key=lambda c: (instant(c[1]), c[2])) if choices else None


def oncall_slot(now: str, until: str, duration: int, calendar: ResourceCalendar):
    """Explicit assumption from building notes; only offered for currently waiting guests."""
    local = instant(now).astimezone(VIENNA)
    start = max(local + timedelta(minutes=45), local.replace(hour=15, minute=30, second=0))
    end = start + timedelta(minutes=duration)
    close = local.replace(hour=22, minute=0, second=0)
    if end > close or instant(utc(end)) > instant(until):
        return None
    if not calendar.available("ONCALL_1", utc(start), utc(end), now):
        return None
    return utc(start), utc(end)
