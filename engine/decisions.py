"""Owner B: implement official serialization and incident deduplication here."""
from pathlib import Path
from typing import Iterable
from common.contracts.schemas import DecisionRecordV2


def write_decisions(records: Iterable[DecisionRecordV2], output: Path) -> None:
    raise NotImplementedError("TODO B: validate, sort chronologically, deduplicate and write JSONL")
