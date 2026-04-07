from __future__ import annotations

from datetime import UTC, datetime

import pytest

from controller.agent_client import AgentDeployResponse
from controller.config import ControllerSettings
from controller.models import (
    AgentHealthReport,
    DeploymentStatus,
    NodeState,
    NodeStatus,
    Placement,
    ResourceSnapshot,
    ServiceDesiredState,
    ServiceHealth,
    ServiceObservedState,
    ServiceSpec,
)
from controller.self_healing import SelfHealingManager
from controller.state_store import InMemoryStateStore


class FakeAgentClient:
    def __init__(self) -> None:
        self.restart_calls: list[tuple[str, str]] = []
        self.stop_calls: list[tuple[str, str]] = []
        self.deploy_calls: list[tuple[str, str]] = []

    async def deploy(self, agent_url: str, service: ServiceSpec) -> AgentDeployResponse:
        self.deploy_calls.append((agent_url, service.service_id))
        return AgentDeployResponse(
            service_id=service.service_id, container_id="rescheduled-container", status="running"
        )

    async def stop(self, agent_url: str, service_id: str) -> None:
        self.stop_calls.append((agent_url, service_id))

    async def restart(self, agent_url: str, service_id: str) -> None:
        self.restart_calls.append((agent_url, service_id))


@pytest.mark.asyncio
async def test_self_healing_restarts_on_consecutive_failures_before_max_restart() -> None:
    settings = ControllerSettings(
        health_check_retries=3, max_restart_attempts=2, reconciliation_interval_seconds=15
    )
    store = InMemoryStateStore()
    agent_client = FakeAgentClient()
    manager = SelfHealingManager(settings=settings, store=store, agent_client=agent_client)

    node = NodeState(
        node_id="node-a",
        agent_url="http://node-a:8080",
        status=NodeStatus.healthy,
        last_heartbeat_at=datetime.now(UTC),
        last_resource_snapshot=ResourceSnapshot(cpu_utilization=0.2, memory_utilization=0.2),
    )
    await store.upsert_node_heartbeat(node.node_id, node.agent_url, at=node.last_heartbeat_at)
    node_snapshot = ResourceSnapshot(cpu_utilization=0.2, memory_utilization=0.2)
    await store.upsert_node_snapshot(node.node_id, node_snapshot)

    spec = ServiceSpec(service_id="svc-restart", image="example/service:latest")
    await store.set_service_desired(
        ServiceDesiredState(service=spec, status=DeploymentStatus.running)
    )
    await store.set_service_observed(
        ServiceObservedState(
            service_id=spec.service_id,
            status=DeploymentStatus.running,
            health=ServiceHealth.unhealthy,
            node_id=node.node_id,
            container_id="container-a",
        )
    )
    await store.set_placement(Placement(service_id=spec.service_id, node_id=node.node_id))

    report = AgentHealthReport(
        node_id=node.node_id,
        service_id=spec.service_id,
        healthy=False,
        consecutive_failures=3,
    )
    await manager.handle_health_report(report)

    assert agent_client.restart_calls == [("http://node-a:8080", "svc-restart")]
    counter = await store.get_restart_counter(spec.service_id)
    assert counter.count == 1


@pytest.mark.asyncio
async def test_self_healing_reschedules_after_restart_limit_reached() -> None:
    settings = ControllerSettings(
        health_check_retries=3, max_restart_attempts=2, reconciliation_interval_seconds=15
    )
    store = InMemoryStateStore()
    agent_client = FakeAgentClient()
    manager = SelfHealingManager(settings=settings, store=store, agent_client=agent_client)

    node_a = NodeState(
        node_id="node-a",
        agent_url="http://node-a:8080",
        status=NodeStatus.healthy,
        last_heartbeat_at=datetime.now(UTC),
        last_resource_snapshot=ResourceSnapshot(cpu_utilization=0.2, memory_utilization=0.2),
    )
    node_b = NodeState(
        node_id="node-b",
        agent_url="http://node-b:8080",
        status=NodeStatus.healthy,
        last_heartbeat_at=datetime.now(UTC),
        last_resource_snapshot=ResourceSnapshot(cpu_utilization=0.1, memory_utilization=0.1),
    )
    snapshot_a = ResourceSnapshot(cpu_utilization=0.2, memory_utilization=0.2)
    snapshot_b = ResourceSnapshot(cpu_utilization=0.1, memory_utilization=0.1)

    await store.upsert_node_heartbeat(node_a.node_id, node_a.agent_url, at=node_a.last_heartbeat_at)
    await store.upsert_node_snapshot(node_a.node_id, snapshot_a)
    await store.upsert_node_heartbeat(node_b.node_id, node_b.agent_url, at=node_b.last_heartbeat_at)
    await store.upsert_node_snapshot(node_b.node_id, snapshot_b)

    spec = ServiceSpec(service_id="svc-reschedule", image="example/service:latest")
    await store.set_service_desired(
        ServiceDesiredState(service=spec, status=DeploymentStatus.running)
    )
    await store.set_service_observed(
        ServiceObservedState(
            service_id=spec.service_id,
            status=DeploymentStatus.running,
            health=ServiceHealth.unhealthy,
            node_id=node_a.node_id,
            container_id="container-a",
        )
    )
    await store.set_placement(Placement(service_id=spec.service_id, node_id=node_a.node_id))

    await store.increment_restart_counter(spec.service_id)
    await store.increment_restart_counter(spec.service_id)

    report = AgentHealthReport(
        node_id=node_a.node_id,
        service_id=spec.service_id,
        healthy=False,
        consecutive_failures=3,
    )
    await manager.handle_health_report(report)

    assert agent_client.deploy_calls == [("http://node-b:8080", "svc-reschedule")]
    placement = await store.get_placement(spec.service_id)
    assert placement is not None
    assert placement.node_id == "node-b"

    counter = await store.get_restart_counter(spec.service_id)
    assert counter.count == 0
