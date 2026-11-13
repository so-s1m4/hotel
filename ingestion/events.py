"""Owner A: normalized event stream. Preserve availability ordering and source IDs."""
from typing import Iterator
from common.contracts.api import ReplayDataGateway
from common.contracts.schemas import NormalizedEventV1


def iter_normalized_events(repo: ReplayDataGateway) -> Iterator[NormalizedEventV1]:
    return repo.iter_events()
