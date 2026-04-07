from __future__ import annotations

import pytest

from controller.state_store import InMemoryStateStore


@pytest.mark.asyncio
async def test_heartbeat_without_node_address_keeps_unknown() -> None:
    store = InMemoryStateStore()

    await store.upsert_node_heartbeat("worker-1", "http://agent-1:8080")

    nodes = await store.list_nodes()
    assert len(nodes) == 1
    assert nodes[0].node_address == "unknown"


@pytest.mark.asyncio
async def test_heartbeat_does_not_overwrite_existing_node_address_with_unknown() -> None:
    store = InMemoryStateStore()

    await store.upsert_node_heartbeat(
        "worker-1", "http://agent-1:8080", node_address="10.0.1.21"
    )
    await store.upsert_node_heartbeat("worker-1", "http://agent-1:8080", node_address="unknown")

    nodes = await store.list_nodes()
    assert len(nodes) == 1
    assert nodes[0].node_address == "10.0.1.21"
