from functools import lru_cache

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class ControllerSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_name: str = "mini-orchestrator-controller"
    environment: str = "development"
    log_level: str = "INFO"

    redis_url: str = "redis://redis:6379/0"
    redis_key_prefix: str = "orchestrator"

    heartbeat_interval_seconds: int = 5
    resource_snapshot_interval_seconds: int = 5
    health_check_interval_seconds: int = 10
    health_check_timeout_seconds: int = 2
    health_check_retries: int = 3

    heartbeat_timeout_seconds: int = 30
    reconciliation_interval_seconds: int = 15

    agent_deploy_timeout_seconds: int = Field(
        default=60,
        ge=5,
        le=600,
        validation_alias=AliasChoices(
            "AGENT_DEPLOY_TIMEOUT_SECONDS",
            "CONTROLLER_AGENT_DEPLOY_TIMEOUT_SECONDS",
        ),
    )
    agent_command_timeout_seconds: int = Field(
        default=15,
        ge=5,
        le=300,
        validation_alias=AliasChoices(
            "AGENT_COMMAND_TIMEOUT_SECONDS",
            "CONTROLLER_AGENT_COMMAND_TIMEOUT_SECONDS",
        ),
    )
    agent_read_timeout_seconds: int = Field(
        default=5,
        ge=1,
        le=120,
        validation_alias=AliasChoices(
            "AGENT_READ_TIMEOUT_SECONDS",
            "CONTROLLER_AGENT_READ_TIMEOUT_SECONDS",
        ),
    )

    max_restart_attempts: int = 2
    cooldown_intervals_after_recovery: int = 1

    event_log_max_items: int = Field(default=500, ge=100, le=10_000)


@lru_cache(maxsize=1)
def get_settings() -> ControllerSettings:
    return ControllerSettings()
