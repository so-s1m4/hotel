"""Owner A: multilingual extraction, evidence checks, cache and offline fallback."""
from common.contracts.schemas import GuestIntentV1


def parse_request(record: dict, context: dict) -> list[GuestIntentV1]:
    raise NotImplementedError("TODO A: source-label fallback and optional LLM parser")
