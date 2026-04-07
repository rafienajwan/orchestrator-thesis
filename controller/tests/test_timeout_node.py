from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from controller.agent_client import AgentDeployResponse
from controller.config import ControllerSettings
from controller.models import (
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
from controller.reconciler import Reconciler
from controller.self_healing import SelfHealingManager
from controller.service_manager import ServiceManager
from controller.state_store import InMemoryStateStore


class FakeAgentClient:
    def __init__(self) -> None:
        self.restart_calls: list[tuple[str, str]] = []
        self.stop_calls: list[tuple[str, str]] = []
        self.deploy_calls: list[tuple[str, str]] = []

    async def deploy(self, agent_url: str, service: ServiceSpec) -> AgentDeployResponse:
        self.deploy_calls.append((agent_url, service.service_id))
        return AgentDeployResponse(
            service_id=service.service_id, container_id="new-container", status="running"
        )

    async def stop(self, agent_url: str, service_id: str) -> None:
        self.stop_calls.append((agent_url, service_id))

    async def restart(self, agent_url: str, service_id: str) -> None:
        self.restart_calls.append((agent_url, service_id))


@pytest.mark.asyncio
async def test_reconciler_marks_node_unavailable_and_reschedules_workload() -> None:
    settings = ControllerSettings(
        heartbeat_timeout_seconds=30,
        reconciliation_interval_seconds=15,
        health_check_retries=3,
        max_restart_attempts=2,
    )
    store = InMemoryStateStore()
    agent_client = FakeAgentClient()
    service_manager = ServiceManager(store=store, agent_client=agent_client)
    self_healing = SelfHealingManager(settings=settings, store=store, agent_client=agent_client)
    reconciler = Reconciler(
        settings=settings, store=store, service_manager=service_manager, self_healing=self_healing
    )

    now = datetime.now(UTC)
    stale_node = NodeState(
        node_id="node-a",
        agent_url="http://node-a:8080",
        status=NodeStatus.healthy,
        last_heartbeat_at=now - timedelta(seconds=60),
        last_resource_snapshot=ResourceSnapshot(cpu_utilization=0.2, memory_utilization=0.2),
    )
    healthy_node = NodeState(
        node_id="node-b",
        agent_url="http://node-b:8080",
        status=NodeStatus.healthy,
        last_heartbeat_at=now,
        last_resource_snapshot=ResourceSnapshot(cpu_utilization=0.1, memory_utilization=0.1),
    )

    await store.upsert_node_heartbeat(
        stale_node.node_id, stale_node.agent_url, at=stale_node.last_heartbeat_at
    )
    stale_snapshot = ResourceSnapshot(cpu_utilization=0.2, memory_utilization=0.2)
    await store.upsert_node_snapshot(stale_node.node_id, stale_snapshot)
    await store.upsert_node_heartbeat(
        healthy_node.node_id, healthy_node.agent_url, at=healthy_node.last_heartbeat_at
    )
    healthy_snapshot = ResourceSnapshot(cpu_utilization=0.1, memory_utilization=0.1)
    await store.upsert_node_snapshot(healthy_node.node_id, healthy_snapshot)

    spec = ServiceSpec(service_id="svc-timeout", image="example/service:latest")
    await store.set_service_desired(
        ServiceDesiredState(service=spec, status=DeploymentStatus.running)
    )
    await store.set_service_observed(
        ServiceObservedState(
            service_id=spec.service_id,
            status=DeploymentStatus.running,
            health=ServiceHealth.healthy,
            node_id=stale_node.node_id,
            container_id="old-container",
        )
    )
    await store.set_placement(Placement(service_id=spec.service_id, node_id=stale_node.node_id))

    await reconciler.run_once(now=now)

    nodes = await store.list_nodes()
    node_a = next(node for node in nodes if node.node_id == "node-a")
    assert node_a.status == NodeStatus.unavailable

    placement = await store.get_placement(spec.service_id)
    assert placement is not None
    assert placement.node_id == "node-b"
    assert agent_client.deploy_calls == [("http://node-b:8080", "svc-timeout")]
