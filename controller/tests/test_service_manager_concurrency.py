from __future__ import annotations

import asyncio

import pytest

from controller.agent_client import AgentDeployResponse
from controller.models import (
    DeploymentStatus,
    ResourceSnapshot,
    ServiceDeploymentRequest,
    ServiceSpec,
)
from controller.service_manager import ServiceManager
from controller.state_store import InMemoryStateStore


class SlowFakeAgentClient:
    def __init__(self) -> None:
        self.deploy_calls: list[tuple[str, str]] = []

    async def deploy(self, agent_url: str, service: ServiceSpec) -> AgentDeployResponse:
        await asyncio.sleep(0.05)
        self.deploy_calls.append((agent_url, service.service_id))
        return AgentDeployResponse(
            service_id=service.service_id, container_id="c-1", status="running"
        )

    async def stop(self, agent_url: str, service_id: str) -> None:
        return None

    async def restart(self, agent_url: str, service_id: str) -> None:
        return None


@pytest.mark.asyncio
async def test_deploy_is_serialized_per_service_id() -> None:
    store = InMemoryStateStore()
    agent = SlowFakeAgentClient()
    manager = ServiceManager(store=store, agent_client=agent)

    await store.upsert_node_heartbeat("node-a", "http://node-a:8080")
    await store.upsert_node_snapshot(
        "node-a", ResourceSnapshot(cpu_utilization=0.2, memory_utilization=0.2)
    )

    request = ServiceDeploymentRequest(
        service=ServiceSpec(service_id="svc-1", image="example/service:latest")
    )

    results = await asyncio.gather(manager.deploy(request), manager.deploy(request))

    assert len(agent.deploy_calls) == 1
    assert results[0].status == DeploymentStatus.running
    assert results[1].status == DeploymentStatus.running
