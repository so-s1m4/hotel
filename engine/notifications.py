"""Booking-scoped message deduplication independent of detector episode resets."""
from datetime import timedelta
from hashlib import sha256

from engine.core import instant


class NotificationLedger:
    def __init__(self):
        self.history = {}
        self.suppressed = 0
        self.explicit_queries = set()

    def allow(self, candidate, scope, now):
        query_id = candidate.context.get("explicit_query_id")
        if query_id and query_id not in self.explicit_queries:
            return True
        kind = candidate.variant.split(":", 1)[0]
        topic = "readiness" if candidate.issue in {"room_turnaround", "occupancy_surge"} else candidate.issue
        key = (candidate.asset, scope, topic)
        fingerprint = sha256(candidate.reason.encode()).hexdigest()
        old = self.history.get(key)
        if old:
            previous, old_kind, old_fingerprint, old_priority = old
            elapsed = instant(now)-instant(previous)
            priorities = {"low": 0, "medium": 1, "high": 2, "critical": 3}
            escalation = priorities[candidate.priority] > priorities[old_priority]
            # Withdrawal/correction must reach the guest even within the cooldown.
            correction = kind in {"withdraw_estimate", "cancelled_plan"}
            if not correction and not escalation:
                if fingerprint == old_fingerprint and elapsed < timedelta(hours=6):
                    self.suppressed += 1
                    return False
                if candidate.context.get("accepted_plan"):
                    return True
                if kind == old_kind and elapsed < timedelta(hours=1):
                    self.suppressed += 1
                    return False
                if elapsed < timedelta(minutes=5):
                    self.suppressed += 1
                    return False
        return True

    def mark(self, candidate, scope, now):
        if candidate.context.get("explicit_query_id"):
            self.explicit_queries.add(candidate.context["explicit_query_id"])
        kind = candidate.variant.split(":", 1)[0]
        topic = "readiness" if candidate.issue in {"room_turnaround", "occupancy_surge"} else candidate.issue
        self.history[(candidate.asset, scope, topic)] = (now, kind, sha256(candidate.reason.encode()).hexdigest(), candidate.priority)
