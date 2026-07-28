from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
from langgraph.types import Command

from agent_service.api import create_app
from agent_service.config import Settings
from agent_service.model_provider import FakeModelProvider
from agent_service.schemas import RelatedBug
from agent_service.sources import load_sources


FIXTURE_DIR = Path(__file__).parent / "fixtures"
MODEL_OUTPUTS = FIXTURE_DIR / "model_outputs.json"
SOURCE = FIXTURE_DIR / "web2_internal_transfer.md"
SOURCE_ID_PLACEHOLDER = "{{SOURCE_ID}}"
TOKEN = "test-agent-token"
AUTH_HEADERS = {"Authorization": f"Bearer {TOKEN}"}


class StubBugClient:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    async def search_related(self, queries: list[str]) -> list[RelatedBug]:
        self.calls.append(queries)
        return []


class FailingModelProvider:
    async def generate_structured(self, **_: Any) -> Any:
        raise AssertionError("a resumed task must not call the model")


def make_settings(tmp_path: Path, *, token: str = TOKEN) -> Settings:
    return Settings(
        test_base_url="https://wallet-test.local",
        allowed_test_origins=["https://wallet-test.local"],
        source_paths=[SOURCE],
        agent_db_path=tmp_path / "agent.sqlite3",
        artifacts_dir=tmp_path / "artifacts",
        agent_api_token=token,
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


@asynccontextmanager
async def app_client(app):
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as client:
            yield client


def test_health_is_public_but_agent_routes_require_token(tmp_path: Path) -> None:
    app = create_app(
        settings=make_settings(tmp_path),
        model_provider=make_provider(),
        bug_client=StubBugClient(),
    )
    assert TOKEN not in repr(app.user_middleware)

    async def exercise() -> None:
        async with app_client(app) as client:
            health = await client.get("/health")
            missing = await client.get("/agent/tasks/chat-1")
            wrong = await client.post(
                "/agent/messages",
                json={"thread_id": "chat-1", "message": "测试内部转账"},
                headers={"Authorization": "Bearer wrong-secret-value"},
            )

        assert health.status_code == 200
        assert health.json()["status"] == "ok"
        assert TOKEN not in health.text
        assert missing.status_code == 401
        assert wrong.status_code == 401
        assert "wrong-secret-value" not in wrong.text
        assert TOKEN not in wrong.text

    asyncio.run(exercise())


def test_bearer_scheme_is_case_insensitive_and_ascii_only(
    tmp_path: Path,
) -> None:
    app = create_app(
        settings=make_settings(tmp_path),
        model_provider=make_provider(),
        bug_client=StubBugClient(),
    )

    async def exercise() -> None:
        async with app_client(app) as client:
            lowercase = await client.get(
                "/agent/tasks/not-created",
                headers={"Authorization": f"bearer {TOKEN}"},
            )
            non_ascii = await client.get(
                "/agent/tasks/not-created",
                headers=[
                    (
                        b"authorization",
                        "Bearer 令牌".encode("utf-8"),
                    )
                ],
            )
            duplicate = await client.get(
                "/agent/tasks/not-created",
                headers=[
                    (b"authorization", f"Bearer {TOKEN}".encode()),
                    (b"authorization", f"Bearer {TOKEN}".encode()),
                ],
            )

        assert lowercase.status_code == 404
        assert non_ascii.status_code == 401
        assert duplicate.status_code == 401
        assert "令牌" not in non_ascii.text
        assert TOKEN not in duplicate.text

    asyncio.run(exercise())


@pytest.mark.parametrize("configured_token", ["令牌", "invalid token", "\ttoken"])
def test_invalid_configured_token_makes_agent_routes_unavailable(
    tmp_path: Path,
    configured_token: str,
) -> None:
    app = create_app(
        settings=make_settings(tmp_path, token=configured_token),
        model_provider=make_provider(),
        bug_client=StubBugClient(),
    )

    async def exercise() -> None:
        async with app_client(app) as client:
            response = await client.get(
                "/agent/tasks/chat-1",
                headers={"Authorization": "Bearer valid-ascii-token"},
            )
        assert response.status_code == 503
        assert configured_token not in response.text

    asyncio.run(exercise())


def test_agent_routes_return_503_when_token_is_not_configured(
    tmp_path: Path,
) -> None:
    app = create_app(
        settings=make_settings(tmp_path, token=""),
        model_provider=make_provider(),
        bug_client=StubBugClient(),
    )

    async def exercise() -> None:
        async with app_client(app) as client:
            response = await client.post(
                "/agent/messages",
                json={"thread_id": "chat-1", "message": "测试内部转账"},
                headers={"Authorization": "Bearer attacker-value"},
            )
        assert response.status_code == 503
        assert "attacker-value" not in response.text

    asyncio.run(exercise())


@pytest.mark.parametrize(
    ("configured_token", "authorization", "expected_status"),
    [
        (TOKEN, None, 401),
        (TOKEN, "Bearer wrong-token", 401),
        ("", "Bearer any-token", 503),
    ],
)
def test_auth_middleware_precedes_malformed_json_validation(
    tmp_path: Path,
    configured_token: str,
    authorization: str | None,
    expected_status: int,
) -> None:
    app = create_app(
        settings=make_settings(tmp_path, token=configured_token),
        model_provider=make_provider(),
        bug_client=StubBugClient(),
    )

    async def exercise() -> None:
        headers = {"Content-Type": "application/json"}
        if authorization is not None:
            headers["Authorization"] = authorization
        async with app_client(app) as client:
            response = await client.post(
                "/agent/messages",
                content=b"{malformed-json",
                headers=headers,
            )
        assert response.status_code == expected_status
        assert "malformed-json" not in response.text
        if authorization:
            assert authorization not in response.text

    asyncio.run(exercise())


def test_valid_auth_reaches_malformed_json_validation(tmp_path: Path) -> None:
    app = create_app(
        settings=make_settings(tmp_path),
        model_provider=make_provider(),
        bug_client=StubBugClient(),
    )

    async def exercise() -> None:
        async with app_client(app) as client:
            response = await client.post(
                "/agent/messages",
                content=b"{malformed-json",
                headers={
                    **AUTH_HEADERS,
                    "Content-Type": "application/json",
                },
            )
        assert response.status_code == 422

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "payload",
    [
        {"thread_id": "", "message": "测试"},
        {"thread_id": "-leading", "message": "测试"},
        {"thread_id": "bad/thread", "message": "测试"},
        {"thread_id": "a" * 129, "message": "测试"},
        {"thread_id": "chat-1", "message": ""},
        {"thread_id": "chat-1", "message": "   "},
        {"thread_id": "chat-1", "message": "x" * 4001},
    ],
)
def test_message_boundary_is_rejected_before_graph(
    tmp_path: Path,
    payload: dict[str, str],
) -> None:
    provider = make_provider()
    app = create_app(
        settings=make_settings(tmp_path),
        model_provider=provider,
        bug_client=StubBugClient(),
    )

    async def exercise() -> None:
        async with app_client(app) as client:
            response = await client.post(
                "/agent/messages",
                json=payload,
                headers=AUTH_HEADERS,
            )
        assert response.status_code == 422
        assert provider.calls == []

    asyncio.run(exercise())


def test_task_reaches_review_and_get_uses_safe_projection(
    tmp_path: Path,
) -> None:
    app = create_app(
        settings=make_settings(tmp_path),
        model_provider=make_provider(),
        bug_client=StubBugClient(),
    )

    async def exercise() -> None:
        async with app_client(app) as client:
            created = await client.post(
                "/agent/messages",
                json={"thread_id": "chat-safe", "message": "测试内部转账"},
                headers=AUTH_HEADERS,
            )
            fetched = await client.get(
                "/agent/tasks/chat-safe",
                headers=AUTH_HEADERS,
            )

        assert created.status_code == 200
        assert created.json()["status"] == "waiting_approval"
        assert fetched.status_code == 200
        body = fetched.json()
        assert set(body) == {
            "thread_id",
            "task_id",
            "status",
            "waiting",
            "interrupts",
        }
        assert body["thread_id"] == "chat-safe"
        assert body["status"] == "waiting_approval"
        assert body["waiting"] is True
        assert body["interrupts"] == [{"status": "waiting_approval"}]
        serialized = fetched.text
        for forbidden in (
            "source_text",
            "source_versions",
            "requirements",
            "test_plan",
            "TEST_TRANSACTION_PASSWORD",
            TOKEN,
        ):
            assert forbidden not in serialized

    asyncio.run(exercise())


def test_task_projection_rejects_tampered_status_and_task_id(
    tmp_path: Path,
) -> None:
    secret = "checkpoint-secret-value"
    app = create_app(
        settings=make_settings(tmp_path),
        model_provider=make_provider(),
        bug_client=StubBugClient(),
    )

    async def exercise() -> None:
        async with app_client(app) as client:
            await client.post(
                "/agent/messages",
                json={"thread_id": "tampered-state", "message": "测试内部转账"},
                headers=AUTH_HEADERS,
            )
            await client.post(
                "/agent/messages",
                json={"thread_id": "tampered-state", "message": "取消"},
                headers=AUTH_HEADERS,
            )
            await app.state.graph.aupdate_state(
                {"configurable": {"thread_id": "tampered-state"}},
                {
                    "status": secret,
                    "task_id": secret,
                },
            )
            response = await client.get(
                "/agent/tasks/tampered-state",
                headers=AUTH_HEADERS,
            )

        assert response.status_code == 200
        assert response.json()["status"] == "unknown"
        assert response.json()["task_id"] is None
        assert secret not in response.text

    asyncio.run(exercise())


def test_waiting_thread_rejects_normal_message_and_empty_feedback(
    tmp_path: Path,
) -> None:
    provider = make_provider()
    app = create_app(
        settings=make_settings(tmp_path),
        model_provider=provider,
        bug_client=StubBugClient(),
    )

    async def exercise() -> None:
        async with app_client(app) as client:
            await client.post(
                "/agent/messages",
                json={"thread_id": "chat-review", "message": "测试内部转账"},
                headers=AUTH_HEADERS,
            )
            calls_before = list(provider.calls)
            ordinary = await client.post(
                "/agent/messages",
                json={"thread_id": "chat-review", "message": "再生成一次"},
                headers=AUTH_HEADERS,
            )
            empty_reject = await client.post(
                "/agent/messages",
                json={"thread_id": "chat-review", "message": "驳回：   "},
                headers=AUTH_HEADERS,
            )
            empty_supplement = await client.post(
                "/agent/messages",
                json={"thread_id": "chat-review", "message": "补充："},
                headers=AUTH_HEADERS,
            )
            current = await client.get(
                "/agent/tasks/chat-review",
                headers=AUTH_HEADERS,
            )

        assert ordinary.status_code == 409
        assert empty_reject.status_code == 422
        assert empty_supplement.status_code == 422
        assert provider.calls == calls_before
        assert current.json()["waiting"] is True
        assert current.json()["status"] == "waiting_approval"

    asyncio.run(exercise())


@pytest.mark.parametrize(
    ("command", "expected_status"),
    [
        ("批准", "approved"),
        ("取消", "cancel"),
        ("驳回：缺少边界值", "reject"),
        ("补充：补充自转账场景", "supplement"),
    ],
)
def test_api_parses_review_decisions(
    tmp_path: Path,
    command: str,
    expected_status: str,
) -> None:
    app = create_app(
        settings=make_settings(tmp_path),
        model_provider=make_provider(),
        bug_client=StubBugClient(),
    )

    async def exercise() -> None:
        async with app_client(app) as client:
            await client.post(
                "/agent/messages",
                json={
                    "thread_id": f"review-{expected_status}",
                    "message": "测试内部转账",
                },
                headers=AUTH_HEADERS,
            )
            response = await client.post(
                "/agent/messages",
                json={
                    "thread_id": f"review-{expected_status}",
                    "message": command,
                },
                headers=AUTH_HEADERS,
            )
        assert response.status_code == 200
        assert response.json()["status"] == expected_status
        assert response.json()["waiting"] is False

    asyncio.run(exercise())


def test_nonwaiting_thread_rejects_decision_and_repeated_new_message(
    tmp_path: Path,
) -> None:
    app = create_app(
        settings=make_settings(tmp_path),
        model_provider=make_provider(),
        bug_client=StubBugClient(),
    )

    async def exercise() -> None:
        async with app_client(app) as client:
            decision = await client.post(
                "/agent/messages",
                json={"thread_id": "new-thread", "message": "批准"},
                headers=AUTH_HEADERS,
            )
            await client.post(
                "/agent/messages",
                json={"thread_id": "done-thread", "message": "测试内部转账"},
                headers=AUTH_HEADERS,
            )
            await client.post(
                "/agent/messages",
                json={"thread_id": "done-thread", "message": "取消"},
                headers=AUTH_HEADERS,
            )
            repeated = await client.post(
                "/agent/messages",
                json={"thread_id": "done-thread", "message": "重新生成"},
                headers=AUTH_HEADERS,
            )

        assert decision.status_code == 409
        assert repeated.status_code == 409

    asyncio.run(exercise())


def test_waiting_detection_uses_task_interrupts_even_when_next_is_empty(
    tmp_path: Path,
) -> None:
    app = create_app(
        settings=make_settings(tmp_path),
        model_provider=make_provider(),
        bug_client=StubBugClient(),
    )

    async def exercise() -> None:
        async with app_client(app) as client:
            await client.post(
                "/agent/messages",
                json={"thread_id": "invalid-resume", "message": "测试内部转账"},
                headers=AUTH_HEADERS,
            )
            config = {"configurable": {"thread_id": "invalid-resume"}}
            await app.state.graph.ainvoke(
                Command(
                    resume={
                        "action": "reject",
                        "feedback": " ",
                    }
                ),
                config=config,
            )
            snapshot = await app.state.graph.aget_state(config)
            response = await client.get(
                "/agent/tasks/invalid-resume",
                headers=AUTH_HEADERS,
            )

        assert snapshot.next == ()
        assert snapshot.tasks[0].interrupts
        assert response.status_code == 200
        assert response.json()["waiting"] is True
        assert response.json()["status"] == "invalid_approval"
        assert response.json()["interrupts"] == [
            {
                "status": "invalid_approval",
                "message": "审批输入或方案无效，请重新操作。",
            }
        ]

    asyncio.run(exercise())


def test_two_threads_are_isolated_when_started_concurrently(
    tmp_path: Path,
) -> None:
    app = create_app(
        settings=make_settings(tmp_path),
        model_provider=make_provider(),
        bug_client=StubBugClient(),
    )

    async def exercise() -> None:
        async with app_client(app) as client:
            first, second = await asyncio.gather(
                client.post(
                    "/agent/messages",
                    json={"thread_id": "parallel-a", "message": "任务 A"},
                    headers=AUTH_HEADERS,
                ),
                client.post(
                    "/agent/messages",
                    json={"thread_id": "parallel-b", "message": "任务 B"},
                    headers=AUTH_HEADERS,
                ),
            )

        assert first.status_code == second.status_code == 200
        assert first.json()["task_id"] != second.json()["task_id"]
        assert first.json()["thread_id"] == "parallel-a"
        assert second.json()["thread_id"] == "parallel-b"

    asyncio.run(exercise())


def test_concurrent_duplicate_new_message_creates_only_one_task(
    tmp_path: Path,
) -> None:
    app = create_app(
        settings=make_settings(tmp_path),
        model_provider=make_provider(),
        bug_client=StubBugClient(),
    )

    async def exercise() -> None:
        async with app_client(app) as client:
            responses = await asyncio.gather(
                *[
                    client.post(
                        "/agent/messages",
                        json={
                            "thread_id": "same-thread",
                            "message": "测试内部转账",
                        },
                        headers=AUTH_HEADERS,
                    )
                    for _ in range(2)
                ]
            )

        assert sorted(item.status_code for item in responses) == [200, 409]

    asyncio.run(exercise())


def test_sqlite_checkpoint_recovers_after_app_restart(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    first_app = create_app(
        settings=settings,
        model_provider=make_provider(),
        bug_client=StubBugClient(),
    )

    async def start_task() -> str:
        async with app_client(first_app) as client:
            response = await client.post(
                "/agent/messages",
                json={"thread_id": "restart-thread", "message": "测试内部转账"},
                headers=AUTH_HEADERS,
            )
            assert response.status_code == 200
            return response.json()["task_id"]

    task_id = asyncio.run(start_task())

    second_app = create_app(
        settings=settings,
        model_provider=FailingModelProvider(),
        bug_client=StubBugClient(),
    )

    async def resume_task() -> None:
        async with app_client(second_app) as client:
            before = await client.get(
                "/agent/tasks/restart-thread",
                headers=AUTH_HEADERS,
            )
            approved = await client.post(
                "/agent/messages",
                json={"thread_id": "restart-thread", "message": "批准"},
                headers=AUTH_HEADERS,
            )

        assert before.status_code == 200
        assert before.json()["task_id"] == task_id
        assert before.json()["waiting"] is True
        assert approved.status_code == 200
        assert approved.json()["task_id"] == task_id
        assert approved.json()["status"] == "approved"

    asyncio.run(resume_task())


def test_startup_storage_failure_is_explicit_and_redacted(
    tmp_path: Path,
) -> None:
    secret = "storage-secret-value"

    @asynccontextmanager
    async def failing_factory(_: Path):
        raise OSError(secret)
        yield

    app = create_app(
        settings=make_settings(tmp_path),
        model_provider=make_provider(),
        bug_client=StubBugClient(),
        checkpointer_factory=failing_factory,
    )

    async def exercise() -> None:
        with pytest.raises(
            RuntimeError,
            match="failed to initialize Agent checkpoint storage",
        ) as caught:
            async with app.router.lifespan_context(app):
                pass
        assert secret not in str(caught.value)
        assert caught.value.__cause__ is None

    asyncio.run(exercise())


def test_run_agent_script_uses_uvicorn_factory_with_one_worker() -> None:
    project_root = Path(__file__).parents[2]
    script = (project_root / "scripts" / "run_agent.sh").read_text(
        encoding="utf-8"
    )
    readme = (project_root / "README.md").read_text(encoding="utf-8")

    assert "agent_service.api:create_app --factory" in script
    assert "--workers" not in script
    assert "`ENABLE_VALVE_ENCRYPTION=true`" in readme
    assert "`WEBUI_SECRET_KEY`" in readme
    assert "http://host.docker.internal:8770" in readme
    assert "单 worker" in readme
