from __future__ import annotations

from typing import Any

import httpx
import pytest

from agent.app.adapters.docker_adapter import ContainerInfo, DockerAdapter
from agent.app.core.config import AgentSettings
from agent.app.core.models import WorkloadRecord, WorkloadStatus
from agent.app.core.state import AgentStateStore
from agent.app.services.workload_manager import AgentWorkloadManager
from controller.models import ServiceSpec


class FakeDockerAdapter(DockerAdapter):
    async def deploy(self, service: ServiceSpec) -> ContainerInfo:
        return ContainerInfo(
            container_id=f"container-{service.service_id}", container_ip="127.0.0.1"
        )

    async def stop(self, service_id: str) -> None:
        return None

    async def restart(self, service_id: str) -> None:
        return None

    async def inspect(self, service_id: str) -> ContainerInfo | None:
        return None


class FakeResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code

    def raise_for_status(self) -> None:
        return None


class FlakyAsyncClient:
    def __init__(self, timeout: Any, calls: dict[str, int]) -> None:
        self._calls = calls

    async def __aenter__(self) -> FlakyAsyncClient:
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    async def get(self, url: str) -> FakeResponse:
        self._calls["count"] += 1
        if self._calls["count"] < 3:
            raise httpx.ConnectError("boom", request=httpx.Request("GET", url))
        return FakeResponse(200)


def _build_client(timeout: object, calls: dict[str, int]) -> FlakyAsyncClient:
    return FlakyAsyncClient(timeout=timeout, calls=calls)


@pytest.mark.asyncio
async def test_health_checker_retries_until_success(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = AgentSettings(
        node_id="worker-1",
        agent_public_url="http://agent-1:8080",
        controller_base_url="http://controller:8000",
        health_check_timeout_seconds=2,
        health_check_retries=3,
    )
    store = AgentStateStore(
        node_id=settings.node_id,
        agent_url=settings.agent_public_url,
        controller_url=settings.controller_base_url,
    )
    service = ServiceSpec(
        service_id="svc-1", image="sample-app:latest", internal_port=8000, health_endpoint="/health"
    )
    await store.upsert_workload(
        WorkloadRecord(
            service=service,
            container_id="container-1",
            container_ip="127.0.0.1",
            status=WorkloadStatus.running,
        )
    )

    calls = {"count": 0}
    monkeypatch.setattr(
        "agent.app.services.workload_manager.httpx.AsyncClient",
        lambda timeout: _build_client(timeout=timeout, calls=calls),
    )

    manager = AgentWorkloadManager(settings=settings, store=store, adapter=FakeDockerAdapter())
    reports = await manager.health_check_all()

    assert calls["count"] == 3
    assert len(reports) == 1
    assert reports[0].healthy is True
    assert reports[0].consecutive_failures == 0

    updated = await store.get_workload("svc-1")
    assert updated is not None
    assert updated.status == WorkloadStatus.running
    assert updated.health_failures == 0
