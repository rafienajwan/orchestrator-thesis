from __future__ import annotations

import logging
import zlib
from datetime import UTC, datetime
from typing import Protocol

import httpx

from agent.app.adapters.docker_adapter import DockerAdapter
from agent.app.core.config import AgentSettings
from agent.app.core.models import AgentLocalState, ExecuteResponse, WorkloadRecord, WorkloadStatus
from agent.app.core.state import AgentStateStore
from controller.models import AgentHealthReport, ServiceSpec

logger = logging.getLogger(__name__)


class WorkloadManager(Protocol):
    async def deploy(self, service: ServiceSpec) -> ExecuteResponse: ...

    async def stop(self, service_id: str) -> ExecuteResponse: ...

    async def restart(self, service_id: str) -> ExecuteResponse: ...

    async def health_check_all(self) -> list[AgentHealthReport]: ...

    async def get_local_state(self) -> AgentLocalState: ...


class AgentWorkloadManager(WorkloadManager):
    def __init__(
        self, settings: AgentSettings, store: AgentStateStore, adapter: DockerAdapter
    ) -> None:
        self._settings = settings
        self._store = store
        self._adapter = adapter

    async def deploy(self, service: ServiceSpec) -> ExecuteResponse:
        service_with_port = await self._ensure_published_port(service)
        current = await self._store.get_workload(service.service_id)
        existing_local = await self._adapter.inspect(service.service_id)
        if (
            current is not None
            and current.status == WorkloadStatus.running
            and current.container_id is not None
            and existing_local is not None
            and existing_local.container_id == current.container_id
        ):
            return ExecuteResponse(
                service_id=service.service_id,
                container_id=current.container_id,
                status="already_running",
                node_id=self._settings.node_id,
            )

        container = await self._adapter.deploy(service_with_port)
        workload = WorkloadRecord(
            service=service_with_port,
            container_id=container.container_id,
            published_port=container.published_port,
            container_ip=container.container_ip,
            status=WorkloadStatus.running,
            health_failures=0,
            last_health_check_at=None,
            last_healthy_at=datetime.now(UTC),
            last_unhealthy_at=None,
        )
        await self._store.upsert_workload(workload)
        return ExecuteResponse(
            service_id=service.service_id,
            container_id=container.container_id,
            status="running",
            node_id=self._settings.node_id,
        )

    async def stop(self, service_id: str) -> ExecuteResponse:
        current = await self._store.get_workload(service_id)
        if current is None:
            return ExecuteResponse(
                service_id=service_id,
                container_id="",
                status="stopped",
                node_id=self._settings.node_id,
            )

        await self._adapter.stop(service_id)
        updated = current.model_copy(
            update={
                "status": WorkloadStatus.stopped,
                "health_failures": 0,
                "last_unhealthy_at": None,
            }
        )
        await self._store.upsert_workload(updated)
        return ExecuteResponse(
            service_id=service_id,
            container_id=current.container_id or "",
            status="stopped",
            node_id=self._settings.node_id,
        )

    async def restart(self, service_id: str) -> ExecuteResponse:
        current = await self._store.get_workload(service_id)
        if current is None:
            raise KeyError(service_id)

        await self._adapter.restart(service_id)
        updated = current.model_copy(
            update={
                "status": WorkloadStatus.running,
                "health_failures": 0,
                "last_healthy_at": datetime.now(UTC),
                "last_unhealthy_at": None,
            }
        )
        await self._store.upsert_workload(updated)
        return ExecuteResponse(
            service_id=service_id,
            container_id=current.container_id or "",
            status="running",
            node_id=self._settings.node_id,
        )

    async def health_check_all(self) -> list[AgentHealthReport]:
        workloads = await self._store.list_workloads()
        reports: list[AgentHealthReport] = []
        for workload in workloads:
            if workload.status not in {WorkloadStatus.running, WorkloadStatus.unhealthy}:
                continue
            health_resolution = await self._resolve_internal_health_url(workload)
            if health_resolution is None:
                logger.warning(
                    "Skipping health check because no internal container endpoint is available",
                    extra={
                        "service_id": workload.service.service_id,
                        "node_id": self._settings.node_id,
                    },
                )
                continue

            health_url, resolved_container_ip = health_resolution
            if resolved_container_ip is not None and resolved_container_ip != workload.container_ip:
                workload = workload.model_copy(update={"container_ip": resolved_container_ip})

            is_healthy = await self._check_workload_health(workload, health_url)
            if is_healthy:
                updated = workload.model_copy(
                    update={
                        "status": WorkloadStatus.running,
                        "health_failures": 0,
                        "last_health_check_at": datetime.now(UTC),
                        "last_healthy_at": datetime.now(UTC),
                        "last_unhealthy_at": None,
                    }
                )
            else:
                updated = workload.model_copy(
                    update={
                        "health_failures": workload.health_failures + 1,
                        "status": WorkloadStatus.unhealthy,
                        "last_health_check_at": datetime.now(UTC),
                        "last_unhealthy_at": datetime.now(UTC),
                    }
                )
            await self._store.upsert_workload(updated)
            reports.append(
                AgentHealthReport(
                    node_id=self._settings.node_id,
                    service_id=workload.service.service_id,
                    healthy=is_healthy,
                    consecutive_failures=updated.health_failures,
                )
            )
        return reports

    async def get_local_state(self) -> AgentLocalState:
        state = await self._store.get_state()
        local_workloads: dict[str, WorkloadRecord] = {}
        for service_id, workload in state.workloads.items():
            container = await self._adapter.inspect(service_id)
            if container is None:
                continue
            local_workloads[service_id] = workload.model_copy(
                update={
                    "container_id": container.container_id,
                    "published_port": container.published_port,
                    "container_ip": container.container_ip,
                }
            )
        return state.model_copy(update={"workloads": local_workloads})

    async def _check_workload_health(self, workload: WorkloadRecord, url: str) -> bool:
        timeout = httpx.Timeout(self._settings.health_check_timeout_seconds)
        async with httpx.AsyncClient(timeout=timeout) as client:
            for _ in range(self._settings.health_check_retries):
                try:
                    response = await client.get(url)
                    if 200 <= response.status_code < 300:
                        return True
                except httpx.HTTPError as exc:
                    logger.debug(
                        "Health check attempt failed",
                        extra={
                            "service_id": workload.service.service_id,
                            "node_id": self._settings.node_id,
                            "url": url,
                            "error": str(exc),
                        },
                    )
                    continue
        return False

    async def _resolve_internal_health_url(
        self, workload: WorkloadRecord
    ) -> tuple[str, str | None] | None:
        container_ip = workload.container_ip
        if container_ip is None:
            refreshed = await self._adapter.inspect(workload.service.service_id)
            if refreshed is not None:
                container_ip = refreshed.container_ip
                if container_ip is not None:
                    await self._store.upsert_workload(
                        workload.model_copy(update={"container_ip": container_ip})
                    )

        if container_ip is None:
            return None

        return (
            f"http://{container_ip}:{workload.service.internal_port}{workload.service.health_endpoint}",
            container_ip,
        )

    async def _ensure_published_port(self, service: ServiceSpec) -> ServiceSpec:
        if service.published_port is not None:
            return service

        current_workloads = await self._store.list_workloads()
        used = {
            record.published_port
            for record in current_workloads
            if record.published_port is not None and record.service.service_id != service.service_id
        }

        base = self._settings.published_port_base
        maximum = self._settings.published_port_max
        span = (maximum - base) + 1
        if span <= 0:
            raise ValueError("Invalid published port range configuration")

        start = base + (zlib.crc32(service.service_id.encode("utf-8")) % span)
        for offset in range(span):
            candidate = base + ((start - base + offset) % span)
            if candidate in used:
                continue
            return service.model_copy(update={"published_port": candidate})

        raise ValueError("No available published port in configured range")
