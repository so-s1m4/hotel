"""Owner B: simulation only; approval requirements are not approval grants."""
from common.contracts.schemas import DecisionRecordV2


class MockExecutor:
    def execute(self, decision: DecisionRecordV2, context: dict) -> dict:
        raise NotImplementedError("TODO B: safety gate, pending approvals, simulated audit")
