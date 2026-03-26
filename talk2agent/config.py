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

DEFAULT_SESSION_HISTORY_PATH = ".talk2agent-session-history.json"


@dataclass(slots=True)
class TelegramConfig:
    bot_token: str
    allowed_user_ids: list[int]
    admin_user_id: int


@dataclass(slots=True)
class AgentConfig:
    provider: str
    workspace_dir: str
    workspaces: list["WorkspaceConfig"]

    @property
    def command(self) -> str:
        # Transitional shim: the active command is derived from the provider profile.
        return resolve_provider_profile(self.provider).command

    @property
    def args(self) -> list[str]:
        # Transitional shim: provider-specific args come from the runtime profile.
        return list(resolve_provider_profile(self.provider).args)

    @property
    def default_workspace(self) -> "WorkspaceConfig":
        for workspace in self.workspaces:
            if workspace.path == self.workspace_dir:
                return workspace
        raise ValueError("agent.workspace_dir must match one configured workspace")

    def resolve_workspace(self, workspace_id: str) -> "WorkspaceConfig":
        for workspace in self.workspaces:
            if workspace.id == workspace_id:
                return workspace
        raise ValueError(f"unknown workspace: {workspace_id}")


@dataclass(slots=True)
class WorkspaceConfig:
    id: str
    label: str
    path: str


@dataclass(slots=True)
class PermissionsConfig:
    mode: str


@dataclass(slots=True)
class RuntimeConfig:
    idle_timeout_minutes: int
    stream_edit_interval_ms: int
    provider_state_path: str
    session_history_path: str


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
    if not config.agent.workspaces:
        raise ValueError("agent.workspaces must not be empty")

    seen_workspace_ids: set[str] = set()
    seen_workspace_paths: set[str] = set()
    default_workspace_matches = 0
    for workspace in config.agent.workspaces:
        if not workspace.id:
            raise ValueError("agent.workspaces[].id must not be empty")
        if not workspace.label:
            raise ValueError("agent.workspaces[].label must not be empty")
        if not workspace.path:
            raise ValueError("agent.workspaces[].path must not be empty")
        if workspace.id in seen_workspace_ids:
            raise ValueError("agent.workspaces[].id must be unique")
        if workspace.path in seen_workspace_paths:
            raise ValueError("agent.workspaces[].path must be unique")
        seen_workspace_ids.add(workspace.id)
        seen_workspace_paths.add(workspace.path)
        if workspace.path == config.agent.workspace_dir:
            default_workspace_matches += 1

    if default_workspace_matches != 1:
        raise ValueError(
            "agent.workspace_dir must match exactly one entry in agent.workspaces"
        )


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
            "workspaces": [
                {
                    "id": "default",
                    "label": "Default Workspace",
                    "path": ".",
                }
            ],
        },
        "permissions": {
            "mode": "auto_approve",
        },
        "runtime": {
            "idle_timeout_minutes": 30,
            "stream_edit_interval_ms": 700,
            "provider_state_path": ".talk2agent-provider-state.json",
            "session_history_path": DEFAULT_SESSION_HISTORY_PATH,
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
            workspaces=_parse_workspaces(agent),
        ),
        permissions=PermissionsConfig(mode=permissions["mode"]),
        runtime=RuntimeConfig(
            idle_timeout_minutes=runtime["idle_timeout_minutes"],
            stream_edit_interval_ms=runtime["stream_edit_interval_ms"],
            provider_state_path=runtime["provider_state_path"],
            session_history_path=runtime.get(
                "session_history_path", DEFAULT_SESSION_HISTORY_PATH
            ),
        ),
    )


def _require_list(value: Any, field_name: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{field_name} must be a list")
    return value


def _parse_workspaces(agent: dict[str, Any]) -> list[WorkspaceConfig]:
    workspace_dir = agent["workspace_dir"]
    raw_workspaces = agent.get("workspaces")
    if raw_workspaces is None:
        return [
            WorkspaceConfig(
                id="default",
                label="Default Workspace",
                path=workspace_dir,
            )
        ]

    workspaces = _require_list(raw_workspaces, "agent.workspaces")
    parsed: list[WorkspaceConfig] = []
    for index, workspace in enumerate(workspaces):
        if not isinstance(workspace, dict):
            raise ValueError(f"agent.workspaces[{index}] must be a mapping")
        parsed.append(
            WorkspaceConfig(
                id=str(workspace["id"]),
                label=str(workspace["label"]),
                path=str(workspace["path"]),
            )
        )
    return parsed
