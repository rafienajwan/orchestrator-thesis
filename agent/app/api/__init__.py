from __future__ import annotations

from typing import cast

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict

from agent.app.core.models import AgentLocalState, ExecuteResponse
from agent.app.services.workload_manager import AgentWorkloadManager
from controller.models import ServiceSpec


class LifecycleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    service_id: str


class DeployRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    service: ServiceSpec


class HealthResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str
    node_id: str


def _get_workload_manager(request: Request) -> AgentWorkloadManager:
    manager = getattr(request.app.state, "workload_manager", None)
    if manager is None:
        raise RuntimeError("Workload manager is not initialized")
    return cast(AgentWorkloadManager, manager)


def build_router() -> APIRouter:
    router = APIRouter()

    @router.post("/execute/deploy", response_model=ExecuteResponse)
    async def deploy_service(
        request_payload: DeployRequest,
        manager: AgentWorkloadManager = Depends(_get_workload_manager),
    ) -> ExecuteResponse:
        return await manager.deploy(request_payload.service)

    @router.post("/execute/stop", response_model=ExecuteResponse)
    async def stop_service(
        request_payload: LifecycleRequest,
        manager: AgentWorkloadManager = Depends(_get_workload_manager),
    ) -> ExecuteResponse:
        return await manager.stop(request_payload.service_id)

    @router.post("/execute/restart", response_model=ExecuteResponse)
    async def restart_service(
        request_payload: LifecycleRequest,
        manager: AgentWorkloadManager = Depends(_get_workload_manager),
    ) -> ExecuteResponse:
        try:
            return await manager.restart(request_payload.service_id)
        except KeyError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="service not found"
            ) from exc

    @router.get("/health", response_model=HealthResponse)
    async def health(request: Request) -> HealthResponse:
        settings = getattr(request.app.state, "settings", None)
        if settings is None:
            raise RuntimeError("Agent settings are not initialized")
        return HealthResponse(status="ok", node_id=settings.node_id)

    @router.get("/local-state", response_model=AgentLocalState)
    async def local_state(
        manager: AgentWorkloadManager = Depends(_get_workload_manager),
    ) -> AgentLocalState:
        return await manager.get_local_state()

    return router
