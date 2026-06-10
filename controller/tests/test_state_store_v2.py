"""Tests for heartbeat write coalescing and snapshot threshold in RedisStateStore."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from controller.models import NodeStatus, ResourceSnapshot
from controller.state_store import InMemoryStateStore


@pytest.mark.asyncio
async def test_service_generation_starts_at_zero() -> None:
    store = InMemoryStateStore()
    gen = await store.get_service_generation("svc-1")
    assert gen == 0


@pytest.mark.asyncio
async def test_increment_service_generation() -> None:
    store = InMemoryStateStore()
    gen1 = await store.increment_service_generation("svc-1")
    assert gen1 == 1
    gen2 = await store.increment_service_generation("svc-1")
    assert gen2 == 2
    current = await store.get_service_generation("svc-1")
    assert current == 2


@pytest.mark.asyncio
async def test_service_generations_are_independent() -> None:
    store = InMemoryStateStore()
    await store.increment_service_generation("svc-1")
    await store.increment_service_generation("svc-1")
    await store.increment_service_generation("svc-2")

    assert await store.get_service_generation("svc-1") == 2
    assert await store.get_service_generation("svc-2") == 1
    assert await store.get_service_generation("svc-3") == 0
