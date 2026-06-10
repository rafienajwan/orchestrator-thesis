"""Tests for event-driven crash handling in SelfHealingManager."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from controller.agent_client import AgentDeployResponse
from controller.config import ControllerSettings
from controller.models import (
    AgentCrashReport,
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


async def _setup_running_service(
    store: InMemoryStateStore,
    service_id: str = "svc-1",
    node_id: str = "node-a",
) -> None:
    """Setup a running service with node, placement, desired, and observed state."""
    await store.upsert_node_heartbeat(node_id, f"http://{node_id}:8080")
    await store.upsert_node_snapshot(
        node_id, ResourceSnapshot(cpu_utilization=0.3, memory_utilization=0.3)
    )
    spec = ServiceSpec(service_id=service_id, image="test:latest")
    await store.set_service_desired(
        ServiceDesiredState(service=spec, status=DeploymentStatus.running)
    )
    await store.set_placement(Placement(service_id=service_id, node_id=node_id))
    await store.set_service_observed(
        ServiceObservedState(
            service_id=service_id,
            status=DeploymentStatus.running,
            health=ServiceHealth.healthy,
            node_id=node_id,
            container_id="old-container-id",
        )
    )


def _crash_report(
    service_id: str = "svc-1",
    node_id: str = "node-a",
    exit_code: int = 137,
    generation: int | None = None,
) -> AgentCrashReport:
    return AgentCrashReport(
        node_id=node_id,
        service_id=service_id,
        container_id="abc123",
        event_type="die",
        exit_code=exit_code,
        occurred_at=datetime.now(UTC),
        service_generation=generation,
    )


@pytest.mark.asyncio
async def test_crash_report_triggers_immediate_restart() -> None:
    """Crash report should trigger restart without waiting for health check failures."""
    store = InMemoryStateStore()
    agent = FakeAgentClient()
    settings = ControllerSettings(
        max_restart_attempts=2, health_check_retries=3, reconciliation_interval_seconds=15
    )
    manager = SelfHealingManager(settings=settings, store=store, agent_client=agent)

    await _setup_running_service(store)

    report = _crash_report()
    await manager.handle_crash_report(report)

    # Should have restarted immediately
    assert len(agent.restart_calls) == 1
    assert agent.restart_calls[0] == ("http://node-a:8080", "svc-1")
    counter = await store.get_restart_counter("svc-1")
    assert counter.count == 1


@pytest.mark.asyncio
async def test_crash_report_respects_max_restart_attempts() -> None:
    """After max restart attempts, crash should trigger reschedule."""
    store = InMemoryStateStore()
    agent = FakeAgentClient()
    settings = ControllerSettings(
        max_restart_attempts=1,
        health_check_retries=3,
        reconciliation_interval_seconds=15,
        startup_grace_period_seconds=0,  # Disable grace so second crash isn't blocked
        cooldown_intervals_after_recovery=0,
    )
    manager = SelfHealingManager(settings=settings, store=store, agent_client=agent)

    await _setup_running_service(store)

    # Second node for reschedule target
    await store.upsert_node_heartbeat("node-b", "http://node-b:8080")
    await store.upsert_node_snapshot(
        "node-b", ResourceSnapshot(cpu_utilization=0.2, memory_utilization=0.2)
    )

    # First crash → restart (count becomes 1, <= max=1)
    await manager.handle_crash_report(_crash_report())
    assert len(agent.restart_calls) == 1

    # Second crash → should reschedule (count becomes 2, > max=1)
    await manager.handle_crash_report(_crash_report())
    assert len(agent.deploy_calls) == 1  # reschedule calls deploy on new node


@pytest.mark.asyncio
async def test_crash_report_rejects_stale_generation() -> None:
    """Crash report with old generation should be ignored."""
    store = InMemoryStateStore()
    agent = FakeAgentClient()
    settings = ControllerSettings(
        max_restart_attempts=2, health_check_retries=3, reconciliation_interval_seconds=15
    )
    manager = SelfHealingManager(settings=settings, store=store, agent_client=agent)

    await _setup_running_service(store)

    # Increment generation to 1
    await store.increment_service_generation("svc-1")

    # Send crash report with generation=0 (stale)
    report = _crash_report(generation=0)
    await manager.handle_crash_report(report)

    # Should NOT restart
    assert len(agent.restart_calls) == 0


@pytest.mark.asyncio
async def test_crash_report_skipped_during_startup_grace() -> None:
    """Crash report during startup grace period should be ignored."""
    store = InMemoryStateStore()
    agent = FakeAgentClient()
    settings = ControllerSettings(
        max_restart_attempts=2,
        health_check_retries=3,
        reconciliation_interval_seconds=15,
        startup_grace_period_seconds=60,
    )
    manager = SelfHealingManager(settings=settings, store=store, agent_client=agent)

    await _setup_running_service(store)

    # Set startup grace
    observed = await store.get_service_observed("svc-1")
    assert observed is not None
    await store.set_service_observed(
        observed.model_copy(
            update={"startup_grace_until": datetime.now(UTC) + timedelta(seconds=60)}
        )
    )

    report = _crash_report()
    await manager.handle_crash_report(report)

    # Should NOT restart
    assert len(agent.restart_calls) == 0


@pytest.mark.asyncio
async def test_crash_report_ignored_for_wrong_node() -> None:
    """Crash from a node that doesn't match placement should be ignored."""
    store = InMemoryStateStore()
    agent = FakeAgentClient()
    settings = ControllerSettings(
        max_restart_attempts=2, health_check_retries=3, reconciliation_interval_seconds=15
    )
    manager = SelfHealingManager(settings=settings, store=store, agent_client=agent)

    await _setup_running_service(store, node_id="node-a")

    report = _crash_report(node_id="node-b")
    await manager.handle_crash_report(report)

    assert len(agent.restart_calls) == 0


@pytest.mark.asyncio
async def test_restart_sets_startup_grace_and_generation() -> None:
    """After restart, service_generation should increment and startup_grace should be set."""
    store = InMemoryStateStore()
    agent = FakeAgentClient()
    settings = ControllerSettings(
        max_restart_attempts=2,
        health_check_retries=3,
        reconciliation_interval_seconds=15,
        startup_grace_period_seconds=45,
    )
    manager = SelfHealingManager(settings=settings, store=store, agent_client=agent)

    await _setup_running_service(store)

    await manager.handle_crash_report(_crash_report())

    observed = await store.get_service_observed("svc-1")
    assert observed is not None
    assert observed.service_generation == 1
    assert observed.startup_grace_until is not None
    assert observed.startup_grace_until > datetime.now(UTC)

    gen = await store.get_service_generation("svc-1")
    assert gen == 1
