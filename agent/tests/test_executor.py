from __future__ import annotations

import builtins
from typing import Any, cast

import pytest
from docker.errors import APIError

from agent.app.adapters.docker_adapter import ContainerInfo, DockerAdapterError, DockerSdkAdapter
from controller.models import ServiceSpec


class FakeContainer:
    def __init__(
        self,
        container_id: str,
        labels: dict[str, str],
        ip_address: str = "172.18.0.2",
        name: str | None = None,
    ) -> None:
        self.id = container_id
        self.labels = labels
        self.name = name or container_id
        self._ip_address = ip_address
        self.status = "running"
        self.removed = False
        self.stopped = False
        self.restarted = False

    def reload(self) -> None:
        return None

    def remove(self, force: bool = False) -> None:
        self.removed = True

    def stop(self, timeout: int = 10) -> None:
        self.stopped = True

    def restart(self, timeout: int = 10) -> None:
        self.restarted = True

    @property
    def attrs(self) -> dict[str, object]:
        return {
            "NetworkSettings": {
                "Networks": {
                    "orchestrator-thesis-net": {"IPAddress": self._ip_address},
                }
            }
        }


class FakeContainersManager:
    def __init__(self) -> None:
        self.created: list[FakeContainer] = []
        self.run_calls: list[dict[str, object]] = []
        self.run_exception: Exception | None = None

    def run(self, image: str, **kwargs: object) -> FakeContainer:
        if self.run_exception is not None:
            raise self.run_exception
        self.run_calls.append({"image": image, **kwargs})
        labels = kwargs.get("labels", {})
        if not isinstance(labels, dict):
            raise TypeError("labels must be a dict")
        container = FakeContainer(
            container_id=f"container-{len(self.created) + 1}",
            labels={str(k): str(v) for k, v in labels.items()},
            name=str(kwargs.get("name", f"container-{len(self.created) + 1}")),
        )
        self.created.append(container)
        return container

    def list(
        self, all: bool = True, filters: dict[str, object] | None = None
    ) -> list[FakeContainer]:
        if filters is None:
            return list(self.created)
        label_filters = filters.get("label")
        name_filter = filters.get("name")
        if name_filter is not None:
            needle = str(name_filter)
            return [container for container in self.created if container.name == needle]
        if label_filters is None:
            return list(self.created)
        if isinstance(label_filters, str):
            checks = [label_filters]
        elif isinstance(label_filters, list):
            checks = [str(value) for value in label_filters]
        else:
            raise TypeError("label filter must be str or list")

        filtered: list[FakeContainer] = []
        for container in self.created:
            if builtins.all(_matches_label_filter(container, check) for check in checks):
                filtered.append(container)
        return filtered


class FakeDockerClient:
    def __init__(self) -> None:
        self.containers = FakeContainersManager()


@pytest.mark.asyncio
async def test_deploy_is_idempotent_for_running_container() -> None:
    client = FakeDockerClient()
    adapter = DockerSdkAdapter(
        network_name="orchestrator-thesis-net",
        node_id="worker-a",
        docker_client=cast(Any, client),
    )
    service = ServiceSpec(service_id="svc-1", image="sample-app:latest", internal_port=8000)

    first = await adapter.deploy(service)
    second = await adapter.deploy(service)

    assert isinstance(first, ContainerInfo)
    assert first.container_id == second.container_id
    assert len(client.containers.run_calls) == 1


@pytest.mark.asyncio
async def test_stop_missing_container_is_graceful() -> None:
    client = FakeDockerClient()
    adapter = DockerSdkAdapter(
        network_name="orchestrator-thesis-net",
        node_id="worker-a",
        docker_client=cast(Any, client),
    )

    await adapter.stop("missing-service")

    assert client.containers.created == []


@pytest.mark.asyncio
async def test_restart_missing_container_raises_clear_error() -> None:
    client = FakeDockerClient()
    adapter = DockerSdkAdapter(
        network_name="orchestrator-thesis-net",
        node_id="worker-a",
        docker_client=cast(Any, client),
    )

    with pytest.raises(DockerAdapterError, match="Service container not found: missing-service"):
        await adapter.restart("missing-service")


@pytest.mark.asyncio
async def test_deploy_ignores_same_service_owned_by_other_node() -> None:
    client = FakeDockerClient()
    client.containers.created.append(
        FakeContainer(
            container_id="foreign-1",
            labels={
                "orchestrator.service_id": "svc-1",
                "orchestrator.node_id": "worker-b",
                "orchestrator.managed": "true",
            },
            ip_address="172.18.0.50",
        )
    )
    adapter = DockerSdkAdapter(
        network_name="orchestrator-thesis-net",
        node_id="worker-a",
        docker_client=cast(Any, client),
    )
    service = ServiceSpec(service_id="svc-1", image="sample-app:latest", internal_port=8000)

    result = await adapter.deploy(service)

    assert result.container_id != "foreign-1"
    assert len(client.containers.run_calls) == 1
    labels = client.containers.run_calls[0]["labels"]
    assert labels == {
        "orchestrator.service_id": "svc-1",
        "orchestrator.node_id": "worker-a",
        "orchestrator.managed": "true",
    }


@pytest.mark.asyncio
async def test_stop_and_restart_do_not_touch_other_node_container() -> None:
    client = FakeDockerClient()
    foreign = FakeContainer(
        container_id="foreign-2",
        labels={
            "orchestrator.service_id": "svc-x",
            "orchestrator.node_id": "worker-b",
            "orchestrator.managed": "true",
        },
    )
    client.containers.created.append(foreign)
    adapter = DockerSdkAdapter(
        network_name="orchestrator-thesis-net",
        node_id="worker-a",
        docker_client=cast(Any, client),
    )

    await adapter.stop("svc-x")
    assert foreign.stopped is False

    with pytest.raises(DockerAdapterError, match="Service container not found: svc-x"):
        await adapter.restart("svc-x")
    assert foreign.restarted is False


@pytest.mark.asyncio
async def test_container_name_is_unique_per_node() -> None:
    shared_client = FakeDockerClient()
    adapter_a = DockerSdkAdapter(
        network_name="orchestrator-thesis-net",
        node_id="worker-a",
        docker_client=cast(Any, shared_client),
    )
    adapter_b = DockerSdkAdapter(
        network_name="orchestrator-thesis-net",
        node_id="worker-b",
        docker_client=cast(Any, shared_client),
    )
    service = ServiceSpec(service_id="sample-app", image="sample-app:latest", internal_port=8000)

    await adapter_a.deploy(service)
    await adapter_b.deploy(service)

    assert shared_client.containers.run_calls[0]["name"] == "orchestrator-worker-a-sample-app"
    assert shared_client.containers.run_calls[1]["name"] == "orchestrator-worker-b-sample-app"


@pytest.mark.asyncio
async def test_restart_same_service_does_not_touch_other_worker_container() -> None:
    shared = FakeDockerClient()
    worker_a_container = FakeContainer(
        container_id="a-1",
        name="orchestrator-worker-a-svc-shared",
        labels={
            "orchestrator.service_id": "svc-shared",
            "orchestrator.node_id": "worker-a",
            "orchestrator.managed": "true",
        },
    )
    worker_b_container = FakeContainer(
        container_id="b-1",
        name="orchestrator-worker-b-svc-shared",
        labels={
            "orchestrator.service_id": "svc-shared",
            "orchestrator.node_id": "worker-b",
            "orchestrator.managed": "true",
        },
    )
    shared.containers.created.extend([worker_a_container, worker_b_container])

    adapter_a = DockerSdkAdapter(
        network_name="orchestrator-thesis-net",
        node_id="worker-a",
        docker_client=cast(Any, shared),
    )

    await adapter_a.restart("svc-shared")

    assert worker_a_container.restarted is True
    assert worker_b_container.restarted is False


@pytest.mark.asyncio
async def test_stop_same_service_on_worker_two_does_not_affect_worker_one() -> None:
    shared = FakeDockerClient()
    worker_a_container = FakeContainer(
        container_id="a-2",
        name="orchestrator-worker-a-svc-shared",
        labels={
            "orchestrator.service_id": "svc-shared",
            "orchestrator.node_id": "worker-a",
            "orchestrator.managed": "true",
        },
    )
    worker_b_container = FakeContainer(
        container_id="b-2",
        name="orchestrator-worker-b-svc-shared",
        labels={
            "orchestrator.service_id": "svc-shared",
            "orchestrator.node_id": "worker-b",
            "orchestrator.managed": "true",
        },
    )
    shared.containers.created.extend([worker_a_container, worker_b_container])

    adapter_b = DockerSdkAdapter(
        network_name="orchestrator-thesis-net",
        node_id="worker-b",
        docker_client=cast(Any, shared),
    )

    await adapter_b.stop("svc-shared")

    assert worker_b_container.stopped is True
    assert worker_a_container.stopped is False


@pytest.mark.asyncio
async def test_helper_ignores_containers_without_managed_true_and_fails_safe_on_name_collision() -> (
    None
):
    client = FakeDockerClient()
    client.containers.created.append(
        FakeContainer(
            container_id="unmanaged-1",
            name="orchestrator-worker-a-svc-unmanaged",
            labels={
                "orchestrator.service_id": "svc-unmanaged",
                "orchestrator.node_id": "worker-a",
            },
        )
    )
    adapter = DockerSdkAdapter(
        network_name="orchestrator-thesis-net",
        node_id="worker-a",
        docker_client=cast(Any, client),
    )

    with pytest.raises(DockerAdapterError, match="Container name collision"):
        await adapter.deploy(
            ServiceSpec(service_id="svc-unmanaged", image="sample-app:latest", internal_port=8000)
        )

    assert len(client.containers.run_calls) == 0


@pytest.mark.asyncio
async def test_container_name_is_sanitized_for_weird_characters() -> None:
    client = FakeDockerClient()
    adapter = DockerSdkAdapter(
        network_name="orchestrator-thesis-net",
        node_id="Worker @ 1",
        docker_client=cast(Any, client),
    )

    await adapter.deploy(
        ServiceSpec(service_id="My Service#$!", image="sample-app:latest", internal_port=8000)
    )

    assert client.containers.run_calls[0]["name"] == "orchestrator-worker-1-my-service"


@pytest.mark.asyncio
async def test_legacy_container_without_node_id_is_ignored_safely() -> None:
    client = FakeDockerClient()
    client.containers.created.append(
        FakeContainer(
            container_id="legacy-1",
            name="orchestrator-svc-legacy",
            labels={
                "orchestrator.service_id": "svc-legacy",
                "orchestrator.managed": "true",
            },
        )
    )
    adapter = DockerSdkAdapter(
        network_name="orchestrator-thesis-net",
        node_id="worker-a",
        docker_client=cast(Any, client),
    )

    result = await adapter.deploy(
        ServiceSpec(service_id="svc-legacy", image="sample-app:latest", internal_port=8000)
    )

    assert result.container_id != "legacy-1"
    assert len(client.containers.run_calls) == 1


@pytest.mark.asyncio
async def test_legacy_collision_on_scoped_name_fails_without_touching_existing() -> None:
    client = FakeDockerClient()
    foreign = FakeContainer(
        container_id="legacy-collision",
        name="orchestrator-worker-a-svc-collision",
        labels={
            "orchestrator.service_id": "svc-collision",
            "orchestrator.managed": "true",
        },
    )
    client.containers.created.append(foreign)
    adapter = DockerSdkAdapter(
        network_name="orchestrator-thesis-net",
        node_id="worker-a",
        docker_client=cast(Any, client),
    )

    with pytest.raises(DockerAdapterError, match="Container name collision"):
        await adapter.deploy(
            ServiceSpec(service_id="svc-collision", image="sample-app:latest", internal_port=8000)
        )

    assert foreign.stopped is False
    assert foreign.removed is False


@pytest.mark.asyncio
async def test_deploy_port_collision_returns_clear_error() -> None:
    client = FakeDockerClient()
    client.containers.run_exception = APIError(
        message="run failed",
        explanation='driver failed programming external connectivity: Bind for 0.0.0.0:28000 failed: port is already allocated',
    )
    adapter = DockerSdkAdapter(
        network_name="orchestrator-thesis-net",
        node_id="worker-a",
        docker_client=cast(Any, client),
    )

    with pytest.raises(DockerAdapterError, match="Published port conflict"):
        await adapter.deploy(
            ServiceSpec(
                service_id="svc-port-conflict",
                image="sample-app:latest",
                internal_port=8000,
                published_port=28000,
            )
        )


def _matches_label_filter(container: FakeContainer, label_filter: str) -> bool:
    key, value = label_filter.split("=", 1)
    return container.labels.get(key) == value
