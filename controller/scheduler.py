from __future__ import annotations

from dataclasses import dataclass

from controller.models import NodeState, NodeStatus, ServiceSpec


@dataclass(frozen=True)
class ScheduleDecision:
    service_id: str
    selected_node_id: str | None
    status: str
    reason: str


@dataclass(frozen=True)
class _Candidate:
    node: NodeState
    load: float
    memory_utilization: float


def _node_satisfies_minimum_resource(spec: ServiceSpec, node: NodeState) -> bool:
    snapshot = node.last_resource_snapshot
    if snapshot is None:
        return False

    free_cpu = 1.0 - snapshot.cpu_utilization
    free_memory = 1.0 - snapshot.memory_utilization
    return free_cpu >= spec.min_free_cpu and free_memory >= spec.min_free_memory


def _build_candidate(node: NodeState) -> _Candidate | None:
    snapshot = node.last_resource_snapshot
    if snapshot is None:
        return None

    load = max(snapshot.cpu_utilization, snapshot.memory_utilization)
    return _Candidate(
        node=node,
        load=load,
        memory_utilization=snapshot.memory_utilization,
    )


def choose_node_least_load(service: ServiceSpec, nodes: list[NodeState]) -> ScheduleDecision:
    eligible_candidates: list[_Candidate] = []

    for node in nodes:
        if node.status != NodeStatus.healthy:
            continue
        if not _node_satisfies_minimum_resource(service, node):
            continue

        candidate = _build_candidate(node)
        if candidate is None:
            continue
        eligible_candidates.append(candidate)

    if not eligible_candidates:
        return ScheduleDecision(
            service_id=service.service_id,
            selected_node_id=None,
            status="placement_failed",
            reason="No healthy node satisfies minimum resource requirement",
        )

    # Tie-break: load asc, memory utilization asc, node_id asc.
    selected = min(
        eligible_candidates,
        key=lambda candidate: (
            candidate.load,
            candidate.memory_utilization,
            candidate.node.node_id,
        ),
    )

    return ScheduleDecision(
        service_id=service.service_id,
        selected_node_id=selected.node.node_id,
        status="scheduled",
        reason="Least-load selection succeeded",
    )
