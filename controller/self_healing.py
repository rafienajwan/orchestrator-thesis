from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from controller.agent_client import AgentClient, AgentClientError
from controller.config import ControllerSettings
from controller.models import (
    AgentHealthReport,
    DeploymentStatus,
    EventType,
    NodeState,
    PendingDeployment,
    Placement,
    RecoveryRecord,
    ServiceHealth,
    ServiceObservedState,
    ServiceSpec,
)
from controller.scheduler import ScheduleDecision, choose_node_least_load
from controller.state_store import StateStore


class SelfHealingManager:
    def __init__(
        self,
        settings: ControllerSettings,
        store: StateStore,
        agent_client: AgentClient,
        scheduler: Callable[
            [ServiceSpec, list[NodeState]], ScheduleDecision
        ] = choose_node_least_load,
    ) -> None:
        self._settings = settings
        self._store = store
        self._agent_client = agent_client
        self._scheduler = scheduler
        self._cooldown_until: dict[str, datetime] = {}

    async def handle_health_report(self, report: AgentHealthReport) -> None:
        service_lock = await self._store.acquire_service_lock(report.service_id)
        async with service_lock:
            observed = await self._store.get_service_observed(report.service_id)
            if observed is None:
                return

            desired = await self._store.get_service_desired(report.service_id)
            placement = await self._store.get_placement(report.service_id)
            if desired is None or desired.status == DeploymentStatus.stopped or placement is None:
                return
            if placement.node_id != report.node_id:
                return

            if report.healthy:
                await self._store.pop_pending_deployment(report.service_id)
                await self._store.reset_restart_counter(report.service_id)
                self._cooldown_until.pop(report.service_id, None)
                await self._store.set_service_observed(
                    observed.model_copy(
                        update={
                            "health": ServiceHealth.healthy,
                            "status": DeploymentStatus.running,
                            "last_reported_at": report.observed_at,
                        }
                    )
                )
                await self._store.set_service_desired(
                    desired.model_copy(
                        update={
                            "status": DeploymentStatus.running,
                            "updated_at": report.observed_at,
                        }
                    )
                )
                return

            await self._store.set_service_observed(
                observed.model_copy(
                    update={
                        "health": ServiceHealth.unhealthy,
                        "last_reported_at": report.observed_at,
                    }
                )
            )

            if report.consecutive_failures < self._settings.health_check_retries:
                return

            if self._is_in_cooldown(report.service_id, report.observed_at):
                return

            current_node = _find_node(await self._store.list_nodes(), placement.node_id)
            if current_node is None:
                await self._store.set_pending_deployment(
                    PendingDeployment(
                        service_id=report.service_id,
                        reason="Current node missing during health reconciliation",
                        created_at=report.observed_at,
                    )
                )
                await self._store.set_service_observed(
                    ServiceObservedState(
                        service_id=report.service_id,
                        status=DeploymentStatus.pending,
                        health=ServiceHealth.unhealthy,
                        node_id=placement.node_id,
                        last_reported_at=report.observed_at,
                    )
                )
                self._set_cooldown(report.service_id, report.observed_at)
                return

            restart_count = await self._store.increment_restart_counter(report.service_id)
            if restart_count <= self._settings.max_restart_attempts:
                await self._restart_same_node(report.service_id, current_node, report.observed_at)
                return

            await self._reschedule_after_exhausted_restart(
                service_id=report.service_id,
                current_node_id=current_node.node_id,
                at=report.observed_at,
            )

    async def handle_node_unreachable(self, node_id: str, at: datetime | None = None) -> None:
        now = at or datetime.now(UTC)
        placements = await self._store.list_placements()

        for placement in placements:
            if placement.node_id != node_id:
                continue
            service_lock = await self._store.acquire_service_lock(placement.service_id)
            async with service_lock:
                await self._reschedule_from_unreachable_node(placement, now)

    async def _restart_same_node(self, service_id: str, node: NodeState, at: datetime) -> None:
        try:
            await self._agent_client.restart(node.agent_url, service_id)
        except AgentClientError:
            await self._store.set_pending_deployment(
                PendingDeployment(
                    service_id=service_id,
                    reason="Restart attempt failed on current node",
                    created_at=at,
                )
            )
            await self._store.set_service_observed(
                ServiceObservedState(
                    service_id=service_id,
                    status=DeploymentStatus.failed,
                    health=ServiceHealth.unhealthy,
                    node_id=node.node_id,
                    last_reported_at=at,
                )
            )
            await self._store.append_event(
                EventType.self_healing,
                "Service restart failed on current node",
                {"service_id": service_id, "node_id": node.node_id},
            )
            self._set_cooldown(service_id, at)
            return

        observed = await self._store.get_service_observed(service_id)
        if observed is not None:
            await self._store.set_service_observed(
                observed.model_copy(
                    update={
                        "health": ServiceHealth.unknown,
                        "status": DeploymentStatus.running,
                        "last_reported_at": at,
                    }
                )
            )

        await self._store.add_recovery_record(
            RecoveryRecord(
                service_id=service_id,
                action="restart",
                reason="health_check_failed",
                from_node_id=node.node_id,
                to_node_id=node.node_id,
                occurred_at=at,
            )
        )
        await self._store.append_event(
            EventType.self_healing,
            "Service restarted after consecutive health failures",
            {"service_id": service_id, "node_id": node.node_id},
        )
        self._set_cooldown(service_id, at)

    async def _reschedule_after_exhausted_restart(
        self, service_id: str, current_node_id: str, at: datetime
    ) -> None:
        desired = await self._store.get_service_desired(service_id)
        if desired is None:
            return

        nodes = await self._store.list_nodes()
        candidates = [node for node in nodes if node.node_id != current_node_id]
        decision = self._scheduler(desired.service, candidates)

        if decision.selected_node_id is None:
            await self._store.set_pending_deployment(
                PendingDeployment(
                    service_id=service_id,
                    reason="Restart exhausted and no healthy candidate for reschedule",
                    created_at=at,
                )
            )
            await self._store.set_service_observed(
                ServiceObservedState(
                    service_id=service_id,
                    status=DeploymentStatus.pending,
                    health=ServiceHealth.unhealthy,
                    node_id=current_node_id,
                    last_reported_at=at,
                )
            )
            self._set_cooldown(service_id, at)
            return

        target = _find_node(nodes, decision.selected_node_id)
        if target is None:
            await self._store.set_pending_deployment(
                PendingDeployment(
                    service_id=service_id,
                    reason="Restart exhausted and selected target node is no longer available",
                    created_at=at,
                )
            )
            await self._store.set_service_observed(
                ServiceObservedState(
                    service_id=service_id,
                    status=DeploymentStatus.pending,
                    health=ServiceHealth.unhealthy,
                    node_id=current_node_id,
                    last_reported_at=at,
                )
            )
            self._set_cooldown(service_id, at)
            return

        try:
            deploy_result = await self._agent_client.deploy(target.agent_url, desired.service)
        except AgentClientError:
            await self._store.set_pending_deployment(
                PendingDeployment(
                    service_id=service_id,
                    reason="Restart exhausted and target node deploy failed",
                    created_at=at,
                )
            )
            await self._store.set_service_observed(
                ServiceObservedState(
                    service_id=service_id,
                    status=DeploymentStatus.failed,
                    health=ServiceHealth.unhealthy,
                    node_id=current_node_id,
                    last_reported_at=at,
                )
            )
            await self._store.append_event(
                EventType.self_healing,
                "Service reschedule failed after restart limit reached",
                {
                    "service_id": service_id,
                    "from_node_id": current_node_id,
                    "candidate_node_id": target.node_id,
                },
            )
            self._set_cooldown(service_id, at)
            return

        old_node = _find_node(nodes, current_node_id)
        if old_node is not None:
            try:
                await self._agent_client.stop(old_node.agent_url, service_id)
            except AgentClientError:
                await self._store.append_event(
                    EventType.self_healing,
                    "Best-effort stop on previous node failed",
                    {"service_id": service_id, "node_id": current_node_id},
                )

        try:
            await self._store.set_placement(
                Placement(service_id=service_id, node_id=target.node_id, placed_at=at)
            )
            await self._store.reset_restart_counter(service_id)
            await self._store.pop_pending_deployment(service_id)
            await self._store.set_service_observed(
                ServiceObservedState(
                    service_id=service_id,
                    status=DeploymentStatus.running,
                    health=ServiceHealth.unknown,
                    container_id=deploy_result.container_id,
                    node_id=target.node_id,
                    last_reported_at=at,
                )
            )
        except Exception:
            try:
                await self._agent_client.stop(target.agent_url, service_id)
            except AgentClientError:
                pass
            raise
        await self._store.add_recovery_record(
            RecoveryRecord(
                service_id=service_id,
                action="reschedule",
                reason="restart_exhausted",
                from_node_id=current_node_id,
                to_node_id=target.node_id,
                occurred_at=at,
            )
        )
        await self._store.append_event(
            EventType.self_healing,
            "Service rescheduled after restart limit reached",
            {
                "service_id": service_id,
                "from_node_id": current_node_id,
                "to_node_id": target.node_id,
            },
        )
        self._set_cooldown(service_id, at)

    async def _reschedule_from_unreachable_node(self, placement: Placement, at: datetime) -> None:
        desired = await self._store.get_service_desired(placement.service_id)
        if desired is None:
            return

        nodes = await self._store.list_nodes()
        candidates = [node for node in nodes if node.node_id != placement.node_id]
        decision = self._scheduler(desired.service, candidates)

        if decision.selected_node_id is None:
            await self._store.set_pending_deployment(
                PendingDeployment(
                    service_id=placement.service_id,
                    reason="Node unreachable and no replacement node available",
                    created_at=at,
                )
            )
            await self._store.set_service_observed(
                ServiceObservedState(
                    service_id=placement.service_id,
                    status=DeploymentStatus.pending,
                    health=ServiceHealth.unknown,
                    node_id=None,
                    last_reported_at=at,
                )
            )
            return

        target = _find_node(nodes, decision.selected_node_id)
        if target is None:
            await self._store.set_pending_deployment(
                PendingDeployment(
                    service_id=placement.service_id,
                    reason="Node unreachable and selected target node is no longer available",
                    created_at=at,
                )
            )
            await self._store.set_service_observed(
                ServiceObservedState(
                    service_id=placement.service_id,
                    status=DeploymentStatus.pending,
                    health=ServiceHealth.unhealthy,
                    node_id=placement.node_id,
                    last_reported_at=at,
                )
            )
            return

        try:
            deploy_result = await self._agent_client.deploy(target.agent_url, desired.service)
        except AgentClientError:
            await self._store.set_pending_deployment(
                PendingDeployment(
                    service_id=placement.service_id,
                    reason="Node unreachable reschedule deploy failed",
                    created_at=at,
                )
            )
            await self._store.set_service_observed(
                ServiceObservedState(
                    service_id=placement.service_id,
                    status=DeploymentStatus.failed,
                    health=ServiceHealth.unhealthy,
                    node_id=placement.node_id,
                    last_reported_at=at,
                )
            )
            await self._store.append_event(
                EventType.self_healing,
                "Service reschedule failed due to unreachable node",
                {
                    "service_id": placement.service_id,
                    "from_node_id": placement.node_id,
                    "candidate_node_id": target.node_id,
                },
            )
            return

        try:
            await self._store.set_placement(
                Placement(service_id=placement.service_id, node_id=target.node_id, placed_at=at)
            )
            await self._store.pop_pending_deployment(placement.service_id)
            await self._store.reset_restart_counter(placement.service_id)
            await self._store.set_service_observed(
                ServiceObservedState(
                    service_id=placement.service_id,
                    status=DeploymentStatus.running,
                    health=ServiceHealth.unknown,
                    container_id=deploy_result.container_id,
                    node_id=target.node_id,
                    last_reported_at=at,
                )
            )
        except Exception:
            try:
                await self._agent_client.stop(target.agent_url, placement.service_id)
            except AgentClientError:
                pass
            raise
        await self._store.add_recovery_record(
            RecoveryRecord(
                service_id=placement.service_id,
                action="reschedule",
                reason="node_unreachable",
                from_node_id=placement.node_id,
                to_node_id=target.node_id,
                occurred_at=at,
            )
        )
        await self._store.append_event(
            EventType.self_healing,
            "Service rescheduled due to unreachable node",
            {
                "service_id": placement.service_id,
                "from_node_id": placement.node_id,
                "to_node_id": target.node_id,
            },
        )

    def _set_cooldown(self, service_id: str, at: datetime) -> None:
        interval_count = max(1, self._settings.cooldown_intervals_after_recovery)
        cooldown = timedelta(
            seconds=self._settings.reconciliation_interval_seconds * interval_count
        )
        self._cooldown_until[service_id] = at + cooldown

    def _is_in_cooldown(self, service_id: str, at: datetime) -> bool:
        until = self._cooldown_until.get(service_id)
        if until is None:
            return False
        return at < until


class SelfHealingCoordinator:
    def __init__(self, manager: SelfHealingManager) -> None:
        self._manager = manager

    async def on_health_report(self, report: AgentHealthReport) -> None:
        await self._manager.handle_health_report(report)

    async def on_node_unreachable(self, node_id: str, at: datetime | None = None) -> None:
        await self._manager.handle_node_unreachable(node_id, at=at)


def _find_node(nodes: list[NodeState], node_id: str) -> NodeState | None:
    for node in nodes:
        if node.node_id == node_id:
            return node
    return None
