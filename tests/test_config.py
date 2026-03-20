from pathlib import Path

import pytest
import yaml

from talk2agent.provider_runtime import resolve_provider_profile
from talk2agent.config import load_config, write_default_config


def test_write_default_config_creates_multi_provider_template(tmp_path: Path):
    path = tmp_path / "config.yaml"
    write_default_config(path)
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert data["telegram"]["admin_user_id"] == 123456789
    assert data["agent"]["provider"] == "gemini"
    assert "command" not in data["agent"]
    assert "args" not in data["agent"]
    assert data["runtime"]["provider_state_path"] == ".talk2agent-provider-state.json"


@pytest.mark.parametrize("provider", ["claude", "codex", "gemini"])
def test_load_config_accepts_supported_providers(tmp_path: Path, provider: str):
    path = tmp_path / "config.yaml"
    path.write_text(
        f"""
telegram:
  bot_token: "x"
  allowed_user_ids: [123]
  admin_user_id: 123
agent:
  provider: "{provider}"
  workspace_dir: "."
permissions:
  mode: "auto_approve"
runtime:
  idle_timeout_minutes: 30
  stream_edit_interval_ms: 700
  provider_state_path: ".provider-state.json"
""".strip(),
        encoding="utf-8",
    )
    assert load_config(path).agent.provider == provider


def test_load_config_ignores_legacy_command_and_args(tmp_path: Path):
    path = tmp_path / "config.yaml"
    path.write_text(
        """
telegram:
  bot_token: "x"
  allowed_user_ids: [123]
  admin_user_id: 123
agent:
  provider: "codex"
  workspace_dir: "."
  command: "node"
  args: "not-a-list-anymore"
permissions:
  mode: "auto_approve"
runtime:
  idle_timeout_minutes: 30
  stream_edit_interval_ms: 700
  provider_state_path: ".provider-state.json"
""".strip(),
        encoding="utf-8",
    )
    config = load_config(path)
    profile = resolve_provider_profile(config.agent.provider)
    assert config.agent.provider == "codex"
    assert config.agent.command == profile.command
    assert config.agent.args == list(profile.args)
    assert config.agent.command != "node"
    assert config.agent.args != ["not-a-list-anymore"]


def test_load_config_parses_runtime_provider_state_path(tmp_path: Path):
    path = tmp_path / "config.yaml"
    path.write_text(
        """
telegram:
  bot_token: "x"
  allowed_user_ids: [123]
  admin_user_id: 123
agent:
  provider: "gemini"
  workspace_dir: "."
permissions:
  mode: "auto_approve"
runtime:
  idle_timeout_minutes: 30
  stream_edit_interval_ms: 700
  provider_state_path: ".provider-state.json"
""".strip(),
        encoding="utf-8",
    )
    assert load_config(path).runtime.provider_state_path == ".provider-state.json"


@pytest.mark.parametrize("provider", ["old-provider", "anthropic", "openai"])
def test_load_config_rejects_unsupported_provider(tmp_path: Path, provider: str):
    path = tmp_path / "config.yaml"
    path.write_text(
        f"""
telegram:
  bot_token: "x"
  allowed_user_ids: [123]
  admin_user_id: 123
agent:
  provider: "{provider}"
  workspace_dir: "."
permissions:
  mode: "auto_approve"
runtime:
  idle_timeout_minutes: 30
  stream_edit_interval_ms: 700
  provider_state_path: ".provider-state.json"
""".strip(),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="agent.provider"):
        load_config(path)


def test_load_config_requires_admin_in_allowed_user_ids(tmp_path: Path):
    path = tmp_path / "config.yaml"
    path.write_text(
        """
telegram:
  bot_token: "x"
  allowed_user_ids: [123]
  admin_user_id: 999
agent:
  provider: "gemini"
  workspace_dir: "."
permissions:
  mode: "auto_approve"
runtime:
  idle_timeout_minutes: 30
  stream_edit_interval_ms: 700
  provider_state_path: ".provider-state.json"
""".strip(),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="admin_user_id"):
        load_config(path)
