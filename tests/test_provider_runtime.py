import sys
from pathlib import Path

import pytest

from talk2agent.provider_runtime import (
    DEFAULT_PROVIDER,
    RuntimeState,
    load_persisted_provider,
    resolve_provider_profile,
    resolve_startup_provider,
    write_persisted_provider,
)


def test_default_provider_is_gemini():
    assert DEFAULT_PROVIDER == "gemini"


@pytest.mark.parametrize(
    ("provider", "base_command", "args"),
    [
        ("claude", "claude-agent-acp", ()),
        ("codex", "codex-acp", ()),
        ("gemini", "gemini", ("--acp",)),
    ],
)
def test_resolve_provider_profile(provider, base_command, args):
    profile = resolve_provider_profile(provider)
    expected_command = f"{base_command}.cmd" if sys.platform == "win32" else base_command
    assert profile.command == expected_command
    assert profile.args == args


def test_write_and_load_persisted_provider_round_trip(tmp_path: Path):
    path = tmp_path / "provider-state.json"
    write_persisted_provider(path, "codex")
    assert load_persisted_provider(path) == "codex"


@pytest.mark.parametrize("payload", ["[]", '"gemini"'])
def test_load_persisted_provider_ignores_valid_json_without_object(tmp_path: Path, payload: str):
    path = tmp_path / "provider-state.json"
    path.write_text(payload, encoding="utf-8")
    assert load_persisted_provider(path) is None


def test_resolve_startup_provider_falls_back_when_state_missing(tmp_path: Path):
    path = tmp_path / "provider-state.json"
    assert resolve_startup_provider("codex", path) == "codex"


def test_resolve_startup_provider_rejects_invalid_configured_provider(tmp_path: Path):
    path = tmp_path / "provider-state.json"
    with pytest.raises(ValueError):
        resolve_startup_provider("invalid", path)


def test_resolve_startup_provider_prefers_persisted_value(tmp_path: Path):
    path = tmp_path / "provider-state.json"
    write_persisted_provider(path, "codex")
    assert resolve_startup_provider("gemini", path) == "codex"
