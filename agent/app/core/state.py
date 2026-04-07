from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from agent.app.core.models import AgentLocalState, WorkloadRecord, WorkloadStatus
from controller.models import ResourceSnapshot


class AgentStateStore:
    def __init__(self, node_id: str, agent_url: str, controller_url: str) -> None:
        self._state = AgentLocalState(
            node_id=node_id,
            agent_url=agent_url,
            controller_url=controller_url,
        )
        self._lock = asyncio.Lock()

    async def get_state(self) -> AgentLocalState:
        async with self._lock:
            return self._state.model_copy(deep=True)

    async def record_heartbeat(self, at: datetime) -> None:
        async with self._lock:
            self._state = self._state.model_copy(update={"last_heartbeat_at": at, "updated_at": at})

    async def record_snapshot(self, snapshot: ResourceSnapshot) -> None:
        async with self._lock:
            self._state = self._state.model_copy(
                update={"last_resource_snapshot": snapshot, "updated_at": snapshot.captured_at}
            )

    async def upsert_workload(self, workload: WorkloadRecord) -> None:
        async with self._lock:
            self._state.workloads[workload.service.service_id] = workload
            self._state.updated_at = datetime.now(UTC)

    async def get_workload(self, service_id: str) -> WorkloadRecord | None:
        async with self._lock:
            workload = self._state.workloads.get(service_id)
            return workload.model_copy(deep=True) if workload is not None else None

    async def list_workloads(self) -> list[WorkloadRecord]:
        async with self._lock:
            workloads = list(self._state.workloads.values())
            return sorted(
                (workload.model_copy(deep=True) for workload in workloads),
                key=lambda item: item.service.service_id,
            )

    async def remove_workload(self, service_id: str) -> None:
        async with self._lock:
            self._state.workloads.pop(service_id, None)
            self._state.updated_at = datetime.now(UTC)

    async def set_workload_status(
        self, service_id: str, status: WorkloadStatus
    ) -> WorkloadRecord | None:
        async with self._lock:
            workload = self._state.workloads.get(service_id)
            if workload is None:
                return None
            updated = workload.model_copy(
                update={"status": status, "updated_at": datetime.now(UTC)}
            )
            self._state.workloads[service_id] = updated
            self._state.updated_at = updated.updated_at
            return updated

    async def update_workload(self, workload: WorkloadRecord) -> None:
        await self.upsert_workload(workload)
