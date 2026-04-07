from __future__ import annotations

from pathlib import Path

import pytest

from controller.agent_client import (
    AgentClientError,
    AgentDeployResponse,
    AgentLocalStateResponse,
    AgentWorkloadState,
)
from controller.config import ControllerSettings
from controller.ingress_manager import IngressManager
from controller.models import (
    DeploymentStatus,
    EventType,
    Placement,
    ResourceSnapshot,
    ServiceObservedState,
    ServiceSpec,
)
from controller.state_store import InMemoryStateStore


class FakeAgentReader:
    def __init__(self) -> None:
        self._states: dict[str, AgentLocalStateResponse] = {}
        self.fail_reads = False

    async def deploy(self, agent_url: str, service: ServiceSpec) -> AgentDeployResponse:
        raise NotImplementedError

    async def stop(self, agent_url: str, service_id: str) -> None:
        return None

    async def restart(self, agent_url: str, service_id: str) -> None:
        return None

    async def get_local_state(self, agent_url: str) -> AgentLocalStateResponse:
        if self.fail_reads:
            raise AgentClientError("temporary read error")
        return self._states[agent_url]

    def set_workload(self, agent_url: str, service_id: str, container_ip: str, internal_port: int) -> None:
        spec = ServiceSpec(service_id=service_id, image="sample-app:latest", internal_port=internal_port)
        self._states[agent_url] = AgentLocalStateResponse(
            node_id=agent_url,
            workloads={
                service_id: AgentWorkloadState(
                    service=spec,
                    container_id=f"container-{service_id}",
                    container_ip=container_ip,
                    status="running",
                )
            },
        )


@pytest.mark.asyncio
async def test_ingress_updates_on_initial_deploy(tmp_path: Path) -> None:
    store = InMemoryStateStore()
    agent = FakeAgentReader()
    settings = ControllerSettings(
        ingress_enabled=True,
        ingress_active_service_id="sample-app",
        ingress_upstream_file_path=str(tmp_path / "active.conf"),
        nginx_reload_enabled=False,
    )
    manager = IngressManager(settings=settings, store=store, agent_client=agent)

    await store.upsert_node_heartbeat("worker-1", "http://agent-1:8080")
    await store.upsert_node_snapshot(
        "worker-1",
        ResourceSnapshot(cpu_utilization=0.2, memory_utilization=0.2),
    )
    await store.set_service_observed(
        ServiceObservedState(service_id="sample-app", status=DeploymentStatus.running, node_id="worker-1")
    )
    await store.set_placement(Placement(service_id="sample-app", node_id="worker-1"))
    agent.set_workload("http://agent-1:8080", "sample-app", "172.18.0.5", 8000)

    await manager.sync_service("sample-app", "deploy_succeeded")

    content = (tmp_path / "active.conf").read_text(encoding="utf-8")
    assert "server 172.18.0.5:8000;" in content
    events = await store.list_events()
    assert events[0].event_type == EventType.ingress


@pytest.mark.asyncio
async def test_ingress_updates_after_reschedule(tmp_path: Path) -> None:
    store = InMemoryStateStore()
    agent = FakeAgentReader()
    settings = ControllerSettings(
        ingress_enabled=True,
        ingress_active_service_id="sample-app",
        ingress_upstream_file_path=str(tmp_path / "active.conf"),
        nginx_reload_enabled=False,
    )
    manager = IngressManager(settings=settings, store=store, agent_client=agent)

    await store.upsert_node_heartbeat("worker-1", "http://agent-1:8080")
    await store.upsert_node_heartbeat("worker-2", "http://agent-2:8080")
    await store.upsert_node_snapshot("worker-1", ResourceSnapshot(cpu_utilization=0.1, memory_utilization=0.2))
    await store.upsert_node_snapshot("worker-2", ResourceSnapshot(cpu_utilization=0.1, memory_utilization=0.2))
    await store.set_service_observed(
        ServiceObservedState(service_id="sample-app", status=DeploymentStatus.running, node_id="worker-1")
    )

    await store.set_placement(Placement(service_id="sample-app", node_id="worker-1"))
    agent.set_workload("http://agent-1:8080", "sample-app", "172.18.0.5", 8000)
    await manager.sync_service("sample-app", "initial")

    await store.set_placement(Placement(service_id="sample-app", node_id="worker-2"))
    await store.set_service_observed(
        ServiceObservedState(service_id="sample-app", status=DeploymentStatus.running, node_id="worker-2")
    )
    agent.set_workload("http://agent-2:8080", "sample-app", "172.18.0.9", 8000)
    await manager.sync_service("sample-app", "rescheduled")

    content = (tmp_path / "active.conf").read_text(encoding="utf-8")
    assert "server 172.18.0.9:8000;" in content


@pytest.mark.asyncio
async def test_ingress_skips_reload_when_target_unchanged(tmp_path: Path) -> None:
    store = InMemoryStateStore()
    agent = FakeAgentReader()
    settings = ControllerSettings(
        ingress_enabled=True,
        ingress_active_service_id="sample-app",
        ingress_upstream_file_path=str(tmp_path / "active.conf"),
        nginx_reload_enabled=False,
    )
    manager = IngressManager(settings=settings, store=store, agent_client=agent)

    await store.upsert_node_heartbeat("worker-1", "http://agent-1:8080")
    await store.upsert_node_snapshot("worker-1", ResourceSnapshot(cpu_utilization=0.2, memory_utilization=0.2))
    await store.set_service_observed(
        ServiceObservedState(service_id="sample-app", status=DeploymentStatus.running, node_id="worker-1")
    )
    await store.set_placement(Placement(service_id="sample-app", node_id="worker-1"))
    agent.set_workload("http://agent-1:8080", "sample-app", "172.18.0.5", 8000)

    await manager.sync_service("sample-app", "deploy")
    first = (tmp_path / "active.conf").read_text(encoding="utf-8")
    await manager.sync_service("sample-app", "restart_same_node")
    second = (tmp_path / "active.conf").read_text(encoding="utf-8")

    assert first == second
    ingress_events = [event for event in await store.list_events() if event.event_type == EventType.ingress]
    assert len(ingress_events) == 1


@pytest.mark.asyncio
async def test_ingress_clears_target_when_no_active_placement(tmp_path: Path) -> None:
    store = InMemoryStateStore()
    agent = FakeAgentReader()
    settings = ControllerSettings(
        ingress_enabled=True,
        ingress_active_service_id="sample-app",
        ingress_upstream_file_path=str(tmp_path / "active.conf"),
        nginx_reload_enabled=False,
    )
    manager = IngressManager(settings=settings, store=store, agent_client=agent)

    await store.set_service_observed(
        ServiceObservedState(service_id="sample-app", status=DeploymentStatus.stopped)
    )

    await manager.sync_service("sample-app", "stopped")

    content = (tmp_path / "active.conf").read_text(encoding="utf-8")
    assert "server " not in content


@pytest.mark.asyncio
async def test_ingress_keeps_existing_target_on_transient_agent_read_failure(tmp_path: Path) -> None:
    store = InMemoryStateStore()
    agent = FakeAgentReader()
    settings = ControllerSettings(
        ingress_enabled=True,
        ingress_active_service_id="sample-app",
        ingress_upstream_file_path=str(tmp_path / "active.conf"),
        nginx_reload_enabled=False,
    )
    manager = IngressManager(settings=settings, store=store, agent_client=agent)

    await store.upsert_node_heartbeat("worker-1", "http://agent-1:8080")
    await store.upsert_node_snapshot("worker-1", ResourceSnapshot(cpu_utilization=0.2, memory_utilization=0.2))
    await store.set_service_observed(
        ServiceObservedState(service_id="sample-app", status=DeploymentStatus.running, node_id="worker-1")
    )
    await store.set_placement(Placement(service_id="sample-app", node_id="worker-1"))
    agent.set_workload("http://agent-1:8080", "sample-app", "172.18.0.5", 8000)

    await manager.sync_service("sample-app", "initial")
    baseline = (tmp_path / "active.conf").read_text(encoding="utf-8")

    agent.fail_reads = True
    await manager.sync_service("sample-app", "transient_read_failure")

    after = (tmp_path / "active.conf").read_text(encoding="utf-8")
    assert after == baseline


@pytest.mark.asyncio
async def test_ingress_disabled_skips_write(tmp_path: Path) -> None:
    store = InMemoryStateStore()
    agent = FakeAgentReader()
    settings = ControllerSettings(
        ingress_enabled=False,
        ingress_active_service_id="sample-app",
        ingress_upstream_file_path=str(tmp_path / "active.conf"),
        nginx_reload_enabled=False,
    )
    manager = IngressManager(settings=settings, store=store, agent_client=agent)

    await manager.sync_service("sample-app", "disabled")
    assert not (tmp_path / "active.conf").exists()
