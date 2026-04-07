from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from agent.app.adapters.docker_adapter import ContainerInfo
from agent.app.core.config import AgentSettings
from agent.app.core.models import WorkloadStatus
from agent.app.main import create_app as create_agent_app
from controller.models import ServiceSpec


class FakeDockerAdapter:
    def __init__(self) -> None:
        self.deploy_calls: list[str] = []
        self.stop_calls: list[str] = []
        self.restart_calls: list[str] = []

    async def deploy(self, service: ServiceSpec) -> ContainerInfo:
        self.deploy_calls.append(service.service_id)
        return ContainerInfo(
            container_id=f"container-{service.service_id}", container_ip="127.0.0.1"
        )

    async def stop(self, service_id: str) -> None:
        self.stop_calls.append(service_id)

    async def restart(self, service_id: str) -> None:
        self.restart_calls.append(service_id)

    async def inspect(self, service_id: str) -> ContainerInfo | None:
        if service_id in self.deploy_calls:
            return ContainerInfo(container_id=f"container-{service_id}", container_ip="127.0.0.1")
        return None


@pytest.mark.asyncio
async def test_agent_execute_endpoints_and_local_state() -> None:
    settings = AgentSettings(
        node_id="node-a",
        agent_public_url="http://agent-a:8080",
        controller_base_url="http://controller:8000",
        telemetry_interval_seconds=60,
        health_check_interval_seconds=60,
    )
    fake_docker = FakeDockerAdapter()
    app = create_agent_app(settings=settings, docker_adapter=fake_docker, start_telemetry=False)

    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://agent-a:8080"
        ) as client:
            service = ServiceSpec(
                service_id="svc-agent", image="example/service:latest", internal_port=8080
            )

            deploy_response = await client.post(
                "/execute/deploy", json={"service": service.model_dump(mode="json")}
            )
            assert deploy_response.status_code == 200
            assert deploy_response.json()["status"] == "running"

            health_response = await client.get("/health")
            assert health_response.status_code == 200
            assert health_response.json()["node_id"] == "node-a"

            local_state_response = await client.get("/local-state")
            assert local_state_response.status_code == 200
            state_json = local_state_response.json()
            assert state_json["workloads"]["svc-agent"]["status"] == WorkloadStatus.running.value

            stop_response = await client.post("/execute/stop", json={"service_id": "svc-agent"})
            assert stop_response.status_code == 200
            assert stop_response.json()["status"] == "stopped"

            restart_response = await client.post(
                "/execute/restart", json={"service_id": "svc-agent"}
            )
            assert restart_response.status_code == 200
            assert restart_response.json()["status"] == "running"

            assert fake_docker.deploy_calls == ["svc-agent"]
            assert fake_docker.stop_calls == ["svc-agent"]
            assert fake_docker.restart_calls == ["svc-agent"]
