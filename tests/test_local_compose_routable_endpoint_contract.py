from __future__ import annotations

from pathlib import Path


def _read(path: str) -> str:
    root = Path(__file__).resolve().parents[1]
    return (root / path).read_text(encoding="utf-8")


def test_compose_uses_host_gateway_for_local_advertised_host() -> None:
    compose = _read("docker-compose.yml")

    assert "ADVERTISED_HOST: ${LOCAL_ADVERTISED_HOST:-host.docker.internal}" in compose
    assert "ADVERTISED_HOST: agent-1" not in compose
    assert "ADVERTISED_HOST: agent-2" not in compose
    assert "host.docker.internal:host-gateway" in compose


def test_compose_uses_non_overlapping_agent_port_ranges() -> None:
    compose = _read("docker-compose.yml")

    assert "PUBLISHED_PORT_BASE: ${AGENT1_PUBLISHED_PORT_BASE:-28000}" in compose
    assert "PUBLISHED_PORT_MAX: ${AGENT1_PUBLISHED_PORT_MAX:-28499}" in compose
    assert "PUBLISHED_PORT_BASE: ${AGENT2_PUBLISHED_PORT_BASE:-28500}" in compose
    assert "PUBLISHED_PORT_MAX: ${AGENT2_PUBLISHED_PORT_MAX:-28999}" in compose


def test_docs_do_not_claim_agent_service_name_as_workload_target() -> None:
    readme = _read("README.md")
    smoke = _read("docs/runtime-smoke-test.md")

    assert "node_address` should be `agent-1`/`agent-2` in local compose" not in smoke
    assert "ADVERTISED_HOST` defaults to service names (`agent-1`, `agent-2`)" not in readme
    assert "host.docker.internal" in readme
    assert "single-host simulation" in smoke
