from __future__ import annotations

import asyncio
import re
import secrets
from collections.abc import Callable
from contextlib import (
    AbstractAsyncContextManager,
    AsyncExitStack,
    asynccontextmanager,
)
from pathlib import Path
from typing import Annotated, Any, Protocol
from weakref import WeakValueDictionary

from fastapi import FastAPI, HTTPException, Request
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.types import Command
from pydantic import (
    BaseModel,
    ConfigDict,
    SecretStr,
    StringConstraints,
    field_validator,
)
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from agent_service.bug_client import BugClient
from agent_service.config import Settings, load_settings
from agent_service.graph.build import (
    GraphDependencies,
    build_graph,
    validate_thread_id,
)
from agent_service.model_provider import ModelProvider, OllamaProvider
from agent_service.schemas import ApprovalDecision


MAX_MESSAGE_LENGTH = 4000
SAFE_INVALID_APPROVAL_MESSAGE = "审批输入或方案无效，请重新操作。"
SAFE_STATE_STATUSES = frozenset(
    {
        "initialized",
        "sources_loaded",
        "requirements_extracted",
        "risks_analyzed",
        "bugs_retrieved",
        "plan_validated",
        "approved",
        "reject",
        "supplement",
        "cancel",
    }
)
TASK_ID_PATTERN = re.compile(r"^TASK-[0-9a-f]{12}$")
BEARER_TOKEN_PATTERN = re.compile(rb"^[A-Za-z0-9._~+/-]+=*$")
MAX_AUTHORIZATION_BYTES = 4096
CheckpointerFactory = Callable[
    [Path],
    AbstractAsyncContextManager[Any],
]


class BugSearchClient(Protocol):
    async def search_related(self, queries: list[str]) -> list[Any]: ...


class AgentMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    thread_id: str
    message: Annotated[
        str,
        StringConstraints(
            strip_whitespace=True,
            min_length=1,
            max_length=MAX_MESSAGE_LENGTH,
        ),
    ]

    @field_validator("thread_id")
    @classmethod
    def thread_id_matches_graph_boundary(cls, value: str) -> str:
        return validate_thread_id(value)


def _configured_token_bytes(token: str) -> bytes | None:
    try:
        encoded = token.encode("ascii")
    except UnicodeEncodeError:
        return None
    if (
        not encoded
        or len(encoded) > MAX_AUTHORIZATION_BYTES
        or BEARER_TOKEN_PATTERN.fullmatch(encoded) is None
    ):
        return None
    return encoded


def _is_agent_path(scope: Scope) -> bool:
    path = scope.get("path")
    return (
        isinstance(path, str)
        and (path == "/agent" or path.startswith("/agent/"))
    )


def _has_valid_authorization(
    scope: Scope,
    expected_token: bytes,
) -> bool:
    authorization_values = [
        value
        for name, value in scope.get("headers", ())
        if name.lower() == b"authorization"
    ]
    if len(authorization_values) != 1:
        return False
    authorization = authorization_values[0]
    if (
        len(authorization) > MAX_AUTHORIZATION_BYTES + len(b"Bearer ")
        or any(byte > 0x7F for byte in authorization)
    ):
        return False
    scheme, separator, supplied_token = authorization.partition(b" ")
    if (
        separator != b" "
        or scheme.lower() != b"bearer"
        or BEARER_TOKEN_PATTERN.fullmatch(supplied_token) is None
    ):
        return False
    return secrets.compare_digest(supplied_token, expected_token)


class AgentAuthMiddleware:
    def __init__(self, app: ASGIApp, *, token: SecretStr) -> None:
        self.app = app
        self.expected_token = _configured_token_bytes(
            token.get_secret_value()
        )

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        if scope["type"] != "http" or not _is_agent_path(scope):
            await self.app(scope, receive, send)
            return

        if self.expected_token is None:
            response = JSONResponse(
                status_code=503,
                content={"detail": "Agent API token is not configured"},
            )
        elif not _has_valid_authorization(scope, self.expected_token):
            response = JSONResponse(
                status_code=401,
                content={"detail": "invalid Agent API token"},
                headers={"WWW-Authenticate": "Bearer"},
            )
        else:
            await self.app(scope, receive, send)
            return
        await response(scope, receive, send)


def _default_checkpointer_factory(
    path: Path,
) -> AbstractAsyncContextManager[Any]:
    return AsyncSqliteSaver.from_conn_string(str(path))


def _pending_interrupt_values(snapshot: Any) -> list[Any]:
    values: list[Any] = []
    for task in getattr(snapshot, "tasks", ()):
        for pending in getattr(task, "interrupts", ()):
            values.append(getattr(pending, "value", None))
    return values


def safe_interrupts(snapshot: Any) -> list[dict[str, str]]:
    rendered: list[dict[str, str]] = []
    for value in _pending_interrupt_values(snapshot):
        status = value.get("status") if isinstance(value, dict) else None
        if status == "waiting_approval":
            rendered.append({"status": "waiting_approval"})
        elif status == "invalid_approval":
            rendered.append(
                {
                    "status": "invalid_approval",
                    "message": SAFE_INVALID_APPROVAL_MESSAGE,
                }
            )
        else:
            rendered.append({"status": "waiting_input"})
    return rendered


def _is_waiting(snapshot: Any) -> bool:
    return bool(_pending_interrupt_values(snapshot))


def _safe_status(snapshot: Any) -> str:
    interrupts = safe_interrupts(snapshot)
    if interrupts:
        return interrupts[-1]["status"]
    values = snapshot.values if isinstance(snapshot.values, dict) else {}
    status = values.get("status")
    return status if status in SAFE_STATE_STATUSES else "unknown"


def parse_decision(message: str) -> ApprovalDecision | None:
    text = message.strip()
    if text == "批准":
        return ApprovalDecision(action="approve")
    if text == "取消":
        return ApprovalDecision(action="cancel")
    for prefix, action in (
        ("驳回：", "reject"),
        ("补充：", "supplement"),
    ):
        if text.startswith(prefix):
            feedback = text.removeprefix(prefix).strip()
            if not feedback:
                raise HTTPException(
                    status_code=422,
                    detail=f"{prefix[:-1]}内容不能为空",
                )
            return ApprovalDecision(action=action, feedback=feedback)
    return None


def _safe_task_projection(thread_id: str, snapshot: Any) -> dict[str, Any]:
    values = snapshot.values if isinstance(snapshot.values, dict) else {}
    task_id = values.get("task_id")
    return {
        "thread_id": thread_id,
        "task_id": (
            task_id
            if isinstance(task_id, str)
            and TASK_ID_PATTERN.fullmatch(task_id)
            else None
        ),
        "status": _safe_status(snapshot),
        "waiting": _is_waiting(snapshot),
        "interrupts": safe_interrupts(snapshot),
    }


def render_chat_message(snapshot: Any) -> str:
    projected = _safe_task_projection(
        str(snapshot.values.get("thread_id", "")),
        snapshot,
    )
    task_id = projected["task_id"] or "未知任务"
    status = projected["status"]
    if projected["waiting"]:
        if status == "invalid_approval":
            return (
                f"任务 {task_id} 的审批输入或方案无效。"
                "请重新回复“批准”、“驳回：原因”、"
                "“补充：内容”或“取消”。"
            )
        plan = snapshot.values.get("test_plan")
        cases = plan.get("cases") if isinstance(plan, dict) else None
        count = len(cases) if isinstance(cases, list) else 0
        return (
            f"任务 {task_id} 已生成 {count} 条测试用例，等待审批。"
            "请回复“批准”、“驳回：原因”、“补充：内容”或“取消”。"
        )
    return f"任务 {task_id} 当前状态：{status}"


async def _thread_lock(app: FastAPI, thread_id: str) -> asyncio.Lock:
    async with app.state.thread_locks_guard:
        lock = app.state.thread_locks.get(thread_id)
        if lock is None:
            lock = asyncio.Lock()
            app.state.thread_locks[thread_id] = lock
        return lock


def create_app(
    *,
    settings: Settings | None = None,
    model_provider: ModelProvider | None = None,
    bug_client: BugSearchClient | None = None,
    checkpointer_factory: CheckpointerFactory | None = None,
) -> FastAPI:
    configured = settings or load_settings()
    provider = model_provider or OllamaProvider(
        base_url=configured.ollama_base_url,
        model=configured.ollama_model,
        retry_limit=configured.model_retry_limit,
    )
    configured_bug_client = bug_client or BugClient(
        configured.bug_service_url
    )
    factory = checkpointer_factory or _default_checkpointer_factory

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.thread_locks = WeakValueDictionary()
        app.state.thread_locks_guard = asyncio.Lock()
        stack = AsyncExitStack()
        try:
            configured.agent_db_path.parent.mkdir(
                parents=True,
                exist_ok=True,
            )
            checkpointer = await stack.enter_async_context(
                factory(configured.agent_db_path)
            )
            await checkpointer.setup()
            app.state.graph = build_graph(
                GraphDependencies(
                    settings=configured,
                    model_provider=provider,
                    bug_client=configured_bug_client,
                ),
                checkpointer,
            )
        except asyncio.CancelledError:
            await stack.aclose()
            raise
        except Exception:
            await stack.aclose()
            raise RuntimeError(
                "failed to initialize Agent checkpoint storage"
            ) from None
        try:
            yield
        finally:
            await stack.aclose()

    app = FastAPI(
        title="Local AI Test Agent",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.add_middleware(
        AgentAuthMiddleware,
        token=configured.agent_api_token,
    )

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "model_provider": type(provider).__name__,
            "model": configured.ollama_model,
            "cloud_model_calls": 0,
        }

    @app.post("/agent/messages")
    async def messages(
        message: AgentMessage,
        request: Request,
    ) -> dict[str, Any]:
        graph = request.app.state.graph
        config = {"configurable": {"thread_id": message.thread_id}}
        lock = await _thread_lock(request.app, message.thread_id)
        async with lock:
            snapshot = await graph.aget_state(config)
            waiting = _is_waiting(snapshot)
            decision = parse_decision(message.message)

            if decision is not None:
                if not waiting:
                    raise HTTPException(
                        status_code=409,
                        detail="thread is not waiting for approval",
                    )
                await graph.ainvoke(
                    Command(resume=decision.model_dump()),
                    config=config,
                )
            else:
                if snapshot.values:
                    detail = (
                        "thread is waiting for approval"
                        if waiting
                        else "thread already has a task"
                    )
                    raise HTTPException(status_code=409, detail=detail)
                await graph.ainvoke(
                    {
                        "thread_id": message.thread_id,
                        "user_message": message.message,
                    },
                    config=config,
                )

            current = await graph.aget_state(config)
            response = _safe_task_projection(message.thread_id, current)
            response["message"] = render_chat_message(current)
            return response

    @app.get("/agent/tasks/{thread_id}")
    async def task(
        thread_id: str,
        request: Request,
    ) -> dict[str, Any]:
        try:
            validated_thread_id = validate_thread_id(thread_id)
        except ValueError:
            raise HTTPException(
                status_code=422,
                detail="invalid thread_id",
            ) from None
        graph = request.app.state.graph
        snapshot = await graph.aget_state(
            {"configurable": {"thread_id": validated_thread_id}}
        )
        if not snapshot.values:
            raise HTTPException(status_code=404, detail="task not found")
        return _safe_task_projection(validated_thread_id, snapshot)

    return app
