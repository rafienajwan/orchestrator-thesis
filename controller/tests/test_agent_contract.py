from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from agent.app.adapters.docker_adapter import ContainerInfo
from agent.app.core.config import AgentSettings
from agent.app.main import create_app as create_agent_app
from controller.agent_client import AgentDeployResponse
from controller.models import ServiceSpec


class FakeDockerAdapter:
    async def deploy(self, service: ServiceSpec) -> ContainerInfo:
        return ContainerInfo(
            container_id=f"container-{service.service_id}", container_ip="127.0.0.1"
        )

    async def stop(self, service_id: str) -> None:
        return None

    async def restart(self, service_id: str) -> None:
        return None

    async def inspect(self, service_id: str) -> ContainerInfo | None:
        return ContainerInfo(container_id=f"container-{service_id}", container_ip="127.0.0.1")


@pytest.mark.asyncio
async def test_agent_deploy_response_matches_controller_contract() -> None:
    settings = AgentSettings(
        node_id="worker-1",
        agent_public_url="http://agent-1:8080",
        controller_base_url="http://controller:8000",
        telemetry_interval_seconds=60,
        health_check_interval_seconds=60,
    )
    app = create_agent_app(
        settings=settings, docker_adapter=FakeDockerAdapter(), start_telemetry=False
    )

    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://agent-1:8080"
        ) as client:
            response = await client.post(
                "/execute/deploy",
                json={
                    "service": {
                        "service_id": "sample-app",
                        "image": "sample-app:latest",
                        "command": [],
                        "env": {},
                        "internal_port": 8000,
                        "health_endpoint": "/health",
                        "min_free_cpu": 0.1,
                        "min_free_memory": 0.1,
                    }
                },
            )

    assert response.status_code == 200
    parsed = AgentDeployResponse.model_validate(response.json())
    assert parsed.service_id == "sample-app"
    assert parsed.container_id == "container-sample-app"
    assert parsed.status == "running"
    assert parsed.node_id == "worker-1"
    assert set(response.json().keys()) == {"service_id", "container_id", "status", "node_id"}
