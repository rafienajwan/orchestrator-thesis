from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import Any, Protocol, cast

import docker
from docker.errors import APIError

from controller.models import ServiceSpec


class DockerAdapterError(RuntimeError):
    pass


@dataclass(frozen=True)
class ContainerInfo:
    container_id: str
    container_ip: str | None = None
    published_port: int | None = None


class DockerAdapter(Protocol):
    async def deploy(self, service: ServiceSpec) -> ContainerInfo: ...

    async def stop(self, service_id: str) -> None: ...

    async def restart(self, service_id: str) -> None: ...

    async def inspect(self, service_id: str) -> ContainerInfo | None: ...


class DockerSdkAdapter(DockerAdapter):
    def __init__(
        self,
        network_name: str,
        node_id: str,
        docker_client: docker.DockerClient | None = None,
    ) -> None:
        self._client = docker_client or docker.from_env()
        self._network_name = network_name
        self._node_id = node_id

    async def deploy(self, service: ServiceSpec) -> ContainerInfo:
        return await asyncio.to_thread(self._deploy_sync, service)

    async def stop(self, service_id: str) -> None:
        await asyncio.to_thread(self._stop_sync, service_id)

    async def restart(self, service_id: str) -> None:
        await asyncio.to_thread(self._restart_sync, service_id)

    async def inspect(self, service_id: str) -> ContainerInfo | None:
        return await asyncio.to_thread(self._inspect_sync, service_id)

    def _deploy_sync(self, service: ServiceSpec) -> ContainerInfo:
        scoped_name = _safe_container_name(self._node_id, service.service_id)
        existing = self._find_container(service.service_id)
        if existing is not None:
            existing.reload()
            if existing.status == "running":
                return ContainerInfo(
                    container_id=_container_id(existing),
                    container_ip=_container_ip(existing, self._network_name),
                    published_port=_published_port(existing, service.internal_port),
                )
            try:
                existing.remove(force=True)
            except APIError as exc:
                raise DockerAdapterError(
                    f"Failed to remove stale container for service={service.service_id}"
                ) from exc

        occupied = self._find_container_by_name(scoped_name)
        if occupied is not None:
            raise DockerAdapterError(
                f"Container name collision for service={service.service_id} on node={self._node_id}; "
                "existing container is not owned by this agent"
            )

        try:
            container = self._client.containers.run(
                service.image,
                command=service.command or None,
                detach=True,
                environment=service.env or None,
                ports={f"{service.internal_port}/tcp": service.published_port},
                labels={
                    "orchestrator.service_id": service.service_id,
                    "orchestrator.node_id": self._node_id,
                    "orchestrator.managed": "true",
                },
                name=scoped_name,
                network=self._network_name,
            )
            container.reload()
            return ContainerInfo(
                container_id=_container_id(container),
                container_ip=_container_ip(container, self._network_name),
                published_port=_published_port(container, service.internal_port),
            )
        except APIError as exc:
            explanation = str(getattr(exc, "explanation", ""))
            message = str(exc)
            combined = f"{message} {explanation}".lower()
            if "port is already allocated" in combined or "address already in use" in combined:
                raise DockerAdapterError(
                    f"Published port conflict for service={service.service_id} "
                    f"on port={service.published_port}"
                ) from exc
            raise DockerAdapterError(f"Failed to deploy service={service.service_id}") from exc

    def _stop_sync(self, service_id: str) -> None:
        container = self._find_container(service_id)
        if container is None:
            return
        try:
            container.stop(timeout=10)
        except APIError as exc:
            raise DockerAdapterError(f"Failed to stop service={service_id}") from exc

    def _restart_sync(self, service_id: str) -> None:
        container = self._find_container(service_id)
        if container is None:
            raise DockerAdapterError(f"Service container not found: {service_id}")
        try:
            container.restart(timeout=10)
        except APIError as exc:
            raise DockerAdapterError(f"Failed to restart service={service_id}") from exc

    def _inspect_sync(self, service_id: str) -> ContainerInfo | None:
        container = self._find_container(service_id)
        if container is None:
            return None
        container.reload()
        return ContainerInfo(
            container_id=_container_id(container),
            container_ip=_container_ip(container, self._network_name),
            published_port=_published_port(container, 0),
        )

    def _find_container(self, service_id: str) -> Any | None:
        label_filters = [
            "orchestrator.managed=true",
            f"orchestrator.service_id={service_id}",
            f"orchestrator.node_id={self._node_id}",
        ]
        containers = self._client.containers.list(
            all=True,
            filters={"label": label_filters},
        )
        for container in containers:
            labels = cast(dict[str, str], getattr(container, "labels", {}) or {})
            if labels.get("orchestrator.managed") != "true":
                continue
            if labels.get("orchestrator.service_id") != service_id:
                continue
            if labels.get("orchestrator.node_id") != self._node_id:
                continue
            return container
        return None

    def _find_container_by_name(self, container_name: str) -> Any | None:
        containers = self._client.containers.list(all=True, filters={"name": container_name})
        if not containers:
            return None
        for container in containers:
            labels = cast(dict[str, str], getattr(container, "labels", {}) or {})
            if labels.get("orchestrator.managed") != "true":
                return container
            if labels.get("orchestrator.node_id") != self._node_id:
                return container
        return None


_safe_name_pattern = re.compile(r"[^a-zA-Z0-9_.-]+")


def _safe_segment(raw: str, fallback: str) -> str:
    safe = _safe_name_pattern.sub("-", raw).strip("-").lower()
    return safe or fallback


def _safe_container_name(node_id: str, service_id: str) -> str:
    safe_node = _safe_segment(node_id, "node")
    safe_service = _safe_segment(service_id, "service")
    return f"orchestrator-{safe_node}-{safe_service}"


def _container_ip(container: Any, network_name: str) -> str | None:
    container.reload()
    container_attrs = cast(dict[str, Any], container.attrs)
    network_settings = cast(dict[str, Any], container_attrs.get("NetworkSettings", {}))
    networks = cast(dict[str, dict[str, Any]], network_settings.get("Networks", {}))
    if network_name in networks:
        ip_address = networks[network_name].get("IPAddress")
        if ip_address:
            return str(ip_address)
    for network in networks.values():
        ip_address = network.get("IPAddress")
        if ip_address:
            return str(ip_address)
    return None


def _container_id(container: Any) -> str:
    container_id = getattr(container, "id", None)
    if not container_id:
        raise DockerAdapterError("Container ID is unavailable")
    return str(container_id)


def _published_port(container: Any, internal_port: int) -> int | None:
    container.reload()
    container_attrs = cast(dict[str, Any], container.attrs)
    network_settings = cast(dict[str, Any], container_attrs.get("NetworkSettings", {}))
    ports = cast(dict[str, list[dict[str, str]] | None], network_settings.get("Ports", {}))

    if internal_port > 0:
        key = f"{internal_port}/tcp"
        bindings = ports.get(key)
        if bindings:
            host_port = bindings[0].get("HostPort")
            if host_port and host_port.isdigit():
                return int(host_port)

    for bindings in ports.values():
        if not bindings:
            continue
        host_port = bindings[0].get("HostPort")
        if host_port and host_port.isdigit():
            return int(host_port)
    return None
