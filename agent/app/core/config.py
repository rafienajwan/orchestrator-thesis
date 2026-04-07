from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class AgentSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    node_id: str = Field(default="node-a", min_length=1)
    agent_public_url: str = Field(default="http://agent-a:8080", min_length=1)
    advertised_host: str = Field(default="agent-a", min_length=1)
    controller_base_url: str = Field(default="http://controller:8000", min_length=1)

    agent_host: str = "0.0.0.0"
    agent_port: int = Field(default=8080, ge=1, le=65535)
    log_level: str = "INFO"

    telemetry_interval_seconds: int = Field(default=5, ge=1, le=60)
    health_check_interval_seconds: int = Field(default=10, ge=1, le=60)
    health_check_timeout_seconds: int = Field(default=2, ge=1, le=30)
    health_check_retries: int = Field(default=3, ge=1, le=10)

    docker_network_name: str = Field(default="orchestrator-thesis-net", min_length=1)
    docker_base_url: str | None = None
    container_stop_timeout_seconds: int = Field(default=10, ge=1, le=120)
    published_port_base: int = Field(default=20000, ge=1024, le=65000)
    published_port_max: int = Field(default=20999, ge=1024, le=65535)


@lru_cache(maxsize=1)
def get_settings() -> AgentSettings:
    return AgentSettings()
