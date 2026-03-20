import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from telegram.error import InvalidToken

from talk2agent.app import build_services
from talk2agent.config import (
    AgentConfig,
    AppConfig,
    PermissionsConfig,
    RuntimeConfig,
    TelegramConfig,
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
        ),
        permissions=PermissionsConfig(mode="auto_approve"),
        runtime=RuntimeConfig(
            idle_timeout_minutes=30,
            stream_edit_interval_ms=700,
            provider_state_path=str(tmp_path / "provider-state.json"),
        ),
    )


def test_build_services_prefers_persisted_provider(tmp_path: Path):
    config = make_config(tmp_path, provider="gemini")
    (tmp_path / "provider-state.json").write_text('{"provider": "codex"}', encoding="utf-8")

    services = build_services(config)
    state = asyncio.run(services.snapshot_runtime_state())

    assert state.provider == "codex"


def test_build_services_falls_back_when_persisted_provider_is_invalid(tmp_path: Path):
    config = make_config(tmp_path, provider="gemini")
    (tmp_path / "provider-state.json").write_text('{"provider": "nope"}', encoding="utf-8")

    services = build_services(config)
    state = asyncio.run(services.snapshot_runtime_state())

    assert state.provider == "gemini"


def test_switch_provider_rolls_back_when_persistence_fails(tmp_path: Path, monkeypatch):
    services = build_services(make_config(tmp_path, provider="gemini"))
    original = asyncio.run(services.snapshot_runtime_state())

    def boom(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("talk2agent.app.write_persisted_provider", boom)

    async def scenario():
        try:
            await services.switch_provider("codex")
        except OSError:
            return await services.snapshot_runtime_state()
        raise AssertionError("switch_provider should have raised")

    state_after_failure = asyncio.run(scenario())

    assert state_after_failure.provider == original.provider


def test_failed_switch_does_not_leak_transient_runtime_state_to_snapshots(tmp_path: Path, monkeypatch):
    services = build_services(make_config(tmp_path, provider="gemini"))
    pending_snapshots = []

    def boom(*_args, **_kwargs):
        loop = asyncio.get_running_loop()
        pending_snapshots.append(loop.create_task(services.snapshot_runtime_state()))
        raise OSError("disk full")

    monkeypatch.setattr("talk2agent.app.write_persisted_provider", boom)

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
    services = build_services(make_config(tmp_path, provider="gemini"))
    original = asyncio.run(services.snapshot_runtime_state())

    def boom(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("talk2agent.app.write_persisted_provider", boom)

    async def scenario():
        with pytest.raises(OSError):
            await services.switch_provider("codex")
        return await original.session_store.get_or_create(123)

    session = asyncio.run(scenario())

    assert session is not None


def test_switch_provider_installs_new_runtime_state_and_retires_old_store(tmp_path: Path):
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
