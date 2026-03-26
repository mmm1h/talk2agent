import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from telegram.error import InvalidToken

from talk2agent.app import build_services
from talk2agent.acp.mcp_servers import build_workspace_mcp_servers
from talk2agent.config import (
    AgentConfig,
    AppConfig,
    McpServerConfig,
    NameValueConfig,
    PermissionsConfig,
    RuntimeConfig,
    TelegramConfig,
    WorkspaceConfig,
)
from talk2agent.session_store import RetiredSessionStoreError, SessionStore


def make_config(tmp_path: Path, provider: str = "gemini") -> AppConfig:
    return AppConfig(
        telegram=TelegramConfig(
            bot_token="YOUR_TELEGRAM_BOT_TOKEN",
            allowed_user_ids=[123],
            admin_user_id=123,
        ),
        agent=AgentConfig(
            provider=provider,
            workspace_dir=str(tmp_path),
            workspaces=[
                WorkspaceConfig(
                    id="default",
                    label="Default Workspace",
                    path=str(tmp_path),
                    mcp_servers=[],
                ),
                WorkspaceConfig(
                    id="alt",
                    label="Alt Workspace",
                    path=str(tmp_path / "alt"),
                    mcp_servers=[],
                ),
            ],
        ),
        permissions=PermissionsConfig(mode="auto_approve"),
        runtime=RuntimeConfig(
            idle_timeout_minutes=30,
            stream_edit_interval_ms=700,
            provider_state_path=str(tmp_path / "provider-state.json"),
            session_history_path=str(tmp_path / "session-history.json"),
        ),
    )


def test_build_services_prefers_persisted_provider(tmp_path: Path):
    config = make_config(tmp_path, provider="gemini")
    (tmp_path / "provider-state.json").write_text('{"provider": "codex"}', encoding="utf-8")

    services = build_services(config)
    state = asyncio.run(services.snapshot_runtime_state())

    assert state.provider == "codex"
    assert state.workspace_id == "default"


def test_build_services_falls_back_when_persisted_provider_is_invalid(tmp_path: Path):
    config = make_config(tmp_path, provider="gemini")
    (tmp_path / "provider-state.json").write_text('{"provider": "nope"}', encoding="utf-8")

    services = build_services(config)
    state = asyncio.run(services.snapshot_runtime_state())

    assert state.provider == "gemini"
    assert state.workspace_id == "default"


def test_build_services_prefers_persisted_workspace_when_present(tmp_path: Path):
    config = make_config(tmp_path, provider="gemini")
    (tmp_path / "provider-state.json").write_text(
        '{"provider": "codex", "workspace_id": "alt"}',
        encoding="utf-8",
    )

    services = build_services(config)
    state = asyncio.run(services.snapshot_runtime_state())

    assert state.provider == "codex"
    assert state.workspace_id == "alt"
    assert state.workspace_path == str(tmp_path / "alt")


def test_build_services_falls_back_when_persisted_workspace_is_invalid(tmp_path: Path):
    config = make_config(tmp_path, provider="gemini")
    (tmp_path / "provider-state.json").write_text(
        '{"provider": "codex", "workspace_id": "missing"}',
        encoding="utf-8",
    )

    services = build_services(config)
    state = asyncio.run(services.snapshot_runtime_state())

    assert state.provider == "codex"
    assert state.workspace_id == "default"


def test_switch_provider_rolls_back_when_persistence_fails(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("talk2agent.app._preflight_runtime", lambda *_args, **_kwargs: asyncio.sleep(0))
    services = build_services(make_config(tmp_path, provider="gemini"))
    original = asyncio.run(services.snapshot_runtime_state())

    def boom(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("talk2agent.app.write_persisted_runtime_selection", boom)

    async def scenario():
        try:
            await services.switch_provider("codex")
        except OSError:
            return await services.snapshot_runtime_state()
        raise AssertionError("switch_provider should have raised")

    state_after_failure = asyncio.run(scenario())

    assert state_after_failure.provider == original.provider


def test_failed_switch_does_not_leak_transient_runtime_state_to_snapshots(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("talk2agent.app._preflight_runtime", lambda *_args, **_kwargs: asyncio.sleep(0))
    services = build_services(make_config(tmp_path, provider="gemini"))
    pending_snapshots = []

    def boom(*_args, **_kwargs):
        loop = asyncio.get_running_loop()
        pending_snapshots.append(loop.create_task(services.snapshot_runtime_state()))
        raise OSError("disk full")

    monkeypatch.setattr("talk2agent.app.write_persisted_runtime_selection", boom)

    async def scenario():
        with pytest.raises(OSError):
            await services.switch_provider("codex")
        current_state = await services.snapshot_runtime_state()
        leaked_snapshot = await pending_snapshots[0]
        return current_state, leaked_snapshot

    current_state, leaked_snapshot = asyncio.run(scenario())

    assert current_state.provider == "gemini"
    assert leaked_snapshot.provider == "gemini"


def test_failed_switch_reactivates_old_store(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("talk2agent.app._preflight_runtime", lambda *_args, **_kwargs: asyncio.sleep(0))
    services = build_services(make_config(tmp_path, provider="gemini"))
    original = asyncio.run(services.snapshot_runtime_state())

    def boom(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("talk2agent.app.write_persisted_runtime_selection", boom)

    async def scenario():
        with pytest.raises(OSError):
            await services.switch_provider("codex")
        return await original.session_store.get_or_create(123)

    session = asyncio.run(scenario())

    assert session is not None


def test_switch_provider_installs_new_runtime_state_and_retires_old_store(
    tmp_path: Path, monkeypatch
):
    async def fake_preflight(*_args, **_kwargs):
        return None

    monkeypatch.setattr("talk2agent.app._preflight_runtime", fake_preflight)
    services = build_services(make_config(tmp_path, provider="gemini"))
    original = asyncio.run(services.snapshot_runtime_state())

    async def scenario():
        selected = await services.switch_provider("codex")
        state = await services.snapshot_runtime_state()
        try:
            await original.session_store.get_or_create(123)
        except RetiredSessionStoreError:
            retired = True
        else:
            retired = False
        return selected, state, retired

    selected, state, retired = asyncio.run(scenario())

    assert selected == "codex"
    assert state.provider == "codex"
    assert state.workspace_id == "default"
    assert state.session_store is not original.session_store
    assert retired is True


def test_new_snapshots_see_new_provider_while_old_close_all_is_still_running(tmp_path: Path, monkeypatch):
    close_started = asyncio.Event()
    allow_close = asyncio.Event()

    class BlockingStore(SessionStore):
        async def close_all(self):
            close_started.set()
            await allow_close.wait()

    monkeypatch.setattr("talk2agent.app.SessionStore", BlockingStore)
    monkeypatch.setattr("talk2agent.app._preflight_runtime", lambda *_args, **_kwargs: asyncio.sleep(0))
    services = build_services(make_config(tmp_path, provider="gemini"))

    async def scenario():
        switch_task = asyncio.create_task(services.switch_provider("codex"))
        await close_started.wait()
        state_during_cleanup = await services.snapshot_runtime_state()
        allow_close.set()
        await switch_task
        return state_during_cleanup

    state_during_cleanup = asyncio.run(scenario())

    assert state_during_cleanup.provider == "codex"


def test_switch_provider_runs_preflight_before_installing_new_runtime(tmp_path: Path, monkeypatch):
    services = build_services(make_config(tmp_path, provider="gemini"))
    calls = []

    async def fake_preflight(config, provider, workspace_dir):
        calls.append((config.agent.provider, provider, workspace_dir))

    monkeypatch.setattr("talk2agent.app._preflight_runtime", fake_preflight)

    asyncio.run(services.switch_provider("codex"))

    assert calls == [("gemini", "codex", str(tmp_path))]


def test_switch_provider_leaves_runtime_untouched_when_preflight_fails(tmp_path: Path, monkeypatch):
    services = build_services(make_config(tmp_path, provider="gemini"))

    async def boom(*_args, **_kwargs):
        raise FileNotFoundError("codex-acp")

    monkeypatch.setattr("talk2agent.app._preflight_runtime", boom)

    async def scenario():
        with pytest.raises(FileNotFoundError):
            await services.switch_provider("codex")
        return await services.snapshot_runtime_state()

    state = asyncio.run(scenario())

    assert state.provider == "gemini"
    assert state.workspace_id == "default"


def test_switch_workspace_installs_new_runtime_state(tmp_path: Path, monkeypatch):
    async def fake_preflight(*_args, **_kwargs):
        return None

    monkeypatch.setattr("talk2agent.app._preflight_runtime", fake_preflight)
    services = build_services(make_config(tmp_path, provider="gemini"))
    original = asyncio.run(services.snapshot_runtime_state())

    async def scenario():
        selected = await services.switch_workspace("alt")
        state = await services.snapshot_runtime_state()
        try:
            await original.session_store.get_or_create(123)
        except RetiredSessionStoreError:
            retired = True
        else:
            retired = False
        return selected, state, retired

    selected, state, retired = asyncio.run(scenario())

    assert selected == "alt"
    assert state.provider == "gemini"
    assert state.workspace_id == "alt"
    assert state.workspace_path == str(tmp_path / "alt")
    assert retired is True


def test_list_provider_sessions_returns_workspace_scoped_entries(tmp_path: Path, monkeypatch):
    services = build_services(make_config(tmp_path, provider="gemini"))
    captured = {}

    class FakeCatalogSession:
        async def list_sessions(self, cursor=None):
            captured["cursor"] = cursor
            return SimpleNamespace(
                sessions=[
                    SimpleNamespace(
                        sessionId="desktop-session",
                        title="Desktop Flow",
                        cwd=str(tmp_path / "src"),
                        updatedAt="2026-03-26T00:00:00+00:00",
                    ),
                    SimpleNamespace(
                        sessionId="outside-session",
                        title="Other Workspace",
                        cwd=str(tmp_path.parent),
                        updatedAt="2026-03-26T00:00:00+00:00",
                    ),
                ],
                nextCursor="cursor-2",
            )

        async def close(self):
            captured["closed"] = True

    monkeypatch.setattr(
        "talk2agent.app._build_agent_session",
        lambda *_args, **_kwargs: FakeCatalogSession(),
    )

    page = asyncio.run(services.list_provider_sessions(cursor="cursor-1"))

    assert captured == {"cursor": "cursor-1", "closed": True}
    assert page.supported is True
    assert page.next_cursor == "cursor-2"
    assert len(page.entries) == 1
    assert page.entries[0].session_id == "desktop-session"
    assert page.entries[0].cwd_label == "src"


def test_list_provider_sessions_reports_unsupported_when_provider_cannot_list(tmp_path: Path, monkeypatch):
    from talk2agent.acp.agent_session import SessionListingNotSupportedError

    services = build_services(make_config(tmp_path, provider="gemini"))

    class FakeCatalogSession:
        async def list_sessions(self, cursor=None):
            raise SessionListingNotSupportedError("listing disabled")

        async def close(self):
            return None

    monkeypatch.setattr(
        "talk2agent.app._build_agent_session",
        lambda *_args, **_kwargs: FakeCatalogSession(),
    )

    page = asyncio.run(services.list_provider_sessions())

    assert page.supported is False
    assert page.entries == ()
    assert page.next_cursor is None


def test_discover_provider_capabilities_returns_prompt_and_session_support(tmp_path: Path, monkeypatch):
    services = build_services(make_config(tmp_path, provider="gemini"))

    class FakeCapabilitySession:
        capabilities = SimpleNamespace(
            supports_image_prompt=True,
            supports_audio_prompt=False,
            supports_embedded_context_prompt=True,
            can_fork=True,
            can_list=True,
            can_resume=False,
        )

        async def ensure_started(self):
            return None

        async def close(self):
            return None

    monkeypatch.setattr(
        "talk2agent.app._build_agent_session",
        lambda *_args, **_kwargs: FakeCapabilitySession(),
    )

    summary = asyncio.run(services.discover_provider_capabilities("codex"))

    assert summary.provider == "codex"
    assert summary.available is True
    assert summary.supports_image_prompt is True
    assert summary.supports_audio_prompt is False
    assert summary.supports_embedded_context_prompt is True
    assert summary.can_fork_sessions is True
    assert summary.can_list_sessions is True
    assert summary.can_resume_sessions is False
    assert summary.error is None


def test_discover_provider_capabilities_reports_unavailable_provider(tmp_path: Path, monkeypatch):
    services = build_services(make_config(tmp_path, provider="gemini"))

    class BrokenCapabilitySession:
        async def ensure_started(self):
            raise FileNotFoundError("codex-acp")

        async def close(self):
            return None

    monkeypatch.setattr(
        "talk2agent.app._build_agent_session",
        lambda *_args, **_kwargs: BrokenCapabilitySession(),
    )

    summary = asyncio.run(services.discover_provider_capabilities("codex"))

    assert summary.provider == "codex"
    assert summary.available is False
    assert summary.error == "command missing"


def test_build_agent_session_attaches_workspace_mcp_servers(tmp_path: Path):
    from talk2agent.app import _build_agent_session

    config = make_config(tmp_path, provider="codex")
    config.agent.workspaces[0].mcp_servers = [
        McpServerConfig(
            name="local-docs",
            transport="stdio",
            command="uvx",
            args=["docs-mcp"],
            env=[NameValueConfig(name="API_KEY", value="secret")],
        ),
        McpServerConfig(
            name="remote-search",
            transport="http",
            url="https://example.com/mcp",
            headers=[NameValueConfig(name="Authorization", value="Bearer token")],
        ),
    ]

    session = _build_agent_session(config, "codex", str(tmp_path))

    assert len(session.mcp_servers) == 2
    assert session.mcp_servers[0].name == "local-docs"
    assert session.mcp_servers[0].command == "uvx"
    assert session.mcp_servers[0].args == ["docs-mcp"]
    assert [(item.name, item.value) for item in session.mcp_servers[0].env] == [
        ("API_KEY", "secret")
    ]
    assert session.mcp_servers[1].name == "remote-search"
    assert session.mcp_servers[1].url == "https://example.com/mcp"
    assert [(item.name, item.value) for item in session.mcp_servers[1].headers] == [
        ("Authorization", "Bearer token")
    ]


def test_build_workspace_mcp_servers_supports_sse_headers(tmp_path: Path):
    workspace = WorkspaceConfig(
        id="default",
        label="Default Workspace",
        path=str(tmp_path),
        mcp_servers=[
            McpServerConfig(
                name="events",
                transport="sse",
                url="https://example.com/sse",
                headers=[NameValueConfig(name="X-Workspace", value="repo-a")],
            )
        ],
    )

    servers = build_workspace_mcp_servers(workspace)

    assert len(servers) == 1
    assert servers[0].name == "events"
    assert servers[0].url == "https://example.com/sse"
    assert [(item.name, item.value) for item in servers[0].headers] == [
        ("X-Workspace", "repo-a")
    ]


def test_run_app_closes_active_store_on_shutdown(monkeypatch):
    import talk2agent.app as app

    calls = []

    class FakeStore:
        async def close_all(self):
            calls.append("close_all")

    services = SimpleNamespace(
        snapshot_runtime_state=lambda: asyncio.sleep(
            0, result=SimpleNamespace(session_store=FakeStore())
        )
    )

    monkeypatch.setattr(app, "build_services", lambda _config: services)
    monkeypatch.setattr(
        app,
        "build_telegram_application",
        lambda _config, _services: SimpleNamespace(run_polling=lambda: None),
    )

    assert app.run_app(object()) == 0
    assert calls == ["close_all"]


def test_run_app_swallows_invalid_placeholder_token(monkeypatch):
    import talk2agent.app as app

    services = SimpleNamespace(
        snapshot_runtime_state=lambda: asyncio.sleep(
            0,
            result=SimpleNamespace(
                session_store=SimpleNamespace(close_all=lambda: asyncio.sleep(0))
            ),
        )
    )

    class FakeApplication:
        def run_polling(self):
            raise InvalidToken("bad token")

    config = SimpleNamespace(telegram=SimpleNamespace(bot_token="YOUR_TELEGRAM_BOT_TOKEN"))

    monkeypatch.setattr(app, "build_services", lambda _config: services)
    monkeypatch.setattr(app, "build_telegram_application", lambda _config, _services: FakeApplication())

    assert app.run_app(config) == 0
