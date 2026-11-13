"""Deterministic episodes and atomic official JSONL serialization."""
import os
from pathlib import Path
import tempfile
from typing import Iterable

from common.contracts.schemas import DecisionRecordV2
from engine.core import instant, stable_id


class EpisodeTracker:
    def __init__(self):
        self.active: dict[str, tuple[str, set[tuple[str, str]]]] = {}
        self._generations: dict[str, int] = {}

    def close(self, key: str):
        self.active.pop(key, None)

    def episode(self, key: str, now: str) -> str:
        if key not in self.active:
            self._generations[key] = self._generations.get(key, 0) + 1
            self.active[key] = (stable_id("episode", key, now, str(self._generations[key])), set())
        return self.active[key][0]

    def emitted(self, key: str, action: str, variant: str) -> bool:
        return key in self.active and (action, variant) in self.active[key][1]

    def mark(self, key: str, action: str, variant: str):
        self.active[key][1].add((action, variant))


def write_decisions(records: Iterable[DecisionRecordV2], output: Path) -> None:
    """Validate all records before replacing an existing result; reject duplicate IDs."""
    rows = [DecisionRecordV2.model_validate(r.model_dump() if isinstance(r, DecisionRecordV2) else r) for r in records]
    for row in rows:
        row.evidence = sorted(set(row.evidence))
        row.dependencies = sorted(set(row.dependencies))
        row.guest_request_ids = sorted(set(row.guest_request_ids))
    rows.sort(key=lambda r: (instant(r.timestamp_utc), r.event_id))
    ids = [r.event_id for r in rows]
    if len(set(ids)) != len(ids):
        raise ValueError("Duplicate decision event_id")
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    name = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=output.parent,
                                         prefix="." + output.name, delete=False) as stream:
            name = stream.name
            for row in rows:
                stream.write(row.model_dump_json(exclude_none=True) + "\n")
        os.replace(name, output)
    finally:
        if name and os.path.exists(name):
            os.unlink(name)
