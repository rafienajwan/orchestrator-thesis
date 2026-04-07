from __future__ import annotations

import asyncio
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
from controller.scheduler import ScheduleDecision
from controller.self_healing import SelfHealingManager
from controller.state_store import InMemoryStateStore


class SlowRestartAgentClient:
    def __init__(self) -> None:
        self.restart_calls: list[tuple[str, str]] = []

    async def deploy(self, agent_url: str, service: ServiceSpec) -> AgentDeployResponse:
        return AgentDeployResponse(
            service_id=service.service_id, container_id="container-1", status="running"
        )

    async def stop(self, agent_url: str, service_id: str) -> None:
        return None

    async def restart(self, agent_url: str, service_id: str) -> None:
        self.restart_calls.append((agent_url, service_id))
        await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_concurrent_health_reports_do_not_trigger_duplicate_restart() -> None:
    settings = ControllerSettings(
        health_check_retries=3, max_restart_attempts=2, reconciliation_interval_seconds=15
    )
    store = InMemoryStateStore()
    agent = SlowRestartAgentClient()
    manager = SelfHealingManager(settings=settings, store=store, agent_client=agent)

    now = datetime.now(UTC)
    await store.upsert_node_heartbeat("node-a", "http://node-a:8080", at=now)
    await store.upsert_node_snapshot(
        "node-a", ResourceSnapshot(cpu_utilization=0.2, memory_utilization=0.2)
    )

    spec = ServiceSpec(service_id="svc-concurrent", image="example/service:latest")
    await store.set_service_desired(
        ServiceDesiredState(service=spec, status=DeploymentStatus.running)
    )
    await store.set_service_observed(
        ServiceObservedState(
            service_id=spec.service_id,
            status=DeploymentStatus.running,
            health=ServiceHealth.unhealthy,
            node_id="node-a",
            container_id="container-a",
        )
    )
    await store.set_placement(Placement(service_id=spec.service_id, node_id="node-a"))

    report = AgentHealthReport(
        node_id="node-a",
        service_id=spec.service_id,
        healthy=False,
        consecutive_failures=3,
        observed_at=now,
    )

    await asyncio.gather(manager.handle_health_report(report), manager.handle_health_report(report))

    assert len(agent.restart_calls) == 1
    assert await store.get_restart_counter(spec.service_id) is not None


@pytest.mark.asyncio
async def test_missing_target_node_creates_pending_deployment() -> None:
    settings = ControllerSettings(
        health_check_retries=3, max_restart_attempts=0, reconciliation_interval_seconds=15
    )

    store = InMemoryStateStore()
    agent = SlowRestartAgentClient()
    manager = SelfHealingManager(
        settings=settings,
        store=store,
        agent_client=agent,
        scheduler=lambda service, nodes: ScheduleDecision(
            service_id=service.service_id,
            selected_node_id="missing-node",
            status="scheduled",
            reason="forced missing node",
        ),
    )

    now = datetime.now(UTC)
    await store.upsert_node_heartbeat("node-a", "http://node-a:8080", at=now)
    await store.upsert_node_snapshot(
        "node-a", ResourceSnapshot(cpu_utilization=0.2, memory_utilization=0.2)
    )

    spec = ServiceSpec(service_id="svc-missing-target", image="example/service:latest")
    await store.set_service_desired(
        ServiceDesiredState(service=spec, status=DeploymentStatus.running)
    )
    await store.set_service_observed(
        ServiceObservedState(
            service_id=spec.service_id,
            status=DeploymentStatus.running,
            health=ServiceHealth.unhealthy,
            node_id="node-a",
            container_id="container-a",
        )
    )
    await store.set_placement(Placement(service_id=spec.service_id, node_id="node-a"))

    await manager.handle_health_report(
        AgentHealthReport(
            node_id="node-a",
            service_id=spec.service_id,
            healthy=False,
            consecutive_failures=3,
            observed_at=now,
        )
    )

    pending = await store.list_pending_deployments()
    observed = await store.get_service_observed(spec.service_id)

    assert len(pending) == 1
    assert pending[0].service_id == spec.service_id
    assert observed is not None
    assert observed.status == DeploymentStatus.pending
