from __future__ import annotations

import pytest

from controller.config import ControllerSettings


def test_controller_timeout_defaults_are_explicit() -> None:
    settings = ControllerSettings()

    assert settings.agent_deploy_timeout_seconds == 60
    assert settings.agent_command_timeout_seconds == 15
    assert settings.agent_read_timeout_seconds == 5


@pytest.mark.parametrize(
    "env_name,field_name,value",
    [
        ("AGENT_DEPLOY_TIMEOUT_SECONDS", "agent_deploy_timeout_seconds", "91"),
        ("CONTROLLER_AGENT_COMMAND_TIMEOUT_SECONDS", "agent_command_timeout_seconds", "17"),
        ("AGENT_READ_TIMEOUT_SECONDS", "agent_read_timeout_seconds", "9"),
    ],
)
def test_controller_timeout_env_names_are_accepted(
    monkeypatch: pytest.MonkeyPatch,
    env_name: str,
    field_name: str,
    value: str,
) -> None:
    monkeypatch.setenv(env_name, value)
    settings = ControllerSettings()

    assert getattr(settings, field_name) == int(value)
