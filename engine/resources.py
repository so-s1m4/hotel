"""Feasible half-open resource intervals shared by planning and mock execution."""
from datetime import timedelta
from math import isfinite

from engine.core import instant, utc, VIENNA


def scheduled(slot: dict, now: str) -> bool:
    effective = slot.get("effective_status_asof")
    if effective is not None:
        known = slot.get("status_known_utc")
        if known and instant(known) <= instant(now) and slot.get("status") in {"sick", "vacation", "out_of_service"}:
            return False
        return effective == "scheduled"
    known = slot.get("status_known_utc")
    return bool(known and instant(known) <= instant(now) and slot.get("status") == "scheduled")


class ResourceCalendar:
    def __init__(self):
        self.reservations: dict[str, list[tuple[str, str, str]]] = {}
        self.tentative_reservations: dict[str, list[tuple[str, str, str]]] = {}
        self.tentative_room_reservations: dict[str, list[tuple[str, str, str]]] = {}
        self.observed_busy: dict[str, dict[str, dict]] = {}
        self.resource_roles: dict[str, str] = {}
        self.current_time: str | None = None
        self.cancellations: list[dict] = []
        self._cancelled_ids: set[str] = set()
        self.volume_reservations: dict[str, list[tuple[str, float]]] = {}
        self.room_reservations: dict[str, list[tuple[str, str, str]]] = {}

    def observe_start(self, resource: str | None, room: str | None, event_id: str, now: str, role: str | None = None):
        """Observed work remains busy until an observed end, never a guessed duration."""
        instant(now)
        if not event_id or not (resource or room):
            raise ValueError("Observed job needs an ID and a resource or room")
        key = resource or "__unassigned__:" + room
        known_role = self.resource_roles.get(resource) if resource else None
        if known_role and role and known_role != role:
            raise ValueError("Observed resource role conflicts with building capabilities")
        role = role or known_role
        jobs = self.observed_busy.setdefault(key, {})
        job = {"resource_id": resource, "room_id": room, "event_id": event_id, "start_utc": now,
               "role": role,
               "uncertainty_reason": (f"Observed {role or 'unknown-role'} work has no identified performer; "
                                      f"{role + ' capacity is held' if role else 'room capacity is held'} until matching completion")
               if resource is None else None}
        if event_id in jobs and jobs[event_id] != job:
            raise ValueError("Conflicting observed job ID")
        jobs[event_id] = job

    def observe_end(self, resource: str | None, room: str | None, now: str, role: str | None = None):
        """Only a matching, not-earlier completion releases observed work."""
        end = instant(now)
        if not (resource or room):
            raise ValueError("Observed completion needs a resource or room")
        role = role or (self.resource_roles.get(resource) if resource else None)
        for key, jobs in list(self.observed_busy.items()):
            self.observed_busy[key] = {
                event_id: job for event_id, job in jobs.items()
                if not ((resource is None or job["resource_id"] == resource
                         or (job["resource_id"] is None and room is not None
                             and role is not None and job.get("role") == role))
                        and (room is None or job["room_id"] == room)
                        and (role is None or job.get("role") in {None, role})
                        and instant(job["start_utc"]) <= end)
            }
            if not self.observed_busy[key]:
                self.observed_busy.pop(key)

    @staticmethod
    def _interval(start: str, end: str, now: str):
        if not instant(now) <= instant(start) < instant(end):
            raise ValueError("Reservation must be a nonempty future interval")

    def reservations_for(self, resource: str, include_tentative: bool = True):
        rows = list(self.reservations.get(resource, []))
        if include_tentative:
            rows.extend(self.tentative_reservations.get(resource, []))
        return rows

    def room_jobs(self, room: str, include_tentative: bool = True):
        rows = list(self.room_reservations.get(room, []))
        if include_tentative:
            rows.extend(self.tentative_room_reservations.get(room, []))
        return rows

    def iter_reservations(self):
        """All capacity commitments, including pending approval holds, grouped by worker."""
        for resource in sorted(set(self.reservations) | set(self.tentative_reservations)):
            yield resource, self.reservations_for(resource)

    def available(self, resource: str, start: str, end: str, now: str,
                  exclude_decision_id: str | None = None) -> bool:
        self._interval(start, end, now)
        if self.observed_busy.get(resource):
            return False
        role = self.resource_roles.get(resource)
        if role and any(job["resource_id"] is None and job.get("role") == role
                        for jobs in self.observed_busy.values() for job in jobs.values()):
            return False
        for collection in (self.reservations, self.tentative_reservations):
            if resource in collection:
                collection[resource] = [r for r in collection[resource] if instant(r[1]) > instant(now)]
        return all(ident == exclude_decision_id or instant(end) <= instant(a) or instant(start) >= instant(b)
                   for a, b, ident in self.reservations_for(resource))

    def room_available(self, room: str, start: str, end: str, now: str,
                       exclude_decision_id: str | None = None) -> bool:
        self._interval(start, end, now)
        if any(job["room_id"] == room for jobs in self.observed_busy.values() for job in jobs.values()):
            return False
        for collection in (self.room_reservations, self.tentative_room_reservations):
            if room in collection:
                collection[room] = [r for r in collection[room] if instant(r[1]) > instant(now)]
        return all(ident == exclude_decision_id or instant(end) <= instant(a) or instant(start) >= instant(b)
                   for a, b, ident in self.room_jobs(room))

    def capacity_available(self, destination: str, observed_l: float, limit_l: float, extra_l: float) -> bool:
        if (not destination or any(isinstance(x, bool) or not isinstance(x, (int, float)) or not isfinite(x)
                                   for x in (observed_l, limit_l, extra_l))
                or observed_l < 0 or limit_l <= 0 or extra_l <= 0):
            return False
        return observed_l + sum(amount for _, amount in self.volume_reservations.get(destination, [])) + extra_l <= limit_l + 1e-8

    def reserve_volume(self, destination: str, amount: float, decision_id: str):
        if not destination or not decision_id or isinstance(amount, bool) or not isinstance(amount, (int, float)) or not isfinite(amount) or amount <= 0:
            raise ValueError("Volume reservation must be positive, finite and identified")
        if decision_id in self._cancelled_ids:
            raise ValueError("Cancelled command requires a fresh decision ID")
        if any(key == decision_id for target, values in self.volume_reservations.items()
               if target != destination for key, _ in values):
            raise ValueError("Conflicting destination for volume reservation")
        existing = [value for key, value in self.volume_reservations.get(destination, []) if key == decision_id]
        if existing:
            if existing != [amount]:
                raise ValueError("Conflicting volume reservation for decision")
            return
        self.volume_reservations.setdefault(destination, []).append((decision_id, amount))

    def release_volume(self, decision_id: str):
        for destination in self.volume_reservations:
            self.volume_reservations[destination] = [r for r in self.volume_reservations[destination] if r[0] != decision_id]

    def cancel(self, decision_id: str, reason: str = "Reservation invalidated or confirmed unnecessary by new observations"):
        existed = (any(r[2] == decision_id for collection in (self.reservations, self.room_reservations, self.tentative_reservations, self.tentative_room_reservations)
                       for reservations in collection.values() for r in reservations)
                   or any(key == decision_id for values in self.volume_reservations.values() for key, _ in values))
        if existed and decision_id not in self._cancelled_ids:
            self.cancellations.append({"decision_id": decision_id, "timestamp_utc": self.current_time,
                                       "reason": reason})
            self._cancelled_ids.add(decision_id)
        self.release_volume(decision_id)
        for collection in (self.reservations, self.room_reservations, self.tentative_reservations, self.tentative_room_reservations):
            for key in collection:
                collection[key] = [r for r in collection[key] if r[2] != decision_id]

    def _validate_reservation_identity(self, resource, start, end, decision_id, room):
        for other, reservations in self.iter_reservations():
            existing = [r for r in reservations if r[2] == decision_id]
            if existing and (other != resource or any(r != (start, end, decision_id) for r in existing)):
                raise ValueError("Conflicting resource or interval for repeated decision")
        recorded_rooms = {key for collection in (self.room_reservations, self.tentative_room_reservations)
                          for key, rows in collection.items() if any(r[2] == decision_id for r in rows)}
        if recorded_rooms and recorded_rooms != {room}:
            raise ValueError("Conflicting room for repeated reservation")
        # A decision first submitted without a room cannot later become room entry.
        if not recorded_rooms and any(r[2] == decision_id for _, rows in self.iter_reservations() for r in rows) and room:
            raise ValueError("Conflicting room for repeated reservation")

    def _check_reservation(self, resource, start, end, decision_id, now, room):
        if not resource or not decision_id:
            raise ValueError("Resource and decision identifiers are required")
        if decision_id in self._cancelled_ids:
            raise ValueError("Cancelled command requires a fresh decision ID")
        self._interval(start, end, now)
        self._validate_reservation_identity(resource, start, end, decision_id, room)
        if not self.available(resource, start, end, now, exclude_decision_id=decision_id):
            raise ValueError(f"Resource already reserved: {resource}")
        if room and not self.room_available(room, start, end, now, exclude_decision_id=decision_id):
            raise ValueError(f"Room already reserved for a job: {room}")

    def reserve_tentative(self, resource: str, start: str, end: str, decision_id: str,
                          now: str, room: str | None = None):
        """Hold planning capacity while approval/consent is pending; no execution occurs."""
        self._check_reservation(resource, start, end, decision_id, now, room)
        row = (start, end, decision_id)
        if row in self.reservations.get(resource, []):
            return  # never downgrade an accepted reservation to a pending one
        if row not in self.tentative_reservations.setdefault(resource, []):
            self.tentative_reservations[resource].append(row)
        if room and row not in self.tentative_room_reservations.setdefault(room, []):
            self.tentative_room_reservations[room].append(row)

    def reserve(self, resource: str, start: str, end: str, decision_id: str, now: str,
                room: str | None = None):
        """Accept the exact proposed slot, atomically upgrading its own pending hold."""
        self._check_reservation(resource, start, end, decision_id, now, room)
        row = (start, end, decision_id)
        if row not in self.reservations.setdefault(resource, []):
            self.reservations[resource].append(row)
        if room and row not in self.room_reservations.setdefault(room, []):
            self.room_reservations[room].append(row)
        for collection in (self.tentative_reservations, self.tentative_room_reservations):
            for key in collection:
                collection[key] = [r for r in collection[key] if r[2] != decision_id]

    def first_slot(self, slots: list[dict], now: str, until: str, duration: int, room: str | None = None, role: str = "cleaner", earliest: str | None = None):
        if isinstance(duration, bool) or not isinstance(duration, (int, float)) or not isfinite(duration) or duration <= 0:
            raise ValueError("Job duration must be positive and finite")
        choices = []
        for slot in slots:
            if slot.get("role") != role or not scheduled(slot, now):
                continue
            resource = slot["resource_id"]
            begin = max(instant(now), instant(slot["start_utc"]), instant(earliest or now))
            latest = min(instant(until), instant(slot["end_utc"]))
            # Try shift start and ends of already-reserved intervals.
            starts = [begin] + [instant(b) for a, b, _ in self.reservations_for(resource)
                                if instant(b) >= begin]
            if room:
                starts += [instant(b) for a, b, _ in self.room_jobs(room) if instant(b) >= begin]
            for start in sorted(starts):
                end = start + timedelta(minutes=duration)
                if (end <= latest and self.available(resource, utc(start), utc(end), now)
                        and (not room or self.room_available(room, utc(start), utc(end), now))):
                    choices.append((utc(start), utc(end), resource, slot))
                    break
        return min(choices, key=lambda c: (instant(c[1]), c[2])) if choices else None


def oncall_slot(now: str, until: str, duration: int, calendar: ResourceCalendar):
    """Explicit assumption from building notes; only offered for currently waiting guests."""
    if isinstance(duration, bool) or not isinstance(duration, (int, float)) or not isfinite(duration) or duration <= 0:
        raise ValueError("On-call duration must be positive and finite")
    local = instant(now).astimezone(VIENNA)
    earliest = max(local + timedelta(minutes=45), local.replace(hour=15, minute=30, second=0))
    close = local.replace(hour=22, minute=0, second=0)
    starts = [earliest] + [instant(end).astimezone(VIENNA) for _, end, _ in calendar.reservations_for("ONCALL_1")
                          if instant(end) >= earliest]
    for start in sorted(set(starts)):
        end = start + timedelta(minutes=duration)
        if end > close or instant(utc(end)) > instant(until):
            continue
        if calendar.available("ONCALL_1", utc(start), utc(end), now):
            return utc(start), utc(end)
    return None
