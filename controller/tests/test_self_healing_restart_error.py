from __future__ import annotations

from datetime import UTC, datetime

import pytest

from controller.agent_client import AgentClientError, AgentDeployResponse
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


class RestartFailingAgentClient:
    async def deploy(self, agent_url: str, service: ServiceSpec) -> AgentDeployResponse:
        return AgentDeployResponse(
            service_id=service.service_id, container_id="c-1", status="running"
        )

    async def stop(self, agent_url: str, service_id: str) -> None:
        return None

    async def restart(self, agent_url: str, service_id: str) -> None:
        raise AgentClientError("restart failed")


@pytest.mark.asyncio
async def test_restart_failure_marks_service_failed_and_pending() -> None:
    settings = ControllerSettings(
        health_check_retries=3, max_restart_attempts=2, reconciliation_interval_seconds=15
    )
    store = InMemoryStateStore()
    manager = SelfHealingManager(
        settings=settings, store=store, agent_client=RestartFailingAgentClient()
    )

    await store.upsert_node_heartbeat("node-a", "http://node-a:8080", at=datetime.now(UTC))
    await store.upsert_node_snapshot(
        "node-a", ResourceSnapshot(cpu_utilization=0.3, memory_utilization=0.3)
    )

    spec = ServiceSpec(service_id="svc-r", image="example/service:latest")
    await store.set_service_desired(
        ServiceDesiredState(service=spec, status=DeploymentStatus.running)
    )
    await store.set_service_observed(
        ServiceObservedState(
            service_id=spec.service_id,
            status=DeploymentStatus.running,
            health=ServiceHealth.unhealthy,
            node_id="node-a",
            container_id="old-c",
        )
    )
    await store.set_placement(Placement(service_id=spec.service_id, node_id="node-a"))

    await manager.handle_health_report(
        AgentHealthReport(
            node_id="node-a", service_id=spec.service_id, healthy=False, consecutive_failures=3
        )
    )

    observed = await store.get_service_observed(spec.service_id)
    assert observed is not None
    assert observed.status == DeploymentStatus.failed
    pending = await store.list_pending_deployments()
    assert len(pending) == 1
    assert pending[0].service_id == spec.service_id
