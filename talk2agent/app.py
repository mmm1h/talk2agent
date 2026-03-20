from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

from telegram.error import InvalidToken

from talk2agent.acp.agent_session import AgentSession
from talk2agent.bots.telegram_bot import build_telegram_application
from talk2agent.config import AppConfig
from talk2agent.provider_runtime import (
    RuntimeState,
    resolve_provider_profile,
    resolve_startup_provider,
    write_persisted_provider,
)
from talk2agent.session_store import SessionStore


@dataclass(slots=True)
class AppServices:
    config: AppConfig
    allowed_user_ids: set[int]
    admin_user_id: int
    _runtime_state: RuntimeState
    _state_lock: asyncio.Lock

    async def snapshot_runtime_state(self) -> RuntimeState:
        async with self._state_lock:
            return self._runtime_state

    async def switch_provider(self, provider: str) -> str:
        profile = resolve_provider_profile(provider)
        new_store = _build_session_store(self.config, profile.provider)
        state_path = Path(self.config.runtime.provider_state_path)
        store_to_close = None
        store_to_discard = None
        error = None

        async with self._state_lock:
            old_state = self._runtime_state
            await old_state.session_store.retire()
            self._runtime_state = RuntimeState(provider=profile.provider, session_store=new_store)
            try:
                write_persisted_provider(state_path, profile.provider)
            except Exception as exc:
                self._runtime_state = old_state
                await old_state.session_store.activate()
                store_to_discard = new_store
                error = exc
            else:
                store_to_close = old_state.session_store

        if store_to_discard is not None:
            try:
                await store_to_discard.close_all()
            except Exception:
                pass
        if error is not None:
            raise error
        if store_to_close is not None:
            try:
                await store_to_close.close_all()
            except Exception:
                pass
        return profile.provider


def _build_session_store(config: AppConfig, provider: str) -> SessionStore:
    profile = resolve_provider_profile(provider)

    def session_factory(_user_id: int) -> AgentSession:
        return AgentSession(
            command=profile.command,
            args=profile.args,
            cwd=config.agent.workspace_dir,
        )

    return SessionStore(
        session_factory=session_factory,
        idle_timeout_minutes=config.runtime.idle_timeout_minutes,
    )


def build_services(config: AppConfig) -> AppServices:
    provider = resolve_startup_provider(
        config.agent.provider,
        Path(config.runtime.provider_state_path),
    )
    runtime_state = RuntimeState(
        provider=provider,
        session_store=_build_session_store(config, provider),
    )

    return AppServices(
        config=config,
        allowed_user_ids=set(config.telegram.allowed_user_ids),
        admin_user_id=config.telegram.admin_user_id,
        _runtime_state=runtime_state,
        _state_lock=asyncio.Lock(),
    )


def run_app(config: AppConfig) -> int:
    services = build_services(config)
    application = build_telegram_application(config, services)
    try:
        try:
            application.run_polling()
        except InvalidToken:
            if config.telegram.bot_token != "YOUR_TELEGRAM_BOT_TOKEN":
                raise
    finally:
        runtime_state = asyncio.run(services.snapshot_runtime_state())
        asyncio.run(runtime_state.session_store.close_all())
    return 0
