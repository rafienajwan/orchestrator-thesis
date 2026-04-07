from __future__ import annotations

from typing import cast

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict

from controller.models import (
    AgentHealthReport,
    AgentHeartbeatReport,
    AgentResourceReport,
    EventRecord,
    NodeState,
    ServiceDeploymentRequest,
    ServiceObservedState,
)
from controller.self_healing import SelfHealingManager
from controller.service_manager import ServiceManager
from controller.state_store import StateStore


class AckResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str


class ControllerInfoResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    service: str
    status: str
    version: str


def _get_service_manager(request: Request) -> ServiceManager:
    manager = getattr(request.app.state, "service_manager", None)
    if manager is None:
        raise RuntimeError("Service manager is not initialized")
    return cast(ServiceManager, manager)


def _get_store(request: Request) -> StateStore:
    store = getattr(request.app.state, "state_store", None)
    if store is None:
        raise RuntimeError("State store is not initialized")
    return cast(StateStore, store)


def _get_self_healing(request: Request) -> SelfHealingManager:
    manager = getattr(request.app.state, "self_healing_manager", None)
    if manager is None:
        raise RuntimeError("Self-healing manager is not initialized")
    return cast(SelfHealingManager, manager)


def build_router() -> APIRouter:
    router = APIRouter()

    @router.get("/", response_model=ControllerInfoResponse)
    async def root() -> ControllerInfoResponse:
        return ControllerInfoResponse(
            service="mini-orchestrator-controller",
            status="ok",
            version="0.1.0",
        )

    @router.get("/health", response_model=AckResponse)
    async def health() -> AckResponse:
        return AckResponse(status="ok")

    @router.post("/services/deploy", response_model=ServiceObservedState)
    async def deploy_service(
        request_payload: ServiceDeploymentRequest,
        manager: ServiceManager = Depends(_get_service_manager),
    ) -> ServiceObservedState:
        return await manager.deploy(request_payload)

    @router.post("/services/{service_id}/stop", response_model=ServiceObservedState)
    async def stop_service(
        service_id: str,
        manager: ServiceManager = Depends(_get_service_manager),
    ) -> ServiceObservedState:
        return await manager.stop(service_id)

    @router.post("/services/{service_id}/restart", response_model=ServiceObservedState)
    async def restart_service(
        service_id: str,
        manager: ServiceManager = Depends(_get_service_manager),
    ) -> ServiceObservedState:
        try:
            return await manager.restart(service_id)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    @router.get("/services/{service_id}", response_model=ServiceObservedState)
    async def get_service(
        service_id: str,
        manager: ServiceManager = Depends(_get_service_manager),
    ) -> ServiceObservedState:
        observed = await manager.get_service(service_id)
        if observed is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="service not found")
        return observed

    @router.get("/nodes", response_model=list[NodeState])
    async def list_nodes(
        manager: ServiceManager = Depends(_get_service_manager),
    ) -> list[NodeState]:
        return await manager.list_nodes()

    @router.get("/events", response_model=list[EventRecord])
    async def list_events(
        limit: int = Query(default=100, ge=1, le=1000),
        manager: ServiceManager = Depends(_get_service_manager),
    ) -> list[EventRecord]:
        return await manager.list_events(limit=limit)

    @router.post("/internal/agent/heartbeat", response_model=AckResponse)
    async def ingest_heartbeat(
        report: AgentHeartbeatReport,
        store: StateStore = Depends(_get_store),
    ) -> AckResponse:
        await store.upsert_node_heartbeat(
            report.node_id,
            report.agent_url,
            node_address=report.node_address,
            at=report.sent_at,
        )
        return AckResponse(status="ok")

    @router.post("/internal/agent/resource-snapshot", response_model=AckResponse)
    async def ingest_snapshot(
        report: AgentResourceReport,
        store: StateStore = Depends(_get_store),
    ) -> AckResponse:
        await store.upsert_node_snapshot(report.node_id, report.snapshot)
        return AckResponse(status="ok")

    @router.post("/internal/agent/health-report", response_model=AckResponse)
    async def ingest_health(
        report: AgentHealthReport,
        self_healing: SelfHealingManager = Depends(_get_self_healing),
    ) -> AckResponse:
        await self_healing.handle_health_report(report)
        return AckResponse(status="ok")

    return router
