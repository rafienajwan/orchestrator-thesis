from __future__ import annotations

from collections.abc import Mapping
from typing import cast

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


class CapturingAsyncClient:
    def __init__(self, timeout: httpx.Timeout, calls: list[tuple[str, httpx.Timeout]]) -> None:
        self._timeout = timeout
        self._calls = calls

    async def __aenter__(self) -> CapturingAsyncClient:
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    async def post(self, url: str, json: Mapping[str, object]) -> FakeResponse:
        self._calls.append((url, self._timeout))
        if url.endswith("/execute/deploy"):
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


@pytest.mark.asyncio
async def test_deploy_uses_deploy_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, httpx.Timeout]] = []
    monkeypatch.setattr(
        "controller.agent_client.httpx.AsyncClient",
        lambda timeout: CapturingAsyncClient(timeout=timeout, calls=calls),
    )

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
    assert calls[0][0] == "http://agent-1:8080/execute/deploy"
    timeout = calls[0][1]
    assert timeout.connect == 5.0
    assert timeout.read == 61
    assert timeout.write == 61
    assert timeout.pool == 5.0


@pytest.mark.asyncio
async def test_stop_and_restart_use_command_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, httpx.Timeout]] = []
    monkeypatch.setattr(
        "controller.agent_client.httpx.AsyncClient",
        lambda timeout: CapturingAsyncClient(timeout=timeout, calls=calls),
    )

    client = HttpAgentClient(
        deploy_timeout_seconds=61,
        command_timeout_seconds=13,
        read_timeout_seconds=5,
    )

    await client.stop("http://agent-1:8080", "svc-1")
    await client.restart("http://agent-1:8080", "svc-1")

    assert [call[0] for call in calls] == [
        "http://agent-1:8080/execute/stop",
        "http://agent-1:8080/execute/restart",
    ]
    for _, timeout in calls:
        assert timeout.connect == 5.0
        assert timeout.read == 13
        assert timeout.write == 13
        assert timeout.pool == 5.0


@pytest.mark.asyncio
async def test_deploy_timeout_error_is_clear(monkeypatch: pytest.MonkeyPatch) -> None:
    class TimeoutAsyncClient:
        def __init__(self, timeout: httpx.Timeout) -> None:
            self._timeout = timeout

        async def __aenter__(self) -> TimeoutAsyncClient:
            return self

        async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
            return None

        async def post(self, url: str, json: Mapping[str, object]) -> FakeResponse:
            raise httpx.ReadTimeout("timed out", request=httpx.Request("POST", url))

    monkeypatch.setattr(
        "controller.agent_client.httpx.AsyncClient",
        lambda timeout: TimeoutAsyncClient(timeout=timeout),
    )

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
