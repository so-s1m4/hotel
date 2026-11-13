"""Owner B: scheduling, incident deduplication and resource reservations."""
from common.contracts.api import ReplayDataGateway
from common.contracts.schemas import NormalizedEventV1, DecisionRecordV2


def plan(event: NormalizedEventV1, repo: ReplayDataGateway) -> list[DecisionRecordV2]:
    raise NotImplementedError("TODO B: coordinate detectors and feasible actions")
