from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import uuid4

from langchain_core.runnables import RunnableConfig
from langgraph.types import interrupt
from pydantic import ValidationError

from agent_service.bug_client import BugClient
from agent_service.config import Settings
from agent_service.dsl import (
    REQUIRED_BASELINE_IDS,
    plan_fingerprint,
    validate_test_plan,
)
from agent_service.execution.assertions import (
    AssertionResult,
    ExecutionBackendResult,
)
from agent_service.execution.security import assert_execution_allowed
from agent_service.graph.state import AgentState
from agent_service.model_provider import ModelProvider, StructuredModelError
from agent_service.schemas import (
    ApprovalDecision,
    FailureAnalysis,
    RequirementSet,
    RelatedBug,
    RiskAnalysis,
    TestPlan,
)
from agent_service.sources import load_sources, read_prompt


class ExecutionBackend(Protocol):
    async def execute(self, plan: TestPlan) -> ExecutionBackendResult: ...


@dataclass(frozen=True)
class GraphDependencies:
    settings: Settings
    model_provider: ModelProvider
    bug_client: BugClient
    execution_backend: ExecutionBackend | None = None


INVALID_APPROVAL_MESSAGE = "Approval response is invalid."
INVALID_PLAN_MESSAGE = "Approved test plan is no longer valid."
RELATED_BUG_FIELDS = frozenset(
    {
        "bug_id",
        "title",
        "severity",
        "status",
        "resolution",
    }
)


def _json_payload(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _source_ids(state: AgentState) -> frozenset[str]:
    versions = state.get("source_versions")
    if not isinstance(versions, list):
        raise ValueError("source_versions are invalid")

    source_ids: set[str] = set()
    for version in versions:
        if not isinstance(version, dict):
            raise ValueError("source_versions are invalid")
        source_id = version.get("source_id")
        if not isinstance(source_id, str) or not source_id:
            raise ValueError("source_versions are invalid")
        source_ids.add(source_id)
    return frozenset(source_ids)


def _validate_requirement_source_refs(
    requirements: RequirementSet,
    state: AgentState,
) -> RequirementSet:
    known_sources = _source_ids(state)
    if any(
        ref not in known_sources
        for requirement in requirements.requirements
        for ref in requirement.source_refs
    ):
        raise ValueError("requirement contains unknown source_ref")
    return requirements


def _validate_risk_source_refs(
    risks: RiskAnalysis,
    state: AgentState,
) -> RiskAnalysis:
    known_sources = _source_ids(state)
    if any(
        ref not in known_sources
        for risk in risks.risks
        for ref in risk.source_refs
    ):
        raise ValueError("risk contains unknown source_ref")
    return risks


def _related_bug_refs(state: AgentState) -> frozenset[str]:
    related_bugs = state.get("related_bugs")
    if not isinstance(related_bugs, list):
        raise ValueError("related_bugs are invalid")

    bug_ids: set[int] = set()
    invalid_record = False
    for item in related_bugs:
        if (
            not isinstance(item, dict)
            or frozenset(item) != RELATED_BUG_FIELDS
            or type(item.get("bug_id")) is not int
        ):
            invalid_record = True
            break
        try:
            bug = RelatedBug.model_validate(item)
        except ValidationError:
            invalid_record = True
            break
        bug_ids.add(bug.bug_id)

    if invalid_record:
        raise ValueError("related_bugs are invalid")
    return frozenset(f"BUG-{bug_id}" for bug_id in bug_ids)


def _validate_test_plan_source_refs(
    plan: TestPlan,
    state: AgentState,
) -> TestPlan:
    known_sources = _source_ids(state) | _related_bug_refs(state)
    for case in plan.cases:
        trusted_manual_ref = (
            f"人工基准:{case.case_id}"
            if case.case_id in REQUIRED_BASELINE_IDS
            else None
        )
        if any(
            ref not in known_sources and ref != trusted_manual_ref
            for ref in case.source_refs
        ):
            raise ValueError("test plan contains unknown source_ref")
    return plan


def _review_request(
    state: AgentState,
    *,
    status: str,
    message: str | None = None,
) -> dict[str, Any]:
    request = {
        "task_id": state["task_id"],
        "status": status,
    }
    if status == "waiting_approval":
        request["test_plan"] = state["test_plan"]
    if message is not None:
        request["message"] = message
    return request


def initialize_task(
    state: AgentState,
    config: RunnableConfig,
) -> dict[str, Any]:
    thread_id = config["configurable"]["thread_id"]
    return {
        "task_id": state.get("task_id") or f"TASK-{uuid4().hex[:12]}",
        "thread_id": thread_id,
        "status": "initialized",
        "errors": [],
    }


def make_nodes(deps: GraphDependencies) -> dict[str, Any]:
    def load_sources_node(_: AgentState) -> dict[str, Any]:
        loaded = load_sources(deps.settings.source_paths)
        return {
            "source_text": loaded.combined_text,
            "source_versions": [
                item.model_dump(exclude={"content"})
                for item in loaded.documents
            ],
            "status": "sources_loaded",
        }

    async def extract_requirements(
        state: AgentState,
    ) -> dict[str, Any]:
        prompt = (
            f"{read_prompt('extract_requirements')}\n\n"
            "<supplied_sources>\n"
            f"{state['source_text']}\n"
            "</supplied_sources>"
        )
        result = await deps.model_provider.generate_structured(
            task_type="extract_requirements",
            prompt=prompt,
            schema=RequirementSet,
        )
        result = _validate_requirement_source_refs(result, state)
        return {
            "requirements": result.model_dump(),
            "status": "requirements_extracted",
        }

    async def analyze_risks(state: AgentState) -> dict[str, Any]:
        prompt = (
            f"{read_prompt('analyze_risks')}\n\n"
            "<validated_requirements>\n"
            f"{_json_payload(state['requirements'])}\n"
            "</validated_requirements>"
        )
        result = await deps.model_provider.generate_structured(
            task_type="analyze_risks",
            prompt=prompt,
            schema=RiskAnalysis,
        )
        result = _validate_risk_source_refs(result, state)
        return {
            "risks": result.model_dump(),
            "status": "risks_analyzed",
        }

    async def retrieve_bugs(state: AgentState) -> dict[str, Any]:
        queries = RiskAnalysis.model_validate(state["risks"]).bug_queries
        bugs = await deps.bug_client.search_related(queries)
        return {
            "related_bugs": [item.model_dump() for item in bugs],
            "status": "bugs_retrieved",
        }

    async def generate_test_plan(
        state: AgentState,
    ) -> dict[str, Any]:
        context = {
            "requirements": state["requirements"],
            "risks": state["risks"],
            "related_bugs": state.get("related_bugs", []),
        }
        prompt = (
            f"{read_prompt('generate_test_plan')}\n\n"
            "<validated_context>\n"
            f"{_json_payload(context)}\n"
            "</validated_context>"
        )
        result = await deps.model_provider.generate_structured(
            task_type="generate_test_plan",
            prompt=prompt,
            schema=TestPlan,
        )
        plan = validate_test_plan(result, require_golden_set=True)
        plan = _validate_test_plan_source_refs(plan, state)
        return {
            "test_plan": plan.model_dump(),
            "status": "plan_validated",
        }

    def human_review(state: AgentState) -> dict[str, Any]:
        request = _review_request(
            state,
            status="waiting_approval",
        )
        while True:
            resumed = interrupt(request)
            candidate = resumed
            if (
                isinstance(resumed, dict)
                and resumed.get("action") == "approve"
            ):
                candidate = {**resumed, "plan_hash": None}
            try:
                decision = ApprovalDecision.model_validate(candidate)
            except ValidationError:
                request = _review_request(
                    state,
                    status="invalid_approval",
                    message=INVALID_APPROVAL_MESSAGE,
                )
                continue

            if decision.action != "approve":
                break

            try:
                plan = validate_test_plan(
                    TestPlan.model_validate(state["test_plan"]),
                    require_golden_set=True,
                )
                plan = _validate_test_plan_source_refs(plan, state)
            except (ValidationError, ValueError):
                request = _review_request(
                    state,
                    status="invalid_approval",
                    message=INVALID_PLAN_MESSAGE,
                )
                continue

            decision = ApprovalDecision(
                action="approve",
                feedback=decision.feedback,
                plan_hash=plan_fingerprint(plan),
            )
            break

        status = (
            "approved"
            if decision.action == "approve"
            else decision.action
        )
        return {
            "approval": decision.model_dump(),
            "status": status,
        }

    def execution_failure(
        *,
        name: str,
        expected: str,
        actual: str,
    ) -> dict[str, Any]:
        assertion = AssertionResult(
            name=name,
            passed=False,
            expected=expected,
            actual=actual,
        )
        return {
            "execution_results": [],
            "assertion_results": [assertion.model_dump()],
            "status": "execution_failed",
            "passed": False,
        }

    async def execute_tests(state: AgentState) -> dict[str, Any]:
        try:
            plan = validate_test_plan(
                TestPlan.model_validate(state["test_plan"]),
                require_golden_set=True,
            )
            plan = _validate_test_plan_source_refs(plan, state)
            approval = ApprovalDecision.model_validate(state["approval"])
            assert_execution_allowed(
                deps.settings,
                plan,
                approval,
            )
        except (KeyError, ValidationError, ValueError, PermissionError):
            return execution_failure(
                name="execution_authorized",
                expected="approved plan with matching hash",
                actual="denied",
            )

        if deps.execution_backend is None:
            return execution_failure(
                name="execution_backend_available",
                expected="configured",
                actual="unavailable",
            )

        try:
            raw_result = await deps.execution_backend.execute(plan)
            result = ExecutionBackendResult.model_validate(raw_result)
        except Exception:
            return execution_failure(
                name="execution_backend_succeeded",
                expected="completed",
                actual="failed",
            )

        expected_case_ids = [case.case_id for case in plan.cases]
        actual_case_ids = [
            item.case_id for item in result.execution_results
        ]
        if (
            actual_case_ids != expected_case_ids
            or any(
                item.status != "completed"
                for item in result.execution_results
            )
        ):
            return execution_failure(
                name="execution_results_complete",
                expected="one completed result per planned case",
                actual="incomplete",
            )

        assertion_results = [
            item.model_dump() for item in result.assertion_results
        ]
        passed = all(item.passed for item in result.assertion_results)
        return {
            "execution_results": [
                item.model_dump(mode="json")
                for item in result.execution_results
            ],
            "assertion_results": assertion_results,
            "status": "completed" if passed else "execution_failed",
            "passed": passed,
        }

    async def classify_failure(state: AgentState) -> dict[str, Any]:
        context = {
            "execution_results": state.get("execution_results", []),
            "assertion_results": state.get("assertion_results", []),
            "related_bugs": state.get("related_bugs", []),
        }
        prompt = (
            f"{read_prompt('classify_failure')}\n\n"
            "<validated_failure_context>\n"
            f"{_json_payload(context)}\n"
            "</validated_failure_context>"
        )
        try:
            result = await deps.model_provider.generate_structured(
                task_type="classify_failure",
                prompt=prompt,
                schema=FailureAnalysis,
            )
        except StructuredModelError:
            result = FailureAnalysis(
                category="unknown",
                summary="Failure classification is unavailable.",
                evidence_refs=["deterministic_assertion_results"],
                related_bug_ids=[],
                recommended_action="Review deterministic execution evidence.",
            )

        known_bug_ids = {
            item["bug_id"]
            for item in state.get("related_bugs", [])
            if isinstance(item, dict)
            and type(item.get("bug_id")) is int
            and item["bug_id"] > 0
        }
        if not set(result.related_bug_ids).issubset(known_bug_ids):
            result = FailureAnalysis(
                category="unknown",
                summary="Failure classification references unknown Bugs.",
                evidence_refs=["deterministic_assertion_results"],
                related_bug_ids=[],
                recommended_action="Review deterministic execution evidence.",
            )
        return {
            "failure_analysis": result.model_dump(),
            "status": "failure_classified",
            "passed": False,
        }

    return {
        "load_sources": load_sources_node,
        "extract_requirements": extract_requirements,
        "analyze_risks": analyze_risks,
        "retrieve_bugs": retrieve_bugs,
        "generate_test_plan": generate_test_plan,
        "human_review": human_review,
        "execute_tests": execute_tests,
        "classify_failure": classify_failure,
    }
