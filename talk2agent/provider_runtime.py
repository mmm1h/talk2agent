from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


SUPPORTED_PROVIDERS = ("claude", "codex", "gemini")
DEFAULT_PROVIDER = "gemini"


@dataclass(frozen=True, slots=True)
class ProviderProfile:
    provider: str
    command: str
    args: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RuntimeState:
    provider: str
    session_store: Any


def _platform_command(command: str) -> str:
    if sys.platform == "win32" and Path(command).suffix == "":
        return f"{command}.cmd"
    return command


PROVIDER_PROFILES = {
    "claude": ProviderProfile("claude", _platform_command("claude-agent-acp"), ()),
    "codex": ProviderProfile("codex", _platform_command("codex-acp"), ()),
    "gemini": ProviderProfile("gemini", _platform_command("gemini"), ("--acp",)),
}


def resolve_provider_profile(provider: str) -> ProviderProfile:
    try:
        return PROVIDER_PROFILES[provider]
    except KeyError as exc:
        raise ValueError(f"unsupported provider: {provider}") from exc


def load_persisted_provider(path: Path) -> str | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except Exception:
        return None

    if not isinstance(data, dict):
        return None

    provider = data.get("provider")
    return provider if provider in SUPPORTED_PROVIDERS else None


def write_persisted_provider(path: Path, provider: str) -> None:
    resolve_provider_profile(provider)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"provider": provider}), encoding="utf-8")


def resolve_startup_provider(configured_provider: str, state_path: Path) -> str:
    resolve_provider_profile(configured_provider)
    persisted = load_persisted_provider(state_path)
    return persisted or configured_provider
