from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


def utc_now() -> datetime:
    return datetime.now(UTC)


class OrchestratorBaseModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NodeStatus(StrEnum):
    healthy = "healthy"
    unavailable = "unavailable"


class ServiceHealth(StrEnum):
    healthy = "healthy"
    unhealthy = "unhealthy"
    unknown = "unknown"


class DeploymentStatus(StrEnum):
    deploying = "deploying"
    running = "running"
    stopped = "stopped"
    pending = "pending"
    placement_failed = "placement_failed"
    failed = "failed"


class EventType(StrEnum):
    deployment = "deployment"
    scheduler = "scheduler"
    self_healing = "self_healing"
    node = "node"
    health = "health"
    ingress = "ingress"


class ResourceSnapshot(OrchestratorBaseModel):
    cpu_utilization: float = Field(ge=0.0, le=1.0)
    memory_utilization: float = Field(ge=0.0, le=1.0)
    captured_at: datetime = Field(default_factory=utc_now)


class NodeState(OrchestratorBaseModel):
    node_id: str = Field(min_length=1)
    agent_url: str = Field(min_length=1)
    status: NodeStatus = NodeStatus.healthy
    last_heartbeat_at: datetime = Field(default_factory=utc_now)
    last_resource_snapshot: ResourceSnapshot | None = None

    @property
    def has_snapshot(self) -> bool:
        return self.last_resource_snapshot is not None


class ServiceSpec(OrchestratorBaseModel):
    service_id: str = Field(min_length=1)
    image: str = Field(min_length=1)
    command: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    internal_port: int = Field(default=8000, ge=1, le=65535)
    health_endpoint: str = "/health"
    min_free_cpu: float = Field(default=0.10, ge=0.0, le=1.0)
    min_free_memory: float = Field(default=0.10, ge=0.0, le=1.0)

    @field_validator("health_endpoint")
    @classmethod
    def ensure_health_endpoint_format(cls, value: str) -> str:
        if not value.startswith("/"):
            raise ValueError("health_endpoint must start with '/'")
        return value


class ServiceDeploymentRequest(OrchestratorBaseModel):
    service: ServiceSpec


class ServiceDesiredState(OrchestratorBaseModel):
    service: ServiceSpec
    status: DeploymentStatus = DeploymentStatus.deploying
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class ServiceObservedState(OrchestratorBaseModel):
    service_id: str
    status: DeploymentStatus
    health: ServiceHealth = ServiceHealth.unknown
    container_id: str | None = None
    node_id: str | None = None
    last_reported_at: datetime = Field(default_factory=utc_now)


class Placement(OrchestratorBaseModel):
    service_id: str
    node_id: str
    placed_at: datetime = Field(default_factory=utc_now)


class PendingDeployment(OrchestratorBaseModel):
    service_id: str
    reason: str
    created_at: datetime = Field(default_factory=utc_now)


class RecoveryRecord(OrchestratorBaseModel):
    service_id: str
    action: str
    reason: str
    from_node_id: str | None = None
    to_node_id: str | None = None
    occurred_at: datetime = Field(default_factory=utc_now)


class RestartCounter(OrchestratorBaseModel):
    service_id: str
    count: int = Field(default=0, ge=0)
    updated_at: datetime = Field(default_factory=utc_now)


class EventRecord(OrchestratorBaseModel):
    event_id: str
    event_type: EventType
    message: str
    details: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)


class AgentHeartbeatReport(OrchestratorBaseModel):
    node_id: str
    agent_url: str
    sent_at: datetime = Field(default_factory=utc_now)


class AgentResourceReport(OrchestratorBaseModel):
    node_id: str
    snapshot: ResourceSnapshot


class AgentHealthReport(OrchestratorBaseModel):
    node_id: str
    service_id: str
    healthy: bool
    consecutive_failures: int = Field(default=0, ge=0)
    observed_at: datetime = Field(default_factory=utc_now)
