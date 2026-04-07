from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from fastapi import FastAPI, HTTPException, status
from pydantic import BaseModel, ConfigDict

UNHEALTHY_FLAG = Path("/tmp/unhealthy")


class SampleResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    service: str
    status: str


@dataclass
class SampleState:
    name: str = "sample-app"

    def is_healthy(self) -> bool:
        return not UNHEALTHY_FLAG.exists()

    def mark_unhealthy(self) -> None:
        UNHEALTHY_FLAG.touch(exist_ok=True)

    def mark_healthy(self) -> None:
        if UNHEALTHY_FLAG.exists():
            UNHEALTHY_FLAG.unlink()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    app.state.sample_state = SampleState()
    yield


app = FastAPI(title="Sample App", version="0.1.0", lifespan=lifespan)


@app.get("/", response_model=SampleResponse)
async def index() -> SampleResponse:
    return SampleResponse(service="sample-app", status="ok")


@app.get("/health", response_model=SampleResponse)
async def health() -> SampleResponse:
    state = app.state.sample_state
    if not state.is_healthy():
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="unhealthy")
    return SampleResponse(service=state.name, status="healthy")


@app.post("/health/fail", response_model=SampleResponse)
async def fail_health() -> SampleResponse:
    state = app.state.sample_state
    state.mark_unhealthy()
    return SampleResponse(service=state.name, status="unhealthy")


@app.post("/health/recover", response_model=SampleResponse)
async def recover_health() -> SampleResponse:
    state = app.state.sample_state
    state.mark_healthy()
    return SampleResponse(service=state.name, status="healthy")
