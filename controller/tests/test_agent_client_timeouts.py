from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from controller.agent_client import AgentClientError, AgentDeployResponse, HttpAgentClient
from controller.models import ServiceSpec


class FakeResponse:
    def __init__(self, payload: dict[str, object] | None = None) -> None:
        self._payload = payload or {}

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, object]:
        return self._payload


class CapturingHttpxClient:
    """Fake httpx.AsyncClient that captures all calls with their per-request timeout."""

    def __init__(self, **kwargs: Any) -> None:
        self.constructor_kwargs = kwargs
        self.calls: list[tuple[str, str, httpx.Timeout | None]] = []  # (method, url, timeout)

    async def aclose(self) -> None:
        pass

    async def post(
        self, url: str, json: Mapping[str, object] | None = None, timeout: Any = None, **kw: Any
    ) -> FakeResponse:
        self.calls.append(("POST", url, timeout))
        if url.endswith("/execute/deploy") and json is not None:
            service_payload = cast(dict[str, object], json["service"])
            return FakeResponse(
                {
                    "service_id": service_payload["service_id"],
                    "container_id": "container-1",
                    "status": "running",
                    "node_id": "worker-1",
                }
            )
        return FakeResponse({})

    async def get(self, url: str, timeout: Any = None, **kw: Any) -> FakeResponse:
        self.calls.append(("GET", url, timeout))
        return FakeResponse(
            {
                "node_id": "worker-1",
                "node_address": "agent-1",
                "workloads": {
                    "svc-1": {
                        "service": {
                            "service_id": "svc-1",
                            "image": "example/service:latest",
                            "command": [],
                            "env": {},
                            "internal_port": 8000,
                            "health_endpoint": "/health",
                            "min_free_cpu": 0.1,
                            "min_free_memory": 0.1,
                        },
                        "container_id": "container-1",
                        "published_port": 28000,
                        "container_ip": "172.18.0.2",
                        "status": "running",
                    }
                },
            }
        )


@pytest.mark.asyncio
async def test_deploy_uses_deploy_timeout() -> None:
    fake_client = CapturingHttpxClient()
    with patch("controller.agent_client.httpx.AsyncClient", return_value=fake_client):
        client = HttpAgentClient(
            deploy_timeout_seconds=61,
            command_timeout_seconds=11,
            read_timeout_seconds=5,
        )

    response = await client.deploy(
        "http://agent-1:8080",
        ServiceSpec(service_id="svc-1", image="example/service:latest"),
    )

    assert isinstance(response, AgentDeployResponse)
    assert fake_client.calls[0][1] == "http://agent-1:8080/execute/deploy"
    timeout = fake_client.calls[0][2]
    assert isinstance(timeout, httpx.Timeout)
    assert timeout.connect == 5.0
    assert timeout.read == 61
    assert timeout.write == 61
    assert timeout.pool == 5.0

    await client.close()


@pytest.mark.asyncio
async def test_stop_and_restart_use_command_timeout() -> None:
    fake_client = CapturingHttpxClient()
    with patch("controller.agent_client.httpx.AsyncClient", return_value=fake_client):
        client = HttpAgentClient(
            deploy_timeout_seconds=61,
            command_timeout_seconds=13,
            read_timeout_seconds=5,
        )

    await client.stop("http://agent-1:8080", "svc-1")
    await client.restart("http://agent-1:8080", "svc-1")

    assert [call[1] for call in fake_client.calls] == [
        "http://agent-1:8080/execute/stop",
        "http://agent-1:8080/execute/restart",
    ]
    for _, _, timeout in fake_client.calls:
        assert isinstance(timeout, httpx.Timeout)
        assert timeout.connect == 5.0
        assert timeout.read == 13
        assert timeout.write == 13
        assert timeout.pool == 5.0

    await client.close()


@pytest.mark.asyncio
async def test_deploy_timeout_error_is_clear() -> None:
    fake_client = CapturingHttpxClient()
    # Override post to raise timeout
    async def _post_timeout(url: str, **kw: Any) -> None:
        raise httpx.ReadTimeout("timed out", request=httpx.Request("POST", url))

    fake_client.post = _post_timeout  # type: ignore[assignment]

    with patch("controller.agent_client.httpx.AsyncClient", return_value=fake_client):
        client = HttpAgentClient(
            deploy_timeout_seconds=61,
            command_timeout_seconds=13,
            read_timeout_seconds=5,
        )

    with pytest.raises(AgentClientError) as exc_info:
        await client.deploy(
            "http://agent-1:8080",
            ServiceSpec(service_id="svc-timeout", image="example/service:latest"),
        )

    assert "timed out after 61" in str(exc_info.value)
    assert "service=svc-timeout" in str(exc_info.value)

    await client.close()


@pytest.mark.asyncio
async def test_local_state_uses_read_timeout() -> None:
    fake_client = CapturingHttpxClient()
    with patch("controller.agent_client.httpx.AsyncClient", return_value=fake_client):
        client = HttpAgentClient(
            deploy_timeout_seconds=61,
            command_timeout_seconds=13,
            read_timeout_seconds=7,
        )

    local_state = await client.get_local_state("http://agent-1:8080")

    assert local_state.node_id == "worker-1"
    assert fake_client.calls[0][1] == "http://agent-1:8080/local-state"
    timeout = fake_client.calls[0][2]
    assert isinstance(timeout, httpx.Timeout)
    assert timeout.read == 7
    assert timeout.write == 7

    await client.close()
