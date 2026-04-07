from __future__ import annotations

from datetime import UTC, datetime

import pytest

from controller.agent_client import AgentDeployResponse
from controller.config import ControllerSettings
from controller.models import (
    DeploymentStatus,
    PendingDeployment,
    ResourceSnapshot,
    ServiceDesiredState,
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
async def test_reconciler_retries_pending_deployment() -> None:
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
    await store.upsert_node_heartbeat("worker-1", "http://agent-1:8080", at=now)
    await store.upsert_node_snapshot(
        "worker-1", ResourceSnapshot(cpu_utilization=0.2, memory_utilization=0.2)
    )

    spec = ServiceSpec(service_id="sample-app", image="sample-app:latest")
    await store.set_service_desired(
        ServiceDesiredState(service=spec, status=DeploymentStatus.deploying)
    )
    await store.set_pending_deployment(
        PendingDeployment(service_id=spec.service_id, reason="initial placement pending")
    )

    await reconciler.run_once(now=now)

    placement = await store.get_placement(spec.service_id)
    observed = await store.get_service_observed(spec.service_id)
    pending = await store.list_pending_deployments()

    assert agent_client.deploy_calls == [("http://agent-1:8080", "sample-app")]
    assert placement is not None and placement.node_id == "worker-1"
    assert observed is not None and observed.status == DeploymentStatus.running
    assert pending == []
