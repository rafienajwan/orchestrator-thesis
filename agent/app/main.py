from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from logging import StreamHandler

from fastapi import FastAPI
from pythonjsonlogger.json import JsonFormatter

from agent.app.adapters.docker_adapter import DockerAdapter, DockerSdkAdapter
from agent.app.api import build_router
from agent.app.core.config import AgentSettings, get_settings
from agent.app.core.state import AgentStateStore
from agent.app.services.docker_event_watcher import DockerEventWatcher
from agent.app.services.telemetry import AgentTelemetryService, ControllerReporter, ResourceSampler
from agent.app.services.workload_manager import AgentWorkloadManager

logger = logging.getLogger(__name__)


def _configure_logging(level: str) -> None:
    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    handler = StreamHandler()
    handler.setFormatter(JsonFormatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root_logger.addHandler(handler)
    root_logger.setLevel(level)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = getattr(app.state, "settings", None)
    if settings is None:
        settings = get_settings()

    _configure_logging(settings.log_level)

    state_store = AgentStateStore(
        node_id=settings.node_id,
        node_address=settings.advertised_host,
        agent_url=settings.agent_public_url,
        controller_url=settings.controller_base_url,
    )
    docker_adapter = getattr(app.state, "docker_adapter", None)
    if docker_adapter is None:
        docker_adapter = DockerSdkAdapter(
            network_name=settings.docker_network_name,
            node_id=settings.node_id,
        )
    workload_manager = AgentWorkloadManager(
        settings=settings, store=state_store, adapter=docker_adapter
    )
    reporter = ControllerReporter(settings=settings)
    sampler = ResourceSampler()
    telemetry_service = AgentTelemetryService(
        settings=settings,
        store=state_store,
        workload_manager=workload_manager,
        reporter=reporter,
        sampler=sampler,
    )

    stop_event = asyncio.Event()
    background_tasks: list[asyncio.Task[None]] = []

    start_telemetry = getattr(app.state, "start_telemetry", True)
    if start_telemetry:
        background_tasks.append(
            asyncio.create_task(telemetry_service.run(stop_event), name="agent-telemetry")
        )

    # Start Docker event watcher for sub-second crash detection
    start_event_watcher = getattr(app.state, "start_event_watcher", True)
    if start_telemetry and start_event_watcher and settings.docker_event_watcher_enabled:
        event_watcher = DockerEventWatcher(
            settings=settings,
            reporter=reporter,
        )
        background_tasks.append(
            asyncio.create_task(event_watcher.run(stop_event), name="agent-event-watcher")
        )

    app.state.settings = settings
    app.state.state_store = state_store
    app.state.docker_adapter = docker_adapter
    app.state.workload_manager = workload_manager
    app.state.telemetry_service = telemetry_service
    app.state.telemetry_stop_event = stop_event
    app.state.telemetry_task = background_tasks[0] if background_tasks else None

    try:
        yield
    finally:
        stop_event.set()
        if background_tasks:
            await asyncio.gather(*background_tasks, return_exceptions=True)
        await reporter.close()


def create_app(
    settings: AgentSettings | None = None,
    docker_adapter: DockerAdapter | None = None,
    start_telemetry: bool = True,
) -> FastAPI:
    app = FastAPI(title="Mini Orchestrator Agent", version="0.1.0", lifespan=lifespan)
    app.include_router(build_router())
    if settings is not None:
        app.state.settings = settings
    if docker_adapter is not None:
        app.state.docker_adapter = docker_adapter
    app.state.start_telemetry = start_telemetry
    return app


app = create_app()
