from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


SUPPORTED_PROVIDERS = ("claude", "codex", "gemini")
DEFAULT_PROVIDER = "codex"


@dataclass(frozen=True, slots=True)
class ProviderProfile:
    provider: str
    display_name: str
    command: str
    args: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RuntimeState:
    provider: str
    workspace_id: str
    workspace_path: str
    session_store: Any


def _platform_command(command: str) -> str:
    if sys.platform == "win32" and Path(command).suffix == "":
        return f"{command}.cmd"
    return command


PROVIDER_PROFILES = {
    "claude": ProviderProfile(
        "claude",
        "Claude Code",
        _platform_command("claude-agent-acp"),
        (),
    ),
    "codex": ProviderProfile(
        "codex",
        "Codex",
        _platform_command("codex-acp"),
        (),
    ),
    "gemini": ProviderProfile(
        "gemini",
        "Gemini CLI",
        _platform_command("gemini"),
        ("--acp",),
    ),
}


@dataclass(frozen=True, slots=True)
class PersistedRuntimeSelection:
    provider: str
    workspace_id: str | None = None


def resolve_provider_profile(provider: str) -> ProviderProfile:
    try:
        return PROVIDER_PROFILES[provider]
    except KeyError as exc:
        raise ValueError(f"unsupported provider: {provider}") from exc


def iter_provider_profiles() -> tuple[ProviderProfile, ...]:
    return tuple(PROVIDER_PROFILES[name] for name in SUPPORTED_PROVIDERS)


def load_persisted_runtime_selection(path: Path) -> PersistedRuntimeSelection | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except Exception:
        return None

    if not isinstance(data, dict):
        return None

    provider = data.get("provider")
    if provider not in SUPPORTED_PROVIDERS:
        return None

    workspace_id = data.get("workspace_id")
    if workspace_id is not None:
        workspace_id = str(workspace_id)
    return PersistedRuntimeSelection(provider=provider, workspace_id=workspace_id)


def load_persisted_provider(path: Path) -> str | None:
    selection = load_persisted_runtime_selection(path)
    return None if selection is None else selection.provider


def write_persisted_runtime_selection(path: Path, provider: str, workspace_id: str | None) -> None:
    resolve_provider_profile(provider)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {"provider": provider}
    if workspace_id is not None:
        payload["workspace_id"] = workspace_id
    path.write_text(json.dumps(payload), encoding="utf-8")


def write_persisted_provider(path: Path, provider: str) -> None:
    write_persisted_runtime_selection(path, provider, None)


def resolve_startup_provider(configured_provider: str, state_path: Path) -> str:
    resolve_provider_profile(configured_provider)
    persisted = load_persisted_runtime_selection(state_path)
    return configured_provider if persisted is None else persisted.provider


def resolve_startup_runtime_selection(
    configured_provider: str,
    configured_workspace_id: str,
    state_path: Path,
) -> PersistedRuntimeSelection:
    resolve_provider_profile(configured_provider)
    persisted = load_persisted_runtime_selection(state_path)
    if persisted is None:
        return PersistedRuntimeSelection(
            provider=configured_provider,
            workspace_id=configured_workspace_id,
        )
    return PersistedRuntimeSelection(
        provider=persisted.provider,
        workspace_id=persisted.workspace_id or configured_workspace_id,
    )
