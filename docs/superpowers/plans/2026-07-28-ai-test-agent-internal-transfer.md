# AI Test Agent Internal Transfer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a fully local LangGraph-based test Agent that analyzes the Web2 internal-transfer requirement, retrieves historical bugs, generates a constrained test DSL, pauses for approval in Open WebUI, executes approved Playwright tests, and produces traceable reports.

**Architecture:** Open WebUI remains the chat surface and calls a thin Pipe Function. A separate FastAPI service owns LangGraph orchestration, local Ollama model access, SQLite checkpoints, deterministic validation, Playwright execution, network capture, assertions, and report artifacts. LLM nodes depend only on a replaceable `ModelProvider`; all safety, pass/fail, and execution decisions remain deterministic.

**Tech Stack:** Python 3.13, FastAPI 0.140.x, LangGraph 1.2.x, LangChain Core 1.5.x, LangChain Ollama 1.1.x, SQLite checkpoint 3.1.x, Playwright 1.61.x, Pydantic 2.13.x, pytest 9.x, httpx 0.28.x, Ollama `qwen3.5:9b`, Open WebUI v0.10.2.

## Global Constraints

- Runtime model calls must use local Ollama `qwen3.5:9b`; no cloud model endpoint is allowed in MVP.
- LangGraph nodes depend on `ModelProvider`, never directly on `ChatOllama`.
- Only allowlisted test-environment origins may be opened or called.
- No transfer action may execute before an explicit `approve` decision is checkpointed.
- Model output must pass Pydantic validation; retry at most twice and then pause.
- The DSL accepts only registered actions and assertions; arbitrary Python, shell, SQL, URLs, and selectors are rejected.
- Credentials live only in ignored `.env` and ignored Playwright storage state; never print or persist secret values.
- Every `/agent/*` request requires a local Bearer token; `/health` is the only
  unauthenticated Agent endpoint.
- SQLite stores checkpoints and compact metadata; screenshots, traces, HAR, and reports live under `artifacts/<task_id>/`.
- The six existing manual internal-transfer cases are the Golden Set and must reach 100% required-case coverage.
- Inferred cases must carry `inferred=true` and a non-empty rationale.
- Existing `bug_service` remains independently deployable; the Agent accesses it over HTTP.
- Agent and Playwright services start manually; no launch agent or boot-time service is added.

---

## Planned File Structure

```text
knowledge_base/
├── agent_service/
│   ├── __init__.py
│   ├── api.py
│   ├── artifacts.py
│   ├── bug_client.py
│   ├── config.py
│   ├── dsl.py
│   ├── model_provider.py
│   ├── reporting.py
│   ├── schemas.py
│   ├── sources.py
│   ├── graph/
│   │   ├── __init__.py
│   │   ├── build.py
│   │   ├── nodes.py
│   │   └── state.py
│   ├── execution/
│   │   ├── __init__.py
│   │   ├── assertions.py
│   │   ├── network.py
│   │   ├── runner.py
│   │   └── security.py
│   └── integrations/
│       ├── __init__.py
│       └── openwebui_pipe.py
├── prompts/
│   ├── analyze_risks.md
│   ├── classify_failure.md
│   ├── extract_requirements.md
│   └── generate_test_plan.md
├── tests/
│   ├── agent/
│   │   ├── fixtures/
│   │   │   ├── model_outputs.json
│   │   │   └── web2_internal_transfer.md
│   │   ├── test_api.py
│   │   ├── test_bug_client.py
│   │   ├── test_config.py
│   │   ├── test_dsl.py
│   │   ├── test_graph.py
│   │   ├── test_model_provider.py
│   │   ├── test_openwebui_pipe.py
│   │   ├── test_reporting.py
│   │   ├── test_runner.py
│   │   └── test_sources.py
│   └── e2e/
│       └── test_internal_transfer_agent.py
├── scripts/
│   ├── run_agent.sh
│   └── run_internal_transfer_demo.py
├── artifacts/
├── data/
├── .env
├── .gitignore
├── pyproject.toml
├── requirements.txt
└── README.md
```

---

### Task 1: Repository Baseline and Reproducible Environment

**Files:**
- Create: `pyproject.toml`
- Create: `.env`
- Create: `scripts/run_agent.sh`
- Modify: `.gitignore`
- Modify: `requirements.txt`
- Modify: `README.md`
- Test: `tests/agent/test_config.py`

**Interfaces:**
- Consumes: existing project root and existing `bug_service`.
- Produces: `agent_service.config.Settings`, reproducible dependency installation, ignored local configuration.

- [x] **Step 1: Initialize Git and capture the existing baseline**

Run:

```bash
git init
git add .gitignore README.md requirements.txt bug_service tests docs
git commit -m "chore: establish knowledge base baseline"
```

Expected: `git status --short` returns no tracked changes. The ignored `.venv/` and `data/bugs.sqlite3` are not committed.

- [x] **Step 2: Write the failing settings test**

Create `tests/agent/test_config.py`:

```python
from pathlib import Path

import pytest

from agent_service.config import Settings


def test_settings_accept_only_configured_test_origin(tmp_path: Path) -> None:
    settings = Settings(
        ollama_base_url="http://localhost:11434",
        ollama_model="qwen3.5:9b",
        bug_service_url="http://localhost:8765",
        test_base_url="https://wallet-test.local",
        allowed_test_origins=["https://wallet-test.local"],
        agent_db_path=tmp_path / "agent.sqlite3",
        artifacts_dir=tmp_path / "artifacts",
        source_paths=[],
    )

    assert settings.test_origin == "https://wallet-test.local"
    with pytest.raises(ValueError, match="not allowlisted"):
        settings.assert_safe_url("https://wallet.example.com/transfer")
```

Run:

```bash
.venv/bin/python -m pytest tests/agent/test_config.py -v
```

Expected: FAIL because `agent_service.config` does not exist.

Before implementation, extend the same test module with parameterized RED cases
for remote or malformed Ollama URLs, malformed allowlist origins, a
`TEST_BASE_URL` outside the allowlist, Playwright storage state outside
`playwright/.auth/`, `.env` token loading, and `SecretStr` masking. These cases
must fail against the minimal implementation and pass only after Step 4.

- [x] **Step 3: Add pinned dependency ranges and pytest configuration**

Append to `requirements.txt`:

```text
langchain-core>=1.5,<2
langchain-ollama>=1.1,<2
langgraph>=1.2,<2
langgraph-checkpoint-sqlite>=3.1,<4
playwright>=1.61,<2
```

Create `pyproject.toml`:

```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
pythonpath = ["."]
addopts = "-ra"
markers = [
  "e2e: requires the configured wallet test environment",
]
```

Run:

```bash
.venv/bin/pip install -r requirements.txt
.venv/bin/playwright install chromium
```

Expected: dependency installation succeeds and Playwright reports Chromium installed.

- [x] **Step 4: Implement settings and local configuration**

Create `agent_service/__init__.py` as an empty UTF-8 file.

Create `agent_service/config.py`:

```python
from __future__ import annotations

from pathlib import Path
from urllib.parse import SplitResult, urlsplit

from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator


LOCAL_OLLAMA_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def _parse_http_url(value: str, field_name: str) -> SplitResult:
    parts = urlsplit(value.strip())
    if parts.scheme.lower() not in {"http", "https"}:
        raise ValueError(f"{field_name} must use http or https")
    if not parts.hostname:
        raise ValueError(f"{field_name} must include a hostname")
    if parts.username is not None or parts.password is not None:
        raise ValueError(f"{field_name} must not include userinfo")
    try:
        parts.port
    except ValueError as exc:
        raise ValueError(f"{field_name} has an invalid port") from exc
    return parts


def _origin_from_parts(parts: SplitResult) -> str:
    hostname = parts.hostname or ""
    rendered_host = f"[{hostname}]" if ":" in hostname else hostname
    port = f":{parts.port}" if parts.port is not None else ""
    return f"{parts.scheme.lower()}://{rendered_host.lower()}{port}"


def _normalize_origin(value: str) -> str:
    parts = _parse_http_url(value, "allowed_test_origins")
    if parts.path not in {"", "/"} or parts.query or parts.fragment:
        raise ValueError(
            "allowed_test_origins entries must be pure origins without "
            "path, query, or fragment"
        )
    return _origin_from_parts(parts)


class Settings(BaseModel):
    ollama_base_url: str = "http://127.0.0.1:11434"
    ollama_model: str = "qwen3.5:9b"
    bug_service_url: str = "http://localhost:8765"
    test_base_url: str
    allowed_test_origins: list[str]
    agent_db_path: Path = Path("data/agent.sqlite3")
    artifacts_dir: Path = Path("artifacts")
    source_paths: list[Path] = Field(default_factory=list)
    playwright_storage_state: Path = Path("playwright/.auth/wallet.json")
    test_payer_account: SecretStr = SecretStr("")
    test_recipient_account: SecretStr = SecretStr("")
    test_transaction_password: SecretStr = SecretStr("")
    agent_api_token: SecretStr = SecretStr("")
    model_retry_limit: int = 2
    environment_retry_limit: int = 1

    @field_validator("ollama_base_url")
    @classmethod
    def ollama_endpoint_must_be_local(cls, value: str) -> str:
        parts = _parse_http_url(value, "ollama_base_url")
        if parts.hostname not in LOCAL_OLLAMA_HOSTS:
            raise ValueError("ollama_base_url must use a local hostname")
        return value.strip().rstrip("/")

    @field_validator("allowed_test_origins")
    @classmethod
    def origins_must_be_explicit(cls, value: list[str]) -> list[str]:
        normalized = [_normalize_origin(item) for item in value if item.strip()]
        if not normalized:
            raise ValueError("allowed_test_origins cannot be empty")
        return normalized

    @field_validator("playwright_storage_state")
    @classmethod
    def storage_state_must_be_in_ignored_directory(cls, value: Path) -> Path:
        if value.is_absolute() or ".." in value.parts:
            raise ValueError(
                "playwright_storage_state must be a relative path under "
                "playwright/.auth"
            )
        if value.parts[:2] != ("playwright", ".auth") or len(value.parts) < 3:
            raise ValueError(
                "playwright_storage_state must be a relative path under "
                "playwright/.auth"
            )
        return value

    @model_validator(mode="after")
    def test_url_must_use_allowed_origin(self) -> "Settings":
        origin = self.test_origin
        if origin not in self.allowed_test_origins:
            raise ValueError(
                f"TEST_BASE_URL origin {origin!r} is not in ALLOWED_TEST_ORIGINS"
            )
        return self

    @property
    def test_origin(self) -> str:
        return _origin_from_parts(_parse_http_url(self.test_base_url, "test_base_url"))

    def assert_safe_url(self, url: str) -> None:
        origin = _origin_from_parts(_parse_http_url(url, "url"))
        if origin not in self.allowed_test_origins:
            raise ValueError(f"URL origin {origin!r} is not allowlisted")


def load_settings(env_file: str | Path = ".env") -> Settings:
    from dotenv import dotenv_values

    values = dotenv_values(env_file)
    origins = [
        item.strip().rstrip("/")
        for item in str(values.get("ALLOWED_TEST_ORIGINS") or "").split(",")
        if item.strip()
    ]
    sources = [
        Path(item.strip())
        for item in str(values.get("AGENT_SOURCE_PATHS") or "").split(",")
        if item.strip()
    ]
    return Settings(
        ollama_base_url=str(
            values.get("OLLAMA_BASE_URL") or "http://127.0.0.1:11434"
        ),
        ollama_model=str(values.get("OLLAMA_MODEL") or "qwen3.5:9b"),
        bug_service_url=str(values.get("BUG_SERVICE_URL") or "http://localhost:8765"),
        test_base_url=str(values.get("TEST_BASE_URL") or ""),
        allowed_test_origins=origins,
        agent_db_path=Path(str(values.get("AGENT_DB_PATH") or "data/agent.sqlite3")),
        artifacts_dir=Path(str(values.get("ARTIFACTS_DIR") or "artifacts")),
        source_paths=sources,
        playwright_storage_state=Path(
            str(values.get("PLAYWRIGHT_STORAGE_STATE") or "playwright/.auth/wallet.json")
        ),
        test_payer_account=str(values.get("TEST_PAYER_ACCOUNT") or ""),
        test_recipient_account=str(values.get("TEST_RECIPIENT_ACCOUNT") or ""),
        test_transaction_password=str(
            values.get("TEST_TRANSACTION_PASSWORD") or ""
        ),
        agent_api_token=str(values.get("AGENT_API_TOKEN") or ""),
    )
```

Create `.env` with non-secret defaults and empty credential slots:

```dotenv
OLLAMA_BASE_URL=http://127.0.0.1:11434
OLLAMA_MODEL=qwen3.5:9b
BUG_SERVICE_URL=http://localhost:8765
TEST_BASE_URL=
ALLOWED_TEST_ORIGINS=
AGENT_DB_PATH=data/agent.sqlite3
ARTIFACTS_DIR=artifacts
PLAYWRIGHT_STORAGE_STATE=playwright/.auth/wallet.json
AGENT_SOURCE_PATHS=/Users/maoyijiu/Documents/tg-work/测试用例/wallet-web2-test-cases.md,/Users/maoyijiu/Documents/tg-work/测试用例/wallet-web2-test-cases-review.md
TEST_PAYER_ACCOUNT=
TEST_RECIPIENT_ACCOUNT=
TEST_TRANSACTION_PASSWORD=
AGENT_API_TOKEN=
```

Add to `.gitignore`:

```gitignore
.env
.DS_Store
.idea/
artifacts/
data/agent.sqlite3
data/agent.sqlite3-*
playwright/.auth/
test-results/
```

- [x] **Step 5: Add the manual start script and verify**

Create `scripts/run_agent.sh`:

```bash
#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
exec .venv/bin/uvicorn agent_service.api:create_app --factory \
  --host 0.0.0.0 --port 8770 --no-access-log
```

Run:

```bash
chmod +x scripts/run_agent.sh
.venv/bin/python -m pytest tests/agent/test_config.py -v
```

Expected: PASS.

- [x] **Step 6: Commit the environment baseline**

```bash
git add .gitignore README.md requirements.txt pyproject.toml scripts/run_agent.sh agent_service tests/agent/test_config.py
git add docs/superpowers/plans/2026-07-28-ai-test-agent-internal-transfer.md
git add docs/superpowers/specs/2026-07-28-ai-test-agent-internal-transfer-design.md
git commit -m "build: add local agent development environment"
```

---

### Task 2: Domain Schemas, DSL, and Safety Validation

**Files:**
- Create: `agent_service/schemas.py`
- Create: `agent_service/dsl.py`
- Create: `agent_service/execution/__init__.py`
- Create: `agent_service/execution/security.py`
- Test: `tests/agent/test_dsl.py`

**Interfaces:**
- Consumes: `Settings.assert_safe_url`.
- Produces: `RequirementSet`, `RiskAnalysis`, `TestPlan`, `ApprovalDecision`, `validate_test_plan(plan)`.

- [x] **Step 1: Write failing DSL validation tests**

Create `tests/agent/test_dsl.py`:

```python
import pytest
from pydantic import ValidationError

from agent_service.dsl import validate_test_plan
from agent_service.schemas import TestCase, TestPlan, TestStep


def make_plan() -> TestPlan:
    return TestPlan(
        summary="Web2 internal transfer",
        cases=[
            TestCase(
                case_id="TC-OTI-002",
                title="内部转账成功",
                priority="P0",
                source_refs=["人工基准:TC-OTI-002"],
                inferred=False,
                rationale="Existing manual baseline",
                preconditions=["付款账号余额充足"],
                steps=[
                    TestStep(action="open_internal_transfer"),
                    TestStep(action="fill_amount", value="10"),
                    TestStep(action="submit"),
                ],
                assertions=[
                    {"type": "transfer_request_succeeded"},
                    {"type": "transaction_record_created"},
                ],
            )
        ],
    )


def test_valid_plan_is_accepted() -> None:
    assert validate_test_plan(make_plan()).cases[0].case_id == "TC-OTI-002"


def test_unknown_action_is_rejected() -> None:
    with pytest.raises(ValidationError):
        TestStep(action="run_shell", value="rm -rf /")


def test_inferred_case_requires_rationale() -> None:
    plan = make_plan()
    data = plan.cases[0].model_dump()
    data.update(case_id="AI-001", inferred=True, rationale="")
    with pytest.raises(ValidationError, match="rationale"):
        TestCase.model_validate(data)
```

Run:

```bash
.venv/bin/python -m pytest tests/agent/test_dsl.py -v
```

Expected: FAIL because the schemas do not exist.

- [x] **Step 2: Implement the domain schemas**

Create `agent_service/schemas.py` with these public models:

```python
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator


ActionName = Literal[
    "login",
    "open_internal_transfer",
    "select_asset",
    "fill_recipient",
    "fill_amount",
    "submit",
    "complete_security_verification",
    "refresh_transaction_history",
]

AssertionName = Literal[
    "page_loaded",
    "validation_message_equals",
    "request_not_sent",
    "transfer_request_succeeded",
    "payer_balance_decreased",
    "recipient_balance_increased",
    "transaction_record_created",
    "single_transaction_created",
]


class SourceRef(BaseModel):
    source_id: str
    path: str
    version: str


class Requirement(BaseModel):
    requirement_id: str
    statement: str
    source_refs: list[str]
    confirmed: bool


class RequirementSet(BaseModel):
    scope: Literal["web2_internal_transfer"]
    requirements: list[Requirement]
    missing_rules: list[str] = Field(default_factory=list)


class RiskItem(BaseModel):
    risk_id: str
    description: str
    severity: Literal["high", "medium", "low"]
    source_refs: list[str]
    inferred: bool


class RiskAnalysis(BaseModel):
    ambiguities: list[str]
    risks: list[RiskItem]
    bug_queries: list[str]


class RelatedBug(BaseModel):
    bug_id: int
    title: str
    severity: int | None
    status: str
    resolution: str


class TestStep(BaseModel):
    action: ActionName
    value: str | None = None
    source: str | None = None


class TestAssertion(BaseModel):
    type: AssertionName
    amount: str | None = None
    expected: str | None = None


class TestCase(BaseModel):
    case_id: str
    title: str
    priority: Literal["P0", "P1", "P2"]
    source_refs: list[str]
    inferred: bool
    rationale: str
    preconditions: list[str]
    steps: list[TestStep]
    assertions: list[TestAssertion]

    @model_validator(mode="after")
    def inferred_cases_need_rationale(self) -> "TestCase":
        if self.inferred and not self.rationale.strip():
            raise ValueError("inferred case requires rationale")
        return self


class TestPlan(BaseModel):
    summary: str
    cases: list[TestCase]


class ApprovalDecision(BaseModel):
    action: Literal["approve", "reject", "supplement", "cancel"]
    feedback: str = ""
```

- [x] **Step 3: Implement deterministic DSL validation**

Create `agent_service/dsl.py`:

```python
from __future__ import annotations

from agent_service.schemas import TestPlan


REQUIRED_BASELINE_IDS = {
    "TC-OTI-001",
    "TC-OTI-002",
    "TC-OTI-003",
    "TC-OTI-004",
    "TC-OTI-005",
    "TC-OTI-006",
}


def validate_test_plan(plan: TestPlan, *, require_golden_set: bool = False) -> TestPlan:
    ids = [case.case_id for case in plan.cases]
    if len(ids) != len(set(ids)):
        raise ValueError("test case ids must be unique")
    if require_golden_set:
        missing = sorted(REQUIRED_BASELINE_IDS.difference(ids))
        if missing:
            raise ValueError(f"missing Golden Set cases: {', '.join(missing)}")
    for case in plan.cases:
        if not case.steps:
            raise ValueError(f"{case.case_id} has no steps")
        if not case.assertions:
            raise ValueError(f"{case.case_id} has no assertions")
    return plan
```

Create `agent_service/execution/security.py`:

```python
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
    if not compare_digest(approval.plan_hash, plan_fingerprint(plan)):
        raise PermissionError("approved plan hash does not match current plan")
```

- [x] **Step 4: Run and extend the safety tests**

Add to `tests/agent/test_dsl.py`:

```python
from pathlib import Path

from agent_service.config import Settings
from agent_service.dsl import plan_fingerprint
from agent_service.execution.security import assert_execution_allowed
from agent_service.schemas import ApprovalDecision


def test_execution_is_blocked_before_approval(tmp_path: Path) -> None:
    settings = Settings(
        test_base_url="https://wallet-test.local",
        allowed_test_origins=["https://wallet-test.local"],
        source_paths=[],
        agent_db_path=tmp_path / "agent.sqlite3",
        artifacts_dir=tmp_path / "artifacts",
        agent_api_token="test-agent-token",
    )
    with pytest.raises(PermissionError, match="approved"):
        assert_execution_allowed(settings, make_plan(), None)

    assert_execution_allowed(
        settings,
        make_plan(),
        ApprovalDecision(
            action="approve",
            plan_hash=plan_fingerprint(make_plan()),
        ),
    )
```

Run:

```bash
.venv/bin/python -m pytest tests/agent/test_dsl.py -v
```

Expected: all tests PASS.

- [x] **Step 5: Commit schemas and safety rules**

```bash
git add agent_service/schemas.py agent_service/dsl.py agent_service/execution tests/agent/test_dsl.py
git commit -m "feat: define safe internal transfer test DSL"
```

---

### Task 3: Replaceable Local Model Provider

**Files:**
- Create: `agent_service/model_provider.py`
- Create: `tests/agent/fixtures/model_outputs.json`
- Test: `tests/agent/test_model_provider.py`

**Interfaces:**
- Consumes: any Pydantic output schema.
- Produces: `ModelProvider.generate_structured(task_type, prompt, schema)`, `OllamaProvider`, `FakeModelProvider`.

- [x] **Step 1: Write failing provider contract tests**

Create `tests/agent/test_model_provider.py`:

```python
import asyncio

import pytest

from agent_service.model_provider import FakeModelProvider, StructuredModelError
from agent_service.schemas import RequirementSet


def test_fake_provider_returns_validated_schema() -> None:
    provider = FakeModelProvider(
        {
            "extract_requirements": {
                "scope": "web2_internal_transfer",
                "requirements": [],
                "missing_rules": ["手续费规则"],
            }
        }
    )
    result = asyncio.run(
        provider.generate_structured(
            task_type="extract_requirements",
            prompt="extract",
            schema=RequirementSet,
        )
    )
    assert result.missing_rules == ["手续费规则"]


def test_provider_stops_after_two_retries() -> None:
    provider = FakeModelProvider(
        {"extract_requirements": {"invalid": True}},
        retry_limit=2,
    )
    with pytest.raises(StructuredModelError, match="3 attempts"):
        asyncio.run(
            provider.generate_structured(
                task_type="extract_requirements",
                prompt="extract",
                schema=RequirementSet,
            )
        )
```

Run:

```bash
.venv/bin/python -m pytest tests/agent/test_model_provider.py -v
```

Expected: FAIL because `model_provider.py` does not exist.

- [x] **Step 2: Implement the provider protocol and retry boundary**

Create `agent_service/model_provider.py`:

```python
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel, ValidationError

T = TypeVar("T", bound=BaseModel)


class StructuredModelError(RuntimeError):
    pass


class ModelProvider(Protocol):
    async def generate_structured(
        self,
        *,
        task_type: str,
        prompt: str,
        schema: type[T],
    ) -> T: ...


class FakeModelProvider:
    def __init__(self, outputs: dict[str, Any], retry_limit: int = 2):
        self.outputs = outputs
        self.retry_limit = retry_limit
        self.calls: list[str] = []

    @classmethod
    def from_fixture(cls, path: Path, retry_limit: int = 2) -> "FakeModelProvider":
        return cls(
            json.loads(path.read_text(encoding="utf-8")),
            retry_limit=retry_limit,
        )

    async def generate_structured(
        self,
        *,
        task_type: str,
        prompt: str,
        schema: type[T],
    ) -> T:
        last_error: ValidationError | None = None
        for _ in range(self.retry_limit + 1):
            self.calls.append(task_type)
            try:
                return schema.model_validate(self.outputs[task_type])
            except ValidationError as exc:
                last_error = exc
        raise StructuredModelError(
            f"{task_type} failed structured validation after "
            f"{self.retry_limit + 1} attempts: {last_error}"
        )
```

Create `tests/agent/fixtures/model_outputs.json`:

```json
{
  "extract_requirements": {
    "scope": "web2_internal_transfer",
    "requirements": [
      {
        "requirement_id": "REQ-001",
        "statement": "余额不足时禁止内部转账",
        "source_refs": ["SRC-001"],
        "confirmed": true
      }
    ],
    "missing_rules": []
  },
  "analyze_risks": {
    "ambiguities": [],
    "risks": [],
    "bug_queries": []
  },
  "generate_test_plan": {
    "summary": "Web2 internal transfer Golden Set",
    "cases": [
      {"case_id":"TC-OTI-001","title":"页面打开","priority":"P0","source_refs":["人工基准:TC-OTI-001"],"inferred":false,"rationale":"manual baseline","preconditions":[],"steps":[{"action":"open_internal_transfer"}],"assertions":[{"type":"page_loaded"}]},
      {"case_id":"TC-OTI-002","title":"转账成功","priority":"P0","source_refs":["人工基准:TC-OTI-002"],"inferred":false,"rationale":"manual baseline","preconditions":[],"steps":[{"action":"submit"}],"assertions":[{"type":"transfer_request_succeeded"}]},
      {"case_id":"TC-OTI-003","title":"收款人为空","priority":"P0","source_refs":["人工基准:TC-OTI-003"],"inferred":false,"rationale":"manual baseline","preconditions":[],"steps":[{"action":"fill_recipient","value":""}],"assertions":[{"type":"request_not_sent"}]},
      {"case_id":"TC-OTI-004","title":"金额非法","priority":"P0","source_refs":["人工基准:TC-OTI-004"],"inferred":false,"rationale":"manual baseline","preconditions":[],"steps":[{"action":"fill_amount","value":"0"}],"assertions":[{"type":"request_not_sent"}]},
      {"case_id":"TC-OTI-005","title":"余额不足","priority":"P0","source_refs":["人工基准:TC-OTI-005"],"inferred":false,"rationale":"manual baseline","preconditions":[],"steps":[{"action":"fill_amount","value":"999999999"}],"assertions":[{"type":"request_not_sent"}]},
      {"case_id":"TC-OTI-006","title":"重复提交","priority":"P0","source_refs":["人工基准:TC-OTI-006"],"inferred":false,"rationale":"manual baseline","preconditions":[],"steps":[{"action":"submit"},{"action":"submit"}],"assertions":[{"type":"single_transaction_created"}]}
    ]
  },
  "classify_failure": {
    "category": "product",
    "summary": "Deterministic assertion failed",
    "evidence_refs": ["execution_results.json"],
    "related_bug_ids": [],
    "recommended_action": "Review captured request and screenshot"
  }
}
```

- [x] **Step 3: Add the Ollama implementation without cloud fallback**

Append to `agent_service/model_provider.py`:

```python
class OllamaProvider:
    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        retry_limit: int = 2,
        temperature: float = 0.0,
    ):
        from langchain_ollama import ChatOllama

        if not base_url.startswith(("http://localhost", "http://127.0.0.1")):
            raise ValueError("MVP Ollama endpoint must be local")
        self.model = ChatOllama(
            base_url=base_url,
            model=model,
            temperature=temperature,
            num_ctx=16384,
        )
        self.retry_limit = retry_limit

    async def generate_structured(
        self,
        *,
        task_type: str,
        prompt: str,
        schema: type[T],
    ) -> T:
        runnable = self.model.with_structured_output(schema)
        current_prompt = prompt
        last_error: Exception | None = None
        for attempt in range(self.retry_limit + 1):
            try:
                result = await runnable.ainvoke(current_prompt)
                return result if isinstance(result, schema) else schema.model_validate(result)
            except Exception as exc:
                last_error = exc
                current_prompt = (
                    f"{prompt}\n\nPrevious output failed validation: {exc}. "
                    f"Return only data matching {schema.model_json_schema()}."
                )
        raise StructuredModelError(
            f"{task_type} failed after {self.retry_limit + 1} attempts: {last_error}"
        )
```

- [x] **Step 4: Verify fake and real provider boundaries**

Run:

```bash
.venv/bin/python -m pytest tests/agent/test_model_provider.py -v
.venv/bin/python -c "from agent_service.model_provider import OllamaProvider; OllamaProvider(base_url='http://127.0.0.1:11434', model='qwen3.5:9b')"
```

Expected: tests PASS and the constructor command exits with code 0 without calling the model.

- [x] **Step 5: Commit the provider abstraction**

```bash
git add agent_service/model_provider.py tests/agent/test_model_provider.py tests/agent/fixtures
git commit -m "feat: add replaceable local model provider"
```

---

### Task 4: Source Loading and Historical Bug Retrieval

**Files:**
- Create: `agent_service/sources.py`
- Create: `agent_service/bug_client.py`
- Create: `tests/agent/fixtures/web2_internal_transfer.md`
- Test: `tests/agent/test_sources.py`
- Test: `tests/agent/test_bug_client.py`

**Interfaces:**
- Consumes: configured source paths and `bug_service` HTTP API.
- Produces: `load_sources(paths) -> LoadedSources`, `BugClient.search_related(queries) -> list[RelatedBug]`.

- [x] **Step 1: Write failing source-loader tests**

Create `tests/agent/fixtures/web2_internal_transfer.md`:

```markdown
# Web2 内部转账

- 收款人不能为空。
- 转账金额必须大于 0。
- 余额不足时禁止转账。
- 重复点击只能生成一笔交易。
```

Create `tests/agent/test_sources.py`:

```python
from pathlib import Path

from agent_service.sources import load_sources


def test_source_loader_records_content_hash() -> None:
    path = Path("tests/agent/fixtures/web2_internal_transfer.md")
    loaded = load_sources([path])
    assert len(loaded.documents) == 1
    assert loaded.documents[0].path.endswith("web2_internal_transfer.md")
    assert len(loaded.documents[0].version) == 64
    assert "余额不足" in loaded.combined_text
```

Run:

```bash
.venv/bin/python -m pytest tests/agent/test_sources.py -v
```

Expected: FAIL because `sources.py` does not exist.

- [x] **Step 2: Implement deterministic source loading**

Create `agent_service/sources.py`:

```python
from __future__ import annotations

import hashlib
from pathlib import Path

from pydantic import BaseModel


class LoadedDocument(BaseModel):
    source_id: str
    path: str
    version: str
    content: str


class LoadedSources(BaseModel):
    documents: list[LoadedDocument]

    @property
    def combined_text(self) -> str:
        return "\n\n".join(
            f"## SOURCE {item.source_id}\n{item.content}" for item in self.documents
        )


def load_sources(paths: list[Path]) -> LoadedSources:
    documents: list[LoadedDocument] = []
    for index, path in enumerate(paths, start=1):
        resolved = path.expanduser().resolve()
        if resolved.suffix.lower() not in {".md", ".txt"}:
            raise ValueError(f"unsupported source type: {resolved.suffix}")
        content = resolved.read_text(encoding="utf-8")
        documents.append(
            LoadedDocument(
                source_id=f"SRC-{index:03d}",
                path=str(resolved),
                version=hashlib.sha256(content.encode("utf-8")).hexdigest(),
                content=content,
            )
        )
    return LoadedSources(documents=documents)
```

- [x] **Step 3: Write the bug-client contract test**

Create `tests/agent/test_bug_client.py`:

```python
import asyncio

import httpx

from agent_service.bug_client import BugClient


def test_bug_client_deduplicates_results() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/bugs"
        return httpx.Response(
            200,
            json={
                "count": 1,
                "bugs": [
                    {
                        "bug_id": 1227,
                        "title": "重复提交产生两笔记录",
                        "severity": 2,
                        "status": "closed",
                        "resolution": "fixed",
                    }
                ],
            },
        )

    client = BugClient(
        "http://127.0.0.1:8765",
        transport=httpx.MockTransport(handler),
    )
    bugs = asyncio.run(client.search_related(["内部转账", "重复提交"]))
    assert [item.bug_id for item in bugs] == [1227]
```

Run:

```bash
.venv/bin/python -m pytest tests/agent/test_bug_client.py -v
```

Expected: FAIL because `bug_client.py` does not exist.

- [x] **Step 4: Implement the async Bug API client**

Create `agent_service/bug_client.py`:

```python
from __future__ import annotations

import httpx

from agent_service.schemas import RelatedBug


class BugClient:
    def __init__(
        self,
        base_url: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.transport = transport

    async def search_related(self, queries: list[str]) -> list[RelatedBug]:
        by_id: dict[int, RelatedBug] = {}
        async with httpx.AsyncClient(
            base_url=self.base_url,
            transport=self.transport,
            timeout=10,
        ) as client:
            for query in queries:
                response = await client.get("/bugs", params={"keyword": query, "limit": 20})
                response.raise_for_status()
                for row in response.json().get("bugs", []):
                    bug = RelatedBug.model_validate(row)
                    by_id[bug.bug_id] = bug
        return sorted(by_id.values(), key=lambda item: item.bug_id, reverse=True)
```

- [x] **Step 5: Verify and commit data adapters**

Run:

```bash
.venv/bin/python -m pytest tests/agent/test_sources.py tests/agent/test_bug_client.py -v
```

Expected: all tests PASS.

```bash
git add agent_service/sources.py agent_service/bug_client.py tests/agent
git commit -m "feat: load versioned sources and related bugs"
```

---

### Task 5: LangGraph Analysis, Golden-Set Validation, and Human Interrupt

**Files:**
- Create: `agent_service/graph/__init__.py`
- Create: `agent_service/graph/state.py`
- Create: `agent_service/graph/nodes.py`
- Create: `agent_service/graph/build.py`
- Create: `prompts/extract_requirements.md`
- Create: `prompts/analyze_risks.md`
- Create: `prompts/generate_test_plan.md`
- Create: `prompts/classify_failure.md`
- Test: `tests/agent/test_graph.py`

**Interfaces:**
- Consumes: `Settings`, `ModelProvider`, `BugClient`, source loader, schemas.
- Produces: `build_graph(deps, checkpointer)`, resumable graph state keyed by `thread_id`.

- [x] **Step 1: Write the failing approval and resume graph test**

Create `tests/agent/test_graph.py`:

```python
import asyncio
from pathlib import Path

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from agent_service.bug_client import BugClient
from agent_service.config import Settings
from agent_service.graph.build import GraphDependencies, build_graph
from agent_service.model_provider import FakeModelProvider


def test_graph_pauses_for_review_and_resumes(tmp_path: Path) -> None:
    provider = FakeModelProvider(
        {
            "extract_requirements": {
                "scope": "web2_internal_transfer",
                "requirements": [],
                "missing_rules": [],
            },
            "analyze_risks": {
                "ambiguities": [],
                "risks": [],
                "bug_queries": [],
            },
            "generate_test_plan": {
                "summary": "baseline",
                "cases": [
                    {
                        "case_id": case_id,
                        "title": case_id,
                        "priority": "P0",
                        "source_refs": [f"人工基准:{case_id}"],
                        "inferred": False,
                        "rationale": "manual baseline",
                        "preconditions": [],
                        "steps": [{"action": "open_internal_transfer"}],
                        "assertions": [{"type": "page_loaded"}],
                    }
                    for case_id in (
                        "TC-OTI-001",
                        "TC-OTI-002",
                        "TC-OTI-003",
                        "TC-OTI-004",
                        "TC-OTI-005",
                        "TC-OTI-006",
                    )
                ],
            },
        }
    )
    settings = Settings(
        test_base_url="https://wallet-test.local",
        allowed_test_origins=["https://wallet-test.local"],
        source_paths=[Path("tests/agent/fixtures/web2_internal_transfer.md")],
        agent_db_path=tmp_path / "agent.sqlite3",
        artifacts_dir=tmp_path / "artifacts",
        agent_api_token="test-agent-token",
    )
    graph = build_graph(
        GraphDependencies(
            settings=settings,
            model_provider=provider,
            bug_client=BugClient("http://127.0.0.1:8765"),
        ),
        InMemorySaver(),
    )
    config = {"configurable": {"thread_id": "chat-1"}}
    asyncio.run(graph.ainvoke({"user_message": "测试内部转账"}, config=config))
    paused = graph.get_state(config)
    assert paused.next
    assert paused.values["status"] == "plan_validated"

    final = asyncio.run(
        graph.ainvoke(
            Command(resume={"action": "approve", "feedback": ""}),
            config=config,
        )
    )
    assert final["approval"]["action"] == "approve"
    assert final["status"] == "approved"
```

Run:

```bash
.venv/bin/python -m pytest tests/agent/test_graph.py -v
```

Expected: FAIL because the graph modules do not exist.

- [x] **Step 2: Define graph state and dependencies**

Create `agent_service/graph/state.py`:

```python
from __future__ import annotations

from typing import Any, TypedDict


class AgentState(TypedDict, total=False):
    task_id: str
    thread_id: str
    user_message: str
    status: str
    source_versions: list[dict[str, str]]
    source_text: str
    requirements: dict[str, Any]
    risks: dict[str, Any]
    related_bugs: list[dict[str, Any]]
    test_plan: dict[str, Any]
    approval: dict[str, str]
    errors: list[str]
```

Create `agent_service/graph/nodes.py` with a dependency container:

```python
from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

from langgraph.types import interrupt

from agent_service.bug_client import BugClient
from agent_service.config import Settings
from agent_service.dsl import plan_fingerprint, validate_test_plan
from agent_service.model_provider import ModelProvider
from agent_service.schemas import ApprovalDecision, RequirementSet, RiskAnalysis, TestPlan
from agent_service.sources import load_sources, read_prompt


@dataclass(frozen=True)
class GraphDependencies:
    settings: Settings
    model_provider: ModelProvider
    bug_client: BugClient


def initialize_task(state: dict) -> dict:
    return {
        "task_id": state.get("task_id") or f"TASK-{uuid4().hex[:12]}",
        "status": "initialized",
        "errors": [],
    }


def make_nodes(deps: GraphDependencies) -> dict[str, object]:
    async def load_sources_node(_: dict) -> dict:
        loaded = load_sources(deps.settings.source_paths)
        return {
            "source_text": loaded.combined_text,
            "source_versions": [
                item.model_dump(exclude={"content"}) for item in loaded.documents
            ],
            "status": "sources_loaded",
        }

    async def extract_requirements(state: dict) -> dict:
        result = await deps.model_provider.generate_structured(
            task_type="extract_requirements",
            prompt=f"{read_prompt('extract_requirements')}\n\n{state['source_text']}",
            schema=RequirementSet,
        )
        return {"requirements": result.model_dump(), "status": "requirements_extracted"}

    async def analyze_risks(state: dict) -> dict:
        result = await deps.model_provider.generate_structured(
            task_type="analyze_risks",
            prompt=(
                f"{read_prompt('analyze_risks')}\n\n"
                f"Requirements: {state['requirements']}"
            ),
            schema=RiskAnalysis,
        )
        return {"risks": result.model_dump(), "status": "risks_analyzed"}

    async def retrieve_bugs(state: dict) -> dict:
        queries = RiskAnalysis.model_validate(state["risks"]).bug_queries
        bugs = await deps.bug_client.search_related(queries)
        return {"related_bugs": [item.model_dump() for item in bugs]}

    async def generate_test_plan(state: dict) -> dict:
        prompt = (
            f"{read_prompt('generate_test_plan')}\n\n"
            f"Requirements: {state['requirements']}\n"
            f"Risks: {state['risks']}\n"
            f"Bugs: {state.get('related_bugs', [])}"
        )
        result = await deps.model_provider.generate_structured(
            task_type="generate_test_plan",
            prompt=prompt,
            schema=TestPlan,
        )
        plan = validate_test_plan(result, require_golden_set=True)
        return {"test_plan": plan.model_dump(), "status": "plan_validated"}

    def human_review(state: dict) -> dict:
        payload = dict(
            interrupt(
                {
                    "task_id": state["task_id"],
                    "status": "waiting_approval",
                    "test_plan": state["test_plan"],
                }
            )
        )
        if payload.get("action") == "approve":
            payload["plan_hash"] = plan_fingerprint(
                TestPlan.model_validate(state["test_plan"])
            )
        decision = ApprovalDecision.model_validate(payload)
        next_status = "approved" if decision.action == "approve" else decision.action
        return {"approval": decision.model_dump(), "status": next_status}

    return {
        "load_sources": load_sources_node,
        "extract_requirements": extract_requirements,
        "analyze_risks": analyze_risks,
        "retrieve_bugs": retrieve_bugs,
        "generate_test_plan": generate_test_plan,
        "human_review": human_review,
    }
```

- [x] **Step 3: Build the first graph through approval**

Create `agent_service/graph/build.py`:

```python
from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from agent_service.graph.nodes import GraphDependencies, initialize_task, make_nodes
from agent_service.graph.state import AgentState

__all__ = ["GraphDependencies", "build_graph"]


def build_graph(deps: GraphDependencies, checkpointer):
    nodes = make_nodes(deps)
    builder = StateGraph(AgentState)
    builder.add_node("initialize_task", initialize_task)
    for name, node in nodes.items():
        builder.add_node(name, node)
    builder.add_edge(START, "initialize_task")
    builder.add_edge("initialize_task", "load_sources")
    builder.add_edge("load_sources", "extract_requirements")
    builder.add_edge("extract_requirements", "analyze_risks")
    builder.add_edge("analyze_risks", "retrieve_bugs")
    builder.add_edge("retrieve_bugs", "generate_test_plan")
    builder.add_edge("generate_test_plan", "human_review")
    builder.add_edge("human_review", END)
    return builder.compile(checkpointer=checkpointer)
```

Create `agent_service/graph/__init__.py` as an empty file.

- [x] **Step 4: Store prompts as versioned files and wire them into nodes**

Create the four prompt files with explicit rules:

`prompts/extract_requirements.md`

```markdown
You are a software test analyst. Extract only Web2 internal-transfer requirements.
Every requirement must cite a supplied source ID. Mark unsupported statements as
missing rules instead of inventing them. Return the required structured schema.
```

`prompts/analyze_risks.md`

```markdown
Analyze internal-transfer security, balance, recipient, validation, idempotency,
and consistency risks. Separate confirmed rules from inference. Produce focused
historical Bug search queries and return the required structured schema.
```

`prompts/generate_test_plan.md`

```markdown
Generate the six mandatory TC-OTI baseline cases and evidence-based additional
cases. Use only allowed DSL actions and assertions. Mark every additional case
as inferred and explain its rationale. Return the required structured schema.
```

`prompts/classify_failure.md`

```markdown
Classify failure evidence as product, automation, environment, data, or unknown.
Do not override deterministic assertions. Return the required structured schema.
```

Append this fixed-directory helper to `agent_service/sources.py`; callers pass a
logical prompt name, never a model-provided path:

```python
PROMPTS_DIR = Path(__file__).resolve().parents[1] / "prompts"
ALLOWED_PROMPTS = {
    "extract_requirements",
    "analyze_risks",
    "generate_test_plan",
    "classify_failure",
}


def read_prompt(name: str) -> str:
    if name not in ALLOWED_PROMPTS:
        raise ValueError(f"unknown prompt: {name}")
    return (PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8")
```

- [x] **Step 5: Run graph tests and commit**

```bash
.venv/bin/python -m pytest tests/agent/test_graph.py -v
```

Expected: PASS and the first invocation contains an interrupt with `waiting_approval`.

```bash
git add agent_service/graph agent_service/sources.py prompts tests/agent/test_graph.py
git commit -m "feat: orchestrate analysis and human approval"
```

---

### Task 6: SQLite Checkpoints, Agent API, and Open WebUI Pipe

**Files:**
- Create: `agent_service/api.py`
- Create: `agent_service/integrations/__init__.py`
- Create: `agent_service/integrations/openwebui_pipe.py`
- Test: `tests/agent/test_api.py`
- Test: `tests/agent/test_openwebui_pipe.py`

**Interfaces:**
- Consumes: compiled graph and Open WebUI `chat_id`.
- Produces: `POST /agent/messages`, `GET /agent/tasks/{thread_id}`, Open WebUI `Pipe`.

- [x] **Step 1: Write failing API lifecycle tests**

Create `tests/agent/test_api.py`:

```python
import asyncio
from pathlib import Path

import httpx

from agent_service.api import create_app
from agent_service.config import Settings
from agent_service.model_provider import FakeModelProvider


def test_agent_api_starts_and_approves_task(tmp_path: Path) -> None:
    settings = Settings(
        test_base_url="https://wallet-test.local",
        allowed_test_origins=["https://wallet-test.local"],
        source_paths=[Path("tests/agent/fixtures/web2_internal_transfer.md")],
        agent_db_path=tmp_path / "agent.sqlite3",
        artifacts_dir=tmp_path / "artifacts",
        agent_api_token="test-agent-token",
    )
    provider = FakeModelProvider.from_fixture(
        Path("tests/agent/fixtures/model_outputs.json")
    )
    app = create_app(settings=settings, model_provider=provider)

    async def exercise_requests() -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            first = await client.post(
                "/agent/messages",
                json={"thread_id": "chat-1", "message": "测试内部转账"},
                headers={"Authorization": "Bearer test-agent-token"},
            )
            assert first.status_code == 200
            assert first.json()["status"] == "waiting_approval"

            approved = await client.post(
                "/agent/messages",
                json={"thread_id": "chat-1", "message": "批准"},
                headers={"Authorization": "Bearer test-agent-token"},
            )
            assert approved.status_code == 200
            assert approved.json()["status"] == "approved"

            missing = await client.post(
                "/agent/messages",
                json={"thread_id": "chat-2", "message": "测试内部转账"},
            )
            assert missing.status_code == 401

            wrong = await client.post(
                "/agent/messages",
                json={"thread_id": "chat-3", "message": "测试内部转账"},
                headers={"Authorization": "Bearer wrong-token"},
            )
            assert wrong.status_code == 401

    async def exercise() -> None:
        async with app.router.lifespan_context(app):
            await exercise_requests()

    asyncio.run(exercise())


def test_agent_api_refuses_unconfigured_token(tmp_path: Path) -> None:
    settings = Settings(
        test_base_url="https://wallet-test.local",
        allowed_test_origins=["https://wallet-test.local"],
        source_paths=[Path("tests/agent/fixtures/web2_internal_transfer.md")],
        agent_db_path=tmp_path / "agent.sqlite3",
        artifacts_dir=tmp_path / "artifacts",
    )
    app = create_app(
        settings=settings,
        model_provider=FakeModelProvider.from_fixture(
            Path("tests/agent/fixtures/model_outputs.json")
        ),
    )

    async def exercise_requests() -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as client:
            response = await client.post(
                "/agent/messages",
                json={"thread_id": "chat-1", "message": "测试内部转账"},
                headers={"Authorization": "Bearer any-token"},
            )
            assert response.status_code == 503

    async def exercise() -> None:
        async with app.router.lifespan_context(app):
            await exercise_requests()

    asyncio.run(exercise())
```

Run:

```bash
.venv/bin/python -m pytest tests/agent/test_api.py -v
```

Expected: FAIL because the Agent API does not exist.

- [x] **Step 2: Implement the API with persistent SQLite checkpoints**

FastAPI 的异步请求路径必须使用 `AsyncSqliteSaver`，并在应用 lifespan
内创建和关闭连接。状态读取使用 `await graph.aget_state(...)`；禁止在
事件循环中使用同步 `SqliteSaver`、同步 SQLite 连接或同步
`graph.get_state(...)`。

Create `agent_service/api.py`:

```python
from __future__ import annotations

from contextlib import asynccontextmanager
from secrets import compare_digest
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.types import Command
from pydantic import BaseModel

from agent_service.bug_client import BugClient
from agent_service.config import Settings, load_settings
from agent_service.graph.build import GraphDependencies, build_graph
from agent_service.model_provider import ModelProvider, OllamaProvider
from agent_service.schemas import ApprovalDecision


class AgentMessage(BaseModel):
    thread_id: str
    message: str


def safe_interrupts(snapshot: Any) -> list[dict[str, str]]:
    rendered: list[dict[str, str]] = []
    for task in snapshot.tasks:
        for pending in task.interrupts:
            value = pending.value
            status = value.get("status") if isinstance(value, dict) else None
            if status == "waiting_approval":
                rendered.append({"status": "waiting_approval"})
            elif status == "invalid_approval":
                rendered.append(
                    {
                        "status": "invalid_approval",
                        "message": "审批输入或方案无效，请重新操作。",
                    }
                )
    return rendered


def parse_decision(message: str) -> ApprovalDecision | None:
    text = message.strip()
    if text == "批准":
        return ApprovalDecision(action="approve")
    if text == "取消":
        return ApprovalDecision(action="cancel")
    if text.startswith("驳回："):
        return ApprovalDecision(action="reject", feedback=text.removeprefix("驳回：").strip())
    if text.startswith("补充："):
        return ApprovalDecision(
            action="supplement",
            feedback=text.removeprefix("补充：").strip(),
        )
    return None


def create_app(
    *,
    settings: Settings | None = None,
    model_provider: ModelProvider | None = None,
) -> FastAPI:
    configured = settings or load_settings()
    configured.agent_db_path.parent.mkdir(parents=True, exist_ok=True)
    provider = model_provider or OllamaProvider(
        base_url=configured.ollama_base_url,
        model=configured.ollama_model,
        retry_limit=configured.model_retry_limit,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async with AsyncSqliteSaver.from_conn_string(
            str(configured.agent_db_path)
        ) as checkpointer:
            await checkpointer.setup()
            app.state.graph = build_graph(
                GraphDependencies(
                    settings=configured,
                    model_provider=provider,
                    bug_client=BugClient(configured.bug_service_url),
                ),
                checkpointer,
            )
            yield

    app = FastAPI(title="Local AI Test Agent", version="0.1.0", lifespan=lifespan)

    async def require_agent_token(
        authorization: str | None = Header(default=None),
    ) -> None:
        expected = configured.agent_api_token.get_secret_value()
        if not expected:
            raise HTTPException(503, "AGENT_API_TOKEN is not configured")
        if not compare_digest(authorization or "", f"Bearer {expected}"):
            raise HTTPException(401, "invalid Agent API token")

    @app.get("/health")
    async def health() -> dict:
        return {
            "status": "ok",
            "model_provider": type(provider).__name__,
            "model": configured.ollama_model,
            "cloud_model_calls": 0,
        }

    @app.post(
        "/agent/messages",
        dependencies=[Depends(require_agent_token)],
    )
    async def messages(message: AgentMessage, request: Request) -> dict:
        graph = request.app.state.graph
        config = {"configurable": {"thread_id": message.thread_id}}
        snapshot = await graph.aget_state(config)
        current_interrupts = safe_interrupts(snapshot)
        interrupted = bool(current_interrupts)
        decision = parse_decision(message.message)
        if decision:
            if not interrupted:
                raise HTTPException(409, "thread is not waiting for approval")
            result = await graph.ainvoke(
                Command(resume=decision.model_dump()),
                config=config,
            )
        else:
            if snapshot.values and interrupted:
                raise HTTPException(409, "thread is waiting for approval")
            result = await graph.ainvoke(
                {
                    "thread_id": message.thread_id,
                    "user_message": message.message,
                },
                config=config,
            )
        state = await graph.aget_state(config)
        current_interrupts = safe_interrupts(state)
        interrupted = bool(current_interrupts)
        return {
            "thread_id": message.thread_id,
            "task_id": state.values.get("task_id"),
            "status": (
                current_interrupts[-1]["status"]
                if interrupted
                else result.get("status")
            ),
            "message": render_chat_message(state.values, interrupted=interrupted),
        }

    @app.get(
        "/agent/tasks/{thread_id}",
        dependencies=[Depends(require_agent_token)],
    )
    async def task(thread_id: str, request: Request) -> dict:
        graph = request.app.state.graph
        state = await graph.aget_state(
            {"configurable": {"thread_id": thread_id}}
        )
        if not state.values:
            raise HTTPException(404, "task not found")
        current_interrupts = safe_interrupts(state)
        return {
            "values": state.values,
            "next": list(state.next),
            "interrupts": current_interrupts,
            "status": (
                current_interrupts[-1]["status"]
                if current_interrupts
                else state.values.get("status")
            ),
            "waiting": bool(current_interrupts),
        }

    return app


def render_chat_message(state: dict, *, interrupted: bool) -> str:
    if interrupted:
        count = len(state.get("test_plan", {}).get("cases", []))
        return (
            f"任务 {state['task_id']} 已生成 {count} 条测试用例，等待审批。"
            "请回复“批准”、“驳回：原因”、“补充：内容”或“取消”。"
        )
    return f"任务 {state.get('task_id')} 当前状态：{state.get('status')}"
```

- [x] **Step 3: Implement the thin Open WebUI Pipe**

Create `agent_service/integrations/__init__.py` as an empty file.

Create `agent_service/integrations/openwebui_pipe.py`:

```python
from __future__ import annotations

import httpx
from pydantic import BaseModel, Field


class Pipe:
    class Valves(BaseModel):
        AGENT_BASE_URL: str = Field(
            default="http://host.docker.internal:8770",
            description="Local AI Test Agent service URL",
        )
        AGENT_API_TOKEN: str = Field(
            default="",
            description="Bearer token from the Agent service .env",
        )

    def __init__(self):
        self.valves = self.Valves()
        self.client_factory = httpx.AsyncClient

    async def pipe(
        self,
        body: dict,
        __metadata__: dict | None = None,
        __chat_id__: str | None = None,
    ) -> str:
        metadata = __metadata__ or {}
        chat_id = __chat_id__ or metadata.get("chat_id")
        message = metadata.get("user_prompt")
        if not message:
            messages = body.get("messages", [])
            message = messages[-1].get("content", "") if messages else ""
        if not chat_id:
            return "无法获取 Open WebUI 会话 ID，请从聊天界面调用测试 Agent。"
        async with self.client_factory(timeout=300) as client:
            response = await client.post(
                f"{self.valves.AGENT_BASE_URL.rstrip('/')}/agent/messages",
                json={"thread_id": chat_id, "message": message},
                headers={
                    "Authorization": f"Bearer {self.valves.AGENT_API_TOKEN}",
                },
            )
        if response.status_code >= 400:
            return f"测试 Agent 调用失败：{response.status_code} {response.text}"
        return response.json()["message"]
```

- [x] **Step 4: Add Pipe tests and persistent restart verification**

Create `tests/agent/test_openwebui_pipe.py`:

```python
import asyncio

import httpx

from agent_service.integrations.openwebui_pipe import Pipe


def test_pipe_forwards_chat_id_and_user_prompt() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = __import__("json").loads(request.content)
        captured["authorization"] = request.headers["Authorization"]
        return httpx.Response(200, json={"message": "等待审批"})

    pipe = Pipe()
    pipe.valves.AGENT_API_TOKEN = "test-agent-token"
    pipe.client_factory = lambda **kwargs: httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        timeout=kwargs["timeout"],
    )
    result = asyncio.run(
        pipe.pipe(
            {"messages": [{"role": "user", "content": "测试内部转账"}]},
            __metadata__={
                "chat_id": "chat-1",
                "user_prompt": "测试内部转账",
            },
        )
    )
    assert result == "等待审批"
    assert captured == {
        "body": {
            "thread_id": "chat-1",
            "message": "测试内部转账",
        },
        "authorization": "Bearer test-agent-token",
    }
```

Then run:

```bash
.venv/bin/python -m pytest tests/agent/test_api.py tests/agent/test_openwebui_pipe.py -v
```

Expected: PASS.

Run a manual persistence smoke:

```bash
export AGENT_API_TOKEN="$(
  .venv/bin/python -c \
  'from dotenv import dotenv_values; print(dotenv_values(".env")["AGENT_API_TOKEN"])'
)"
scripts/run_agent.sh
curl -sS -X POST http://localhost:8770/agent/messages \
  -H "Authorization: Bearer ${AGENT_API_TOKEN}" \
  -H 'Content-Type: application/json' \
  -d '{"thread_id":"manual-1","message":"测试内部转账"}'
```

Expected: JSON status `waiting_approval`. Stop and restart the Agent, then:

```bash
curl -sS -X POST http://localhost:8770/agent/messages \
  -H "Authorization: Bearer ${AGENT_API_TOKEN}" \
  -H 'Content-Type: application/json' \
  -d '{"thread_id":"manual-1","message":"批准"}'
```

Expected: the existing thread resumes rather than creating a new task.

- [ ] **Step 5: Commit API and Open WebUI integration**

```bash
git add agent_service/api.py agent_service/integrations tests/agent/test_api.py
git commit -m "feat: expose resumable agent through Open WebUI"
```

---

### Task 7: Playwright DSL Runner and Network Inventory

**Files:**
- Create: `agent_service/execution/network.py`
- Create: `agent_service/execution/runner.py`
- Test: `tests/agent/test_runner.py`

**Interfaces:**
- Consumes: approved `TestPlan`, account aliases, `Settings`.
- Produces: `ExecutionResult`, screenshots, trace ZIP, `NetworkInventory`.

- [x] **Step 1: Write failing runner tests against a local fixture page**

Create `tests/agent/test_runner.py`:

```python
import asyncio
from pathlib import Path

from playwright.async_api import async_playwright

from agent_service.execution.runner import RunnerContext, run_case
from agent_service.schemas import TestCase


def test_runner_executes_registered_actions_and_collects_api(tmp_path: Path) -> None:
    async def exercise():
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            browser_context = await browser.new_context()
            page = await browser_context.new_page()
            await page.route(
                "https://wallet-test.local/api/internal-transfer",
                lambda route: route.fulfill(
                    status=200,
                    content_type="application/json",
                    body='{"transaction_id":"tx-1","status":"success"}',
                ),
            )
            await page.set_content(
                """
                <input data-testid="recipient" />
                <input data-testid="amount" />
                <button data-testid="submit-transfer"
                  onclick="fetch('https://wallet-test.local/api/internal-transfer',
                    {method:'POST',headers:{'content-type':'application/json'},
                     body:JSON.stringify({recipient:'r',amount:'10'})})">
                  Submit
                </button>
                """
            )
            case = TestCase(
                case_id="TC-OTI-002",
                title="内部转账成功",
                priority="P0",
                source_refs=["人工基准:TC-OTI-002"],
                inferred=False,
                rationale="manual baseline",
                preconditions=[],
                steps=[
                    {"action":"fill_recipient","source":"recipient_account"},
                    {"action":"fill_amount","value":"10"},
                    {"action":"submit"}
                ],
                assertions=[{"type":"transfer_request_succeeded"}],
            )
            result = await run_case(
                case=case,
                context=RunnerContext(
                    page=page,
                    browser_context=browser_context,
                    artifacts_dir=tmp_path,
                    allowed_origin="https://wallet-test.local",
                    recipient_account="recipient@example.test",
                    transaction_password="secret",
                ),
            )
            await browser.close()
            return result

    result = asyncio.run(exercise())
    assert result.status == "passed"
    assert Path(result.trace_path).exists()
    assert result.network_inventory[0]["path"] == "/api/internal-transfer"
    assert result.network_inventory[0]["method"] == "POST"
```

Run:

```bash
.venv/bin/python -m pytest tests/agent/test_runner.py -v
```

Expected: FAIL because runner modules do not exist.

- [x] **Step 2: Implement normalized, redacted network collection**

Create `agent_service/execution/network.py`:

```python
from __future__ import annotations

from urllib.parse import urlsplit

from pydantic import BaseModel, Field


SENSITIVE_HEADERS = {"authorization", "cookie", "set-cookie", "x-api-key"}


class NetworkEntry(BaseModel):
    method: str
    path: str
    status: int | None
    duration_ms: int | None = None
    request_headers: dict[str, str] = Field(default_factory=dict)
    request_body: object | None = None
    response_body: object | None = None


def redact_headers(headers: dict[str, str]) -> dict[str, str]:
    return {
        key: "[REDACTED]" if key.lower() in SENSITIVE_HEADERS else value
        for key, value in headers.items()
    }


def normalized_path(url: str) -> str:
    parts = urlsplit(url)
    return parts.path or "/"
```

- [x] **Step 3: Implement the allowlisted action registry**

Create `agent_service/execution/runner.py` with:

```python
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from playwright.async_api import BrowserContext, Page
from pydantic import BaseModel

from agent_service.schemas import TestCase, TestStep


class ExecutionResult(BaseModel):
    case_id: str
    status: str
    error: str = ""
    screenshot_paths: list[str]
    trace_path: str
    network_inventory: list[dict]


@dataclass
class RunnerContext:
    page: Page
    browser_context: BrowserContext
    artifacts_dir: Path
    allowed_origin: str
    recipient_account: str
    transaction_password: str


async def execute_step(step: TestStep, context: RunnerContext) -> None:
    page = context.page
    if step.action == "open_internal_transfer":
        await page.get_by_test_id("internal-transfer").click()
    elif step.action == "select_asset":
        await page.get_by_test_id("asset-select").select_option(step.value)
    elif step.action == "fill_recipient":
        value = context.recipient_account if step.source == "recipient_account" else step.value
        await page.get_by_test_id("recipient").fill(value or "")
    elif step.action == "fill_amount":
        await page.get_by_test_id("amount").fill(step.value or "")
    elif step.action == "submit":
        await page.get_by_test_id("submit-transfer").click()
    elif step.action == "complete_security_verification":
        await page.get_by_test_id("transaction-password").fill(
            context.transaction_password
        )
        await page.get_by_test_id("confirm-security").click()
    elif step.action == "refresh_transaction_history":
        await page.get_by_test_id("transaction-history-refresh").click()
    elif step.action == "login":
        raise RuntimeError("login is handled by saved Playwright storage state")
    else:
        raise ValueError(f"unsupported action: {step.action}")
```

Append a concrete `run_case` implementation:

```python
import asyncio
import json
from urllib.parse import urlsplit

from agent_service.execution.network import (
    NetworkEntry,
    normalized_path,
    redact_headers,
)


async def run_case(case: TestCase, context: RunnerContext) -> ExecutionResult:
    context.artifacts_dir.mkdir(parents=True, exist_ok=True)
    screenshots: list[str] = []
    entries: list[NetworkEntry] = []
    capture_tasks: set[asyncio.Task] = set()

    async def capture(request) -> None:
        if f"{urlsplit(request.url).scheme}://{urlsplit(request.url).netloc}" != context.allowed_origin:
            return
        response = await request.response()
        request_body = None
        response_body = None
        try:
            request_body = request.post_data_json
        except Exception:
            request_body = None
        if response is not None:
            try:
                response_body = await response.json()
            except Exception:
                response_body = None
        entries.append(
            NetworkEntry(
                method=request.method,
                path=normalized_path(request.url),
                status=response.status if response else None,
                request_headers=redact_headers(dict(request.headers)),
                request_body=request_body,
                response_body=response_body,
            )
        )

    def schedule_capture(request) -> None:
        task = asyncio.create_task(capture(request))
        capture_tasks.add(task)
        task.add_done_callback(capture_tasks.discard)

    context.page.on("requestfinished", schedule_capture)
    trace_path = context.artifacts_dir / f"{case.case_id}-trace.zip"
    screenshot_path = context.artifacts_dir / f"{case.case_id}-final.png"
    await context.browser_context.tracing.start(screenshots=True, snapshots=True)
    try:
        for step in case.steps:
            await execute_step(step, context)
        await context.page.wait_for_timeout(200)
        if capture_tasks:
            await asyncio.gather(*list(capture_tasks))
        await context.page.screenshot(path=str(screenshot_path), full_page=True)
        screenshots.append(str(screenshot_path))
        status = "passed"
        error = ""
    except Exception as exc:
        await context.page.screenshot(path=str(screenshot_path), full_page=True)
        screenshots.append(str(screenshot_path))
        status = "failed"
        error = f"{type(exc).__name__}: {exc}"
    finally:
        await context.browser_context.tracing.stop(path=str(trace_path))
        context.page.remove_listener("requestfinished", schedule_capture)
    return ExecutionResult(
        case_id=case.case_id,
        status=status,
        error=error,
        screenshot_paths=screenshots,
        trace_path=str(trace_path),
        network_inventory=[item.model_dump() for item in entries],
    )
```

- [x] **Step 4: Verify runner isolation**

Append these concrete checks to `tests/agent/test_runner.py`:

```python
from agent_service.execution.network import redact_headers


def test_sensitive_network_headers_are_redacted() -> None:
    assert redact_headers(
        {
            "Authorization": "Bearer secret-token",
            "Cookie": "session=secret-cookie",
            "Content-Type": "application/json",
        }
    ) == {
        "Authorization": "[REDACTED]",
        "Cookie": "[REDACTED]",
        "Content-Type": "application/json",
    }


def test_runner_source_contains_no_dynamic_code_execution() -> None:
    source = Path("agent_service/execution/runner.py").read_text(encoding="utf-8")
    for forbidden in ("eval(", "exec(", "page.evaluate(", "subprocess"):
        assert forbidden not in source
```

Extend the local fixture test with a second routed request to
`https://outside.invalid/telemetry`, then assert no inventory entry contains
`/telemetry`. Add a missing-selector case and assert:

```python
assert result.status == "failed"
assert Path(result.screenshot_paths[0]).exists()
assert Path(result.trace_path).exists()
```

Run:

```bash
.venv/bin/python -m pytest tests/agent/test_runner.py -v
```

Expected: all tests PASS.

- [x] **Step 5: Commit the deterministic runner**

```bash
git add agent_service/execution tests/agent/test_runner.py
git commit -m "feat: execute approved DSL with Playwright"
```

---

### Task 8: Assertions, Balance Consistency, and Failure Classification

**Files:**
- Create: `agent_service/execution/assertions.py`
- Modify: `agent_service/schemas.py`
- Modify: `agent_service/graph/nodes.py`
- Modify: `agent_service/graph/build.py`
- Test: `tests/agent/test_assertions.py`
- Modify: `tests/agent/test_graph.py`

**Interfaces:**
- Consumes: before/after snapshots, network entries, deterministic assertions.
- Produces: `AssertionResult`, final case status, schema-constrained `FailureAnalysis`.

- [x] **Step 1: Write failing precision and idempotency tests**

Create `tests/agent/test_assertions.py`:

```python
from decimal import Decimal

from agent_service.execution.assertions import (
    assert_balance_change,
    assert_single_transaction,
)


def test_balance_assertion_uses_decimal() -> None:
    result = assert_balance_change(
        before=Decimal("100.00000000"),
        after=Decimal("90.00000000"),
        expected_delta=Decimal("-10.00000000"),
    )
    assert result.passed is True


def test_duplicate_submission_requires_one_transaction() -> None:
    result = assert_single_transaction(
        before_ids={"tx-old"},
        after_ids={"tx-old", "tx-new"},
    )
    assert result.passed is True

    duplicate = assert_single_transaction(
        before_ids={"tx-old"},
        after_ids={"tx-old", "tx-new-1", "tx-new-2"},
    )
    assert duplicate.passed is False
```

Run:

```bash
.venv/bin/python -m pytest tests/agent/test_assertions.py -v
```

Expected: FAIL because assertion helpers do not exist.

- [x] **Step 2: Implement deterministic assertion results**

Create `agent_service/execution/assertions.py`:

```python
from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel


class AssertionResult(BaseModel):
    name: str
    passed: bool
    expected: str
    actual: str


def assert_balance_change(
    *,
    before: Decimal,
    after: Decimal,
    expected_delta: Decimal,
) -> AssertionResult:
    actual_delta = after - before
    return AssertionResult(
        name="balance_change",
        passed=actual_delta == expected_delta,
        expected=str(expected_delta),
        actual=str(actual_delta),
    )


def assert_single_transaction(
    *,
    before_ids: set[str],
    after_ids: set[str],
) -> AssertionResult:
    created = after_ids.difference(before_ids)
    return AssertionResult(
        name="single_transaction_created",
        passed=len(created) == 1,
        expected="1",
        actual=str(len(created)),
    )
```

- [x] **Step 3: Add failure-analysis schema and node**

Append to `agent_service/schemas.py`:

```python
class FailureAnalysis(BaseModel):
    category: Literal["product", "automation", "environment", "data", "unknown"]
    summary: str
    evidence_refs: list[str]
    related_bug_ids: list[int]
    recommended_action: str
```

Add a `classify_failure` model node that runs only when deterministic assertions fail:

```python
async def classify_failure(state: dict) -> dict:
    result = await deps.model_provider.generate_structured(
        task_type="classify_failure",
        prompt=(
            f"Assertion results: {state['assertion_results']}\n"
            f"Execution evidence: {state['execution_results']}\n"
            f"Related bugs: {state.get('related_bugs', [])}"
        ),
        schema=FailureAnalysis,
    )
    return {
        "failure_analysis": result.model_dump(),
        "status": "failure_classified",
        "passed": False,
    }
```

The node may classify and explain the failure but cannot change `passed=False`. Add `passed`, `assertion_results`, `execution_results`, and `failure_analysis` to `AgentState`.

- [x] **Step 4: Extend the graph after approval**

Add nodes and edges:

```text
human_review
→ prepare_execution
→ execute_tests
→ verify_results
→ conditional:
    failed → classify_failure → generate_report
    passed → generate_report
→ END
```

Update graph tests with fake execution dependencies so:

- approve reaches `completed`;
- reject returns to `generate_test_plan`;
- failed assertion invokes `classify_failure`;
- passed assertion never invokes `classify_failure`;
- deterministic `passed` cannot be overwritten by the model.

- [x] **Step 5: Run and commit assertion integration**

```bash
.venv/bin/python -m pytest tests/agent/test_assertions.py tests/agent/test_graph.py -v
```

Expected: all tests PASS.

```bash
git add agent_service tests/agent/test_assertions.py tests/agent/test_graph.py
git commit -m "feat: verify transfer outcomes and classify failures"
```

---

### Task 9: Artifacts, Reports, and Golden-Set Evaluation

**Files:**
- Create: `agent_service/artifacts.py`
- Create: `agent_service/reporting.py`
- Test: `tests/agent/test_reporting.py`

**Interfaces:**
- Consumes: complete graph state.
- Produces: atomic JSON artifacts, `report.md`, `report.html`, coverage metrics.

- [x] **Step 1: Write failing artifact and coverage tests**

Create `tests/agent/test_reporting.py`:

```python
import json
from html import escape
from pathlib import Path

from agent_service.reporting import evaluate_golden_set, write_reports


def test_golden_set_coverage_is_measurable() -> None:
    metrics = evaluate_golden_set(
        [
            "TC-OTI-001",
            "TC-OTI-002",
            "TC-OTI-003",
            "TC-OTI-004",
            "TC-OTI-005",
            "TC-OTI-006",
        ]
    )
    assert metrics.coverage_percent == 100
    assert metrics.missing_case_ids == []


def test_report_writes_json_markdown_and_html(tmp_path: Path) -> None:
    paths = write_reports(
        task_id="TASK-001",
        artifacts_root=tmp_path,
        state={"status": "completed", "test_plan": {"cases": []}},
    )
    assert Path(paths["json"]).exists()
    assert Path(paths["markdown"]).exists()
    assert Path(paths["html"]).exists()
    assert json.loads(Path(paths["json"]).read_text())["status"] == "completed"
```

Run:

```bash
.venv/bin/python -m pytest tests/agent/test_reporting.py -v
```

Expected: FAIL because reporting modules do not exist.

- [x] **Step 2: Implement atomic artifact writes**

Create `agent_service/artifacts.py`:

```python
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    os.replace(temporary, path)
```

- [x] **Step 3: Implement evaluation and human-readable reports**

Create `agent_service/reporting.py`:

```python
from __future__ import annotations

from html import escape
from pathlib import Path

from pydantic import BaseModel

from agent_service.artifacts import atomic_write_json
from agent_service.dsl import REQUIRED_BASELINE_IDS


class CoverageMetrics(BaseModel):
    coverage_percent: float
    missing_case_ids: list[str]


def evaluate_golden_set(case_ids: list[str]) -> CoverageMetrics:
    missing = sorted(REQUIRED_BASELINE_IDS.difference(case_ids))
    covered = len(REQUIRED_BASELINE_IDS) - len(missing)
    percent = covered / len(REQUIRED_BASELINE_IDS) * 100
    return CoverageMetrics(coverage_percent=percent, missing_case_ids=missing)


def write_reports(
    *,
    task_id: str,
    artifacts_root: Path,
    state: dict,
) -> dict[str, str]:
    directory = artifacts_root / task_id
    directory.mkdir(parents=True, exist_ok=True)
    json_path = directory / "execution_results.json"
    markdown_path = directory / "report.md"
    html_path = directory / "report.html"
    atomic_write_json(json_path, state)

    cases = state.get("test_plan", {}).get("cases", [])
    metrics = evaluate_golden_set([item["case_id"] for item in cases])
    markdown = (
        f"# AI Test Agent Report: {task_id}\n\n"
        f"- Status: {state.get('status', 'unknown')}\n"
        f"- Golden Set Coverage: {metrics.coverage_percent:.0f}%\n"
        f"- Cases: {len(cases)}\n"
    )
    markdown_path.write_text(markdown, encoding="utf-8")
    html_path.write_text(
        "<!doctype html><meta charset='utf-8'>"
        f"<title>{escape(task_id)}</title><pre>{escape(markdown)}</pre>",
        encoding="utf-8",
    )
    return {
        "json": str(json_path),
        "markdown": str(markdown_path),
        "html": str(html_path),
    }
```

- [x] **Step 4: Add graph report integration and redaction checks**

Update `generate_report` to write:

- `requirements.json`
- `risks.json`
- `related_bugs.json`
- `test_plan.json`
- `execution_results.json`
- `network_inventory.json`
- `report.md`
- `report.html`

Before every write, recursively replace values under keys matching:

```text
password, transaction_password, token, authorization, cookie, set-cookie
```

Add tests asserting known secret strings never occur under the task artifact directory.

Update `/agent/messages` completed responses to include deterministic portfolio metrics:

```python
response = {
    "thread_id": request.thread_id,
    "task_id": state.values.get("task_id"),
    "status": "waiting_approval" if interrupted else result.get("status"),
    "message": render_chat_message(state.values, interrupted=interrupted),
}
if not interrupted and result.get("status") == "completed":
    cases = result.get("test_plan", {}).get("cases", [])
    metrics = evaluate_golden_set([item["case_id"] for item in cases])
    response["metrics"] = {
        "golden_set_coverage_percent": metrics.coverage_percent,
    }
    response["summary"] = {
        "cloud_model_calls": 0,
        "report_paths": result.get("report_paths", {}),
    }
return response
```

- [ ] **Step 5: Run and commit reporting**

```bash
.venv/bin/python -m pytest tests/agent/test_reporting.py -v
```

Expected: all tests PASS.

```bash
git add agent_service/artifacts.py agent_service/reporting.py tests/agent/test_reporting.py
git commit -m "feat: generate traceable local test reports"
```

---

### Task 10: End-to-End Demo, Open WebUI Installation, and Portfolio Documentation

**Files:**
- Create: `scripts/run_internal_transfer_demo.py`
- Create: `tests/e2e/test_internal_transfer_agent.py`
- Modify: `README.md`
- Modify: `docs/superpowers/specs/2026-07-28-ai-test-agent-internal-transfer-design.md` only if implementation evidence exposes a confirmed contradiction.

**Interfaces:**
- Consumes: all completed Agent components and configured test environment.
- Produces: one real approved internal-transfer run, reproducible demo commands, portfolio evidence.

- [ ] **Step 1: Add an opt-in real-environment E2E test**

Create `tests/e2e/test_internal_transfer_agent.py`:

```python
import os

import httpx
import pytest
from dotenv import load_dotenv


pytestmark = pytest.mark.e2e
load_dotenv()


class RealAgentClient:
    def __init__(self, base_url: str, token: str):
        self.base_url = base_url.rstrip("/")
        self.token = token

    def send(self, thread_id: str, message: str) -> dict:
        response = httpx.post(
            f"{self.base_url}/agent/messages",
            json={"thread_id": thread_id, "message": message},
            headers={"Authorization": f"Bearer {self.token}"},
            timeout=600,
        )
        response.raise_for_status()
        return response.json()


@pytest.fixture
def real_agent_client() -> RealAgentClient:
    base_url = os.getenv("AGENT_BASE_URL", "http://localhost:8770")
    token = os.environ["AGENT_API_TOKEN"]
    response = httpx.get(f"{base_url}/health", timeout=10)
    response.raise_for_status()
    return RealAgentClient(base_url, token)


@pytest.mark.skipif(
    os.getenv("RUN_WALLET_E2E") != "1",
    reason="requires explicit RUN_WALLET_E2E=1",
)
def test_internal_transfer_full_agent_flow(real_agent_client) -> None:
    started = real_agent_client.send("e2e-internal-transfer", "测试内部转账")
    assert started["status"] == "waiting_approval"

    completed = real_agent_client.send("e2e-internal-transfer", "批准")
    assert completed["status"] == "completed"
    assert completed["metrics"]["golden_set_coverage_percent"] == 100
    assert completed["summary"]["cloud_model_calls"] == 0
```

- [ ] **Step 2: Create a command-line demo wrapper**

Create `scripts/run_internal_transfer_demo.py`:

```python
from __future__ import annotations

import argparse
import json
import os

import httpx
from dotenv import load_dotenv


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser()
    parser.add_argument("--thread-id", default="portfolio-demo")
    parser.add_argument("--approve", action="store_true")
    args = parser.parse_args()
    message = "批准" if args.approve else "测试 Web2 内部转账"
    response = httpx.post(
        "http://localhost:8770/agent/messages",
        json={"thread_id": args.thread_id, "message": message},
        headers={
            "Authorization": f"Bearer {os.environ['AGENT_API_TOKEN']}",
        },
        timeout=300,
    )
    response.raise_for_status()
    print(json.dumps(response.json(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
```

- [ ] **Step 3: Install the reviewed Pipe in Open WebUI**

Use the content of `agent_service/integrations/openwebui_pipe.py`:

1. Open `工作空间 → 函数`.
2. Create a Pipe Function named `AI Test Agent`.
3. Review the code and save it.
4. Enable the Function.
5. Set `AGENT_BASE_URL=http://host.docker.internal:8770`.
6. Set `AGENT_API_TOKEN` to the same local token stored in the Agent `.env`.
7. Select `AI Test Agent` as the chat model.

Expected: sending `测试 Web2 内部转账` returns a task summary and waits for approval. Sending `批准` in the same chat resumes the same LangGraph thread.

- [ ] **Step 4: Execute the complete local validation matrix**

Run:

```bash
.venv/bin/python -m pytest -m "not e2e" -q
curl -sS http://localhost:11434/api/tags | rg 'qwen3.5:9b'
curl -sS http://localhost:8765/health
curl -sS http://localhost:8770/health
RUN_WALLET_E2E=1 .venv/bin/python -m pytest tests/e2e/test_internal_transfer_agent.py -v
```

Expected:

- all non-E2E tests PASS;
- Ollama lists `qwen3.5:9b`;
- Bug service reports healthy data;
- Agent reports healthy dependencies;
- real E2E flow pauses, resumes, executes, captures network evidence, and produces reports;
- report states `cloud_model_calls=0`;
- Golden Set coverage is 100%;
- no credential value appears in artifacts.

- [ ] **Step 5: Complete portfolio-focused README**

Document:

- problem statement: missing Swagger and unreliable free-form tool calls;
- architecture and why LangGraph is used;
- local-only model setup;
- deterministic-versus-agentic responsibility boundary;
- how to configure `.env` without committing secrets;
- how to start Ollama, Bug service, Agent service, and Open WebUI;
- how to run unit and E2E tests;
- screenshots of approval pause, resumed execution, network inventory, and report;
- measured Golden Set coverage and model validation retry rate;
- known limitation: Web2 internal transfer only;
- roadmap: chain transfer, backend, App, model comparison, automated Bug draft.

- [ ] **Step 6: Final review and commit**

Run:

```bash
git status --short
git diff --check
.venv/bin/python -m pytest -m "not e2e" -q
git add README.md scripts tests/e2e
git commit -m "docs: add reproducible internal transfer agent demo"
```

Expected: clean diff check, all non-E2E tests PASS, and the commit contains no `.env`, credentials, SQLite databases, auth state, or artifacts.

---

## Implementation Order and Review Gates

1. Tasks 1-4 establish stable contracts and data adapters without LangGraph execution.
2. Tasks 5-6 deliver a locally resumable Agent that reaches human approval in Open WebUI.
3. Tasks 7-9 add deterministic execution, verification, and reporting.
4. Task 10 is accepted only after a real test-environment run produces portfolio evidence.

Each task must be reviewed before the next task starts. A failed review is fixed within the same task and committed separately; do not combine unrelated task changes.

## Reference Documentation

- LangGraph overview: <https://docs.langchain.com/oss/python/langgraph/overview>
- LangGraph persistence: <https://docs.langchain.com/oss/python/langgraph/persistence>
- LangChain agents: <https://docs.langchain.com/oss/python/langchain/agents>
- Playwright network monitoring: <https://playwright.dev/docs/network>
- Playwright API testing: <https://playwright.dev/docs/api-testing>
- Open WebUI Pipe Functions: <https://docs.openwebui.com/features/extensibility/plugin/functions/pipe/>
- Open WebUI reserved arguments: <https://docs.openwebui.com/features/extensibility/plugin/development/reserved-args/>
