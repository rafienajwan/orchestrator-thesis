from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

import docker
from docker.errors import DockerException

from agent.app.core.config import AgentSettings
from controller.models import AgentCrashReport

logger = logging.getLogger(__name__)

# Docker event types that indicate a container crash or abnormal exit
_CRASH_EVENT_ACTIONS = frozenset({"die", "oom", "kill"})


class DockerEventWatcher:
    """Watches Docker event stream for container crashes on managed workloads.

    When a managed container emits a 'die', 'oom', or 'kill' event, this watcher
    immediately sends a crash report to the controller, bypassing the poll-based
    health check interval for sub-second crash detection.
    """

    def __init__(
        self,
        settings: AgentSettings,
        reporter: _CrashReporter,
        docker_client: docker.DockerClient | None = None,
    ) -> None:
        self._settings = settings
        self._reporter = reporter
        self._client = docker_client
        self._cooldown: dict[str, datetime] = {}
        self._cooldown_seconds = settings.crash_report_cooldown_seconds

    async def run(self, stop_event: asyncio.Event) -> None:
        """Run the event watcher loop. Reconnects on failure with backoff."""
        backoff = 1.0
        max_backoff = 30.0

        while not stop_event.is_set():
            try:
                await asyncio.to_thread(self._watch_events_blocking, stop_event)
            except Exception:
                logger.exception(
                    "Docker event watcher stream error, reconnecting",
                    extra={"backoff_seconds": backoff},
                )

            if stop_event.is_set():
                break

            # Exponential backoff on reconnect
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=backoff)
                break  # stop_event was set
            except TimeoutError:
                pass
            backoff = min(backoff * 2, max_backoff)

    def _get_client(self) -> docker.DockerClient:
        if self._client is None:
            self._client = docker.from_env()
        return self._client

    def _watch_events_blocking(self, stop_event: asyncio.Event) -> None:
        """Blocking Docker event stream reader (runs in thread pool)."""
        client = self._get_client()

        # Filter for container events only, with managed label
        filters = {
            "type": "container",
            "event": list(_CRASH_EVENT_ACTIONS),
            "label": ["orchestrator.managed=true"],
        }

        for event in client.events(decode=True, filters=filters):
            if stop_event.is_set():
                break

            action = event.get("Action", "")
            if action not in _CRASH_EVENT_ACTIONS:
                continue

            self._handle_event(event)

    def _handle_event(self, event: dict[str, Any]) -> None:
        """Process a single Docker event."""
        actor = event.get("Actor", {})
        attributes = actor.get("Attributes", {})

        # Only handle our managed containers
        if attributes.get("orchestrator.managed") != "true":
            return

        node_id = attributes.get("orchestrator.node_id", "")
        if node_id != self._settings.node_id:
            return

        service_id = attributes.get("orchestrator.service_id")
        if not service_id:
            return

        container_id = actor.get("ID", "")
        action = event.get("Action", "die")

        # Extract exit code from attributes
        exit_code = -1
        exit_code_str = attributes.get("exitCode", "")
        if exit_code_str:
            try:
                exit_code = int(exit_code_str)
            except ValueError:
                pass

        # For 'die' events with exit code 0, this is a normal stop — skip
        if action == "die" and exit_code == 0:
            return

        # Cooldown: don't spam controller with duplicate crash reports
        now = datetime.now(UTC)
        last_report = self._cooldown.get(service_id)
        if last_report is not None:
            elapsed = (now - last_report).total_seconds()
            if elapsed < self._cooldown_seconds:
                return

        self._cooldown[service_id] = now

        report = AgentCrashReport(
            node_id=self._settings.node_id,
            service_id=service_id,
            container_id=container_id[:12] if len(container_id) > 12 else container_id,
            event_type=action,
            exit_code=exit_code,
            occurred_at=now,
        )

        logger.info(
            "Container crash detected via Docker event",
            extra={
                "service_id": service_id,
                "event_type": action,
                "exit_code": exit_code,
                "container_id": report.container_id,
            },
        )

        # Fire-and-forget send to controller
        try:
            asyncio.get_event_loop().create_task(self._reporter.send_crash_report(report))
        except RuntimeError:
            # Event loop not running — we're being shut down
            pass


class _CrashReporter:
    """Protocol for sending crash reports to the controller."""

    async def send_crash_report(self, report: AgentCrashReport) -> None: ...
