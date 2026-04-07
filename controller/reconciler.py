from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from controller.config import ControllerSettings
from controller.models import EventType, NodeStatus
from controller.self_healing import SelfHealingManager
from controller.service_manager import ServiceManager
from controller.state_store import StateStore

logger = logging.getLogger(__name__)


class Reconciler:
    def __init__(
        self,
        settings: ControllerSettings,
        store: StateStore,
        service_manager: ServiceManager,
        self_healing: SelfHealingManager,
    ) -> None:
        self._settings = settings
        self._store = store
        self._service_manager = service_manager
        self._self_healing = self_healing

    async def run_once(self, now: datetime | None = None) -> None:
        reference_now = now or datetime.now(UTC)
        timeout_delta = timedelta(
            seconds=self._settings.heartbeat_timeout_seconds
            + self._settings.reconciliation_interval_seconds
        )

        nodes = await self._store.list_nodes()
        for node in nodes:
            if node.status != NodeStatus.healthy:
                continue
            if node.last_heartbeat_at >= reference_now - timeout_delta:
                continue

            await self._store.mark_node_unavailable(node.node_id)
            await self._store.append_event(
                EventType.node,
                "Node marked unavailable due to heartbeat timeout",
                {"node_id": node.node_id},
            )
            await self._self_healing.handle_node_unreachable(node.node_id, at=reference_now)

        pending = await self._store.list_pending_deployments()
        for pending_item in pending:
            await self._service_manager.retry_pending_deployment(pending_item.service_id)

    async def run_forever(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            try:
                await self.run_once()
            except Exception:
                logger.exception("Reconciliation loop failed")
            try:
                await asyncio.wait_for(
                    stop_event.wait(),
                    timeout=self._settings.reconciliation_interval_seconds,
                )
            except TimeoutError:
                continue
