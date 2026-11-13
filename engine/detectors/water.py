"""Owner B: S02: cumulative deltas, resets, missing data and benign look-alikes."""
from common.contracts.api import ReplayDataGateway
from common.contracts.schemas import NormalizedEventV1, DecisionRecordV2


def detect(event: NormalizedEventV1, repo: ReplayDataGateway) -> list[DecisionRecordV2]:
    raise NotImplementedError("TODO B: implement water detector")
