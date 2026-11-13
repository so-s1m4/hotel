"""S05: causal arrival pressure, known absences and feasible shared-resource jobs."""
from copy import deepcopy
from datetime import timedelta
from math import ceil

from engine.core import Candidate, DetectionResult, instant, utc, room_state, ago
from engine.resources import scheduled


class SurgeDetector:
    def __init__(self, config):
        self.config = config
        self.healthy = 0

    def detect(self, event, repo, building, calendar):
        result = DetectionResult()
        now = event.available_at_utc
        if event.event_type == "clock_tick":
            if instant(now).minute != 0:
                return result
        elif event.event_type != "roster_update":
            return result
        until = utc(instant(now) + timedelta(hours=24))
        rooms = building["building"]["rooms"]
        arrivals = {}
        for room in rooms:
            for b in repo.get_bookings_for_room(room["id"], now, until, now):
                if (b.get("created_utc") and instant(b["created_utc"]) <= instant(now)
                        and b.get("arrival_utc") and instant(now) <= instant(b["arrival_utc"]) < instant(until)):
                    arrivals[b["booking_id"]] = b
        slots = repo.get_staff_slots(now, until, now, role="cleaner")
        absence = [s for s in slots if s.get("effective_status_asof") in {"sick", "vacation", "out_of_service"}
                   and s.get("status_known_utc") and instant(s["status_known_utc"]) <= instant(now)]
        available = [s for s in slots if scheduled(s, now)]
        planning = deepcopy(calendar)
        ready_jobs = []
        for b in sorted(arrivals.values(), key=lambda x: (instant(x["arrival_utc"]), x["booking_id"])):
            room = b["room_id"]
            state = room_state(repo, room, now)
            if (state.get("occupancy") == "vacant" and state.get("housekeeping_state") == "dirty"
                    and state.get("last_observed_utc")
                    and instant(state["last_observed_utc"]) >= instant(ago(now, self.config.telemetry_max_age_minutes))):
                ready_jobs.append((b, state))
        demand = len(arrivals) >= max(2, ceil(len(rooms) * self.config.surge_arrival_fraction))
        capacity_short = False
        # Probe actual slots and reservations, not nominal staff count or shift lengths.
        probe = deepcopy(calendar)
        for b, state in ready_jobs:
            choice = probe.first_slot(available, now, b["arrival_utc"], self.config.cleaning_minutes, room=b["room_id"])
            if not choice:
                capacity_short = True
            else:
                a, z, resource, _ = choice
                probe.reserve(resource, a, z, "probe:" + b["booking_id"], now, room=b["room_id"])
        if not demand or not (absence or capacity_short):
            self.healthy += 1
            if self.healthy >= 3:
                result.resolved.add("surge:BUILDING")
            return result
        self.healthy = 0
        evidence = [f"bookings.csv:{b['booking_id']} (created={b['created_utc']}; planned arrival={b['arrival_utc']})"
                    for b in arrivals.values()]
        evidence += [f"staff_roster.csv:{s['shift_id']}:known={s['status_known_utc']}" for s in absence]
        result.candidates.append(Candidate("surge:BUILDING", "BUILDING", "occupancy_surge", "flag_anomaly",
            f"{len(arrivals)} known planned arrivals in 24 h, {len(absence)} known absent cleaner shifts, {len(ready_jobs)} observed vacant dirty arrival rooms. {'Deadline capacity is insufficient.' if capacity_short else 'Absence creates coordination risk.'} Bookings are plans; cancellation timing and actual guest movements remain uncertain.",
            evidence, "Recheck observed room readiness, actual arrivals and known staff status at each deadline.",
            "Coordinate a busy-period plan; no extra staff or completed cleaning is invented.", priority="high"))
        robot_slots = repo.get_staff_slots(now, until, now, role="robot")
        planned_rooms = set()
        for b, state in ready_jobs:
            room = b["room_id"]
            if room in planned_rooms:
                continue
            planned_rooms.add(room)
            # A current/future room job already scheduled by S01 is reused.
            if any(instant(end) > instant(now) for _, end, _ in calendar.room_reservations.get(room, [])):
                continue
            key = "surge_job:" + b["booking_id"]
            refs = [f"bookings.csv:{b['booking_id']}", *state.get("evidence", [])]
            choice = planning.first_slot(available, now, b["arrival_utc"], self.config.cleaning_minutes, room=room)
            if not choice:
                result.candidates.append(Candidate(key, room, "occupancy_surge", "notify_guest",
                    f"Known arrival planned at {b['arrival_utc']}, but no observed-vacant-room human cleaning slot can meet it. Propose an update and human coordination; readiness time is unconfirmed.",
                    refs, "Confirm an eligible staff slot and observed completion before offering access.",
                    "Propose a delay message; no delivery, reassignment or on-call availability is assumed.", priority="high"))
                continue
            start, end, resource, slot = choice
            planning.reserve(resource, start, end, "surge:" + key, now, room=room)
            result.candidates.append(Candidate(key, room, "occupancy_surge", "schedule_cleaning",
                f"Propose {resource} {start}..{end} ahead of planned arrival {b['arrival_utc']}; {self.config.cleaning_minutes} min is a simulated duration, with release checks still required.",
                refs + [f"staff_roster.csv:{slot['shift_id']}"],
                "Verify actual human cleaning and required inspection, then reassess room readiness.",
                "Reserve one feasible human job without claiming future cleanliness.", resource=resource,
                context={"room": state, "slot": slot, "start_utc": start, "end_utc": end, "entry_required": True}))
            # Robot work is supplemental. It never reduces the assumed human duration.
            robot = planning.first_slot(robot_slots, now, b["arrival_utc"], self.config.robot_minutes, room=room, role="robot", earliest=end)
            if robot:
                a, z, robot_id, robot_slot = robot
                planning.reserve(robot_id, a, z, "robot:" + key, now, room=room)
                result.candidates.append(Candidate(key, room, "occupancy_surge", "dispatch_robot",
                    f"Optional floor-only task {robot_id} {a}..{z}, conditional on continued vacancy; no bathroom cleaning, certification or added human capacity.",
                    refs + [f"staff_roster.csv:{robot_slot['shift_id']}"],
                    "Recheck vacancy before the future task; humans independently certify cleaning and release.",
                    "Reserve supplemental simulated floor work only.", resource=robot_id,
                    context={"room": state, "slot": robot_slot, "start_utc": a, "end_utc": z,
                             "task": "floor_cleaning", "entry_required": True,
                             "after_action": "schedule_cleaning"}))
        return result
