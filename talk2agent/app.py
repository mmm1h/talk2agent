from __future__ import annotations

import asyncio
import shutil
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from telegram.error import InvalidToken

from talk2agent.acp.agent_session import AgentSession, SessionListingNotSupportedError
from talk2agent.bots.telegram_bot import build_telegram_application
from talk2agent.config import AppConfig
from talk2agent.provider_runtime import (
    RuntimeState,
    resolve_provider_profile,
    resolve_startup_runtime_selection,
    write_persisted_runtime_selection,
)
from talk2agent.session_history import SessionHistoryStore
from talk2agent.session_store import SessionStore


@dataclass(frozen=True, slots=True)
class ProviderSessionEntry:
    session_id: str
    title: str
    cwd: str
    cwd_label: str
    updated_at: str | None


@dataclass(frozen=True, slots=True)
class ProviderSessionPage:
    entries: tuple[ProviderSessionEntry, ...]
    next_cursor: str | None
    supported: bool = True


@dataclass(frozen=True, slots=True)
class ProviderCapabilitySummary:
    provider: str
    available: bool
    supports_image_prompt: bool = False
    supports_audio_prompt: bool = False
    supports_embedded_context_prompt: bool = False
    can_list_sessions: bool = False
    can_resume_sessions: bool = False
    error: str | None = None


@dataclass(slots=True)
class AppServices:
    config: AppConfig
    allowed_user_ids: set[int]
    admin_user_id: int
    history_store: SessionHistoryStore
    _runtime_state: RuntimeState
    _state_lock: asyncio.Lock
    _telegram_command_menu_updater: Callable[[], Awaitable[None]] | None = None

    async def snapshot_runtime_state(self) -> RuntimeState:
        async with self._state_lock:
            return self._runtime_state

    async def bind_telegram_command_menu_updater(
        self,
        updater: Callable[[], Awaitable[None]],
    ) -> None:
        async with self._state_lock:
            self._telegram_command_menu_updater = updater

    async def refresh_telegram_command_menu(self) -> None:
        async with self._state_lock:
            updater = self._telegram_command_menu_updater
        if updater is None:
            return
        try:
            await updater()
        except Exception:
            pass

    async def discover_agent_commands(self, timeout_seconds: float = 2.0):
        state = await self.snapshot_runtime_state()
        session = _build_agent_session(
            self.config,
            state.provider,
            state.workspace_path,
        )
        try:
            await session.ensure_started()
            return await session.wait_for_available_commands(timeout_seconds)
        except Exception:
            return ()
        finally:
            try:
                await session.close()
            except Exception:
                pass

    async def list_provider_sessions(self, cursor: str | None = None) -> ProviderSessionPage:
        state = await self.snapshot_runtime_state()
        session = _build_agent_session(
            self.config,
            state.provider,
            state.workspace_path,
        )
        try:
            response = await session.list_sessions(cursor=cursor)
        except SessionListingNotSupportedError:
            return ProviderSessionPage(entries=(), next_cursor=None, supported=False)
        finally:
            try:
                await session.close()
            except Exception:
                pass

        entries: list[ProviderSessionEntry] = []
        for raw_session in getattr(response, "sessions", ()):
            entry = _coerce_provider_session_entry(raw_session, workspace_dir=state.workspace_path)
            if entry is not None:
                entries.append(entry)
        return ProviderSessionPage(
            entries=tuple(entries),
            next_cursor=_coerce_optional_text(
                getattr(response, "next_cursor", getattr(response, "nextCursor", None))
            ),
        )

    async def discover_provider_capabilities(
        self,
        provider: str,
        *,
        workspace_id: str | None = None,
    ) -> ProviderCapabilitySummary:
        state = await self.snapshot_runtime_state()
        workspace_path = state.workspace_path
        if workspace_id is not None:
            workspace_path = self.config.agent.resolve_workspace(workspace_id).path

        session = _build_agent_session(
            self.config,
            provider,
            workspace_path,
        )
        try:
            await session.ensure_started()
            capabilities = session.capabilities
            return ProviderCapabilitySummary(
                provider=provider,
                available=True,
                supports_image_prompt=capabilities.supports_image_prompt,
                supports_audio_prompt=capabilities.supports_audio_prompt,
                supports_embedded_context_prompt=capabilities.supports_embedded_context_prompt,
                can_list_sessions=capabilities.can_list,
                can_resume_sessions=capabilities.can_resume,
            )
        except Exception as exc:
            return ProviderCapabilitySummary(
                provider=provider,
                available=False,
                error=_describe_provider_discovery_error(exc),
            )
        finally:
            try:
                await session.close()
            except Exception:
                pass

    async def switch_provider(self, provider: str) -> str:
        profile = resolve_provider_profile(provider)
        current_state = await self.snapshot_runtime_state()
        if current_state.provider == profile.provider:
            return profile.provider
        await self._switch_runtime(
            provider=profile.provider,
            workspace_id=current_state.workspace_id,
        )
        return profile.provider

    async def switch_workspace(self, workspace_id: str) -> str:
        workspace = self.config.agent.resolve_workspace(workspace_id)
        current_state = await self.snapshot_runtime_state()
        if current_state.workspace_id == workspace.id:
            return workspace.id
        await self._switch_runtime(
            provider=current_state.provider,
            workspace_id=workspace.id,
        )
        return workspace.id

    async def _switch_runtime(self, *, provider: str, workspace_id: str) -> None:
        workspace = self.config.agent.resolve_workspace(workspace_id)
        await _preflight_runtime(self.config, provider, workspace.path)
        new_store = _build_session_store(
            self.config,
            provider,
            workspace.path,
            self.history_store,
        )
        new_state = RuntimeState(
            provider=provider,
            workspace_id=workspace.id,
            workspace_path=workspace.path,
            session_store=new_store,
        )
        state_path = Path(self.config.runtime.provider_state_path)
        store_to_close = None
        store_to_discard = None
        error = None

        async with self._state_lock:
            old_state = self._runtime_state
            await old_state.session_store.retire()
            self._runtime_state = new_state
            try:
                write_persisted_runtime_selection(
                    state_path,
                    provider,
                    workspace.id,
                )
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
        await self.refresh_telegram_command_menu()


def _build_agent_session(config: AppConfig, provider: str, workspace_dir: str) -> AgentSession:
    profile = resolve_provider_profile(provider)
    return AgentSession(
        command=profile.command,
        args=profile.args,
        cwd=workspace_dir,
    )


async def _preflight_runtime(config: AppConfig, provider: str, workspace_dir: str) -> None:
    profile = resolve_provider_profile(provider)
    if shutil.which(profile.command) is None:
        raise FileNotFoundError(profile.command)
    session = _build_agent_session(config, provider, workspace_dir)
    try:
        await session.ensure_started()
    finally:
        try:
            await session.close()
        except Exception:
            pass


def _coerce_provider_session_entry(raw_session, *, workspace_dir: str) -> ProviderSessionEntry | None:
    session = getattr(raw_session, "root", raw_session)
    session_id = _coerce_optional_text(
        getattr(session, "session_id", getattr(session, "sessionId", None))
    )
    cwd = _coerce_optional_text(getattr(session, "cwd", None))
    if session_id is None or cwd is None:
        return None
    cwd_label = _workspace_relative_cwd_label(workspace_dir, cwd)
    if cwd_label is None:
        return None
    return ProviderSessionEntry(
        session_id=session_id,
        title=_coerce_optional_text(getattr(session, "title", None)) or "",
        cwd=cwd,
        cwd_label=cwd_label,
        updated_at=_coerce_optional_text(
            getattr(session, "updated_at", getattr(session, "updatedAt", None))
        ),
    )


def _coerce_optional_text(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _describe_provider_discovery_error(exc: Exception) -> str:
    if isinstance(exc, FileNotFoundError):
        return "command missing"
    return "session creation failed"


def _workspace_relative_cwd_label(workspace_dir: str, candidate_dir: str) -> str | None:
    try:
        root = Path(workspace_dir).resolve()
        candidate = Path(candidate_dir).resolve()
        relative = candidate.relative_to(root)
    except Exception:
        return None
    return "." if str(relative) == "." else relative.as_posix()


def _build_session_store(
    config: AppConfig,
    provider: str,
    workspace_dir: str,
    history_store: SessionHistoryStore,
) -> SessionStore:
    def session_factory(_user_id: int) -> AgentSession:
        return _build_agent_session(config, provider, workspace_dir)

    return SessionStore(
        session_factory=session_factory,
        idle_timeout_minutes=config.runtime.idle_timeout_minutes,
        provider=provider,
        workspace_dir=workspace_dir,
        history_store=history_store,
    )


def build_services(config: AppConfig) -> AppServices:
    history_store = SessionHistoryStore(Path(config.runtime.session_history_path))
    selection = resolve_startup_runtime_selection(
        config.agent.provider,
        config.agent.default_workspace.id,
        Path(config.runtime.provider_state_path),
    )
    try:
        workspace = config.agent.resolve_workspace(selection.workspace_id)
    except ValueError:
        workspace = config.agent.default_workspace
    runtime_state = RuntimeState(
        provider=selection.provider,
        workspace_id=workspace.id,
        workspace_path=workspace.path,
        session_store=_build_session_store(
            config,
            selection.provider,
            workspace.path,
            history_store,
        ),
    )

    return AppServices(
        config=config,
        allowed_user_ids=set(config.telegram.allowed_user_ids),
        admin_user_id=config.telegram.admin_user_id,
        history_store=history_store,
        _runtime_state=runtime_state,
        _state_lock=asyncio.Lock(),
        _telegram_command_menu_updater=None,
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
