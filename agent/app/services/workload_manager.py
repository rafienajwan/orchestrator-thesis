from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol

import httpx

from agent.app.adapters.docker_adapter import DockerAdapter
from agent.app.core.config import AgentSettings
from agent.app.core.models import AgentLocalState, ExecuteResponse, WorkloadRecord, WorkloadStatus
from agent.app.core.state import AgentStateStore
from controller.models import AgentHealthReport, ServiceSpec


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

        container = await self._adapter.deploy(service)
        workload = WorkloadRecord(
            service=service,
            container_id=container.container_id,
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
            is_healthy = await self._check_workload_health(workload)
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
                    "container_ip": container.container_ip,
                }
            )
        return state.model_copy(update={"workloads": local_workloads})

    async def _check_workload_health(self, workload: WorkloadRecord) -> bool:
        if workload.container_ip is None:
            return False

        url = f"http://{workload.container_ip}:{workload.service.internal_port}{workload.service.health_endpoint}"
        timeout = httpx.Timeout(self._settings.health_check_timeout_seconds)
        async with httpx.AsyncClient(timeout=timeout) as client:
            for _ in range(self._settings.health_check_retries):
                try:
                    response = await client.get(url)
                    if 200 <= response.status_code < 300:
                        return True
                except httpx.HTTPError:
                    continue
        return False
