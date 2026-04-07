from __future__ import annotations

import pytest

from agent.app.adapters.docker_adapter import ContainerInfo, DockerAdapter
from agent.app.core.config import AgentSettings
from agent.app.core.models import WorkloadRecord, WorkloadStatus
from agent.app.core.state import AgentStateStore
from agent.app.services.workload_manager import AgentWorkloadManager
from controller.models import ServiceSpec


class FilteringDockerAdapter(DockerAdapter):
    def __init__(self, local_service_ids: set[str]) -> None:
        self._local_service_ids = local_service_ids

    async def deploy(self, service: ServiceSpec) -> ContainerInfo:
        self._local_service_ids.add(service.service_id)
        return ContainerInfo(
            container_id=f"container-{service.service_id}",
            container_ip="127.0.0.1",
        )

    async def stop(self, service_id: str) -> None:
        return None

    async def restart(self, service_id: str) -> None:
        return None

    async def inspect(self, service_id: str) -> ContainerInfo | None:
        if service_id not in self._local_service_ids:
            return None
        return ContainerInfo(
            container_id=f"container-{service_id}",
            container_ip="127.0.0.1",
        )


@pytest.mark.asyncio
async def test_local_state_only_contains_node_owned_workloads() -> None:
    settings = AgentSettings(
        node_id="worker-1",
        agent_public_url="http://agent-1:8080",
        controller_base_url="http://controller:8000",
    )
    store = AgentStateStore(
        node_id=settings.node_id,
        agent_url=settings.agent_public_url,
        controller_url=settings.controller_base_url,
    )

    local_service = ServiceSpec(service_id="svc-local", image="sample-app:latest")
    foreign_service = ServiceSpec(service_id="svc-foreign", image="sample-app:latest")

    await store.upsert_workload(
        WorkloadRecord(
            service=local_service,
            container_id="container-svc-local",
            container_ip="127.0.0.1",
            status=WorkloadStatus.running,
        )
    )
    await store.upsert_workload(
        WorkloadRecord(
            service=foreign_service,
            container_id="container-svc-foreign",
            container_ip="127.0.0.2",
            status=WorkloadStatus.running,
        )
    )

    adapter = FilteringDockerAdapter(local_service_ids={"svc-local"})
    manager = AgentWorkloadManager(settings=settings, store=store, adapter=adapter)

    state = await manager.get_local_state()

    assert set(state.workloads.keys()) == {"svc-local"}
