from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

import httpx

from agent.app.core.config import AgentSettings
from agent.app.core.state import AgentStateStore
from agent.app.services.workload_manager import AgentWorkloadManager
from controller.models import (
    AgentCrashReport,
    AgentHealthReport,
    AgentHeartbeatReport,
    AgentResourceReport,
    ResourceSnapshot,
)

logger = logging.getLogger(__name__)


class ResourceSampler:
    def __init__(self) -> None:
        self._previous_cpu: tuple[int, int] | None = None

    def sample(self) -> ResourceSnapshot:
        cpu_utilization = self._sample_cpu_utilization()
        memory_utilization = self._sample_memory_utilization()
        return ResourceSnapshot(
            cpu_utilization=cpu_utilization,
            memory_utilization=memory_utilization,
        )

    def _sample_cpu_utilization(self) -> float:
        total, idle = _read_proc_stat()
        previous = self._previous_cpu
        self._previous_cpu = (total, idle)
        if previous is None:
            return 0.0

        previous_total, previous_idle = previous
        total_delta = total - previous_total
        idle_delta = idle - previous_idle
        if total_delta <= 0:
            return 0.0
        busy_delta = total_delta - idle_delta
        return max(0.0, min(1.0, busy_delta / total_delta))

    @staticmethod
    def _sample_memory_utilization() -> float:
        mem_total, mem_available = _read_proc_meminfo()
        if mem_total <= 0:
            return 0.0
        used = mem_total - mem_available
        return max(0.0, min(1.0, used / mem_total))


class ControllerReporter:
    def __init__(self, settings: AgentSettings) -> None:
        self._settings = settings
        # Shared connection pool for all controller communications
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(settings.health_check_timeout_seconds),
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def send_heartbeat(self) -> None:
        report = AgentHeartbeatReport(
            node_id=self._settings.node_id,
            agent_url=self._settings.agent_public_url,
            node_address=self._settings.advertised_host,
            sent_at=datetime.now(UTC),
        )
        await self._post("/api/internal/agent/heartbeat", report.model_dump(mode="json"))

    async def send_resource_snapshot(self, snapshot: ResourceSnapshot) -> None:
        report = AgentResourceReport(node_id=self._settings.node_id, snapshot=snapshot)
        await self._post("/api/internal/agent/resource-snapshot", report.model_dump(mode="json"))

    async def send_health_report(self, report: AgentHealthReport) -> None:
        await self._post("/api/internal/agent/health-report", report.model_dump(mode="json"))

    async def send_crash_report(self, report: AgentCrashReport) -> None:
        await self._post("/api/internal/agent/crash-report", report.model_dump(mode="json"))

    async def _post(self, path: str, payload: dict[str, object]) -> None:
        url = f"{self._settings.controller_base_url.rstrip('/')}{path}"
        try:
            response = await self._client.post(url, json=payload)
            response.raise_for_status()
        except httpx.HTTPError:
            return


class AgentTelemetryService:
    def __init__(
        self,
        settings: AgentSettings,
        store: AgentStateStore,
        workload_manager: AgentWorkloadManager,
        reporter: ControllerReporter,
        sampler: ResourceSampler,
    ) -> None:
        self._settings = settings
        self._store = store
        self._workload_manager = workload_manager
        self._reporter = reporter
        self._sampler = sampler
        # Adaptive resource snapshot tracking
        self._last_sent_snapshot: tuple[float, float] | None = None
        self._snapshot_change_threshold = 0.05  # 5% change threshold

    async def run(self, stop_event: asyncio.Event) -> None:
        await asyncio.gather(
            self._heartbeat_loop(stop_event),
            self._resource_snapshot_loop(stop_event),
            self._health_loop(stop_event),
        )

    async def _heartbeat_loop(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            now = datetime.now(UTC)
            await self._store.record_heartbeat(now)
            await self._reporter.send_heartbeat()
            await _sleep_or_stop(stop_event, self._settings.telemetry_interval_seconds)

    async def _resource_snapshot_loop(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            snapshot = self._sampler.sample()
            await self._store.record_snapshot(snapshot)

            # Only send to controller if values changed significantly
            should_send = True
            if self._last_sent_snapshot is not None:
                cpu_delta = abs(snapshot.cpu_utilization - self._last_sent_snapshot[0])
                mem_delta = abs(snapshot.memory_utilization - self._last_sent_snapshot[1])
                if (
                    cpu_delta < self._snapshot_change_threshold
                    and mem_delta < self._snapshot_change_threshold
                ):
                    should_send = False

            if should_send:
                await self._reporter.send_resource_snapshot(snapshot)
                self._last_sent_snapshot = (snapshot.cpu_utilization, snapshot.memory_utilization)

            await _sleep_or_stop(
                stop_event, self._settings.resource_snapshot_interval_seconds
            )

    async def _health_loop(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            reports = await self._workload_manager.health_check_all()
            for report in reports:
                await self._reporter.send_health_report(report)
            await _sleep_or_stop(stop_event, self._settings.health_check_interval_seconds)


async def _sleep_or_stop(stop_event: asyncio.Event, timeout_seconds: int) -> None:
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=timeout_seconds)
    except TimeoutError:
        return


def _read_proc_stat() -> tuple[int, int]:
    with open("/proc/stat", encoding="utf-8") as proc_stat:
        line = proc_stat.readline().strip()
    fields = line.split()
    values = [int(value) for value in fields[1:]]
    total = sum(values)
    idle = values[3] if len(values) > 3 else 0
    return total, idle


def _read_proc_meminfo() -> tuple[float, float]:
    mem_total = 0.0
    mem_available = 0.0
    with open("/proc/meminfo", encoding="utf-8") as meminfo:
        for line in meminfo:
            if line.startswith("MemTotal:"):
                mem_total = float(line.split()[1])
            elif line.startswith("MemAvailable:"):
                mem_available = float(line.split()[1])
    if mem_total <= 0:
        return 0.0, 0.0
    return mem_total, mem_available
