from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from logging import StreamHandler

from fastapi import FastAPI
from pythonjsonlogger.json import JsonFormatter
from redis.asyncio import Redis

from controller.agent_client import HttpAgentClient
from controller.api import build_router
from controller.config import ControllerSettings, get_settings
from controller.reconciler import Reconciler
from controller.self_healing import SelfHealingManager
from controller.service_manager import ServiceManager
from controller.state_store import RedisStateStore

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
    redis_client = Redis.from_url(settings.redis_url, encoding="utf-8", decode_responses=True)
    state_store = RedisStateStore(
        redis=redis_client,
        key_prefix=settings.redis_key_prefix,
        event_log_max_items=settings.event_log_max_items,
    )
    agent_client = HttpAgentClient(
        deploy_timeout_seconds=float(settings.agent_deploy_timeout_seconds),
        command_timeout_seconds=float(settings.agent_command_timeout_seconds),
        read_timeout_seconds=float(settings.agent_read_timeout_seconds),
    )
    service_manager = ServiceManager(store=state_store, agent_client=agent_client)
    self_healing_manager = SelfHealingManager(
        settings=settings,
        store=state_store,
        agent_client=agent_client,
    )
    reconciler = Reconciler(
        settings=settings,
        store=state_store,
        service_manager=service_manager,
        self_healing=self_healing_manager,
    )

    stop_event = asyncio.Event()
    reconcile_task = asyncio.create_task(
        reconciler.run_forever(stop_event), name="controller-reconciler"
    )

    app.state.settings = settings
    app.state.redis_client = redis_client
    app.state.state_store = state_store
    app.state.agent_client = agent_client
    app.state.service_manager = service_manager
    app.state.self_healing_manager = self_healing_manager
    app.state.reconciler = reconciler
    app.state.reconcile_stop_event = stop_event
    app.state.reconcile_task = reconcile_task

    try:
        yield
    finally:
        stop_event.set()
        await asyncio.gather(reconcile_task, return_exceptions=True)
        await redis_client.aclose()


def create_app(settings: ControllerSettings | None = None) -> FastAPI:
    app = FastAPI(
        title="Mini Orchestrator Controller",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.include_router(build_router())
    if settings is not None:
        app.state.settings = settings
    return app


app = create_app()
