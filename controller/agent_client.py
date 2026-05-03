from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

import httpx
from pydantic import BaseModel, ConfigDict

from controller.models import ServiceSpec


class AgentClientError(RuntimeError):
    pass


class AgentDeployResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    service_id: str
    container_id: str
    status: str
    node_id: str | None = None


class AgentWorkloadState(BaseModel):
    model_config = ConfigDict(extra="ignore")

    service: ServiceSpec
    container_id: str | None = None
    published_port: int | None = None
    # Debug-only field; ingress should use node_address:published_port.
    container_ip: str | None = None
    status: str


class AgentLocalStateResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    node_id: str
    node_address: str
    workloads: dict[str, AgentWorkloadState]


class AgentClient(Protocol):
    async def deploy(self, agent_url: str, service: ServiceSpec) -> AgentDeployResponse: ...

    async def stop(self, agent_url: str, service_id: str) -> None: ...

    async def restart(self, agent_url: str, service_id: str) -> None: ...


class HttpAgentClient(AgentClient):
    def __init__(
        self,
        deploy_timeout_seconds: float = 60.0,
        command_timeout_seconds: float = 15.0,
        read_timeout_seconds: float = 5.0,
    ) -> None:
        self._deploy_timeout = self._build_timeout(deploy_timeout_seconds)
        self._command_timeout = self._build_timeout(command_timeout_seconds)
        self._read_timeout = self._build_timeout(read_timeout_seconds)

    @staticmethod
    def _build_timeout(operation_timeout_seconds: float) -> httpx.Timeout:
        return httpx.Timeout(
            connect=5.0,
            read=operation_timeout_seconds,
            write=operation_timeout_seconds,
            pool=5.0,
        )

    async def deploy(self, agent_url: str, service: ServiceSpec) -> AgentDeployResponse:
        url = f"{agent_url.rstrip('/')}/execute/deploy"
        payload = {"service": service.model_dump(mode="json")}

        try:
            response = await self._post_json(
                url,
                payload,
                request_timeout=self._deploy_timeout,
                operation_name="deploy",
                service_id=service.service_id,
            )
        except httpx.HTTPError as exc:
            raise AgentClientError(
                f"Agent deploy request failed for service={service.service_id}"
            ) from exc

        try:
            return AgentDeployResponse.model_validate(response.json())
        except Exception as exc:
            raise AgentClientError("Agent deploy response is invalid") from exc

    async def stop(self, agent_url: str, service_id: str) -> None:
        url = f"{agent_url.rstrip('/')}/execute/stop"
        payload = {"service_id": service_id}

        try:
            await self._post_json(
                url,
                payload,
                request_timeout=self._command_timeout,
                operation_name="stop",
                service_id=service_id,
            )
        except httpx.HTTPError as exc:
            raise AgentClientError(f"Agent stop request failed for service={service_id}") from exc

    async def restart(self, agent_url: str, service_id: str) -> None:
        url = f"{agent_url.rstrip('/')}/execute/restart"
        payload = {"service_id": service_id}

        try:
            await self._post_json(
                url,
                payload,
                request_timeout=self._command_timeout,
                operation_name="restart",
                service_id=service_id,
            )
        except httpx.HTTPError as exc:
            raise AgentClientError(
                f"Agent restart request failed for service={service_id}"
            ) from exc

    async def get_local_state(self, agent_url: str) -> AgentLocalStateResponse:
        url = f"{agent_url.rstrip('/')}/local-state"
        try:
            response = await self._get_json(url, request_timeout=self._read_timeout)
        except httpx.HTTPError as exc:
            raise AgentClientError("Agent local-state read failed") from exc

        try:
            return AgentLocalStateResponse.model_validate(response.json())
        except Exception as exc:
            raise AgentClientError("Agent local-state response is invalid") from exc

    async def _post_json(
        self,
        url: str,
        payload: Mapping[str, object],
        request_timeout: httpx.Timeout,
        operation_name: str,
        service_id: str,
    ) -> httpx.Response:
        try:
            async with httpx.AsyncClient(timeout=request_timeout) as client:
                response = await client.post(url, json=payload)
                response.raise_for_status()
                return response
        except httpx.TimeoutException as exc:
            timeout_seconds = request_timeout.read
            raise AgentClientError(
                f"Agent {operation_name} timed out after {timeout_seconds} seconds for service={service_id}"
            ) from exc

    async def _get_json(
        self,
        url: str,
        request_timeout: httpx.Timeout,
    ) -> httpx.Response:
        async with httpx.AsyncClient(timeout=request_timeout) as client:
            response = await client.get(url)
            response.raise_for_status()
            return response
