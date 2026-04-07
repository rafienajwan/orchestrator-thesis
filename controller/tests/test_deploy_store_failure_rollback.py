from __future__ import annotations

import pytest

from controller.agent_client import AgentDeployResponse
from controller.models import ResourceSnapshot, ServiceDeploymentRequest, ServiceSpec
from controller.service_manager import ServiceManager
from controller.state_store import InMemoryStateStore


class RecordingAgentClient:
    def __init__(self) -> None:
        self.deploy_calls: list[tuple[str, str]] = []
        self.stop_calls: list[tuple[str, str]] = []

    async def deploy(self, agent_url: str, service: ServiceSpec) -> AgentDeployResponse:
        self.deploy_calls.append((agent_url, service.service_id))
        return AgentDeployResponse(
            service_id=service.service_id, container_id="container-1", status="running"
        )

    async def stop(self, agent_url: str, service_id: str) -> None:
        self.stop_calls.append((agent_url, service_id))

    async def restart(self, agent_url: str, service_id: str) -> None:
        return None


@pytest.mark.asyncio
async def test_deploy_rolls_back_agent_container_when_store_write_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = InMemoryStateStore()
    agent = RecordingAgentClient()
    manager = ServiceManager(store=store, agent_client=agent)

    await store.upsert_node_heartbeat("node-a", "http://node-a:8080")
    await store.upsert_node_snapshot(
        "node-a", ResourceSnapshot(cpu_utilization=0.2, memory_utilization=0.2)
    )

    async def fail_set_placement(placement: object) -> None:
        raise RuntimeError("store write failed")

    monkeypatch.setattr(store, "set_placement", fail_set_placement)

    request = ServiceDeploymentRequest(
        service=ServiceSpec(service_id="svc-rollback", image="example/service:latest")
    )

    with pytest.raises(RuntimeError, match="store write failed"):
        await manager.deploy(request)

    assert agent.deploy_calls == [("http://node-a:8080", "svc-rollback")]
    assert agent.stop_calls == [("http://node-a:8080", "svc-rollback")]
