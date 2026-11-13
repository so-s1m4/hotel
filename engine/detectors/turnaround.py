"""Owner B: S01: availability, dirty rooms, inspection and early arrivals."""
from common.contracts.api import ReplayDataGateway
from common.contracts.schemas import NormalizedEventV1, DecisionRecordV2


def detect(event: NormalizedEventV1, repo: ReplayDataGateway) -> list[DecisionRecordV2]:
    raise NotImplementedError("TODO B: implement turnaround detector")
