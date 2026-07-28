from __future__ import annotations

import asyncio
from copy import deepcopy
import hashlib
from pathlib import Path
from typing import Any

import httpx
import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from agent_service.bug_client import BugClient
from agent_service.config import Settings
from agent_service.dsl import plan_fingerprint
from agent_service.graph.build import GraphDependencies, build_graph
from agent_service.model_provider import FakeModelProvider
from agent_service.schemas import RelatedBug, TestPlan as GeneratedTestPlan
from agent_service.sources import load_sources, read_prompt


FIXTURE_DIR = Path(__file__).parent / "fixtures"
MODEL_OUTPUTS = FIXTURE_DIR / "model_outputs.json"
SOURCE = FIXTURE_DIR / "web2_internal_transfer.md"
SOURCE_ID_PLACEHOLDER = "{{SOURCE_ID}}"
INVALID_APPROVAL_MESSAGE = "Approval response is invalid."
INVALID_PLAN_MESSAGE = "Approved test plan is no longer valid."
INVALID_THREAD_MESSAGE = (
    "thread_id must be a 1-128 character identifier"
)


class StubBugClient:
    def __init__(self, bugs: list[RelatedBug]) -> None:
        self.bugs = bugs
        self.calls: list[list[str]] = []

    async def search_related(self, queries: list[str]) -> list[RelatedBug]:
        self.calls.append(queries)
        return self.bugs


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        test_base_url="https://wallet-test.local",
        allowed_test_origins=["https://wallet-test.local"],
        source_paths=[SOURCE],
        agent_db_path=tmp_path / "agent.sqlite3",
        artifacts_dir=tmp_path / "artifacts",
        agent_api_token="test-agent-token",
    )


def make_provider() -> FakeModelProvider:
    provider = FakeModelProvider.from_fixture(MODEL_OUTPUTS)
    source_id = load_sources([SOURCE]).documents[0].source_id
    for requirement in provider.outputs["extract_requirements"][
        "requirements"
    ]:
        requirement["source_refs"] = [
            source_id if ref == SOURCE_ID_PLACEHOLDER else ref
            for ref in requirement["source_refs"]
        ]
    for case in provider.outputs["generate_test_plan"]["cases"]:
        case["source_refs"] = [
            source_id if ref == SOURCE_ID_PLACEHOLDER else ref
            for ref in case["source_refs"]
        ]
    return provider


def make_graph(
    tmp_path: Path,
    *,
    provider: FakeModelProvider | None = None,
    bug_client: Any | None = None,
):
    provider = provider or make_provider()
    bug_client = bug_client or StubBugClient([])
    graph = build_graph(
        GraphDependencies(
            settings=make_settings(tmp_path),
            model_provider=provider,
            bug_client=bug_client,
        ),
        InMemorySaver(),
    )
    return graph, provider, bug_client


def invoke_until_review(graph, thread_id: str, message: str = "测试内部转账"):
    config = {"configurable": {"thread_id": thread_id}}
    result = asyncio.run(
        graph.ainvoke({"user_message": message}, config=config)
    )
    return config, result


def test_graph_pauses_for_review_and_approve_uses_server_plan_hash(
    tmp_path: Path,
) -> None:
    graph, provider, _ = make_graph(tmp_path)
    assert {
        (edge.source, edge.target)
        for edge in graph.get_graph().edges
    } == {
        ("__start__", "initialize_task"),
        ("initialize_task", "load_sources"),
        ("load_sources", "extract_requirements"),
        ("extract_requirements", "analyze_risks"),
        ("analyze_risks", "retrieve_bugs"),
        ("retrieve_bugs", "generate_test_plan"),
        ("generate_test_plan", "human_review"),
        ("human_review", "__end__"),
    }
    config, interrupted = invoke_until_review(graph, "chat-1")

    assert provider.calls == [
        "extract_requirements",
        "analyze_risks",
        "generate_test_plan",
    ]
    assert len(interrupted["__interrupt__"]) == 1
    interrupt_value = interrupted["__interrupt__"][0].value
    assert interrupt_value["status"] == "waiting_approval"
    assert interrupt_value["test_plan"] == interrupted["test_plan"]

    paused = graph.get_state(config)
    assert paused.next == ("human_review",)
    assert paused.values["status"] == "plan_validated"
    plan = GeneratedTestPlan.model_validate(paused.values["test_plan"])

    final = asyncio.run(
        graph.ainvoke(
            Command(
                resume={
                    "action": "approve",
                    "feedback": "",
                    "plan_hash": "0" * 64,
                }
            ),
            config=config,
        )
    )

    assert final["approval"] == {
        "action": "approve",
        "feedback": "",
        "plan_hash": plan_fingerprint(plan),
    }
    assert final["status"] == "approved"
    assert graph.get_state(config).next == ()


def test_graph_preserves_source_versions_and_checkpoint_thread_isolation(
    tmp_path: Path,
) -> None:
    graph, _, _ = make_graph(tmp_path)
    first_config, _ = invoke_until_review(graph, "chat-a", "第一个任务")
    second_config, _ = invoke_until_review(graph, "chat-b", "第二个任务")

    first = graph.get_state(first_config)
    second = graph.get_state(second_config)
    source_bytes = SOURCE.read_bytes()

    assert first.values["thread_id"] == "chat-a"
    assert second.values["thread_id"] == "chat-b"
    assert first.values["task_id"] != second.values["task_id"]
    assert first.values["user_message"] == "第一个任务"
    assert second.values["user_message"] == "第二个任务"
    assert first.values["source_versions"] == [
        {
            "source_id": first.values["source_versions"][0]["source_id"],
            "path": str(SOURCE.resolve()),
            "version": hashlib.sha256(source_bytes).hexdigest(),
        }
    ]
    assert first.values["source_versions"] == second.values["source_versions"]

    asyncio.run(
        graph.ainvoke(
            Command(resume={"action": "approve", "feedback": ""}),
            config=first_config,
        )
    )

    assert graph.get_state(first_config).values["status"] == "approved"
    assert graph.get_state(first_config).next == ()
    assert graph.get_state(second_config).values["status"] == "plan_validated"
    assert graph.get_state(second_config).next == ("human_review",)


def test_empty_bug_queries_never_reach_http_transport(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []

    def reject_network(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        raise AssertionError("empty Bug queries must not access the network")

    client = BugClient(
        "http://127.0.0.1:8765",
        transport=httpx.MockTransport(reject_network),
    )
    graph, _, _ = make_graph(tmp_path, bug_client=client)

    invoke_until_review(graph, "empty-bugs")

    assert requests == []


def test_bug_queries_are_wired_to_client_and_results_enter_checkpoint(
    tmp_path: Path,
) -> None:
    provider = make_provider()
    provider.outputs["analyze_risks"] = {
        "ambiguities": [],
        "risks": [],
        "bug_queries": ["内部转账", "重复提交"],
    }
    bug = RelatedBug(
        bug_id=1227,
        title="重复提交产生两笔交易",
        severity=1,
        status="closed",
        resolution="fixed",
    )
    client = StubBugClient([bug])
    graph, _, _ = make_graph(
        tmp_path,
        provider=provider,
        bug_client=client,
    )

    config, _ = invoke_until_review(graph, "bugs")
    checkpoint = graph.get_state(config)

    assert client.calls == [["内部转账", "重复提交"]]
    assert checkpoint.values["related_bugs"] == [bug.model_dump()]


def test_graph_rejects_model_plan_missing_a_golden_case(
    tmp_path: Path,
) -> None:
    provider = make_provider()
    provider.outputs["generate_test_plan"]["cases"] = provider.outputs[
        "generate_test_plan"
    ]["cases"][:-1]
    graph, _, _ = make_graph(tmp_path, provider=provider)
    config = {"configurable": {"thread_id": "invalid-plan"}}

    with pytest.raises(ValueError, match="missing Golden Set cases"):
        asyncio.run(
            graph.ainvoke(
                {"user_message": "生成内部转账方案"},
                config=config,
            )
        )

    checkpoint = graph.get_state(config)
    assert checkpoint.values["status"] == "bugs_retrieved"
    assert "test_plan" not in checkpoint.values
    assert checkpoint.next == ("generate_test_plan",)


@pytest.mark.parametrize(
    ("legal_action", "feedback", "expected_status"),
    [
        ("reject", "缺少边界场景", "reject"),
        ("approve", "", "approved"),
    ],
)
def test_invalid_reject_resume_can_recover_with_a_legal_decision(
    tmp_path: Path,
    legal_action: str,
    feedback: str,
    expected_status: str,
) -> None:
    graph, _, _ = make_graph(tmp_path)
    config, _ = invoke_until_review(
        graph,
        f"recover-{legal_action}",
    )

    invalid = asyncio.run(
        graph.ainvoke(
            Command(resume={"action": "reject", "feedback": "  "}),
            config=config,
        )
    )

    invalid_value = invalid["__interrupt__"][0].value
    assert invalid_value["status"] == "invalid_approval"
    assert invalid_value["message"] == INVALID_APPROVAL_MESSAGE
    assert "requires feedback" not in str(invalid_value)
    invalid_snapshot = graph.get_state(config)
    assert invalid_snapshot.tasks[0].interrupts

    final = asyncio.run(
        graph.ainvoke(
            Command(
                resume={
                    "action": legal_action,
                    "feedback": feedback,
                }
            ),
            config=config,
        )
    )

    assert final["status"] == expected_status
    assert graph.get_state(config).next == ()


@pytest.mark.parametrize("action", ["reject", "supplement"])
def test_reject_and_supplement_finish_task_with_feedback(
    tmp_path: Path,
    action: str,
) -> None:
    graph, _, _ = make_graph(tmp_path)
    config, _ = invoke_until_review(graph, f"valid-{action}")

    final = asyncio.run(
        graph.ainvoke(
            Command(resume={"action": action, "feedback": "请补充边界场景"}),
            config=config,
        )
    )

    assert final["status"] == action
    assert final["approval"] == {
        "action": action,
        "feedback": "请补充边界场景",
        "plan_hash": None,
    }


def test_approve_revalidates_tampered_plan_and_can_then_cancel(
    tmp_path: Path,
) -> None:
    graph, _, _ = make_graph(tmp_path)
    config, _ = invoke_until_review(graph, "tampered-plan")
    tampered = graph.get_state(config).values["test_plan"].copy()
    tampered["cases"] = [
        case
        for case in tampered["cases"]
        if case["case_id"] != "TC-OTI-006"
    ]
    graph.update_state(config, {"test_plan": tampered})

    invalid = asyncio.run(
        graph.ainvoke(
            Command(resume={"action": "approve", "feedback": ""}),
            config=config,
        )
    )

    invalid_value = invalid["__interrupt__"][0].value
    assert invalid_value["status"] == "invalid_approval"
    assert invalid_value["message"] == INVALID_PLAN_MESSAGE
    assert "TC-OTI-006" not in str(invalid_value)
    assert graph.get_state(config).values["status"] == "plan_validated"
    assert graph.get_state(config).tasks[0].interrupts

    cancelled = asyncio.run(
        graph.ainvoke(
            Command(resume={"action": "cancel", "feedback": ""}),
            config=config,
        )
    )

    assert cancelled["status"] == "cancel"
    assert cancelled["approval"]["action"] == "cancel"
    assert graph.get_state(config).next == ()


def test_graph_rejects_unknown_requirement_source_ref(
    tmp_path: Path,
) -> None:
    provider = make_provider()
    provider.outputs["extract_requirements"]["requirements"][0][
        "source_refs"
    ] = ["secret-fake-requirement-ref"]
    graph, _, _ = make_graph(tmp_path, provider=provider)

    with pytest.raises(
        ValueError,
        match="requirement contains unknown source_ref",
    ) as caught:
        invoke_until_review(graph, "bad-requirement-ref")

    assert "secret-fake-requirement-ref" not in str(caught.value)


def test_graph_rejects_unknown_non_inferred_risk_source_ref(
    tmp_path: Path,
) -> None:
    provider = make_provider()
    provider.outputs["analyze_risks"]["risks"] = [
        {
            "risk_id": "RISK-001",
            "description": "重复提交风险",
            "severity": "high",
            "source_refs": ["secret-fake-risk-ref"],
            "inferred": False,
        }
    ]
    graph, _, _ = make_graph(tmp_path, provider=provider)

    with pytest.raises(
        ValueError,
        match="risk contains unknown source_ref",
    ) as caught:
        invoke_until_review(graph, "bad-risk-ref")

    assert "secret-fake-risk-ref" not in str(caught.value)


@pytest.mark.parametrize(
    "bad_ref",
    [
        "secret-fake-plan-ref",
        "人工基准:TC-OTI-999",
    ],
)
def test_graph_rejects_unknown_or_untrusted_plan_source_ref(
    tmp_path: Path,
    bad_ref: str,
) -> None:
    provider = make_provider()
    provider.outputs["generate_test_plan"]["cases"][0][
        "source_refs"
    ].append(bad_ref)
    graph, _, _ = make_graph(tmp_path, provider=provider)

    with pytest.raises(
        ValueError,
        match="test plan contains unknown source_ref",
    ) as caught:
        invoke_until_review(graph, "bad-plan-ref")

    assert bad_ref not in str(caught.value)


def test_graph_allows_exact_source_ref_for_a_retrieved_bug(
    tmp_path: Path,
) -> None:
    provider = make_provider()
    provider.outputs["analyze_risks"]["bug_queries"] = ["重复提交"]
    provider.outputs["generate_test_plan"]["cases"][5][
        "source_refs"
    ].append("BUG-1227")
    bug = RelatedBug(
        bug_id=1227,
        title="重复提交产生两笔交易",
        severity=1,
        status="closed",
        resolution="fixed",
    )
    client = StubBugClient([bug])
    graph, _, _ = make_graph(
        tmp_path,
        provider=provider,
        bug_client=client,
    )

    config, interrupted = invoke_until_review(graph, "known-bug-ref")

    assert client.calls == [["重复提交"]]
    assert graph.get_state(config).values["related_bugs"] == [
        bug.model_dump()
    ]
    assert interrupted["__interrupt__"][0].value["status"] == (
        "waiting_approval"
    )

    approved = asyncio.run(
        graph.ainvoke(
            Command(resume={"action": "approve", "feedback": ""}),
            config=config,
        )
    )
    assert approved["status"] == "approved"


@pytest.mark.parametrize(
    "bad_ref",
    [
        "BUG-9999",
        "BUG-not-retrieved",
    ],
)
def test_graph_rejects_bug_ref_not_present_in_related_bugs(
    tmp_path: Path,
    bad_ref: str,
) -> None:
    provider = make_provider()
    provider.outputs["analyze_risks"]["bug_queries"] = ["重复提交"]
    provider.outputs["generate_test_plan"]["cases"][5][
        "source_refs"
    ].append(bad_ref)
    bug = RelatedBug(
        bug_id=1227,
        title="重复提交产生两笔交易",
        severity=1,
        status="closed",
        resolution="fixed",
    )
    graph, _, _ = make_graph(
        tmp_path,
        provider=provider,
        bug_client=StubBugClient([bug]),
    )

    with pytest.raises(
        ValueError,
        match="test plan contains unknown source_ref",
    ) as caught:
        invoke_until_review(graph, f"unknown-bug-{bad_ref[-4:]}")

    assert bad_ref not in str(caught.value)


def test_approve_rejects_bug_ref_added_after_pause_when_bug_was_not_retrieved(
    tmp_path: Path,
) -> None:
    provider = make_provider()
    provider.outputs["analyze_risks"]["bug_queries"] = ["重复提交"]
    bug = RelatedBug(
        bug_id=1227,
        title="重复提交产生两笔交易",
        severity=1,
        status="closed",
        resolution="fixed",
    )
    graph, _, _ = make_graph(
        tmp_path,
        provider=provider,
        bug_client=StubBugClient([bug]),
    )
    config, _ = invoke_until_review(graph, "tampered-bug-ref")
    tampered = deepcopy(graph.get_state(config).values["test_plan"])
    tampered["cases"][0]["source_refs"].append("BUG-9999")
    graph.update_state(config, {"test_plan": tampered})

    invalid = asyncio.run(
        graph.ainvoke(
            Command(resume={"action": "approve", "feedback": ""}),
            config=config,
        )
    )

    invalid_value = invalid["__interrupt__"][0].value
    assert invalid_value["status"] == "invalid_approval"
    assert invalid_value["message"] == INVALID_PLAN_MESSAGE
    assert "BUG-9999" not in str(invalid_value)

    cancelled = asyncio.run(
        graph.ainvoke(
            Command(resume={"action": "cancel", "feedback": ""}),
            config=config,
        )
    )
    assert cancelled["status"] == "cancel"


def test_inferred_risk_without_source_ref_is_allowed(
    tmp_path: Path,
) -> None:
    provider = make_provider()
    provider.outputs["analyze_risks"]["risks"] = [
        {
            "risk_id": "RISK-INFERRED",
            "description": "模型推断风险",
            "severity": "low",
            "source_refs": [],
            "inferred": True,
        }
    ]
    graph, _, _ = make_graph(tmp_path, provider=provider)

    config, interrupted = invoke_until_review(graph, "inferred-risk")

    assert interrupted["__interrupt__"][0].value["status"] == (
        "waiting_approval"
    )
    assert graph.get_state(config).values["risks"]["risks"][0][
        "source_refs"
    ] == []


@pytest.mark.parametrize(
    "thread_id",
    [
        "",
        None,
        {},
        "a" * 129,
        "contains space",
    ],
)
def test_ainvoke_rejects_invalid_thread_id_before_graph_access(
    tmp_path: Path,
    thread_id: Any,
) -> None:
    graph, _, _ = make_graph(tmp_path)
    config = {"configurable": {"thread_id": thread_id}}

    with pytest.raises(ValueError) as caught:
        asyncio.run(
            graph.ainvoke(
                {"user_message": "不应执行"},
                config=config,
            )
        )

    assert str(caught.value) == INVALID_THREAD_MESSAGE
    if str(thread_id):
        assert str(thread_id) not in str(caught.value)


@pytest.mark.parametrize(
    "method_name",
    [
        "get_state",
        "update_state",
        "aget_state",
        "aupdate_state",
    ],
)
def test_state_entry_points_reject_invalid_thread_id(
    tmp_path: Path,
    method_name: str,
) -> None:
    graph, _, _ = make_graph(tmp_path)
    config = {"configurable": {"thread_id": None}}

    with pytest.raises(ValueError, match=f"^{INVALID_THREAD_MESSAGE}$"):
        if method_name == "get_state":
            graph.get_state(config)
        elif method_name == "update_state":
            graph.update_state(config, {"status": "tampered"})
        elif method_name == "aget_state":
            asyncio.run(graph.aget_state(config))
        else:
            asyncio.run(
                graph.aupdate_state(
                    config,
                    {"status": "tampered"},
                )
            )


def test_prompt_allowlist_rejects_paths_and_documents_safety_contract() -> None:
    with pytest.raises(ValueError, match="unknown prompt"):
        read_prompt("../README")
    with pytest.raises(ValueError, match="unknown prompt"):
        read_prompt("/tmp/external")

    generation_prompt = read_prompt("generate_test_plan")
    for action in (
        "open_internal_transfer",
        "fill_recipient",
        "fill_amount",
        "submit",
        "complete_security_verification",
    ):
        assert action in generation_prompt
    for case_id in (
        "TC-OTI-001",
        "TC-OTI-002",
        "TC-OTI-003",
        "TC-OTI-004",
        "TC-OTI-005",
        "TC-OTI-006",
    ):
        assert case_id in generation_prompt
    assert "fixed value `10`" in generation_prompt
    assert "`recipient_account`" in generation_prompt
    assert "`amount_above_available_balance`" in generation_prompt
    assert "`BUG-<bug_id>`" in generation_prompt
    assert "Do not generate Python or Playwright code" in generation_prompt

    assert read_prompt("extract_requirements").strip()
    assert read_prompt("analyze_risks").strip()
    assert read_prompt("classify_failure").strip()
