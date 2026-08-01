from __future__ import annotations

import os
from uuid import uuid4

import pytest
from dotenv import load_dotenv

from scripts.run_internal_transfer_demo import AgentClient


load_dotenv()
pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        os.getenv("RUN_WALLET_E2E") != "1",
        reason="requires explicit RUN_WALLET_E2E=1",
    ),
]


def test_internal_transfer_full_agent_flow() -> None:
    client = AgentClient(
        base_url=os.getenv("AGENT_BASE_URL", "http://127.0.0.1:8770"),
        token=os.environ["AGENT_API_TOKEN"],
    )
    thread_id = f"e2e-{uuid4().hex[:16]}"

    started = client.send(thread_id, "测试 Web2 内部转账")
    assert started["status"] == "waiting_approval"

    completed = client.send(thread_id, "批准")
    assert completed["status"] == "completed"
    assert completed["metrics"]["golden_set_coverage_percent"] == 100
    assert completed["summary"]["cloud_model_calls"] == 0
    assert completed["summary"]["passed"] is True
