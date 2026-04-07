from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from controller.models import ResourceSnapshot, ServiceSpec


class AgentBaseModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WorkloadStatus(StrEnum):
    running = "running"
    stopped = "stopped"
    pending = "pending"
    unhealthy = "unhealthy"


class ExecuteResponse(AgentBaseModel):
    service_id: str
    container_id: str
    status: str
    node_id: str | None = None


class WorkloadRecord(AgentBaseModel):
    service: ServiceSpec
    container_id: str | None = None
    published_port: int | None = None
    # Debug-only field; ingress should not depend on container bridge IP.
    container_ip: str | None = None
    status: WorkloadStatus = WorkloadStatus.pending
    health_failures: int = Field(default=0, ge=0)
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    last_health_check_at: datetime | None = None
    last_healthy_at: datetime | None = None
    last_unhealthy_at: datetime | None = None


class AgentLocalState(AgentBaseModel):
    node_id: str
    node_address: str
    agent_url: str
    controller_url: str
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    last_heartbeat_at: datetime | None = None
    last_resource_snapshot: ResourceSnapshot | None = None
    workloads: dict[str, WorkloadRecord] = Field(default_factory=dict)
