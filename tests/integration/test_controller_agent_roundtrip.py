from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from agent.app.adapters.docker_adapter import ContainerInfo
from agent.app.core.config import AgentSettings
from agent.app.core.models import WorkloadStatus
from agent.app.main import create_app as create_agent_app
from controller.agent_client import AgentClient, AgentDeployResponse
from controller.models import (
    DeploymentStatus,
    ResourceSnapshot,
    ServiceDeploymentRequest,
    ServiceSpec,
)
from controller.scheduler import choose_node_least_load
from controller.service_manager import ServiceManager
from controller.state_store import InMemoryStateStore


class FakeDockerAdapter:
    def __init__(self) -> None:
        self.deploy_calls: list[str] = []
        self.stop_calls: list[str] = []
        self.restart_calls: list[str] = []

    async def deploy(self, service: ServiceSpec) -> ContainerInfo:
        self.deploy_calls.append(service.service_id)
        return ContainerInfo(
            container_id=f"container-{service.service_id}",
            container_ip="127.0.0.1",
            published_port=28000,
        )

    async def stop(self, service_id: str) -> None:
        self.stop_calls.append(service_id)

    async def restart(self, service_id: str) -> None:
        self.restart_calls.append(service_id)

    async def inspect(self, service_id: str) -> ContainerInfo | None:
        if service_id in self.deploy_calls:
            return ContainerInfo(
                container_id=f"container-{service_id}",
                container_ip="127.0.0.1",
                published_port=28000,
            )
        return None


class AsgiAgentClient(AgentClient):
    def __init__(self, client: AsyncClient) -> None:
        self._client = client

    async def deploy(self, agent_url: str, service: ServiceSpec) -> AgentDeployResponse:
        response = await self._client.post(
            "/execute/deploy", json={"service": service.model_dump(mode="json")}
        )
        response.raise_for_status()
        return AgentDeployResponse.model_validate(response.json())

    async def stop(self, agent_url: str, service_id: str) -> None:
        response = await self._client.post("/execute/stop", json={"service_id": service_id})
        response.raise_for_status()

    async def restart(self, agent_url: str, service_id: str) -> None:
        response = await self._client.post("/execute/restart", json={"service_id": service_id})
        response.raise_for_status()


@pytest.mark.asyncio
async def test_controller_deploys_service_to_agent_and_agent_tracks_state() -> None:
    controller_store = InMemoryStateStore()
    agent_settings = AgentSettings(
        node_id="node-a",
        advertised_host="node-a.internal",
        agent_public_url="http://agent-a:8080",
        controller_base_url="http://controller:8000",
        telemetry_interval_seconds=60,
        health_check_interval_seconds=60,
    )
    fake_docker = FakeDockerAdapter()
    agent_app = create_agent_app(
        settings=agent_settings, docker_adapter=fake_docker, start_telemetry=False
    )

    await controller_store.upsert_node_heartbeat("node-a", "http://agent-a:8080")
    await controller_store.upsert_node_snapshot(
        "node-a",
        ResourceSnapshot(cpu_utilization=0.2, memory_utilization=0.2),
    )

    async with agent_app.router.lifespan_context(agent_app):
        async with AsyncClient(
            transport=ASGITransport(app=agent_app), base_url="http://agent-a:8080"
        ) as agent_http:
            controller_manager = ServiceManager(
                store=controller_store,
                agent_client=AsgiAgentClient(agent_http),
                scheduler=choose_node_least_load,
            )
            service = ServiceSpec(
                service_id="svc-roundtrip", image="example/service:latest", internal_port=8080
            )
            observed = await controller_manager.deploy(ServiceDeploymentRequest(service=service))

            assert observed.status == DeploymentStatus.running
            placement = await controller_store.get_placement("svc-roundtrip")
            assert placement is not None
            assert placement.node_id == "node-a"

            local_state = await agent_app.state.workload_manager.get_local_state()
            assert local_state.workloads["svc-roundtrip"].status == WorkloadStatus.running
            assert fake_docker.deploy_calls == ["svc-roundtrip"]
            response = await agent_http.get("/local-state")
            assert response.status_code == 200
