from __future__ import annotations

from datetime import UTC, datetime

import pytest

from controller.agent_client import AgentDeployResponse
from controller.config import ControllerSettings
from controller.models import (
    AgentHealthReport,
    DeploymentStatus,
    Placement,
    ResourceSnapshot,
    ServiceDesiredState,
    ServiceHealth,
    ServiceObservedState,
    ServiceSpec,
)
from controller.self_healing import SelfHealingManager
from controller.state_store import InMemoryStateStore


class RecordingAgentClient:
    def __init__(self) -> None:
        self.restart_calls: list[tuple[str, str]] = []
        self.deploy_calls: list[tuple[str, str]] = []

    async def deploy(self, agent_url: str, service: ServiceSpec) -> AgentDeployResponse:
        self.deploy_calls.append((agent_url, service.service_id))
        return AgentDeployResponse(
            service_id=service.service_id, container_id="container-1", status="running"
        )

    async def stop(self, agent_url: str, service_id: str) -> None:
        return None

    async def restart(self, agent_url: str, service_id: str) -> None:
        self.restart_calls.append((agent_url, service_id))


@pytest.mark.asyncio
async def test_stale_health_report_from_old_node_is_ignored() -> None:
    settings = ControllerSettings(
        health_check_retries=3, max_restart_attempts=2, reconciliation_interval_seconds=15
    )
    store = InMemoryStateStore()
    manager = SelfHealingManager(
        settings=settings, store=store, agent_client=RecordingAgentClient()
    )

    now = datetime.now(UTC)
    await store.upsert_node_heartbeat("node-a", "http://node-a:8080", at=now)
    await store.upsert_node_snapshot(
        "node-a", ResourceSnapshot(cpu_utilization=0.2, memory_utilization=0.2)
    )
    await store.upsert_node_heartbeat("node-b", "http://node-b:8080", at=now)
    await store.upsert_node_snapshot(
        "node-b", ResourceSnapshot(cpu_utilization=0.1, memory_utilization=0.1)
    )

    spec = ServiceSpec(service_id="svc-old", image="example/service:latest")
    await store.set_service_desired(
        ServiceDesiredState(service=spec, status=DeploymentStatus.running)
    )
    await store.set_service_observed(
        ServiceObservedState(
            service_id=spec.service_id,
            status=DeploymentStatus.running,
            health=ServiceHealth.healthy,
            node_id="node-b",
            container_id="container-b",
        )
    )
    await store.set_placement(Placement(service_id=spec.service_id, node_id="node-b"))

    await manager.handle_health_report(
        AgentHealthReport(
            node_id="node-a",
            service_id=spec.service_id,
            healthy=False,
            consecutive_failures=3,
            observed_at=now,
        )
    )

    observed = await store.get_service_observed(spec.service_id)
    counter = await store.get_restart_counter(spec.service_id)
    assert observed is not None
    assert observed.node_id == "node-b"
    assert observed.health == ServiceHealth.healthy
    assert counter.count == 0


@pytest.mark.asyncio
async def test_health_report_for_stopped_service_is_ignored() -> None:
    settings = ControllerSettings(
        health_check_retries=3, max_restart_attempts=2, reconciliation_interval_seconds=15
    )
    store = InMemoryStateStore()
    manager = SelfHealingManager(
        settings=settings, store=store, agent_client=RecordingAgentClient()
    )

    now = datetime.now(UTC)
    await store.upsert_node_heartbeat("node-a", "http://node-a:8080", at=now)
    await store.upsert_node_snapshot(
        "node-a", ResourceSnapshot(cpu_utilization=0.2, memory_utilization=0.2)
    )

    spec = ServiceSpec(service_id="svc-stopped", image="example/service:latest")
    await store.set_service_desired(
        ServiceDesiredState(service=spec, status=DeploymentStatus.stopped)
    )
    await store.set_service_observed(
        ServiceObservedState(
            service_id=spec.service_id,
            status=DeploymentStatus.stopped,
            health=ServiceHealth.unknown,
            node_id=None,
            container_id=None,
        )
    )

    await manager.handle_health_report(
        AgentHealthReport(
            node_id="node-a",
            service_id=spec.service_id,
            healthy=True,
            consecutive_failures=0,
            observed_at=now,
        )
    )

    observed = await store.get_service_observed(spec.service_id)
    pending = await store.list_pending_deployments()

    assert observed is not None
    assert observed.status == DeploymentStatus.stopped
    assert pending == []
