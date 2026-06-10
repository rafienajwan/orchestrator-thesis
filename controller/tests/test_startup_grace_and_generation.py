"""Tests for startup grace period and service generation in SelfHealingManager."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

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
async def test_unhealthy_report_ignored_during_startup_grace() -> None:
    """Unhealthy health reports should be ignored during startup grace period."""
    store = InMemoryStateStore()
    agent = FakeAgentClient()
    settings = ControllerSettings(
        max_restart_attempts=2,
        health_check_retries=3,
        reconciliation_interval_seconds=15,
        startup_grace_period_seconds=60,
    )
    manager = SelfHealingManager(settings=settings, store=store, agent_client=agent)

    # Setup running service
    await store.upsert_node_heartbeat("node-a", "http://node-a:8080")
    await store.upsert_node_snapshot(
        "node-a", ResourceSnapshot(cpu_utilization=0.2, memory_utilization=0.2)
    )
    spec = ServiceSpec(service_id="svc-1", image="test:latest")
    await store.set_service_desired(
        ServiceDesiredState(service=spec, status=DeploymentStatus.running)
    )
    await store.set_placement(Placement(service_id="svc-1", node_id="node-a"))

    # Set observed with startup grace
    grace_until = datetime.now(UTC) + timedelta(seconds=60)
    await store.set_service_observed(
        ServiceObservedState(
            service_id="svc-1",
            status=DeploymentStatus.running,
            health=ServiceHealth.unknown,
            node_id="node-a",
            startup_grace_until=grace_until,
        )
    )

    # Send unhealthy report with enough consecutive failures
    report = AgentHealthReport(
        node_id="node-a",
        service_id="svc-1",
        healthy=False,
        consecutive_failures=5,
    )
    await manager.handle_health_report(report)

    # Should NOT restart during grace
    assert len(agent.restart_calls) == 0


@pytest.mark.asyncio
async def test_healthy_report_clears_startup_grace() -> None:
    """Healthy report should clear the startup grace period."""
    store = InMemoryStateStore()
    agent = FakeAgentClient()
    settings = ControllerSettings(
        max_restart_attempts=2,
        health_check_retries=3,
        reconciliation_interval_seconds=15,
        startup_grace_period_seconds=60,
    )
    manager = SelfHealingManager(settings=settings, store=store, agent_client=agent)

    await store.upsert_node_heartbeat("node-a", "http://node-a:8080")
    await store.upsert_node_snapshot(
        "node-a", ResourceSnapshot(cpu_utilization=0.2, memory_utilization=0.2)
    )
    spec = ServiceSpec(service_id="svc-1", image="test:latest")
    await store.set_service_desired(
        ServiceDesiredState(service=spec, status=DeploymentStatus.running)
    )
    await store.set_placement(Placement(service_id="svc-1", node_id="node-a"))

    grace_until = datetime.now(UTC) + timedelta(seconds=60)
    await store.set_service_observed(
        ServiceObservedState(
            service_id="svc-1",
            status=DeploymentStatus.running,
            health=ServiceHealth.unknown,
            node_id="node-a",
            startup_grace_until=grace_until,
        )
    )

    # Send healthy report
    report = AgentHealthReport(
        node_id="node-a",
        service_id="svc-1",
        healthy=True,
    )
    await manager.handle_health_report(report)

    # Grace should be cleared
    observed = await store.get_service_observed("svc-1")
    assert observed is not None
    assert observed.startup_grace_until is None
    assert observed.health == ServiceHealth.healthy


@pytest.mark.asyncio
async def test_stale_health_report_rejected_by_generation() -> None:
    """Health report with old generation should be silently ignored."""
    store = InMemoryStateStore()
    agent = FakeAgentClient()
    settings = ControllerSettings(
        max_restart_attempts=2,
        health_check_retries=3,
        reconciliation_interval_seconds=15,
    )
    manager = SelfHealingManager(settings=settings, store=store, agent_client=agent)

    await store.upsert_node_heartbeat("node-a", "http://node-a:8080")
    await store.upsert_node_snapshot(
        "node-a", ResourceSnapshot(cpu_utilization=0.2, memory_utilization=0.2)
    )
    spec = ServiceSpec(service_id="svc-1", image="test:latest")
    await store.set_service_desired(
        ServiceDesiredState(service=spec, status=DeploymentStatus.running)
    )
    await store.set_placement(Placement(service_id="svc-1", node_id="node-a"))
    await store.set_service_observed(
        ServiceObservedState(
            service_id="svc-1",
            status=DeploymentStatus.running,
            health=ServiceHealth.healthy,
            node_id="node-a",
            service_generation=2,
        )
    )

    # Set generation to 2
    await store.increment_service_generation("svc-1")
    await store.increment_service_generation("svc-1")

    # Send unhealthy report with old generation (1)
    report = AgentHealthReport(
        node_id="node-a",
        service_id="svc-1",
        healthy=False,
        consecutive_failures=5,
        service_generation=1,
    )
    await manager.handle_health_report(report)

    # Should NOT restart (stale generation)
    assert len(agent.restart_calls) == 0


@pytest.mark.asyncio
async def test_health_report_without_generation_still_accepted() -> None:
    """Health reports without service_generation (backward compat) should work normally."""
    store = InMemoryStateStore()
    agent = FakeAgentClient()
    settings = ControllerSettings(
        max_restart_attempts=2,
        health_check_retries=3,
        reconciliation_interval_seconds=15,
    )
    manager = SelfHealingManager(settings=settings, store=store, agent_client=agent)

    await store.upsert_node_heartbeat("node-a", "http://node-a:8080")
    await store.upsert_node_snapshot(
        "node-a", ResourceSnapshot(cpu_utilization=0.2, memory_utilization=0.2)
    )
    spec = ServiceSpec(service_id="svc-1", image="test:latest")
    await store.set_service_desired(
        ServiceDesiredState(service=spec, status=DeploymentStatus.running)
    )
    await store.set_placement(Placement(service_id="svc-1", node_id="node-a"))
    await store.set_service_observed(
        ServiceObservedState(
            service_id="svc-1",
            status=DeploymentStatus.running,
            health=ServiceHealth.healthy,
            node_id="node-a",
        )
    )

    # Report WITHOUT service_generation (None = accept always)
    report = AgentHealthReport(
        node_id="node-a",
        service_id="svc-1",
        healthy=False,
        consecutive_failures=3,
    )
    await manager.handle_health_report(report)

    # Should restart normally
    assert len(agent.restart_calls) == 1


@pytest.mark.asyncio
async def test_reschedule_sets_generation_and_grace() -> None:
    """After reschedule from unreachable node, generation and grace should be set."""
    store = InMemoryStateStore()
    agent = FakeAgentClient()
    settings = ControllerSettings(
        max_restart_attempts=2,
        health_check_retries=3,
        reconciliation_interval_seconds=15,
        startup_grace_period_seconds=45,
    )
    manager = SelfHealingManager(settings=settings, store=store, agent_client=agent)

    # Node A (will become unreachable)
    await store.upsert_node_heartbeat("node-a", "http://node-a:8080")
    await store.upsert_node_snapshot(
        "node-a", ResourceSnapshot(cpu_utilization=0.2, memory_utilization=0.2)
    )
    # Node B (healthy target)
    await store.upsert_node_heartbeat("node-b", "http://node-b:8080")
    await store.upsert_node_snapshot(
        "node-b", ResourceSnapshot(cpu_utilization=0.1, memory_utilization=0.1)
    )

    spec = ServiceSpec(service_id="svc-1", image="test:latest")
    await store.set_service_desired(
        ServiceDesiredState(service=spec, status=DeploymentStatus.running)
    )
    await store.set_placement(Placement(service_id="svc-1", node_id="node-a"))
    await store.set_service_observed(
        ServiceObservedState(
            service_id="svc-1",
            status=DeploymentStatus.running,
            health=ServiceHealth.healthy,
            node_id="node-a",
        )
    )

    # Mark node-a as unreachable and trigger reschedule
    await store.mark_node_unavailable("node-a")
    await manager.handle_node_unreachable("node-a")

    # Verify reschedule happened
    placement = await store.get_placement("svc-1")
    assert placement is not None
    assert placement.node_id == "node-b"

    # Check generation was set
    gen = await store.get_service_generation("svc-1")
    assert gen == 1

    # Check startup grace was set
    observed = await store.get_service_observed("svc-1")
    assert observed is not None
    assert observed.service_generation == 1
    assert observed.startup_grace_until is not None
    assert observed.startup_grace_until > datetime.now(UTC)
