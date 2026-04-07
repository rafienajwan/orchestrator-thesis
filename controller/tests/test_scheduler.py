from __future__ import annotations

from datetime import UTC, datetime

from controller.models import NodeState, NodeStatus, ResourceSnapshot, ServiceSpec
from controller.scheduler import choose_node_least_load


def _node(
    node_id: str, cpu: float, memory: float, status: NodeStatus = NodeStatus.healthy
) -> NodeState:
    return NodeState(
        node_id=node_id,
        agent_url=f"http://{node_id}:8080",
        status=status,
        last_heartbeat_at=datetime.now(UTC),
        last_resource_snapshot=ResourceSnapshot(cpu_utilization=cpu, memory_utilization=memory),
    )


def _service() -> ServiceSpec:
    return ServiceSpec(
        service_id="svc-1",
        image="example/service:latest",
        min_free_cpu=0.1,
        min_free_memory=0.1,
    )


def test_choose_node_least_load_uses_max_cpu_memory() -> None:
    service = _service()
    nodes = [
        _node("node-a", cpu=0.6, memory=0.2),
        _node("node-b", cpu=0.4, memory=0.4),
    ]

    decision = choose_node_least_load(service, nodes)

    assert decision.selected_node_id == "node-b"
    assert decision.status == "scheduled"


def test_tie_breaker_prefers_lower_memory_utilization() -> None:
    service = _service()
    nodes = [
        _node("node-a", cpu=0.5, memory=0.4),
        _node("node-b", cpu=0.5, memory=0.3),
    ]

    decision = choose_node_least_load(service, nodes)

    assert decision.selected_node_id == "node-b"


def test_tie_breaker_prefers_stable_node_id_order() -> None:
    service = _service()
    nodes = [
        _node("node-b", cpu=0.2, memory=0.2),
        _node("node-a", cpu=0.2, memory=0.2),
    ]

    decision = choose_node_least_load(service, nodes)

    assert decision.selected_node_id == "node-a"


def test_no_eligible_node_results_placement_failed() -> None:
    service = _service()
    nodes = [
        _node("node-a", cpu=0.95, memory=0.95),
        _node("node-b", cpu=0.1, memory=0.1, status=NodeStatus.unavailable),
    ]

    decision = choose_node_least_load(service, nodes)

    assert decision.selected_node_id is None
    assert decision.status == "placement_failed"
