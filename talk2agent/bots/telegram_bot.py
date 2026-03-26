from __future__ import annotations

import asyncio
import base64
import mimetypes
import re
import secrets
import time
from urllib.parse import quote, urlparse
from dataclasses import dataclass
from functools import partial
from types import SimpleNamespace
from typing import Any

from telegram import (
    BotCommand,
    BotCommandScopeChat,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from talk2agent.acp.agent_session import (
    PromptAudio,
    PromptBlobResource,
    PromptImage,
    PromptText,
    PromptTextResource,
    UnsupportedPromptContentError,
)
from talk2agent.bots.telegram_stream import TelegramTurnStream
from talk2agent.provider_runtime import iter_provider_profiles, resolve_provider_profile
from talk2agent.session_store import RetiredSessionStoreError
from talk2agent.workspace_inbox import save_workspace_inbox_file
from talk2agent.workspace_git import read_workspace_git_diff_preview, read_workspace_git_status
from talk2agent.workspace_files import (
    list_workspace_entries,
    read_workspace_file_preview,
    search_workspace_text,
)


BUTTON_NEW_SESSION = "New Session"
BUTTON_BOT_STATUS = "Bot Status"
BUTTON_RETRY_LAST_TURN = "Retry Last Turn"
BUTTON_FORK_LAST_TURN = "Fork Last Turn"
BUTTON_SESSION_HISTORY = "Session History"
BUTTON_AGENT_COMMANDS = "Agent Commands"
BUTTON_MODEL_MODE = "Model / Mode"
BUTTON_WORKSPACE_FILES = "Workspace Files"
BUTTON_WORKSPACE_SEARCH = "Workspace Search"
BUTTON_WORKSPACE_CHANGES = "Workspace Changes"
BUTTON_CONTEXT_BUNDLE = "Context Bundle"
BUTTON_RESTART_AGENT = "Restart Agent"
BUTTON_SWITCH_AGENT = "Switch Agent"
BUTTON_SWITCH_WORKSPACE = "Switch Workspace"

CALLBACK_PREFIX = "menu:"
HISTORY_PAGE_SIZE = 5
COMMAND_PAGE_SIZE = 6
WORKSPACE_PAGE_SIZE = 8
WORKSPACE_SEARCH_PAGE_SIZE = 5
WORKSPACE_CHANGES_PAGE_SIZE = 6
CONTEXT_BUNDLE_PAGE_SIZE = 6
COMMAND_DISCOVERY_TIMEOUT_SECONDS = 2.0
CALLBACK_OPERATION_TIMEOUT_SECONDS = 15.0
DEBUG_STATUS_COMMAND = "debug_status"
_RESERVED_COMMAND_ALIASES = {DEBUG_STATUS_COMMAND}
ATTACHMENT_MAX_BYTES = 8 * 1024 * 1024
MEDIA_GROUP_SETTLE_SECONDS = 0.4
INLINE_TEXT_DOCUMENT_CHAR_LIMIT = 12000
STATUS_TEXT_SNIPPET_LIMIT = 80
STATUS_BUNDLE_PREVIEW_LIMIT = 3
STATUS_COMMAND_PREVIEW_LIMIT = 3
STATUS_WORKSPACE_CHANGE_PREVIEW_LIMIT = 3
STATUS_COMMAND_BUTTONS_PER_ROW = 2
STATUS_SELECTION_QUICK_LIMIT = 2
STATUS_SELECTION_BUTTONS_PER_ROW = 2
STATUS_RECENT_SESSION_PREVIEW_LIMIT = 2
_TEXT_DOCUMENT_SUFFIXES = {
    ".c",
    ".cfg",
    ".cpp",
    ".css",
    ".csv",
    ".env",
    ".go",
    ".h",
    ".hpp",
    ".html",
    ".ini",
    ".java",
    ".js",
    ".json",
    ".jsx",
    ".kt",
    ".md",
    ".py",
    ".ps1",
    ".rs",
    ".scss",
    ".sh",
    ".sql",
    ".svg",
    ".swift",
    ".toml",
    ".ts",
    ".tsx",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
}


@dataclass(slots=True)
class _CallbackAction:
    user_id: int
    action: str
    payload: dict[str, Any]
    expires_at: float


@dataclass(slots=True)
class _PendingTextAction:
    user_id: int
    action: str
    payload: dict[str, Any]
    expires_at: float


@dataclass(frozen=True, slots=True)
class _HistoryViewState:
    entries: list[Any]
    active_session_id: str | None


@dataclass(frozen=True, slots=True)
class _ProviderSessionsViewState:
    entries: tuple[Any, ...]
    next_cursor: str | None
    supported: bool
    active_session_id: str | None


@dataclass(frozen=True, slots=True)
class _CommandCenterState:
    commands: tuple[Any, ...]
    session_id: str | None


@dataclass(frozen=True, slots=True)
class _AttachmentPrompt:
    prompt_items: tuple[
        PromptText | PromptImage | PromptAudio | PromptTextResource | PromptBlobResource,
        ...,
    ]
    title_hint: str


@dataclass(frozen=True, slots=True)
class _AttachmentContent:
    prompt_items: tuple[PromptImage | PromptAudio | PromptTextResource | PromptBlobResource, ...]
    fallback_text: str
    title_hint: str


@dataclass(frozen=True, slots=True)
class _ContextBundleItem:
    kind: str
    relative_path: str
    status_code: str | None = None


@dataclass(frozen=True, slots=True)
class _AttachmentPromptCoercion:
    prompt_items: tuple[
        PromptText | PromptImage | PromptAudio | PromptTextResource | PromptBlobResource,
        ...,
    ]
    saved_context_items: tuple[_ContextBundleItem, ...]


@dataclass(frozen=True, slots=True)
class _ReplayTurn:
    provider: str
    workspace_id: str
    prompt_items: tuple[
        PromptText | PromptImage | PromptAudio | PromptTextResource | PromptBlobResource,
        ...,
    ]
    title_hint: str
    saved_context_items: tuple[_ContextBundleItem, ...] = ()


@dataclass(slots=True)
class _ContextBundle:
    provider: str
    workspace_id: str
    items: list[_ContextBundleItem]


@dataclass(slots=True)
class _ActiveContextBundleChat:
    provider: str
    workspace_id: str


@dataclass(frozen=True, slots=True)
class _LastRequestText:
    workspace_id: str
    text: str


@dataclass(slots=True)
class _MediaGroupBuffer:
    messages: list[Any]
    task: asyncio.Task | None = None


class AttachmentPromptError(ValueError):
    pass


class TelegramUiState:
    def __init__(
        self,
        *,
        ttl_seconds: float = 600.0,
        media_group_settle_seconds: float = MEDIA_GROUP_SETTLE_SECONDS,
        clock=None,
    ):
        self._ttl_seconds = ttl_seconds
        self.media_group_settle_seconds = media_group_settle_seconds
        self._clock = time.monotonic if clock is None else clock
        self._actions: dict[str, _CallbackAction] = {}
        self._pending_text_actions: dict[int, _PendingTextAction] = {}
        self._agent_command_aliases: dict[int, dict[str, str]] = {}
        self._context_bundles: dict[int, _ContextBundle] = {}
        self._active_context_bundle_chats: dict[int, _ActiveContextBundleChat] = {}
        self._last_turns: dict[int, _ReplayTurn] = {}
        self._last_request_texts: dict[int, _LastRequestText] = {}
        self._media_groups: dict[tuple[int, str], _MediaGroupBuffer] = {}

    def create(self, user_id: int, action: str, **payload: Any) -> str:
        self._prune()
        token = secrets.token_hex(4)
        self._actions[token] = _CallbackAction(
            user_id=user_id,
            action=action,
            payload=payload,
            expires_at=self._clock() + self._ttl_seconds,
        )
        return token

    def get(self, token: str) -> _CallbackAction | None:
        self._prune()
        return self._actions.get(token)

    def pop(self, token: str) -> _CallbackAction | None:
        self._prune()
        return self._actions.pop(token, None)

    def set_agent_command_aliases(self, user_id: int, aliases: dict[str, str]) -> None:
        self._agent_command_aliases[user_id] = dict(aliases)

    def resolve_agent_command(self, user_id: int, alias: str) -> str | None:
        return self._agent_command_aliases.get(user_id, {}).get(alias)

    def set_pending_text_action(self, user_id: int, action: str, **payload: Any) -> None:
        self._prune()
        self._pending_text_actions[user_id] = _PendingTextAction(
            user_id=user_id,
            action=action,
            payload=payload,
            expires_at=self._clock() + self._ttl_seconds,
        )

    def get_pending_text_action(self, user_id: int) -> _PendingTextAction | None:
        self._prune()
        return self._pending_text_actions.get(user_id)

    def clear_pending_text_action(self, user_id: int) -> _PendingTextAction | None:
        self._prune()
        return self._pending_text_actions.pop(user_id, None)

    def set_last_turn(
        self,
        user_id: int,
        replay_turn: _ReplayTurn,
    ) -> None:
        self._last_turns[user_id] = replay_turn

    def get_last_turn(
        self,
        user_id: int,
        provider: str,
        workspace_id: str,
    ) -> _ReplayTurn | None:
        replay_turn = self._last_turns.get(user_id)
        if replay_turn is None:
            return None
        if replay_turn.workspace_id != workspace_id:
            self._last_turns.pop(user_id, None)
            return None
        return replay_turn

    def set_last_request_text(
        self,
        user_id: int,
        workspace_id: str,
        text: str,
    ) -> None:
        normalized_text = text.strip()
        if not normalized_text:
            self._last_request_texts.pop(user_id, None)
            return
        self._last_request_texts[user_id] = _LastRequestText(
            workspace_id=workspace_id,
            text=normalized_text,
        )

    def get_last_request_text(
        self,
        user_id: int,
        workspace_id: str,
    ) -> str | None:
        last_request_text = self._last_request_texts.get(user_id)
        if last_request_text is None:
            return None
        if last_request_text.workspace_id != workspace_id:
            self._last_request_texts.pop(user_id, None)
            return None
        return last_request_text.text

    def get_context_bundle(
        self,
        user_id: int,
        provider: str,
        workspace_id: str,
    ) -> _ContextBundle | None:
        bundle = self._context_bundles.get(user_id)
        if bundle is None:
            return None
        if bundle.provider != provider or bundle.workspace_id != workspace_id:
            self._context_bundles.pop(user_id, None)
            self._active_context_bundle_chats.pop(user_id, None)
            return None
        return bundle

    def add_context_item(
        self,
        user_id: int,
        provider: str,
        workspace_id: str,
        item: _ContextBundleItem,
    ) -> tuple[_ContextBundle, bool]:
        bundle = self.get_context_bundle(user_id, provider, workspace_id)
        if bundle is None:
            bundle = _ContextBundle(
                provider=provider,
                workspace_id=workspace_id,
                items=[],
            )
            self._context_bundles[user_id] = bundle

        if item in bundle.items:
            return bundle, False

        bundle.items.append(item)
        return bundle, True

    def remove_context_item(
        self,
        user_id: int,
        provider: str,
        workspace_id: str,
        item_index: int,
    ) -> _ContextBundle | None:
        bundle = self.get_context_bundle(user_id, provider, workspace_id)
        if bundle is None:
            return None
        if item_index < 0 or item_index >= len(bundle.items):
            raise IndexError(item_index)
        bundle.items.pop(item_index)
        if not bundle.items:
            self._context_bundles.pop(user_id, None)
            self._active_context_bundle_chats.pop(user_id, None)
            return None
        return bundle

    def remove_context_item_by_value(
        self,
        user_id: int,
        provider: str,
        workspace_id: str,
        item: _ContextBundleItem,
    ) -> _ContextBundle | None:
        bundle = self.get_context_bundle(user_id, provider, workspace_id)
        if bundle is None:
            return None
        try:
            bundle.items.remove(item)
        except ValueError as exc:
            raise ValueError(item) from exc
        if not bundle.items:
            self._context_bundles.pop(user_id, None)
            self._active_context_bundle_chats.pop(user_id, None)
            return None
        return bundle

    def clear_context_bundle(
        self,
        user_id: int,
        provider: str,
        workspace_id: str,
    ) -> None:
        bundle = self.get_context_bundle(user_id, provider, workspace_id)
        if bundle is not None:
            self._context_bundles.pop(user_id, None)
            self._active_context_bundle_chats.pop(user_id, None)

    def enable_context_bundle_chat(
        self,
        user_id: int,
        provider: str,
        workspace_id: str,
    ) -> bool:
        bundle = self.get_context_bundle(user_id, provider, workspace_id)
        if bundle is None or not bundle.items:
            self._active_context_bundle_chats.pop(user_id, None)
            return False
        self._active_context_bundle_chats[user_id] = _ActiveContextBundleChat(
            provider=provider,
            workspace_id=workspace_id,
        )
        return True

    def disable_context_bundle_chat(self, user_id: int) -> None:
        self._active_context_bundle_chats.pop(user_id, None)

    def context_bundle_chat_active(
        self,
        user_id: int,
        provider: str,
        workspace_id: str,
    ) -> bool:
        chat = self._active_context_bundle_chats.get(user_id)
        if chat is None:
            return False
        if chat.provider != provider or chat.workspace_id != workspace_id:
            self._active_context_bundle_chats.pop(user_id, None)
            return False
        bundle = self.get_context_bundle(user_id, provider, workspace_id)
        if bundle is None or not bundle.items:
            self._active_context_bundle_chats.pop(user_id, None)
            return False
        return True

    def _cancel_media_group_tasks(self) -> None:
        for buffer in self._media_groups.values():
            if buffer.task is not None:
                buffer.task.cancel()
        self._media_groups.clear()

    def invalidate_session_bound_interactions(self) -> None:
        self._actions.clear()
        self._pending_text_actions.clear()
        self._agent_command_aliases.clear()
        self._cancel_media_group_tasks()

    def invalidate_runtime_bound_interactions(self) -> None:
        self.invalidate_session_bound_interactions()
        self._active_context_bundle_chats.clear()

    def add_media_group_message(self, user_id: int, media_group_id: str, message) -> _MediaGroupBuffer:
        key = (user_id, media_group_id)
        buffer = self._media_groups.get(key)
        if buffer is None:
            buffer = _MediaGroupBuffer(messages=[])
            self._media_groups[key] = buffer
        buffer.messages.append(message)
        return buffer

    def replace_media_group_task(
        self,
        user_id: int,
        media_group_id: str,
        task: asyncio.Task,
    ) -> asyncio.Task | None:
        key = (user_id, media_group_id)
        buffer = self._media_groups.get(key)
        if buffer is None:
            buffer = _MediaGroupBuffer(messages=[])
            self._media_groups[key] = buffer
        previous = buffer.task
        buffer.task = task
        return previous

    def pop_media_group_messages(self, user_id: int, media_group_id: str) -> tuple[Any, ...]:
        buffer = self._media_groups.pop((user_id, media_group_id), None)
        if buffer is None:
            return ()
        return tuple(buffer.messages)

    def _prune(self) -> None:
        now = self._clock()
        expired = [token for token, action in self._actions.items() if action.expires_at <= now]
        for token in expired:
            self._actions.pop(token, None)
        expired_user_ids = [
            user_id
            for user_id, action in self._pending_text_actions.items()
            if action.expires_at <= now
        ]
        for user_id in expired_user_ids:
            self._pending_text_actions.pop(user_id, None)


def _main_menu_markup(user_id: int, services) -> ReplyKeyboardMarkup:
    rows = [
        [BUTTON_BOT_STATUS],
        [BUTTON_NEW_SESSION, BUTTON_RETRY_LAST_TURN],
        [BUTTON_FORK_LAST_TURN, BUTTON_SESSION_HISTORY],
        [BUTTON_AGENT_COMMANDS, BUTTON_MODEL_MODE],
        [BUTTON_WORKSPACE_FILES, BUTTON_WORKSPACE_SEARCH],
        [BUTTON_WORKSPACE_CHANGES, BUTTON_CONTEXT_BUNDLE],
        [BUTTON_RESTART_AGENT],
    ]
    if user_id == services.admin_user_id:
        rows.append([BUTTON_SWITCH_AGENT, BUTTON_SWITCH_WORKSPACE])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, is_persistent=True)


def _is_authorized(update: Update, services) -> bool:
    user = update.effective_user
    return user is not None and user.id in services.allowed_user_ids


def _is_admin(update: Update, services) -> bool:
    user = update.effective_user
    return user is not None and user.id == services.admin_user_id


def _is_main_menu_button(text: str) -> bool:
    return text in {
        BUTTON_NEW_SESSION,
        BUTTON_RETRY_LAST_TURN,
        BUTTON_FORK_LAST_TURN,
        BUTTON_SESSION_HISTORY,
        BUTTON_AGENT_COMMANDS,
        BUTTON_MODEL_MODE,
        BUTTON_WORKSPACE_FILES,
        BUTTON_WORKSPACE_SEARCH,
        BUTTON_WORKSPACE_CHANGES,
        BUTTON_CONTEXT_BUNDLE,
        BUTTON_RESTART_AGENT,
        BUTTON_SWITCH_AGENT,
        BUTTON_SWITCH_WORKSPACE,
    }


def _workspace_label(services, workspace_id: str) -> str:
    return services.config.agent.resolve_workspace(workspace_id).label


def _trim_command_description(description: str | None) -> str:
    text = (description or "Agent command").strip() or "Agent command"
    return text[:256]


def _normalize_command_alias(command_name: str) -> str:
    alias = command_name.lstrip("/").strip().lower()
    alias = re.sub(r"[^a-z0-9_]", "_", alias)
    alias = re.sub(r"_+", "_", alias).strip("_")
    if not alias:
        alias = "agent"
    if alias[0].isdigit():
        alias = f"cmd_{alias}"
    return alias[:32]


def _allocate_command_alias(command_name: str, used_aliases: set[str]) -> str:
    base = _normalize_command_alias(command_name)
    if base in _RESERVED_COMMAND_ALIASES:
        base = f"agent_{base}"[:32]
    alias = base
    suffix = 2
    while alias in used_aliases or alias in _RESERVED_COMMAND_ALIASES:
        suffix_text = f"_{suffix}"
        alias = f"{base[: max(1, 32 - len(suffix_text))]}{suffix_text}"
        suffix += 1
    used_aliases.add(alias)
    return alias


def _build_agent_command_menu(commands) -> tuple[list[BotCommand], dict[str, str]]:
    used_aliases: set[str] = set()
    bot_commands: list[BotCommand] = []
    aliases: dict[str, str] = {}
    for command in commands[:100]:
        alias = _allocate_command_alias(command.name, used_aliases)
        aliases[alias] = command.name
        bot_commands.append(BotCommand(alias, _trim_command_description(command.description)))
    return bot_commands, aliases


async def _sync_agent_commands_for_user(application, ui_state: TelegramUiState, user_id: int, commands) -> None:
    menu_commands, aliases = _build_agent_command_menu(list(commands))
    ui_state.set_agent_command_aliases(user_id, aliases)
    scope = BotCommandScopeChat(chat_id=user_id)
    await application.bot.set_my_commands(menu_commands, scope=scope)


async def _sync_agent_commands_for_session(
    application,
    ui_state: TelegramUiState,
    user_id: int,
    session,
) -> None:
    if application is None:
        return
    commands = tuple(getattr(session, "available_commands", ()) or ())
    if not commands:
        wait_for_available_commands = getattr(session, "wait_for_available_commands", None)
        if wait_for_available_commands is not None:
            try:
                commands = tuple(
                    await wait_for_available_commands(COMMAND_DISCOVERY_TIMEOUT_SECONDS)
                )
            except Exception:
                commands = tuple(getattr(session, "available_commands", ()) or ())
    try:
        await _sync_agent_commands_for_user(application, ui_state, user_id, commands)
    except Exception:
        pass


async def _sync_discovered_agent_commands_for_user(
    application,
    services,
    ui_state: TelegramUiState,
    user_id: int,
) -> None:
    if application is None:
        return
    try:
        commands = await services.discover_agent_commands(
            timeout_seconds=COMMAND_DISCOVERY_TIMEOUT_SECONDS
        )
    except Exception:
        return
    try:
        await _sync_agent_commands_for_user(application, ui_state, user_id, commands)
    except Exception:
        pass


async def _clear_session_bound_ui_after_session_loss(
    application,
    services,
    ui_state: TelegramUiState,
    user_id: int,
) -> None:
    ui_state.invalidate_session_bound_interactions()
    await _sync_discovered_agent_commands_for_user(
        application,
        services,
        ui_state,
        user_id,
    )


def _message_update_from_callback(query):
    return SimpleNamespace(
        effective_user=query.from_user,
        message=query.message,
        callback_query=None,
    )


async def _sync_agent_commands_for_all_users(application, services, ui_state: TelegramUiState) -> None:
    commands = await services.discover_agent_commands(
        timeout_seconds=COMMAND_DISCOVERY_TIMEOUT_SECONDS
    )
    for user_id in services.allowed_user_ids:
        await _sync_agent_commands_for_user(application, ui_state, user_id, commands)


async def _reply_with_menu(message, services, user_id: int, text: str, *, reply_markup=None):
    markup = _main_menu_markup(user_id, services) if reply_markup is None else reply_markup
    await message.reply_text(text, reply_markup=markup)


async def _reply_unauthorized(update: Update) -> None:
    if update.message is not None:
        await update.message.reply_text("Unauthorized user.")


async def _reply_request_failed(update: Update, services) -> None:
    if update.message is not None and update.effective_user is not None:
        await _reply_with_menu(update.message, services, update.effective_user.id, "Request failed.")


async def _reply_session_creation_failed(update: Update, services) -> None:
    if update.message is not None and update.effective_user is not None:
        await _reply_with_menu(
            update.message,
            services,
            update.effective_user.id,
            "session creation failed",
        )


async def _with_active_store(services, action):
    last_error = None
    for _ in range(2):
        state = await services.snapshot_runtime_state()
        try:
            result = await action(state.session_store)
        except RetiredSessionStoreError as exc:
            last_error = exc
            continue
        return state, result
    raise last_error or RuntimeError("retired store retry exhausted")


async def handle_text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    services,
    ui_state: TelegramUiState,
) -> None:
    if update.message is None:
        return
    if not _is_authorized(update, services):
        await _reply_unauthorized(update)
        return

    user_id = update.effective_user.id
    text = update.message.text or ""
    if _is_main_menu_button(text):
        ui_state.clear_pending_text_action(user_id)

    if text == BUTTON_BOT_STATUS:
        await _show_runtime_status(update, services, ui_state)
        return
    if text == BUTTON_NEW_SESSION:
        await _start_new_session(
            update,
            services,
            ui_state,
            application=None if context is None else context.application,
        )
        return
    if text == BUTTON_RETRY_LAST_TURN:
        await _retry_last_turn(
            update,
            services,
            ui_state,
            application=None if context is None else context.application,
        )
        return
    if text == BUTTON_FORK_LAST_TURN:
        await _fork_last_turn(
            update,
            services,
            ui_state,
            application=None if context is None else context.application,
        )
        return
    if text == BUTTON_RESTART_AGENT:
        await _restart_agent(
            update,
            services,
            ui_state,
            application=None if context is None else context.application,
        )
        return
    if text == BUTTON_SWITCH_AGENT:
        await _show_switch_agent_menu(update, services, ui_state)
        return
    if text == BUTTON_SWITCH_WORKSPACE:
        await _show_switch_workspace_menu(update, services, ui_state)
        return
    if text == BUTTON_SESSION_HISTORY:
        await _show_session_history(update, services, ui_state, page=0)
        return
    if text == BUTTON_AGENT_COMMANDS:
        await _show_agent_commands_menu(update, services, ui_state)
        return
    if text == BUTTON_MODEL_MODE:
        await _show_model_mode_menu(
            update,
            services,
            ui_state,
            application=None if context is None else context.application,
        )
        return
    if text == BUTTON_WORKSPACE_FILES:
        await _show_workspace_files(update, services, ui_state)
        return
    if text == BUTTON_WORKSPACE_SEARCH:
        await _start_workspace_search(update, services, ui_state)
        return
    if text == BUTTON_WORKSPACE_CHANGES:
        await _show_workspace_changes(update, services, ui_state)
        return
    if text == BUTTON_CONTEXT_BUNDLE:
        await _show_context_bundle(update, services, ui_state)
        return

    pending_text_action = ui_state.get_pending_text_action(user_id)
    if pending_text_action is not None:
        if await _handle_pending_text_action(
            update,
            services,
            ui_state,
            pending_text_action,
            text,
        ):
            return

    state = await services.snapshot_runtime_state()
    ui_state.set_last_request_text(user_id, state.workspace_id, text)
    if ui_state.context_bundle_chat_active(user_id, state.provider, state.workspace_id):
        bundle = ui_state.get_context_bundle(user_id, state.provider, state.workspace_id)
        if bundle is None or not bundle.items:
            ui_state.disable_context_bundle_chat(user_id)
            await _reply_with_menu(
                update.message,
                services,
                user_id,
                "Context bundle chat was turned off because the current bundle is empty.",
            )
            return

        await _run_agent_prompt_turn_on_message(
            update.message,
            user_id,
            services,
            ui_state,
            _context_bundle_agent_prompt(tuple(bundle.items), text),
            title_hint=text,
            application=None if context is None else context.application,
        )
        return

    await _run_agent_text_turn(
        update,
        services,
        ui_state,
        text,
        application=None if context is None else context.application,
    )


async def handle_attachment(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    services,
    ui_state: TelegramUiState,
) -> None:
    if update.message is None:
        return
    if not _is_authorized(update, services):
        await _reply_unauthorized(update)
        return

    user_id = update.effective_user.id
    media_group_id = getattr(update.message, "media_group_id", None)
    if media_group_id:
        _queue_media_group_attachment(
            message=update.message,
            user_id=user_id,
            media_group_id=str(media_group_id),
            services=services,
            ui_state=ui_state,
            application=None if context is None else context.application,
        )
        return

    if ui_state.get_pending_text_action(user_id) is not None:
        await _reply_with_menu(
            update.message,
            services,
            user_id,
            "The current action is waiting for plain text. Send text or cancel the pending action first.",
        )
        return

    try:
        prompt = await _build_attachment_prompt(update.message)
    except AttachmentPromptError as exc:
        await _reply_with_menu(update.message, services, user_id, str(exc))
        return
    except Exception:
        await _reply_request_failed(update, services)
        return

    await _run_agent_attachment_turn_on_message(
        update.message,
        user_id,
        services,
        ui_state,
        prompt,
        application=None if context is None else context.application,
    )


async def handle_agent_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    services,
    ui_state: TelegramUiState,
) -> None:
    if update.message is None:
        return
    if not _is_authorized(update, services):
        await _reply_unauthorized(update)
        return

    ui_state.clear_pending_text_action(update.effective_user.id)
    text = _restore_agent_command_text(
        update.message.text or "",
        update.effective_user.id,
        ui_state,
    )
    await _run_agent_text_turn(
        update,
        services,
        ui_state,
        text,
        application=None if context is None else context.application,
    )


async def handle_debug_status(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    services,
) -> None:
    del context

    if update.message is None:
        return
    if not _is_authorized(update, services):
        await _reply_unauthorized(update)
        return

    try:
        state, session = await _with_active_store(
            services,
            lambda store: store.peek(update.effective_user.id),
        )
    except Exception:
        await _reply_request_failed(update, services)
        return

    session_id = "none" if session is None else (session.session_id or "pending")
    workspace_label = _workspace_label(services, state.workspace_id)
    capability_suffix = ""
    if session is not None and session.session_id is not None:
        capabilities = getattr(session, "capabilities", None)
        if capabilities is not None:
            prompt_caps = (
                f"img={'yes' if getattr(capabilities, 'supports_image_prompt', False) else 'no'}"
                f",audio={'yes' if getattr(capabilities, 'supports_audio_prompt', False) else 'no'}"
                f",docs={'yes' if getattr(capabilities, 'supports_embedded_context_prompt', False) else 'no'}"
            )
            session_caps = (
                f"list={'yes' if getattr(capabilities, 'can_list', False) else 'no'}"
                f",resume={'yes' if getattr(capabilities, 'can_resume', False) else 'no'}"
            )
            capability_suffix = f" prompt_caps={prompt_caps} session_caps={session_caps}"
    await _reply_with_menu(
        update.message,
        services,
        update.effective_user.id,
        (
            f"provider={state.provider} workspace_id={state.workspace_id} "
            f"workspace={workspace_label} cwd={state.workspace_path} session_id={session_id}"
            f"{capability_suffix}"
        ),
    )


async def _load_runtime_status_view_state(services, user_id: int):
    async def load(store):
        session = await store.peek(user_id)
        history_entries = await store.list_history(user_id)
        return session, history_entries

    state, result = await _with_active_store(services, load)
    session, history_entries = result
    git_status = _safe_read_workspace_git_status(state.workspace_path)
    return state, session, history_entries, git_status


async def _show_runtime_status(update: Update, services, ui_state: TelegramUiState) -> None:
    if update.message is None:
        return
    if not _is_authorized(update, services):
        await _reply_unauthorized(update)
        return

    try:
        state, session, history_entries, git_status = await _load_runtime_status_view_state(
            services,
            update.effective_user.id,
        )
    except Exception:
        await _reply_request_failed(update, services)
        return

    text, markup = _build_runtime_status_view(
        provider=state.provider,
        workspace_id=state.workspace_id,
        workspace_label=_workspace_label(services, state.workspace_id),
        workspace_path=state.workspace_path,
        git_status=git_status,
        session=session,
        session_title=_current_session_history_title(session, history_entries),
        history_entries=history_entries,
        history_count=len(history_entries),
        user_id=update.effective_user.id,
        ui_state=ui_state,
        is_admin=update.effective_user.id == services.admin_user_id,
    )
    await update.message.reply_text(text, reply_markup=markup)


async def _show_runtime_status_from_callback(
    query,
    services,
    ui_state: TelegramUiState,
    *,
    user_id: int,
    notice: str | None = None,
) -> None:
    if query.message is not None:
        await _show_runtime_status_on_message(
            query.message,
            services,
            ui_state,
            user_id=user_id,
            notice=notice,
        )


async def _show_runtime_status_on_message(
    message,
    services,
    ui_state: TelegramUiState,
    *,
    user_id: int,
    notice: str | None = None,
) -> None:
    state, session, history_entries, git_status = await _load_runtime_status_view_state(
        services,
        user_id,
    )
    text, markup = _build_runtime_status_view(
        provider=state.provider,
        workspace_id=state.workspace_id,
        workspace_label=_workspace_label(services, state.workspace_id),
        workspace_path=state.workspace_path,
        git_status=git_status,
        session=session,
        session_title=_current_session_history_title(session, history_entries),
        history_entries=history_entries,
        history_count=len(history_entries),
        user_id=user_id,
        ui_state=ui_state,
        is_admin=user_id == services.admin_user_id,
        notice=notice,
    )
    await message.edit_text(text, reply_markup=markup)


def _pending_text_action_label(pending_text_action: _PendingTextAction | None) -> str:
    if pending_text_action is None:
        return "none"

    action = pending_text_action.action
    payload = pending_text_action.payload
    if action == "rename_history":
        return _status_summary_with_details(
            "Rename session title",
            _status_text_snippet(str(payload.get("session_id", ""))),
        )
    if action == "run_agent_command":
        return (
            f"Command args for {_agent_command_name(str(payload.get('command_name', 'command')))}"
        )
    if action == "workspace_search":
        return "Workspace search"
    if action == "workspace_file_agent_prompt":
        return _status_summary_with_details(
            "Workspace file request",
            _status_text_snippet(str(payload.get("relative_path", ""))),
        )
    if action == "workspace_change_agent_prompt":
        return _status_summary_with_details(
            "Workspace change request",
            _status_text_snippet(str(payload.get("relative_path", ""))),
        )
    if action == "context_bundle_agent_prompt":
        items = payload.get("items")
        item_count = len(items) if isinstance(items, (list, tuple)) else 0
        return _status_summary_with_details(
            "Context bundle request",
            _status_item_count_summary(item_count),
        )
    if action == "context_items_agent_prompt":
        items = payload.get("items")
        item_count = len(items) if isinstance(items, (list, tuple)) else 0
        return _status_summary_with_details(
            "Selected context request",
            _status_text_snippet(str(payload.get("prompt_label", ""))),
            _status_item_count_summary(item_count),
        )
    return action


def _status_text_snippet(text: str | None, *, limit: int = STATUS_TEXT_SNIPPET_LIMIT) -> str | None:
    if text is None:
        return None
    normalized = re.sub(r"\s+", " ", text).strip()
    if not normalized:
        return None
    if len(normalized) <= limit:
        return normalized
    if limit <= 3:
        return normalized[:limit]
    return normalized[: limit - 3] + "..."


def _status_item_count_summary(count: int) -> str | None:
    if count <= 0:
        return None
    return f"{count} item{'s' if count != 1 else ''}"


def _status_summary_with_details(summary: str, *details: str | None) -> str:
    filtered_details = [detail for detail in details if detail]
    if not filtered_details:
        return summary
    return f"{summary} ({', '.join(filtered_details)})"


def _current_session_history_title(session, history_entries) -> str | None:
    if session is None or session.session_id is None:
        return None
    for entry in history_entries:
        if entry.session_id == session.session_id and entry.title:
            return _status_text_snippet(entry.title)
    return None


def _status_context_bundle_preview_lines(
    bundle: _ContextBundle | None,
    *,
    limit: int = STATUS_BUNDLE_PREVIEW_LIMIT,
) -> list[str]:
    if bundle is None or not bundle.items:
        return []
    lines = ["Bundle preview:"]
    visible_items = bundle.items[:limit]
    for index, item in enumerate(visible_items, start=1):
        item_label = _status_text_snippet(_context_bundle_item_label(item))
        lines.append(f"{index}. {item_label or _context_bundle_item_label(item)}")
    remaining = len(bundle.items) - len(visible_items)
    if remaining > 0:
        lines.append(f"... {remaining} more item{'s' if remaining != 1 else ''}")
    return lines


def _status_agent_command_preview_lines(
    commands,
    *,
    limit: int = STATUS_COMMAND_PREVIEW_LIMIT,
) -> list[str]:
    if not commands:
        return []
    lines = ["Command preview:"]
    visible_commands = tuple(commands[:limit])
    for index, command in enumerate(visible_commands, start=1):
        label = _agent_command_name(command.name)
        if command.hint:
            label = f"{label} args: {command.hint}"
        lines.append(f"{index}. {_status_text_snippet(label) or label}")
    remaining = len(commands) - len(visible_commands)
    if remaining > 0:
        lines.append(f"... {remaining} more command{'s' if remaining != 1 else ''}")
    return lines


def _status_agent_command_quick_buttons(
    ui_state: TelegramUiState,
    *,
    user_id: int,
    commands,
    limit: int = STATUS_COMMAND_PREVIEW_LIMIT,
) -> list[list[InlineKeyboardButton]]:
    if not commands:
        return []
    rows: list[list[InlineKeyboardButton]] = []
    current_row: list[InlineKeyboardButton] = []
    for command in tuple(commands[:limit]):
        label = f"{'Args' if command.hint else 'Run'} {_agent_command_name(command.name)}"
        current_row.append(
            _callback_button(
                ui_state,
                user_id,
                label,
                "runtime_status_control",
                target="agent_command_quick",
                command_name=command.name,
                hint=command.hint,
            )
        )
        if len(current_row) >= STATUS_COMMAND_BUTTONS_PER_ROW:
            rows.append(current_row)
            current_row = []
    if current_row:
        rows.append(current_row)
    return rows


def _status_selection_quick_rows(
    ui_state: TelegramUiState,
    *,
    user_id: int,
    model_selection,
    mode_selection,
    can_retry_last_turn: bool = False,
) -> list[list[InlineKeyboardButton]]:
    rows: list[list[InlineKeyboardButton]] = []
    rows.extend(
        _status_selection_quick_rows_for_selection(
            ui_state,
            user_id=user_id,
            selection=model_selection,
            label_prefix="Model",
            target="selection_quick",
        )
    )
    rows.extend(
        _status_selection_quick_rows_for_selection(
            ui_state,
            user_id=user_id,
            selection=mode_selection,
            label_prefix="Mode",
            target="selection_quick",
        )
    )
    if can_retry_last_turn:
        rows.extend(
            _status_selection_quick_rows_for_selection(
                ui_state,
                user_id=user_id,
                selection=model_selection,
                label_prefix="Model+Retry",
                target="selection_retry_quick",
            )
        )
        rows.extend(
            _status_selection_quick_rows_for_selection(
                ui_state,
                user_id=user_id,
                selection=mode_selection,
                label_prefix="Mode+Retry",
                target="selection_retry_quick",
            )
        )
    return rows


def _status_selection_quick_rows_for_selection(
    ui_state: TelegramUiState,
    *,
    user_id: int,
    selection,
    label_prefix: str,
    target: str,
    limit: int = STATUS_SELECTION_QUICK_LIMIT,
) -> list[list[InlineKeyboardButton]]:
    if selection is None:
        return []
    alternative_choices = [
        choice
        for choice in getattr(selection, "choices", ())
        if choice.value != getattr(selection, "current_value", None)
    ][:limit]
    if not alternative_choices:
        return []

    rows: list[list[InlineKeyboardButton]] = []
    current_row: list[InlineKeyboardButton] = []
    for choice in alternative_choices:
        current_row.append(
            _callback_button(
                ui_state,
                user_id,
                f"{label_prefix}: {choice.label}",
                "runtime_status_control",
                target=target,
                kind=selection.kind,
                value=choice.value,
            )
        )
        if len(current_row) >= STATUS_SELECTION_BUTTONS_PER_ROW:
            rows.append(current_row)
            current_row = []
    if current_row:
        rows.append(current_row)
    return rows


def _workspace_change_status_label(status_code: str | None) -> str:
    normalized = (status_code or "").strip()
    return normalized or "??"


def _status_workspace_changes_summary(git_status) -> str:
    if git_status is None:
        return "unavailable"
    if not getattr(git_status, "is_git_repo", False):
        return "not a git repo"
    change_count = len(getattr(git_status, "entries", ()))
    if change_count <= 0:
        return "clean"
    return f"{change_count} change{'s' if change_count != 1 else ''}"


def _status_workspace_changes_available(git_status) -> bool:
    if git_status is None or not getattr(git_status, "is_git_repo", False):
        return False
    return bool(getattr(git_status, "entries", ()))


def _status_workspace_change_preview_lines(
    git_status,
    *,
    limit: int = STATUS_WORKSPACE_CHANGE_PREVIEW_LIMIT,
) -> list[str]:
    if git_status is None or not getattr(git_status, "is_git_repo", False):
        return []
    entries = tuple(getattr(git_status, "entries", ()))
    if not entries:
        return []

    lines = ["Workspace change preview:"]
    visible_entries = entries[:limit]
    for index, entry in enumerate(visible_entries, start=1):
        path_label = _status_text_snippet(entry.display_path)
        lines.append(
            f"{index}. [{_workspace_change_status_label(entry.status_code)}] "
            f"{path_label or entry.display_path}"
        )
    remaining = len(entries) - len(visible_entries)
    if remaining > 0:
        lines.append(f"... {remaining} more change{'s' if remaining != 1 else ''}")
    return lines


def _status_recent_history_entries(
    history_entries,
    *,
    current_session_id: str | None,
    limit: int = STATUS_RECENT_SESSION_PREVIEW_LIMIT,
) -> tuple[list[Any], int]:
    recent_entries = []
    total = 0
    for entry in history_entries:
        if current_session_id is not None and entry.session_id == current_session_id:
            continue
        total += 1
        if len(recent_entries) < limit:
            recent_entries.append(entry)
    return recent_entries, total


def _status_recent_session_label(entry) -> str:
    title = _status_text_snippet(getattr(entry, "title", None))
    session_id = _status_text_snippet(str(getattr(entry, "session_id", "")), limit=32)
    if title and session_id and title != session_id:
        return f"{title} ({session_id})"
    return title or session_id or "untitled session"


def _status_recent_session_preview_lines(
    entries,
    *,
    total_count: int,
) -> list[str]:
    if not entries:
        return []
    lines = ["Recent sessions:"]
    for index, entry in enumerate(entries, start=1):
        lines.append(f"{index}. {_status_recent_session_label(entry)}")
    remaining = total_count - len(entries)
    if remaining > 0:
        lines.append(f"... {remaining} more session{'s' if remaining != 1 else ''}")
    return lines


def _status_recent_session_quick_buttons(
    ui_state: TelegramUiState,
    *,
    user_id: int,
    entries,
    can_retry_last_turn: bool,
) -> list[list[InlineKeyboardButton]]:
    rows: list[list[InlineKeyboardButton]] = []
    for index, entry in enumerate(entries, start=1):
        row = [
            _callback_button(
                ui_state,
                user_id,
                f"Switch {index}",
                "runtime_status_control",
                target="history_session_quick_switch",
                session_id=entry.session_id,
            )
        ]
        if can_retry_last_turn:
            row.append(
                _callback_button(
                    ui_state,
                    user_id,
                    f"Switch+Retry {index}",
                    "runtime_status_control",
                    target="history_session_quick_retry",
                    session_id=entry.session_id,
                )
            )
        rows.append(row)
    return rows


async def handle_callback_query(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    services,
    ui_state: TelegramUiState,
) -> None:
    query = update.callback_query
    if query is None:
        return
    if not _is_authorized(update, services):
        await query.answer("Unauthorized user.", show_alert=True)
        return

    data = query.data or ""
    if not data.startswith(CALLBACK_PREFIX):
        await query.answer("Unknown action.", show_alert=True)
        return

    token = data[len(CALLBACK_PREFIX) :]
    callback_action = ui_state.get(token)
    if callback_action is None:
        await query.answer("This button has expired.", show_alert=True)
        return
    if update.effective_user is None or callback_action.user_id != update.effective_user.id:
        await query.answer("This button is not for you.", show_alert=True)
        return

    callback_action = ui_state.pop(token)
    if callback_action is None:
        await query.answer("This button has expired.", show_alert=True)
        return

    try:
        await _dispatch_callback_action(
            query,
            services,
            ui_state,
            callback_action,
            application=None if context is None else context.application,
        )
    except Exception:
        try:
            await query.answer("Request failed.", show_alert=True)
        except Exception:
            pass


def build_telegram_application(config, services) -> Application:
    ui_state = TelegramUiState()

    async def _post_init(application: Application) -> None:
        await services.bind_telegram_command_menu_updater(
            partial(_sync_agent_commands_for_all_users, application, services, ui_state)
        )
        await services.refresh_telegram_command_menu()

    builder = ApplicationBuilder().token(config.telegram.bot_token).post_init(_post_init)
    application = builder.build()
    application.add_handler(
        CommandHandler(DEBUG_STATUS_COMMAND, partial(handle_debug_status, services=services))
    )
    application.add_handler(
        CallbackQueryHandler(partial(handle_callback_query, services=services, ui_state=ui_state))
    )
    application.add_handler(
        MessageHandler(
        filters.PHOTO | filters.Document.ALL | filters.VOICE | filters.AUDIO | filters.VIDEO,
        partial(handle_attachment, services=services, ui_state=ui_state),
    )
    )
    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            partial(handle_text, services=services, ui_state=ui_state),
        )
    )
    application.add_handler(
        MessageHandler(
            filters.COMMAND,
            partial(handle_agent_command, services=services, ui_state=ui_state),
        )
    )
    return application


def _restore_agent_command_text(text: str, user_id: int, ui_state: TelegramUiState) -> str:
    if not text.startswith("/"):
        return text

    parts = text.split(maxsplit=1)
    alias = parts[0][1:].split("@", 1)[0]
    original = ui_state.resolve_agent_command(user_id, alias) or alias
    prefix = original if original.startswith("/") else f"/{original}"
    if len(parts) == 1:
        return prefix
    return f"{prefix} {parts[1]}"


async def _handle_pending_text_action(
    update: Update,
    services,
    ui_state: TelegramUiState,
    pending_text_action: _PendingTextAction,
    text: str,
) -> bool:
    if update.message is None or update.effective_user is None:
        return False

    if pending_text_action.action == "rename_history":
        title = text.strip()
        if not title:
            await _reply_with_menu(
                update.message,
                services,
                update.effective_user.id,
                "Session title cannot be empty. Send another title or press Cancel Rename.",
            )
            return True

        ui_state.clear_pending_text_action(update.effective_user.id)
        page = int(pending_text_action.payload.get("page", 0))
        try:
            state, history_state = await _with_active_store(
                services,
                lambda store: _rename_history_entry(
                    store,
                    update.effective_user.id,
                    pending_text_action.payload["session_id"],
                    title,
                ),
            )
        except Exception:
            await _reply_request_failed(update, services)
            return True

        history_text, history_markup = _build_history_view(
            entries=history_state.entries,
            provider=state.provider,
            workspace_id=state.workspace_id,
            workspace_label=_workspace_label(services, state.workspace_id),
            user_id=update.effective_user.id,
            page=page,
            ui_state=ui_state,
            active_session_id=history_state.active_session_id,
            notice="Renamed session.",
            show_provider_sessions=update.effective_user.id == services.admin_user_id,
            back_target=str(pending_text_action.payload.get("back_target", "none")),
        )
        await update.message.reply_text(history_text, reply_markup=history_markup)
        return True

    if pending_text_action.action == "run_agent_command":
        command_args = text.strip()
        if not command_args:
            await _reply_with_menu(
                update.message,
                services,
                update.effective_user.id,
                "Command arguments cannot be empty. Send another value or press Cancel Command.",
            )
            return True

        ui_state.clear_pending_text_action(update.effective_user.id)
        after_turn_success, on_prepare_failure, on_turn_failure = _pending_status_turn_callbacks(
            pending_text_action,
            services,
            ui_state,
            user_id=update.effective_user.id,
        )
        await _run_agent_text_turn_on_message(
            update.message,
            update.effective_user.id,
            services,
            ui_state,
            _agent_command_text(
                pending_text_action.payload["command_name"],
                command_args,
            ),
            application=None,
            after_turn_success=after_turn_success,
            on_prepare_failure=on_prepare_failure,
            on_turn_failure=on_turn_failure,
        )
        return True

    if pending_text_action.action == "workspace_search":
        query_text = text.strip()
        if not query_text:
            await _reply_with_menu(
                update.message,
                services,
                update.effective_user.id,
                "Search query cannot be empty. Send another query or press Cancel Search.",
            )
            return True

        ui_state.clear_pending_text_action(update.effective_user.id)
        try:
            state, search_results = await _load_workspace_search_results(services, query_text)
        except Exception:
            source_message = _pending_source_message(pending_text_action)
            if source_message is not None:
                await _show_runtime_status_on_message(
                    source_message,
                    services,
                    ui_state,
                    user_id=update.effective_user.id,
                    notice="Request failed.",
                )
            else:
                await _reply_request_failed(update, services)
            return True

        search_text, search_markup = _build_workspace_search_results_view(
            search_results=search_results,
            provider=state.provider,
            workspace_label=_workspace_label(services, state.workspace_id),
            user_id=update.effective_user.id,
            page=0,
            ui_state=ui_state,
            last_request_text=ui_state.get_last_request_text(
                update.effective_user.id,
                state.workspace_id,
            ),
            back_target=str(pending_text_action.payload.get("back_target", "none")),
        )
        source_message = _pending_source_message(pending_text_action)
        if source_message is not None:
            await source_message.edit_text(search_text, reply_markup=search_markup)
        else:
            await update.message.reply_text(search_text, reply_markup=search_markup)
        return True

    if pending_text_action.action == "workspace_file_agent_prompt":
        request_text = text.strip()
        if not request_text:
            await _reply_with_menu(
                update.message,
                services,
                update.effective_user.id,
                "File request cannot be empty. Send another request or press Cancel Ask.",
            )
            return True

        ui_state.clear_pending_text_action(update.effective_user.id)
        after_turn_success, on_prepare_failure, on_turn_failure = _pending_status_turn_callbacks(
            pending_text_action,
            services,
            ui_state,
            user_id=update.effective_user.id,
        )
        await _run_workspace_file_request_on_message(
            update.message,
            update.effective_user.id,
            services,
            ui_state,
            relative_path=pending_text_action.payload["relative_path"],
            request_text=request_text,
            application=None,
            after_turn_success=after_turn_success,
            on_prepare_failure=on_prepare_failure,
            on_turn_failure=on_turn_failure,
        )
        return True

    if pending_text_action.action == "workspace_change_agent_prompt":
        request_text = text.strip()
        if not request_text:
            await _reply_with_menu(
                update.message,
                services,
                update.effective_user.id,
                "Change request cannot be empty. Send another request or press Cancel Ask.",
            )
            return True

        ui_state.clear_pending_text_action(update.effective_user.id)
        after_turn_success, on_prepare_failure, on_turn_failure = _pending_status_turn_callbacks(
            pending_text_action,
            services,
            ui_state,
            user_id=update.effective_user.id,
        )
        await _run_workspace_change_request_on_message(
            update.message,
            update.effective_user.id,
            services,
            ui_state,
            relative_path=pending_text_action.payload["relative_path"],
            status_code=pending_text_action.payload["status_code"],
            request_text=request_text,
            application=None,
            after_turn_success=after_turn_success,
            on_prepare_failure=on_prepare_failure,
            on_turn_failure=on_turn_failure,
        )
        return True

    if pending_text_action.action == "context_bundle_agent_prompt":
        request_text = text.strip()
        if not request_text:
            await _reply_with_menu(
                update.message,
                services,
                update.effective_user.id,
                "Context bundle request cannot be empty. Send another request or press Cancel Ask.",
            )
            return True

        context_items = tuple(pending_text_action.payload.get("items", ()))
        ui_state.clear_pending_text_action(update.effective_user.id)
        if not context_items:
            await _reply_with_menu(
                update.message,
                services,
                update.effective_user.id,
                "Context bundle is empty. Add files or changes first.",
            )
            return True

        after_turn_success, on_prepare_failure, on_turn_failure = _pending_status_turn_callbacks(
            pending_text_action,
            services,
            ui_state,
            user_id=update.effective_user.id,
        )
        await _run_context_bundle_request_on_message(
            update.message,
            update.effective_user.id,
            services,
            ui_state,
            items=context_items,
            request_text=request_text,
            application=None,
            after_turn_success=after_turn_success,
            on_prepare_failure=on_prepare_failure,
            on_turn_failure=on_turn_failure,
        )
        return True

    if pending_text_action.action == "context_items_agent_prompt":
        request_text = text.strip()
        if not request_text:
            await _reply_with_menu(
                update.message,
                services,
                update.effective_user.id,
                "Request cannot be empty. Send another request or press Cancel Ask.",
            )
            return True

        context_items = tuple(pending_text_action.payload.get("items", ()))
        ui_state.clear_pending_text_action(update.effective_user.id)
        if not context_items:
            await _reply_with_menu(
                update.message,
                services,
                update.effective_user.id,
                str(pending_text_action.payload.get("empty_notice", "Selected context is empty.")),
            )
            return True

        after_turn_success, on_prepare_failure, on_turn_failure = _pending_status_turn_callbacks(
            pending_text_action,
            services,
            ui_state,
            user_id=update.effective_user.id,
        )
        await _run_context_items_request_on_message(
            update.message,
            update.effective_user.id,
            services,
            ui_state,
            items=context_items,
            request_text=request_text,
            context_label=str(
                pending_text_action.payload.get("prompt_label", "selected context")
            ),
            application=None,
            after_turn_success=after_turn_success,
            on_prepare_failure=on_prepare_failure,
            on_turn_failure=on_turn_failure,
        )
        return True

    ui_state.clear_pending_text_action(update.effective_user.id)
    return False


def _agent_command_name(command_name: str) -> str:
    stripped = command_name.strip()
    return stripped if stripped.startswith("/") else f"/{stripped}"


def _agent_command_text(command_name: str, command_args: str | None = None) -> str:
    command = _agent_command_name(command_name)
    if command_args is None or not command_args.strip():
        return command
    return f"{command} {command_args.strip()}"


def _workspace_file_agent_prompt(relative_path: str, request_text: str) -> str:
    normalized_path = _normalize_relative_path(relative_path)
    return (
        f"Please work with the file `{normalized_path}` in the current workspace.\n"
        "Read the file from disk so you use the latest workspace state.\n"
        f"User request: {request_text.strip()}"
    )


def _workspace_change_agent_prompt(relative_path: str, status_code: str, request_text: str) -> str:
    normalized_path = _normalize_relative_path(relative_path)
    return (
        f"Please inspect the Git change for `{normalized_path}` in the current workspace.\n"
        f"Current git status code: {status_code}.\n"
        "Use the local repository state, including git diff or file reads, so you use the latest workspace state.\n"
        f"User request: {request_text.strip()}"
    )


def _context_items_agent_prompt(
    items: list[_ContextBundleItem] | tuple[_ContextBundleItem, ...],
    request_text: str,
    *,
    context_label: str,
) -> str:
    lines = _context_items_prompt_lines(
        items,
        context_label=context_label,
    )
    lines.append(f"User request: {request_text.strip()}")
    return "\n".join(lines)


def _context_items_prompt_lines(
    items: list[_ContextBundleItem] | tuple[_ContextBundleItem, ...],
    *,
    context_label: str,
) -> list[str]:
    file_paths: list[str] = []
    change_items: list[tuple[str, str]] = []
    for item in items:
        normalized_path = _normalize_relative_path(item.relative_path)
        if item.kind == "change":
            change_items.append((normalized_path, item.status_code or "??"))
            continue
        file_paths.append(normalized_path)

    lines = [
        f"Please work with the following {context_label} in the current workspace.",
        "Use the local workspace state directly: read files from disk and inspect current Git changes so you use the latest state.",
    ]
    if file_paths:
        lines.append("Files:")
        lines.extend(f"- {path}" for path in file_paths)
    if change_items:
        lines.append("Git changes:")
        lines.extend(f"- {path} [{status_code}]" for path, status_code in change_items)
    return lines


def _context_bundle_agent_prompt(items: list[_ContextBundleItem] | tuple[_ContextBundleItem, ...], request_text: str) -> str:
    return _context_items_agent_prompt(
        items,
        request_text,
        context_label="context bundle",
    )


def _attachment_prompt_with_context_bundle(
    prompt: _AttachmentPrompt,
    items: list[_ContextBundleItem] | tuple[_ContextBundleItem, ...],
) -> _AttachmentPrompt:
    prefix_lines = _context_items_prompt_lines(
        items,
        context_label="context bundle",
    )
    prefix_lines.append(
        "Also process the attached Telegram content in this same turn and combine it with the current context bundle."
    )
    return _AttachmentPrompt(
        prompt_items=(PromptText("\n".join(prefix_lines)), *prompt.prompt_items),
        title_hint=prompt.title_hint,
    )


def _normalize_relative_path(relative_path: str) -> str:
    return relative_path.strip().replace("\\", "/")


def _context_bundle_item_label(item: _ContextBundleItem) -> str:
    normalized_path = _normalize_relative_path(item.relative_path)
    if item.kind == "change":
        return f"[change {item.status_code or '??'}] {normalized_path}"
    return f"[file] {normalized_path}"


def _queue_media_group_attachment(
    *,
    message,
    user_id: int,
    media_group_id: str,
    services,
    ui_state: TelegramUiState,
    application,
) -> None:
    ui_state.add_media_group_message(user_id, media_group_id, message)
    task = asyncio.create_task(
        _flush_media_group_after_delay(
            user_id=user_id,
            media_group_id=media_group_id,
            services=services,
            ui_state=ui_state,
            application=application,
        )
    )
    previous = ui_state.replace_media_group_task(user_id, media_group_id, task)
    if previous is not None:
        previous.cancel()


async def _flush_media_group_after_delay(
    *,
    user_id: int,
    media_group_id: str,
    services,
    ui_state: TelegramUiState,
    application,
) -> None:
    try:
        await asyncio.sleep(ui_state.media_group_settle_seconds)
    except asyncio.CancelledError:
        return

    messages = ui_state.pop_media_group_messages(user_id, media_group_id)
    if not messages:
        return

    lead_message = messages[0]
    if ui_state.get_pending_text_action(user_id) is not None:
        await _reply_with_menu(
            lead_message,
            services,
            user_id,
            "The current action is waiting for plain text. Send text or cancel the pending action first.",
        )
        return

    try:
        prompt = await _build_media_group_prompt(messages)
    except AttachmentPromptError as exc:
        await _reply_with_menu(lead_message, services, user_id, str(exc))
        return
    except Exception:
        await _reply_with_menu(lead_message, services, user_id, "Request failed.")
        return

    await _run_agent_attachment_turn_on_message(
        lead_message,
        user_id,
        services,
        ui_state,
        prompt,
        application=application,
    )


async def _build_attachment_prompt(message) -> _AttachmentPrompt:
    caption = _normalized_attachment_caption(message)
    content = await _build_attachment_content(message)
    lead_text = caption or content.fallback_text
    return _AttachmentPrompt(
        prompt_items=(PromptText(lead_text), *content.prompt_items),
        title_hint=caption or content.title_hint,
    )


async def _build_media_group_prompt(messages: tuple[Any, ...]) -> _AttachmentPrompt:
    if not messages:
        raise AttachmentPromptError("Media group is empty.")

    prompt_items: list[PromptImage | PromptAudio | PromptTextResource | PromptBlobResource] = []
    caption: str | None = None
    for message in messages:
        if caption is None:
            caption = _normalized_attachment_caption(message)
        content = await _build_attachment_content(message)
        prompt_items.extend(content.prompt_items)

    lead_text = caption or _media_group_fallback_text(len(prompt_items))
    title_hint = caption or _media_group_title_hint(len(prompt_items))
    return _AttachmentPrompt(
        prompt_items=(PromptText(lead_text), *tuple(prompt_items)),
        title_hint=title_hint,
    )


async def _build_attachment_content(message) -> _AttachmentContent:
    if message.photo:
        return _AttachmentContent(
            prompt_items=(await _build_photo_prompt_item(message.photo[-1]),),
            fallback_text="Please inspect the attached Telegram image.",
            title_hint="Telegram photo",
        )

    voice = getattr(message, "voice", None)
    if voice is not None:
        return _AttachmentContent(
            prompt_items=(await _build_audio_prompt_item(voice, default_mime_type="audio/ogg"),),
            fallback_text="Please inspect or transcribe the attached Telegram voice note.",
            title_hint="Telegram voice note",
        )

    audio = getattr(message, "audio", None)
    if audio is not None:
        return _AttachmentContent(
            prompt_items=(await _build_audio_prompt_item(audio, default_mime_type="audio/mpeg"),),
            fallback_text=_audio_fallback_prompt_text(audio),
            title_hint=_audio_title_hint(audio),
        )

    video = getattr(message, "video", None)
    if video is not None:
        return _AttachmentContent(
            prompt_items=(await _build_video_prompt_item(video),),
            fallback_text=_video_fallback_prompt_text(video),
            title_hint=_video_title_hint(video),
        )

    document = message.document
    if document is None:
        raise AttachmentPromptError("Only photo, voice, audio, video, and document messages are supported right now.")

    document_prompt_item = await _build_document_prompt_item(document)
    fallback_text = _document_fallback_prompt_text(getattr(document, "file_name", None))
    title_hint = _document_title_hint(getattr(document, "file_name", None))
    if isinstance(document_prompt_item, PromptAudio):
        fallback_text = _audio_fallback_text_from_name(getattr(document, "file_name", None))
        title_hint = _audio_title_hint_from_name(getattr(document, "file_name", None))
    return _AttachmentContent(
        prompt_items=(document_prompt_item,),
        fallback_text=fallback_text,
        title_hint=title_hint,
    )


def _normalized_attachment_caption(message) -> str | None:
    caption = (getattr(message, "caption", None) or "").strip()
    return caption or None


async def _build_photo_prompt_item(photo_size) -> PromptImage:
    mime_type = _guess_photo_mime_type(photo_size)
    data = await _download_attachment_bytes(photo_size)
    encoded = base64.b64encode(data).decode("ascii")
    return PromptImage(
        data=encoded,
        mime_type=mime_type,
        uri=f"telegram://photo/{quote(getattr(photo_size, 'file_unique_id', 'photo'))}",
    )


async def _build_document_prompt_item(
    document,
) -> PromptImage | PromptAudio | PromptTextResource | PromptBlobResource:
    mime_type = _document_mime_type(document)
    data = await _download_attachment_bytes(document)
    uri = _document_uri(document)

    if mime_type.startswith("image/"):
        return PromptImage(
            data=base64.b64encode(data).decode("ascii"),
            mime_type=mime_type,
            uri=uri,
        )
    if mime_type.startswith("audio/"):
        return PromptAudio(
            data=base64.b64encode(data).decode("ascii"),
            mime_type=mime_type,
            uri=uri,
        )

    text_payload = _decode_text_document(
        data,
        mime_type=mime_type,
        file_name=getattr(document, "file_name", None),
    )
    if text_payload is not None:
        return PromptTextResource(uri=uri, text=text_payload, mime_type=mime_type)

    return PromptBlobResource(
        uri=uri,
        blob=base64.b64encode(data).decode("ascii"),
        mime_type=mime_type,
    )


async def _build_audio_prompt_item(attachment, *, default_mime_type: str) -> PromptAudio:
    mime_type = getattr(attachment, "mime_type", None) or default_mime_type
    data = await _download_attachment_bytes(attachment)
    file_name = getattr(attachment, "file_name", None)
    file_unique_id = getattr(attachment, "file_unique_id", None) or getattr(
        attachment,
        "file_id",
        "audio",
    )
    if file_name:
        uri = f"telegram://audio/{quote(str(file_unique_id))}/{quote(file_name)}"
    else:
        extension = _default_extension_for_attachment_mime(mime_type)
        uri = f"telegram://audio/{quote(str(file_unique_id))}{extension}"
    return PromptAudio(
        data=base64.b64encode(data).decode("ascii"),
        mime_type=mime_type,
        uri=uri,
    )


async def _build_video_prompt_item(video) -> PromptBlobResource:
    mime_type = getattr(video, "mime_type", None) or "video/mp4"
    data = await _download_attachment_bytes(video)
    return PromptBlobResource(
        uri=_video_uri(video, mime_type=mime_type),
        blob=base64.b64encode(data).decode("ascii"),
        mime_type=mime_type,
    )


async def _download_attachment_bytes(attachment) -> bytes:
    file_size = getattr(attachment, "file_size", None)
    if file_size is not None and file_size > ATTACHMENT_MAX_BYTES:
        raise AttachmentPromptError("Attachment is too large. Max size is 8 MiB.")

    telegram_file = await attachment.get_file()
    payload = await telegram_file.download_as_bytearray()
    data = bytes(payload)
    if len(data) > ATTACHMENT_MAX_BYTES:
        raise AttachmentPromptError("Attachment is too large. Max size is 8 MiB.")
    return data


def _guess_photo_mime_type(photo_size) -> str:
    file_path = getattr(photo_size, "file_path", None)
    guessed_mime, _ = mimetypes.guess_type(file_path or "")
    return guessed_mime or "image/jpeg"


def _document_mime_type(document) -> str:
    mime_type = getattr(document, "mime_type", None)
    if mime_type:
        return mime_type
    guessed_mime, _ = mimetypes.guess_type(getattr(document, "file_name", None) or "")
    return guessed_mime or "application/octet-stream"


def _default_extension_for_attachment_mime(mime_type: str | None) -> str:
    overrides = {
        "audio/ogg": ".ogg",
        "image/jpeg": ".jpg",
    }
    if mime_type in overrides:
        return overrides[mime_type]
    guessed_extension = mimetypes.guess_extension(mime_type or "", strict=False)
    if guessed_extension is None:
        return ""
    return guessed_extension


def _document_uri(document) -> str:
    file_name = getattr(document, "file_name", None) or "document"
    file_id = getattr(document, "file_unique_id", None) or getattr(document, "file_id", "document")
    return f"telegram://document/{quote(str(file_id))}/{quote(file_name)}"


def _video_uri(video, *, mime_type: str) -> str:
    file_name = getattr(video, "file_name", None)
    file_id = getattr(video, "file_unique_id", None) or getattr(video, "file_id", "video")
    if file_name:
        return f"telegram://video/{quote(str(file_id))}/{quote(file_name)}"
    extension = _default_extension_for_attachment_mime(mime_type)
    return f"telegram://video/{quote(str(file_id))}{extension}"


def _document_title_hint(file_name: str | None) -> str:
    if file_name:
        return f"Telegram document: {file_name}"
    return "Telegram document"


def _document_fallback_prompt_text(file_name: str | None) -> str:
    if file_name:
        return f"Please inspect the attached Telegram document {file_name}."
    return "Please inspect the attached Telegram document."


def _video_title_hint(video) -> str:
    file_name = (getattr(video, "file_name", None) or "").strip()
    if file_name:
        return f"Telegram video: {file_name}"
    return "Telegram video"


def _video_fallback_prompt_text(video) -> str:
    file_name = (getattr(video, "file_name", None) or "").strip()
    if file_name:
        return f"Please inspect the attached Telegram video {file_name}."
    return "Please inspect the attached Telegram video."


def _media_group_title_hint(item_count: int) -> str:
    return f"Telegram media group ({item_count} items)"


def _media_group_fallback_text(item_count: int) -> str:
    if item_count == 1:
        return "Please inspect the attached Telegram media item."
    return f"Please inspect the attached Telegram media group with {item_count} items."


def _audio_title_hint_from_name(file_name: str | None) -> str:
    if file_name:
        return f"Telegram audio: {file_name}"
    return "Telegram audio"


def _audio_fallback_text_from_name(file_name: str | None) -> str:
    if file_name:
        return f"Please inspect or transcribe the attached Telegram audio {file_name}."
    return "Please inspect or transcribe the attached Telegram audio."


def _audio_title_hint(audio) -> str:
    title = (getattr(audio, "title", None) or "").strip()
    file_name = (getattr(audio, "file_name", None) or "").strip()
    if title:
        return f"Telegram audio: {title}"
    return _audio_title_hint_from_name(file_name or None)


def _audio_fallback_prompt_text(audio) -> str:
    title = (getattr(audio, "title", None) or "").strip()
    file_name = (getattr(audio, "file_name", None) or "").strip()
    if title:
        return f"Please inspect or transcribe the attached Telegram audio {title}."
    return _audio_fallback_text_from_name(file_name or None)


def _unsupported_prompt_content_message(provider: str, error: UnsupportedPromptContentError) -> str:
    labels = {
        "image": "image attachments",
        "audio": "audio attachments",
        "embedded_context": "document or binary attachments",
    }
    parts = [labels.get(item, item) for item in error.unsupported_content_types]
    if not parts:
        return f"The current {provider} session rejected this prompt content."
    if len(parts) == 1:
        unsupported = parts[0]
    elif len(parts) == 2:
        unsupported = f"{parts[0]} or {parts[1]}"
    else:
        unsupported = f"{', '.join(parts[:-1])}, or {parts[-1]}"
    return (
        f"The current {provider} session does not support {unsupported} via ACP prompts. "
        "Try plain text, a different attachment type, or switch agent."
    )


def _coerce_attachment_prompt_for_capabilities(prompt_items, capabilities, *, workspace_path: str):
    coerced_items: list[
        PromptText | PromptImage | PromptAudio | PromptTextResource | PromptBlobResource
    ] = []
    saved_context_items: list[_ContextBundleItem] = []
    for item in prompt_items:
        if isinstance(item, PromptImage) and not getattr(capabilities, "supports_image_prompt", False):
            fallback_text, saved_context_item = _save_prompt_item_to_workspace_fallback(
                workspace_path,
                item,
                unsupported_kind="image attachments",
                default_stem="telegram-image",
            )
            coerced_items.append(
                PromptText(fallback_text)
            )
            saved_context_items.append(saved_context_item)
            continue
        if isinstance(item, PromptAudio) and not getattr(capabilities, "supports_audio_prompt", False):
            fallback_text, saved_context_item = _save_prompt_item_to_workspace_fallback(
                workspace_path,
                item,
                unsupported_kind="audio attachments",
                default_stem="telegram-audio",
            )
            coerced_items.append(
                PromptText(fallback_text)
            )
            saved_context_items.append(saved_context_item)
            continue
        if isinstance(item, PromptTextResource) and not getattr(
            capabilities, "supports_embedded_context_prompt", False
        ):
            coerced_items.append(PromptText(_inline_text_resource_for_prompt(item)))
            continue
        if isinstance(item, PromptBlobResource) and not getattr(
            capabilities, "supports_embedded_context_prompt", False
        ):
            unsupported_kind, default_stem = _blob_prompt_fallback_details(item)
            fallback_text, saved_context_item = _save_prompt_item_to_workspace_fallback(
                workspace_path,
                item,
                unsupported_kind=unsupported_kind,
                default_stem=default_stem,
            )
            coerced_items.append(
                PromptText(fallback_text)
            )
            saved_context_items.append(saved_context_item)
            continue
        coerced_items.append(item)
    return _AttachmentPromptCoercion(
        prompt_items=tuple(coerced_items),
        saved_context_items=tuple(saved_context_items),
    )


def _merge_saved_context_items(
    *groups: tuple[_ContextBundleItem, ...],
) -> tuple[_ContextBundleItem, ...]:
    merged: list[_ContextBundleItem] = []
    seen: set[_ContextBundleItem] = set()
    for group in groups:
        for item in group:
            if item in seen:
                continue
            seen.add(item)
            merged.append(item)
    return tuple(merged)


def _coerce_replay_turn_for_capabilities(
    replay_turn: _ReplayTurn,
    capabilities,
    *,
    provider: str,
    workspace_id: str,
    workspace_path: str,
) -> _ReplayTurn:
    coercion = _coerce_attachment_prompt_for_capabilities(
        replay_turn.prompt_items,
        capabilities,
        workspace_path=workspace_path,
    )
    return _ReplayTurn(
        provider=provider,
        workspace_id=workspace_id,
        prompt_items=coercion.prompt_items,
        title_hint=replay_turn.title_hint,
        saved_context_items=_merge_saved_context_items(
            replay_turn.saved_context_items,
            coercion.saved_context_items,
        ),
    )


def _inline_text_resource_for_prompt(item: PromptTextResource) -> str:
    original_text = item.text
    truncated_text = original_text[:INLINE_TEXT_DOCUMENT_CHAR_LIMIT]
    lines = [
        "Attached Telegram document content was inlined because the current provider does not support ACP embedded context.",
        f"URI: {item.uri}",
    ]
    if item.mime_type:
        lines.append(f"MIME type: {item.mime_type}")
    if len(truncated_text) != len(original_text):
        lines.append(
            f"Content was truncated to {len(truncated_text)} of {len(original_text)} characters."
        )
    lines.append("Begin document content:")
    lines.append(truncated_text)
    lines.append("End document content.")
    return "\n".join(lines)


def _blob_prompt_fallback_details(item: PromptBlobResource) -> tuple[str, str]:
    mime_type = getattr(item, "mime_type", None) or ""
    if mime_type.startswith("video/"):
        return "video attachments", "telegram-video"
    return "document attachments", "telegram-document"


def _save_prompt_item_to_workspace_fallback(
    workspace_path: str,
    item: PromptImage | PromptAudio | PromptBlobResource,
    *,
    unsupported_kind: str,
    default_stem: str,
) -> tuple[str, _ContextBundleItem]:
    try:
        inbox_file = save_workspace_inbox_file(
            workspace_path,
            _prompt_item_binary_payload(item),
            suggested_name=_prompt_item_suggested_name(item),
            mime_type=getattr(item, "mime_type", None),
            default_stem=default_stem,
        )
    except Exception as exc:
        raise AttachmentPromptError("Failed to save attachment into workspace inbox.") from exc

    return (
        f"Telegram attachment was saved to `{inbox_file.relative_path}` in the current workspace "
        f"because the current provider does not support {unsupported_kind} via ACP prompts.\n"
        "Read the file from disk and continue with the user's request using the local workspace state.",
        _ContextBundleItem(kind="file", relative_path=inbox_file.relative_path),
    )


def _prompt_item_binary_payload(item: PromptImage | PromptAudio | PromptBlobResource) -> bytes:
    if isinstance(item, PromptImage):
        return base64.b64decode(item.data)
    if isinstance(item, PromptAudio):
        return base64.b64decode(item.data)
    if isinstance(item, PromptBlobResource):
        return base64.b64decode(item.blob)
    raise TypeError(f"unsupported prompt item for workspace inbox fallback: {type(item)!r}")


def _prompt_item_suggested_name(item: PromptImage | PromptAudio | PromptBlobResource) -> str | None:
    uri = getattr(item, "uri", None)
    if uri is None:
        return None
    parsed = urlparse(uri)
    candidate = parsed.path.rsplit("/", 1)[-1].strip()
    return candidate or None


async def _discover_provider_capabilities_for_switch_menu(services, *, workspace_id: str):
    profiles = iter_provider_profiles()
    summaries = await asyncio.gather(
        *(
            services.discover_provider_capabilities(profile.provider, workspace_id=workspace_id)
            for profile in profiles
        )
    )
    return {summary.provider: summary for summary in summaries}


def _format_provider_capability_summary(profile, summary, *, is_current: bool) -> str:
    prefix = "* " if is_current else "- "
    if summary is None or not getattr(summary, "available", False):
        error = "unavailable"
        if summary is not None and getattr(summary, "error", None):
            error = f"unavailable ({summary.error})"
        return f"{prefix}{profile.display_name}: {error}"

    image = "yes" if getattr(summary, "supports_image_prompt", False) else "no"
    audio = "yes" if getattr(summary, "supports_audio_prompt", False) else "no"
    docs = "yes" if getattr(summary, "supports_embedded_context_prompt", False) else "no"
    sessions = []
    if getattr(summary, "can_list_sessions", False):
        sessions.append("list")
    if getattr(summary, "can_resume_sessions", False):
        sessions.append("resume")
    session_text = "none" if not sessions else "/".join(sessions)
    current = " [current]" if is_current else ""
    return (
        f"{prefix}{profile.display_name}{current}: "
        f"img={image} audio={audio} docs={docs} sessions={session_text}"
    )


def _decode_text_document(data: bytes, *, mime_type: str, file_name: str | None) -> str | None:
    file_name = file_name or ""
    suffix = ""
    if "." in file_name:
        suffix = f".{file_name.rsplit('.', 1)[-1].lower()}"
    looks_textual = mime_type.startswith("text/") or suffix in _TEXT_DOCUMENT_SUFFIXES
    if not looks_textual:
        return None
    if b"\x00" in data:
        return None
    try:
        decoded = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return decoded


async def _run_agent_text_turn(update: Update, services, ui_state: TelegramUiState, text: str, *, application) -> None:
    await _run_agent_text_turn_on_message(
        update.message,
        update.effective_user.id,
        services,
        ui_state,
        text,
        application=application,
    )


async def _run_agent_prompt_turn_on_message(
    message,
    user_id: int,
    services,
    ui_state: TelegramUiState,
    prompt_text: str,
    *,
    title_hint: str,
    application,
    after_turn_success=None,
    on_prepare_failure=None,
    on_turn_failure=None,
) -> None:
    async def _run(session, stream, _state):
        ui_state.set_last_turn(
            user_id,
            _ReplayTurn(
                provider=_state.provider,
                workspace_id=_state.workspace_id,
                prompt_items=(PromptText(prompt_text),),
                title_hint=title_hint,
            ),
        )
        return await session.run_turn(prompt_text, stream)

    await _run_agent_session_turn_on_message(
        message,
        user_id,
        services,
        ui_state,
        title_hint=title_hint,
        application=application,
        turn_runner=_run,
        after_turn_success=after_turn_success,
        on_prepare_failure=on_prepare_failure,
        on_turn_failure=on_turn_failure,
    )


async def _run_agent_text_turn_on_message(
    message,
    user_id: int,
    services,
    ui_state: TelegramUiState,
    text: str,
    *,
    application,
    after_turn_success=None,
    on_prepare_failure=None,
    on_turn_failure=None,
) -> None:
    await _run_agent_prompt_turn_on_message(
        message,
        user_id,
        services,
        ui_state,
        text,
        title_hint=text,
        application=application,
        after_turn_success=after_turn_success,
        on_prepare_failure=on_prepare_failure,
        on_turn_failure=on_turn_failure,
    )


async def _run_workspace_file_request_on_message(
    message,
    user_id: int,
    services,
    ui_state: TelegramUiState,
    *,
    relative_path: str,
    request_text: str,
    application,
    after_turn_success=None,
    on_prepare_failure=None,
    on_turn_failure=None,
) -> None:
    state = await services.snapshot_runtime_state()
    ui_state.set_last_request_text(user_id, state.workspace_id, request_text)
    await _run_agent_text_turn_on_message(
        message,
        user_id,
        services,
        ui_state,
        _workspace_file_agent_prompt(relative_path, request_text),
        application=application,
        after_turn_success=after_turn_success,
        on_prepare_failure=on_prepare_failure,
        on_turn_failure=on_turn_failure,
    )


async def _run_workspace_change_request_on_message(
    message,
    user_id: int,
    services,
    ui_state: TelegramUiState,
    *,
    relative_path: str,
    status_code: str,
    request_text: str,
    application,
    after_turn_success=None,
    on_prepare_failure=None,
    on_turn_failure=None,
) -> None:
    state = await services.snapshot_runtime_state()
    ui_state.set_last_request_text(user_id, state.workspace_id, request_text)
    await _run_agent_text_turn_on_message(
        message,
        user_id,
        services,
        ui_state,
        _workspace_change_agent_prompt(relative_path, status_code, request_text),
        application=application,
        after_turn_success=after_turn_success,
        on_prepare_failure=on_prepare_failure,
        on_turn_failure=on_turn_failure,
    )


async def _run_context_items_request_on_message(
    message,
    user_id: int,
    services,
    ui_state: TelegramUiState,
    *,
    items: tuple[_ContextBundleItem, ...],
    request_text: str,
    context_label: str,
    application,
    after_turn_success=None,
    on_prepare_failure=None,
    on_turn_failure=None,
) -> None:
    state = await services.snapshot_runtime_state()
    ui_state.set_last_request_text(user_id, state.workspace_id, request_text)
    await _run_agent_text_turn_on_message(
        message,
        user_id,
        services,
        ui_state,
        _context_items_agent_prompt(
            items,
            request_text,
            context_label=context_label,
        ),
        application=application,
        after_turn_success=after_turn_success,
        on_prepare_failure=on_prepare_failure,
        on_turn_failure=on_turn_failure,
    )


async def _run_context_bundle_request_on_message(
    message,
    user_id: int,
    services,
    ui_state: TelegramUiState,
    *,
    items: tuple[_ContextBundleItem, ...],
    request_text: str,
    application,
    after_turn_success=None,
    on_prepare_failure=None,
    on_turn_failure=None,
) -> None:
    state = await services.snapshot_runtime_state()
    ui_state.set_last_request_text(user_id, state.workspace_id, request_text)
    await _run_agent_text_turn_on_message(
        message,
        user_id,
        services,
        ui_state,
        _context_bundle_agent_prompt(items, request_text),
        application=application,
        after_turn_success=after_turn_success,
        on_prepare_failure=on_prepare_failure,
        on_turn_failure=on_turn_failure,
    )


async def _run_agent_replay_turn_on_message(
    message,
    user_id: int,
    services,
    ui_state: TelegramUiState,
    replay_turn: _ReplayTurn,
    *,
    application,
    after_turn_success=None,
    on_prepare_failure=None,
    on_turn_failure=None,
) -> None:
    effective_replay_turn = replay_turn

    async def _run(session, stream, state):
        nonlocal effective_replay_turn
        effective_replay_turn = _coerce_replay_turn_for_capabilities(
            replay_turn,
            getattr(session, "capabilities", None),
            provider=state.provider,
            workspace_id=state.workspace_id,
            workspace_path=state.workspace_path,
        )
        ui_state.set_last_turn(
            user_id,
            effective_replay_turn,
        )
        return await session.run_prompt(
            effective_replay_turn.prompt_items,
            stream,
        )

    async def _after_success(state):
        for item in effective_replay_turn.saved_context_items:
            ui_state.add_context_item(
                user_id,
                state.provider,
                state.workspace_id,
                item,
            )

    await _run_agent_session_turn_on_message(
        message,
        user_id,
        services,
        ui_state,
        title_hint=replay_turn.title_hint,
        application=application,
        turn_runner=_run,
        after_success=_after_success,
        after_turn_success=after_turn_success,
        on_prepare_failure=on_prepare_failure,
        on_turn_failure=on_turn_failure,
    )


async def _run_agent_attachment_turn_on_message(
    message,
    user_id: int,
    services,
    ui_state: TelegramUiState,
    prompt: _AttachmentPrompt,
    *,
    application,
) -> None:
    saved_context_items: tuple[_ContextBundleItem, ...] = ()

    async def _run(session, stream, state):
        nonlocal saved_context_items
        prompt_for_turn = prompt
        if ui_state.context_bundle_chat_active(user_id, state.provider, state.workspace_id):
            bundle = ui_state.get_context_bundle(user_id, state.provider, state.workspace_id)
            if bundle is not None and bundle.items:
                prompt_for_turn = _attachment_prompt_with_context_bundle(
                    prompt,
                    tuple(bundle.items),
                )
        coercion = _coerce_attachment_prompt_for_capabilities(
            prompt_for_turn.prompt_items,
            getattr(session, "capabilities", None),
            workspace_path=state.workspace_path,
        )
        saved_context_items = coercion.saved_context_items
        ui_state.set_last_turn(
            user_id,
            _ReplayTurn(
                provider=state.provider,
                workspace_id=state.workspace_id,
                prompt_items=coercion.prompt_items,
                title_hint=prompt.title_hint,
                saved_context_items=coercion.saved_context_items,
            ),
        )
        return await session.run_prompt(
            coercion.prompt_items,
            stream,
        )

    async def _after_success(state):
        for item in saved_context_items:
            ui_state.add_context_item(
                user_id,
                state.provider,
                state.workspace_id,
                item,
            )

    await _run_agent_session_turn_on_message(
        message,
        user_id,
        services,
        ui_state,
        title_hint=prompt.title_hint,
        application=application,
        turn_runner=_run,
        after_success=_after_success,
    )


async def _run_agent_session_turn_on_message(
    message,
    user_id: int,
    services,
    ui_state: TelegramUiState,
    *,
    title_hint: str,
    application,
    turn_runner,
    after_success=None,
    after_turn_success=None,
    on_prepare_failure=None,
    on_turn_failure=None,
) -> None:
    try:
        state, session = await _with_active_store(
            services,
            lambda store: _prepare_turn_session(
                store,
                user_id,
                time.monotonic(),
            ),
        )
    except Exception:
        if on_prepare_failure is not None:
            try:
                await on_prepare_failure()
                return
            except Exception:
                pass
        await message.reply_text(
            "Request failed.",
            reply_markup=_main_menu_markup(user_id, services),
        )
        return

    await _run_agent_session_turn_with_prepared_session_on_message(
        message,
        user_id,
        services,
        ui_state,
        state=state,
        session=session,
        title_hint=title_hint,
        application=application,
        turn_runner=turn_runner,
        after_success=after_success,
        after_turn_success=after_turn_success,
        on_turn_failure=on_turn_failure,
    )


async def _run_agent_session_turn_with_prepared_session_on_message(
    message,
    user_id: int,
    services,
    ui_state: TelegramUiState,
    *,
    state,
    session,
    title_hint: str,
    application,
    turn_runner,
    after_success=None,
    after_turn_success=None,
    on_turn_failure=None,
) -> None:

    before_workspace_git_status = _safe_read_workspace_git_status(state.workspace_path)
    stream = TelegramTurnStream(
        message=message,
        edit_interval=services.config.runtime.stream_edit_interval_ms / 1000.0,
    )
    await stream.start()
    try:
        response = await turn_runner(session, stream, state)
    except UnsupportedPromptContentError as exc:
        await stream.fail(_unsupported_prompt_content_message(state.provider, exc))
        await _invoke_turn_failure_callback(on_turn_failure)
        return
    except AttachmentPromptError as exc:
        await stream.fail(str(exc))
        await _invoke_turn_failure_callback(on_turn_failure)
        return
    except Exception:
        invalidate = getattr(state.session_store, "invalidate", None)
        session_lost = False
        try:
            if invalidate is not None:
                await invalidate(user_id, session)
                session_lost = True
            else:
                await session.close()
                session_lost = True
        except Exception:
            pass
        if session_lost:
            await _clear_session_bound_ui_after_session_loss(
                application,
                services,
                ui_state,
                user_id,
            )
            failure_text, failure_markup = _build_session_loss_recovery_view(
                provider=state.provider,
                workspace_label=_workspace_label(services, state.workspace_id),
                user_id=user_id,
                services=services,
                ui_state=ui_state,
            )
            await stream.fail(failure_text, reply_markup=failure_markup)
            await _invoke_turn_failure_callback(on_turn_failure)
            return
        await stream.fail("Request failed.")
        await _invoke_turn_failure_callback(on_turn_failure)
        return

    try:
        await state.session_store.record_session_usage(
            user_id,
            session,
            title_hint=title_hint,
        )
    except Exception:
        pass

    await stream.finish(stop_reason=response.stop_reason)

    if after_success is not None:
        try:
            await after_success(state)
        except Exception:
            pass

    await _maybe_reply_workspace_changes_follow_up(
        message,
        services,
        ui_state,
        user_id=user_id,
        state=state,
        before_git_status=before_workspace_git_status,
    )

    if application is not None and session.available_commands:
        await _sync_agent_commands_for_user(
            application,
            ui_state,
            user_id,
            session.available_commands,
        )

    if after_turn_success is not None:
        try:
            await after_turn_success(state, session)
        except Exception:
            pass


async def _invoke_turn_failure_callback(on_turn_failure) -> None:
    if on_turn_failure is not None:
        try:
            await on_turn_failure()
        except Exception:
            pass


async def _run_agent_command_from_callback(
    query,
    *,
    user_id: int,
    command_name: str,
    services,
    ui_state: TelegramUiState,
    application,
    back_target: str = "none",
) -> None:
    await _edit_query_message(query, f"Running {_agent_command_name(command_name)}...")
    if query.message is None:
        return

    after_turn_success = None
    on_prepare_failure = None
    on_turn_failure = None
    if back_target == "status":
        after_turn_success, on_prepare_failure, on_turn_failure = _status_turn_callbacks(
            query,
            services,
            ui_state,
            user_id=user_id,
            success_notice=f"Ran {_agent_command_name(command_name)}.",
        )

    await _run_agent_text_turn_on_message(
        query.message,
        user_id,
        services,
        ui_state,
        _agent_command_text(command_name),
        application=application,
        after_turn_success=after_turn_success,
        on_prepare_failure=on_prepare_failure,
        on_turn_failure=on_turn_failure,
    )


def _status_turn_callbacks(
    query,
    services,
    ui_state: TelegramUiState,
    *,
    user_id: int,
    success_notice: str,
):
    async def _after_turn_success(_state, _session) -> None:
        await _show_runtime_status_from_callback(
            query,
            services,
            ui_state,
            user_id=user_id,
            notice=success_notice,
        )

    async def _on_prepare_failure() -> None:
        await _show_runtime_status_from_callback(
            query,
            services,
            ui_state,
            user_id=user_id,
            notice="Request failed.",
        )

    async def _on_turn_failure() -> None:
        await _show_runtime_status_from_callback(
            query,
            services,
            ui_state,
            user_id=user_id,
            notice="Request failed.",
        )

    return _after_turn_success, _on_prepare_failure, _on_turn_failure


def _pending_source_message(pending_text_action: _PendingTextAction):
    return pending_text_action.payload.get("source_message")


async def _restore_pending_source_message(
    pending_text_action: _PendingTextAction,
    services,
    ui_state: TelegramUiState,
    *,
    user_id: int,
    notice: str,
) -> None:
    source_message = _pending_source_message(pending_text_action)
    if source_message is None:
        return
    if str(pending_text_action.payload.get("back_target", "none")) == "status":
        await _show_runtime_status_on_message(
            source_message,
            services,
            ui_state,
            user_id=user_id,
            notice=notice,
        )
        return
    restore_action = str(pending_text_action.payload.get("source_restore_action", ""))
    if restore_action == "workspace_changes_follow_up":
        await _show_workspace_changes_follow_up_on_message(
            source_message,
            services,
            ui_state,
            user_id=user_id,
            notice=notice,
        )


def _pending_status_turn_callbacks(
    pending_text_action: _PendingTextAction,
    services,
    ui_state: TelegramUiState,
    *,
    user_id: int,
):
    source_message = _pending_source_message(pending_text_action)
    success_notice = pending_text_action.payload.get("status_success_notice") or pending_text_action.payload.get(
        "source_success_notice"
    )
    if source_message is None or not success_notice:
        return None, None, None

    async def _after_turn_success(_state, _session) -> None:
        await _restore_pending_source_message(
            pending_text_action,
            services,
            ui_state,
            user_id=user_id,
            notice=str(success_notice),
        )

    async def _on_prepare_failure() -> None:
        await _restore_pending_source_message(
            pending_text_action,
            services,
            ui_state,
            user_id=user_id,
            notice="Request failed.",
        )

    async def _on_turn_failure() -> None:
        await _restore_pending_source_message(
            pending_text_action,
            services,
            ui_state,
            user_id=user_id,
            notice="Request failed.",
        )

    return _after_turn_success, _on_prepare_failure, _on_turn_failure


async def _prepare_turn_session(store, user_id: int, now: float):
    await store.close_idle_sessions(now)
    return await store.get_or_create(user_id)


async def _load_history_view_state(store, user_id: int) -> _HistoryViewState:
    active_session = await store.peek(user_id)
    entries = await store.list_history(user_id)
    return _HistoryViewState(
        entries=entries,
        active_session_id=None if active_session is None else active_session.session_id,
    )


async def _load_provider_sessions_view_state(
    services,
    user_id: int,
    *,
    cursor: str | None,
) -> tuple[Any, _ProviderSessionsViewState]:
    state, active_session = await _with_active_store(
        services,
        lambda store: store.peek(user_id),
    )
    provider_page = await services.list_provider_sessions(cursor=cursor)
    return state, _ProviderSessionsViewState(
        entries=provider_page.entries,
        next_cursor=provider_page.next_cursor,
        supported=provider_page.supported,
        active_session_id=None if active_session is None else active_session.session_id,
    )


async def _rename_history_entry(
    store,
    user_id: int,
    session_id: str,
    title: str,
) -> _HistoryViewState:
    await store.rename_history(user_id, session_id, title)
    return await _load_history_view_state(store, user_id)


async def _load_command_center_state(services, user_id: int) -> tuple[Any, _CommandCenterState]:
    state, session = await _with_active_store(
        services,
        lambda store: store.peek(user_id),
    )
    if session is not None:
        try:
            await session.ensure_started()
            commands = session.available_commands
            if not commands:
                commands = await session.wait_for_available_commands(
                    COMMAND_DISCOVERY_TIMEOUT_SECONDS
                )
        except Exception:
            commands = ()
        return state, _CommandCenterState(
            commands=tuple(commands),
            session_id=session.session_id,
        )

    commands = await services.discover_agent_commands(
        timeout_seconds=COMMAND_DISCOVERY_TIMEOUT_SECONDS
    )
    return state, _CommandCenterState(
        commands=tuple(commands),
        session_id=None,
    )


async def _show_agent_commands_menu(update: Update, services, ui_state: TelegramUiState) -> None:
    if update.message is None:
        return
    if not _is_authorized(update, services):
        await _reply_unauthorized(update)
        return

    try:
        state, command_state = await _load_command_center_state(
            services,
            update.effective_user.id,
        )
    except Exception:
        await _reply_request_failed(update, services)
        return

    text, markup = _build_agent_commands_view(
        commands=command_state.commands,
        provider=state.provider,
        workspace_label=_workspace_label(services, state.workspace_id),
        user_id=update.effective_user.id,
        page=0,
        ui_state=ui_state,
        session_id=command_state.session_id,
    )
    await update.message.reply_text(text, reply_markup=markup)


async def _show_agent_commands_menu_from_callback(
    query,
    services,
    ui_state: TelegramUiState,
    *,
    user_id: int,
    page: int,
    back_target: str = "none",
    notice: str | None = None,
) -> None:
    state, command_state = await _load_command_center_state(services, user_id)
    text, markup = _build_agent_commands_view(
        commands=command_state.commands,
        provider=state.provider,
        workspace_label=_workspace_label(services, state.workspace_id),
        user_id=user_id,
        page=page,
        ui_state=ui_state,
        session_id=command_state.session_id,
        back_target=back_target,
        notice=notice,
    )
    await _edit_query_message(query, text, reply_markup=markup)


async def _load_workspace_listing(services, relative_path: str = ""):
    state = await services.snapshot_runtime_state()
    listing = list_workspace_entries(state.workspace_path, relative_path)
    return state, listing


async def _show_workspace_files(update: Update, services, ui_state: TelegramUiState) -> None:
    if update.message is None:
        return
    if not _is_authorized(update, services):
        await _reply_unauthorized(update)
        return

    try:
        state, listing = await _load_workspace_listing(services)
    except Exception:
        await _reply_request_failed(update, services)
        return

    text, markup = _build_workspace_listing_view(
        listing=listing,
        provider=state.provider,
        workspace_label=_workspace_label(services, state.workspace_id),
        user_id=update.effective_user.id,
        page=0,
        ui_state=ui_state,
        last_request_text=ui_state.get_last_request_text(
            update.effective_user.id,
            state.workspace_id,
        ),
    )
    await update.message.reply_text(text, reply_markup=markup)


async def _start_workspace_search(update: Update, services, ui_state: TelegramUiState) -> None:
    if update.message is None:
        return
    if not _is_authorized(update, services):
        await _reply_unauthorized(update)
        return

    ui_state.set_pending_text_action(update.effective_user.id, "workspace_search")
    await update.message.reply_text(
        "Send your workspace search query as the next plain text message.",
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    _callback_button(
                        ui_state,
                        update.effective_user.id,
                        "Cancel Search",
                        "workspace_search_cancel",
                    )
                ]
            ]
        ),
    )


async def _show_workspace_listing_from_callback(
    query,
    services,
    ui_state: TelegramUiState,
    *,
    user_id: int,
    relative_path: str,
    page: int,
    back_target: str = "none",
    notice: str | None = None,
) -> None:
    state, listing = await _load_workspace_listing(services, relative_path)
    text, markup = _build_workspace_listing_view(
        listing=listing,
        provider=state.provider,
        workspace_label=_workspace_label(services, state.workspace_id),
        user_id=user_id,
        page=page,
        ui_state=ui_state,
        last_request_text=ui_state.get_last_request_text(user_id, state.workspace_id),
        back_target=back_target,
        notice=notice,
    )
    await _edit_query_message(query, text, reply_markup=markup)


async def _load_workspace_search_results(services, query_text: str):
    state = await services.snapshot_runtime_state()
    return state, search_workspace_text(state.workspace_path, query_text)


async def _show_workspace_search_results_from_callback(
    query,
    services,
    ui_state: TelegramUiState,
    *,
    user_id: int,
    query_text: str,
    page: int,
    back_target: str = "none",
    notice: str | None = None,
) -> None:
    state, search_results = await _load_workspace_search_results(services, query_text)
    text, markup = _build_workspace_search_results_view(
        search_results=search_results,
        provider=state.provider,
        workspace_label=_workspace_label(services, state.workspace_id),
        user_id=user_id,
        page=page,
        ui_state=ui_state,
        last_request_text=ui_state.get_last_request_text(user_id, state.workspace_id),
        back_target=back_target,
        notice=notice,
    )
    await _edit_query_message(query, text, reply_markup=markup)


async def _show_workspace_file_preview_from_callback(
    query,
    services,
    ui_state: TelegramUiState,
    *,
    user_id: int,
    relative_path: str,
    page: int,
    back_target: str = "none",
) -> None:
    state = await services.snapshot_runtime_state()
    preview = read_workspace_file_preview(state.workspace_path, relative_path)
    last_request_text = ui_state.get_last_request_text(user_id, state.workspace_id)
    bundle_source_payload = _callback_source_restore_payload(
        source_restore_action="workspace_file_preview_dir",
        source_restore_payload={
            "relative_path": preview.relative_path,
            "page": page,
            "back_target": back_target,
        },
        source_back_label="Back to File",
    )
    text, markup = _build_workspace_file_preview_view(
        preview=preview,
        provider=state.provider,
        workspace_label=_workspace_label(services, state.workspace_id),
        user_id=user_id,
        ui_state=ui_state,
        last_request_text=last_request_text,
        back_label="Back to Folder",
        back_action="workspace_back_to_dir",
        back_payload={
            "relative_path": _parent_relative_path(preview.relative_path),
            "page": page,
            "back_target": back_target,
        },
        ask_payload={
            "relative_path": preview.relative_path,
            "source": "dir",
            "page": page,
            "back_target": back_target,
        },
        quick_ask_payload={
            "relative_path": preview.relative_path,
            "source": "dir",
            "page": page,
            "back_target": back_target,
        },
        secondary_button_label="Add File to Context",
        secondary_button_action="workspace_file_add_context",
        secondary_button_payload={"relative_path": preview.relative_path},
        supplemental_buttons=(
            (
                "Start Bundle Chat With File",
                "workspace_file_start_bundle_chat",
                {
                    "relative_path": preview.relative_path,
                    "back_target": back_target,
                    **bundle_source_payload,
                },
            ),
            (
                "Open Context Bundle",
                "context_bundle_page",
                {"page": 0, "back_target": back_target, **bundle_source_payload},
            ),
        ),
    )
    await _edit_query_message(query, text, reply_markup=markup)


async def _show_workspace_search_file_preview_from_callback(
    query,
    services,
    ui_state: TelegramUiState,
    *,
    user_id: int,
    relative_path: str,
    query_text: str,
    page: int,
    back_target: str = "none",
) -> None:
    state = await services.snapshot_runtime_state()
    preview = read_workspace_file_preview(state.workspace_path, relative_path)
    last_request_text = ui_state.get_last_request_text(user_id, state.workspace_id)
    bundle_source_payload = _callback_source_restore_payload(
        source_restore_action="workspace_file_preview_search",
        source_restore_payload={
            "relative_path": preview.relative_path,
            "query_text": query_text,
            "page": page,
            "back_target": back_target,
        },
        source_back_label="Back to File",
    )
    text, markup = _build_workspace_file_preview_view(
        preview=preview,
        provider=state.provider,
        workspace_label=_workspace_label(services, state.workspace_id),
        user_id=user_id,
        ui_state=ui_state,
        last_request_text=last_request_text,
        back_label="Back to Search",
        back_action="workspace_search_back",
        back_payload={
            "query_text": query_text,
            "page": page,
            "back_target": back_target,
        },
        ask_payload={
            "relative_path": preview.relative_path,
            "source": "search",
            "query_text": query_text,
            "page": page,
            "back_target": back_target,
        },
        quick_ask_payload={
            "relative_path": preview.relative_path,
            "source": "search",
            "query_text": query_text,
            "page": page,
            "back_target": back_target,
        },
        secondary_button_label="Add File to Context",
        secondary_button_action="workspace_file_add_context",
        secondary_button_payload={"relative_path": preview.relative_path},
        supplemental_buttons=(
            (
                "Start Bundle Chat With File",
                "workspace_file_start_bundle_chat",
                {
                    "relative_path": preview.relative_path,
                    "back_target": back_target,
                    **bundle_source_payload,
                },
            ),
            (
                "Open Context Bundle",
                "context_bundle_page",
                {"page": 0, "back_target": back_target, **bundle_source_payload},
            ),
        ),
    )
    await _edit_query_message(query, text, reply_markup=markup)


async def _load_workspace_changes(services):
    state = await services.snapshot_runtime_state()
    return state, read_workspace_git_status(state.workspace_path)


def _safe_read_workspace_git_status(workspace_path: str):
    try:
        return read_workspace_git_status(workspace_path)
    except Exception:
        return None


def _workspace_changes_state_token(git_status) -> tuple[Any, ...] | None:
    if git_status is None:
        return None
    if not getattr(git_status, "is_git_repo", False):
        return ("not_git_repo",)
    return (
        "git_repo",
        tuple(
            (entry.status_code, entry.relative_path)
            for entry in getattr(git_status, "entries", ())
        ),
    )


def _count_noun(count: int, singular: str, plural: str | None = None) -> str:
    if count == 1:
        return singular
    return singular if plural is None else plural


def _context_items_add_to_bundle(
    ui_state: TelegramUiState,
    *,
    user_id: int,
    provider: str,
    workspace_id: str,
    items: tuple[_ContextBundleItem, ...],
) -> tuple[int, int]:
    added_count = 0
    duplicate_count = 0
    for item in items:
        _, added = ui_state.add_context_item(
            user_id,
            provider,
            workspace_id,
            item,
        )
        if added:
            added_count += 1
        else:
            duplicate_count += 1
    return added_count, duplicate_count


def _workspace_changes_context_items(git_status) -> tuple[_ContextBundleItem, ...]:
    return tuple(
        _ContextBundleItem(
            kind="change",
            relative_path=entry.relative_path,
            status_code=entry.status_code,
        )
        for entry in getattr(git_status, "entries", ())
    )


def _add_workspace_changes_to_context_bundle(
    ui_state: TelegramUiState,
    *,
    user_id: int,
    provider: str,
    workspace_id: str,
    git_status,
) -> tuple[int, int]:
    return _context_items_add_to_bundle(
        ui_state,
        user_id=user_id,
        provider=provider,
        workspace_id=workspace_id,
        items=_workspace_changes_context_items(git_status),
    )


def _workspace_changes_add_to_bundle_notice(*, added_count: int, duplicate_count: int) -> str:
    total = added_count + duplicate_count
    if total <= 0:
        return "No workspace changes to add."
    if added_count == total:
        return f"Added {added_count} {_count_noun(added_count, 'change', 'changes')} to context bundle."
    if added_count == 0:
        return (
            f"All {duplicate_count} {_count_noun(duplicate_count, 'change', 'changes')} "
            "are already in the context bundle."
        )
    return (
        f"Added {added_count} {_count_noun(added_count, 'change', 'changes')} to context bundle. "
        f"{duplicate_count} {_count_noun(duplicate_count, 'change', 'changes')} "
        f"{'was' if duplicate_count == 1 else 'were'} already present."
    )


def _workspace_changes_start_bundle_chat_notice(
    *,
    added_count: int,
    duplicate_count: int,
    already_active: bool,
) -> str:
    base_notice = _workspace_changes_add_to_bundle_notice(
        added_count=added_count,
        duplicate_count=duplicate_count,
    )
    if added_count + duplicate_count <= 0:
        return base_notice
    if already_active:
        return f"{base_notice} Bundle chat stays on."
    return f"{base_notice} Bundle chat enabled."


def _single_context_item_add_to_bundle_notice(*, item_kind: str, added: bool) -> str:
    noun = "file" if item_kind == "file" else "change"
    if added:
        return f"Added {noun} to context bundle."
    return f"{noun.capitalize()} is already in the context bundle."


def _single_context_item_start_bundle_chat_notice(
    *,
    item_kind: str,
    added: bool,
    already_active: bool,
) -> str:
    base_notice = _single_context_item_add_to_bundle_notice(
        item_kind=item_kind,
        added=added,
    )
    if already_active:
        return f"{base_notice} Bundle chat stays on."
    return f"{base_notice} Bundle chat enabled."


def _search_result_unique_paths(search_results) -> tuple[str, ...]:
    unique_paths: list[str] = []
    seen_paths: set[str] = set()
    for match in getattr(search_results, "matches", ()):
        relative_path = match.relative_path
        if relative_path in seen_paths:
            continue
        seen_paths.add(relative_path)
        unique_paths.append(relative_path)
    return tuple(unique_paths)


def _workspace_search_context_items(search_results) -> tuple[_ContextBundleItem, ...]:
    return tuple(
        _ContextBundleItem(
            kind="file",
            relative_path=relative_path,
        )
        for relative_path in _search_result_unique_paths(search_results)
    )


def _add_workspace_search_results_to_context_bundle(
    ui_state: TelegramUiState,
    *,
    user_id: int,
    provider: str,
    workspace_id: str,
    search_results,
) -> tuple[int, int]:
    return _context_items_add_to_bundle(
        ui_state,
        user_id=user_id,
        provider=provider,
        workspace_id=workspace_id,
        items=_workspace_search_context_items(search_results),
    )


def _workspace_search_add_to_bundle_notice(*, added_count: int, duplicate_count: int) -> str:
    total = added_count + duplicate_count
    if total <= 0:
        return "No matching files to add."
    if added_count == total:
        return (
            f"Added {added_count} {_count_noun(added_count, 'file', 'files')} "
            "from search results to context bundle."
        )
    if added_count == 0:
        return (
            f"All {duplicate_count} {_count_noun(duplicate_count, 'file', 'files')} "
            "from search results are already in the context bundle."
        )
    return (
        f"Added {added_count} {_count_noun(added_count, 'file', 'files')} "
        "from search results to context bundle. "
        f"{duplicate_count} {_count_noun(duplicate_count, 'file', 'files')} "
        f"{'was' if duplicate_count == 1 else 'were'} already present."
    )


def _workspace_search_start_bundle_chat_notice(
    *,
    added_count: int,
    duplicate_count: int,
    already_active: bool,
) -> str:
    base_notice = _workspace_search_add_to_bundle_notice(
        added_count=added_count,
        duplicate_count=duplicate_count,
    )
    if added_count + duplicate_count <= 0:
        return base_notice
    if already_active:
        return f"{base_notice} Bundle chat stays on."
    return f"{base_notice} Bundle chat enabled."


def _visible_workspace_entries(listing, page: int):
    page_count = max(1, (len(listing.entries) + WORKSPACE_PAGE_SIZE - 1) // WORKSPACE_PAGE_SIZE)
    page = min(max(page, 0), page_count - 1)
    start = page * WORKSPACE_PAGE_SIZE
    return page, page_count, listing.entries[start : start + WORKSPACE_PAGE_SIZE]


def _visible_workspace_file_paths(listing, page: int) -> tuple[str, ...]:
    _, _, visible_entries = _visible_workspace_entries(listing, page)
    return tuple(entry.relative_path for entry in visible_entries if not entry.is_dir)


def _workspace_listing_context_items(listing, page: int) -> tuple[_ContextBundleItem, ...]:
    return tuple(
        _ContextBundleItem(
            kind="file",
            relative_path=relative_path,
        )
        for relative_path in _visible_workspace_file_paths(listing, page)
    )


def _add_workspace_listing_files_to_context_bundle(
    ui_state: TelegramUiState,
    *,
    user_id: int,
    provider: str,
    workspace_id: str,
    listing,
    page: int,
) -> tuple[int, int]:
    return _context_items_add_to_bundle(
        ui_state,
        user_id=user_id,
        provider=provider,
        workspace_id=workspace_id,
        items=_workspace_listing_context_items(listing, page),
    )


def _workspace_listing_add_to_bundle_notice(*, added_count: int, duplicate_count: int) -> str:
    total = added_count + duplicate_count
    if total <= 0:
        return "No visible files to add."
    if added_count == total:
        return (
            f"Added {added_count} {_count_noun(added_count, 'file', 'files')} "
            "from workspace view to context bundle."
        )
    if added_count == 0:
        return (
            f"All {duplicate_count} visible {_count_noun(duplicate_count, 'file', 'files')} "
            f"{'is' if duplicate_count == 1 else 'are'} already in the context bundle."
        )
    return (
        f"Added {added_count} {_count_noun(added_count, 'file', 'files')} "
        "from workspace view to context bundle. "
        f"{duplicate_count} {_count_noun(duplicate_count, 'file', 'files')} "
        f"{'was' if duplicate_count == 1 else 'were'} already present."
    )


def _workspace_listing_start_bundle_chat_notice(
    *,
    added_count: int,
    duplicate_count: int,
    already_active: bool,
) -> str:
    base_notice = _workspace_listing_add_to_bundle_notice(
        added_count=added_count,
        duplicate_count=duplicate_count,
    )
    if added_count + duplicate_count <= 0:
        return base_notice
    if already_active:
        return f"{base_notice} Bundle chat stays on."
    return f"{base_notice} Bundle chat enabled."


def _build_workspace_changes_follow_up_view(
    *,
    git_status,
    provider: str,
    workspace_label: str,
    user_id: int,
    ui_state: TelegramUiState,
    last_request_text: str | None = None,
    notice: str | None = None,
):
    follow_up_back_target = "workspace_changes_follow_up"
    change_count = len(git_status.entries)
    lines = []
    if notice:
        lines.append(notice)
    lines.extend(
        [
        f"Workspace changes updated for {resolve_provider_profile(provider).display_name} in {workspace_label}",
        f"Branch: {git_status.branch_line or 'unknown'}",
        f"Current changes: {change_count}",
        ]
    )
    preview_entries = git_status.entries[:3]
    for index, entry in enumerate(preview_entries, start=1):
        lines.append(f"{index}. [{entry.status_code}] {entry.display_path}")
    remaining = change_count - len(preview_entries)
    if remaining > 0:
        lines.append(f"... {remaining} more")

    buttons = [
        [
            _callback_button(
                ui_state,
                user_id,
                "Open Workspace Changes",
                "workspace_changes_page",
                page=0,
                back_target=follow_up_back_target,
            )
        ],
        [
            _callback_button(
                ui_state,
                user_id,
                "Ask Agent With Current Changes",
                "workspace_changes_ask_agent",
                page=0,
                source="follow_up",
            ),
            _callback_button(
                ui_state,
                user_id,
                "Start Bundle Chat With Changes",
                "workspace_changes_start_bundle_chat",
                page=0,
                back_target=follow_up_back_target,
            ),
        ],
    ]
    if last_request_text is not None:
        buttons.append(
            [
                _callback_button(
                    ui_state,
                    user_id,
                    "Ask With Last Request",
                    "workspace_changes_ask_last_request",
                    page=0,
                    source="follow_up",
                )
            ]
        )
    buttons.extend(
        [
            [
                _callback_button(
                    ui_state,
                    user_id,
                    "Add All Changes to Context",
                    "workspace_changes_add_all",
                    page=0,
                    back_target=follow_up_back_target,
                ),
            ],
            [
                _callback_button(
                    ui_state,
                    user_id,
                    "Open Context Bundle",
                    "context_bundle_page",
                    page=0,
                    back_target=follow_up_back_target,
                ),
            ],
        ]
    )
    return "\n".join(lines), InlineKeyboardMarkup(buttons)


def _build_session_loss_recovery_view(
    *,
    provider: str,
    workspace_label: str,
    user_id: int,
    services,
    ui_state: TelegramUiState,
):
    text = (
        "Request failed. "
        f"The current live session for {resolve_provider_profile(provider).display_name} "
        f"in {workspace_label} was closed.\n"
        "Choose a recovery action."
    )
    buttons = [
        [
            _callback_button(
                ui_state,
                user_id,
                "Retry Last Turn",
                "recover_retry_last_turn",
            ),
            _callback_button(
                ui_state,
                user_id,
                "New Session",
                "recover_new_session",
            ),
        ],
        [
            _callback_button(
                ui_state,
                user_id,
                "Fork Last Turn",
                "recover_fork_last_turn",
            ),
            _callback_button(
                ui_state,
                user_id,
                "Session History",
                "recover_session_history",
            ),
        ],
        [
            _callback_button(
                ui_state,
                user_id,
                "Model / Mode",
                "recover_model_mode",
            ),
        ],
    ]
    if user_id == services.admin_user_id:
        buttons.append(
            [
                _callback_button(
                    ui_state,
                    user_id,
                    "Switch Agent",
                    "recover_switch_agent",
                ),
                _callback_button(
                    ui_state,
                    user_id,
                    "Switch Workspace",
                    "recover_switch_workspace",
                ),
            ]
        )
    return text, InlineKeyboardMarkup(buttons)


async def _maybe_reply_workspace_changes_follow_up(
    message,
    services,
    ui_state: TelegramUiState,
    *,
    user_id: int,
    state,
    before_git_status,
) -> None:
    after_git_status = _safe_read_workspace_git_status(state.workspace_path)
    if before_git_status is None or after_git_status is None:
        return
    if not getattr(after_git_status, "is_git_repo", False):
        return
    if not getattr(after_git_status, "entries", ()):
        return
    if _workspace_changes_state_token(before_git_status) == _workspace_changes_state_token(after_git_status):
        return

    text, markup = _build_workspace_changes_follow_up_view(
        git_status=after_git_status,
        provider=state.provider,
        workspace_label=_workspace_label(services, state.workspace_id),
        user_id=user_id,
        ui_state=ui_state,
        last_request_text=ui_state.get_last_request_text(user_id, state.workspace_id),
    )
    try:
        await message.reply_text(text, reply_markup=markup)
    except Exception:
        pass


async def _show_workspace_changes_follow_up_on_message(
    message,
    services,
    ui_state: TelegramUiState,
    *,
    user_id: int,
    notice: str | None = None,
) -> None:
    state, git_status = await _load_workspace_changes(services)
    if getattr(git_status, "is_git_repo", False) and getattr(git_status, "entries", ()):
        text, markup = _build_workspace_changes_follow_up_view(
            git_status=git_status,
            provider=state.provider,
            workspace_label=_workspace_label(services, state.workspace_id),
            user_id=user_id,
            ui_state=ui_state,
            last_request_text=ui_state.get_last_request_text(user_id, state.workspace_id),
            notice=notice,
        )
    else:
        text, markup = _build_workspace_changes_view(
            git_status=git_status,
            provider=state.provider,
            workspace_label=_workspace_label(services, state.workspace_id),
            user_id=user_id,
            page=0,
            ui_state=ui_state,
            last_request_text=ui_state.get_last_request_text(user_id, state.workspace_id),
            notice=notice or "No current workspace changes.",
        )
    await message.edit_text(text, reply_markup=markup)


async def _show_workspace_changes_follow_up_from_callback(
    query,
    services,
    ui_state: TelegramUiState,
    *,
    user_id: int,
    notice: str | None = None,
) -> None:
    if query.message is not None:
        await _show_workspace_changes_follow_up_on_message(
            query.message,
            services,
            ui_state,
            user_id=user_id,
            notice=notice,
        )


async def _show_workspace_changes(update: Update, services, ui_state: TelegramUiState) -> None:
    if update.message is None:
        return
    if not _is_authorized(update, services):
        await _reply_unauthorized(update)
        return

    try:
        state, git_status = await _load_workspace_changes(services)
    except Exception:
        await _reply_request_failed(update, services)
        return

    text, markup = _build_workspace_changes_view(
        git_status=git_status,
        provider=state.provider,
        workspace_label=_workspace_label(services, state.workspace_id),
        user_id=update.effective_user.id,
        page=0,
        ui_state=ui_state,
        last_request_text=ui_state.get_last_request_text(
            update.effective_user.id,
            state.workspace_id,
        ),
    )
    await update.message.reply_text(text, reply_markup=markup)


async def _show_workspace_changes_from_callback(
    query,
    services,
    ui_state: TelegramUiState,
    *,
    user_id: int,
    page: int,
    back_target: str = "none",
    notice: str | None = None,
) -> None:
    state, git_status = await _load_workspace_changes(services)
    text, markup = _build_workspace_changes_view(
        git_status=git_status,
        provider=state.provider,
        workspace_label=_workspace_label(services, state.workspace_id),
        user_id=user_id,
        page=page,
        ui_state=ui_state,
        last_request_text=ui_state.get_last_request_text(user_id, state.workspace_id),
        back_target=back_target,
        notice=notice,
    )
    await _edit_query_message(query, text, reply_markup=markup)


async def _show_workspace_change_preview_from_callback(
    query,
    services,
    ui_state: TelegramUiState,
    *,
    user_id: int,
    relative_path: str,
    status_code: str,
    page: int,
    back_target: str = "none",
) -> None:
    state = await services.snapshot_runtime_state()
    diff_preview = read_workspace_git_diff_preview(
        state.workspace_path,
        relative_path,
        status_code=status_code,
    )
    last_request_text = ui_state.get_last_request_text(user_id, state.workspace_id)
    bundle_source_payload = _callback_source_restore_payload(
        source_restore_action="workspace_change_preview",
        source_restore_payload={
            "relative_path": diff_preview.relative_path,
            "status_code": diff_preview.status_code,
            "page": page,
            "back_target": back_target,
        },
        source_back_label="Back to Change",
    )
    text, markup = _build_workspace_change_preview_view(
        diff_preview=diff_preview,
        provider=state.provider,
        workspace_label=_workspace_label(services, state.workspace_id),
        user_id=user_id,
        ui_state=ui_state,
        last_request_text=last_request_text,
        back_label="Back to Changes",
        back_action="workspace_changes_back",
        back_payload={"page": page, "back_target": back_target},
        ask_payload={
            "relative_path": relative_path,
            "status_code": status_code,
            "page": page,
            "source": "changes",
            "back_target": back_target,
        },
        quick_ask_payload={
            "relative_path": relative_path,
            "status_code": status_code,
            "page": page,
            "source": "changes",
            "back_target": back_target,
        },
        secondary_button_label="Add Change to Context",
        secondary_button_action="workspace_change_add_context",
        secondary_button_payload={
            "relative_path": diff_preview.relative_path,
            "status_code": diff_preview.status_code,
        },
        supplemental_buttons=(
            (
                "Start Bundle Chat With Change",
                "workspace_change_start_bundle_chat",
                {
                    "relative_path": diff_preview.relative_path,
                    "status_code": diff_preview.status_code,
                    "back_target": back_target,
                    **bundle_source_payload,
                },
            ),
            (
                "Open Context Bundle",
                "context_bundle_page",
                {"page": 0, "back_target": back_target, **bundle_source_payload},
            ),
        ),
    )
    await _edit_query_message(query, text, reply_markup=markup)


async def _show_context_bundle_file_preview_from_callback(
    query,
    services,
    ui_state: TelegramUiState,
    *,
    user_id: int,
    relative_path: str,
    page: int,
    back_target: str = "none",
    source_restore_action: str | None = None,
    source_restore_payload: dict[str, Any] | None = None,
    source_back_label: str | None = None,
) -> None:
    state = await services.snapshot_runtime_state()
    preview = read_workspace_file_preview(state.workspace_path, relative_path)
    last_request_text = ui_state.get_last_request_text(user_id, state.workspace_id)
    source_payload = _callback_source_restore_payload(
        source_restore_action=source_restore_action,
        source_restore_payload=source_restore_payload,
        source_back_label=source_back_label,
    )
    source_buttons = _source_restore_supplemental_buttons(
        source_restore_action=source_restore_action,
        source_restore_payload=source_restore_payload,
        source_back_label=source_back_label,
    )
    text, markup = _build_workspace_file_preview_view(
        preview=preview,
        provider=state.provider,
        workspace_label=_workspace_label(services, state.workspace_id),
        user_id=user_id,
        ui_state=ui_state,
        last_request_text=last_request_text,
        back_label="Back to Context Bundle",
        back_action="context_bundle_page",
        back_payload={"page": page, "back_target": back_target, **source_payload},
        ask_payload={
            "relative_path": preview.relative_path,
            "source": "bundle",
            "page": page,
            "back_target": back_target,
            **source_payload,
        },
        quick_ask_payload={
            "relative_path": preview.relative_path,
            "source": "bundle",
            "page": page,
            "back_target": back_target,
            **source_payload,
        },
        secondary_button_label="Remove From Context",
        secondary_button_action="context_bundle_preview_remove",
        secondary_button_payload={
            "kind": "file",
            "relative_path": preview.relative_path,
            "page": page,
            "back_target": back_target,
            **source_payload,
        },
        supplemental_buttons=source_buttons,
    )
    await _edit_query_message(query, text, reply_markup=markup)


async def _show_context_bundle_change_preview_from_callback(
    query,
    services,
    ui_state: TelegramUiState,
    *,
    user_id: int,
    relative_path: str,
    status_code: str,
    page: int,
    back_target: str = "none",
    source_restore_action: str | None = None,
    source_restore_payload: dict[str, Any] | None = None,
    source_back_label: str | None = None,
) -> None:
    state = await services.snapshot_runtime_state()
    diff_preview = read_workspace_git_diff_preview(
        state.workspace_path,
        relative_path,
        status_code=status_code,
    )
    last_request_text = ui_state.get_last_request_text(user_id, state.workspace_id)
    source_payload = _callback_source_restore_payload(
        source_restore_action=source_restore_action,
        source_restore_payload=source_restore_payload,
        source_back_label=source_back_label,
    )
    source_buttons = _source_restore_supplemental_buttons(
        source_restore_action=source_restore_action,
        source_restore_payload=source_restore_payload,
        source_back_label=source_back_label,
    )
    text, markup = _build_workspace_change_preview_view(
        diff_preview=diff_preview,
        provider=state.provider,
        workspace_label=_workspace_label(services, state.workspace_id),
        user_id=user_id,
        ui_state=ui_state,
        last_request_text=last_request_text,
        back_label="Back to Context Bundle",
        back_action="context_bundle_page",
        back_payload={"page": page, "back_target": back_target, **source_payload},
        ask_payload={
            "relative_path": relative_path,
            "status_code": status_code,
            "page": page,
            "source": "bundle",
            "back_target": back_target,
            **source_payload,
        },
        quick_ask_payload={
            "relative_path": relative_path,
            "status_code": status_code,
            "page": page,
            "source": "bundle",
            "back_target": back_target,
            **source_payload,
        },
        secondary_button_label="Remove From Context",
        secondary_button_action="context_bundle_preview_remove",
        secondary_button_payload={
            "kind": "change",
            "relative_path": relative_path,
            "status_code": status_code,
            "page": page,
            "back_target": back_target,
            **source_payload,
        },
        supplemental_buttons=source_buttons,
    )
    await _edit_query_message(query, text, reply_markup=markup)


async def _show_context_bundle(update: Update, services, ui_state: TelegramUiState) -> None:
    if update.message is None:
        return
    if not _is_authorized(update, services):
        await _reply_unauthorized(update)
        return

    try:
        state = await services.snapshot_runtime_state()
    except Exception:
        await _reply_request_failed(update, services)
        return

    bundle = ui_state.get_context_bundle(
        update.effective_user.id,
        state.provider,
        state.workspace_id,
    )
    text, markup = _build_context_bundle_view(
        bundle=bundle,
        provider=state.provider,
        workspace_label=_workspace_label(services, state.workspace_id),
        user_id=update.effective_user.id,
        page=0,
        ui_state=ui_state,
        last_request_text=ui_state.get_last_request_text(
            update.effective_user.id,
            state.workspace_id,
        ),
        bundle_chat_active=ui_state.context_bundle_chat_active(
            update.effective_user.id,
            state.provider,
            state.workspace_id,
        ),
    )
    await update.message.reply_text(text, reply_markup=markup)


async def _show_context_bundle_from_callback(
    query,
    services,
    ui_state: TelegramUiState,
    *,
    user_id: int,
    page: int,
    back_target: str = "none",
    notice: str | None = None,
    source_restore_action: str | None = None,
    source_restore_payload: dict[str, Any] | None = None,
    source_back_label: str | None = None,
) -> None:
    state = await services.snapshot_runtime_state()
    bundle = ui_state.get_context_bundle(user_id, state.provider, state.workspace_id)
    text, markup = _build_context_bundle_view(
        bundle=bundle,
        provider=state.provider,
        workspace_label=_workspace_label(services, state.workspace_id),
        user_id=user_id,
        page=page,
        ui_state=ui_state,
        last_request_text=ui_state.get_last_request_text(user_id, state.workspace_id),
        bundle_chat_active=ui_state.context_bundle_chat_active(
            user_id,
            state.provider,
            state.workspace_id,
        ),
        back_target=back_target,
        notice=notice,
        source_restore_action=source_restore_action,
        source_restore_payload=source_restore_payload,
        source_back_label=source_back_label,
    )
    await _edit_query_message(query, text, reply_markup=markup)


async def _begin_context_items_ask_from_callback(
    query,
    ui_state: TelegramUiState,
    *,
    user_id: int,
    items: tuple[_ContextBundleItem, ...],
    prompt_label: str,
    empty_notice: str,
    prompt_text: str,
    restore_action: str,
    restore_payload: dict[str, Any],
    cancel_notice: str,
    status_success_notice: str | None = None,
    source_restore_action: str | None = None,
    source_restore_payload: dict[str, Any] | None = None,
    source_success_notice: str | None = None,
) -> None:
    pending_payload: dict[str, Any] = {
        "items": items,
        "prompt_label": prompt_label,
        "empty_notice": empty_notice,
    }
    if str(restore_payload.get("back_target", "none")) == "status" and query.message is not None:
        pending_payload["back_target"] = "status"
        pending_payload["source_message"] = query.message
        if status_success_notice is not None:
            pending_payload["status_success_notice"] = status_success_notice
    elif source_restore_action is not None and query.message is not None:
        pending_payload["source_message"] = query.message
        pending_payload["source_restore_action"] = source_restore_action
        pending_payload["source_restore_payload"] = (
            {} if source_restore_payload is None else dict(source_restore_payload)
        )
        if source_success_notice is not None:
            pending_payload["source_success_notice"] = source_success_notice
    ui_state.set_pending_text_action(
        user_id,
        "context_items_agent_prompt",
        **pending_payload,
    )
    await _edit_query_message(
        query,
        prompt_text,
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    _callback_button(
                        ui_state,
                        user_id,
                        "Cancel Ask",
                        "context_items_ask_cancel",
                        restore_action=restore_action,
                        restore_payload=restore_payload,
                        notice=cancel_notice,
                    )
                ]
            ]
        ),
    )


async def _restore_context_items_source_from_callback(
    query,
    services,
    ui_state: TelegramUiState,
    *,
    user_id: int,
    restore_action: str,
    restore_payload: dict[str, Any],
    notice: str | None = None,
) -> None:
    if restore_action == "runtime_status_page":
        await _show_runtime_status_from_callback(
            query,
            services,
            ui_state,
            user_id=user_id,
            notice=notice,
        )
        return

    if restore_action == "workspace_page":
        await _show_workspace_listing_from_callback(
            query,
            services,
            ui_state,
            user_id=user_id,
            relative_path=str(restore_payload.get("relative_path", "")),
            page=int(restore_payload.get("page", 0)),
            back_target=str(restore_payload.get("back_target", "none")),
            notice=notice,
        )
        return

    if restore_action == "workspace_search_page":
        await _show_workspace_search_results_from_callback(
            query,
            services,
            ui_state,
            user_id=user_id,
            query_text=str(restore_payload["query_text"]),
            page=int(restore_payload.get("page", 0)),
            back_target=str(restore_payload.get("back_target", "none")),
            notice=notice,
        )
        return

    if restore_action == "workspace_file_preview_dir":
        await _show_workspace_file_preview_from_callback(
            query,
            services,
            ui_state,
            user_id=user_id,
            relative_path=str(restore_payload["relative_path"]),
            page=int(restore_payload.get("page", 0)),
            back_target=str(restore_payload.get("back_target", "none")),
        )
        return

    if restore_action == "workspace_file_preview_search":
        await _show_workspace_search_file_preview_from_callback(
            query,
            services,
            ui_state,
            user_id=user_id,
            relative_path=str(restore_payload["relative_path"]),
            query_text=str(restore_payload["query_text"]),
            page=int(restore_payload.get("page", 0)),
            back_target=str(restore_payload.get("back_target", "none")),
        )
        return

    if restore_action == "workspace_changes_page":
        await _show_workspace_changes_from_callback(
            query,
            services,
            ui_state,
            user_id=user_id,
            page=int(restore_payload.get("page", 0)),
            back_target=str(restore_payload.get("back_target", "none")),
            notice=notice,
        )
        return

    if restore_action == "workspace_change_preview":
        await _show_workspace_change_preview_from_callback(
            query,
            services,
            ui_state,
            user_id=user_id,
            relative_path=str(restore_payload["relative_path"]),
            status_code=str(restore_payload["status_code"]),
            page=int(restore_payload.get("page", 0)),
            back_target=str(restore_payload.get("back_target", "none")),
        )
        return

    if restore_action == "workspace_changes_follow_up":
        await _show_workspace_changes_follow_up_from_callback(
            query,
            services,
            ui_state,
            user_id=user_id,
            notice=notice,
        )
        return

    if restore_action == "context_bundle_page":
        await _show_context_bundle_from_callback(
            query,
            services,
            ui_state,
            user_id=user_id,
            page=int(restore_payload.get("page", 0)),
            back_target=str(restore_payload.get("back_target", "none")),
            notice=notice,
            source_restore_action=(
                None
                if restore_payload.get("source_restore_action") is None
                else str(restore_payload["source_restore_action"])
            ),
            source_restore_payload=(
                dict(restore_payload["source_restore_payload"])
                if isinstance(restore_payload.get("source_restore_payload"), dict)
                else None
            ),
            source_back_label=(
                None
                if restore_payload.get("source_back_label") is None
                else str(restore_payload["source_back_label"])
            ),
        )
        return

    await _edit_query_message(query, notice or "Request cancelled.")


async def _start_new_session(update: Update, services, ui_state: TelegramUiState, *, application) -> None:
    if update.message is None:
        return
    if not _is_authorized(update, services):
        await _reply_unauthorized(update)
        return

    try:
        state, session = await _with_active_store(
            services,
            lambda store: store.reset(update.effective_user.id),
        )
        await session.ensure_started()
    except Exception:
        await _reply_session_creation_failed(update, services)
        return

    try:
        await state.session_store.record_session_usage(
            update.effective_user.id,
            session,
            title_hint=None,
        )
    except Exception:
        pass
    ui_state.invalidate_session_bound_interactions()
    await _sync_agent_commands_for_session(
        application,
        ui_state,
        update.effective_user.id,
        session,
    )

    await _reply_with_menu(
        update.message,
        services,
        update.effective_user.id,
        (
            f"Started new session: {session.session_id}\n"
            "Old bot buttons and pending inputs tied to the previous session were cleared."
        ),
    )


async def _start_new_session_from_callback(
    query,
    services,
    ui_state: TelegramUiState,
    *,
    user_id: int,
    application,
    back_target: str = "none",
) -> None:
    try:
        state, session = await _with_active_store(
            services,
            lambda store: store.reset(user_id),
        )
        await session.ensure_started()
    except Exception:
        if back_target == "status":
            await _show_runtime_status_from_callback(
                query,
                services,
                ui_state,
                user_id=user_id,
                notice="session creation failed",
            )
            return
        await _edit_query_message(query, "session creation failed")
        return

    try:
        await state.session_store.record_session_usage(
            user_id,
            session,
            title_hint=None,
        )
    except Exception:
        pass
    ui_state.invalidate_session_bound_interactions()
    await _sync_agent_commands_for_session(
        application,
        ui_state,
        user_id,
        session,
    )

    success_text = (
        f"Started new session: {session.session_id}\n"
        "Old bot buttons and pending inputs tied to the previous session were cleared."
    )
    if back_target == "status":
        await _show_runtime_status_from_callback(
            query,
            services,
            ui_state,
            user_id=user_id,
            notice=success_text,
        )
        return
    await _edit_query_message(query, success_text)


async def _restart_agent(update: Update, services, ui_state: TelegramUiState, *, application) -> None:
    if update.message is None:
        return
    if not _is_authorized(update, services):
        await _reply_unauthorized(update)
        return

    try:
        state, session = await _with_active_store(
            services,
            lambda store: store.restart(update.effective_user.id),
        )
        await session.ensure_started()
    except Exception:
        await _reply_session_creation_failed(update, services)
        return

    try:
        await state.session_store.record_session_usage(
            update.effective_user.id,
            session,
            title_hint=None,
        )
    except Exception:
        pass
    ui_state.invalidate_session_bound_interactions()
    await _sync_agent_commands_for_session(
        application,
        ui_state,
        update.effective_user.id,
        session,
    )

    await _reply_with_menu(
        update.message,
        services,
        update.effective_user.id,
        (
            f"Restarted agent: {session.session_id}\n"
            "Old bot buttons and pending inputs tied to the previous session were cleared."
        ),
    )


async def _restart_agent_from_callback(
    query,
    services,
    ui_state: TelegramUiState,
    *,
    user_id: int,
    application,
    back_target: str = "none",
) -> None:
    try:
        state, session = await _with_active_store(
            services,
            lambda store: store.restart(user_id),
        )
        await session.ensure_started()
    except Exception:
        if back_target == "status":
            await _show_runtime_status_from_callback(
                query,
                services,
                ui_state,
                user_id=user_id,
                notice="session creation failed",
            )
            return
        await _edit_query_message(query, "session creation failed")
        return

    try:
        await state.session_store.record_session_usage(
            user_id,
            session,
            title_hint=None,
        )
    except Exception:
        pass
    ui_state.invalidate_session_bound_interactions()
    await _sync_agent_commands_for_session(
        application,
        ui_state,
        user_id,
        session,
    )

    success_text = (
        f"Restarted agent: {session.session_id}\n"
        "Old bot buttons and pending inputs tied to the previous session were cleared."
    )
    if back_target == "status":
        await _show_runtime_status_from_callback(
            query,
            services,
            ui_state,
            user_id=user_id,
            notice=success_text,
        )
        return
    await _edit_query_message(query, success_text)


async def _show_switch_agent_menu(update: Update, services, ui_state: TelegramUiState) -> None:
    if update.message is None:
        return
    if not _is_authorized(update, services):
        await _reply_unauthorized(update)
        return
    if not _is_admin(update, services):
        await _reply_unauthorized(update)
        return

    state = await services.snapshot_runtime_state()
    capability_summaries = await _discover_provider_capabilities_for_switch_menu(
        services,
        workspace_id=state.workspace_id,
    )
    user_id = update.effective_user.id
    replay_turn = ui_state.get_last_turn(
        user_id,
        state.provider,
        state.workspace_id,
    )
    text, markup = _build_switch_agent_view(
        state=state,
        services=services,
        capability_summaries=capability_summaries,
        user_id=user_id,
        ui_state=ui_state,
        replay_turn=replay_turn,
    )
    await update.message.reply_text(
        text,
        reply_markup=markup,
    )


async def _show_switch_workspace_menu(update: Update, services, ui_state: TelegramUiState) -> None:
    if update.message is None:
        return
    if not _is_authorized(update, services):
        await _reply_unauthorized(update)
        return
    if not _is_admin(update, services):
        await _reply_unauthorized(update)
        return

    state = await services.snapshot_runtime_state()
    text, markup = _build_switch_workspace_view(
        state=state,
        services=services,
        user_id=update.effective_user.id,
        ui_state=ui_state,
    )
    await update.message.reply_text(
        text,
        reply_markup=markup,
    )


async def _show_session_history(
    update: Update,
    services,
    ui_state: TelegramUiState,
    *,
    page: int,
) -> None:
    if update.message is None:
        return
    if not _is_authorized(update, services):
        await _reply_unauthorized(update)
        return

    try:
        state, history_state = await _with_active_store(
            services,
            lambda store: _load_history_view_state(store, update.effective_user.id),
        )
    except Exception:
        await _reply_request_failed(update, services)
        return

    text, markup = _build_history_view(
        entries=history_state.entries,
        provider=state.provider,
        workspace_id=state.workspace_id,
        workspace_label=_workspace_label(services, state.workspace_id),
        user_id=update.effective_user.id,
        page=page,
        ui_state=ui_state,
        active_session_id=history_state.active_session_id,
        show_provider_sessions=update.effective_user.id == services.admin_user_id,
    )
    await update.message.reply_text(text, reply_markup=markup)


async def _show_session_history_from_callback(
    query,
    services,
    ui_state: TelegramUiState,
    *,
    user_id: int,
    page: int,
    back_target: str = "none",
    notice: str | None = None,
) -> None:
    state, history_state = await _with_active_store(
        services,
        lambda store: _load_history_view_state(store, user_id),
    )
    text, markup = _build_history_view(
        entries=history_state.entries,
        provider=state.provider,
        workspace_id=state.workspace_id,
        workspace_label=_workspace_label(services, state.workspace_id),
        user_id=user_id,
        page=page,
        ui_state=ui_state,
        active_session_id=history_state.active_session_id,
        notice=notice,
        show_provider_sessions=user_id == services.admin_user_id,
        back_target=back_target,
    )
    await _edit_query_message(query, text, reply_markup=markup)


async def _show_provider_sessions_from_callback(
    query,
    services,
    ui_state: TelegramUiState,
    *,
    user_id: int,
    cursor: str | None,
    previous_cursors: tuple[str | None, ...],
    history_page: int,
    back_target: str = "history",
    history_back_target: str = "none",
    notice: str | None = None,
) -> None:
    state, provider_state = await _load_provider_sessions_view_state(
        services,
        user_id,
        cursor=cursor,
    )
    text, markup = _build_provider_sessions_view(
        entries=provider_state.entries,
        next_cursor=provider_state.next_cursor,
        supported=provider_state.supported,
        provider=state.provider,
        workspace_id=state.workspace_id,
        workspace_label=_workspace_label(services, state.workspace_id),
        user_id=user_id,
        ui_state=ui_state,
        active_session_id=provider_state.active_session_id,
        cursor=cursor,
        previous_cursors=previous_cursors,
        history_page=history_page,
        back_target=back_target,
        history_back_target=history_back_target,
        notice=notice,
    )
    await _edit_query_message(query, text, reply_markup=markup)


def _build_switch_agent_view(
    *,
    state,
    services,
    capability_summaries,
    user_id: int,
    ui_state: TelegramUiState,
    replay_turn,
    back_target: str = "none",
    notice: str | None = None,
):
    lines = []
    if notice:
        lines.append(notice)
    lines.extend(
        [
            f"Current provider: {resolve_provider_profile(state.provider).display_name}",
            f"Workspace: {_workspace_label(services, state.workspace_id)}",
            "Provider capabilities:",
        ]
    )
    buttons = []
    for profile in iter_provider_profiles():
        lines.append(
            _format_provider_capability_summary(
                profile,
                capability_summaries.get(profile.provider),
                is_current=profile.provider == state.provider,
            )
        )
        if profile.provider == state.provider:
            buttons.append(
                [
                    _callback_button(
                        ui_state,
                        user_id,
                        f"Current: {profile.display_name}",
                        "noop",
                        notice=f"Already using {profile.display_name}.",
                    )
                ]
            )
            continue
        buttons.append(
            [
                _callback_button(
                    ui_state,
                    user_id,
                    profile.display_name,
                    "switch_provider",
                    provider=profile.provider,
                    back_target=back_target,
                )
            ]
        )
        if replay_turn is not None:
            buttons.append(
                [
                    _callback_button(
                        ui_state,
                        user_id,
                        f"Retry on {profile.display_name}",
                        "switch_provider_retry_last_turn",
                        provider=profile.provider,
                        back_target=back_target,
                    ),
                    _callback_button(
                        ui_state,
                        user_id,
                        f"Fork on {profile.display_name}",
                        "switch_provider_fork_last_turn",
                        provider=profile.provider,
                        back_target=back_target,
                    ),
                ]
            )
    _append_back_to_status_button(
        buttons,
        ui_state=ui_state,
        user_id=user_id,
        back_target=back_target,
    )
    return "\n".join(lines), InlineKeyboardMarkup(buttons)


async def _show_switch_agent_menu_from_callback(
    query,
    services,
    ui_state: TelegramUiState,
    *,
    user_id: int,
    back_target: str = "none",
    notice: str | None = None,
) -> None:
    state = await services.snapshot_runtime_state()
    capability_summaries = await _discover_provider_capabilities_for_switch_menu(
        services,
        workspace_id=state.workspace_id,
    )
    replay_turn = ui_state.get_last_turn(
        user_id,
        state.provider,
        state.workspace_id,
    )
    text, markup = _build_switch_agent_view(
        state=state,
        services=services,
        capability_summaries=capability_summaries,
        user_id=user_id,
        ui_state=ui_state,
        replay_turn=replay_turn,
        back_target=back_target,
        notice=notice,
    )
    await _edit_query_message(query, text, reply_markup=markup)


def _build_switch_workspace_view(
    *,
    state,
    services,
    user_id: int,
    ui_state: TelegramUiState,
    back_target: str = "none",
    notice: str | None = None,
):
    lines = []
    if notice:
        lines.append(notice)
    lines.append(f"Current workspace: {_workspace_label(services, state.workspace_id)}")
    buttons = []
    for workspace in services.config.agent.workspaces:
        if workspace.id == state.workspace_id:
            buttons.append(
                [
                    _callback_button(
                        ui_state,
                        user_id,
                        f"Current: {workspace.label}",
                        "noop",
                        notice=f"Already in {workspace.label}.",
                    )
                ]
            )
            continue
        buttons.append(
            [
                _callback_button(
                    ui_state,
                    user_id,
                    workspace.label,
                    "switch_workspace",
                    workspace_id=workspace.id,
                    back_target=back_target,
                )
            ]
        )
    _append_back_to_status_button(
        buttons,
        ui_state=ui_state,
        user_id=user_id,
        back_target=back_target,
    )
    return "\n".join(lines), InlineKeyboardMarkup(buttons)


async def _show_switch_workspace_menu_from_callback(
    query,
    services,
    ui_state: TelegramUiState,
    *,
    user_id: int,
    back_target: str = "none",
    notice: str | None = None,
) -> None:
    state = await services.snapshot_runtime_state()
    text, markup = _build_switch_workspace_view(
        state=state,
        services=services,
        user_id=user_id,
        ui_state=ui_state,
        back_target=back_target,
        notice=notice,
    )
    await _edit_query_message(query, text, reply_markup=markup)


async def _show_model_mode_menu(update: Update, services, ui_state: TelegramUiState, *, application) -> None:
    if update.message is None:
        return
    if not _is_authorized(update, services):
        await _reply_unauthorized(update)
        return

    created_session = False
    try:
        state, session = await _with_active_store(
            services,
            lambda store: store.peek(update.effective_user.id),
        )
    except Exception:
        await _reply_request_failed(update, services)
        return

    if session is None:
        try:
            state, session = await _with_active_store(
                services,
                lambda store: _prepare_turn_session(
                    store,
                    update.effective_user.id,
                    time.monotonic(),
                ),
            )
        except Exception:
            await _reply_session_creation_failed(update, services)
            return
        created_session = True

    try:
        await session.ensure_started()
    except Exception:
        await _reply_session_creation_failed(update, services)
        return

    if created_session:
        try:
            await state.session_store.record_session_usage(
                update.effective_user.id,
                session,
                title_hint=None,
            )
        except Exception:
            pass
        await _sync_agent_commands_for_session(
            application,
            ui_state,
            update.effective_user.id,
            session,
        )

    model_selection = session.get_selection("model")
    mode_selection = session.get_selection("mode")
    if model_selection is None and mode_selection is None:
        await _reply_with_menu(
            update.message,
            services,
            update.effective_user.id,
            "Model / mode switching is not available for this agent.",
        )
        return

    text, markup = _build_model_mode_view(
        user_id=update.effective_user.id,
        session_id=session.session_id,
        model_selection=model_selection,
        mode_selection=mode_selection,
        ui_state=ui_state,
        can_retry_last_turn=ui_state.get_last_turn(
            update.effective_user.id,
            state.provider,
            state.workspace_id,
        )
        is not None,
        notice=(
            "Started session for model / mode controls."
            if created_session
            else None
        ),
    )
    await update.message.reply_text(text, reply_markup=markup)


async def _show_model_mode_menu_from_callback(
    query,
    services,
    ui_state: TelegramUiState,
    *,
    user_id: int,
    application,
    back_target: str = "none",
    notice: str | None = None,
) -> None:
    created_session = False
    try:
        state, session = await _with_active_store(
            services,
            lambda store: store.peek(user_id),
        )
    except Exception:
        if back_target == "status":
            await _show_runtime_status_from_callback(
                query,
                services,
                ui_state,
                user_id=user_id,
                notice="Failed to load model / mode controls.",
            )
            return
        raise

    if session is None:
        try:
            state, session = await _with_active_store(
                services,
                lambda store: _prepare_turn_session(
                    store,
                    user_id,
                    time.monotonic(),
                ),
            )
        except Exception:
            if back_target == "status":
                await _show_runtime_status_from_callback(
                    query,
                    services,
                    ui_state,
                    user_id=user_id,
                    notice="session creation failed",
                )
                return
            raise
        created_session = True

    try:
        await session.ensure_started()
    except Exception:
        if back_target == "status":
            await _show_runtime_status_from_callback(
                query,
                services,
                ui_state,
                user_id=user_id,
                notice="session creation failed",
            )
            return
        raise

    if created_session:
        try:
            await state.session_store.record_session_usage(
                user_id,
                session,
                title_hint=None,
            )
        except Exception:
            pass
        await _sync_agent_commands_for_session(
            application,
            ui_state,
            user_id,
            session,
        )

    model_selection = session.get_selection("model")
    mode_selection = session.get_selection("mode")
    if model_selection is None and mode_selection is None:
        buttons: list[list[InlineKeyboardButton]] = []
        _append_back_to_status_button(
            buttons,
            ui_state=ui_state,
            user_id=user_id,
            back_target=back_target,
        )
        markup = None if not buttons else InlineKeyboardMarkup(buttons)
        await _edit_query_message(
            query,
            "Model / mode switching is not available for this agent.",
            reply_markup=markup,
        )
        return

    text, markup = _build_model_mode_view(
        user_id=user_id,
        session_id=session.session_id,
        model_selection=model_selection,
        mode_selection=mode_selection,
        ui_state=ui_state,
        can_retry_last_turn=ui_state.get_last_turn(
            user_id,
            state.provider,
            state.workspace_id,
        )
        is not None,
        back_target=back_target,
        notice=notice or (
            "Started session for model / mode controls."
            if created_session
            else None
        ),
    )
    await _edit_query_message(query, text, reply_markup=markup)


async def _retry_last_turn(
    update: Update,
    services,
    ui_state: TelegramUiState,
    *,
    application,
    after_turn_success=None,
    on_prepare_failure=None,
    on_turn_failure=None,
) -> None:
    if update.message is None:
        return
    if not _is_authorized(update, services):
        await _reply_unauthorized(update)
        return

    try:
        state = await services.snapshot_runtime_state()
    except Exception:
        await _reply_request_failed(update, services)
        return

    replay_turn = ui_state.get_last_turn(
        update.effective_user.id,
        state.provider,
        state.workspace_id,
    )
    if replay_turn is None:
        await _reply_with_menu(
            update.message,
            services,
            update.effective_user.id,
            "No previous turn is available to retry for the current provider and workspace.",
        )
        return

    await _run_agent_replay_turn_on_message(
        update.message,
        update.effective_user.id,
        services,
        ui_state,
        replay_turn,
        application=application,
        after_turn_success=after_turn_success,
        on_prepare_failure=on_prepare_failure,
        on_turn_failure=on_turn_failure,
    )


async def _fork_last_turn(
    update: Update,
    services,
    ui_state: TelegramUiState,
    *,
    application,
    after_turn_success=None,
    on_session_creation_failed=None,
    on_turn_failure=None,
) -> None:
    if update.message is None:
        return
    if not _is_authorized(update, services):
        await _reply_unauthorized(update)
        return

    try:
        state = await services.snapshot_runtime_state()
    except Exception:
        await _reply_request_failed(update, services)
        return

    replay_turn = ui_state.get_last_turn(
        update.effective_user.id,
        state.provider,
        state.workspace_id,
    )
    if replay_turn is None:
        await _reply_with_menu(
            update.message,
            services,
            update.effective_user.id,
            "No previous turn is available to fork for the current provider and workspace.",
        )
        return

    try:
        state, session = await _with_active_store(
            services,
            lambda store: store.reset(update.effective_user.id),
        )
        await session.ensure_started()
    except Exception:
        if on_session_creation_failed is not None:
            try:
                await on_session_creation_failed()
                return
            except Exception:
                pass
        await _reply_session_creation_failed(update, services)
        return

    ui_state.invalidate_session_bound_interactions()
    await _sync_agent_commands_for_session(
        application,
        ui_state,
        update.effective_user.id,
        session,
    )

    effective_replay_turn = replay_turn

    async def _run(session, stream, state):
        nonlocal effective_replay_turn
        effective_replay_turn = _coerce_replay_turn_for_capabilities(
            replay_turn,
            getattr(session, "capabilities", None),
            provider=state.provider,
            workspace_id=state.workspace_id,
            workspace_path=state.workspace_path,
        )
        ui_state.set_last_turn(
            update.effective_user.id,
            effective_replay_turn,
        )
        return await session.run_prompt(
            effective_replay_turn.prompt_items,
            stream,
        )

    async def _after_success(state):
        for item in effective_replay_turn.saved_context_items:
            ui_state.add_context_item(
                update.effective_user.id,
                state.provider,
                state.workspace_id,
                item,
            )

    await _run_agent_session_turn_with_prepared_session_on_message(
        update.message,
        update.effective_user.id,
        services,
        ui_state,
        state=state,
        session=session,
        title_hint=replay_turn.title_hint,
        application=application,
        turn_runner=_run,
        after_success=_after_success,
        after_turn_success=after_turn_success,
        on_turn_failure=on_turn_failure,
    )


async def _switch_provider_from_callback(
    query,
    services,
    ui_state: TelegramUiState,
    *,
    provider: str,
    application,
    replay_action: str | None = None,
    back_target: str = "none",
) -> None:
    if query.from_user is None or query.from_user.id != services.admin_user_id:
        await query.answer("Unauthorized user.", show_alert=True)
        return

    target_name = resolve_provider_profile(provider).display_name
    await query.answer()
    await _edit_query_message(query, f"Switching to {target_name}...")
    try:
        switched = await asyncio.wait_for(
            services.switch_provider(provider),
            CALLBACK_OPERATION_TIMEOUT_SECONDS,
        )
    except Exception:
        if back_target == "status":
            await _show_switch_agent_menu_from_callback(
                query,
                services,
                ui_state,
                user_id=query.from_user.id,
                back_target=back_target,
                notice="session creation failed",
            )
            return
        await _edit_query_message(query, "session creation failed")
        return

    ui_state.invalidate_runtime_bound_interactions()
    state = await services.snapshot_runtime_state()
    success_text = (
        f"Switched agent to {resolve_provider_profile(switched).display_name} "
        f"in {_workspace_label(services, state.workspace_id)}. "
        "Old bot buttons and pending inputs were cleared."
    )
    if replay_action == "retry_last_turn":
        await _edit_query_message(
            query,
            f"{success_text}\nRetrying last turn on the new agent...",
        )
        if query.message is None:
            return

        async def _after_retry_success(state, session) -> None:
            if back_target == "status":
                await _show_runtime_status_from_callback(
                    query,
                    services,
                    ui_state,
                    user_id=query.from_user.id,
                    notice=f"{success_text}\nRetried last turn on the new agent.",
                )

        async def _on_retry_prepare_failure() -> None:
            if back_target == "status":
                await _show_runtime_status_from_callback(
                    query,
                    services,
                    ui_state,
                    user_id=query.from_user.id,
                    notice=f"{success_text}\nRequest failed.",
                )

        async def _on_retry_turn_failure() -> None:
            if back_target == "status":
                await _show_runtime_status_from_callback(
                    query,
                    services,
                    ui_state,
                    user_id=query.from_user.id,
                    notice=f"{success_text}\nRequest failed.",
                )

        await _retry_last_turn(
            _message_update_from_callback(query),
            services,
            ui_state,
            application=application,
            after_turn_success=_after_retry_success if back_target == "status" else None,
            on_prepare_failure=_on_retry_prepare_failure if back_target == "status" else None,
            on_turn_failure=_on_retry_turn_failure if back_target == "status" else None,
        )
        return
    if replay_action == "fork_last_turn":
        await _edit_query_message(
            query,
            f"{success_text}\nForking last turn on the new agent...",
        )
        if query.message is None:
            return

        async def _after_fork_success(state, session) -> None:
            if back_target == "status":
                await _show_runtime_status_from_callback(
                    query,
                    services,
                    ui_state,
                    user_id=query.from_user.id,
                    notice=f"{success_text}\nForked last turn on the new agent.",
                )

        async def _on_fork_session_creation_failed() -> None:
            if back_target == "status":
                await _show_runtime_status_from_callback(
                    query,
                    services,
                    ui_state,
                    user_id=query.from_user.id,
                    notice=f"{success_text}\nsession creation failed",
                )

        async def _on_fork_turn_failure() -> None:
            if back_target == "status":
                await _show_runtime_status_from_callback(
                    query,
                    services,
                    ui_state,
                    user_id=query.from_user.id,
                    notice=f"{success_text}\nRequest failed.",
                )

        await _fork_last_turn(
            _message_update_from_callback(query),
            services,
            ui_state,
            application=application,
            after_turn_success=_after_fork_success if back_target == "status" else None,
            on_session_creation_failed=(
                _on_fork_session_creation_failed if back_target == "status" else None
            ),
            on_turn_failure=_on_fork_turn_failure if back_target == "status" else None,
        )
        return

    if back_target == "status":
        await _show_runtime_status_from_callback(
            query,
            services,
            ui_state,
            user_id=query.from_user.id,
            notice=success_text,
        )
        return

    await _edit_query_message(query, success_text)


async def _switch_history_session_from_callback(
    query,
    services,
    ui_state: TelegramUiState,
    *,
    user_id: int,
    session_id: str,
    application,
    replay_after_switch: bool = False,
    page: int = 0,
    back_target: str = "none",
    restore_status_on_failure: bool = False,
) -> None:
    await query.answer()
    await _edit_query_message(query, "Switching to session...")
    try:
        state, session = await asyncio.wait_for(
            _with_active_store(
                services,
                lambda store: store.activate_history_session(user_id, session_id),
            ),
            CALLBACK_OPERATION_TIMEOUT_SECONDS,
        )
    except Exception:
        if restore_status_on_failure and back_target == "status":
            try:
                await _show_runtime_status_from_callback(
                    query,
                    services,
                    ui_state,
                    user_id=user_id,
                    notice="Failed to switch session.",
                )
                return
            except Exception:
                pass
        if back_target != "none":
            try:
                await _show_session_history_from_callback(
                    query,
                    services,
                    ui_state,
                    user_id=user_id,
                    page=page,
                    back_target=back_target,
                    notice="Failed to switch session.",
                )
                return
            except Exception:
                pass
        await _edit_query_message(query, "Failed to switch session.")
        return
    ui_state.invalidate_session_bound_interactions()
    await _sync_agent_commands_for_session(
        application,
        ui_state,
        user_id,
        session,
    )
    success_text = (
        f"Switched to session {session.session_id} on "
        f"{resolve_provider_profile(state.provider).display_name} in "
        f"{_workspace_label(services, state.workspace_id)}. "
        "Old bot buttons and pending inputs tied to the previous session were cleared."
    )
    if replay_after_switch:
        await _edit_query_message(
            query,
            f"{success_text}\nRetrying last turn in this session...",
        )
        if query.message is None:
            return
        await _retry_last_turn(
            _message_update_from_callback(query),
            services,
            ui_state,
            application=application,
        )
        if back_target == "status":
            await _show_runtime_status_from_callback(
                query,
                services,
                ui_state,
                user_id=user_id,
                notice=f"{success_text}\nRetried last turn in this session.",
            )
            return
        return
    if back_target == "status":
        await _show_runtime_status_from_callback(
            query,
            services,
            ui_state,
            user_id=user_id,
            notice=success_text,
        )
        return
    await _edit_query_message(query, success_text)


async def _switch_provider_session_from_callback(
    query,
    services,
    ui_state: TelegramUiState,
    *,
    user_id: int,
    payload: dict[str, Any],
    application,
    replay_after_switch: bool = False,
) -> None:
    if query.from_user is None or query.from_user.id != services.admin_user_id:
        await query.answer("Unauthorized user.", show_alert=True)
        return
    await query.answer()
    await _edit_query_message(query, "Switching to provider session...")
    back_target = str(payload.get("back_target", "history"))
    history_back_target = str(payload.get("history_back_target", "none"))
    try:
        _, session = await asyncio.wait_for(
            _with_active_store(
                services,
                lambda store: store.activate_provider_session(
                    user_id,
                    payload["session_id"],
                    title_hint=payload.get("title"),
                ),
            ),
            CALLBACK_OPERATION_TIMEOUT_SECONDS,
        )
    except Exception:
        try:
            await _show_provider_sessions_from_callback(
                query,
                services,
                ui_state,
                user_id=user_id,
                cursor=payload.get("cursor"),
                previous_cursors=tuple(payload.get("previous_cursors", ())),
                history_page=int(payload.get("history_page", 0)),
                back_target=back_target,
                history_back_target=history_back_target,
                notice="Failed to switch provider session.",
            )
        except Exception:
            await _edit_query_message(query, "Failed to switch provider session.")
        return

    ui_state.invalidate_session_bound_interactions()
    await _sync_agent_commands_for_session(
        application,
        ui_state,
        user_id,
        session,
    )
    success_text = (
        f"Switched to provider session {payload['session_id']}. "
        "Old bot buttons and pending inputs tied to the previous session were cleared."
    )
    if replay_after_switch:
        await _edit_query_message(
            query,
            f"{success_text}\nRetrying last turn in this session...",
        )
        if query.message is None:
            return
        await _retry_last_turn(
            _message_update_from_callback(query),
            services,
            ui_state,
            application=application,
        )
        if back_target == "status":
            await _show_runtime_status_from_callback(
                query,
                services,
                ui_state,
                user_id=user_id,
                notice=f"{success_text}\nRetried last turn in this session.",
            )
            return
        return
    if back_target == "status":
        await _show_runtime_status_from_callback(
            query,
            services,
            ui_state,
            user_id=user_id,
            notice=success_text,
        )
        return
    await _show_provider_sessions_from_callback(
        query,
        services,
        ui_state,
        user_id=user_id,
        cursor=payload.get("cursor"),
        previous_cursors=tuple(payload.get("previous_cursors", ())),
        history_page=int(payload.get("history_page", 0)),
        back_target=back_target,
        history_back_target=history_back_target,
        notice=success_text,
    )


async def _set_selection_from_callback(
    query,
    services,
    ui_state: TelegramUiState,
    *,
    user_id: int,
    kind: str,
    value: str,
    application,
    retry_after_update: bool = False,
    back_target: str = "none",
) -> None:
    await query.answer()
    state, session = await _with_active_store(
        services,
        lambda store: store.peek(user_id),
    )
    if session is None:
        await _edit_query_message(query, "No active session.")
        return
    try:
        await session.set_selection(kind, value)
    except Exception:
        await _edit_query_message(query, "Failed to update model / mode.")
        return
    try:
        await state.session_store.record_session_usage(
            user_id,
            session,
            title_hint=None,
        )
    except Exception:
        pass
    await _sync_agent_commands_for_session(
        application,
        ui_state,
        user_id,
        session,
    )

    updated_notice = (
        f"Updated {kind} to "
        f"{_current_choice_label(session.get_selection(kind))}."
    )
    replay_turn = ui_state.get_last_turn(
        user_id,
        state.provider,
        state.workspace_id,
    )
    if retry_after_update:
        if replay_turn is None:
            text, markup = _build_model_mode_view(
                user_id=user_id,
                session_id=session.session_id,
                model_selection=session.get_selection("model"),
                mode_selection=session.get_selection("mode"),
                ui_state=ui_state,
                can_retry_last_turn=False,
                back_target=back_target,
                notice=(
                    f"{updated_notice}\n"
                    "No previous turn is available to retry in the current workspace."
                ),
            )
            await _edit_query_message(query, text, reply_markup=markup)
            return
        await _edit_query_message(
            query,
            f"{updated_notice}\nRetrying last turn with the updated setting...",
        )
        if query.message is None:
            return
        await _run_agent_replay_turn_on_message(
            query.message,
            user_id,
            services,
            ui_state,
            replay_turn,
            application=application,
        )
        if back_target == "status":
            await _show_runtime_status_from_callback(
                query,
                services,
                ui_state,
                user_id=user_id,
                notice=f"{updated_notice}\nRetried last turn with the updated setting.",
            )
            return
        return

    text, markup = _build_model_mode_view(
        user_id=user_id,
        session_id=session.session_id,
        model_selection=session.get_selection("model"),
        mode_selection=session.get_selection("mode"),
        ui_state=ui_state,
        can_retry_last_turn=replay_turn is not None,
        back_target=back_target,
        notice=updated_notice,
    )
    await _edit_query_message(query, text, reply_markup=markup)


async def _set_selection_from_status_callback(
    query,
    services,
    ui_state: TelegramUiState,
    *,
    user_id: int,
    kind: str,
    value: str,
    application,
) -> None:
    state, session = await _with_active_store(
        services,
        lambda store: store.peek(user_id),
    )
    if session is None:
        await _show_runtime_status_from_callback(
            query,
            services,
            ui_state,
            user_id=user_id,
            notice="No active session.",
        )
        return
    try:
        updated_selection = await session.set_selection(kind, value)
    except Exception:
        await _show_runtime_status_from_callback(
            query,
            services,
            ui_state,
            user_id=user_id,
            notice="Failed to update model / mode.",
        )
        return
    try:
        await state.session_store.record_session_usage(
            user_id,
            session,
            title_hint=None,
        )
    except Exception:
        pass
    await _sync_agent_commands_for_session(
        application,
        ui_state,
        user_id,
        session,
    )
    selection = updated_selection if updated_selection is not None else session.get_selection(kind)
    await _show_runtime_status_from_callback(
        query,
        services,
        ui_state,
        user_id=user_id,
        notice=f"Updated {kind} to {_current_choice_label(selection)}.",
    )


async def _set_selection_retry_from_status_callback(
    query,
    services,
    ui_state: TelegramUiState,
    *,
    user_id: int,
    kind: str,
    value: str,
    application,
) -> None:
    state, session = await _with_active_store(
        services,
        lambda store: store.peek(user_id),
    )
    if session is None:
        await _show_runtime_status_from_callback(
            query,
            services,
            ui_state,
            user_id=user_id,
            notice="No active session.",
        )
        return
    try:
        updated_selection = await session.set_selection(kind, value)
    except Exception:
        await _show_runtime_status_from_callback(
            query,
            services,
            ui_state,
            user_id=user_id,
            notice="Failed to update model / mode.",
        )
        return
    try:
        await state.session_store.record_session_usage(
            user_id,
            session,
            title_hint=None,
        )
    except Exception:
        pass
    await _sync_agent_commands_for_session(
        application,
        ui_state,
        user_id,
        session,
    )

    selection = updated_selection if updated_selection is not None else session.get_selection(kind)
    updated_notice = f"Updated {kind} to {_current_choice_label(selection)}."
    replay_turn = ui_state.get_last_turn(
        user_id,
        state.provider,
        state.workspace_id,
    )
    if replay_turn is None:
        await _show_runtime_status_from_callback(
            query,
            services,
            ui_state,
            user_id=user_id,
            notice=(
                f"{updated_notice}\n"
                "No previous turn is available to retry in the current workspace."
            ),
        )
        return
    await _edit_query_message(
        query,
        f"{updated_notice}\nRetrying last turn with the updated setting...",
    )
    if query.message is None:
        return

    async def _after_retry_success(_state, _session) -> None:
        await _show_runtime_status_from_callback(
            query,
            services,
            ui_state,
            user_id=user_id,
            notice=f"{updated_notice}\nRetried last turn with the updated setting.",
        )

    async def _on_retry_prepare_failure() -> None:
        await _show_runtime_status_from_callback(
            query,
            services,
            ui_state,
            user_id=user_id,
            notice=f"{updated_notice}\nRequest failed.",
        )

    async def _on_retry_turn_failure() -> None:
        await _show_runtime_status_from_callback(
            query,
            services,
            ui_state,
            user_id=user_id,
            notice=f"{updated_notice}\nRequest failed.",
        )

    await _run_agent_replay_turn_on_message(
        query.message,
        user_id,
        services,
        ui_state,
        replay_turn,
        application=application,
        after_turn_success=_after_retry_success,
        on_prepare_failure=_on_retry_prepare_failure,
        on_turn_failure=_on_retry_turn_failure,
    )


async def _dispatch_callback_action(
    query,
    services,
    ui_state: TelegramUiState,
    callback_action,
    *,
    application,
):
    action = callback_action.action
    payload = callback_action.payload
    user_id = callback_action.user_id

    if action == "noop":
        await query.answer(payload.get("notice", "Already selected."))
        return

    if action == "restore_source_view":
        await query.answer()
        await _restore_context_items_source_from_callback(
            query,
            services,
            ui_state,
            user_id=user_id,
            restore_action=str(payload.get("restore_action", "")),
            restore_payload=dict(payload.get("restore_payload", {})),
        )
        return

    if action == "recover_retry_last_turn":
        await query.answer()
        if query.message is None or query.from_user is None:
            return
        await _retry_last_turn(
            _message_update_from_callback(query),
            services,
            ui_state,
            application=application,
        )
        return

    if action == "recover_fork_last_turn":
        await query.answer()
        if query.message is None or query.from_user is None:
            return
        await _fork_last_turn(
            _message_update_from_callback(query),
            services,
            ui_state,
            application=application,
        )
        return

    if action == "recover_new_session":
        await query.answer()
        if query.message is None or query.from_user is None:
            return
        await _start_new_session(
            _message_update_from_callback(query),
            services,
            ui_state,
            application=application,
        )
        return

    if action == "recover_session_history":
        await query.answer()
        if query.message is None or query.from_user is None:
            return
        await _show_session_history(
            _message_update_from_callback(query),
            services,
            ui_state,
            page=0,
        )
        return

    if action == "recover_model_mode":
        await query.answer()
        if query.message is None or query.from_user is None:
            return
        await _show_model_mode_menu(
            _message_update_from_callback(query),
            services,
            ui_state,
            application=application,
        )
        return

    if action == "recover_switch_agent":
        await query.answer()
        if query.message is None or query.from_user is None:
            return
        await _show_switch_agent_menu(
            _message_update_from_callback(query),
            services,
            ui_state,
        )
        return

    if action == "recover_switch_workspace":
        await query.answer()
        if query.message is None or query.from_user is None:
            return
        await _show_switch_workspace_menu(
            _message_update_from_callback(query),
            services,
            ui_state,
        )
        return

    if action == "runtime_status_page":
        await query.answer()
        try:
            await _show_runtime_status_from_callback(
                query,
                services,
                ui_state,
                user_id=user_id,
            )
        except Exception:
            await _edit_query_message(query, "Failed to load bot status.")
        return

    if action == "runtime_status_open":
        await query.answer()
        target = str(payload.get("target", ""))
        ui_state.clear_pending_text_action(user_id)
        try:
            if target == "history":
                await _show_session_history_from_callback(
                    query,
                    services,
                    ui_state,
                    user_id=user_id,
                    page=0,
                    back_target="status",
                )
                return
            if target == "commands":
                await _show_agent_commands_menu_from_callback(
                    query,
                    services,
                    ui_state,
                    user_id=user_id,
                    page=0,
                    back_target="status",
                )
                return
            if target == "provider_sessions":
                if query.from_user is None or query.from_user.id != services.admin_user_id:
                    await query.answer("Unauthorized user.", show_alert=True)
                    return
                await _show_provider_sessions_from_callback(
                    query,
                    services,
                    ui_state,
                    user_id=user_id,
                    cursor=None,
                    previous_cursors=(),
                    history_page=0,
                    back_target="status",
                    history_back_target="status",
                )
                return
            if target == "files":
                await _show_workspace_listing_from_callback(
                    query,
                    services,
                    ui_state,
                    user_id=user_id,
                    relative_path="",
                    page=0,
                    back_target="status",
                )
                return
            if target == "search":
                pending_payload: dict[str, Any] = {"back_target": "status"}
                if query.message is not None:
                    pending_payload["source_message"] = query.message
                ui_state.set_pending_text_action(
                    user_id,
                    "workspace_search",
                    **pending_payload,
                )
                await _edit_query_message(
                    query,
                    "Send your workspace search query as the next plain text message.",
                    reply_markup=InlineKeyboardMarkup(
                        [
                            [
                                _callback_button(
                                    ui_state,
                                    user_id,
                                    "Cancel Search",
                                    "runtime_status_search_cancel",
                                )
                            ]
                        ]
                    ),
                )
                return
            if target == "changes":
                await _show_workspace_changes_from_callback(
                    query,
                    services,
                    ui_state,
                    user_id=user_id,
                    page=0,
                    back_target="status",
                )
                return
            if target == "bundle":
                await _show_context_bundle_from_callback(
                    query,
                    services,
                    ui_state,
                    user_id=user_id,
                    page=0,
                    back_target="status",
                )
                return
        except Exception:
            await _edit_query_message(query, "Failed to open the selected view.")
            return
        return

    if action == "runtime_status_control":
        await query.answer()
        ui_state.clear_pending_text_action(user_id)
        if query.message is None or query.from_user is None:
            return
        target = str(payload.get("target", ""))
        update = _message_update_from_callback(query)
        if target == "new_session":
            await _start_new_session_from_callback(
                query,
                services,
                ui_state,
                user_id=user_id,
                application=application,
                back_target="status",
            )
            return
        if target == "retry_last_turn":
            await query.answer()

            async def _after_retry_success(state, session) -> None:
                await _show_runtime_status_from_callback(
                    query,
                    services,
                    ui_state,
                    user_id=user_id,
                    notice="Retried last turn.",
                )

            async def _on_retry_prepare_failure() -> None:
                await _show_runtime_status_from_callback(
                    query,
                    services,
                    ui_state,
                    user_id=user_id,
                    notice="Request failed.",
                )

            async def _on_retry_turn_failure() -> None:
                await _show_runtime_status_from_callback(
                    query,
                    services,
                    ui_state,
                    user_id=user_id,
                    notice="Request failed.",
                )

            await _retry_last_turn(
                update,
                services,
                ui_state,
                application=application,
                after_turn_success=_after_retry_success,
                on_prepare_failure=_on_retry_prepare_failure,
                on_turn_failure=_on_retry_turn_failure,
            )
            return
        if target == "fork_last_turn":
            await query.answer()

            async def _after_fork_success(state, session) -> None:
                await _show_runtime_status_from_callback(
                    query,
                    services,
                    ui_state,
                    user_id=user_id,
                    notice="Forked last turn into a new session.",
                )

            async def _on_fork_session_creation_failed() -> None:
                await _show_runtime_status_from_callback(
                    query,
                    services,
                    ui_state,
                    user_id=user_id,
                    notice="session creation failed",
                )

            async def _on_fork_turn_failure() -> None:
                await _show_runtime_status_from_callback(
                    query,
                    services,
                    ui_state,
                    user_id=user_id,
                    notice="Request failed.",
                )

            await _fork_last_turn(
                update,
                services,
                ui_state,
                application=application,
                after_turn_success=_after_fork_success,
                on_session_creation_failed=_on_fork_session_creation_failed,
                on_turn_failure=_on_fork_turn_failure,
            )
            return
        if target == "selection_quick":
            await _set_selection_from_status_callback(
                query,
                services,
                ui_state,
                user_id=user_id,
                kind=str(payload.get("kind", "")),
                value=str(payload.get("value", "")),
                application=application,
            )
            return
        if target == "selection_retry_quick":
            await _set_selection_retry_from_status_callback(
                query,
                services,
                ui_state,
                user_id=user_id,
                kind=str(payload.get("kind", "")),
                value=str(payload.get("value", "")),
                application=application,
            )
            return
        if target == "history_session_quick_switch":
            await _switch_history_session_from_callback(
                query,
                services,
                ui_state,
                user_id=user_id,
                session_id=str(payload.get("session_id", "")),
                application=application,
                back_target="status",
                restore_status_on_failure=True,
            )
            return
        if target == "history_session_quick_retry":
            await _switch_history_session_from_callback(
                query,
                services,
                ui_state,
                user_id=user_id,
                session_id=str(payload.get("session_id", "")),
                application=application,
                replay_after_switch=True,
                back_target="status",
                restore_status_on_failure=True,
            )
            return
        if target == "agent_command_quick":
            command_name = str(payload.get("command_name", "")).strip()
            if not command_name:
                await _show_runtime_status_from_callback(
                    query,
                    services,
                    ui_state,
                    user_id=user_id,
                    notice="No agent command is available.",
                )
                return
            hint = str(payload.get("hint", "") or "").strip()
            if hint:
                ui_state.set_pending_text_action(
                    user_id,
                    "run_agent_command",
                    command_name=command_name,
                    hint=hint,
                    back_target="status",
                    source_message=query.message,
                    status_success_notice=f"Ran {_agent_command_name(command_name)}.",
                )
                await _edit_query_message(
                    query,
                    (
                        f"Send arguments for {_agent_command_name(command_name)} "
                        "as your next plain text message.\n"
                        f"Hint: {hint}"
                    ),
                    reply_markup=InlineKeyboardMarkup(
                        [
                            [
                                _callback_button(
                                    ui_state,
                                    user_id,
                                    "Cancel Command",
                                    "runtime_status_command_cancel",
                                )
                            ]
                        ]
                    ),
                )
                return
            await _run_agent_command_from_callback(
                query,
                user_id=user_id,
                command_name=command_name,
                services=services,
                ui_state=ui_state,
                application=application,
                back_target="status",
            )
            return
        if target == "workspace_changes_ask_agent":
            state = await services.snapshot_runtime_state()
            git_status = _safe_read_workspace_git_status(state.workspace_path)
            items = _workspace_changes_context_items(git_status)
            if not items:
                await _show_runtime_status_from_callback(
                    query,
                    services,
                    ui_state,
                    user_id=user_id,
                    notice="No workspace changes to ask about.",
                )
                return
            await _begin_context_items_ask_from_callback(
                query,
                ui_state,
                user_id=user_id,
                items=items,
                prompt_label="current workspace changes",
                empty_notice="No workspace changes to ask about.",
                prompt_text=(
                    "Send your request about the current workspace changes as the next plain text message.\n"
                    "The agent will inspect the current Git changes from the local workspace."
                ),
                restore_action="runtime_status_page",
                restore_payload={"back_target": "status"},
                cancel_notice="Workspace changes request cancelled.",
                status_success_notice="Asked agent about current workspace changes.",
            )
            return
        if target == "workspace_changes_ask_last_request":
            state = await services.snapshot_runtime_state()
            git_status = _safe_read_workspace_git_status(state.workspace_path)
            items = _workspace_changes_context_items(git_status)
            if not items:
                await _show_runtime_status_from_callback(
                    query,
                    services,
                    ui_state,
                    user_id=user_id,
                    notice="No workspace changes to ask about.",
                )
                return
            last_request_text = ui_state.get_last_request_text(user_id, state.workspace_id)
            if last_request_text is None:
                await _show_runtime_status_from_callback(
                    query,
                    services,
                    ui_state,
                    user_id=user_id,
                    notice="No previous request text is available in this workspace.",
                )
                return
            after_turn_success, on_prepare_failure, on_turn_failure = _status_turn_callbacks(
                query,
                services,
                ui_state,
                user_id=user_id,
                success_notice="Asked agent with the last request about current workspace changes.",
            )
            await _run_context_items_request_on_message(
                query.message,
                user_id,
                services,
                ui_state,
                items=items,
                request_text=last_request_text,
                context_label="current workspace changes",
                application=application,
                after_turn_success=after_turn_success,
                on_prepare_failure=on_prepare_failure,
                on_turn_failure=on_turn_failure,
            )
            return
        if target == "workspace_changes_add_all":
            state = await services.snapshot_runtime_state()
            git_status = _safe_read_workspace_git_status(state.workspace_path)
            if not _status_workspace_changes_available(git_status):
                await _show_runtime_status_from_callback(
                    query,
                    services,
                    ui_state,
                    user_id=user_id,
                    notice="No workspace changes to add.",
                )
                return
            added_count, duplicate_count = _add_workspace_changes_to_context_bundle(
                ui_state,
                user_id=user_id,
                provider=state.provider,
                workspace_id=state.workspace_id,
                git_status=git_status,
            )
            await _show_runtime_status_from_callback(
                query,
                services,
                ui_state,
                user_id=user_id,
                notice=_workspace_changes_add_to_bundle_notice(
                    added_count=added_count,
                    duplicate_count=duplicate_count,
                ),
            )
            return
        if target == "workspace_changes_start_bundle_chat":
            state = await services.snapshot_runtime_state()
            git_status = _safe_read_workspace_git_status(state.workspace_path)
            if not _status_workspace_changes_available(git_status):
                await _show_runtime_status_from_callback(
                    query,
                    services,
                    ui_state,
                    user_id=user_id,
                    notice="No workspace changes to add.",
                )
                return
            already_active = ui_state.context_bundle_chat_active(
                user_id,
                state.provider,
                state.workspace_id,
            )
            added_count, duplicate_count = _add_workspace_changes_to_context_bundle(
                ui_state,
                user_id=user_id,
                provider=state.provider,
                workspace_id=state.workspace_id,
                git_status=git_status,
            )
            ui_state.enable_context_bundle_chat(user_id, state.provider, state.workspace_id)
            await _show_runtime_status_from_callback(
                query,
                services,
                ui_state,
                user_id=user_id,
                notice=_workspace_changes_start_bundle_chat_notice(
                    added_count=added_count,
                    duplicate_count=duplicate_count,
                    already_active=already_active,
                ),
            )
            return
        if target == "context_bundle_ask":
            state = await services.snapshot_runtime_state()
            bundle = ui_state.get_context_bundle(user_id, state.provider, state.workspace_id)
            if bundle is None or not bundle.items:
                await _show_runtime_status_from_callback(
                    query,
                    services,
                    ui_state,
                    user_id=user_id,
                    notice="Context bundle is empty.",
                )
                return
            ui_state.set_pending_text_action(
                user_id,
                "context_bundle_agent_prompt",
                items=tuple(bundle.items),
                back_target="status",
                source_message=query.message,
                status_success_notice="Asked agent with the current context bundle.",
            )
            await _edit_query_message(
                query,
                (
                    "Send your request for the current context bundle as the next plain text message.\n"
                    "The agent will read the listed files and inspect the listed Git changes from the current workspace."
                ),
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            _callback_button(
                                ui_state,
                                user_id,
                                "Cancel Ask",
                                "context_items_ask_cancel",
                                restore_action="runtime_status_page",
                                restore_payload={"back_target": "status"},
                                notice="Context bundle request cancelled.",
                            )
                        ]
                    ]
                ),
            )
            return
        if target == "context_bundle_ask_last_request":
            state = await services.snapshot_runtime_state()
            bundle = ui_state.get_context_bundle(user_id, state.provider, state.workspace_id)
            if bundle is None or not bundle.items:
                await _show_runtime_status_from_callback(
                    query,
                    services,
                    ui_state,
                    user_id=user_id,
                    notice="Context bundle is empty.",
                )
                return
            last_request_text = ui_state.get_last_request_text(user_id, state.workspace_id)
            if last_request_text is None:
                await _show_runtime_status_from_callback(
                    query,
                    services,
                    ui_state,
                    user_id=user_id,
                    notice="No previous request text is available in this workspace.",
                )
                return
            after_turn_success, on_prepare_failure, on_turn_failure = _status_turn_callbacks(
                query,
                services,
                ui_state,
                user_id=user_id,
                success_notice="Asked agent with the last request using the current context bundle.",
            )
            await _run_context_bundle_request_on_message(
                query.message,
                user_id,
                services,
                ui_state,
                items=tuple(bundle.items),
                request_text=last_request_text,
                application=application,
                after_turn_success=after_turn_success,
                on_prepare_failure=on_prepare_failure,
                on_turn_failure=on_turn_failure,
            )
            return
        if target == "context_bundle_clear":
            state = await services.snapshot_runtime_state()
            was_bundle_chat_active = ui_state.context_bundle_chat_active(
                user_id,
                state.provider,
                state.workspace_id,
            )
            ui_state.clear_context_bundle(user_id, state.provider, state.workspace_id)
            ui_state.clear_pending_text_action(user_id)
            await _show_runtime_status_from_callback(
                query,
                services,
                ui_state,
                user_id=user_id,
                notice=(
                    "Cleared context bundle. Bundle chat was turned off."
                    if was_bundle_chat_active
                    else "Cleared context bundle."
                ),
            )
            return
        if target == "restart_agent":
            await _restart_agent_from_callback(
                query,
                services,
                ui_state,
                user_id=user_id,
                application=application,
                back_target="status",
            )
            return
        if target == "model_mode":
            try:
                await _show_model_mode_menu_from_callback(
                    query,
                    services,
                    ui_state,
                    user_id=user_id,
                    application=application,
                    back_target="status",
                )
            except Exception:
                await _edit_query_message(query, "Failed to load model / mode controls.")
            return
        if target == "switch_agent":
            try:
                await _show_switch_agent_menu_from_callback(
                    query,
                    services,
                    ui_state,
                    user_id=user_id,
                    back_target="status",
                )
            except Exception:
                await _edit_query_message(query, "Failed to load switch agent menu.")
            return
        if target == "switch_workspace":
            try:
                await _show_switch_workspace_menu_from_callback(
                    query,
                    services,
                    ui_state,
                    user_id=user_id,
                    back_target="status",
                )
            except Exception:
                await _edit_query_message(query, "Failed to load switch workspace menu.")
            return
        await query.answer("Unknown action.", show_alert=True)
        return

    if action == "runtime_status_cancel_pending":
        await query.answer()
        cleared = ui_state.clear_pending_text_action(user_id)
        try:
            await _show_runtime_status_from_callback(
                query,
                services,
                ui_state,
                user_id=user_id,
                notice="Pending input cancelled." if cleared is not None else "No pending input to cancel.",
            )
        except Exception:
            await _edit_query_message(query, "Failed to update bot status.")
        return

    if action == "runtime_status_command_cancel":
        await query.answer()
        ui_state.clear_pending_text_action(user_id)
        try:
            await _show_runtime_status_from_callback(
                query,
                services,
                ui_state,
                user_id=user_id,
                notice="Command input cancelled.",
            )
        except Exception:
            await _edit_query_message(query, "Failed to update bot status.")
        return

    if action == "runtime_status_start_bundle_chat":
        await query.answer()
        try:
            state = await services.snapshot_runtime_state()
            bundle = ui_state.get_context_bundle(user_id, state.provider, state.workspace_id)
            if bundle is None or not bundle.items:
                notice = "Context bundle is empty."
            elif ui_state.context_bundle_chat_active(user_id, state.provider, state.workspace_id):
                notice = "Bundle chat is already on."
            else:
                ui_state.enable_context_bundle_chat(user_id, state.provider, state.workspace_id)
                notice = "Bundle chat enabled."
            await _show_runtime_status_from_callback(
                query,
                services,
                ui_state,
                user_id=user_id,
                notice=notice,
            )
        except Exception:
            await _edit_query_message(query, "Failed to update bot status.")
        return

    if action == "runtime_status_stop_bundle_chat":
        await query.answer()
        try:
            state = await services.snapshot_runtime_state()
            if ui_state.context_bundle_chat_active(user_id, state.provider, state.workspace_id):
                ui_state.disable_context_bundle_chat(user_id)
                notice = "Bundle chat disabled."
            else:
                notice = "Bundle chat is already off."
            await _show_runtime_status_from_callback(
                query,
                services,
                ui_state,
                user_id=user_id,
                notice=notice,
            )
        except Exception:
            await _edit_query_message(query, "Failed to update bot status.")
        return

    if action == "runtime_status_search_cancel":
        await query.answer()
        ui_state.clear_pending_text_action(user_id)
        try:
            await _show_runtime_status_from_callback(
                query,
                services,
                ui_state,
                user_id=user_id,
                notice="Search cancelled.",
            )
        except Exception:
            await _edit_query_message(query, "Failed to load bot status.")
        return

    if action == "switch_provider":
        await _switch_provider_from_callback(
            query,
            services,
            ui_state,
            provider=payload["provider"],
            application=application,
            back_target=str(payload.get("back_target", "none")),
        )
        return

    if action == "switch_provider_retry_last_turn":
        await _switch_provider_from_callback(
            query,
            services,
            ui_state,
            provider=payload["provider"],
            application=application,
            replay_action="retry_last_turn",
            back_target=str(payload.get("back_target", "none")),
        )
        return

    if action == "switch_provider_fork_last_turn":
        await _switch_provider_from_callback(
            query,
            services,
            ui_state,
            provider=payload["provider"],
            application=application,
            replay_action="fork_last_turn",
            back_target=str(payload.get("back_target", "none")),
        )
        return

    if action == "switch_workspace":
        if query.from_user is None or query.from_user.id != services.admin_user_id:
            await query.answer("Unauthorized user.", show_alert=True)
            return
        workspace = services.config.agent.resolve_workspace(payload["workspace_id"])
        await query.answer()
        await _edit_query_message(query, f"Switching workspace to {workspace.label}...")
        try:
            await asyncio.wait_for(
                services.switch_workspace(workspace.id),
                CALLBACK_OPERATION_TIMEOUT_SECONDS,
            )
        except Exception:
            if str(payload.get("back_target", "none")) == "status":
                await _show_switch_workspace_menu_from_callback(
                    query,
                    services,
                    ui_state,
                    user_id=user_id,
                    back_target="status",
                    notice="session creation failed",
                )
                return
            await _edit_query_message(query, "session creation failed")
            return
        ui_state.invalidate_runtime_bound_interactions()
        state = await services.snapshot_runtime_state()
        success_text = (
            f"Switched workspace to {workspace.label} on "
            f"{resolve_provider_profile(state.provider).display_name}. "
            "Old bot buttons and pending inputs were cleared."
        )
        if str(payload.get("back_target", "none")) == "status":
            await _show_runtime_status_from_callback(
                query,
                services,
                ui_state,
                user_id=user_id,
                notice=success_text,
            )
            return
        await _edit_query_message(query, success_text)
        return

    if action == "history_page":
        await query.answer()
        state, history_state = await _with_active_store(
            services,
            lambda store: _load_history_view_state(store, user_id),
        )
        text, markup = _build_history_view(
            entries=history_state.entries,
            provider=state.provider,
            workspace_id=state.workspace_id,
            workspace_label=_workspace_label(services, state.workspace_id),
            user_id=user_id,
            page=int(payload["page"]),
            ui_state=ui_state,
            active_session_id=history_state.active_session_id,
            show_provider_sessions=user_id == services.admin_user_id,
            back_target=str(payload.get("back_target", "none")),
        )
        await _edit_query_message(query, text, reply_markup=markup)
        return

    if action == "history_provider_sessions":
        if query.from_user is None or query.from_user.id != services.admin_user_id:
            await query.answer("Unauthorized user.", show_alert=True)
            return
        await query.answer()
        try:
            await _show_provider_sessions_from_callback(
                query,
                services,
                ui_state,
                user_id=user_id,
                cursor=payload.get("cursor"),
                previous_cursors=tuple(payload.get("previous_cursors", ())),
                history_page=int(payload.get("history_page", 0)),
                back_target=str(payload.get("back_target", "history")),
                history_back_target=str(payload.get("history_back_target", "none")),
            )
        except Exception:
            await _edit_query_message(query, "Failed to load provider sessions.")
        return

    if action == "history_run":
        await _switch_history_session_from_callback(
            query,
            services,
            ui_state,
            user_id=user_id,
            session_id=payload["session_id"],
            application=application,
            page=int(payload.get("page", 0)),
            back_target=str(payload.get("back_target", "none")),
        )
        return

    if action == "history_run_retry_last_turn":
        await _switch_history_session_from_callback(
            query,
            services,
            ui_state,
            user_id=user_id,
            session_id=payload["session_id"],
            application=application,
            replay_after_switch=True,
            page=int(payload.get("page", 0)),
            back_target=str(payload.get("back_target", "none")),
        )
        return

    if action == "history_delete":
        await query.answer()
        await _edit_query_message(query, "Deleting session...")
        _, active_session = await _with_active_store(
            services,
            lambda store: store.peek(user_id),
        )
        active_session_id = None if active_session is None else active_session.session_id
        state, deleted = await _with_active_store(
            services,
            lambda store: store.delete_history(user_id, payload["session_id"]),
        )
        _, history_state = await _with_active_store(
            services,
            lambda store: _load_history_view_state(store, user_id),
        )
        deleted_active_session = (
            active_session_id == payload["session_id"]
            and history_state.active_session_id != payload["session_id"]
        )
        if deleted_active_session:
            ui_state.invalidate_session_bound_interactions()
            await _sync_discovered_agent_commands_for_user(
                application,
                services,
                ui_state,
                user_id,
            )
        if deleted and deleted_active_session:
            notice = (
                "Deleted session. Old bot buttons and pending inputs tied to that session were cleared."
            )
        elif deleted:
            notice = "Deleted session."
        elif deleted_active_session:
            notice = (
                "Closed the current live session, but failed to remove the local history entry. "
                "Old bot buttons and pending inputs tied to that session were cleared."
            )
        else:
            notice = "Failed to delete session."
        text, markup = _build_history_view(
            entries=history_state.entries,
            provider=state.provider,
            workspace_id=state.workspace_id,
            workspace_label=_workspace_label(services, state.workspace_id),
            user_id=user_id,
            page=int(payload.get("page", 0)),
            ui_state=ui_state,
            active_session_id=history_state.active_session_id,
            notice=notice,
            show_provider_sessions=user_id == services.admin_user_id,
            back_target=str(payload.get("back_target", "none")),
        )
        await _edit_query_message(query, text, reply_markup=markup)
        return

    if action == "history_rename":
        await query.answer()
        ui_state.set_pending_text_action(
            user_id,
            "rename_history",
            session_id=payload["session_id"],
            page=int(payload.get("page", 0)),
            back_target=str(payload.get("back_target", "none")),
        )
        await _edit_query_message(
            query,
            (
                "Send the new session title as your next plain text message.\n"
                f"Current title: {payload['title']}\n"
                f"Session: {payload['session_id']}"
            ),
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        _callback_button(
                            ui_state,
                            user_id,
                            "Cancel Rename",
                            "history_rename_cancel",
                            page=int(payload.get("page", 0)),
                            back_target=str(payload.get("back_target", "none")),
                        )
                    ]
                ]
            ),
        )
        return

    if action == "history_rename_cancel":
        await query.answer()
        ui_state.clear_pending_text_action(user_id)
        state, history_state = await _with_active_store(
            services,
            lambda store: _load_history_view_state(store, user_id),
        )
        text, markup = _build_history_view(
            entries=history_state.entries,
            provider=state.provider,
            workspace_id=state.workspace_id,
            workspace_label=_workspace_label(services, state.workspace_id),
            user_id=user_id,
            page=int(payload.get("page", 0)),
            ui_state=ui_state,
            active_session_id=history_state.active_session_id,
            notice="Rename cancelled.",
            show_provider_sessions=user_id == services.admin_user_id,
            back_target=str(payload.get("back_target", "none")),
        )
        await _edit_query_message(query, text, reply_markup=markup)
        return

    if action == "provider_sessions_page":
        if query.from_user is None or query.from_user.id != services.admin_user_id:
            await query.answer("Unauthorized user.", show_alert=True)
            return
        await query.answer()
        try:
            await _show_provider_sessions_from_callback(
                query,
                services,
                ui_state,
                user_id=user_id,
                cursor=payload.get("cursor"),
                previous_cursors=tuple(payload.get("previous_cursors", ())),
                history_page=int(payload.get("history_page", 0)),
                back_target=str(payload.get("back_target", "history")),
                history_back_target=str(payload.get("history_back_target", "none")),
            )
        except Exception:
            await _edit_query_message(query, "Failed to load provider sessions.")
        return

    if action == "provider_session_run":
        await _switch_provider_session_from_callback(
            query,
            services,
            ui_state,
            user_id=user_id,
            payload=payload,
            application=application,
        )
        return

    if action == "provider_session_run_retry_last_turn":
        await _switch_provider_session_from_callback(
            query,
            services,
            ui_state,
            user_id=user_id,
            payload=payload,
            application=application,
            replay_after_switch=True,
        )
        return

    if action == "agent_commands_page":
        await query.answer()
        await _show_agent_commands_menu_from_callback(
            query,
            services,
            ui_state,
            user_id=user_id,
            page=int(payload["page"]),
            back_target=str(payload.get("back_target", "none")),
        )
        return

    if action == "agent_command_use":
        await query.answer()
        if payload.get("hint"):
            pending_payload: dict[str, Any] = {
                "command_name": payload["command_name"],
                "hint": payload["hint"],
                "page": int(payload.get("page", 0)),
                "back_target": str(payload.get("back_target", "none")),
            }
            if pending_payload["back_target"] == "status" and query.message is not None:
                pending_payload["source_message"] = query.message
                pending_payload["status_success_notice"] = (
                    f"Ran {_agent_command_name(payload['command_name'])}."
                )
            ui_state.set_pending_text_action(
                user_id,
                "run_agent_command",
                **pending_payload,
            )
            await _edit_query_message(
                query,
                (
                    f"Send arguments for {_agent_command_name(payload['command_name'])} "
                    "as your next plain text message.\n"
                    f"Hint: {payload['hint']}"
                ),
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            _callback_button(
                                ui_state,
                                user_id,
                                "Cancel Command",
                                "agent_command_cancel",
                                page=int(payload.get("page", 0)),
                                back_target=str(payload.get("back_target", "none")),
                            )
                        ]
                    ]
                ),
            )
            return

        await _run_agent_command_from_callback(
            query,
            user_id=user_id,
            command_name=payload["command_name"],
            services=services,
            ui_state=ui_state,
            application=application,
            back_target=str(payload.get("back_target", "none")),
        )
        return

    if action == "agent_command_cancel":
        await query.answer()
        ui_state.clear_pending_text_action(user_id)
        await _show_agent_commands_menu_from_callback(
            query,
            services,
            ui_state,
            user_id=user_id,
            page=int(payload.get("page", 0)),
            back_target=str(payload.get("back_target", "none")),
            notice="Command input cancelled.",
        )
        return

    if action == "workspace_page":
        await query.answer()
        await _show_workspace_listing_from_callback(
            query,
            services,
            ui_state,
            user_id=user_id,
            relative_path=payload.get("relative_path", ""),
            page=int(payload["page"]),
            back_target=str(payload.get("back_target", "none")),
        )
        return

    if action == "workspace_open_dir":
        await query.answer()
        await _show_workspace_listing_from_callback(
            query,
            services,
            ui_state,
            user_id=user_id,
            relative_path=payload["relative_path"],
            page=0,
            back_target=str(payload.get("back_target", "none")),
        )
        return

    if action == "workspace_open_file":
        await query.answer()
        await _show_workspace_file_preview_from_callback(
            query,
            services,
            ui_state,
            user_id=user_id,
            relative_path=payload["relative_path"],
            page=int(payload.get("page", 0)),
            back_target=str(payload.get("back_target", "none")),
        )
        return

    if action == "workspace_back_to_dir":
        await query.answer()
        await _show_workspace_listing_from_callback(
            query,
            services,
            ui_state,
            user_id=user_id,
            relative_path=payload.get("relative_path", ""),
            page=int(payload.get("page", 0)),
            back_target=str(payload.get("back_target", "none")),
        )
        return

    if action == "workspace_page_add_context":
        await query.answer()
        state, listing = await _load_workspace_listing(services, payload.get("relative_path", ""))
        page = int(payload.get("page", 0))
        visible_file_paths = _visible_workspace_file_paths(listing, page)
        if not visible_file_paths:
            await _show_workspace_listing_from_callback(
                query,
                services,
                ui_state,
                user_id=user_id,
                relative_path=payload.get("relative_path", ""),
                page=page,
                back_target=str(payload.get("back_target", "none")),
                notice="No visible files to add.",
            )
            return

        added_count, duplicate_count = _add_workspace_listing_files_to_context_bundle(
            ui_state,
            user_id=user_id,
            provider=state.provider,
            workspace_id=state.workspace_id,
            listing=listing,
            page=page,
        )
        source_restore_action, source_restore_payload, source_back_label = _callback_source_restore_values(payload)
        await _show_context_bundle_from_callback(
            query,
            services,
            ui_state,
            user_id=user_id,
            page=0,
            back_target=str(payload.get("back_target", "none")),
            notice=_workspace_listing_add_to_bundle_notice(
                added_count=added_count,
                duplicate_count=duplicate_count,
            ),
            source_restore_action=source_restore_action,
            source_restore_payload=source_restore_payload,
            source_back_label=source_back_label,
        )
        return

    if action == "workspace_page_start_bundle_chat":
        await query.answer()
        state, listing = await _load_workspace_listing(services, payload.get("relative_path", ""))
        page = int(payload.get("page", 0))
        visible_file_paths = _visible_workspace_file_paths(listing, page)
        if not visible_file_paths:
            await _show_workspace_listing_from_callback(
                query,
                services,
                ui_state,
                user_id=user_id,
                relative_path=payload.get("relative_path", ""),
                page=page,
                back_target=str(payload.get("back_target", "none")),
                notice="No visible files to add.",
            )
            return

        already_active = ui_state.context_bundle_chat_active(
            user_id,
            state.provider,
            state.workspace_id,
        )
        added_count, duplicate_count = _add_workspace_listing_files_to_context_bundle(
            ui_state,
            user_id=user_id,
            provider=state.provider,
            workspace_id=state.workspace_id,
            listing=listing,
            page=page,
        )
        ui_state.enable_context_bundle_chat(user_id, state.provider, state.workspace_id)
        source_restore_action, source_restore_payload, source_back_label = _callback_source_restore_values(payload)
        await _show_context_bundle_from_callback(
            query,
            services,
            ui_state,
            user_id=user_id,
            page=0,
            back_target=str(payload.get("back_target", "none")),
            notice=_workspace_listing_start_bundle_chat_notice(
                added_count=added_count,
                duplicate_count=duplicate_count,
                already_active=already_active,
            ),
            source_restore_action=source_restore_action,
            source_restore_payload=source_restore_payload,
            source_back_label=source_back_label,
        )
        return

    if action == "workspace_page_ask_agent":
        await query.answer()
        _, listing = await _load_workspace_listing(services, payload.get("relative_path", ""))
        page = int(payload.get("page", 0))
        items = _workspace_listing_context_items(listing, page)
        if not items:
            await _show_workspace_listing_from_callback(
                query,
                services,
                ui_state,
                user_id=user_id,
                relative_path=payload.get("relative_path", ""),
                page=page,
                back_target=str(payload.get("back_target", "none")),
                notice="No visible files to ask about.",
            )
            return

        await _begin_context_items_ask_from_callback(
            query,
            ui_state,
            user_id=user_id,
            items=items,
            prompt_label="visible workspace files",
            empty_notice="No visible files to ask about.",
            prompt_text=(
                "Send your request about the visible files as the next plain text message.\n"
                "The agent will read the currently visible files from the current workspace."
            ),
            restore_action="workspace_page",
            restore_payload={
                "relative_path": payload.get("relative_path", ""),
                "page": page,
                "back_target": str(payload.get("back_target", "none")),
            },
            cancel_notice="Visible files request cancelled.",
            status_success_notice="Asked agent about visible workspace files.",
        )
        return

    if action == "workspace_page_ask_last_request":
        await query.answer()
        state, listing = await _load_workspace_listing(services, payload.get("relative_path", ""))
        page = int(payload.get("page", 0))
        back_target = str(payload.get("back_target", "none"))
        items = _workspace_listing_context_items(listing, page)
        if not items:
            await _show_workspace_listing_from_callback(
                query,
                services,
                ui_state,
                user_id=user_id,
                relative_path=payload.get("relative_path", ""),
                page=page,
                back_target=back_target,
                notice="No visible files to ask about.",
            )
            return
        last_request_text = ui_state.get_last_request_text(user_id, state.workspace_id)
        if last_request_text is None:
            await _show_workspace_listing_from_callback(
                query,
                services,
                ui_state,
                user_id=user_id,
                relative_path=payload.get("relative_path", ""),
                page=page,
                back_target=back_target,
                notice="No previous request text is available in this workspace.",
            )
            return
        if query.message is None:
            return
        after_turn_success = None
        on_prepare_failure = None
        on_turn_failure = None
        if back_target == "status":
            after_turn_success, on_prepare_failure, on_turn_failure = _status_turn_callbacks(
                query,
                services,
                ui_state,
                user_id=user_id,
                success_notice="Asked agent with the last request about visible workspace files.",
            )
        await _run_context_items_request_on_message(
            query.message,
            user_id,
            services,
            ui_state,
            items=items,
            request_text=last_request_text,
            context_label="visible workspace files",
            application=application,
            after_turn_success=after_turn_success,
            on_prepare_failure=on_prepare_failure,
            on_turn_failure=on_turn_failure,
        )
        return

    if action == "workspace_search_page":
        await query.answer()
        await _show_workspace_search_results_from_callback(
            query,
            services,
            ui_state,
            user_id=user_id,
            query_text=payload["query_text"],
            page=int(payload["page"]),
            back_target=str(payload.get("back_target", "none")),
        )
        return

    if action == "workspace_search_open_file":
        await query.answer()
        await _show_workspace_search_file_preview_from_callback(
            query,
            services,
            ui_state,
            user_id=user_id,
            relative_path=payload["relative_path"],
            query_text=payload["query_text"],
            page=int(payload.get("page", 0)),
            back_target=str(payload.get("back_target", "none")),
        )
        return

    if action == "workspace_search_back":
        await query.answer()
        await _show_workspace_search_results_from_callback(
            query,
            services,
            ui_state,
            user_id=user_id,
            query_text=payload["query_text"],
            page=int(payload.get("page", 0)),
            back_target=str(payload.get("back_target", "none")),
        )
        return

    if action == "workspace_search_add_context":
        await query.answer()
        state, search_results = await _load_workspace_search_results(services, payload["query_text"])
        unique_paths = _search_result_unique_paths(search_results)
        if not unique_paths:
            await _show_workspace_search_results_from_callback(
                query,
                services,
                ui_state,
                user_id=user_id,
                query_text=payload["query_text"],
                page=0,
                back_target=str(payload.get("back_target", "none")),
                notice="No matching files to add.",
            )
            return

        added_count, duplicate_count = _add_workspace_search_results_to_context_bundle(
            ui_state,
            user_id=user_id,
            provider=state.provider,
            workspace_id=state.workspace_id,
            search_results=search_results,
        )
        source_restore_action, source_restore_payload, source_back_label = _callback_source_restore_values(payload)
        await _show_context_bundle_from_callback(
            query,
            services,
            ui_state,
            user_id=user_id,
            page=0,
            back_target=str(payload.get("back_target", "none")),
            notice=_workspace_search_add_to_bundle_notice(
                added_count=added_count,
                duplicate_count=duplicate_count,
            ),
            source_restore_action=source_restore_action,
            source_restore_payload=source_restore_payload,
            source_back_label=source_back_label,
        )
        return

    if action == "workspace_search_start_bundle_chat":
        await query.answer()
        state, search_results = await _load_workspace_search_results(services, payload["query_text"])
        unique_paths = _search_result_unique_paths(search_results)
        if not unique_paths:
            await _show_workspace_search_results_from_callback(
                query,
                services,
                ui_state,
                user_id=user_id,
                query_text=payload["query_text"],
                page=int(payload.get("page", 0)),
                back_target=str(payload.get("back_target", "none")),
                notice="No matching files to add.",
            )
            return

        already_active = ui_state.context_bundle_chat_active(
            user_id,
            state.provider,
            state.workspace_id,
        )
        added_count, duplicate_count = _add_workspace_search_results_to_context_bundle(
            ui_state,
            user_id=user_id,
            provider=state.provider,
            workspace_id=state.workspace_id,
            search_results=search_results,
        )
        ui_state.enable_context_bundle_chat(user_id, state.provider, state.workspace_id)
        source_restore_action, source_restore_payload, source_back_label = _callback_source_restore_values(payload)
        await _show_context_bundle_from_callback(
            query,
            services,
            ui_state,
            user_id=user_id,
            page=0,
            back_target=str(payload.get("back_target", "none")),
            notice=_workspace_search_start_bundle_chat_notice(
                added_count=added_count,
                duplicate_count=duplicate_count,
                already_active=already_active,
            ),
            source_restore_action=source_restore_action,
            source_restore_payload=source_restore_payload,
            source_back_label=source_back_label,
        )
        return

    if action == "workspace_search_ask_agent":
        await query.answer()
        _, search_results = await _load_workspace_search_results(services, payload["query_text"])
        items = _workspace_search_context_items(search_results)
        if not items:
            await _show_workspace_search_results_from_callback(
                query,
                services,
                ui_state,
                user_id=user_id,
                query_text=payload["query_text"],
                page=int(payload.get("page", 0)),
                back_target=str(payload.get("back_target", "none")),
                notice="No matching files to ask about.",
            )
            return

        await _begin_context_items_ask_from_callback(
            query,
            ui_state,
            user_id=user_id,
            items=items,
            prompt_label="matching workspace files",
            empty_notice="No matching files to ask about.",
            prompt_text=(
                "Send your request about the matching files as the next plain text message.\n"
                "The agent will read the files that match the current search from the current workspace."
            ),
            restore_action="workspace_search_page",
            restore_payload={
                "query_text": payload["query_text"],
                "page": int(payload.get("page", 0)),
                "back_target": str(payload.get("back_target", "none")),
            },
            cancel_notice="Matching files request cancelled.",
            status_success_notice="Asked agent about matching workspace files.",
        )
        return

    if action == "workspace_search_ask_last_request":
        await query.answer()
        state, search_results = await _load_workspace_search_results(services, payload["query_text"])
        back_target = str(payload.get("back_target", "none"))
        items = _workspace_search_context_items(search_results)
        if not items:
            await _show_workspace_search_results_from_callback(
                query,
                services,
                ui_state,
                user_id=user_id,
                query_text=payload["query_text"],
                page=int(payload.get("page", 0)),
                back_target=back_target,
                notice="No matching files to ask about.",
            )
            return
        last_request_text = ui_state.get_last_request_text(user_id, state.workspace_id)
        if last_request_text is None:
            await _show_workspace_search_results_from_callback(
                query,
                services,
                ui_state,
                user_id=user_id,
                query_text=payload["query_text"],
                page=int(payload.get("page", 0)),
                back_target=back_target,
                notice="No previous request text is available in this workspace.",
            )
            return
        if query.message is None:
            return
        after_turn_success = None
        on_prepare_failure = None
        on_turn_failure = None
        if back_target == "status":
            after_turn_success, on_prepare_failure, on_turn_failure = _status_turn_callbacks(
                query,
                services,
                ui_state,
                user_id=user_id,
                success_notice="Asked agent with the last request about matching workspace files.",
            )
        await _run_context_items_request_on_message(
            query.message,
            user_id,
            services,
            ui_state,
            items=items,
            request_text=last_request_text,
            context_label="matching workspace files",
            application=application,
            after_turn_success=after_turn_success,
            on_prepare_failure=on_prepare_failure,
            on_turn_failure=on_turn_failure,
        )
        return

    if action == "workspace_search_cancel":
        await query.answer()
        ui_state.clear_pending_text_action(user_id)
        await _edit_query_message(query, "Search cancelled.")
        return

    if action == "workspace_changes_page":
        await query.answer()
        await _show_workspace_changes_from_callback(
            query,
            services,
            ui_state,
            user_id=user_id,
            page=int(payload["page"]),
            back_target=str(payload.get("back_target", "none")),
        )
        return

    if action == "workspace_changes_follow_up_page":
        await query.answer()
        await _show_workspace_changes_follow_up_from_callback(
            query,
            services,
            ui_state,
            user_id=user_id,
        )
        return

    if action == "workspace_change_open":
        await query.answer()
        await _show_workspace_change_preview_from_callback(
            query,
            services,
            ui_state,
            user_id=user_id,
            relative_path=payload["relative_path"],
            status_code=payload["status_code"],
            page=int(payload.get("page", 0)),
            back_target=str(payload.get("back_target", "none")),
        )
        return

    if action == "workspace_changes_back":
        await query.answer()
        await _show_workspace_changes_from_callback(
            query,
            services,
            ui_state,
            user_id=user_id,
            page=int(payload.get("page", 0)),
            back_target=str(payload.get("back_target", "none")),
        )
        return

    if action == "workspace_changes_add_all":
        await query.answer()
        state, git_status = await _load_workspace_changes(services)
        if not git_status.is_git_repo or not git_status.entries:
            await _show_workspace_changes_from_callback(
                query,
                services,
                ui_state,
                user_id=user_id,
                page=int(payload.get("page", 0)),
                back_target=str(payload.get("back_target", "none")),
                notice="No workspace changes to add.",
            )
            return

        added_count, duplicate_count = _add_workspace_changes_to_context_bundle(
            ui_state,
            user_id=user_id,
            provider=state.provider,
            workspace_id=state.workspace_id,
            git_status=git_status,
        )
        source_restore_action, source_restore_payload, source_back_label = _callback_source_restore_values(payload)
        await _show_context_bundle_from_callback(
            query,
            services,
            ui_state,
            user_id=user_id,
            page=0,
            back_target=str(payload.get("back_target", "none")),
            notice=_workspace_changes_add_to_bundle_notice(
                added_count=added_count,
                duplicate_count=duplicate_count,
            ),
            source_restore_action=source_restore_action,
            source_restore_payload=source_restore_payload,
            source_back_label=source_back_label,
        )
        return

    if action == "workspace_changes_start_bundle_chat":
        await query.answer()
        state, git_status = await _load_workspace_changes(services)
        if not git_status.is_git_repo or not git_status.entries:
            await _show_workspace_changes_from_callback(
                query,
                services,
                ui_state,
                user_id=user_id,
                page=int(payload.get("page", 0)),
                back_target=str(payload.get("back_target", "none")),
                notice="No workspace changes to add.",
            )
            return

        already_active = ui_state.context_bundle_chat_active(
            user_id,
            state.provider,
            state.workspace_id,
        )
        added_count, duplicate_count = _add_workspace_changes_to_context_bundle(
            ui_state,
            user_id=user_id,
            provider=state.provider,
            workspace_id=state.workspace_id,
            git_status=git_status,
        )
        ui_state.enable_context_bundle_chat(user_id, state.provider, state.workspace_id)
        source_restore_action, source_restore_payload, source_back_label = _callback_source_restore_values(payload)
        await _show_context_bundle_from_callback(
            query,
            services,
            ui_state,
            user_id=user_id,
            page=0,
            back_target=str(payload.get("back_target", "none")),
            notice=_workspace_changes_start_bundle_chat_notice(
                added_count=added_count,
                duplicate_count=duplicate_count,
                already_active=already_active,
            ),
            source_restore_action=source_restore_action,
            source_restore_payload=source_restore_payload,
            source_back_label=source_back_label,
        )
        return

    if action == "workspace_changes_ask_agent":
        await query.answer()
        _, git_status = await _load_workspace_changes(services)
        page = int(payload.get("page", 0))
        source = str(payload.get("source", "changes"))
        items = _workspace_changes_context_items(git_status)
        if not items:
            if source == "follow_up":
                await _show_workspace_changes_follow_up_from_callback(
                    query,
                    services,
                    ui_state,
                    user_id=user_id,
                    notice="No workspace changes to ask about.",
                )
            else:
                await _show_workspace_changes_from_callback(
                    query,
                    services,
                    ui_state,
                    user_id=user_id,
                    page=page,
                    back_target=str(payload.get("back_target", "none")),
                    notice="No workspace changes to ask about.",
                )
            return

        await _begin_context_items_ask_from_callback(
            query,
            ui_state,
            user_id=user_id,
            items=items,
            prompt_label="current workspace changes",
            empty_notice="No workspace changes to ask about.",
            prompt_text=(
                "Send your request about the current workspace changes as the next plain text message.\n"
                "The agent will inspect the current Git changes from the local workspace."
            ),
            restore_action="workspace_changes_follow_up" if source == "follow_up" else "workspace_changes_page",
            restore_payload=(
                {}
                if source == "follow_up"
                else {
                    "page": page,
                    "back_target": str(payload.get("back_target", "none")),
                }
            ),
            cancel_notice="Workspace changes request cancelled.",
            status_success_notice="Asked agent about current workspace changes.",
            source_restore_action="workspace_changes_follow_up" if source == "follow_up" else None,
            source_success_notice=(
                "Asked agent about current workspace changes." if source == "follow_up" else None
            ),
        )
        return

    if action == "workspace_changes_ask_last_request":
        await query.answer()
        state, git_status = await _load_workspace_changes(services)
        page = int(payload.get("page", 0))
        back_target = str(payload.get("back_target", "none"))
        source = str(payload.get("source", "changes"))
        items = _workspace_changes_context_items(git_status)
        if not items:
            if source == "follow_up":
                await _show_workspace_changes_follow_up_from_callback(
                    query,
                    services,
                    ui_state,
                    user_id=user_id,
                    notice="No workspace changes to ask about.",
                )
            else:
                await _show_workspace_changes_from_callback(
                    query,
                    services,
                    ui_state,
                    user_id=user_id,
                    page=page,
                    back_target=back_target,
                    notice="No workspace changes to ask about.",
                )
            return
        last_request_text = ui_state.get_last_request_text(user_id, state.workspace_id)
        if last_request_text is None:
            if source == "follow_up":
                await _show_workspace_changes_follow_up_from_callback(
                    query,
                    services,
                    ui_state,
                    user_id=user_id,
                    notice="No previous request text is available in this workspace.",
                )
            else:
                await _show_workspace_changes_from_callback(
                    query,
                    services,
                    ui_state,
                    user_id=user_id,
                    page=page,
                    back_target=back_target,
                    notice="No previous request text is available in this workspace.",
                )
            return
        if query.message is None:
            return
        after_turn_success = None
        on_prepare_failure = None
        on_turn_failure = None
        if back_target == "status":
            after_turn_success, on_prepare_failure, on_turn_failure = _status_turn_callbacks(
                query,
                services,
                ui_state,
                user_id=user_id,
                success_notice="Asked agent with the last request about current workspace changes.",
            )
        elif source == "follow_up":
            async def _after_follow_up_success(_state, _session) -> None:
                await _show_workspace_changes_follow_up_from_callback(
                    query,
                    services,
                    ui_state,
                    user_id=user_id,
                    notice="Asked agent with the last request about current workspace changes.",
                )

            async def _on_follow_up_failure() -> None:
                await _show_workspace_changes_follow_up_from_callback(
                    query,
                    services,
                    ui_state,
                    user_id=user_id,
                    notice="Request failed.",
                )

            after_turn_success = _after_follow_up_success
            on_prepare_failure = _on_follow_up_failure
            on_turn_failure = _on_follow_up_failure
        await _run_context_items_request_on_message(
            query.message,
            user_id,
            services,
            ui_state,
            items=items,
            request_text=last_request_text,
            context_label="current workspace changes",
            application=application,
            after_turn_success=after_turn_success,
            on_prepare_failure=on_prepare_failure,
            on_turn_failure=on_turn_failure,
        )
        return

    if action == "workspace_file_add_context":
        state = await services.snapshot_runtime_state()
        _, added = ui_state.add_context_item(
            user_id,
            state.provider,
            state.workspace_id,
            _ContextBundleItem(
                kind="file",
                relative_path=payload["relative_path"],
            ),
        )
        await query.answer(_single_context_item_add_to_bundle_notice(item_kind="file", added=added))
        return

    if action == "workspace_change_add_context":
        state = await services.snapshot_runtime_state()
        _, added = ui_state.add_context_item(
            user_id,
            state.provider,
            state.workspace_id,
            _ContextBundleItem(
                kind="change",
                relative_path=payload["relative_path"],
                status_code=payload["status_code"],
            ),
        )
        await query.answer(_single_context_item_add_to_bundle_notice(item_kind="change", added=added))
        return

    if action == "workspace_file_start_bundle_chat":
        await query.answer()
        state = await services.snapshot_runtime_state()
        already_active = ui_state.context_bundle_chat_active(
            user_id,
            state.provider,
            state.workspace_id,
        )
        _, added = ui_state.add_context_item(
            user_id,
            state.provider,
            state.workspace_id,
            _ContextBundleItem(
                kind="file",
                relative_path=payload["relative_path"],
            ),
        )
        ui_state.enable_context_bundle_chat(user_id, state.provider, state.workspace_id)
        source_restore_action, source_restore_payload, source_back_label = _callback_source_restore_values(payload)
        await _show_context_bundle_from_callback(
            query,
            services,
            ui_state,
            user_id=user_id,
            page=0,
            back_target=str(payload.get("back_target", "none")),
            notice=_single_context_item_start_bundle_chat_notice(
                item_kind="file",
                added=added,
                already_active=already_active,
            ),
            source_restore_action=source_restore_action,
            source_restore_payload=source_restore_payload,
            source_back_label=source_back_label,
        )
        return

    if action == "workspace_change_start_bundle_chat":
        await query.answer()
        state = await services.snapshot_runtime_state()
        already_active = ui_state.context_bundle_chat_active(
            user_id,
            state.provider,
            state.workspace_id,
        )
        _, added = ui_state.add_context_item(
            user_id,
            state.provider,
            state.workspace_id,
            _ContextBundleItem(
                kind="change",
                relative_path=payload["relative_path"],
                status_code=payload["status_code"],
            ),
        )
        ui_state.enable_context_bundle_chat(user_id, state.provider, state.workspace_id)
        source_restore_action, source_restore_payload, source_back_label = _callback_source_restore_values(payload)
        await _show_context_bundle_from_callback(
            query,
            services,
            ui_state,
            user_id=user_id,
            page=0,
            back_target=str(payload.get("back_target", "none")),
            notice=_single_context_item_start_bundle_chat_notice(
                item_kind="change",
                added=added,
                already_active=already_active,
            ),
            source_restore_action=source_restore_action,
            source_restore_payload=source_restore_payload,
            source_back_label=source_back_label,
        )
        return

    if action == "workspace_file_ask_agent":
        await query.answer()
        pending_payload = dict(payload)
        if str(payload.get("back_target", "none")) == "status" and query.message is not None:
            pending_payload["source_message"] = query.message
            pending_payload["status_success_notice"] = "Asked agent about this file."
        ui_state.set_pending_text_action(
            user_id,
            "workspace_file_agent_prompt",
            **pending_payload,
        )
        await _edit_query_message(
            query,
            (
                f"Send your request about {payload['relative_path']} as the next plain text message.\n"
                "The agent will read the file from the current workspace."
            ),
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        _callback_button(
                            ui_state,
                            user_id,
                            "Cancel Ask",
                            "workspace_file_ask_cancel",
                            **payload,
                        )
                    ]
                ]
            ),
        )
        return

    if action == "workspace_file_ask_last_request":
        state = await services.snapshot_runtime_state()
        back_target = str(payload.get("back_target", "none"))
        last_request_text = ui_state.get_last_request_text(user_id, state.workspace_id)
        if last_request_text is None:
            await query.answer("No previous request text is available in this workspace.", show_alert=True)
            return
        await query.answer()
        if query.message is None:
            return
        after_turn_success = None
        on_prepare_failure = None
        on_turn_failure = None
        if back_target == "status":
            after_turn_success, on_prepare_failure, on_turn_failure = _status_turn_callbacks(
                query,
                services,
                ui_state,
                user_id=user_id,
                success_notice="Asked agent with the last request about this file.",
            )
        await _run_workspace_file_request_on_message(
            query.message,
            user_id,
            services,
            ui_state,
            relative_path=payload["relative_path"],
            request_text=last_request_text,
            application=application,
            after_turn_success=after_turn_success,
            on_prepare_failure=on_prepare_failure,
            on_turn_failure=on_turn_failure,
        )
        return

    if action == "workspace_change_ask_agent":
        await query.answer()
        pending_payload = dict(payload)
        if str(payload.get("back_target", "none")) == "status" and query.message is not None:
            pending_payload["source_message"] = query.message
            pending_payload["status_success_notice"] = "Asked agent about this change."
        ui_state.set_pending_text_action(
            user_id,
            "workspace_change_agent_prompt",
            **pending_payload,
        )
        await _edit_query_message(
            query,
            (
                f"Send your request about the change in {payload['relative_path']} as the next plain text message.\n"
                "The agent will inspect the current Git change from the local workspace."
            ),
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        _callback_button(
                            ui_state,
                            user_id,
                            "Cancel Ask",
                            "workspace_change_ask_cancel",
                            **payload,
                        )
                    ]
                ]
            ),
        )
        return

    if action == "workspace_change_ask_last_request":
        state = await services.snapshot_runtime_state()
        back_target = str(payload.get("back_target", "none"))
        last_request_text = ui_state.get_last_request_text(user_id, state.workspace_id)
        if last_request_text is None:
            await query.answer("No previous request text is available in this workspace.", show_alert=True)
            return
        await query.answer()
        if query.message is None:
            return
        after_turn_success = None
        on_prepare_failure = None
        on_turn_failure = None
        if back_target == "status":
            after_turn_success, on_prepare_failure, on_turn_failure = _status_turn_callbacks(
                query,
                services,
                ui_state,
                user_id=user_id,
                success_notice="Asked agent with the last request about this change.",
            )
        await _run_workspace_change_request_on_message(
            query.message,
            user_id,
            services,
            ui_state,
            relative_path=payload["relative_path"],
            status_code=payload["status_code"],
            request_text=last_request_text,
            application=application,
            after_turn_success=after_turn_success,
            on_prepare_failure=on_prepare_failure,
            on_turn_failure=on_turn_failure,
        )
        return

    if action == "workspace_change_ask_cancel":
        await query.answer()
        ui_state.clear_pending_text_action(user_id)
        if payload.get("source") == "bundle":
            source_restore_action, source_restore_payload, source_back_label = _callback_source_restore_values(payload)
            await _show_context_bundle_change_preview_from_callback(
                query,
                services,
                ui_state,
                user_id=user_id,
                relative_path=payload["relative_path"],
                status_code=payload["status_code"],
                page=int(payload.get("page", 0)),
                back_target=str(payload.get("back_target", "none")),
                source_restore_action=source_restore_action,
                source_restore_payload=source_restore_payload,
                source_back_label=source_back_label,
            )
            return
        await _show_workspace_change_preview_from_callback(
            query,
            services,
            ui_state,
            user_id=user_id,
            relative_path=payload["relative_path"],
            status_code=payload["status_code"],
            page=int(payload.get("page", 0)),
            back_target=str(payload.get("back_target", "none")),
        )
        return

    if action == "context_bundle_page":
        await query.answer()
        source_restore_action, source_restore_payload, source_back_label = _callback_source_restore_values(payload)
        await _show_context_bundle_from_callback(
            query,
            services,
            ui_state,
            user_id=user_id,
            page=int(payload["page"]),
            back_target=str(payload.get("back_target", "none")),
            source_restore_action=source_restore_action,
            source_restore_payload=source_restore_payload,
            source_back_label=source_back_label,
        )
        return

    if action == "context_bundle_open_item":
        state = await services.snapshot_runtime_state()
        bundle = ui_state.get_context_bundle(user_id, state.provider, state.workspace_id)
        if bundle is None:
            await query.answer("Context bundle is empty.", show_alert=True)
            return
        item_index = int(payload["item_index"])
        if item_index < 0 or item_index >= len(bundle.items):
            await query.answer("This context item no longer exists.", show_alert=True)
            return
        item = bundle.items[item_index]
        await query.answer()
        source_restore_action, source_restore_payload, source_back_label = _callback_source_restore_values(payload)
        if item.kind == "change":
            await _show_context_bundle_change_preview_from_callback(
                query,
                services,
                ui_state,
                user_id=user_id,
                relative_path=item.relative_path,
                status_code=item.status_code or "??",
                page=int(payload.get("page", 0)),
                back_target=str(payload.get("back_target", "none")),
                source_restore_action=source_restore_action,
                source_restore_payload=source_restore_payload,
                source_back_label=source_back_label,
            )
            return
        await _show_context_bundle_file_preview_from_callback(
            query,
            services,
            ui_state,
            user_id=user_id,
            relative_path=item.relative_path,
            page=int(payload.get("page", 0)),
            back_target=str(payload.get("back_target", "none")),
            source_restore_action=source_restore_action,
            source_restore_payload=source_restore_payload,
            source_back_label=source_back_label,
        )
        return

    if action == "context_bundle_remove":
        state = await services.snapshot_runtime_state()
        was_bundle_chat_active = ui_state.context_bundle_chat_active(
            user_id,
            state.provider,
            state.workspace_id,
        )
        try:
            bundle = ui_state.remove_context_item(
                user_id,
                state.provider,
                state.workspace_id,
                int(payload["item_index"]),
            )
        except IndexError:
            await query.answer("This context item no longer exists.", show_alert=True)
            return
        if bundle is None:
            ui_state.clear_pending_text_action(user_id)
        await query.answer()
        source_restore_action, source_restore_payload, source_back_label = _callback_source_restore_values(payload)
        await _show_context_bundle_from_callback(
            query,
            services,
            ui_state,
            user_id=user_id,
            page=int(payload.get("page", 0)),
            back_target=str(payload.get("back_target", "none")),
            notice=(
                "Removed item from context bundle. Bundle chat was turned off because the bundle is empty."
                if bundle is None and was_bundle_chat_active
                else "Removed item from context bundle."
            ),
            source_restore_action=source_restore_action,
            source_restore_payload=source_restore_payload,
            source_back_label=source_back_label,
        )
        return

    if action == "context_bundle_preview_remove":
        state = await services.snapshot_runtime_state()
        was_bundle_chat_active = ui_state.context_bundle_chat_active(
            user_id,
            state.provider,
            state.workspace_id,
        )
        item = _ContextBundleItem(
            kind=str(payload["kind"]),
            relative_path=str(payload["relative_path"]),
            status_code=payload.get("status_code"),
        )
        try:
            bundle = ui_state.remove_context_item_by_value(
                user_id,
                state.provider,
                state.workspace_id,
                item,
            )
        except ValueError:
            await query.answer("This context item no longer exists.", show_alert=True)
            return
        if bundle is None:
            ui_state.clear_pending_text_action(user_id)
        await query.answer()
        source_restore_action, source_restore_payload, source_back_label = _callback_source_restore_values(payload)
        await _show_context_bundle_from_callback(
            query,
            services,
            ui_state,
            user_id=user_id,
            page=int(payload.get("page", 0)),
            back_target=str(payload.get("back_target", "none")),
            notice=(
                "Removed item from context bundle. Bundle chat was turned off because the bundle is empty."
                if bundle is None and was_bundle_chat_active
                else "Removed item from context bundle."
            ),
            source_restore_action=source_restore_action,
            source_restore_payload=source_restore_payload,
            source_back_label=source_back_label,
        )
        return

    if action == "context_bundle_clear":
        state = await services.snapshot_runtime_state()
        was_bundle_chat_active = ui_state.context_bundle_chat_active(
            user_id,
            state.provider,
            state.workspace_id,
        )
        ui_state.clear_context_bundle(user_id, state.provider, state.workspace_id)
        ui_state.clear_pending_text_action(user_id)
        await query.answer()
        source_restore_action, source_restore_payload, source_back_label = _callback_source_restore_values(payload)
        await _show_context_bundle_from_callback(
            query,
            services,
            ui_state,
            user_id=user_id,
            page=0,
            back_target=str(payload.get("back_target", "none")),
            notice="Cleared context bundle. Bundle chat was turned off." if was_bundle_chat_active else "Cleared context bundle.",
            source_restore_action=source_restore_action,
            source_restore_payload=source_restore_payload,
            source_back_label=source_back_label,
        )
        return

    if action == "context_bundle_chat_enable":
        state = await services.snapshot_runtime_state()
        if not ui_state.enable_context_bundle_chat(user_id, state.provider, state.workspace_id):
            await query.answer("Context bundle is empty.", show_alert=True)
            return
        await query.answer()
        source_restore_action, source_restore_payload, source_back_label = _callback_source_restore_values(payload)
        await _show_context_bundle_from_callback(
            query,
            services,
            ui_state,
            user_id=user_id,
            page=int(payload.get("page", 0)),
            back_target=str(payload.get("back_target", "none")),
            notice="Bundle chat enabled. New plain text messages will use the current context bundle.",
            source_restore_action=source_restore_action,
            source_restore_payload=source_restore_payload,
            source_back_label=source_back_label,
        )
        return

    if action == "context_bundle_chat_disable":
        await query.answer()
        ui_state.disable_context_bundle_chat(user_id)
        source_restore_action, source_restore_payload, source_back_label = _callback_source_restore_values(payload)
        await _show_context_bundle_from_callback(
            query,
            services,
            ui_state,
            user_id=user_id,
            page=int(payload.get("page", 0)),
            back_target=str(payload.get("back_target", "none")),
            notice="Bundle chat disabled.",
            source_restore_action=source_restore_action,
            source_restore_payload=source_restore_payload,
            source_back_label=source_back_label,
        )
        return

    if action == "context_bundle_ask":
        state = await services.snapshot_runtime_state()
        bundle = ui_state.get_context_bundle(user_id, state.provider, state.workspace_id)
        if bundle is None or not bundle.items:
            await query.answer("Context bundle is empty.", show_alert=True)
            return

        await query.answer()
        source_restore_action, source_restore_payload, source_back_label = _callback_source_restore_values(payload)
        source_payload = _callback_source_restore_payload(
            source_restore_action=source_restore_action,
            source_restore_payload=source_restore_payload,
            source_back_label=source_back_label,
        )
        pending_payload: dict[str, Any] = {
            "items": tuple(bundle.items),
            "page": int(payload.get("page", 0)),
            "back_target": str(payload.get("back_target", "none")),
        }
        if pending_payload["back_target"] == "status" and query.message is not None:
            pending_payload["source_message"] = query.message
            pending_payload["status_success_notice"] = "Asked agent with the current context bundle."
        ui_state.set_pending_text_action(
            user_id,
            "context_bundle_agent_prompt",
            **pending_payload,
        )
        await _edit_query_message(
            query,
            (
                "Send your request for the current context bundle as the next plain text message.\n"
                "The agent will read the listed files and inspect the listed Git changes from the current workspace."
            ),
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        _callback_button(
                            ui_state,
                            user_id,
                            "Cancel Ask",
                            "context_bundle_ask_cancel",
                            page=int(payload.get("page", 0)),
                            back_target=str(payload.get("back_target", "none")),
                            **source_payload,
                        )
                    ]
                ]
            ),
        )
        return

    if action == "context_bundle_ask_last_request":
        state = await services.snapshot_runtime_state()
        bundle = ui_state.get_context_bundle(user_id, state.provider, state.workspace_id)
        back_target = str(payload.get("back_target", "none"))
        source_restore_action, source_restore_payload, source_back_label = _callback_source_restore_values(payload)
        if bundle is None or not bundle.items:
            await query.answer()
            await _show_context_bundle_from_callback(
                query,
                services,
                ui_state,
                user_id=user_id,
                page=int(payload.get("page", 0)),
                back_target=back_target,
                notice="Context bundle is empty.",
                source_restore_action=source_restore_action,
                source_restore_payload=source_restore_payload,
                source_back_label=source_back_label,
            )
            return
        last_request_text = ui_state.get_last_request_text(user_id, state.workspace_id)
        if last_request_text is None:
            await query.answer()
            await _show_context_bundle_from_callback(
                query,
                services,
                ui_state,
                user_id=user_id,
                page=int(payload.get("page", 0)),
                back_target=back_target,
                notice="No previous request text is available in this workspace.",
                source_restore_action=source_restore_action,
                source_restore_payload=source_restore_payload,
                source_back_label=source_back_label,
            )
            return
        await query.answer()
        if query.message is None:
            return
        after_turn_success = None
        on_prepare_failure = None
        on_turn_failure = None
        if back_target == "status":
            after_turn_success, on_prepare_failure, on_turn_failure = _status_turn_callbacks(
                query,
                services,
                ui_state,
                user_id=user_id,
                success_notice="Asked agent with the last request using the current context bundle.",
            )
        await _run_context_bundle_request_on_message(
            query.message,
            user_id,
            services,
            ui_state,
            items=tuple(bundle.items),
            request_text=last_request_text,
            application=application,
            after_turn_success=after_turn_success,
            on_prepare_failure=on_prepare_failure,
            on_turn_failure=on_turn_failure,
        )
        return

    if action == "context_bundle_ask_cancel":
        await query.answer()
        ui_state.clear_pending_text_action(user_id)
        source_restore_action, source_restore_payload, source_back_label = _callback_source_restore_values(payload)
        await _show_context_bundle_from_callback(
            query,
            services,
            ui_state,
            user_id=user_id,
            page=int(payload.get("page", 0)),
            back_target=str(payload.get("back_target", "none")),
            notice="Context bundle request cancelled.",
            source_restore_action=source_restore_action,
            source_restore_payload=source_restore_payload,
            source_back_label=source_back_label,
        )
        return

    if action == "context_items_ask_cancel":
        await query.answer()
        ui_state.clear_pending_text_action(user_id)
        await _restore_context_items_source_from_callback(
            query,
            services,
            ui_state,
            user_id=user_id,
            restore_action=str(payload.get("restore_action", "")),
            restore_payload=dict(payload.get("restore_payload", {})),
            notice=str(payload.get("notice", "Request cancelled.")),
        )
        return

    if action == "workspace_file_ask_cancel":
        await query.answer()
        ui_state.clear_pending_text_action(user_id)
        if payload.get("source") == "search":
            await _show_workspace_search_file_preview_from_callback(
                query,
                services,
                ui_state,
                user_id=user_id,
                relative_path=payload["relative_path"],
                query_text=payload["query_text"],
                page=int(payload.get("page", 0)),
                back_target=str(payload.get("back_target", "none")),
            )
            return
        if payload.get("source") == "bundle":
            source_restore_action, source_restore_payload, source_back_label = _callback_source_restore_values(payload)
            await _show_context_bundle_file_preview_from_callback(
                query,
                services,
                ui_state,
                user_id=user_id,
                relative_path=payload["relative_path"],
                page=int(payload.get("page", 0)),
                back_target=str(payload.get("back_target", "none")),
                source_restore_action=source_restore_action,
                source_restore_payload=source_restore_payload,
                source_back_label=source_back_label,
            )
            return
        await _show_workspace_file_preview_from_callback(
            query,
            services,
            ui_state,
            user_id=user_id,
            relative_path=payload["relative_path"],
            page=int(payload.get("page", 0)),
            back_target=str(payload.get("back_target", "none")),
        )
        return

    if action == "set_selection":
        await _set_selection_from_callback(
            query,
            services,
            ui_state,
            user_id=user_id,
            kind=payload["kind"],
            value=payload["value"],
            application=application,
            back_target=str(payload.get("back_target", "none")),
        )
        return

    if action == "set_selection_retry_last_turn":
        await _set_selection_from_callback(
            query,
            services,
            ui_state,
            user_id=user_id,
            kind=payload["kind"],
            value=payload["value"],
            application=application,
            retry_after_update=True,
            back_target=str(payload.get("back_target", "none")),
        )
        return

    await query.answer("Unknown action.", show_alert=True)


def _callback_button(
    ui_state: TelegramUiState,
    user_id: int,
    text: str,
    action: str,
    **payload: Any,
) -> InlineKeyboardButton:
    token = ui_state.create(user_id, action, **payload)
    return InlineKeyboardButton(text=text, callback_data=f"{CALLBACK_PREFIX}{token}")


def _build_runtime_status_view(
    *,
    provider: str,
    workspace_id: str,
    workspace_label: str,
    workspace_path: str,
    git_status,
    session,
    session_title: str | None,
    history_entries,
    history_count: int,
    user_id: int,
    ui_state: TelegramUiState,
    is_admin: bool,
    notice: str | None = None,
):
    lines = []
    if notice:
        lines.append(notice)
    lines.append(
        f"Bot status for {resolve_provider_profile(provider).display_name} in {workspace_label}"
    )
    lines.append(f"Workspace ID: {workspace_id}")
    lines.append(f"Path: {workspace_path}")

    if session is None:
        lines.append("Session: none (will start on first request)")
    else:
        lines.append(f"Session: {session.session_id or 'pending'}")
        if session_title is not None:
            lines.append(f"Session title: {session_title}")

    get_selection = None if session is None else getattr(session, "get_selection", None)
    if callable(get_selection):
        try:
            model_selection = get_selection("model")
        except Exception:
            model_selection = None
        try:
            mode_selection = get_selection("mode")
        except Exception:
            mode_selection = None
        if model_selection is not None:
            lines.append(f"Model: {_current_choice_label(model_selection)}")
        if mode_selection is not None:
            lines.append(f"Mode: {_current_choice_label(mode_selection)}")

    lines.append(
        f"Pending input: {_pending_text_action_label(ui_state.get_pending_text_action(user_id))}"
    )
    lines.append(f"Local sessions: {history_count}")
    recent_history_entries, recent_history_total = _status_recent_history_entries(
        history_entries,
        current_session_id=None if session is None else session.session_id,
    )
    lines.extend(
        _status_recent_session_preview_lines(
            recent_history_entries,
            total_count=recent_history_total,
        )
    )
    lines.append(f"Workspace changes: {_status_workspace_changes_summary(git_status)}")
    lines.extend(_status_workspace_change_preview_lines(git_status))
    last_turn = ui_state.get_last_turn(user_id, provider, workspace_id)
    if last_turn is None:
        lines.append("Last turn replay: none")
    else:
        replay_snippet = _status_text_snippet(last_turn.title_hint) or "untitled turn"
        lines.append(f"Last turn replay: available ({replay_snippet})")
    last_request_text = ui_state.get_last_request_text(user_id, workspace_id)
    if last_request_text is None:
        lines.append("Last request text: none")
    else:
        lines.append(
            f"Last request text: {_status_text_snippet(last_request_text) or '[empty]'}"
        )
    last_turn_available = last_turn is not None

    bundle = ui_state.get_context_bundle(user_id, provider, workspace_id)
    bundle_count = 0 if bundle is None else len(bundle.items)
    workspace_changes_available = _status_workspace_changes_available(git_status)
    lines.append(f"Context bundle: {bundle_count} item{'s' if bundle_count != 1 else ''}")
    lines.append(
        f"Bundle chat: {'on' if ui_state.context_bundle_chat_active(user_id, provider, workspace_id) else 'off'}"
    )
    lines.extend(_status_context_bundle_preview_lines(bundle))

    if session is None:
        lines.append("Agent commands cached: unknown until a live session starts.")
    elif session.session_id is None:
        lines.append("Agent commands cached: waiting for session start.")
    else:
        cached_commands = tuple(getattr(session, "available_commands", ()) or ())
        lines.append(f"Agent commands cached: {len(cached_commands)}")
        lines.extend(_status_agent_command_preview_lines(cached_commands))
        capabilities = getattr(session, "capabilities", None)
        if capabilities is not None:
            lines.append(
                "Prompt input: "
                f"img={'yes' if getattr(capabilities, 'supports_image_prompt', False) else 'no'},"
                f"audio={'yes' if getattr(capabilities, 'supports_audio_prompt', False) else 'no'},"
                f"docs={'yes' if getattr(capabilities, 'supports_embedded_context_prompt', False) else 'no'}"
            )
            lines.append(
                "Session control: "
                f"list={'yes' if getattr(capabilities, 'can_list', False) else 'no'},"
                f"resume={'yes' if getattr(capabilities, 'can_resume', False) else 'no'}"
            )

    lines.append(
        "Main keyboard: New Session, Retry/Fork Last Turn, Model / Mode, Restart Agent."
    )
    if is_admin:
        lines.append("Admin switches stay on the main keyboard.")

    buttons = [
        [
            _callback_button(ui_state, user_id, "Refresh", "runtime_status_page"),
            _callback_button(ui_state, user_id, "Session History", "runtime_status_open", target="history"),
        ],
    ]
    if is_admin:
        buttons[0].append(
            _callback_button(
                ui_state,
                user_id,
                "Provider Sessions",
                "runtime_status_open",
                target="provider_sessions",
            )
        )
    buttons.extend(
        _status_recent_session_quick_buttons(
            ui_state,
            user_id=user_id,
            entries=recent_history_entries,
            can_retry_last_turn=last_turn_available,
        )
    )
    control_buttons = []
    if ui_state.get_pending_text_action(user_id) is not None:
        control_buttons.append(
            _callback_button(ui_state, user_id, "Cancel Pending Input", "runtime_status_cancel_pending")
        )
    if bundle_count > 0:
        if ui_state.context_bundle_chat_active(user_id, provider, workspace_id):
            control_buttons.append(
                _callback_button(ui_state, user_id, "Stop Bundle Chat", "runtime_status_stop_bundle_chat")
            )
        else:
            control_buttons.append(
                _callback_button(ui_state, user_id, "Start Bundle Chat", "runtime_status_start_bundle_chat")
            )
    if control_buttons:
        buttons.append(control_buttons)
    buttons.append(
        [
            _callback_button(
                ui_state,
                user_id,
                "New Session",
                "runtime_status_control",
                target="new_session",
            ),
            _callback_button(
                ui_state,
                user_id,
                "Restart Agent",
                "runtime_status_control",
                target="restart_agent",
            ),
        ]
    )
    if last_turn_available:
        buttons.append(
            [
                _callback_button(
                    ui_state,
                    user_id,
                    "Retry Last Turn",
                    "runtime_status_control",
                    target="retry_last_turn",
                ),
                _callback_button(
                    ui_state,
                    user_id,
                    "Fork Last Turn",
                    "runtime_status_control",
                    target="fork_last_turn",
                ),
            ]
        )
    buttons.append(
        [
            _callback_button(
                ui_state,
                user_id,
                "Model / Mode",
                "runtime_status_control",
                target="model_mode",
            )
        ]
    )
    if is_admin:
        buttons.append(
            [
                _callback_button(
                    ui_state,
                    user_id,
                    "Switch Agent",
                    "runtime_status_control",
                    target="switch_agent",
                ),
                _callback_button(
                    ui_state,
                    user_id,
                    "Switch Workspace",
                    "runtime_status_control",
                    target="switch_workspace",
                ),
            ]
        )
    if session is not None and getattr(session, "session_id", None) is not None:
        buttons.extend(
            _status_selection_quick_rows(
                ui_state,
                user_id=user_id,
                model_selection=model_selection,
                mode_selection=mode_selection,
                can_retry_last_turn=last_turn_available,
            )
        )
    if session is not None and getattr(session, "session_id", None) is not None:
        buttons.extend(
            _status_agent_command_quick_buttons(
                ui_state,
                user_id=user_id,
                commands=tuple(getattr(session, "available_commands", ()) or ()),
            )
        )
    if bundle_count > 0:
        bundle_buttons = [
            _callback_button(
                ui_state,
                user_id,
                "Ask Agent With Context",
                "runtime_status_control",
                target="context_bundle_ask",
            )
        ]
        if last_request_text is not None:
            bundle_buttons.append(
                _callback_button(
                    ui_state,
                    user_id,
                    "Bundle + Last Request",
                    "runtime_status_control",
                    target="context_bundle_ask_last_request",
                )
            )
        buttons.append(bundle_buttons)
        buttons.append(
            [
                _callback_button(
                    ui_state,
                    user_id,
                    "Clear Bundle",
                    "runtime_status_control",
                    target="context_bundle_clear",
                ),
            ]
        )
    if workspace_changes_available:
        change_buttons = [
            _callback_button(
                ui_state,
                user_id,
                "Ask Agent With Current Changes",
                "runtime_status_control",
                target="workspace_changes_ask_agent",
            )
        ]
        if last_request_text is not None:
            change_buttons.append(
                _callback_button(
                    ui_state,
                    user_id,
                    "Ask With Last Request",
                    "runtime_status_control",
                    target="workspace_changes_ask_last_request",
                )
            )
        buttons.append(change_buttons)
        buttons.append(
            [
                _callback_button(
                    ui_state,
                    user_id,
                    "Start Bundle Chat With Changes",
                    "runtime_status_control",
                    target="workspace_changes_start_bundle_chat",
                ),
                _callback_button(
                    ui_state,
                    user_id,
                    "Add All Changes to Context",
                    "runtime_status_control",
                    target="workspace_changes_add_all",
                ),
            ]
        )
    buttons.extend(
        [
            [
                _callback_button(ui_state, user_id, "Agent Commands", "runtime_status_open", target="commands"),
                _callback_button(ui_state, user_id, "Workspace Files", "runtime_status_open", target="files"),
            ],
            [
                _callback_button(ui_state, user_id, "Workspace Search", "runtime_status_open", target="search"),
                _callback_button(ui_state, user_id, "Workspace Changes", "runtime_status_open", target="changes"),
            ],
            [
                _callback_button(ui_state, user_id, "Context Bundle", "runtime_status_open", target="bundle"),
            ],
        ]
    )
    return "\n".join(lines), InlineKeyboardMarkup(buttons)


def _build_history_view(
    *,
    entries,
    provider: str,
    workspace_id: str,
    workspace_label: str,
    user_id: int,
    page: int,
    ui_state: TelegramUiState,
    active_session_id: str | None = None,
    notice: str | None = None,
    show_provider_sessions: bool = False,
    back_target: str = "none",
):
    lines = []
    if notice:
        lines.append(notice)

    lines.append(
        f"Session history for {resolve_provider_profile(provider).display_name} in {workspace_label}"
    )
    buttons = []
    if not entries:
        lines.append("No local session history.")
        if show_provider_sessions:
            buttons.append(
                [
                    _callback_button(
                        ui_state,
                        user_id,
                        "Provider Sessions",
                        "history_provider_sessions",
                        cursor=None,
                        previous_cursors=(),
                        history_page=page,
                        back_target="history",
                        history_back_target=back_target,
                    )
                ]
            )
        if back_target == "status":
            buttons.append(
                [
                    _callback_button(
                        ui_state,
                        user_id,
                        "Back to Bot Status",
                        "runtime_status_page",
                    )
                ]
            )
        markup = None if not buttons else InlineKeyboardMarkup(buttons)
        return "\n".join(lines), markup

    page_count = max(1, (len(entries) + HISTORY_PAGE_SIZE - 1) // HISTORY_PAGE_SIZE)
    page = min(max(page, 0), page_count - 1)
    start = page * HISTORY_PAGE_SIZE
    visible_entries = entries[start : start + HISTORY_PAGE_SIZE]
    can_retry_last_turn = ui_state.get_last_turn(user_id, provider, workspace_id) is not None
    for offset, entry in enumerate(visible_entries, start=1):
        is_current = entry.session_id == active_session_id
        label = entry.title or entry.session_id
        if is_current:
            label = f"{label} [current]"
        lines.append(f"{start + offset}. {label}")
        lines.append(f"updated={entry.updated_at}")
        buttons.append(
            [
                _callback_button(
                    ui_state,
                    user_id,
                    f"{'Current' if is_current else 'Run'} {start + offset}",
                    "noop" if is_current else "history_run",
                    **(
                        {"notice": "Already using this session."}
                        if is_current
                        else {
                            "session_id": entry.session_id,
                            "page": page,
                            "back_target": back_target,
                        }
                    ),
                ),
                _callback_button(
                    ui_state,
                    user_id,
                    f"Rename {start + offset}",
                    "history_rename",
                    session_id=entry.session_id,
                    title=entry.title or entry.session_id,
                    page=page,
                    back_target=back_target,
                ),
                _callback_button(
                    ui_state,
                    user_id,
                    f"Delete {start + offset}",
                    "history_delete",
                    session_id=entry.session_id,
                    page=page,
                    back_target=back_target,
                ),
            ]
        )
        if can_retry_last_turn and not is_current:
            buttons.append(
                [
                    _callback_button(
                        ui_state,
                        user_id,
                        f"Run+Retry {start + offset}",
                        "history_run_retry_last_turn",
                        session_id=entry.session_id,
                        page=page,
                        back_target=back_target,
                    )
                ]
            )

    if page_count > 1:
        nav = []
        if page > 0:
            nav.append(
                _callback_button(
                    ui_state,
                    user_id,
                    "Prev",
                    "history_page",
                    page=page - 1,
                    back_target=back_target,
                )
            )
        if page < page_count - 1:
            nav.append(
                _callback_button(
                    ui_state,
                    user_id,
                    "Next",
                    "history_page",
                    page=page + 1,
                    back_target=back_target,
                )
        )
        if nav:
            buttons.append(nav)

    if show_provider_sessions:
        buttons.append(
            [
                _callback_button(
                    ui_state,
                    user_id,
                    "Provider Sessions",
                    "history_provider_sessions",
                    cursor=None,
                    previous_cursors=(),
                    history_page=page,
                    back_target="history",
                    history_back_target=back_target,
                )
            ]
        )

    if back_target == "status":
        buttons.append(
            [
                _callback_button(
                    ui_state,
                    user_id,
                    "Back to Bot Status",
                    "runtime_status_page",
                )
            ]
        )

    return "\n".join(lines), InlineKeyboardMarkup(buttons)


def _build_provider_sessions_view(
    *,
    entries,
    next_cursor: str | None,
    supported: bool,
    provider: str,
    workspace_id: str,
    workspace_label: str,
    user_id: int,
    ui_state: TelegramUiState,
    active_session_id: str | None,
    cursor: str | None,
    previous_cursors: tuple[str | None, ...],
    history_page: int,
    back_target: str,
    history_back_target: str,
    notice: str | None = None,
):
    lines = []
    if notice:
        lines.append(notice)

    lines.append(
        f"Provider sessions for {resolve_provider_profile(provider).display_name} in {workspace_label}"
    )
    lines.append("Only sessions inside the current workspace are shown.")

    buttons = []
    can_retry_last_turn = ui_state.get_last_turn(user_id, provider, workspace_id) is not None
    if not supported:
        lines.append("Provider session browsing is not available for this agent.")
    elif not entries:
        lines.append("No provider sessions found.")
    else:
        for index, entry in enumerate(entries, start=1):
            is_current = entry.session_id == active_session_id
            label = entry.title or entry.session_id
            if is_current:
                label = f"{label} [current]"
            lines.append(f"{index}. {label}")
            if entry.cwd_label != ".":
                lines.append(f"cwd={entry.cwd_label}")
            lines.append(f"session={entry.session_id}")
            if entry.updated_at:
                lines.append(f"updated={entry.updated_at}")
            buttons.append(
                [
                    _callback_button(
                        ui_state,
                        user_id,
                        f"{'Current' if is_current else 'Run'} {index}",
                        "noop" if is_current else "provider_session_run",
                        **(
                            {"notice": "Already using this session."}
                            if is_current
                            else {
                                "session_id": entry.session_id,
                                "title": entry.title,
                                "cursor": cursor,
                                "previous_cursors": previous_cursors,
                                "history_page": history_page,
                                "back_target": back_target,
                                "history_back_target": history_back_target,
                            }
                        ),
                    )
                ]
            )
            if can_retry_last_turn and not is_current:
                buttons[-1].append(
                    _callback_button(
                        ui_state,
                        user_id,
                        f"Run+Retry {index}",
                        "provider_session_run_retry_last_turn",
                        session_id=entry.session_id,
                        title=entry.title,
                        cursor=cursor,
                        previous_cursors=previous_cursors,
                        history_page=history_page,
                        back_target=back_target,
                        history_back_target=history_back_target,
                    )
                )

    nav = []
    if previous_cursors:
        nav.append(
            _callback_button(
                ui_state,
                user_id,
                "Prev",
                "provider_sessions_page",
                cursor=previous_cursors[-1],
                previous_cursors=previous_cursors[:-1],
                history_page=history_page,
                back_target=back_target,
                history_back_target=history_back_target,
            )
        )
    if next_cursor is not None:
        nav.append(
            _callback_button(
                ui_state,
                user_id,
                "Next",
                "provider_sessions_page",
                cursor=next_cursor,
                previous_cursors=previous_cursors + (cursor,),
                history_page=history_page,
                back_target=back_target,
                history_back_target=history_back_target,
            )
        )
    if nav:
        buttons.append(nav)

    if back_target == "status":
        back_label = "Back to Bot Status"
        back_action = "runtime_status_page"
        back_payload: dict[str, Any] = {}
    else:
        back_label = "Back to History"
        back_action = "history_page"
        back_payload = {"page": history_page, "back_target": history_back_target}
    buttons.append(
        [
            _callback_button(
                ui_state,
                user_id,
                back_label,
                back_action,
                **back_payload,
            )
        ]
    )

    return "\n".join(lines), InlineKeyboardMarkup(buttons)


def _build_agent_commands_view(
    *,
    commands,
    provider: str,
    workspace_label: str,
    user_id: int,
    page: int,
    ui_state: TelegramUiState,
    session_id: str | None,
    back_target: str = "none",
    notice: str | None = None,
):
    lines = []
    if notice:
        lines.append(notice)

    lines.append(
        f"Agent commands for {resolve_provider_profile(provider).display_name} in {workspace_label}"
    )
    lines.append(f"Session: {session_id or 'none (will start on first command)'}")

    buttons = []
    if not commands:
        lines.append("No agent commands available.")
        if back_target == "status":
            buttons.append(
                [
                    _callback_button(
                        ui_state,
                        user_id,
                        "Back to Bot Status",
                        "runtime_status_page",
                    )
                ]
            )
        markup = None if not buttons else InlineKeyboardMarkup(buttons)
        return "\n".join(lines), markup

    page_count = max(1, (len(commands) + COMMAND_PAGE_SIZE - 1) // COMMAND_PAGE_SIZE)
    page = min(max(page, 0), page_count - 1)
    start = page * COMMAND_PAGE_SIZE
    visible_commands = commands[start : start + COMMAND_PAGE_SIZE]

    for offset, command in enumerate(visible_commands, start=1):
        index = start + offset
        lines.append(f"{index}. {_agent_command_name(command.name)}")
        description = (command.description or "").strip()
        if description:
            lines.append(description)
        if command.hint:
            lines.append(f"args: {command.hint}")
        buttons.append(
            [
                _callback_button(
                    ui_state,
                    user_id,
                    f"{'Args' if command.hint else 'Run'} {index}",
                    "agent_command_use",
                    command_name=command.name,
                    hint=command.hint,
                    page=page,
                    back_target=back_target,
                )
            ]
        )

    if page_count > 1:
        nav = []
        if page > 0:
            nav.append(
                _callback_button(
                    ui_state,
                    user_id,
                    "Prev",
                    "agent_commands_page",
                    page=page - 1,
                    back_target=back_target,
                )
            )
        if page < page_count - 1:
            nav.append(
                _callback_button(
                    ui_state,
                    user_id,
                    "Next",
                    "agent_commands_page",
                    page=page + 1,
                    back_target=back_target,
                )
            )
        if nav:
            buttons.append(nav)

    if back_target == "status":
        buttons.append(
            [
                _callback_button(
                    ui_state,
                    user_id,
                    "Back to Bot Status",
                    "runtime_status_page",
                )
            ]
        )

    return "\n".join(lines), InlineKeyboardMarkup(buttons)


def _append_back_to_status_button(
    buttons: list[list[InlineKeyboardButton]],
    *,
    ui_state: TelegramUiState,
    user_id: int,
    back_target: str,
) -> None:
    if back_target == "status":
        button = _callback_button(
            ui_state,
            user_id,
            "Back to Bot Status",
            "runtime_status_page",
        )
    elif back_target == "workspace_changes_follow_up":
        button = _callback_button(
            ui_state,
            user_id,
            "Back to Change Update",
            "workspace_changes_follow_up_page",
        )
    else:
        return
    buttons.append(
        [button]
    )


def _callback_source_restore_payload(
    *,
    source_restore_action: str | None,
    source_restore_payload: dict[str, Any] | None,
    source_back_label: str | None,
) -> dict[str, Any]:
    if source_restore_action is None:
        return {}
    payload: dict[str, Any] = {
        "source_restore_action": source_restore_action,
        "source_restore_payload": {} if source_restore_payload is None else dict(source_restore_payload),
    }
    if source_back_label is not None:
        payload["source_back_label"] = source_back_label
    return payload


def _callback_source_restore_values(
    payload: dict[str, Any],
) -> tuple[str | None, dict[str, Any] | None, str | None]:
    source_restore_action = payload.get("source_restore_action")
    if not source_restore_action:
        return None, None, None
    raw_restore_payload = payload.get("source_restore_payload")
    restore_payload = dict(raw_restore_payload) if isinstance(raw_restore_payload, dict) else {}
    source_back_label = payload.get("source_back_label")
    return (
        str(source_restore_action),
        restore_payload,
        None if source_back_label is None else str(source_back_label),
    )


def _append_restore_source_or_status_button(
    buttons: list[list[InlineKeyboardButton]],
    *,
    ui_state: TelegramUiState,
    user_id: int,
    back_target: str,
    source_restore_action: str | None,
    source_restore_payload: dict[str, Any] | None,
    source_back_label: str | None,
) -> None:
    if source_restore_action is not None:
        buttons.append(
            [
                _callback_button(
                    ui_state,
                    user_id,
                    source_back_label or "Back",
                    "restore_source_view",
                    restore_action=source_restore_action,
                    restore_payload={} if source_restore_payload is None else dict(source_restore_payload),
                )
            ]
        )
        return
    _append_back_to_status_button(
        buttons,
        ui_state=ui_state,
        user_id=user_id,
        back_target=back_target,
    )


def _source_restore_supplemental_buttons(
    *,
    source_restore_action: str | None,
    source_restore_payload: dict[str, Any] | None,
    source_back_label: str | None,
) -> tuple[tuple[str, str, dict[str, Any]], ...]:
    if source_restore_action is None:
        return ()
    return (
        (
            source_back_label or "Back",
            "restore_source_view",
            {
                "restore_action": source_restore_action,
                "restore_payload": {} if source_restore_payload is None else dict(source_restore_payload),
            },
        ),
    )


def _build_workspace_listing_view(
    *,
    listing,
    provider: str,
    workspace_label: str,
    user_id: int,
    page: int,
    ui_state: TelegramUiState,
    last_request_text: str | None,
    back_target: str = "none",
    notice: str | None = None,
):
    lines = []
    if notice:
        lines.append(notice)

    lines.append(
        f"Workspace files for {resolve_provider_profile(provider).display_name} in {workspace_label}"
    )
    lines.append(f"Path: {listing.relative_path or '.'}")

    if not listing.entries:
        lines.append("[empty directory]")
        buttons = []
        if listing.relative_path:
            buttons.append(
                [
                    _callback_button(
                        ui_state,
                        user_id,
                        "Up",
                        "workspace_open_dir",
                        relative_path=_parent_relative_path(listing.relative_path),
                    )
                ]
            )
        _append_back_to_status_button(
            buttons,
            ui_state=ui_state,
            user_id=user_id,
            back_target=back_target,
        )
        markup = None if not buttons else InlineKeyboardMarkup(buttons)
        return "\n".join(lines), markup

    page, page_count, visible_entries = _visible_workspace_entries(listing, page)

    buttons = []
    bundle_source_payload = _callback_source_restore_payload(
        source_restore_action="workspace_page",
        source_restore_payload={
            "relative_path": listing.relative_path,
            "page": page,
            "back_target": back_target,
        },
        source_back_label="Back to Folder",
    )
    for offset, entry in enumerate(visible_entries, start=1):
        index = page * WORKSPACE_PAGE_SIZE + offset
        lines.append(f"{index}. {entry.name}{'/' if entry.is_dir else ''}")
        buttons.append(
            [
                _callback_button(
                    ui_state,
                    user_id,
                    entry.name + ("/" if entry.is_dir else ""),
                    "workspace_open_dir" if entry.is_dir else "workspace_open_file",
                    relative_path=entry.relative_path,
                    page=page,
                    back_target=back_target,
                )
            ]
        )

    nav = []
    if listing.relative_path:
        nav.append(
            _callback_button(
                ui_state,
                user_id,
                "Up",
                "workspace_open_dir",
                relative_path=_parent_relative_path(listing.relative_path),
                back_target=back_target,
            )
        )
    if page > 0:
        nav.append(
            _callback_button(
                ui_state,
                user_id,
                "Prev",
                "workspace_page",
                relative_path=listing.relative_path,
                page=page - 1,
                back_target=back_target,
            )
        )
    if page < page_count - 1:
        nav.append(
            _callback_button(
                ui_state,
                user_id,
                "Next",
                "workspace_page",
                relative_path=listing.relative_path,
                page=page + 1,
                back_target=back_target,
            )
        )
    if nav:
        buttons.append(nav)

    if _visible_workspace_file_paths(listing, page):
        buttons.append(
            [
                _callback_button(
                    ui_state,
                    user_id,
                    "Ask Agent With Visible Files",
                    "workspace_page_ask_agent",
                    relative_path=listing.relative_path,
                    page=page,
                    back_target=back_target,
                ),
                _callback_button(
                    ui_state,
                    user_id,
                    "Start Bundle Chat With Visible Files",
                    "workspace_page_start_bundle_chat",
                    relative_path=listing.relative_path,
                    page=page,
                    back_target=back_target,
                    **bundle_source_payload,
                ),
            ]
        )
        if last_request_text is not None:
            buttons.append(
                [
                    _callback_button(
                        ui_state,
                        user_id,
                        "Ask With Last Request",
                        "workspace_page_ask_last_request",
                        relative_path=listing.relative_path,
                        page=page,
                        back_target=back_target,
                    ),
                ]
            )
        buttons.append(
            [
                _callback_button(
                    ui_state,
                    user_id,
                    "Add Visible Files to Context",
                    "workspace_page_add_context",
                    relative_path=listing.relative_path,
                    page=page,
                    back_target=back_target,
                    **bundle_source_payload,
                ),
            ]
        )

    buttons.append(
        [
            _callback_button(
                ui_state,
                user_id,
                "Open Context Bundle",
                "context_bundle_page",
                page=0,
                back_target=back_target,
                **bundle_source_payload,
            ),
        ]
    )
    _append_back_to_status_button(
        buttons,
        ui_state=ui_state,
        user_id=user_id,
        back_target=back_target,
    )

    return "\n".join(lines), InlineKeyboardMarkup(buttons)


def _build_workspace_search_results_view(
    *,
    search_results,
    provider: str,
    workspace_label: str,
    user_id: int,
    page: int,
    ui_state: TelegramUiState,
    last_request_text: str | None,
    back_target: str = "none",
    notice: str | None = None,
):
    lines = []
    if notice:
        lines.append(notice)

    lines.append(
        f"Workspace search for {resolve_provider_profile(provider).display_name} in {workspace_label}"
    )
    lines.append(f"Query: {search_results.query}")

    if not search_results.matches:
        lines.append("No matches found.")
        buttons: list[list[InlineKeyboardButton]] = []
        _append_back_to_status_button(
            buttons,
            ui_state=ui_state,
            user_id=user_id,
            back_target=back_target,
        )
        markup = None if not buttons else InlineKeyboardMarkup(buttons)
        return "\n".join(lines), markup

    page_count = max(
        1,
        (len(search_results.matches) + WORKSPACE_SEARCH_PAGE_SIZE - 1) // WORKSPACE_SEARCH_PAGE_SIZE,
    )
    page = min(max(page, 0), page_count - 1)
    start = page * WORKSPACE_SEARCH_PAGE_SIZE
    visible_matches = search_results.matches[start : start + WORKSPACE_SEARCH_PAGE_SIZE]

    buttons = []
    bundle_source_payload = _callback_source_restore_payload(
        source_restore_action="workspace_search_page",
        source_restore_payload={
            "query_text": search_results.query,
            "page": page,
            "back_target": back_target,
        },
        source_back_label="Back to Search",
    )
    for offset, match in enumerate(visible_matches, start=1):
        index = start + offset
        lines.append(f"{index}. {match.relative_path}:{match.line_number}")
        lines.append(match.line_text)
        buttons.append(
            [
                _callback_button(
                    ui_state,
                    user_id,
                    f"Open {index}",
                    "workspace_search_open_file",
                    relative_path=match.relative_path,
                    query_text=search_results.query,
                    page=page,
                    back_target=back_target,
                )
            ]
        )

    if search_results.truncated:
        lines.append("[results truncated]")

    if page_count > 1:
        nav = []
        if page > 0:
            nav.append(
                _callback_button(
                    ui_state,
                    user_id,
                    "Prev",
                    "workspace_search_page",
                    query_text=search_results.query,
                    page=page - 1,
                    back_target=back_target,
                )
            )
        if page < page_count - 1:
            nav.append(
                _callback_button(
                    ui_state,
                    user_id,
                    "Next",
                    "workspace_search_page",
                    query_text=search_results.query,
                    page=page + 1,
                    back_target=back_target,
                )
            )
        if nav:
            buttons.append(nav)

    buttons.append(
        [
            _callback_button(
                ui_state,
                user_id,
                "Ask Agent With Matching Files",
                "workspace_search_ask_agent",
                query_text=search_results.query,
                page=page,
                back_target=back_target,
            ),
            _callback_button(
                ui_state,
                user_id,
                "Start Bundle Chat With Matching Files",
                "workspace_search_start_bundle_chat",
                query_text=search_results.query,
                page=page,
                back_target=back_target,
                **bundle_source_payload,
            ),
        ]
    )
    if last_request_text is not None:
        buttons.append(
            [
                _callback_button(
                    ui_state,
                    user_id,
                    "Ask With Last Request",
                    "workspace_search_ask_last_request",
                    query_text=search_results.query,
                    page=page,
                    back_target=back_target,
                ),
            ]
        )
    buttons.append(
        [
            _callback_button(
                ui_state,
                user_id,
                "Add Matching Files to Context",
                "workspace_search_add_context",
                query_text=search_results.query,
                back_target=back_target,
                **bundle_source_payload,
            ),
        ]
    )
    buttons.append(
        [
            _callback_button(
                ui_state,
                user_id,
                "Open Context Bundle",
                "context_bundle_page",
                page=0,
                back_target=back_target,
                **bundle_source_payload,
            ),
        ]
    )
    _append_back_to_status_button(
        buttons,
        ui_state=ui_state,
        user_id=user_id,
        back_target=back_target,
    )

    return "\n".join(lines), InlineKeyboardMarkup(buttons)


def _build_workspace_changes_view(
    *,
    git_status,
    provider: str,
    workspace_label: str,
    user_id: int,
    page: int,
    ui_state: TelegramUiState,
    last_request_text: str | None,
    back_target: str = "none",
    notice: str | None = None,
):
    lines = []
    if notice:
        lines.append(notice)

    lines.append(
        f"Workspace changes for {resolve_provider_profile(provider).display_name} in {workspace_label}"
    )

    if not git_status.is_git_repo:
        lines.append("Current workspace is not a Git repository.")
        buttons: list[list[InlineKeyboardButton]] = []
        _append_back_to_status_button(
            buttons,
            ui_state=ui_state,
            user_id=user_id,
            back_target=back_target,
        )
        markup = None if not buttons else InlineKeyboardMarkup(buttons)
        return "\n".join(lines), markup

    lines.append(f"Branch: {git_status.branch_line or 'unknown'}")
    if not git_status.entries:
        lines.append("No working tree changes.")
        buttons = []
        _append_back_to_status_button(
            buttons,
            ui_state=ui_state,
            user_id=user_id,
            back_target=back_target,
        )
        markup = None if not buttons else InlineKeyboardMarkup(buttons)
        return "\n".join(lines), markup

    page_count = max(1, (len(git_status.entries) + WORKSPACE_CHANGES_PAGE_SIZE - 1) // WORKSPACE_CHANGES_PAGE_SIZE)
    page = min(max(page, 0), page_count - 1)
    start = page * WORKSPACE_CHANGES_PAGE_SIZE
    visible_entries = git_status.entries[start : start + WORKSPACE_CHANGES_PAGE_SIZE]

    buttons = []
    bundle_source_payload = _callback_source_restore_payload(
        source_restore_action="workspace_changes_page",
        source_restore_payload={
            "page": page,
            "back_target": back_target,
        },
        source_back_label="Back to Changes",
    )
    for offset, entry in enumerate(visible_entries, start=1):
        index = start + offset
        lines.append(f"{index}. [{entry.status_code}] {entry.display_path}")
        buttons.append(
            [
                _callback_button(
                    ui_state,
                    user_id,
                    f"Open {index}",
                    "workspace_change_open",
                    relative_path=entry.relative_path,
                    status_code=entry.status_code,
                    page=page,
                    back_target=back_target,
                )
            ]
        )

    if page_count > 1:
        nav = []
        if page > 0:
            nav.append(
                _callback_button(
                    ui_state,
                    user_id,
                    "Prev",
                    "workspace_changes_page",
                    page=page - 1,
                    back_target=back_target,
                )
            )
        if page < page_count - 1:
            nav.append(
                _callback_button(
                    ui_state,
                    user_id,
                    "Next",
                    "workspace_changes_page",
                    page=page + 1,
                    back_target=back_target,
                )
            )
        if nav:
            buttons.append(nav)

    buttons.append(
        [
            _callback_button(
                ui_state,
                user_id,
                "Ask Agent With Current Changes",
                "workspace_changes_ask_agent",
                page=page,
                back_target=back_target,
            ),
            _callback_button(
                ui_state,
                user_id,
                "Start Bundle Chat With Changes",
                "workspace_changes_start_bundle_chat",
                page=page,
                back_target=back_target,
                **bundle_source_payload,
            ),
        ]
    )
    if last_request_text is not None:
        buttons.append(
            [
                _callback_button(
                    ui_state,
                    user_id,
                    "Ask With Last Request",
                    "workspace_changes_ask_last_request",
                    page=page,
                    back_target=back_target,
                ),
            ]
        )
    buttons.append(
        [
            _callback_button(
                ui_state,
                user_id,
                "Add All Changes to Context",
                "workspace_changes_add_all",
                page=page,
                back_target=back_target,
                **bundle_source_payload,
            ),
        ]
    )
    buttons.append(
        [
            _callback_button(
                ui_state,
                user_id,
                "Open Context Bundle",
                "context_bundle_page",
                page=0,
                back_target=back_target,
                **bundle_source_payload,
            ),
        ]
    )
    _append_back_to_status_button(
        buttons,
        ui_state=ui_state,
        user_id=user_id,
        back_target=back_target,
    )

    return "\n".join(lines), InlineKeyboardMarkup(buttons)


def _build_context_bundle_view(
    *,
    bundle: _ContextBundle | None,
    provider: str,
    workspace_label: str,
    user_id: int,
    page: int,
    ui_state: TelegramUiState,
    last_request_text: str | None,
    bundle_chat_active: bool,
    back_target: str = "none",
    notice: str | None = None,
    source_restore_action: str | None = None,
    source_restore_payload: dict[str, Any] | None = None,
    source_back_label: str | None = None,
):
    source_payload = _callback_source_restore_payload(
        source_restore_action=source_restore_action,
        source_restore_payload=source_restore_payload,
        source_back_label=source_back_label,
    )
    lines = []
    if notice:
        lines.append(notice)

    lines.append(
        f"Context bundle for {resolve_provider_profile(provider).display_name} in {workspace_label}"
    )

    if bundle is None or not bundle.items:
        lines.append("Context bundle is empty.")
        buttons: list[list[InlineKeyboardButton]] = []
        _append_restore_source_or_status_button(
            buttons,
            ui_state=ui_state,
            user_id=user_id,
            back_target=back_target,
            source_restore_action=source_restore_action,
            source_restore_payload=source_restore_payload,
            source_back_label=source_back_label,
        )
        markup = None if not buttons else InlineKeyboardMarkup(buttons)
        return "\n".join(lines), markup

    lines.append(f"Items: {len(bundle.items)}")
    lines.append(f"Bundle chat: {'on' if bundle_chat_active else 'off'}")

    page_count = max(1, (len(bundle.items) + CONTEXT_BUNDLE_PAGE_SIZE - 1) // CONTEXT_BUNDLE_PAGE_SIZE)
    page = min(max(page, 0), page_count - 1)
    start = page * CONTEXT_BUNDLE_PAGE_SIZE
    visible_items = bundle.items[start : start + CONTEXT_BUNDLE_PAGE_SIZE]

    buttons = []
    for offset, item in enumerate(visible_items, start=1):
        index = start + offset
        lines.append(f"{index}. {_context_bundle_item_label(item)}")
        buttons.append(
            [
                _callback_button(
                    ui_state,
                    user_id,
                    f"Open {index}",
                    "context_bundle_open_item",
                    item_index=index - 1,
                    page=page,
                    back_target=back_target,
                    **source_payload,
                ),
                _callback_button(
                    ui_state,
                    user_id,
                    f"Remove {index}",
                    "context_bundle_remove",
                    item_index=index - 1,
                    page=page,
                    back_target=back_target,
                    **source_payload,
                )
            ]
        )

    buttons.append(
        [
            _callback_button(
                ui_state,
                user_id,
                "Ask Agent With Context",
                "context_bundle_ask",
                page=page,
                back_target=back_target,
                **source_payload,
            ),
            _callback_button(
                ui_state,
                user_id,
                "Stop Bundle Chat" if bundle_chat_active else "Start Bundle Chat",
                "context_bundle_chat_disable" if bundle_chat_active else "context_bundle_chat_enable",
                page=page,
                back_target=back_target,
                **source_payload,
            ),
        ]
    )
    if last_request_text is not None:
        buttons.append(
            [
                _callback_button(
                    ui_state,
                    user_id,
                    "Ask With Last Request",
                    "context_bundle_ask_last_request",
                    page=page,
                    back_target=back_target,
                    **source_payload,
                ),
            ]
        )

    buttons.append(
        [
            _callback_button(
                ui_state,
                user_id,
                "Clear Bundle",
                "context_bundle_clear",
                page=page,
                back_target=back_target,
                **source_payload,
            ),
        ]
    )

    if page_count > 1:
        nav = []
        if page > 0:
            nav.append(
                _callback_button(
                    ui_state,
                    user_id,
                    "Prev",
                    "context_bundle_page",
                    page=page - 1,
                    back_target=back_target,
                    **source_payload,
                )
            )
        if page < page_count - 1:
            nav.append(
                _callback_button(
                    ui_state,
                    user_id,
                    "Next",
                    "context_bundle_page",
                    page=page + 1,
                    back_target=back_target,
                    **source_payload,
                )
            )
        if nav:
            buttons.append(nav)

    _append_restore_source_or_status_button(
        buttons,
        ui_state=ui_state,
        user_id=user_id,
        back_target=back_target,
        source_restore_action=source_restore_action,
        source_restore_payload=source_restore_payload,
        source_back_label=source_back_label,
    )

    return "\n".join(lines), InlineKeyboardMarkup(buttons)


def _build_workspace_file_preview_view(
    *,
    preview,
    provider: str,
    workspace_label: str,
    user_id: int,
    ui_state: TelegramUiState,
    last_request_text: str | None,
    back_label: str,
    back_action: str,
    back_payload: dict[str, Any],
    ask_payload: dict[str, Any],
    quick_ask_payload: dict[str, Any],
    secondary_button_label: str,
    secondary_button_action: str,
    secondary_button_payload: dict[str, Any],
    supplemental_buttons: tuple[tuple[str, str, dict[str, Any]], ...] = (),
):
    lines = [
        f"Workspace file for {resolve_provider_profile(provider).display_name} in {workspace_label}",
        f"Path: {preview.relative_path}",
    ]
    if preview.is_binary:
        lines.append(preview.text)
    else:
        lines.append(preview.text)
        if preview.truncated:
            lines.append("[preview truncated]")

    buttons = [
        [
            _callback_button(
                ui_state,
                user_id,
                "Ask Agent About File",
                "workspace_file_ask_agent",
                **ask_payload,
            ),
            _callback_button(
                ui_state,
                user_id,
                secondary_button_label,
                secondary_button_action,
                **secondary_button_payload,
            ),
        ]
    ]
    if last_request_text is not None:
        buttons.append(
            [
                _callback_button(
                    ui_state,
                    user_id,
                    "Ask With Last Request",
                    "workspace_file_ask_last_request",
                    **quick_ask_payload,
                )
            ]
        )
    if supplemental_buttons:
        buttons.append(
            [
                _callback_button(
                    ui_state,
                    user_id,
                    label,
                    action,
                    **payload,
                )
                for label, action, payload in supplemental_buttons
            ]
        )
    buttons.append(
        [
            _callback_button(
                ui_state,
                user_id,
                back_label,
                back_action,
                **back_payload,
            )
        ]
    )
    markup = InlineKeyboardMarkup(buttons)
    return "\n".join(lines), markup


def _build_workspace_change_preview_view(
    *,
    diff_preview,
    provider: str,
    workspace_label: str,
    user_id: int,
    ui_state: TelegramUiState,
    last_request_text: str | None,
    back_label: str,
    back_action: str,
    back_payload: dict[str, Any],
    ask_payload: dict[str, Any],
    quick_ask_payload: dict[str, Any],
    secondary_button_label: str,
    secondary_button_action: str,
    secondary_button_payload: dict[str, Any],
    supplemental_buttons: tuple[tuple[str, str, dict[str, Any]], ...] = (),
):
    lines = [
        f"Workspace change for {resolve_provider_profile(provider).display_name} in {workspace_label}",
        f"Path: {diff_preview.relative_path}",
        f"Status: {diff_preview.status_code}",
        diff_preview.text,
    ]
    if diff_preview.truncated:
        lines.append("[diff preview truncated]")

    buttons = [
        [
            _callback_button(
                ui_state,
                user_id,
                "Ask Agent About Change",
                "workspace_change_ask_agent",
                **ask_payload,
            ),
            _callback_button(
                ui_state,
                user_id,
                secondary_button_label,
                secondary_button_action,
                **secondary_button_payload,
            ),
        ]
    ]
    if last_request_text is not None:
        buttons.append(
            [
                _callback_button(
                    ui_state,
                    user_id,
                    "Ask With Last Request",
                    "workspace_change_ask_last_request",
                    **quick_ask_payload,
                )
            ]
        )
    if supplemental_buttons:
        buttons.append(
            [
                _callback_button(
                    ui_state,
                    user_id,
                    label,
                    action,
                    **payload,
                )
                for label, action, payload in supplemental_buttons
            ]
        )
    buttons.append(
        [
            _callback_button(
                ui_state,
                user_id,
                back_label,
                back_action,
                **back_payload,
            )
        ]
    )
    markup = InlineKeyboardMarkup(buttons)
    return "\n".join(lines), markup


def _parent_relative_path(relative_path: str) -> str:
    if not relative_path:
        return ""
    parts = [part for part in relative_path.split("/") if part]
    if len(parts) <= 1:
        return ""
    return "/".join(parts[:-1])


def _build_model_mode_view(
    *,
    user_id: int,
    session_id: str | None,
    model_selection,
    mode_selection,
    ui_state: TelegramUiState,
    can_retry_last_turn: bool,
    back_target: str = "none",
    notice: str | None = None,
):
    lines = []
    if notice:
        lines.append(notice)
    lines.append(f"Session: {session_id or 'pending'}")
    buttons = []

    if model_selection is not None:
        lines.append(f"Model: {_current_choice_label(model_selection)}")
        buttons.extend(
            _selection_buttons(
                user_id=user_id,
                selection=model_selection,
                prefix="Model",
                ui_state=ui_state,
                can_retry_last_turn=can_retry_last_turn,
                back_target=back_target,
            )
        )

    if mode_selection is not None:
        lines.append(f"Mode: {_current_choice_label(mode_selection)}")
        buttons.extend(
            _selection_buttons(
                user_id=user_id,
                selection=mode_selection,
                prefix="Mode",
                ui_state=ui_state,
                can_retry_last_turn=can_retry_last_turn,
                back_target=back_target,
            )
        )

    _append_back_to_status_button(
        buttons,
        ui_state=ui_state,
        user_id=user_id,
        back_target=back_target,
    )
    return "\n".join(lines), InlineKeyboardMarkup(buttons)


def _selection_buttons(
    *,
    user_id: int,
    selection,
    prefix: str,
    ui_state: TelegramUiState,
    can_retry_last_turn: bool,
    back_target: str = "none",
):
    buttons = []
    for choice in selection.choices:
        if choice.value == selection.current_value:
            buttons.append(
                [
                    _callback_button(
                        ui_state,
                        user_id,
                        f"Current {prefix}: {choice.label}",
                        "noop",
                        notice=f"Already using {choice.label}.",
                    )
                ]
            )
            continue
        row = [
            _callback_button(
                ui_state,
                user_id,
                f"{prefix}: {choice.label}",
                "set_selection",
                kind=selection.kind,
                value=choice.value,
                back_target=back_target,
            )
        ]
        if can_retry_last_turn:
            row.append(
                _callback_button(
                    ui_state,
                    user_id,
                    f"{prefix}+Retry: {choice.label}",
                    "set_selection_retry_last_turn",
                    kind=selection.kind,
                    value=choice.value,
                    back_target=back_target,
                )
            )
        buttons.append(row)
    return buttons


def _current_choice_label(selection) -> str:
    for choice in selection.choices:
        if choice.value == selection.current_value:
            return choice.label
    return selection.current_value


async def _edit_query_message(query, text: str, *, reply_markup=None) -> None:
    if query.message is not None:
        await query.message.edit_text(text, reply_markup=reply_markup)
