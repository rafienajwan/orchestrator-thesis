from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from controller.agent_client import AgentDeployResponse
from controller.config import ControllerSettings
from controller.models import (
    DeploymentStatus,
    EventType,
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
        self.deploy_calls: list[tuple[str, str]] = []
        self.stop_calls: list[tuple[str, str]] = []
        self.restart_calls: list[tuple[str, str]] = []

    async def deploy(self, agent_url: str, service: ServiceSpec) -> AgentDeployResponse:
        self.deploy_calls.append((agent_url, service.service_id))
        return AgentDeployResponse(
            service_id=service.service_id,
            container_id=f"container-{service.service_id}",
            status="running",
        )

    async def stop(self, agent_url: str, service_id: str) -> None:
        self.stop_calls.append((agent_url, service_id))

    async def restart(self, agent_url: str, service_id: str) -> None:
        self.restart_calls.append((agent_url, service_id))


@pytest.mark.asyncio
async def test_node_unreachable_triggers_reschedule_of_all_workloads_on_node() -> None:
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
    stale_heartbeat = now - timedelta(seconds=60)

    await store.upsert_node_heartbeat("worker-1", "http://agent-1:8080", at=stale_heartbeat)
    await store.upsert_node_snapshot(
        "worker-1", ResourceSnapshot(cpu_utilization=0.2, memory_utilization=0.2)
    )
    await store.upsert_node_heartbeat("worker-2", "http://agent-2:8080", at=now)
    await store.upsert_node_snapshot(
        "worker-2", ResourceSnapshot(cpu_utilization=0.1, memory_utilization=0.1)
    )

    spec = ServiceSpec(service_id="sample-app", image="sample-app:latest")
    await store.set_service_desired(
        ServiceDesiredState(service=spec, status=DeploymentStatus.running)
    )
    await store.set_service_observed(
        ServiceObservedState(
            service_id=spec.service_id,
            status=DeploymentStatus.running,
            health=ServiceHealth.healthy,
            node_id="worker-1",
            container_id="container-old",
        )
    )
    await store.set_placement(Placement(service_id=spec.service_id, node_id="worker-1"))

    await reconciler.run_once(now=now)

    nodes = await store.list_nodes()
    node_1 = next(node for node in nodes if node.node_id == "worker-1")
    placement = await store.get_placement(spec.service_id)
    observed = await store.get_service_observed(spec.service_id)
    events = await store.list_events()
    restart_counter = await store.get_restart_counter(spec.service_id)

    assert node_1.status == NodeStatus.unavailable
    assert agent_client.deploy_calls == [("http://agent-2:8080", "sample-app")]
    assert placement is not None and placement.node_id == "worker-2"
    assert observed is not None and observed.status == DeploymentStatus.running
    assert restart_counter.count == 0
    assert any(event.event_type == EventType.node for event in events)
    assert any(event.event_type == EventType.self_healing for event in events)
