from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from controller.config import ControllerSettings
from controller.models import NodeStatus, ResourceSnapshot
from controller.reconciler import Reconciler
from controller.self_healing import SelfHealingManager
from controller.service_manager import ServiceManager
from controller.state_store import InMemoryStateStore


class NoopAgentClient:
    async def deploy(self, agent_url: str, service):  # type: ignore[no-untyped-def]
        raise AssertionError("deploy should not be called")

    async def stop(self, agent_url: str, service_id: str) -> None:
        return None

    async def restart(self, agent_url: str, service_id: str) -> None:
        return None


@pytest.mark.asyncio
async def test_node_is_not_marked_unavailable_within_grace_window() -> None:
    settings = ControllerSettings(
        heartbeat_timeout_seconds=30,
        reconciliation_interval_seconds=15,
        health_check_retries=3,
        max_restart_attempts=2,
    )
    store = InMemoryStateStore()
    manager = Reconciler(
        settings=settings,
        store=store,
        service_manager=ServiceManager(store=store, agent_client=NoopAgentClient()),
        self_healing=SelfHealingManager(
            settings=settings, store=store, agent_client=NoopAgentClient()
        ),
    )

    now = datetime.now(UTC)
    stale_but_graceful = now - timedelta(seconds=35)
    await store.upsert_node_heartbeat("node-a", "http://node-a:8080", at=stale_but_graceful)
    await store.upsert_node_snapshot(
        "node-a", ResourceSnapshot(cpu_utilization=0.2, memory_utilization=0.2)
    )

    await manager.run_once(now=now)

    nodes = await store.list_nodes()
    assert len(nodes) == 1
    assert nodes[0].status == NodeStatus.healthy
