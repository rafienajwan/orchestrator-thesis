from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from controller.agent_client import AgentClient, AgentClientError
from controller.models import (
    DeploymentStatus,
    EventRecord,
    EventType,
    NodeState,
    PendingDeployment,
    Placement,
    ServiceDeploymentRequest,
    ServiceDesiredState,
    ServiceHealth,
    ServiceObservedState,
    ServiceSpec,
)
from controller.scheduler import ScheduleDecision, choose_node_least_load
from controller.state_store import StateStore


class ServiceManager:
    def __init__(
        self,
        store: StateStore,
        agent_client: AgentClient,
        scheduler: Callable[
            [ServiceSpec, list[NodeState]], ScheduleDecision
        ] = choose_node_least_load,
    ) -> None:
        self._store = store
        self._agent_client = agent_client
        self._scheduler = scheduler

    async def deploy(self, request: ServiceDeploymentRequest) -> ServiceObservedState:
        service_id = request.service.service_id
        service_lock = await self._store.acquire_service_lock(service_id)
        async with service_lock:
            return await self._deploy_unlocked(request)

    async def _deploy_unlocked(self, request: ServiceDeploymentRequest) -> ServiceObservedState:
        service_id = request.service.service_id
        now = datetime.now(UTC)

        existing = await self._store.get_service_observed(service_id)
        if existing is not None and existing.status == DeploymentStatus.running:
            return existing

        desired = ServiceDesiredState(
            service=request.service, status=DeploymentStatus.deploying, updated_at=now
        )
        await self._store.set_service_desired(desired)

        nodes = await self._store.list_nodes()
        decision = self._scheduler(request.service, nodes)
        if decision.selected_node_id is None:
            observed = ServiceObservedState(
                service_id=service_id,
                status=DeploymentStatus.placement_failed,
                health=ServiceHealth.unknown,
                node_id=None,
                container_id=None,
            )
            await self._store.set_service_observed(observed)
            await self._store.set_pending_deployment(
                PendingDeployment(
                    service_id=service_id,
                    reason=decision.reason,
                )
            )
            await self._store.append_event(
                EventType.scheduler,
                "Placement failed, deployment moved to pending",
                {"service_id": service_id, "reason": decision.reason},
            )
            return observed

        selected_node = _find_node(nodes, decision.selected_node_id)
        if selected_node is None:
            raise RuntimeError(f"Scheduler selected unknown node_id={decision.selected_node_id}")

        try:
            result = await self._agent_client.deploy(selected_node.agent_url, request.service)
        except AgentClientError as exc:
            observed = ServiceObservedState(
                service_id=service_id,
                status=DeploymentStatus.pending,
                health=ServiceHealth.unknown,
            )
            await self._store.set_service_observed(observed)
            await self._store.set_pending_deployment(
                PendingDeployment(service_id=service_id, reason=str(exc))
            )
            await self._store.append_event(
                EventType.deployment,
                "Agent deployment failed, service pending",
                {"service_id": service_id, "node_id": selected_node.node_id},
            )
            return observed

        placement = Placement(service_id=service_id, node_id=selected_node.node_id)
        observed = ServiceObservedState(
            service_id=service_id,
            status=DeploymentStatus.running,
            health=ServiceHealth.unknown,
            container_id=result.container_id,
            node_id=selected_node.node_id,
        )
        desired_running = desired.model_copy(
            update={"status": DeploymentStatus.running, "updated_at": now}
        )

        try:
            await self._store.set_placement(placement)
            await self._store.pop_pending_deployment(service_id)
            await self._store.set_service_desired(desired_running)
            await self._store.set_service_observed(observed)
            await self._store.reset_restart_counter(service_id)
        except Exception:
            try:
                await self._agent_client.stop(selected_node.agent_url, service_id)
            except AgentClientError:
                pass
            raise
        await self._store.append_event(
            EventType.deployment,
            "Service deployed",
            {
                "service_id": service_id,
                "node_id": selected_node.node_id,
                "container_id": result.container_id,
            },
        )
        return observed

    async def stop(self, service_id: str) -> ServiceObservedState:
        service_lock = await self._store.acquire_service_lock(service_id)
        async with service_lock:
            return await self._stop_unlocked(service_id)

    async def _stop_unlocked(self, service_id: str) -> ServiceObservedState:
        now = datetime.now(UTC)
        placement = await self._store.get_placement(service_id)

        if placement is not None:
            nodes = await self._store.list_nodes()
            node = _find_node(nodes, placement.node_id)
            if node is not None:
                try:
                    await self._agent_client.stop(node.agent_url, service_id)
                except AgentClientError:
                    await self._store.append_event(
                        EventType.deployment,
                        "Service stop failed on agent",
                        {"service_id": service_id, "node_id": node.node_id},
                    )

        observed = ServiceObservedState(
            service_id=service_id,
            status=DeploymentStatus.stopped,
            health=ServiceHealth.unknown,
            node_id=None,
            container_id=None,
            last_reported_at=now,
        )
        await self._store.set_service_observed(observed)
        await self._store.clear_placement(service_id)
        await self._store.pop_pending_deployment(service_id)
        await self._store.reset_restart_counter(service_id)

        desired = await self._store.get_service_desired(service_id)
        if desired is not None:
            await self._store.set_service_desired(
                desired.model_copy(update={"status": DeploymentStatus.stopped, "updated_at": now})
            )

        await self._store.append_event(
            EventType.deployment,
            "Service stopped",
            {"service_id": service_id},
        )
        return observed

    async def restart(self, service_id: str) -> ServiceObservedState:
        service_lock = await self._store.acquire_service_lock(service_id)
        async with service_lock:
            return await self._restart_unlocked(service_id)

    async def _restart_unlocked(self, service_id: str) -> ServiceObservedState:
        placement = await self._store.get_placement(service_id)
        observed = await self._store.get_service_observed(service_id)

        if placement is None:
            if observed is not None:
                return observed
            raise ValueError(f"Service placement not found for service_id={service_id}")

        nodes = await self._store.list_nodes()
        node = _find_node(nodes, placement.node_id)
        if node is None:
            raise ValueError(f"Service node not found for service_id={service_id}")

        await self._agent_client.restart(node.agent_url, service_id)
        if observed is None:
            observed = ServiceObservedState(
                service_id=service_id, status=DeploymentStatus.running, node_id=node.node_id
            )
        else:
            observed = observed.model_copy(
                update={"status": DeploymentStatus.running, "health": ServiceHealth.unknown}
            )

        await self._store.set_service_observed(observed)
        await self._store.append_event(
            EventType.deployment,
            "Service restarted",
            {"service_id": service_id, "node_id": node.node_id},
        )
        return observed

    async def get_service(self, service_id: str) -> ServiceObservedState | None:
        return await self._store.get_service_observed(service_id)

    async def list_nodes(self) -> list[NodeState]:
        return await self._store.list_nodes()

    async def list_events(self, limit: int = 100) -> list[EventRecord]:
        return await self._store.list_events(limit=limit)

    async def retry_pending_deployment(self, service_id: str) -> ServiceObservedState | None:
        service_lock = await self._store.acquire_service_lock(service_id)
        async with service_lock:
            return await self._retry_pending_deployment_unlocked(service_id)

    async def _retry_pending_deployment_unlocked(
        self, service_id: str
    ) -> ServiceObservedState | None:
        desired = await self._store.get_service_desired(service_id)
        if desired is None:
            await self._store.pop_pending_deployment(service_id)
            return None
        return await self._deploy_unlocked(ServiceDeploymentRequest(service=desired.service))


def _find_node(nodes: list[NodeState], node_id: str) -> NodeState | None:
    for node in nodes:
        if node.node_id == node_id:
            return node
    return None
