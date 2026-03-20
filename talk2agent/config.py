from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from talk2agent.provider_runtime import (
    DEFAULT_PROVIDER,
    SUPPORTED_PROVIDERS,
    resolve_provider_profile,
)


@dataclass(slots=True)
class TelegramConfig:
    bot_token: str
    allowed_user_ids: list[int]
    admin_user_id: int


@dataclass(slots=True)
class AgentConfig:
    provider: str
    workspace_dir: str

    @property
    def command(self) -> str:
        # Transitional shim: the active command is derived from the provider profile.
        return resolve_provider_profile(self.provider).command

    @property
    def args(self) -> list[str]:
        # Transitional shim: provider-specific args come from the runtime profile.
        return list(resolve_provider_profile(self.provider).args)


@dataclass(slots=True)
class PermissionsConfig:
    mode: str


@dataclass(slots=True)
class RuntimeConfig:
    idle_timeout_minutes: int
    stream_edit_interval_ms: int
    provider_state_path: str


@dataclass(slots=True)
class AppConfig:
    telegram: TelegramConfig
    agent: AgentConfig
    permissions: PermissionsConfig
    runtime: RuntimeConfig


def validate_config(config: AppConfig) -> None:
    if config.agent.provider not in SUPPORTED_PROVIDERS:
        raise ValueError("agent.provider must be one of claude/codex/gemini")
    if not config.telegram.allowed_user_ids:
        raise ValueError("telegram.allowed_user_ids must not be empty")
    if config.telegram.admin_user_id not in config.telegram.allowed_user_ids:
        raise ValueError("telegram.admin_user_id must be present in telegram.allowed_user_ids")
    if config.permissions.mode != "auto_approve":
        raise ValueError("MVP only supports permissions.mode=auto_approve")


def load_config(path: Path) -> AppConfig:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    config = _parse_config(data)
    validate_config(config)
    return config


def write_default_config(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "telegram": {
            "bot_token": "YOUR_TELEGRAM_BOT_TOKEN",
            "allowed_user_ids": [123456789],
            "admin_user_id": 123456789,
        },
        "agent": {
            "provider": DEFAULT_PROVIDER,
            "workspace_dir": ".",
        },
        "permissions": {
            "mode": "auto_approve",
        },
        "runtime": {
            "idle_timeout_minutes": 30,
            "stream_edit_interval_ms": 700,
            "provider_state_path": ".talk2agent-provider-state.json",
        },
    }
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


def _parse_config(data: Any) -> AppConfig:
    if not isinstance(data, dict):
        raise ValueError("config must be a mapping")

    telegram = data["telegram"]
    agent = data["agent"]
    permissions = data["permissions"]
    runtime = data["runtime"]

    return AppConfig(
        telegram=TelegramConfig(
            bot_token=telegram["bot_token"],
            allowed_user_ids=_require_list(
                telegram["allowed_user_ids"], "telegram.allowed_user_ids"
            ),
            admin_user_id=telegram["admin_user_id"],
        ),
        agent=AgentConfig(
            provider=agent["provider"],
            workspace_dir=agent["workspace_dir"],
        ),
        permissions=PermissionsConfig(mode=permissions["mode"]),
        runtime=RuntimeConfig(
            idle_timeout_minutes=runtime["idle_timeout_minutes"],
            stream_edit_interval_ms=runtime["stream_edit_interval_ms"],
            provider_state_path=runtime["provider_state_path"],
        ),
    )


def _require_list(value: Any, field_name: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{field_name} must be a list")
    return value
