from __future__ import annotations

from typing import Any
from unittest.mock import mock_open

import pytest

from agent.app.services.telemetry import ResourceSampler, _read_proc_meminfo


def test_resource_sampler_parses_proc_stat_delta_and_memory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sampler = ResourceSampler()
    stat_reads = iter(
        [
            "cpu 100 0 50 850 0 0 0 0 0 0\n",
            "cpu 150 0 50 900 0 0 0 0 0 0\n",
        ]
    )
    meminfo_data = "MemTotal:       1000 kB\nMemAvailable:    250 kB\n"

    def fake_open(path: str, *args: object, **kwargs: object) -> Any:
        if path == "/proc/stat":
            return mock_open(read_data=next(stat_reads))()
        if path == "/proc/meminfo":
            return mock_open(read_data=meminfo_data)()
        raise FileNotFoundError(path)

    monkeypatch.setattr("builtins.open", fake_open)

    first_snapshot = sampler.sample()
    second_snapshot = sampler.sample()

    assert first_snapshot.cpu_utilization == 0.0
    assert first_snapshot.memory_utilization == 0.75
    assert second_snapshot.cpu_utilization == 0.5
    assert second_snapshot.memory_utilization == 0.75


def test_read_proc_meminfo_returns_expected_utilization(monkeypatch: pytest.MonkeyPatch) -> None:
    meminfo_data = "MemTotal:       2000 kB\nMemAvailable:    500 kB\n"

    def fake_open(path: str, *args: object, **kwargs: object) -> Any:
        if path == "/proc/meminfo":
            return mock_open(read_data=meminfo_data)()
        raise FileNotFoundError(path)

    monkeypatch.setattr("builtins.open", fake_open)

    mem_total, mem_available = _read_proc_meminfo()

    assert mem_total == 2000.0
    assert mem_available == 500.0
