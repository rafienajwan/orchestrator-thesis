from __future__ import annotations

from agent.app.services.workload_manager import AgentWorkloadManager
from controller.models import AgentHealthReport


class HealthChecker:
    def __init__(self, workload_manager: AgentWorkloadManager) -> None:
        self._workload_manager = workload_manager

    async def check_all(self) -> list[AgentHealthReport]:
        return await self._workload_manager.health_check_all()
