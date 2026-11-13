"""Owner B: all decisions must pass this gate before mock execution."""
from common.contracts.schemas import DecisionRecordV2


def validate_safety(decision: DecisionRecordV2, context: dict) -> None:
    raise NotImplementedError("TODO B: approvals, consent, occupancy, comfort, capacity")
