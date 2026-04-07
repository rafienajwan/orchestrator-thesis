from __future__ import annotations

from typing import Any

import pytest

from agent.app.core.config import AgentSettings
from agent.app.services.telemetry import ControllerReporter
from controller.models import ResourceSnapshot


class FakeResponse:
    def raise_for_status(self) -> None:
        return None


class CapturingAsyncClient:
    def __init__(self, timeout: Any, calls: list[tuple[str, dict[str, object]]]) -> None:
        self._calls = calls

    async def __aenter__(self) -> CapturingAsyncClient:
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    async def post(self, url: str, json: dict[str, object]) -> FakeResponse:
        self._calls.append((url, json))
        return FakeResponse()


@pytest.mark.asyncio
async def test_controller_reporter_posts_heartbeat_and_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = AgentSettings(
        node_id="worker-1",
        agent_public_url="http://agent-1:8080",
        controller_base_url="http://controller:8000",
        health_check_timeout_seconds=2,
    )
    calls: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        "agent.app.services.telemetry.httpx.AsyncClient",
        lambda timeout: CapturingAsyncClient(timeout=timeout, calls=calls),
    )

    reporter = ControllerReporter(settings=settings)
    await reporter.send_heartbeat()
    await reporter.send_resource_snapshot(
        ResourceSnapshot(cpu_utilization=0.25, memory_utilization=0.5)
    )

    assert calls[0][0] == "http://controller:8000/internal/agent/heartbeat"
    assert calls[0][1]["node_id"] == "worker-1"
    assert calls[0][1]["agent_url"] == "http://agent-1:8080"
    assert calls[1][0] == "http://controller:8000/internal/agent/resource-snapshot"
    assert calls[1][1]["node_id"] == "worker-1"
    snapshot = calls[1][1]["snapshot"]
    assert snapshot["cpu_utilization"] == 0.25
    assert snapshot["memory_utilization"] == 0.5
