from __future__ import annotations

import asyncio
import json
import uuid
from collections import deque
from datetime import UTC, datetime
from typing import Any, Protocol

from redis.asyncio import Redis
from redis.exceptions import RedisError

from controller.models import (
    EventRecord,
    EventType,
    NodeState,
    NodeStatus,
    PendingDeployment,
    Placement,
    RecoveryRecord,
    ResourceSnapshot,
    RestartCounter,
    ServiceDesiredState,
    ServiceObservedState,
)


class StateStoreError(RuntimeError):
    pass


class StateStore(Protocol):
    async def acquire_service_lock(self, service_id: str) -> asyncio.Lock: ...

    async def upsert_node_heartbeat(
        self,
        node_id: str,
        agent_url: str,
        node_address: str | None = None,
        at: datetime | None = None,
    ) -> None: ...

    async def upsert_node_snapshot(self, node_id: str, snapshot: ResourceSnapshot) -> None: ...

    async def mark_node_unavailable(self, node_id: str) -> None: ...

    async def list_nodes(self) -> list[NodeState]: ...

    async def set_service_desired(self, state: ServiceDesiredState) -> None: ...

    async def get_service_desired(self, service_id: str) -> ServiceDesiredState | None: ...

    async def set_service_observed(self, state: ServiceObservedState) -> None: ...

    async def get_service_observed(self, service_id: str) -> ServiceObservedState | None: ...

    async def set_placement(self, placement: Placement) -> None: ...

    async def get_placement(self, service_id: str) -> Placement | None: ...

    async def clear_placement(self, service_id: str) -> None: ...

    async def list_placements(self) -> list[Placement]: ...

    async def set_pending_deployment(self, pending: PendingDeployment) -> None: ...

    async def pop_pending_deployment(self, service_id: str) -> None: ...

    async def list_pending_deployments(self) -> list[PendingDeployment]: ...

    async def increment_restart_counter(self, service_id: str) -> int: ...

    async def reset_restart_counter(self, service_id: str) -> None: ...

    async def get_restart_counter(self, service_id: str) -> RestartCounter: ...

    async def add_recovery_record(self, record: RecoveryRecord) -> None: ...

    async def list_recovery_records(self, limit: int = 100) -> list[RecoveryRecord]: ...

    async def append_event(
        self, event_type: EventType, message: str, details: dict[str, object]
    ) -> EventRecord: ...

    async def list_events(self, limit: int = 100) -> list[EventRecord]: ...


class RedisStateStore(StateStore):
    def __init__(
        self, redis: Redis, key_prefix: str = "orchestrator", event_log_max_items: int = 500
    ) -> None:
        self._redis: Any = redis
        self._prefix = key_prefix
        self._event_log_max_items = event_log_max_items
        self._service_locks: dict[str, asyncio.Lock] = {}
        self._service_locks_guard = asyncio.Lock()

    def _k(self, suffix: str) -> str:
        return f"{self._prefix}:{suffix}"

    async def acquire_service_lock(self, service_id: str) -> asyncio.Lock:
        async with self._service_locks_guard:
            existing = self._service_locks.get(service_id)
            if existing is not None:
                return existing
            lock = asyncio.Lock()
            self._service_locks[service_id] = lock
            return lock

    @staticmethod
    def _from_json(raw: str) -> dict[str, object]:
        loaded = json.loads(raw)
        if not isinstance(loaded, dict):
            raise StateStoreError("State payload must be a JSON object")
        return loaded

    async def upsert_node_heartbeat(
        self,
        node_id: str,
        agent_url: str,
        node_address: str | None = None,
        at: datetime | None = None,
    ) -> None:
        timestamp = at or datetime.now(UTC)
        key = self._k(f"node:{node_id}")
        incoming_address = _normalize_node_address(node_address)

        try:
            existing_raw = await self._redis.get(key)
            if existing_raw:
                existing = NodeState.model_validate(self._from_json(existing_raw))
                routable_address = incoming_address or _normalize_node_address(existing.node_address) or "unknown"
                state = existing.model_copy(
                    update={
                        "agent_url": agent_url,
                        "node_address": routable_address,
                        "status": NodeStatus.healthy,
                        "last_heartbeat_at": timestamp,
                    }
                )
            else:
                routable_address = incoming_address or "unknown"
                state = NodeState(
                    node_id=node_id,
                    agent_url=agent_url,
                    node_address=routable_address,
                    status=NodeStatus.healthy,
                    last_heartbeat_at=timestamp,
                )

            await self._redis.set(key, state.model_dump_json())
            await self._redis.sadd(self._k("nodes"), node_id)
        except RedisError as exc:
            raise StateStoreError(f"Failed to upsert heartbeat for node={node_id}") from exc

    async def upsert_node_snapshot(self, node_id: str, snapshot: ResourceSnapshot) -> None:
        key = self._k(f"node:{node_id}")

        try:
            existing_raw = await self._redis.get(key)
            if not existing_raw:
                raise StateStoreError(f"Cannot update snapshot for unknown node={node_id}")

            existing = NodeState.model_validate(self._from_json(existing_raw))
            updated = existing.model_copy(update={"last_resource_snapshot": snapshot})
            await self._redis.set(key, updated.model_dump_json())
        except RedisError as exc:
            raise StateStoreError(f"Failed to upsert snapshot for node={node_id}") from exc

    async def mark_node_unavailable(self, node_id: str) -> None:
        key = self._k(f"node:{node_id}")
        try:
            existing_raw = await self._redis.get(key)
            if not existing_raw:
                return
            existing = NodeState.model_validate(self._from_json(existing_raw))
            updated = existing.model_copy(update={"status": NodeStatus.unavailable})
            await self._redis.set(key, updated.model_dump_json())
        except RedisError as exc:
            raise StateStoreError(f"Failed to mark node unavailable node={node_id}") from exc

    async def list_nodes(self) -> list[NodeState]:
        try:
            node_ids = await self._redis.smembers(self._k("nodes"))
            if not node_ids:
                return []
            keys = [self._k(f"node:{node_id}") for node_id in sorted(node_ids)]
            raw_states = await self._redis.mget(keys)
        except RedisError as exc:
            raise StateStoreError("Failed to list nodes") from exc

        states: list[NodeState] = []
        for raw in raw_states:
            if raw is None:
                continue
            states.append(NodeState.model_validate(self._from_json(raw)))
        return states

    async def set_service_desired(self, state: ServiceDesiredState) -> None:
        try:
            await self._redis.set(
                self._k(f"service:{state.service.service_id}:desired"), state.model_dump_json()
            )
            await self._redis.sadd(self._k("services"), state.service.service_id)
        except RedisError as exc:
            raise StateStoreError("Failed to set desired state") from exc

    async def get_service_desired(self, service_id: str) -> ServiceDesiredState | None:
        try:
            raw = await self._redis.get(self._k(f"service:{service_id}:desired"))
        except RedisError as exc:
            raise StateStoreError("Failed to get desired state") from exc
        if raw is None:
            return None
        return ServiceDesiredState.model_validate(self._from_json(raw))

    async def set_service_observed(self, state: ServiceObservedState) -> None:
        try:
            await self._redis.set(
                self._k(f"service:{state.service_id}:observed"), state.model_dump_json()
            )
            await self._redis.sadd(self._k("services"), state.service_id)
        except RedisError as exc:
            raise StateStoreError("Failed to set observed state") from exc

    async def get_service_observed(self, service_id: str) -> ServiceObservedState | None:
        try:
            raw = await self._redis.get(self._k(f"service:{service_id}:observed"))
        except RedisError as exc:
            raise StateStoreError("Failed to get observed state") from exc
        if raw is None:
            return None
        return ServiceObservedState.model_validate(self._from_json(raw))

    async def set_placement(self, placement: Placement) -> None:
        try:
            await self._redis.set(
                self._k(f"service:{placement.service_id}:placement"), placement.model_dump_json()
            )
            await self._redis.sadd(self._k("placements"), placement.service_id)
        except RedisError as exc:
            raise StateStoreError("Failed to set placement") from exc

    async def get_placement(self, service_id: str) -> Placement | None:
        try:
            raw = await self._redis.get(self._k(f"service:{service_id}:placement"))
        except RedisError as exc:
            raise StateStoreError("Failed to get placement") from exc
        if raw is None:
            return None
        return Placement.model_validate(self._from_json(raw))

    async def clear_placement(self, service_id: str) -> None:
        try:
            await self._redis.delete(self._k(f"service:{service_id}:placement"))
            await self._redis.srem(self._k("placements"), service_id)
        except RedisError as exc:
            raise StateStoreError("Failed to clear placement") from exc

    async def list_placements(self) -> list[Placement]:
        try:
            service_ids = await self._redis.smembers(self._k("placements"))
            if not service_ids:
                return []
            keys = [
                self._k(f"service:{service_id}:placement") for service_id in sorted(service_ids)
            ]
            raw_placements = await self._redis.mget(keys)
        except RedisError as exc:
            raise StateStoreError("Failed to list placements") from exc

        placements: list[Placement] = []
        for raw in raw_placements:
            if raw is None:
                continue
            placements.append(Placement.model_validate(self._from_json(raw)))
        return placements

    async def set_pending_deployment(self, pending: PendingDeployment) -> None:
        try:
            await self._redis.set(
                self._k(f"service:{pending.service_id}:pending"), pending.model_dump_json()
            )
            await self._redis.sadd(self._k("pending_services"), pending.service_id)
        except RedisError as exc:
            raise StateStoreError("Failed to set pending deployment") from exc

    async def pop_pending_deployment(self, service_id: str) -> None:
        try:
            await self._redis.delete(self._k(f"service:{service_id}:pending"))
            await self._redis.srem(self._k("pending_services"), service_id)
        except RedisError as exc:
            raise StateStoreError("Failed to pop pending deployment") from exc

    async def list_pending_deployments(self) -> list[PendingDeployment]:
        try:
            pending_ids = await self._redis.smembers(self._k("pending_services"))
            if not pending_ids:
                return []
            keys = [self._k(f"service:{service_id}:pending") for service_id in sorted(pending_ids)]
            raw_pending = await self._redis.mget(keys)
        except RedisError as exc:
            raise StateStoreError("Failed to list pending deployments") from exc

        deployments: list[PendingDeployment] = []
        for raw in raw_pending:
            if raw is None:
                continue
            deployments.append(PendingDeployment.model_validate(self._from_json(raw)))
        return deployments

    async def increment_restart_counter(self, service_id: str) -> int:
        try:
            new_count = await self._redis.incr(self._k(f"service:{service_id}:restart_counter"))
        except RedisError as exc:
            raise StateStoreError("Failed to increment restart counter") from exc
        return int(new_count)

    async def reset_restart_counter(self, service_id: str) -> None:
        try:
            await self._redis.set(self._k(f"service:{service_id}:restart_counter"), "0")
        except RedisError as exc:
            raise StateStoreError("Failed to reset restart counter") from exc

    async def get_restart_counter(self, service_id: str) -> RestartCounter:
        try:
            raw = await self._redis.get(self._k(f"service:{service_id}:restart_counter"))
        except RedisError as exc:
            raise StateStoreError("Failed to get restart counter") from exc

        if raw is None:
            return RestartCounter(service_id=service_id)
        try:
            parsed = int(raw)
        except ValueError:
            # Backward compatibility with older JSON-encoded counter payload.
            return RestartCounter.model_validate(self._from_json(raw))
        return RestartCounter(service_id=service_id, count=parsed, updated_at=datetime.now(UTC))

    async def add_recovery_record(self, record: RecoveryRecord) -> None:
        key = self._k("recovery_history")
        try:
            await self._redis.lpush(key, record.model_dump_json())
            await self._redis.ltrim(key, 0, self._event_log_max_items - 1)
        except RedisError as exc:
            raise StateStoreError("Failed to append recovery record") from exc

    async def list_recovery_records(self, limit: int = 100) -> list[RecoveryRecord]:
        key = self._k("recovery_history")
        try:
            raw_records = await self._redis.lrange(key, 0, max(0, limit - 1))
        except RedisError as exc:
            raise StateStoreError("Failed to list recovery records") from exc

        return [RecoveryRecord.model_validate(self._from_json(raw)) for raw in raw_records]

    async def append_event(
        self, event_type: EventType, message: str, details: dict[str, object]
    ) -> EventRecord:
        event = EventRecord(
            event_id=str(uuid.uuid4()),
            event_type=event_type,
            message=message,
            details=details,
        )
        key = self._k("events")
        try:
            await self._redis.lpush(key, event.model_dump_json())
            await self._redis.ltrim(key, 0, self._event_log_max_items - 1)
        except RedisError as exc:
            raise StateStoreError("Failed to append event") from exc
        return event

    async def list_events(self, limit: int = 100) -> list[EventRecord]:
        key = self._k("events")
        try:
            raw_events = await self._redis.lrange(key, 0, max(0, limit - 1))
        except RedisError as exc:
            raise StateStoreError("Failed to list events") from exc

        return [EventRecord.model_validate(self._from_json(raw)) for raw in raw_events]


class InMemoryStateStore(StateStore):
    def __init__(self, event_log_max_items: int = 500) -> None:
        self._nodes: dict[str, NodeState] = {}
        self._desired: dict[str, ServiceDesiredState] = {}
        self._observed: dict[str, ServiceObservedState] = {}
        self._placement: dict[str, Placement] = {}
        self._pending: dict[str, PendingDeployment] = {}
        self._restart_counter: dict[str, RestartCounter] = {}
        self._recovery_history: deque[RecoveryRecord] = deque(maxlen=event_log_max_items)
        self._events: deque[EventRecord] = deque(maxlen=event_log_max_items)
        self._lock = asyncio.Lock()
        self._service_locks: dict[str, asyncio.Lock] = {}
        self._service_locks_guard = asyncio.Lock()

    async def acquire_service_lock(self, service_id: str) -> asyncio.Lock:
        async with self._service_locks_guard:
            existing = self._service_locks.get(service_id)
            if existing is not None:
                return existing
            lock = asyncio.Lock()
            self._service_locks[service_id] = lock
            return lock

    async def upsert_node_heartbeat(
        self,
        node_id: str,
        agent_url: str,
        node_address: str | None = None,
        at: datetime | None = None,
    ) -> None:
        timestamp = at or datetime.now(UTC)
        incoming_address = _normalize_node_address(node_address)
        async with self._lock:
            current = self._nodes.get(node_id)
            if current is None:
                routable_address = incoming_address or "unknown"
                self._nodes[node_id] = NodeState(
                    node_id=node_id,
                    agent_url=agent_url,
                    node_address=routable_address,
                    status=NodeStatus.healthy,
                    last_heartbeat_at=timestamp,
                )
                return
            routable_address = (
                incoming_address or _normalize_node_address(current.node_address) or "unknown"
            )
            self._nodes[node_id] = current.model_copy(
                update={
                    "agent_url": agent_url,
                    "node_address": routable_address,
                    "status": NodeStatus.healthy,
                    "last_heartbeat_at": timestamp,
                }
            )

    async def upsert_node_snapshot(self, node_id: str, snapshot: ResourceSnapshot) -> None:
        async with self._lock:
            current = self._nodes.get(node_id)
            if current is None:
                raise StateStoreError(f"Cannot update snapshot for unknown node={node_id}")
            self._nodes[node_id] = current.model_copy(update={"last_resource_snapshot": snapshot})

    async def mark_node_unavailable(self, node_id: str) -> None:
        async with self._lock:
            current = self._nodes.get(node_id)
            if current is None:
                return
            self._nodes[node_id] = current.model_copy(update={"status": NodeStatus.unavailable})

    async def list_nodes(self) -> list[NodeState]:
        async with self._lock:
            return sorted(self._nodes.values(), key=lambda node: node.node_id)

    async def set_service_desired(self, state: ServiceDesiredState) -> None:
        async with self._lock:
            self._desired[state.service.service_id] = state

    async def get_service_desired(self, service_id: str) -> ServiceDesiredState | None:
        async with self._lock:
            return self._desired.get(service_id)

    async def set_service_observed(self, state: ServiceObservedState) -> None:
        async with self._lock:
            self._observed[state.service_id] = state

    async def get_service_observed(self, service_id: str) -> ServiceObservedState | None:
        async with self._lock:
            return self._observed.get(service_id)

    async def set_placement(self, placement: Placement) -> None:
        async with self._lock:
            self._placement[placement.service_id] = placement

    async def get_placement(self, service_id: str) -> Placement | None:
        async with self._lock:
            return self._placement.get(service_id)

    async def clear_placement(self, service_id: str) -> None:
        async with self._lock:
            self._placement.pop(service_id, None)

    async def list_placements(self) -> list[Placement]:
        async with self._lock:
            return sorted(self._placement.values(), key=lambda placement: placement.service_id)

    async def set_pending_deployment(self, pending: PendingDeployment) -> None:
        async with self._lock:
            self._pending[pending.service_id] = pending

    async def pop_pending_deployment(self, service_id: str) -> None:
        async with self._lock:
            self._pending.pop(service_id, None)

    async def list_pending_deployments(self) -> list[PendingDeployment]:
        async with self._lock:
            return sorted(self._pending.values(), key=lambda item: item.service_id)

    async def increment_restart_counter(self, service_id: str) -> int:
        async with self._lock:
            current = self._restart_counter.get(service_id, RestartCounter(service_id=service_id))
            updated = RestartCounter(
                service_id=service_id, count=current.count + 1, updated_at=datetime.now(UTC)
            )
            self._restart_counter[service_id] = updated
            return updated.count

    async def reset_restart_counter(self, service_id: str) -> None:
        async with self._lock:
            self._restart_counter[service_id] = RestartCounter(
                service_id=service_id, count=0, updated_at=datetime.now(UTC)
            )

    async def get_restart_counter(self, service_id: str) -> RestartCounter:
        async with self._lock:
            return self._restart_counter.get(service_id, RestartCounter(service_id=service_id))

    async def add_recovery_record(self, record: RecoveryRecord) -> None:
        async with self._lock:
            self._recovery_history.appendleft(record)

    async def list_recovery_records(self, limit: int = 100) -> list[RecoveryRecord]:
        async with self._lock:
            return list(self._recovery_history)[:limit]

    async def append_event(
        self, event_type: EventType, message: str, details: dict[str, object]
    ) -> EventRecord:
        event = EventRecord(
            event_id=str(uuid.uuid4()),
            event_type=event_type,
            message=message,
            details=details,
        )
        async with self._lock:
            self._events.appendleft(event)
        return event

    async def list_events(self, limit: int = 100) -> list[EventRecord]:
        async with self._lock:
            return list(self._events)[:limit]

def _normalize_node_address(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    if not normalized:
        return None
    if normalized.lower() == "unknown":
        return None
    return normalized
