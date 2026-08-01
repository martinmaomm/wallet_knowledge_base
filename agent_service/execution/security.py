from __future__ import annotations

from secrets import compare_digest

from agent_service.config import Settings
from agent_service.dsl import plan_fingerprint
from agent_service.schemas import ApprovalDecision, TestPlan


def assert_execution_allowed(
    settings: Settings,
    plan: TestPlan,
    approval: ApprovalDecision | None,
) -> None:
    settings.assert_safe_url(settings.test_base_url)
    if approval is None or approval.action != "approve":
        raise PermissionError("test execution requires an approved decision")
    if approval.plan_hash is None:
        raise PermissionError("approved decision requires a plan hash")

    current_hash = plan_fingerprint(plan)
    if not compare_digest(approval.plan_hash, current_hash):
        raise PermissionError("approved plan hash does not match current plan")
