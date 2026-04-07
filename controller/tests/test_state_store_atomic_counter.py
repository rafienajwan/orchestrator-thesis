from __future__ import annotations

import asyncio

import pytest

from controller.state_store import InMemoryStateStore


@pytest.mark.asyncio
async def test_restart_counter_in_memory_monotonic_under_concurrency() -> None:
    store = InMemoryStateStore()

    async def _inc() -> int:
        return await store.increment_restart_counter("svc-atomic")

    results = await asyncio.gather(*[_inc() for _ in range(20)])

    assert sorted(results) == list(range(1, 21))
    counter = await store.get_restart_counter("svc-atomic")
    assert counter.count == 20
