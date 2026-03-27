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
    resolve_workspace_path,
    search_workspace_text,
)


BUTTON_NEW_SESSION = "New Session"
BUTTON_BOT_STATUS = "Bot Status"
BUTTON_HELP = "Help"
BUTTON_CANCEL_OR_STOP = "Cancel / Stop"
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
START_COMMAND = "start"
STATUS_COMMAND = "status"
HELP_COMMAND = "help"
CANCEL_COMMAND = "cancel"
DEBUG_STATUS_COMMAND = "debug_status"
_RESERVED_COMMAND_ALIASES = {
    START_COMMAND,
    STATUS_COMMAND,
    HELP_COMMAND,
    CANCEL_COMMAND,
    DEBUG_STATUS_COMMAND,
}
_LOCAL_MENU_COMMAND_SPECS = (
    (START_COMMAND, "Open the welcome screen and restore bot controls"),
    (STATUS_COMMAND, "Open Bot Status with runtime state, recovery, and shortcuts"),
    (HELP_COMMAND, "Show a quick guide to commands, recovery, and workspace tools"),
    (CANCEL_COMMAND, "Cancel pending input, stop a turn, or leave bundle chat"),
)
MAX_PUBLIC_COMMANDS = 100
ATTACHMENT_MAX_BYTES = 8 * 1024 * 1024
MEDIA_GROUP_SETTLE_SECONDS = 0.4
INLINE_TEXT_DOCUMENT_CHAR_LIMIT = 12000
_SUPPORTED_ATTACHMENT_FILTER = (
    filters.PHOTO | filters.Document.ALL | filters.VOICE | filters.AUDIO | filters.VIDEO
)
_UNSUPPORTED_RICH_MESSAGE_FILTER = (
    filters.Sticker.ALL
    | filters.CONTACT
    | filters.LOCATION
    | filters.VENUE
    | filters.POLL
    | filters.ANIMATION
    | filters.VIDEO_NOTE
    | filters.Dice.ALL
)
STATUS_TEXT_SNIPPET_LIMIT = 80
STATUS_BUNDLE_PREVIEW_LIMIT = 3
STATUS_COMMAND_PREVIEW_LIMIT = 3
STATUS_WORKSPACE_CHANGE_PREVIEW_LIMIT = 3
STATUS_COMMAND_BUTTONS_PER_ROW = 2
STATUS_SELECTION_QUICK_LIMIT = 2
STATUS_SELECTION_BUTTONS_PER_ROW = 2
STATUS_RECENT_SESSION_PREVIEW_LIMIT = 2
STATUS_PLAN_PREVIEW_LIMIT = 3
STATUS_TOOL_ACTIVITY_PREVIEW_LIMIT = 3
PLAN_PAGE_SIZE = 6
LAST_TURN_PAGE_SIZE = 5
LAST_TURN_TEXT_SNIPPET_LIMIT = 120
LAST_TURN_TEXT_DETAIL_LIMIT = 3000
LAST_TURN_CONTEXT_PREVIEW_LIMIT = 3
TOOL_ACTIVITY_PAGE_SIZE = 5
TOOL_ACTIVITY_PATH_BUTTON_LIMIT = 3
TOOL_ACTIVITY_TERMINAL_PREVIEW_LIMIT = 2
TOOL_ACTIVITY_OUTPUT_PREVIEW_LIMIT = 600
WORKSPACE_RUNTIME_SERVER_PREVIEW_LIMIT = 8
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
    active_session_can_fork: bool


@dataclass(frozen=True, slots=True)
class _ProviderSessionsViewState:
    entries: tuple[Any, ...]
    next_cursor: str | None
    supported: bool
    active_session_id: str | None
    active_session_can_fork: bool


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
    provider: str | None = None
    source_summary: str | None = None


@dataclass(slots=True)
class _MediaGroupBuffer:
    messages: list[Any]
    task: asyncio.Task | None = None


@dataclass(frozen=True, slots=True)
class _PendingMediaGroupStats:
    group_count: int
    item_count: int


@dataclass(slots=True)
class _ActiveTurn:
    provider: str
    workspace_id: str
    title_hint: str
    task: asyncio.Task
    started_at: float
    session: Any | None = None
    stop_requested: bool = False


@dataclass(frozen=True, slots=True)
class _ToolActivityTerminalPreview:
    terminal_id: str
    status_label: str
    output: str | None
    truncated: bool = False


class AttachmentPromptError(ValueError):
    pass


class _ModelModeSessionCreationError(RuntimeError):
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
        self._active_turns: dict[int, _ActiveTurn] = {}

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

    def current_time(self) -> float:
        return self._clock()

    def start_active_turn(
        self,
        user_id: int,
        *,
        provider: str,
        workspace_id: str,
        title_hint: str,
        task: asyncio.Task,
    ) -> _ActiveTurn:
        self._prune()
        active_turn = _ActiveTurn(
            provider=provider,
            workspace_id=workspace_id,
            title_hint=title_hint,
            task=task,
            started_at=self._clock(),
        )
        self._active_turns[user_id] = active_turn
        return active_turn

    def get_active_turn(
        self,
        user_id: int,
        *,
        provider: str | None = None,
        workspace_id: str | None = None,
    ) -> _ActiveTurn | None:
        self._prune()
        active_turn = self._active_turns.get(user_id)
        if active_turn is None:
            return None
        if provider is not None and active_turn.provider != provider:
            return None
        if workspace_id is not None and active_turn.workspace_id != workspace_id:
            return None
        return active_turn

    def bind_active_turn_session(
        self,
        user_id: int,
        *,
        task: asyncio.Task | None,
        session: Any,
    ) -> None:
        self._prune()
        active_turn = self._active_turns.get(user_id)
        if active_turn is None:
            return
        if task is not None and active_turn.task is not task:
            return
        active_turn.session = session

    def mark_active_turn_stop_requested(
        self,
        user_id: int,
        *,
        task: asyncio.Task | None = None,
    ) -> bool:
        self._prune()
        active_turn = self._active_turns.get(user_id)
        if active_turn is None:
            return False
        if task is not None and active_turn.task is not task:
            return False
        active_turn.stop_requested = True
        return True

    def clear_active_turn(
        self,
        user_id: int,
        *,
        task: asyncio.Task | None = None,
    ) -> _ActiveTurn | None:
        self._prune()
        active_turn = self._active_turns.get(user_id)
        if active_turn is None:
            return None
        if task is not None and active_turn.task is not task:
            return None
        return self._active_turns.pop(user_id, None)

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
        *,
        provider: str | None = None,
        source_summary: str | None = None,
    ) -> None:
        normalized_text = text.strip()
        if not normalized_text:
            self._last_request_texts.pop(user_id, None)
            return
        self._last_request_texts[user_id] = _LastRequestText(
            workspace_id=workspace_id,
            text=normalized_text,
            provider=provider,
            source_summary=source_summary,
        )

    def get_last_request(
        self,
        user_id: int,
        workspace_id: str,
    ) -> _LastRequestText | None:
        last_request_text = self._last_request_texts.get(user_id)
        if last_request_text is None:
            return None
        if last_request_text.workspace_id != workspace_id:
            self._last_request_texts.pop(user_id, None)
            return None
        return last_request_text

    def get_last_request_text(
        self,
        user_id: int,
        workspace_id: str,
    ) -> str | None:
        last_request_text = self.get_last_request(user_id, workspace_id)
        if last_request_text is None:
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

    def pending_media_group_stats(self, user_id: int) -> _PendingMediaGroupStats | None:
        group_count = 0
        item_count = 0
        for (buffer_user_id, _media_group_id), buffer in self._media_groups.items():
            if buffer_user_id != user_id:
                continue
            group_count += 1
            item_count += len(buffer.messages)
        if group_count == 0:
            return None
        return _PendingMediaGroupStats(group_count=group_count, item_count=item_count)

    def cancel_pending_media_groups(self, user_id: int) -> _PendingMediaGroupStats | None:
        group_count = 0
        item_count = 0
        keys_to_remove = [key for key in self._media_groups if key[0] == user_id]
        for key in keys_to_remove:
            buffer = self._media_groups.pop(key, None)
            if buffer is None:
                continue
            group_count += 1
            item_count += len(buffer.messages)
            if buffer.task is not None:
                buffer.task.cancel()
        if group_count == 0:
            return None
        return _PendingMediaGroupStats(group_count=group_count, item_count=item_count)

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
        completed_turn_user_ids = [
            user_id
            for user_id, active_turn in self._active_turns.items()
            if active_turn.task.done()
        ]
        for user_id in completed_turn_user_ids:
            self._active_turns.pop(user_id, None)


def _main_menu_markup(user_id: int, services) -> ReplyKeyboardMarkup:
    rows = [
        [BUTTON_NEW_SESSION, BUTTON_BOT_STATUS],
        [BUTTON_RETRY_LAST_TURN, BUTTON_FORK_LAST_TURN],
        [BUTTON_WORKSPACE_SEARCH, BUTTON_CONTEXT_BUNDLE],
        [BUTTON_HELP, BUTTON_CANCEL_OR_STOP],
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


def _clears_pending_text_action_button(text: str) -> bool:
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


def _build_local_menu_commands() -> list[BotCommand]:
    return [
        BotCommand(command, _trim_command_description(description))
        for command, description in _LOCAL_MENU_COMMAND_SPECS
    ]


def _build_public_command_menu(commands) -> tuple[list[BotCommand], dict[str, str]]:
    used_aliases: set[str] = set()
    bot_commands = _build_local_menu_commands()
    aliases: dict[str, str] = {}
    available_agent_slots = max(0, MAX_PUBLIC_COMMANDS - len(bot_commands))
    for command in commands[:available_agent_slots]:
        alias = _allocate_command_alias(command.name, used_aliases)
        aliases[alias] = command.name
        bot_commands.append(BotCommand(alias, _trim_command_description(command.description)))
    return bot_commands, aliases


async def _sync_agent_commands_for_user(application, ui_state: TelegramUiState, user_id: int, commands) -> None:
    menu_commands, aliases = _build_public_command_menu(list(commands))
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
        commands = ()
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
    try:
        commands = await services.discover_agent_commands(
            timeout_seconds=COMMAND_DISCOVERY_TIMEOUT_SECONDS
        )
    except Exception:
        commands = ()
    for user_id in services.allowed_user_ids:
        await _sync_agent_commands_for_user(application, ui_state, user_id, commands)


async def _reply_with_menu(message, services, user_id: int, text: str, *, reply_markup=None):
    markup = _main_menu_markup(user_id, services) if reply_markup is None else reply_markup
    await message.reply_text(text, reply_markup=markup)


def _inline_notice_markup(
    ui_state: TelegramUiState,
    user_id: int,
    *rows: tuple[tuple[str, str, dict[str, Any]], ...],
) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                _callback_button(
                    ui_state,
                    user_id,
                    label,
                    action,
                    **payload,
                )
                for label, action, payload in row
            ]
            for row in rows
            if row
        ]
    )


def _status_only_notice_markup(
    ui_state: TelegramUiState,
    user_id: int,
) -> InlineKeyboardMarkup:
    return _inline_notice_markup(
        ui_state,
        user_id,
        (("Open Bot Status", "runtime_status_page", {}),),
    )


def _pending_input_notice_markup(
    ui_state: TelegramUiState,
    user_id: int,
) -> InlineKeyboardMarkup:
    return _inline_notice_markup(
        ui_state,
        user_id,
        (
            ("Cancel Pending Input", "runtime_status_cancel_pending", {}),
            ("Open Bot Status", "runtime_status_page", {}),
        ),
    )


def _pending_uploads_notice_markup(
    ui_state: TelegramUiState,
    user_id: int,
) -> InlineKeyboardMarkup:
    return _inline_notice_markup(
        ui_state,
        user_id,
        (
            ("Discard Pending Uploads", "runtime_status_discard_pending_uploads", {}),
            ("Open Bot Status", "runtime_status_page", {}),
        ),
    )


def _active_turn_notice_markup(
    ui_state: TelegramUiState,
    user_id: int,
) -> InlineKeyboardMarkup:
    return _inline_notice_markup(
        ui_state,
        user_id,
        (
            ("Stop Turn", "runtime_status_stop_turn", {}),
            ("Open Bot Status", "runtime_status_page", {}),
        ),
    )


def _unauthorized_text() -> str:
    return "Access denied. Ask the operator to allow your Telegram user ID."


def _unknown_action_text() -> str:
    return (
        "This action is no longer available because that menu is out of date. "
        "Reopen the latest menu or use /start."
    )


def _button_not_for_you_text() -> str:
    return "This button belongs to another user. Reopen the menu from your own chat or use /start there."


async def _reply_unauthorized(update: Update) -> None:
    if update.message is not None:
        await update.message.reply_text(_unauthorized_text())


async def _reply_request_failed(update: Update, services) -> None:
    if update.message is not None and update.effective_user is not None:
        await _reply_with_menu(
            update.message,
            services,
            update.effective_user.id,
            _request_failed_text(),
        )


def _request_failed_text() -> str:
    return "Request failed. Try again, use /start, or open Bot Status."


def _expired_button_text() -> str:
    return (
        "This button has expired because that menu is out of date. "
        "Reopen the latest menu or use /start."
    )


def _context_bundle_empty_text() -> str:
    return "Context bundle is empty. Add files or changes first."


def _no_previous_request_text() -> str:
    return "No previous request is available in this workspace yet. Send a new request first."


def _no_previous_turn_text() -> str:
    return "No previous turn is available yet. Send a new request first, then try again."


def _no_active_session_text() -> str:
    return "No active session. Send text or an attachment to start one."


def _switch_session_failed_text() -> str:
    return "Couldn't switch to that session. Try again, reopen Session History, or start a new session."


def _fork_session_failed_text() -> str:
    return "Couldn't fork that session. Try again or start a new session."


def _switch_provider_session_failed_text() -> str:
    return "Couldn't switch to that provider session. Try again or reopen Provider Sessions."


def _fork_provider_session_failed_text() -> str:
    return "Couldn't fork that provider session. Try again or reopen Provider Sessions."


def _selection_update_failed_text() -> str:
    return "Couldn't update model or mode. Try again or reopen Model / Mode."


def _model_mode_load_failed_text() -> str:
    return "Couldn't load Model / Mode. Try again or go back to Bot Status."


def _session_creation_failed_text() -> str:
    return "Couldn't start a session. Try again, use /start, or open Bot Status."


def _switch_agent_failed_text() -> str:
    return "Couldn't switch agent. Try again or choose another agent."


def _switch_workspace_failed_text() -> str:
    return "Couldn't switch workspace. Try again or choose another workspace."


def _runtime_status_refresh_failed_text() -> str:
    return "Couldn't refresh Bot Status. Reopen Bot Status to confirm the latest state."


def _runtime_status_refresh_degraded_notice(notice: str) -> str:
    return f"{notice} Reopen Bot Status to confirm the latest state."


def _stop_turn_failed_text() -> str:
    return "Couldn't stop the current turn. Try again or reopen Bot Status."


def _bundle_chat_update_failed_text() -> str:
    return "Couldn't update bundle chat. Reopen Bot Status and try again."


def _delete_session_failed_text() -> str:
    return "Couldn't delete that session. Try again or reopen Session History."


def _empty_media_group_text() -> str:
    return (
        "Telegram didn't deliver any usable attachments from that album. "
        "Send the album again. Nothing was sent to the agent."
    )


def _unsupported_attachment_for_turn_text() -> str:
    return (
        "This attachment type can't be sent in this chat flow. Send a photo, document, audio, "
        "voice note, or video instead, use /help for supported flows, or use /start to reopen "
        "the main keyboard. Nothing was sent to the agent."
    )


def _attachment_too_large_text() -> str:
    limit_mib = ATTACHMENT_MAX_BYTES // (1024 * 1024)
    return (
        f"This attachment is larger than the {limit_mib} MiB bot limit. "
        "Send a smaller file or compress it before retrying. Nothing was sent to the agent."
    )


def _workspace_fallback_save_failed_text() -> str:
    return (
        "Couldn't save the attachment into the current workspace for fallback handling. "
        "Try again or send a different file if possible. Nothing was sent to the agent."
    )


def _saved_attachment_notice_text(
    saved_context_items: tuple[_ContextBundleItem, ...],
    *,
    recovery: bool,
) -> str:
    count = len(saved_context_items)
    if count <= 0:
        raise ValueError("saved attachment notice requires at least one context item")

    if count == 1:
        lines = [
            (
                "The request did not finish, but this attachment was saved in the workspace and "
                "added to Context Bundle."
            )
            if recovery
            else (
                "This attachment couldn't be sent directly to the current agent, so it was "
                "saved in the workspace and added to Context Bundle."
            )
        ]
        lines.append(
            "You can retry without uploading it again."
            if recovery
            else "You can reuse it in follow-up turns."
        )
    else:
        lines = [
            (
                f"The request did not finish, but these {count} attachments were saved in the "
                "workspace and added to Context Bundle."
            )
            if recovery
            else (
                f"These {count} attachments couldn't be sent directly to the current agent, so they "
                "were saved in the workspace and added to Context Bundle."
            )
        ]
        lines.append(
            "You can retry without uploading them again."
            if recovery
            else "You can reuse them in follow-up turns."
        )

    preview_items = saved_context_items[:3]
    for index, item in enumerate(preview_items, start=1):
        lines.append(f"{index}. {_context_bundle_item_label(item)}")
    remaining = count - len(preview_items)
    if remaining > 0:
        lines.append(f"... {remaining} more {_count_noun(remaining, 'item', 'items')}")

    lines.append("Open Context Bundle to inspect them, or open Bot Status to keep going.")
    return "\n".join(lines)


def _saved_attachment_notice_markup(
    ui_state: TelegramUiState,
    user_id: int,
) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                _callback_button(
                    ui_state,
                    user_id,
                    "Open Context Bundle",
                    "context_bundle_page",
                    page=0,
                ),
                _callback_button(
                    ui_state,
                    user_id,
                    "Open Bot Status",
                    "runtime_status_page",
                ),
            ]
        ]
    )


def _preserve_saved_attachment_context(
    ui_state: TelegramUiState,
    *,
    user_id: int,
    state,
    saved_context_items: tuple[_ContextBundleItem, ...],
) -> None:
    for item in saved_context_items:
        ui_state.add_context_item(
            user_id,
            state.provider,
            state.workspace_id,
            item,
        )


async def _reply_saved_attachment_notice(
    message,
    *,
    ui_state: TelegramUiState,
    user_id: int,
    saved_context_items: tuple[_ContextBundleItem, ...],
    recovery: bool,
) -> None:
    if not saved_context_items:
        return
    try:
        await message.reply_text(
            _saved_attachment_notice_text(
                saved_context_items,
                recovery=recovery,
            ),
            reply_markup=_saved_attachment_notice_markup(ui_state, user_id),
        )
    except Exception:
        pass


def _workspace_search_cancelled_text() -> str:
    return "Search cancelled. Use Workspace Search to search again or open Bot Status when ready."


def _unsupported_message_subject(message) -> tuple[str, str]:
    if getattr(message, "sticker", None) is not None:
        return "Stickers", "aren't"
    if getattr(message, "location", None) is not None:
        return "Locations", "aren't"
    if getattr(message, "contact", None) is not None:
        return "Contacts", "aren't"
    if getattr(message, "venue", None) is not None:
        return "Venues", "aren't"
    if getattr(message, "poll", None) is not None:
        return "Polls", "aren't"
    if getattr(message, "animation", None) is not None:
        return "GIFs and animations", "aren't"
    if getattr(message, "video_note", None) is not None:
        return "Video notes", "aren't"
    if getattr(message, "dice", None) is not None:
        return "Dice messages", "aren't"
    return "This Telegram message type", "isn't"


def _unsupported_message_text(message, *, bundle_chat_active: bool) -> str:
    subject, verb = _unsupported_message_subject(message)
    if bundle_chat_active:
        return (
            f"{subject} {verb} supported in this chat yet. Send plain text next to keep using "
            "the current context bundle, or send a photo, document, audio, or video instead. "
            "Use /help for supported flows, or use /start to reopen the main keyboard."
        )
    return (
        f"{subject} {verb} supported in this chat yet. Send plain text, photo, document, "
        "audio, or video instead, use /help for supported flows, or use /start to reopen the "
        "main keyboard."
    )


async def _reply_session_creation_failed(
    update: Update,
    services,
    *,
    notice: str | None = None,
) -> None:
    if update.message is not None and update.effective_user is not None:
        await _reply_with_menu(
            update.message,
            services,
            update.effective_user.id,
            _prefixed_notice_text(notice, _session_creation_failed_text()),
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


def _pending_input_cancel_notice(text: str) -> str:
    separator = "\n" if "\n" in text else " "
    return f"{text}{separator}Send /cancel to back out."


def _pending_text_action_waiting_hint(pending_text_action: _PendingTextAction | None) -> str:
    if pending_text_action is None:
        return "Send text next"

    action = pending_text_action.action
    if action == "rename_history":
        return "Send the new session title next"
    if action == "run_agent_command":
        return "Send the command arguments next"
    if action == "workspace_search":
        return "Send the search text next"
    if action == "workspace_file_agent_prompt":
        return "Send the request for this file next"
    if action == "workspace_change_agent_prompt":
        return "Send the request for this change next"
    if action == "context_bundle_agent_prompt":
        return "Send the request for this bundle next"
    if action == "context_items_agent_prompt":
        return "Send the request for this context next"
    return "Send the text next"


def _waiting_for_plain_text_notice(
    pending_text_action: _PendingTextAction | None = None,
) -> str:
    if pending_text_action is None:
        return (
            "The current action is waiting for plain text. Send text or send /cancel to back "
            "out. Nothing was sent to the agent."
        )
    return (
        f"{_pending_text_action_label(pending_text_action)} is waiting for plain text. "
        f"{_pending_text_action_waiting_hint(pending_text_action)}, or send /cancel to back out. "
        "Nothing was sent to the agent."
    )


def _pending_media_group_summary(stats: _PendingMediaGroupStats) -> str:
    group_label = "attachment group" if stats.group_count == 1 else "attachment groups"
    item_label = "item" if stats.item_count == 1 else "items"
    return f"{stats.group_count} {group_label} ({stats.item_count} {item_label})"


def _pending_media_group_status_line(stats: _PendingMediaGroupStats) -> str:
    item_label = "attachment" if stats.item_count == 1 else "attachments"
    if stats.group_count == 1:
        return f"Status: collecting {stats.item_count} {item_label} from a pending Telegram album."
    return (
        "Status: collecting "
        f"{stats.item_count} {item_label} across {_pending_media_group_summary(stats)}."
    )


def _pending_media_group_next_step_line(stats: _PendingMediaGroupStats) -> str:
    item_label = "it" if stats.item_count == 1 else "them"
    return (
        "Recommended next step: wait for the attachments to finish collecting, or use /cancel "
        f"or Cancel / Stop to discard {item_label} before anything reaches the agent."
    )


def _pending_media_group_blocked_input_text(stats: _PendingMediaGroupStats) -> str:
    item_label = "it" if stats.item_count == 1 else "them"
    album_label = (
        "a pending Telegram album"
        if stats.group_count == 1
        else "pending Telegram albums"
    )
    return (
        f"Still collecting {_pending_media_group_summary(stats)} from {album_label}. "
        f"Wait for {item_label} to finish, or use /cancel or Cancel / Stop to discard the "
        "pending uploads first. This new message was not sent to the agent."
    )


def _pending_media_group_cancelled_text(stats: _PendingMediaGroupStats) -> str:
    if stats.group_count == 1:
        return (
            "Discarded pending attachment group "
            f"({stats.item_count} {'item' if stats.item_count == 1 else 'items'}). "
            "Nothing was sent to the agent."
        )
    return f"Discarded pending {_pending_media_group_summary(stats)}. Nothing was sent to the agent."


def _format_elapsed_duration(seconds: float) -> str:
    total_seconds = max(0, int(seconds))
    if total_seconds < 60:
        return f"{total_seconds}s"
    minutes, remaining_seconds = divmod(total_seconds, 60)
    if minutes < 60:
        return f"{minutes}m {remaining_seconds}s"
    hours, remaining_minutes = divmod(minutes, 60)
    return f"{hours}h {remaining_minutes}m"


def _discard_pending_uploads_for_transition(
    ui_state: TelegramUiState,
    user_id: int,
) -> str | None:
    cleared = ui_state.cancel_pending_media_groups(user_id)
    if cleared is None:
        return None
    return _pending_media_group_cancelled_text(cleared)


def _prefixed_notice_text(notice: str | None, text: str) -> str:
    if not notice:
        return text
    return f"{notice}\n{text}"


def _interaction_status_line(
    *,
    session,
    active_turn: _ActiveTurn | None,
    pending_text_action: _PendingTextAction | None,
    pending_media_group_stats: _PendingMediaGroupStats | None,
    bundle_count: int,
    bundle_chat_active: bool,
) -> str:
    if active_turn is not None:
        title = _status_text_snippet(active_turn.title_hint, limit=120) or "current request"
        if active_turn.stop_requested:
            return f"Status: stopping {title}."
        return f"Status: running {title}."
    if pending_text_action is not None:
        return (
            "Status: waiting for plain text for "
            f"{_pending_text_action_label(pending_text_action)}."
        )
    if pending_media_group_stats is not None:
        return _pending_media_group_status_line(pending_media_group_stats)
    if bundle_chat_active and bundle_count > 0:
        item_summary = _status_item_count_summary(bundle_count) or "current bundle"
        return (
            "Status: bundle chat is on. "
            f"Your next plain text message will use the current context bundle ({item_summary})."
        )
    if session is None:
        return "Status: ready. Your first text or attachment will start a session."
    return "Status: ready. The current live session is idle."


def _recommended_next_step_line(
    *,
    session,
    active_turn: _ActiveTurn | None,
    pending_text_action: _PendingTextAction | None,
    pending_media_group_stats: _PendingMediaGroupStats | None,
    bundle_count: int,
    bundle_chat_active: bool,
    last_request_available: bool,
    last_turn_available: bool,
) -> str:
    if active_turn is not None:
        return (
            "Recommended next step: wait for the reply, or use /cancel or Cancel / Stop to "
            "interrupt."
        )
    if pending_text_action is not None:
        return (
            "Recommended next step: send the plain text for "
            f"{_pending_text_action_label(pending_text_action)}, or use /cancel to back out."
        )
    if pending_media_group_stats is not None:
        return _pending_media_group_next_step_line(pending_media_group_stats)
    if bundle_chat_active and bundle_count > 0:
        if last_request_available:
            return (
                "Recommended next step: send plain text to continue with this bundle, or tap "
                "Bundle + Last Request to replay the previous request with the same context."
            )
        return (
            "Recommended next step: send plain text to continue with this bundle, or stop "
            "bundle chat if you want a normal turn."
        )
    if bundle_count > 0:
        if last_request_available:
            return (
                "Recommended next step: tap Ask Agent With Context or Bundle + Last Request, "
                "or send a fresh request."
            )
        return "Recommended next step: tap Ask Agent With Context, or send a fresh request."
    if last_request_available and last_turn_available:
        return (
            "Recommended next step: run the last request again from Bot Status, reuse the "
            "previous turn with Retry Last Turn / Fork Last Turn, or send a fresh request."
        )
    if last_request_available:
        if session is None:
            return (
                "Recommended next step: run the last request again from Bot Status, send text "
                "or an attachment, or use Workspace Search / Context Bundle before you ask."
            )
        return (
            "Recommended next step: run the last request again from Bot Status, send text or "
            "an attachment, or open Bot Status if you want files, changes, or history first."
        )
    if last_turn_available:
        return (
            "Recommended next step: send a fresh request, or reuse the previous turn with "
            "Retry Last Turn / Fork Last Turn."
        )
    if session is None:
        return (
            "Recommended next step: send text or an attachment, or use Workspace Search / "
            "Context Bundle before you ask."
        )
    return (
        "Recommended next step: send text or an attachment, or open Bot Status if you want "
        "files, changes, or history first."
    )


def _primary_controls_line(
    *,
    session,
    active_turn: _ActiveTurn | None,
    pending_text_action: _PendingTextAction | None,
    pending_media_group_stats: _PendingMediaGroupStats | None,
    bundle_count: int,
    bundle_chat_active: bool,
    last_request_available: bool,
    last_turn_available: bool,
) -> str:
    if active_turn is not None:
        return "Primary controls right now: Stop Turn in Bot Status, or use /cancel from chat."
    if pending_text_action is not None:
        return (
            "Primary controls right now: send the expected text next, or use Cancel Pending Input "
            "in Bot Status."
        )
    if pending_media_group_stats is not None:
        return (
            "Primary controls right now: wait for the album to finish, or use Discard Pending "
            "Uploads in Bot Status."
        )
    if bundle_chat_active and bundle_count > 0:
        if last_request_available:
            return (
                "Primary controls right now: send plain text, use Bundle + Last Request, or stop "
                "bundle chat from Bot Status."
            )
        return (
            "Primary controls right now: send plain text, Ask Agent With Context, or stop bundle "
            "chat from Bot Status."
        )
    if bundle_count > 0:
        if last_request_available:
            return (
                "Primary controls right now: Ask Agent With Context, Bundle + Last Request, or "
                "Context Bundle."
            )
        return "Primary controls right now: Ask Agent With Context or Context Bundle."
    if last_request_available and last_turn_available:
        return (
            "Primary controls right now: Run Last Request, Retry Last Turn, Fork Last Turn, "
            "or send a fresh request."
        )
    if last_request_available:
        if session is None:
            return (
                "Primary controls right now: Run Last Request, send text or an attachment, or "
                "use Workspace Search / Context Bundle first."
            )
        return (
            "Primary controls right now: Run Last Request, send text or an attachment, or "
            "open Bot Status for files, changes, and context prep."
        )
    if last_turn_available:
        return "Primary controls right now: Retry Last Turn, Fork Last Turn, or send a fresh request."
    if session is None:
        return (
            "Primary controls right now: send text or an attachment, or use Workspace Search "
            "/ Context Bundle first."
        )
    return (
        "Primary controls right now: send text or an attachment, or open Bot Status for files, "
        "changes, and context prep."
    )


def _resume_snapshot_lines(
    *,
    provider: str,
    last_request: _LastRequestText | None,
    last_turn: _ReplayTurn | None,
    bundle_count: int,
    bundle_chat_active: bool,
) -> list[str]:
    if last_request is None and last_turn is None and bundle_count <= 0:
        return []

    lines = ["Resume snapshot:"]
    if last_request is not None:
        lines.append(
            f"Last request: {_status_text_snippet(last_request.text, limit=120) or '[empty]'}"
        )
        lines.append(f"Last request source: {_last_request_source_summary(last_request)}")
        lines.append(
            "Replay text only: "
            + _last_request_replay_note(
                last_request=last_request,
                current_provider=provider,
            )
        )
    if last_turn is not None:
        replay_snippet = _status_text_snippet(last_turn.title_hint) or "untitled turn"
        lines.append(f"Last turn replay: available ({replay_snippet})")
        lines.append(
            "Replay full payload: "
            + _last_turn_replay_note(
                replay_turn=last_turn,
                current_provider=provider,
            )
        )
    if bundle_count > 0:
        bundle_summary = _status_item_count_summary(bundle_count) or "current bundle"
        if bundle_chat_active:
            lines.append(
                "Context bundle ready: "
                f"{bundle_summary}; bundle chat is on, so your next plain text message will include it."
            )
        else:
            lines.append(
                "Context bundle ready: "
                f"{bundle_summary}; use Context Bundle or Bot Status to send it with your next request."
            )
    return lines


def _join_label_series(labels: list[str]) -> str:
    if not labels:
        return ""
    if len(labels) == 1:
        return labels[0]
    if len(labels) == 2:
        return f"{labels[0]} and {labels[1]}"
    return f"{', '.join(labels[:-1])}, and {labels[-1]}"


def _workspace_reuse_labels(
    *,
    ui_state: TelegramUiState,
    user_id: int,
    provider: str,
    workspace_id: str,
) -> list[str]:
    labels: list[str] = []
    if ui_state.get_last_request(user_id, workspace_id) is not None:
        labels.append("Last Request")
    if ui_state.get_last_turn(user_id, provider, workspace_id) is not None:
        labels.append("Last Turn")
    bundle = ui_state.get_context_bundle(user_id, provider, workspace_id)
    if bundle is not None and bundle.items:
        labels.append("Context Bundle")
    return labels


def _workspace_reuse_summary_line(
    *,
    ui_state: TelegramUiState,
    user_id: int,
    provider: str,
    workspace_id: str,
) -> str | None:
    labels = _workspace_reuse_labels(
        ui_state=ui_state,
        user_id=user_id,
        provider=provider,
        workspace_id=workspace_id,
    )
    if not labels:
        return None
    return f"Reusable in this workspace: {_join_label_series(labels)}."


def _session_ready_extra_lines(
    *,
    ui_state: TelegramUiState,
    user_id: int,
    provider: str,
    workspace_id: str,
) -> tuple[str, ...]:
    lines = []
    reuse_summary = _workspace_reuse_summary_line(
        ui_state=ui_state,
        user_id=user_id,
        provider=provider,
        workspace_id=workspace_id,
    )
    if reuse_summary is not None:
        lines.append(reuse_summary)
    bundle = ui_state.get_context_bundle(user_id, provider, workspace_id)
    bundle_count = 0 if bundle is None else len(bundle.items)
    if (
        bundle_count > 0
        and ui_state.context_bundle_chat_active(user_id, provider, workspace_id)
    ):
        lines.append(
            "Bundle chat is still on, so your next plain text message will include the current context bundle."
        )
    return tuple(lines)


def _session_ready_notice_for_runtime(
    *,
    ui_state: TelegramUiState,
    user_id: int,
    state,
) -> str:
    return _session_ready_notice_text(
        extra_lines=_session_ready_extra_lines(
            ui_state=ui_state,
            user_id=user_id,
            provider=state.provider,
            workspace_id=state.workspace_id,
        )
    )


def _main_keyboard_priority_lines(*, is_admin: bool) -> list[str]:
    lines = [
        "Main keyboard focus: New Session and Bot Status first, then Retry / Fork Last Turn.",
        "Context prep row: Workspace Search and Context Bundle stay one tap away before you ask.",
        (
            "Advanced actions live in Bot Status: Session History, Model / Mode, Agent "
            "Commands, Workspace Files/Changes, and Restart Agent."
        ),
        (
            "Recovery row: Help and Cancel / Stop stay on the keyboard, and /start, /status, "
            "/help, and /cancel still work if Telegram hides it."
        ),
    ]
    if is_admin:
        lines.append(
            "Admin row: Switch Agent and Switch Workspace stay available and change the shared runtime."
        )
    return lines


def _start_quick_path_lines() -> list[str]:
    return [
        "1. Ask right now: send plain text or an attachment.",
        "2. Prepare context first: use Workspace Search or Context Bundle.",
        (
            "3. Recover or branch work: open Bot Status for Last Request, Last Turn, "
            "history, model / mode, and session actions."
        ),
    ]


def _help_common_task_lines() -> list[str]:
    return [
        "1. Ask a fresh question: send text or an attachment.",
        (
            "2. Prepare reusable local context: use Workspace Search or Workspace Files / "
            "Changes, then keep it in Context Bundle if you want to reuse it."
        ),
        "3. Replay only the saved request text: Run Last Request.",
        (
            "4. Replay the full saved turn payload: Retry Last Turn. Use Fork Last Turn to do "
            "that in a new session."
        ),
        (
            "5. Recover, inspect, or switch setup: Bot Status for history, model / mode, "
            "agent commands, new session, and restart."
        ),
    ]


def _help_core_concept_lines() -> list[str]:
    return [
        "Context Bundle keeps selected files, changes, and fallback attachments ready across turns.",
        (
            "Bundle chat means your next plain text message will automatically include the "
            "current context bundle until you stop it."
        ),
    ]


def _session_ready_notice_text(*, extra_lines: tuple[str, ...] = ()) -> str:
    lines = [
        (
            "You're ready for the next request. Old bot buttons and pending inputs tied to the "
            "previous session were cleared."
        )
    ]
    lines.extend(line for line in extra_lines if line)
    return "\n".join(lines)


def _new_session_success_text(
    session_id: str,
    *,
    extra_lines: tuple[str, ...] = (),
) -> str:
    return f"Started new session: {session_id}\n{_session_ready_notice_text(extra_lines=extra_lines)}"


def _restart_agent_success_text(
    session_id: str,
    *,
    extra_lines: tuple[str, ...] = (),
) -> str:
    return f"Restarted agent: {session_id}\n{_session_ready_notice_text(extra_lines=extra_lines)}"


def _build_start_text(
    *,
    provider: str,
    workspace_id: str,
    workspace_label: str,
    session,
    user_id: int,
    ui_state: TelegramUiState,
    is_admin: bool,
) -> str:
    active_turn = ui_state.get_active_turn(
        user_id,
        provider=provider,
        workspace_id=workspace_id,
    )
    pending_text_action = ui_state.get_pending_text_action(user_id)
    pending_media_group_stats = ui_state.pending_media_group_stats(user_id)
    bundle = ui_state.get_context_bundle(user_id, provider, workspace_id)
    bundle_count = 0 if bundle is None else len(bundle.items)
    bundle_chat_active = ui_state.context_bundle_chat_active(user_id, provider, workspace_id)
    last_request = ui_state.get_last_request(user_id, workspace_id)
    last_request_available = last_request is not None
    last_turn = ui_state.get_last_turn(user_id, provider, workspace_id)
    last_turn_available = last_turn is not None

    lines = [
        f"Welcome to Talk2Agent for {resolve_provider_profile(provider).display_name} in {workspace_label}.",
        f"Workspace ID: {workspace_id}",
        _interaction_status_line(
            session=session,
            active_turn=active_turn,
            pending_text_action=pending_text_action,
            pending_media_group_stats=pending_media_group_stats,
            bundle_count=bundle_count,
            bundle_chat_active=bundle_chat_active,
        ),
        _recommended_next_step_line(
            session=session,
            active_turn=active_turn,
            pending_text_action=pending_text_action,
            pending_media_group_stats=pending_media_group_stats,
            bundle_count=bundle_count,
            bundle_chat_active=bundle_chat_active,
            last_request_available=last_request_available,
            last_turn_available=last_turn_available,
        ),
        _primary_controls_line(
            session=session,
            active_turn=active_turn,
            pending_text_action=pending_text_action,
            pending_media_group_stats=pending_media_group_stats,
            bundle_count=bundle_count,
            bundle_chat_active=bundle_chat_active,
            last_request_available=last_request_available,
            last_turn_available=last_turn_available,
        ),
        "",
    ]

    if session is None:
        lines.append("Session: none yet. Your first text or attachment will start one.")
    else:
        lines.append(f"Session: {session.session_id or 'pending'}")
        session_title = _status_text_snippet(getattr(session, "session_title", None), limit=120)
        if session_title is not None:
            lines.append(f"Session title: {session_title}")

    lines.extend(_status_active_turn_lines(active_turn))

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
        model_summary = _selection_summary_line("Model", model_selection)
        mode_summary = _selection_summary_line("Mode", mode_selection)
        if model_summary is not None:
            lines.append(model_summary)
        if mode_summary is not None:
            lines.append(mode_summary)

    lines.append(f"Pending input: {_pending_text_action_label(pending_text_action)}")
    if pending_media_group_stats is not None:
        lines.append(f"Pending uploads: {_pending_media_group_summary(pending_media_group_stats)}")

    if bundle_count == 0:
        lines.append("Context bundle: empty")
    else:
        bundle_chat_state = "bundle chat on" if bundle_chat_active else "bundle chat off"
        lines.append(
            f"Context bundle: {bundle_count} item{'s' if bundle_count != 1 else ''} ({bundle_chat_state})"
        )

    resume_lines = _resume_snapshot_lines(
        provider=provider,
        last_request=last_request,
        last_turn=last_turn,
        bundle_count=bundle_count,
        bundle_chat_active=bundle_chat_active,
    )
    if resume_lines:
        lines.append("")
        lines.extend(resume_lines)

    lines.append("")
    lines.append("Quick paths:")
    lines.extend(_start_quick_path_lines())
    lines.append("")
    lines.append("Keyboard layout:")
    lines.extend(_main_keyboard_priority_lines(is_admin=is_admin))

    return "\n".join(lines)


def _build_help_text(
    *,
    provider: str,
    workspace_id: str,
    workspace_label: str,
    session,
    user_id: int,
    ui_state: TelegramUiState,
    is_admin: bool,
) -> str:
    active_turn = ui_state.get_active_turn(
        user_id,
        provider=provider,
        workspace_id=workspace_id,
    )
    pending_text_action = ui_state.get_pending_text_action(user_id)
    pending_media_group_stats = ui_state.pending_media_group_stats(user_id)
    bundle = ui_state.get_context_bundle(user_id, provider, workspace_id)
    bundle_count = 0 if bundle is None else len(bundle.items)
    bundle_chat_active = ui_state.context_bundle_chat_active(user_id, provider, workspace_id)
    last_request = ui_state.get_last_request(user_id, workspace_id)
    last_request_available = last_request is not None
    last_turn = ui_state.get_last_turn(user_id, provider, workspace_id)
    last_turn_available = last_turn is not None

    lines = [
        f"Talk2Agent help for {resolve_provider_profile(provider).display_name} in {workspace_label}.",
        f"Workspace ID: {workspace_id}",
        _interaction_status_line(
            session=session,
            active_turn=active_turn,
            pending_text_action=pending_text_action,
            pending_media_group_stats=pending_media_group_stats,
            bundle_count=bundle_count,
            bundle_chat_active=bundle_chat_active,
        ),
        _recommended_next_step_line(
            session=session,
            active_turn=active_turn,
            pending_text_action=pending_text_action,
            pending_media_group_stats=pending_media_group_stats,
            bundle_count=bundle_count,
            bundle_chat_active=bundle_chat_active,
            last_request_available=last_request_available,
            last_turn_available=last_turn_available,
        ),
        _primary_controls_line(
            session=session,
            active_turn=active_turn,
            pending_text_action=pending_text_action,
            pending_media_group_stats=pending_media_group_stats,
            bundle_count=bundle_count,
            bundle_chat_active=bundle_chat_active,
            last_request_available=last_request_available,
            last_turn_available=last_turn_available,
        ),
        "",
    ]

    if session is None:
        lines.append("Session: none yet. Send text or an attachment to start one.")
    else:
        lines.append(f"Session: {session.session_id or 'pending'}")

    lines.extend(_status_active_turn_lines(active_turn))
    lines.append(f"Pending input: {_pending_text_action_label(pending_text_action)}")
    if pending_media_group_stats is not None:
        lines.append(f"Pending uploads: {_pending_media_group_summary(pending_media_group_stats)}")
    lines.append(f"Context bundle: {bundle_count} item{'s' if bundle_count != 1 else ''}")

    resume_lines = _resume_snapshot_lines(
        provider=provider,
        last_request=last_request,
        last_turn=last_turn,
        bundle_count=bundle_count,
        bundle_chat_active=bundle_chat_active,
    )
    if resume_lines:
        lines.append("")
        lines.extend(resume_lines)

    lines.append("")
    lines.append("Common tasks:")
    lines.extend(_help_common_task_lines())
    lines.append("")
    lines.append("Core concepts:")
    lines.extend(_help_core_concept_lines())
    lines.append("")
    lines.append("Keyboard:")
    lines.extend(_main_keyboard_priority_lines(is_admin=is_admin))
    lines.append("")
    lines.append("Recovery:")
    lines.append("/start restores the welcome screen and the full keyboard.")
    lines.append("/status opens Bot Status even when the keyboard is hidden.")
    lines.append("Help or /help reopens this guide without changing the current session.")
    lines.append(
        "Cancel / Stop or /cancel backs out of pending input, stops a running turn, or leaves "
        "bundle chat."
    )

    return "\n".join(lines)


async def handle_start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    services,
    ui_state: TelegramUiState,
) -> None:
    del context

    if update.message is None:
        return
    if not _is_authorized(update, services):
        await _reply_unauthorized(update)
        return

    user_id = update.effective_user.id
    try:
        state, session = await _with_active_store(
            services,
            lambda store: store.peek(user_id),
        )
    except Exception:
        await _reply_request_failed(update, services)
        return

    await _reply_with_menu(
        update.message,
        services,
        user_id,
        _build_start_text(
            provider=state.provider,
            workspace_id=state.workspace_id,
            workspace_label=_workspace_label(services, state.workspace_id),
            session=session,
            user_id=user_id,
            ui_state=ui_state,
            is_admin=user_id == services.admin_user_id,
        ),
    )


async def handle_help(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    services,
    ui_state: TelegramUiState,
) -> None:
    del context

    if update.message is None:
        return
    if not _is_authorized(update, services):
        await _reply_unauthorized(update)
        return

    user_id = update.effective_user.id
    try:
        state, session = await _with_active_store(
            services,
            lambda store: store.peek(user_id),
        )
    except Exception:
        await _reply_request_failed(update, services)
        return

    await _reply_with_menu(
        update.message,
        services,
        user_id,
        _build_help_text(
            provider=state.provider,
            workspace_id=state.workspace_id,
            workspace_label=_workspace_label(services, state.workspace_id),
            session=session,
            user_id=user_id,
            ui_state=ui_state,
            is_admin=user_id == services.admin_user_id,
        ),
    )


async def handle_status(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    services,
    ui_state: TelegramUiState,
) -> None:
    del context
    await _show_runtime_status(update, services, ui_state)


async def _request_stop_active_turn(
    ui_state: TelegramUiState,
    *,
    user_id: int,
    active_turn: _ActiveTurn,
) -> None:
    ui_state.mark_active_turn_stop_requested(
        user_id,
        task=active_turn.task,
    )
    cancel_turn = None if active_turn.session is None else getattr(active_turn.session, "cancel_turn", None)
    if callable(cancel_turn):
        cancelled = await cancel_turn()
        if not cancelled:
            active_turn.task.cancel()
        return
    active_turn.task.cancel()


async def handle_cancel(
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
    pending_text_action = ui_state.clear_pending_text_action(user_id)
    pending_media_group_stats = ui_state.cancel_pending_media_groups(user_id)
    if pending_text_action is not None or pending_media_group_stats is not None:
        notice_parts = []
        if pending_text_action is not None:
            pending_input_notice = (
                "Cancelled pending input: "
                f"{_pending_text_action_label(pending_text_action)}."
            )
            if pending_media_group_stats is None:
                pending_input_notice = f"{pending_input_notice} Nothing was sent to the agent."
            notice_parts.append(pending_input_notice)
        if pending_media_group_stats is not None:
            notice_parts.append(_pending_media_group_cancelled_text(pending_media_group_stats))
        await _reply_with_menu(
            update.message,
            services,
            user_id,
            " ".join(notice_parts),
        )
        return

    active_turn = ui_state.get_active_turn(user_id)
    if active_turn is not None:
        try:
            await _request_stop_active_turn(
                ui_state,
                user_id=user_id,
                active_turn=active_turn,
            )
        except Exception:
            await _reply_request_failed(update, services)
            return
        await _reply_with_menu(
            update.message,
            services,
            user_id,
            "Stop requested for the current turn. Open Bot Status to track progress.",
        )
        return

    try:
        state = await services.snapshot_runtime_state()
    except Exception:
        if ui_state.resolve_agent_command(user_id, CANCEL_COMMAND) is not None:
            await handle_agent_command(update, context, services, ui_state)
            return
        await _reply_request_failed(update, services)
        return

    if ui_state.context_bundle_chat_active(user_id, state.provider, state.workspace_id):
        ui_state.disable_context_bundle_chat(user_id)
        await _reply_with_menu(
            update.message,
            services,
            user_id,
            "Bundle chat disabled. New plain text messages will use the normal session again.",
        )
        return

    if ui_state.resolve_agent_command(user_id, CANCEL_COMMAND) is not None:
        await handle_agent_command(update, context, services, ui_state)
        return

    await _reply_with_menu(
        update.message,
        services,
        user_id,
        "Nothing to cancel. Send text, use /start to restore the main keyboard, or open Bot Status.",
    )


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
    if _clears_pending_text_action_button(text):
        ui_state.clear_pending_text_action(user_id)

    if text == BUTTON_HELP:
        await handle_help(update, context, services, ui_state)
        return
    if text == BUTTON_CANCEL_OR_STOP:
        await handle_cancel(update, context, services, ui_state)
        return
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
        try:
            handled_pending_text = await _handle_pending_text_action(
                update,
                services,
                ui_state,
                pending_text_action,
                text,
            )
        except Exception:
            await _reply_request_failed(update, services)
            return
        if handled_pending_text:
            return

    pending_media_group_stats = ui_state.pending_media_group_stats(user_id)
    if pending_media_group_stats is not None:
        await _reply_with_menu(
            update.message,
            services,
            user_id,
            _pending_media_group_blocked_input_text(pending_media_group_stats),
            reply_markup=_pending_uploads_notice_markup(ui_state, user_id),
        )
        return

    try:
        state = await services.snapshot_runtime_state()
    except Exception:
        await _reply_request_failed(update, services)
        return
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

        ui_state.set_last_request_text(
            user_id,
            state.workspace_id,
            text,
            provider=state.provider,
            source_summary=_last_request_bundle_chat_source_summary(len(bundle.items)),
        )
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

    ui_state.set_last_request_text(
        user_id,
        state.workspace_id,
        text,
        provider=state.provider,
        source_summary=_last_request_plain_text_source_summary(),
    )
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

    pending_text_action = ui_state.get_pending_text_action(user_id)
    if pending_text_action is not None:
        await _reply_with_menu(
            update.message,
            services,
            user_id,
            _waiting_for_plain_text_notice(pending_text_action),
            reply_markup=_pending_input_notice_markup(ui_state, user_id),
        )
        return

    pending_media_group_stats = ui_state.pending_media_group_stats(user_id)
    if pending_media_group_stats is not None:
        await _reply_with_menu(
            update.message,
            services,
            user_id,
            _pending_media_group_blocked_input_text(pending_media_group_stats),
            reply_markup=_pending_uploads_notice_markup(ui_state, user_id),
        )
        return

    try:
        prompt = await _build_attachment_prompt(update.message)
    except AttachmentPromptError as exc:
        await _reply_with_menu(
            update.message,
            services,
            user_id,
            str(exc),
            reply_markup=_status_only_notice_markup(ui_state, user_id),
        )
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


async def handle_unsupported_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    services,
    ui_state: TelegramUiState,
) -> None:
    del context

    if update.message is None:
        return
    if not _is_authorized(update, services):
        await _reply_unauthorized(update)
        return

    user_id = update.effective_user.id
    pending_text_action = ui_state.get_pending_text_action(user_id)
    if pending_text_action is not None:
        await _reply_with_menu(
            update.message,
            services,
            user_id,
            _waiting_for_plain_text_notice(pending_text_action),
            reply_markup=_pending_input_notice_markup(ui_state, user_id),
        )
        return

    pending_media_group_stats = ui_state.pending_media_group_stats(user_id)
    if pending_media_group_stats is not None:
        await _reply_with_menu(
            update.message,
            services,
            user_id,
            _pending_media_group_blocked_input_text(pending_media_group_stats),
            reply_markup=_pending_uploads_notice_markup(ui_state, user_id),
        )
        return

    active_turn = ui_state.get_active_turn(user_id)
    if active_turn is not None:
        await _reply_with_menu(
            update.message,
            services,
            user_id,
            _turn_busy_notice(active_turn),
            reply_markup=_active_turn_notice_markup(ui_state, user_id),
        )
        return

    bundle_chat_active = False
    try:
        state = await services.snapshot_runtime_state()
    except Exception:
        state = None
    if state is not None:
        bundle_chat_active = ui_state.context_bundle_chat_active(
            user_id,
            state.provider,
            state.workspace_id,
        )

    await _reply_with_menu(
        update.message,
        services,
        user_id,
        _unsupported_message_text(update.message, bundle_chat_active=bundle_chat_active),
        reply_markup=_status_only_notice_markup(ui_state, user_id),
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
                f"fork={'yes' if getattr(capabilities, 'can_fork', False) else 'no'}"
                f",list={'yes' if getattr(capabilities, 'can_list', False) else 'no'}"
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


async def _show_runtime_status(
    update: Update,
    services,
    ui_state: TelegramUiState,
    *,
    notice: str | None = None,
) -> None:
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
        notice=notice,
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


async def _show_session_info_from_callback(
    query,
    services,
    ui_state: TelegramUiState,
    *,
    user_id: int,
    back_target: str = "none",
    notice: str | None = None,
) -> None:
    state, session, history_entries, _ = await _load_runtime_status_view_state(
        services,
        user_id,
    )
    text, markup = _build_session_info_view(
        provider=state.provider,
        workspace_id=state.workspace_id,
        workspace_label=_workspace_label(services, state.workspace_id),
        user_id=user_id,
        ui_state=ui_state,
        session=session,
        session_title=_current_session_history_title(session, history_entries),
        back_target=back_target,
        notice=notice,
    )
    await _edit_query_message(query, text, reply_markup=markup)


async def _show_usage_from_callback(
    query,
    services,
    ui_state: TelegramUiState,
    *,
    user_id: int,
    back_target: str = "none",
    notice: str | None = None,
) -> None:
    state, session, history_entries, _ = await _load_runtime_status_view_state(
        services,
        user_id,
    )
    text, markup = _build_usage_view(
        provider=state.provider,
        workspace_label=_workspace_label(services, state.workspace_id),
        user_id=user_id,
        ui_state=ui_state,
        session=session,
        session_title=_current_session_history_title(session, history_entries),
        back_target=back_target,
        notice=notice,
    )
    await _edit_query_message(query, text, reply_markup=markup)


async def _show_last_request_from_callback(
    query,
    services,
    ui_state: TelegramUiState,
    *,
    user_id: int,
    back_target: str = "none",
    notice: str | None = None,
) -> None:
    state = await services.snapshot_runtime_state()
    text, markup = _build_last_request_view(
        last_request=ui_state.get_last_request(user_id, state.workspace_id),
        last_turn_available=ui_state.get_last_turn(user_id, state.provider, state.workspace_id)
        is not None,
        current_provider=state.provider,
        workspace_label=_workspace_label(services, state.workspace_id),
        user_id=user_id,
        ui_state=ui_state,
        back_target=back_target,
        notice=notice,
    )
    await _edit_query_message(query, text, reply_markup=markup)


async def _show_workspace_runtime_from_callback(
    query,
    services,
    ui_state: TelegramUiState,
    *,
    user_id: int,
    back_target: str = "none",
    notice: str | None = None,
) -> None:
    state = await services.snapshot_runtime_state()
    workspace = services.config.agent.resolve_workspace(state.workspace_id)
    text, markup = _build_workspace_runtime_view(
        provider=state.provider,
        workspace=workspace,
        workspace_path=state.workspace_path,
        user_id=user_id,
        ui_state=ui_state,
        back_target=back_target,
        notice=notice,
    )
    await _edit_query_message(query, text, reply_markup=markup)


async def _show_workspace_runtime_server_from_callback(
    query,
    services,
    ui_state: TelegramUiState,
    *,
    user_id: int,
    server_index: int,
    back_target: str = "none",
    notice: str | None = None,
) -> None:
    state = await services.snapshot_runtime_state()
    workspace = services.config.agent.resolve_workspace(state.workspace_id)
    mcp_servers = tuple(getattr(workspace, "mcp_servers", ()) or ())
    if server_index < 0 or server_index >= len(mcp_servers):
        await _show_workspace_runtime_from_callback(
            query,
            services,
            ui_state,
            user_id=user_id,
            back_target=back_target,
            notice="MCP server is no longer available in this workspace runtime.",
        )
        return
    text, markup = _build_workspace_runtime_server_view(
        provider=state.provider,
        workspace=workspace,
        workspace_path=state.workspace_path,
        user_id=user_id,
        ui_state=ui_state,
        server=mcp_servers[server_index],
        server_index=server_index,
        server_count=len(mcp_servers),
        back_target=back_target,
        notice=notice,
    )
    await _edit_query_message(query, text, reply_markup=markup)


async def _show_last_turn_from_callback(
    query,
    services,
    ui_state: TelegramUiState,
    *,
    user_id: int,
    page: int,
    back_target: str = "none",
    notice: str | None = None,
) -> None:
    state = await services.snapshot_runtime_state()
    replay_turn = ui_state.get_last_turn(user_id, state.provider, state.workspace_id)
    text, markup = _build_last_turn_view(
        replay_turn=replay_turn,
        current_provider=state.provider,
        workspace_label=_workspace_label(services, state.workspace_id),
        user_id=user_id,
        page=page,
        ui_state=ui_state,
        back_target=back_target,
        notice=notice,
    )
    await _edit_query_message(query, text, reply_markup=markup)


async def _show_last_turn_item_from_callback(
    query,
    services,
    ui_state: TelegramUiState,
    *,
    user_id: int,
    page: int,
    item_index: int,
    back_target: str = "none",
    notice: str | None = None,
) -> None:
    state = await services.snapshot_runtime_state()
    replay_turn = ui_state.get_last_turn(user_id, state.provider, state.workspace_id)
    prompt_items = _replay_prompt_items(replay_turn)
    if replay_turn is None or item_index < 0 or item_index >= len(prompt_items):
        await _show_last_turn_from_callback(
            query,
            services,
            ui_state,
            user_id=user_id,
            page=page,
            back_target=back_target,
            notice="Selected replay item is no longer available.",
        )
        return

    text, markup = _build_last_turn_item_view(
        replay_turn=replay_turn,
        current_provider=state.provider,
        workspace_label=_workspace_label(services, state.workspace_id),
        item=prompt_items[item_index],
        item_index=item_index,
        total_count=len(prompt_items),
        user_id=user_id,
        page=page,
        ui_state=ui_state,
        back_target=back_target,
        notice=notice,
    )
    await _edit_query_message(query, text, reply_markup=markup)


async def _show_plan_from_callback(
    query,
    services,
    ui_state: TelegramUiState,
    *,
    user_id: int,
    page: int,
    back_target: str = "none",
    notice: str | None = None,
) -> None:
    state, session, _, _ = await _load_runtime_status_view_state(
        services,
        user_id,
    )
    text, markup = _build_plan_view(
        entries=_plan_items(session),
        provider=state.provider,
        workspace_label=_workspace_label(services, state.workspace_id),
        user_id=user_id,
        page=page,
        ui_state=ui_state,
        session_id=None if session is None else getattr(session, "session_id", None),
        back_target=back_target,
        notice=notice,
    )
    await _edit_query_message(query, text, reply_markup=markup)


async def _show_plan_detail_from_callback(
    query,
    services,
    ui_state: TelegramUiState,
    *,
    user_id: int,
    page: int,
    plan_index: int,
    back_target: str = "none",
    notice: str | None = None,
) -> None:
    state, session, _, _ = await _load_runtime_status_view_state(
        services,
        user_id,
    )
    entries = _plan_items(session)
    if plan_index < 0 or plan_index >= len(entries):
        await _show_plan_from_callback(
            query,
            services,
            ui_state,
            user_id=user_id,
            page=page,
            back_target=back_target,
            notice="Selected plan entry is no longer available.",
        )
        return

    text, markup = _build_plan_detail_view(
        entry=entries[plan_index],
        plan_index=plan_index,
        total_count=len(entries),
        provider=state.provider,
        workspace_label=_workspace_label(services, state.workspace_id),
        user_id=user_id,
        page=page,
        ui_state=ui_state,
        back_target=back_target,
        notice=notice,
    )
    await _edit_query_message(query, text, reply_markup=markup)


async def _show_tool_activity_from_callback(
    query,
    services,
    ui_state: TelegramUiState,
    *,
    user_id: int,
    page: int,
    back_target: str = "none",
    notice: str | None = None,
) -> None:
    state, session, _, _ = await _load_runtime_status_view_state(
        services,
        user_id,
    )
    text, markup = _build_tool_activity_view(
        activities=_tool_activity_items(session),
        provider=state.provider,
        workspace_label=_workspace_label(services, state.workspace_id),
        user_id=user_id,
        page=page,
        ui_state=ui_state,
        session_id=None if session is None else getattr(session, "session_id", None),
        back_target=back_target,
        notice=notice,
    )
    await _edit_query_message(query, text, reply_markup=markup)


async def _show_tool_activity_detail_from_callback(
    query,
    services,
    ui_state: TelegramUiState,
    *,
    user_id: int,
    page: int,
    activity_index: int,
    back_target: str = "none",
    notice: str | None = None,
) -> None:
    state, session, _, git_status = await _load_runtime_status_view_state(
        services,
        user_id,
    )
    activities = _tool_activity_items(session)
    if activity_index < 0 or activity_index >= len(activities):
        await _show_tool_activity_from_callback(
            query,
            services,
            ui_state,
            user_id=user_id,
            page=page,
            back_target=back_target,
            notice="Selected tool activity is no longer available.",
        )
        return

    activity = activities[activity_index]
    openable_paths = _tool_activity_openable_paths(state.workspace_path, activity)
    change_targets = _tool_activity_change_targets(git_status, openable_paths)
    terminal_previews = await _load_tool_activity_terminal_previews(session, activity)
    text, markup = _build_tool_activity_detail_view(
        activity=activity,
        activity_index=activity_index,
        total_count=len(activities),
        provider=state.provider,
        workspace_label=_workspace_label(services, state.workspace_id),
        user_id=user_id,
        page=page,
        ui_state=ui_state,
        openable_paths=openable_paths,
        change_targets=change_targets,
        terminal_previews=terminal_previews,
        back_target=back_target,
        notice=notice,
    )
    await _edit_query_message(query, text, reply_markup=markup)


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


def _turn_busy_notice(active_turn: _ActiveTurn | None) -> str:
    if active_turn is None:
        return (
            "Another request is already running. "
            "Send /cancel to stop it, open Bot Status to inspect progress, or wait for it to "
            "finish. This new message was not sent to the agent."
        )
    title = _status_text_snippet(active_turn.title_hint) or "current request"
    return (
        f"Another request is already running ({title}). "
        "Send /cancel to stop it, open Bot Status to inspect progress, or wait for it to finish. "
        "This new message was not sent to the agent."
    )


def _status_active_turn_lines(
    active_turn: _ActiveTurn | None,
    *,
    now: float | None = None,
) -> list[str]:
    if active_turn is None:
        return ["Turn: idle"]

    details = [_status_text_snippet(active_turn.title_hint) or "current request"]
    session_id = None if active_turn.session is None else getattr(active_turn.session, "session_id", None)
    if session_id:
        details.append(session_id)
    status = "stop requested" if active_turn.stop_requested else "running"
    lines = [f"Turn: {status} ({', '.join(details)})"]
    if now is not None:
        lines.append(f"Turn elapsed: {_format_elapsed_duration(now - active_turn.started_at)}")
    return lines


def _pending_text_action_hint_line(
    pending_text_action: _PendingTextAction | None,
) -> str | None:
    if pending_text_action is None:
        return None
    return f"Next plain text: {_pending_text_action_waiting_hint(pending_text_action)}."


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
    native_title = getattr(session, "session_title", None)
    if native_title:
        return _status_text_snippet(native_title)
    return None


def _status_usage_summary(session) -> str | None:
    if session is None:
        return None
    usage = getattr(session, "usage", None)
    if usage is None:
        return None

    parts = [f"used={usage.used}", f"size={usage.size}"]
    amount = getattr(usage, "cost_amount", None)
    currency = getattr(usage, "cost_currency", None)
    if amount is not None and currency:
        parts.append(f"cost={amount:.2f} {currency}")
    elif amount is not None:
        parts.append(f"cost={amount:.2f}")
    return " ".join(parts)


def _usage_cost_label(usage) -> str:
    amount = getattr(usage, "cost_amount", None)
    currency = getattr(usage, "cost_currency", None)
    if amount is None:
        return "unavailable"
    if currency:
        return f"{amount:.2f} {currency}"
    return f"{amount:.2f}"


def _usage_remaining(usage) -> int | None:
    used = getattr(usage, "used", None)
    size = getattr(usage, "size", None)
    if used is None or size is None:
        return None
    return int(size) - int(used)


def _usage_utilization_percent(usage) -> float | None:
    used = getattr(usage, "used", None)
    size = getattr(usage, "size", None)
    if used is None or size is None:
        return None
    size_value = int(size)
    if size_value <= 0:
        return None
    return (int(used) / size_value) * 100


def _last_request_plain_text_source_summary() -> str:
    return "plain text"


def _last_request_replay_source_summary() -> str:
    return "last request replay"


def _last_request_bundle_chat_source_summary(item_count: int) -> str:
    return _status_summary_with_details(
        "bundle chat",
        _status_item_count_summary(item_count),
    )


def _last_request_workspace_file_source_summary(relative_path: str) -> str:
    return _status_summary_with_details(
        "workspace file request",
        _status_text_snippet(relative_path, limit=120) or relative_path,
    )


def _last_request_workspace_change_source_summary(relative_path: str) -> str:
    return _status_summary_with_details(
        "workspace change request",
        _status_text_snippet(relative_path, limit=120) or relative_path,
    )


def _last_request_context_items_source_summary(context_label: str, item_count: int) -> str:
    return _status_summary_with_details(
        "selected context request",
        _status_text_snippet(context_label, limit=120) or context_label,
        _status_item_count_summary(item_count),
    )


def _last_request_context_bundle_source_summary(item_count: int) -> str:
    return _status_summary_with_details(
        "context bundle request",
        _status_item_count_summary(item_count),
    )


def _last_request_source_summary(last_request: _LastRequestText | None) -> str:
    if last_request is None:
        return _last_request_plain_text_source_summary()
    source_summary = getattr(last_request, "source_summary", None)
    if source_summary:
        return source_summary
    return _last_request_plain_text_source_summary()


def _workspace_runtime_server_target(server) -> str | None:
    if getattr(server, "transport", None) == "stdio":
        command = _status_text_snippet(getattr(server, "command", None), limit=80)
        args = tuple(getattr(server, "args", ()) or ())
        if command is None:
            return None
        if not args:
            return command
        joined_args = " ".join(str(arg) for arg in args[:3])
        if len(args) > 3:
            joined_args += " ..."
        return _status_text_snippet(f"{command} {joined_args}", limit=120)
    url = getattr(server, "url", None)
    return _status_text_snippet(None if url is None else str(url), limit=120)


def _workspace_runtime_server_summary(server) -> str:
    transport = _status_text_snippet(getattr(server, "transport", None), limit=24) or "unknown"
    name = _status_text_snippet(getattr(server, "name", None), limit=60) or "server"
    detail_parts: list[str] = []
    target = _workspace_runtime_server_target(server)
    if target is not None:
        detail_parts.append(target)
    env_count = len(tuple(getattr(server, "env", ()) or ()))
    header_count = len(tuple(getattr(server, "headers", ()) or ()))
    if env_count > 0:
        detail_parts.append(f"env: {env_count}")
    if header_count > 0:
        detail_parts.append(f"headers: {header_count}")
    return _status_summary_with_details(f"[{transport}] {name}", *detail_parts)


def _status_plan_preview_lines(session, *, limit: int = STATUS_PLAN_PREVIEW_LIMIT) -> list[str]:
    if session is None:
        return []
    entries = tuple(getattr(session, "plan_entries", ()) or ())
    if not entries:
        return []

    lines = [f"Agent plan: {len(entries)} item{'s' if len(entries) != 1 else ''}", "Plan preview:"]
    visible_entries = entries[:limit]
    for index, entry in enumerate(visible_entries, start=1):
        content = _status_text_snippet(getattr(entry, "content", "")) or "[empty]"
        status = getattr(entry, "status", "pending")
        if status == "completed":
            prefix = "[x]"
        elif status == "in_progress":
            prefix = "[>]"
        else:
            prefix = "[ ]"
        lines.append(f"{index}. {prefix} {content}")
    remaining = len(entries) - len(visible_entries)
    if remaining > 0:
        lines.append(f"... {remaining} more item{'s' if remaining != 1 else ''}")
    return lines


def _plan_items(session) -> tuple[Any, ...]:
    if session is None:
        return ()
    return tuple(getattr(session, "plan_entries", ()) or ())


def _plan_status_prefix(status: str) -> str:
    normalized = status.strip().lower()
    if normalized == "completed":
        return "[x]"
    if normalized == "in_progress":
        return "[>]"
    if normalized == "pending":
        return "[ ]"
    if normalized == "failed":
        return "[!]"
    return "[?]"


def _selection_summary_line(label: str, selection) -> str | None:
    if selection is None:
        return None
    current_label = _current_choice_label(selection)
    choice_count = len(tuple(getattr(selection, "choices", ()) or ()))
    return f"{label}: {current_label} ({choice_count} choice{'s' if choice_count != 1 else ''})"


def _replay_prompt_items(replay_turn: _ReplayTurn | None) -> tuple[Any, ...]:
    if replay_turn is None:
        return ()
    return tuple(getattr(replay_turn, "prompt_items", ()) or ())


def _replay_provider_display_name(provider: str) -> str:
    try:
        return resolve_provider_profile(provider).display_name
    except Exception:
        return provider


def _last_request_recorded_provider(
    last_request: _LastRequestText,
    *,
    current_provider: str,
) -> str:
    return last_request.provider or current_provider


def _last_request_replay_note(
    *,
    last_request: _LastRequestText,
    current_provider: str,
) -> str:
    recorded_provider = _last_request_recorded_provider(
        last_request,
        current_provider=current_provider,
    )
    current_display = _replay_provider_display_name(current_provider)
    recorded_display = _replay_provider_display_name(recorded_provider)
    if recorded_provider == current_provider:
        return (
            f"Run Last Request will send this text again to {current_display} in the current "
            "workspace."
        )
    return (
        f"This request was recorded on {recorded_display}, but Run Last Request will send it to "
        f"{current_display} in the current workspace now."
    )


def _last_turn_replay_note(
    *,
    replay_turn: _ReplayTurn,
    current_provider: str,
) -> str:
    current_display = _replay_provider_display_name(current_provider)
    recorded_display = _replay_provider_display_name(replay_turn.provider)
    if replay_turn.provider == current_provider:
        return (
            f"Retry Last Turn / Fork Last Turn will replay this saved payload on "
            f"{current_display} in the current workspace."
        )
    return (
        f"This payload was recorded on {recorded_display}, but Retry Last Turn / Fork Last Turn "
        f"will replay it on {current_display} in the current workspace now. If attachment "
        "support differs, the bot adapts the saved payload first."
    )


def _last_turn_item_kind_label(item: Any) -> str:
    if isinstance(item, PromptText):
        return "text"
    if isinstance(item, PromptImage):
        return "image"
    if isinstance(item, PromptAudio):
        return "audio"
    if isinstance(item, PromptTextResource):
        return "text resource"
    if isinstance(item, PromptBlobResource):
        return "blob resource"
    return type(item).__name__


def _last_turn_item_primary_label(item: Any) -> str:
    if isinstance(item, PromptText):
        return _status_text_snippet(item.text, limit=LAST_TURN_TEXT_SNIPPET_LIMIT) or "[empty]"
    if isinstance(item, PromptTextResource):
        return _status_text_snippet(item.uri, limit=LAST_TURN_TEXT_SNIPPET_LIMIT) or "resource"
    uri = _status_text_snippet(getattr(item, "uri", None), limit=LAST_TURN_TEXT_SNIPPET_LIMIT)
    if uri is not None:
        return uri
    mime_type = _status_text_snippet(getattr(item, "mime_type", None), limit=LAST_TURN_TEXT_SNIPPET_LIMIT)
    if mime_type is not None:
        return mime_type
    return _last_turn_item_kind_label(item)


def _last_turn_item_summary(item: Any) -> str:
    kind = _last_turn_item_kind_label(item)
    details: list[str | None] = []
    if not isinstance(item, PromptText):
        details.append(_status_text_snippet(getattr(item, "mime_type", None)))
    if isinstance(item, PromptTextResource):
        details.append(_status_text_snippet(item.text, limit=LAST_TURN_TEXT_SNIPPET_LIMIT))
    return _status_summary_with_details(
        f"[{kind}] {_last_turn_item_primary_label(item)}",
        *details,
    )


def _last_turn_payload_size_bytes(item: Any) -> int | None:
    try:
        if isinstance(item, PromptText):
            return len(item.text.encode("utf-8"))
        if isinstance(item, PromptTextResource):
            return len(item.text.encode("utf-8"))
        if isinstance(item, PromptImage):
            return len(base64.b64decode(item.data))
        if isinstance(item, PromptAudio):
            return len(base64.b64decode(item.data))
        if isinstance(item, PromptBlobResource):
            return len(base64.b64decode(item.blob))
    except Exception:
        return None
    return None


def _last_turn_render_text_detail(
    text: str,
    *,
    limit: int = LAST_TURN_TEXT_DETAIL_LIMIT,
) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    if limit <= 3:
        return text[:limit], True
    return text[: limit - 3] + "...", True


def _last_turn_context_preview_lines(
    items: tuple[_ContextBundleItem, ...],
    *,
    limit: int = LAST_TURN_CONTEXT_PREVIEW_LIMIT,
) -> list[str]:
    if not items:
        return []
    lines = ["Saved context preview:"]
    visible_items = items[:limit]
    for index, item in enumerate(visible_items, start=1):
        lines.append(f"{index}. {_context_bundle_item_label(item)}")
    remaining = len(items) - len(visible_items)
    if remaining > 0:
        lines.append(f"... {remaining} more item{'s' if remaining != 1 else ''}")
    return lines


def _status_tool_activity_preview_lines(
    session,
    *,
    limit: int = STATUS_TOOL_ACTIVITY_PREVIEW_LIMIT,
) -> list[str]:
    if session is None:
        return []
    activities = tuple(getattr(session, "recent_tool_activities", ()) or ())
    if not activities:
        return []

    lines = [f"Recent tools: {len(activities)}", "Tool preview:"]
    visible_activities = activities[:limit]
    for index, activity in enumerate(visible_activities, start=1):
        title = _status_text_snippet(getattr(activity, "title", None)) or getattr(
            activity, "tool_call_id", "tool"
        )
        status = str(getattr(activity, "status", "pending"))
        summary = f"[{status}] {title}"
        detail_parts = []
        kind = _status_text_snippet(getattr(activity, "kind", None))
        if kind is not None:
            detail_parts.append(kind)
        for detail in tuple(getattr(activity, "details", ()) or ())[:2]:
            detail_snippet = _status_text_snippet(detail)
            if detail_snippet is not None:
                detail_parts.append(detail_snippet)
        lines.append(f"{index}. {_status_summary_with_details(summary, *detail_parts)}")

    remaining = len(activities) - len(visible_activities)
    if remaining > 0:
        lines.append(f"... {remaining} more item{'s' if remaining != 1 else ''}")
    return lines


def _tool_activity_items(session) -> tuple[Any, ...]:
    if session is None:
        return ()
    return tuple(getattr(session, "recent_tool_activities", ()) or ())


def _tool_activity_path_ref_to_path(path_ref: str) -> str:
    match = re.match(r"^(.*?):(\d+)(?::(\d+))?$", path_ref)
    if match is None:
        return path_ref
    return match.group(1)


def _tool_activity_openable_paths(workspace_path: str, activity) -> tuple[str, ...]:
    seen: set[str] = set()
    resolved_paths: list[str] = []
    raw_paths = tuple(getattr(activity, "paths", ()) or ())
    raw_path_refs = tuple(getattr(activity, "path_refs", ()) or ())
    for candidate in (*raw_paths, *(_tool_activity_path_ref_to_path(item) for item in raw_path_refs)):
        if not candidate:
            continue
        try:
            resolved = resolve_workspace_path(workspace_path, candidate)
            relative_path = resolved.relative_to(resolve_workspace_path(workspace_path)).as_posix()
        except Exception:
            continue
        if relative_path in seen:
            continue
        seen.add(relative_path)
        resolved_paths.append(relative_path)
    return tuple(resolved_paths)


def _tool_activity_change_targets(git_status, relative_paths: tuple[str, ...]) -> tuple[tuple[str, str], ...]:
    if git_status is None or not getattr(git_status, "is_git_repo", False):
        return ()
    status_by_path = {
        entry.relative_path: entry.status_code
        for entry in getattr(git_status, "entries", ())
    }
    targets: list[tuple[str, str]] = []
    for relative_path in relative_paths:
        status_code = status_by_path.get(relative_path)
        if status_code is None:
            continue
        targets.append((relative_path, status_code))
    return tuple(targets)


def _tool_activity_output_snippet(
    text: str | None,
    *,
    limit: int = TOOL_ACTIVITY_OUTPUT_PREVIEW_LIMIT,
) -> str | None:
    if text is None:
        return None
    stripped = text.strip()
    if not stripped:
        return None
    if len(stripped) <= limit:
        return stripped
    if limit <= 4:
        return stripped[-limit:]
    return "...\n" + stripped[-(limit - 4) :]


def _tool_activity_exit_status_label(exit_status) -> str:
    if exit_status is None:
        return "running"
    signal_name = getattr(exit_status, "signal", None)
    if signal_name:
        return f"signal={signal_name}"
    exit_code = getattr(exit_status, "exit_code", getattr(exit_status, "exitCode", None))
    if exit_code is None:
        return "completed"
    return f"exit={exit_code}"


async def _load_tool_activity_terminal_previews(session, activity) -> tuple[_ToolActivityTerminalPreview, ...]:
    read_terminal_output = getattr(session, "read_terminal_output", None)
    terminal_ids = tuple(getattr(activity, "terminal_ids", ()) or ())
    if not callable(read_terminal_output) or not terminal_ids:
        return ()

    previews: list[_ToolActivityTerminalPreview] = []
    for terminal_id in terminal_ids[:TOOL_ACTIVITY_TERMINAL_PREVIEW_LIMIT]:
        try:
            terminal_output = await read_terminal_output(terminal_id)
        except Exception:
            terminal_output = None
        if terminal_output is None:
            previews.append(
                _ToolActivityTerminalPreview(
                    terminal_id=terminal_id,
                    status_label="unavailable",
                    output=None,
                    truncated=False,
                )
            )
            continue
        previews.append(
            _ToolActivityTerminalPreview(
                terminal_id=terminal_id,
                status_label=_tool_activity_exit_status_label(
                    getattr(terminal_output, "exit_status", getattr(terminal_output, "exitStatus", None))
                ),
                output=_tool_activity_output_snippet(getattr(terminal_output, "output", None)),
                truncated=bool(getattr(terminal_output, "truncated", False)),
            )
        )
    return tuple(previews)


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
        await query.answer(_unauthorized_text(), show_alert=True)
        return

    data = query.data or ""
    if not data.startswith(CALLBACK_PREFIX):
        await query.answer(_unknown_action_text(), show_alert=True)
        return

    token = data[len(CALLBACK_PREFIX) :]
    callback_action = ui_state.get(token)
    if callback_action is None:
        await query.answer(_expired_button_text(), show_alert=True)
        return
    if update.effective_user is None or callback_action.user_id != update.effective_user.id:
        await query.answer(_button_not_for_you_text(), show_alert=True)
        return

    callback_action = ui_state.pop(token)
    if callback_action is None:
        await query.answer(_expired_button_text(), show_alert=True)
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
            await query.answer(_request_failed_text(), show_alert=True)
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
        CommandHandler(START_COMMAND, partial(handle_start, services=services, ui_state=ui_state))
    )
    application.add_handler(
        CommandHandler(STATUS_COMMAND, partial(handle_status, services=services, ui_state=ui_state))
    )
    application.add_handler(
        CommandHandler(HELP_COMMAND, partial(handle_help, services=services, ui_state=ui_state))
    )
    application.add_handler(
        CommandHandler(CANCEL_COMMAND, partial(handle_cancel, services=services, ui_state=ui_state))
    )
    application.add_handler(
        CommandHandler(DEBUG_STATUS_COMMAND, partial(handle_debug_status, services=services))
    )
    application.add_handler(
        CallbackQueryHandler(partial(handle_callback_query, services=services, ui_state=ui_state))
    )
    application.add_handler(
        MessageHandler(
            _SUPPORTED_ATTACHMENT_FILTER,
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
            _UNSUPPORTED_RICH_MESSAGE_FILTER,
            partial(handle_unsupported_message, services=services, ui_state=ui_state),
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
                _pending_input_cancel_notice("Session title cannot be empty. Send another title."),
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

        can_fork = await _resolve_runtime_session_fork_support(
            services,
            state=state,
            active_session_id=history_state.active_session_id,
            active_session_can_fork=history_state.active_session_can_fork,
        )
        history_text, history_markup = _build_history_view(
            entries=history_state.entries,
            provider=state.provider,
            workspace_id=state.workspace_id,
            workspace_label=_workspace_label(services, state.workspace_id),
            user_id=update.effective_user.id,
            page=page,
            ui_state=ui_state,
            active_session_id=history_state.active_session_id,
            can_fork=can_fork,
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
                _pending_input_cancel_notice("Command arguments cannot be empty. Send another value."),
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
                _pending_input_cancel_notice("Search query cannot be empty. Send another query."),
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
                    notice=_request_failed_text(),
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
                _pending_input_cancel_notice("File request cannot be empty. Send another request."),
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
                _pending_input_cancel_notice("Change request cannot be empty. Send another request."),
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
                _pending_input_cancel_notice(
                    "Context bundle request cannot be empty. Send another request."
                ),
            )
            return True

        context_items = tuple(pending_text_action.payload.get("items", ()))
        ui_state.clear_pending_text_action(update.effective_user.id)
        if not context_items:
            await _reply_with_menu(
                update.message,
                services,
                update.effective_user.id,
                _context_bundle_empty_text(),
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
                _pending_input_cancel_notice("Request cannot be empty. Send another request."),
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
    pending_text_action = ui_state.get_pending_text_action(user_id)
    if pending_text_action is not None:
        await _reply_with_menu(
            lead_message,
            services,
            user_id,
            _waiting_for_plain_text_notice(pending_text_action),
            reply_markup=_pending_input_notice_markup(ui_state, user_id),
        )
        return

    try:
        prompt = await _build_media_group_prompt(messages)
    except AttachmentPromptError as exc:
        await _reply_with_menu(
            lead_message,
            services,
            user_id,
            str(exc),
            reply_markup=_status_only_notice_markup(ui_state, user_id),
        )
        return
    except Exception:
        await _reply_with_menu(lead_message, services, user_id, _request_failed_text())
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
        raise AttachmentPromptError(_empty_media_group_text())

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
        raise AttachmentPromptError(_unsupported_attachment_for_turn_text())

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
        raise AttachmentPromptError(_attachment_too_large_text())

    telegram_file = await attachment.get_file()
    payload = await telegram_file.download_as_bytearray()
    data = bytes(payload)
    if len(data) > ATTACHMENT_MAX_BYTES:
        raise AttachmentPromptError(_attachment_too_large_text())
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
        f"The current {provider} session can't accept {unsupported} directly in this chat. "
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
        "Attached Telegram document content was pasted into this turn because the current provider can't read attached documents directly.",
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
        raise AttachmentPromptError(_workspace_fallback_save_failed_text()) from exc

    return (
        f"Telegram attachment was saved to `{inbox_file.relative_path}` in the current workspace "
        f"because the current provider can't read {unsupported_kind} directly in this turn.\n"
        "Open that file from the workspace and continue with the user's request.",
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
    if getattr(summary, "can_fork_sessions", False):
        sessions.append("fork")
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


async def _run_last_request_on_message(
    message,
    user_id: int,
    services,
    ui_state: TelegramUiState,
    *,
    last_request: _LastRequestText,
    provider: str,
    workspace_id: str,
    application,
    after_turn_success=None,
    on_prepare_failure=None,
    on_turn_failure=None,
) -> None:
    ui_state.set_last_request_text(
        user_id,
        workspace_id,
        last_request.text,
        provider=provider,
        source_summary=_last_request_replay_source_summary(),
    )
    await _run_agent_text_turn_on_message(
        message,
        user_id,
        services,
        ui_state,
        last_request.text,
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
    ui_state.set_last_request_text(
        user_id,
        state.workspace_id,
        request_text,
        provider=state.provider,
        source_summary=_last_request_workspace_file_source_summary(relative_path),
    )
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
    ui_state.set_last_request_text(
        user_id,
        state.workspace_id,
        request_text,
        provider=state.provider,
        source_summary=_last_request_workspace_change_source_summary(relative_path),
    )
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
    ui_state.set_last_request_text(
        user_id,
        state.workspace_id,
        request_text,
        provider=state.provider,
        source_summary=_last_request_context_items_source_summary(
            context_label,
            len(items),
        ),
    )
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
    ui_state.set_last_request_text(
        user_id,
        state.workspace_id,
        request_text,
        provider=state.provider,
        source_summary=_last_request_context_bundle_source_summary(len(items)),
    )
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
    turn_state = None

    async def _run(session, stream, state):
        nonlocal saved_context_items, turn_state
        turn_state = state
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
        _preserve_saved_attachment_context(
            ui_state,
            user_id=user_id,
            state=state,
            saved_context_items=saved_context_items,
        )
        await _reply_saved_attachment_notice(
            message,
            ui_state=ui_state,
            user_id=user_id,
            saved_context_items=saved_context_items,
            recovery=False,
        )

    async def _on_turn_failure():
        if turn_state is None or not saved_context_items:
            return
        _preserve_saved_attachment_context(
            ui_state,
            user_id=user_id,
            state=turn_state,
            saved_context_items=saved_context_items,
        )
        await _reply_saved_attachment_notice(
            message,
            ui_state=ui_state,
            user_id=user_id,
            saved_context_items=saved_context_items,
            recovery=True,
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
        on_turn_failure=_on_turn_failure,
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
    active_turn = ui_state.get_active_turn(user_id)
    if active_turn is not None:
        await _reply_with_menu(
            message,
            services,
            user_id,
            _turn_busy_notice(active_turn),
            reply_markup=_active_turn_notice_markup(ui_state, user_id),
        )
        return

    create_task = None if application is None else getattr(application, "create_task", None)
    if callable(create_task):
        try:
            runtime_state = await services.snapshot_runtime_state()
        except Exception:
            if on_prepare_failure is not None:
                try:
                    await on_prepare_failure()
                    return
                except Exception:
                    pass
            await message.reply_text(
                _request_failed_text(),
                reply_markup=_main_menu_markup(user_id, services),
            )
            return
        task: asyncio.Task | None = None

        async def _run_in_background() -> None:
            try:
                await _execute_agent_session_turn_on_message(
                    message,
                    user_id,
                    services,
                    ui_state,
                    title_hint=title_hint,
                    application=application,
                    turn_runner=turn_runner,
                    after_success=after_success,
                    after_turn_success=after_turn_success,
                    on_prepare_failure=on_prepare_failure,
                    on_turn_failure=on_turn_failure,
                )
            finally:
                ui_state.clear_active_turn(user_id, task=task)

        task = create_task(_run_in_background())
        ui_state.start_active_turn(
            user_id,
            provider=runtime_state.provider,
            workspace_id=runtime_state.workspace_id,
            title_hint=title_hint,
            task=task,
        )
        return

    await _execute_agent_session_turn_on_message(
        message,
        user_id,
        services,
        ui_state,
        title_hint=title_hint,
        application=application,
        turn_runner=turn_runner,
        after_success=after_success,
        after_turn_success=after_turn_success,
        on_prepare_failure=on_prepare_failure,
        on_turn_failure=on_turn_failure,
    )


async def _execute_agent_session_turn_on_message(
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
            _request_failed_text(),
            reply_markup=_main_menu_markup(user_id, services),
        )
        return

    ui_state.bind_active_turn_session(
        user_id,
        task=asyncio.current_task(),
        session=session,
    )

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
    except asyncio.CancelledError:
        await stream.finish(stop_reason="cancelled")
        await _invoke_turn_failure_callback(on_turn_failure)
        return
    except UnsupportedPromptContentError as exc:
        await stream.fail(_unsupported_prompt_content_message(state.provider, exc))
        await _invoke_turn_failure_callback(on_turn_failure)
        return
    except AttachmentPromptError as exc:
        await stream.fail(
            str(exc),
            reply_markup=_status_only_notice_markup(ui_state, user_id),
        )
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
                workspace_id=state.workspace_id,
                workspace_label=_workspace_label(services, state.workspace_id),
                user_id=user_id,
                services=services,
                ui_state=ui_state,
            )
            await stream.fail(failure_text, reply_markup=failure_markup)
            await _invoke_turn_failure_callback(on_turn_failure)
            return
        await stream.fail(_request_failed_text())
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

    workspace_changes_follow_up_git_status = _workspace_changes_follow_up_git_status(
        before_workspace_git_status,
        _safe_read_workspace_git_status(state.workspace_path),
    )

    final_reply_markup = None
    if response.stop_reason != "cancelled" and workspace_changes_follow_up_git_status is None:
        final_reply_markup = _completed_turn_reply_markup(
            ui_state,
            user_id=user_id,
        )

    await stream.finish(
        stop_reason=response.stop_reason,
        reply_markup=final_reply_markup,
    )

    if after_success is not None:
        try:
            await after_success(state)
        except Exception:
            pass

    if workspace_changes_follow_up_git_status is not None:
        await _reply_workspace_changes_follow_up(
            message,
            services,
            ui_state,
            user_id=user_id,
            state=state,
            git_status=workspace_changes_follow_up_git_status,
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
            notice=_request_failed_text(),
        )

    async def _on_turn_failure() -> None:
        await _show_runtime_status_from_callback(
            query,
            services,
            ui_state,
            user_id=user_id,
            notice=_request_failed_text(),
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
            notice=_request_failed_text(),
        )

    async def _on_turn_failure() -> None:
        await _restore_pending_source_message(
            pending_text_action,
            services,
            ui_state,
            user_id=user_id,
            notice=_request_failed_text(),
        )

    return _after_turn_success, _on_prepare_failure, _on_turn_failure


async def _prepare_turn_session(store, user_id: int, now: float):
    await store.close_idle_sessions(now)
    return await store.get_or_create(user_id)


def _session_can_fork(session) -> bool:
    if session is None:
        return False
    capabilities = getattr(session, "capabilities", None)
    return bool(getattr(capabilities, "can_fork", False))


async def _resolve_runtime_session_fork_support(
    services,
    *,
    state,
    active_session_id: str | None,
    active_session_can_fork: bool,
) -> bool:
    if active_session_id is not None:
        return active_session_can_fork
    try:
        summary = await services.discover_provider_capabilities(
            state.provider,
            workspace_id=state.workspace_id,
        )
    except Exception:
        return False
    return bool(
        getattr(summary, "available", False)
        and getattr(summary, "can_fork_sessions", False)
    )


async def _load_history_view_state(store, user_id: int) -> _HistoryViewState:
    active_session = await store.peek(user_id)
    entries = await store.list_history(user_id)
    return _HistoryViewState(
        entries=entries,
        active_session_id=None if active_session is None else active_session.session_id,
        active_session_can_fork=_session_can_fork(active_session),
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
        active_session_can_fork=_session_can_fork(active_session),
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


async def _show_agent_command_detail_from_callback(
    query,
    services,
    ui_state: TelegramUiState,
    *,
    user_id: int,
    page: int,
    command_index: int,
    back_target: str = "none",
    notice: str | None = None,
) -> None:
    state, command_state = await _load_command_center_state(services, user_id)
    commands = command_state.commands
    if command_index < 0 or command_index >= len(commands):
        await _show_agent_commands_menu_from_callback(
            query,
            services,
            ui_state,
            user_id=user_id,
            page=page,
            back_target=back_target,
            notice="Agent command is no longer available.",
        )
        return
    text, markup = _build_agent_command_detail_view(
        command=commands[command_index],
        command_index=command_index,
        total_count=len(commands),
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
        _pending_input_cancel_notice("Send your workspace search query as the next plain text message."),
        reply_markup=_workspace_search_prompt_markup(
            ui_state,
            update.effective_user.id,
            cancel_action="workspace_search_cancel",
        ),
    )


def _workspace_search_prompt_markup(
    ui_state: TelegramUiState,
    user_id: int,
    *,
    cancel_action: str,
) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                _callback_button(
                    ui_state,
                    user_id,
                    "Cancel Search",
                    cancel_action,
                )
            ]
        ]
    )


def _workspace_search_cancelled_markup(
    ui_state: TelegramUiState,
    user_id: int,
) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                _callback_button(
                    ui_state,
                    user_id,
                    "Search Again",
                    "recover_workspace_search",
                )
            ],
            [
                _callback_button(
                    ui_state,
                    user_id,
                    "Open Bot Status",
                    "runtime_status_page",
                )
            ],
        ]
    )


async def _show_workspace_search_prompt_from_callback(
    query,
    ui_state: TelegramUiState,
    *,
    user_id: int,
    cancel_action: str,
    pending_payload: dict[str, Any] | None = None,
) -> None:
    ui_state.set_pending_text_action(
        user_id,
        "workspace_search",
        **({} if pending_payload is None else dict(pending_payload)),
    )
    await _edit_query_message(
        query,
        _pending_input_cancel_notice("Send your workspace search query as the next plain text message."),
        reply_markup=_workspace_search_prompt_markup(
            ui_state,
            user_id,
            cancel_action=cancel_action,
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
        action_guide_entries=_workspace_item_action_guide_entries(
            ask_label="Ask Agent About File",
            subject_summary="this file",
            secondary_label="Add File to Context",
            secondary_summary="saves this file to Context Bundle without sending anything yet.",
            has_last_request=last_request_text is not None,
            bundle_chat_label="Start Bundle Chat With File",
            bundle_chat_summary="keeps this file attached to your next plain text messages.",
        ),
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
        action_guide_entries=_workspace_item_action_guide_entries(
            ask_label="Ask Agent About File",
            subject_summary="this file",
            secondary_label="Add File to Context",
            secondary_summary="saves this file to Context Bundle without sending anything yet.",
            has_last_request=last_request_text is not None,
            bundle_chat_label="Start Bundle Chat With File",
            bundle_chat_summary="keeps this file attached to your next plain text messages.",
        ),
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
    workspace_id: str,
    workspace_label: str,
    user_id: int,
    services,
    ui_state: TelegramUiState,
):
    last_request = ui_state.get_last_request(user_id, workspace_id)
    last_turn = ui_state.get_last_turn(user_id, provider, workspace_id)
    lines = [
        "Request failed. "
        f"The current live session for {resolve_provider_profile(provider).display_name} "
        f"in {workspace_label} was closed."
    ]
    if last_turn is not None:
        lines.append(
            "Recommended first step: Retry Last Turn to rerun the previous request, or open Bot "
            "Status if you want to inspect runtime and history first."
        )
    elif last_request is not None:
        lines.append(
            "Recommended first step: Run Last Request to replay the saved request text, or open "
            "Bot Status if you want to inspect runtime and history first."
        )
    else:
        lines.append(
            "Recommended first step: Open Bot Status to inspect runtime and history, or start a "
            "New Session if you want a clean slate."
        )
    if last_request is not None:
        lines.append(
            f"Last request: {_status_text_snippet(last_request.text, limit=120) or '[empty]'}"
        )
        lines.append(f"Last request source: {_last_request_source_summary(last_request)}")
    text = "\n".join(lines)
    buttons: list[list[InlineKeyboardButton]] = []
    primary_buttons = []
    if last_turn is not None:
        primary_buttons.append(
            _callback_button(
                ui_state,
                user_id,
                "Retry Last Turn",
                "recover_retry_last_turn",
            )
        )
        primary_buttons.append(
            _callback_button(
                ui_state,
                user_id,
                "New Session",
                "recover_new_session",
            )
        )
    elif last_request is not None:
        primary_buttons.append(
            _callback_button(
                ui_state,
                user_id,
                "Run Last Request",
                "recover_run_last_request",
            )
        )
        primary_buttons.append(
            _callback_button(
                ui_state,
                user_id,
                "New Session",
                "recover_new_session",
            )
        )
    else:
        primary_buttons.append(
            _callback_button(
                ui_state,
                user_id,
                "New Session",
                "recover_new_session",
            )
        )
    buttons.append(primary_buttons)
    if last_request is not None and last_turn is not None:
        buttons.append(
            [
                _callback_button(
                    ui_state,
                    user_id,
                    "Run Last Request",
                    "recover_run_last_request",
                )
            ]
        )
    buttons.append(
        [
            _callback_button(
                ui_state,
                user_id,
                "Open Bot Status",
                "runtime_status_page",
            ),
            _callback_button(
                ui_state,
                user_id,
                "Session History",
                "recover_session_history",
            ),
        ]
    )
    secondary_buttons = []
    if last_turn is not None:
        secondary_buttons.append(
            _callback_button(
                ui_state,
                user_id,
                "Fork Last Turn",
                "recover_fork_last_turn",
            )
        )
    secondary_buttons.append(
        _callback_button(
            ui_state,
            user_id,
            "Model / Mode",
            "recover_model_mode",
        )
    )
    buttons.append(secondary_buttons)
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


def _session_action_guide_lines(
    *,
    run_summary: str,
    can_fork: bool,
    can_retry_last_turn: bool,
) -> list[str]:
    lines = [f"Actions: Run {run_summary}."]
    if can_fork:
        lines.append("Fork creates a new live session branched from it.")
    if can_retry_last_turn:
        retry_labels = "Run+Retry / Fork+Retry" if can_fork else "Run+Retry"
        lines.append(
            f"{retry_labels} also replay the previous turn immediately after the switch."
        )
    return lines


def _append_paged_list_summary_lines(
    lines: list[str],
    *,
    total_label: str,
    total_count: int,
    start_index: int,
    visible_count: int,
    page: int,
    page_count: int,
) -> None:
    lines.append(f"{total_label}: {total_count}")
    if visible_count > 0 and page_count > 1:
        end_index = start_index + visible_count - 1
        lines.append(f"Showing: {start_index}-{end_index} of {total_count}")
    if page_count > 1:
        lines.append(f"Page: {page + 1}/{page_count}")


def _append_action_guide_lines(
    lines: list[str],
    *,
    entries: tuple[tuple[str, str], ...],
) -> None:
    if not entries:
        return
    lines.append("")
    lines.append("Action guide:")
    for label, summary in entries:
        lines.append(f"- {label} {summary}")


def _workspace_collection_action_guide_entries(
    *,
    ask_label: str,
    subject_summary: str,
    bundle_chat_label: str,
    add_label: str,
    has_last_request: bool,
) -> tuple[tuple[str, str], ...]:
    entries = [
        (ask_label, f"starts a fresh turn using {subject_summary}."),
    ]
    if has_last_request:
        entries.append(
            ("Ask With Last Request", f"reuses the saved request text with {subject_summary}.")
        )
    entries.extend(
        [
            (
                bundle_chat_label,
                f"keeps {subject_summary} attached to your next plain text messages.",
            ),
            (
                add_label,
                f"saves {subject_summary} to Context Bundle without sending anything yet.",
            ),
        ]
    )
    return tuple(entries)


def _workspace_item_action_guide_entries(
    *,
    ask_label: str,
    subject_summary: str,
    secondary_label: str,
    secondary_summary: str,
    has_last_request: bool,
    bundle_chat_label: str | None = None,
    bundle_chat_summary: str | None = None,
) -> tuple[tuple[str, str], ...]:
    entries = [
        (ask_label, f"starts a fresh turn about {subject_summary}."),
    ]
    if has_last_request:
        entries.append(
            ("Ask With Last Request", f"reuses the saved request text with {subject_summary}.")
        )
    if bundle_chat_label is not None and bundle_chat_summary is not None:
        entries.append((bundle_chat_label, bundle_chat_summary))
    entries.append((secondary_label, secondary_summary))
    return tuple(entries)


def _no_model_mode_controls_text() -> str:
    return (
        "This agent does not expose model or mode controls in the current session. "
        "Keep chatting normally, restart the agent if you expected new controls, or open Bot "
        "Status for the rest of the runtime tools."
    )


def _completed_turn_reply_markup(
    ui_state: TelegramUiState,
    *,
    user_id: int,
) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
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
                    "Fork Last Turn",
                    "recover_fork_last_turn",
                ),
            ],
            [
                _callback_button(
                    ui_state,
                    user_id,
                    "Open Bot Status",
                    "recover_runtime_status",
                ),
                _callback_button(
                    ui_state,
                    user_id,
                    "New Session",
                    "recover_new_session",
                ),
            ],
        ]
    )


def _workspace_changes_follow_up_git_status(before_git_status, after_git_status):
    if before_git_status is None or after_git_status is None:
        return None
    if not getattr(after_git_status, "is_git_repo", False):
        return None
    if not getattr(after_git_status, "entries", ()):
        return None
    if _workspace_changes_state_token(before_git_status) == _workspace_changes_state_token(
        after_git_status
    ):
        return None
    return after_git_status


async def _reply_workspace_changes_follow_up(
    message,
    services,
    ui_state: TelegramUiState,
    *,
    user_id: int,
    state,
    git_status,
) -> None:
    text, markup = _build_workspace_changes_follow_up_view(
        git_status=git_status,
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
        action_guide_entries=_workspace_item_action_guide_entries(
            ask_label="Ask Agent About Change",
            subject_summary="this change",
            secondary_label="Add Change to Context",
            secondary_summary="saves this change to Context Bundle without sending anything yet.",
            has_last_request=last_request_text is not None,
            bundle_chat_label="Start Bundle Chat With Change",
            bundle_chat_summary="keeps this change attached to your next plain text messages.",
        ),
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


async def _show_tool_activity_file_preview_from_callback(
    query,
    services,
    ui_state: TelegramUiState,
    *,
    user_id: int,
    relative_path: str,
    page: int,
    activity_index: int,
    back_target: str = "none",
) -> None:
    state = await services.snapshot_runtime_state()
    preview = read_workspace_file_preview(state.workspace_path, relative_path)
    last_request_text = ui_state.get_last_request_text(user_id, state.workspace_id)
    text, markup = _build_workspace_file_preview_view(
        preview=preview,
        provider=state.provider,
        workspace_label=_workspace_label(services, state.workspace_id),
        user_id=user_id,
        ui_state=ui_state,
        last_request_text=last_request_text,
        back_label="Back to Tool Activity",
        back_action="tool_activity_open",
        back_payload={
            "page": page,
            "activity_index": activity_index,
            "back_target": back_target,
        },
        ask_payload={
            "relative_path": preview.relative_path,
            "source": "tool_activity",
            "page": page,
            "activity_index": activity_index,
            "back_target": back_target,
        },
        quick_ask_payload={
            "relative_path": preview.relative_path,
            "source": "tool_activity",
            "page": page,
            "activity_index": activity_index,
            "back_target": back_target,
        },
        secondary_button_label="Add File to Context",
        secondary_button_action="workspace_file_add_context",
        secondary_button_payload={"relative_path": preview.relative_path},
        action_guide_entries=_workspace_item_action_guide_entries(
            ask_label="Ask Agent About File",
            subject_summary="this file",
            secondary_label="Add File to Context",
            secondary_summary="saves this file to Context Bundle without sending anything yet.",
            has_last_request=last_request_text is not None,
        ),
        supplemental_buttons=(),
    )
    await _edit_query_message(query, text, reply_markup=markup)


async def _show_tool_activity_change_preview_from_callback(
    query,
    services,
    ui_state: TelegramUiState,
    *,
    user_id: int,
    relative_path: str,
    status_code: str,
    page: int,
    activity_index: int,
    back_target: str = "none",
) -> None:
    state = await services.snapshot_runtime_state()
    diff_preview = read_workspace_git_diff_preview(
        state.workspace_path,
        relative_path,
        status_code=status_code,
    )
    last_request_text = ui_state.get_last_request_text(user_id, state.workspace_id)
    text, markup = _build_workspace_change_preview_view(
        diff_preview=diff_preview,
        provider=state.provider,
        workspace_label=_workspace_label(services, state.workspace_id),
        user_id=user_id,
        ui_state=ui_state,
        last_request_text=last_request_text,
        back_label="Back to Tool Activity",
        back_action="tool_activity_open",
        back_payload={
            "page": page,
            "activity_index": activity_index,
            "back_target": back_target,
        },
        ask_payload={
            "relative_path": relative_path,
            "status_code": status_code,
            "source": "tool_activity",
            "page": page,
            "activity_index": activity_index,
            "back_target": back_target,
        },
        quick_ask_payload={
            "relative_path": relative_path,
            "status_code": status_code,
            "source": "tool_activity",
            "page": page,
            "activity_index": activity_index,
            "back_target": back_target,
        },
        secondary_button_label="Add Change to Context",
        secondary_button_action="workspace_change_add_context",
        secondary_button_payload={
            "relative_path": diff_preview.relative_path,
            "status_code": diff_preview.status_code,
        },
        action_guide_entries=_workspace_item_action_guide_entries(
            ask_label="Ask Agent About Change",
            subject_summary="this change",
            secondary_label="Add Change to Context",
            secondary_summary="saves this change to Context Bundle without sending anything yet.",
            has_last_request=last_request_text is not None,
        ),
        supplemental_buttons=(),
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
        action_guide_entries=_workspace_item_action_guide_entries(
            ask_label="Ask Agent About File",
            subject_summary="this file",
            secondary_label="Remove From Context",
            secondary_summary="drops this file from the saved bundle without sending anything.",
            has_last_request=last_request_text is not None,
        ),
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
        action_guide_entries=_workspace_item_action_guide_entries(
            ask_label="Ask Agent About Change",
            subject_summary="this change",
            secondary_label="Remove From Context",
            secondary_summary="drops this change from the saved bundle without sending anything.",
            has_last_request=last_request_text is not None,
        ),
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
        _pending_input_cancel_notice(prompt_text),
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

    user_id = update.effective_user.id
    pending_upload_notice = _discard_pending_uploads_for_transition(ui_state, user_id)
    try:
        state, session = await _with_active_store(
            services,
            lambda store: store.reset(user_id),
        )
        await session.ensure_started()
    except Exception:
        await _reply_session_creation_failed(
            update,
            services,
            notice=pending_upload_notice,
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
    ui_state.invalidate_session_bound_interactions()
    await _sync_agent_commands_for_session(
        application,
        ui_state,
        user_id,
        session,
    )

    await _reply_with_menu(
        update.message,
        services,
        user_id,
        _prefixed_notice_text(
            pending_upload_notice,
            _new_session_success_text(
                session.session_id,
                extra_lines=(
                    _session_ready_extra_lines(
                        ui_state=ui_state,
                        user_id=user_id,
                        provider=state.provider,
                        workspace_id=state.workspace_id,
                    )
                ),
            ),
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
    pending_upload_notice = _discard_pending_uploads_for_transition(ui_state, user_id)
    try:
        state, session = await _with_active_store(
            services,
            lambda store: store.reset(user_id),
        )
        await session.ensure_started()
    except Exception:
        failure_text = _prefixed_notice_text(
            pending_upload_notice,
            _session_creation_failed_text(),
        )
        if back_target == "status":
            await _show_runtime_status_from_callback(
                query,
                services,
                ui_state,
                user_id=user_id,
                notice=failure_text,
            )
            return
        await _edit_query_message(query, failure_text)
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

    success_text = _prefixed_notice_text(
        pending_upload_notice,
        _new_session_success_text(
            session.session_id,
            extra_lines=(
                _session_ready_extra_lines(
                    ui_state=ui_state,
                    user_id=user_id,
                    provider=state.provider,
                    workspace_id=state.workspace_id,
                )
            ),
        ),
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

    user_id = update.effective_user.id
    pending_upload_notice = _discard_pending_uploads_for_transition(ui_state, user_id)
    try:
        state, session = await _with_active_store(
            services,
            lambda store: store.restart(user_id),
        )
        await session.ensure_started()
    except Exception:
        await _reply_session_creation_failed(
            update,
            services,
            notice=pending_upload_notice,
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
    ui_state.invalidate_session_bound_interactions()
    await _sync_agent_commands_for_session(
        application,
        ui_state,
        user_id,
        session,
    )

    await _reply_with_menu(
        update.message,
        services,
        user_id,
        _prefixed_notice_text(
            pending_upload_notice,
            _restart_agent_success_text(
                session.session_id,
                extra_lines=(
                    _session_ready_extra_lines(
                        ui_state=ui_state,
                        user_id=user_id,
                        provider=state.provider,
                        workspace_id=state.workspace_id,
                    )
                ),
            ),
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
    pending_upload_notice = _discard_pending_uploads_for_transition(ui_state, user_id)
    try:
        state, session = await _with_active_store(
            services,
            lambda store: store.restart(user_id),
        )
        await session.ensure_started()
    except Exception:
        failure_text = _prefixed_notice_text(
            pending_upload_notice,
            _session_creation_failed_text(),
        )
        if back_target == "status":
            await _show_runtime_status_from_callback(
                query,
                services,
                ui_state,
                user_id=user_id,
                notice=failure_text,
            )
            return
        await _edit_query_message(query, failure_text)
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

    success_text = _prefixed_notice_text(
        pending_upload_notice,
        _restart_agent_success_text(
            session.session_id,
            extra_lines=(
                _session_ready_extra_lines(
                    ui_state=ui_state,
                    user_id=user_id,
                    provider=state.provider,
                    workspace_id=state.workspace_id,
                )
            ),
        ),
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


async def _fork_live_session_from_callback(
    query,
    services,
    ui_state: TelegramUiState,
    *,
    user_id: int,
    application,
    back_target: str = "none",
) -> None:
    pending_upload_notice = _discard_pending_uploads_for_transition(ui_state, user_id)
    try:
        state, session = await _with_active_store(
            services,
            lambda store: store.fork_live_session(user_id),
        )
    except Exception:
        failure_text = _prefixed_notice_text(
            pending_upload_notice,
            _fork_session_failed_text(),
        )
        if back_target == "status":
            await _show_runtime_status_from_callback(
                query,
                services,
                ui_state,
                user_id=user_id,
                notice=failure_text,
            )
            return
        await _edit_query_message(query, failure_text)
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

    success_text = _prefixed_notice_text(
        pending_upload_notice,
        f"Forked session: {session.session_id}\n"
        f"{_session_ready_notice_for_runtime(ui_state=ui_state, user_id=user_id, state=state)}",
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

    can_fork = await _resolve_runtime_session_fork_support(
        services,
        state=state,
        active_session_id=history_state.active_session_id,
        active_session_can_fork=history_state.active_session_can_fork,
    )
    text, markup = _build_history_view(
        entries=history_state.entries,
        provider=state.provider,
        workspace_id=state.workspace_id,
        workspace_label=_workspace_label(services, state.workspace_id),
        user_id=update.effective_user.id,
        page=page,
        ui_state=ui_state,
        active_session_id=history_state.active_session_id,
        can_fork=can_fork,
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
    can_fork = await _resolve_runtime_session_fork_support(
        services,
        state=state,
        active_session_id=history_state.active_session_id,
        active_session_can_fork=history_state.active_session_can_fork,
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
        can_fork=can_fork,
        notice=notice,
        show_provider_sessions=user_id == services.admin_user_id,
        back_target=back_target,
    )
    await _edit_query_message(query, text, reply_markup=markup)


async def _show_history_entry_from_callback(
    query,
    services,
    ui_state: TelegramUiState,
    *,
    user_id: int,
    session_id: str,
    page: int,
    back_target: str = "none",
    notice: str | None = None,
) -> None:
    state, history_state = await _with_active_store(
        services,
        lambda store: _load_history_view_state(store, user_id),
    )
    entry = next(
        (candidate for candidate in history_state.entries if candidate.session_id == session_id),
        None,
    )
    if entry is None:
        await _show_session_history_from_callback(
            query,
            services,
            ui_state,
            user_id=user_id,
            page=page,
            back_target=back_target,
            notice="Session no longer exists in local history.",
        )
        return
    can_fork = await _resolve_runtime_session_fork_support(
        services,
        state=state,
        active_session_id=history_state.active_session_id,
        active_session_can_fork=history_state.active_session_can_fork,
    )
    text, markup = _build_history_entry_view(
        entry=entry,
        provider=state.provider,
        workspace_id=state.workspace_id,
        workspace_label=_workspace_label(services, state.workspace_id),
        user_id=user_id,
        page=page,
        ui_state=ui_state,
        active_session_id=history_state.active_session_id,
        can_fork=can_fork,
        back_target=back_target,
        notice=notice,
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
    can_fork = await _resolve_runtime_session_fork_support(
        services,
        state=state,
        active_session_id=provider_state.active_session_id,
        active_session_can_fork=provider_state.active_session_can_fork,
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
        can_fork=can_fork,
        cursor=cursor,
        previous_cursors=previous_cursors,
        history_page=history_page,
        back_target=back_target,
        history_back_target=history_back_target,
        notice=notice,
    )
    await _edit_query_message(query, text, reply_markup=markup)


async def _show_provider_session_detail_from_callback(
    query,
    services,
    ui_state: TelegramUiState,
    *,
    user_id: int,
    session_id: str,
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
    entry = next(
        (candidate for candidate in provider_state.entries if candidate.session_id == session_id),
        None,
    )
    if entry is None:
        await _show_provider_sessions_from_callback(
            query,
            services,
            ui_state,
            user_id=user_id,
            cursor=cursor,
            previous_cursors=previous_cursors,
            history_page=history_page,
            back_target=back_target,
            history_back_target=history_back_target,
            notice="Provider session no longer exists on this page.",
        )
        return
    can_fork = await _resolve_runtime_session_fork_support(
        services,
        state=state,
        active_session_id=provider_state.active_session_id,
        active_session_can_fork=provider_state.active_session_can_fork,
    )
    text, markup = _build_provider_session_detail_view(
        entry=entry,
        provider=state.provider,
        workspace_id=state.workspace_id,
        workspace_label=_workspace_label(services, state.workspace_id),
        user_id=user_id,
        ui_state=ui_state,
        active_session_id=provider_state.active_session_id,
        can_fork=can_fork,
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
    provider_profiles = tuple(iter_provider_profiles())
    lines = []
    if notice:
        lines.append(notice)
    lines.append(f"Current provider: {resolve_provider_profile(state.provider).display_name}")
    lines.append(f"Workspace: {_workspace_label(services, state.workspace_id)}")
    lines.append("Admin action: this changes the shared agent runtime for every Telegram user.")
    lines.extend(
        _switch_agent_impact_lines(
            state=state,
            user_id=user_id,
            ui_state=ui_state,
            replay_turn=replay_turn,
        )
    )
    lines.append(f"Available agents: {len(provider_profiles)}")
    if replay_turn is not None:
        lines.append(
            "Choose a provider below. Retry on ... replays the last turn in this workspace; "
            "Fork on ... starts a new session there first."
        )
    else:
        lines.append("Choose a provider below to switch the shared runtime now.")
    lines.append("Provider capabilities:")
    buttons = []
    for profile in provider_profiles:
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


def _switch_agent_impact_lines(
    *,
    state,
    user_id: int,
    ui_state: TelegramUiState,
    replay_turn,
) -> list[str]:
    lines = [
        "Switch impact:",
        "- Old bot buttons and pending inputs will be cleared.",
    ]
    bundle = ui_state.get_context_bundle(user_id, state.provider, state.workspace_id)
    bundle_count = 0 if bundle is None else len(bundle.items)
    if bundle_count > 0:
        lines.append(
            "- Context bundle "
            f"({_status_item_count_summary(bundle_count)}) stays with the current agent runtime "
            "and won't follow the switch."
        )
    else:
        lines.append("- Context bundle does not follow an agent switch.")
    if replay_turn is not None:
        replay_label = _status_text_snippet(replay_turn.title_hint, limit=80) or "untitled turn"
        lines.append(f"- Last Turn stays available in this workspace: {replay_label}")
        return lines
    if ui_state.get_last_request_text(user_id, state.workspace_id) is not None:
        lines.append("- Last Request stays available in this workspace after the switch.")
        return lines
    lines.append("- After switching, send a fresh request or open Bot Status to keep going.")
    return lines


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
    lines.append(f"Current provider: {resolve_provider_profile(state.provider).display_name}")
    lines.append(f"Current workspace: {_workspace_label(services, state.workspace_id)}")
    lines.append("Admin action: this changes the shared workspace for every Telegram user.")
    lines.append("Only configured workspaces are listed below.")
    lines.extend(
        _switch_workspace_impact_lines(
            state=state,
            user_id=user_id,
            ui_state=ui_state,
        )
    )
    workspaces = tuple(services.config.agent.workspaces)
    lines.append(f"Configured workspaces: {len(workspaces)}")
    lines.append("Choose a workspace below to switch the shared runtime there.")
    buttons = []
    for workspace in workspaces:
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


def _switch_agent_success_detail_text() -> str:
    return (
        "Everyone now lands in the selected agent runtime for this workspace. "
        "Context bundle does not follow an agent switch. "
        "Last Turn and Last Request stay reusable in this workspace when available."
    )


def _switch_workspace_impact_lines(
    *,
    state,
    user_id: int,
    ui_state: TelegramUiState,
) -> list[str]:
    bundle = ui_state.get_context_bundle(user_id, state.provider, state.workspace_id)
    bundle_count = 0 if bundle is None else len(bundle.items)
    state_labels = []
    if bundle_count > 0:
        state_labels.append(f"Context Bundle ({_status_item_count_summary(bundle_count)})")
    if ui_state.get_last_request_text(user_id, state.workspace_id) is not None:
        state_labels.append("Last Request")
    if ui_state.get_last_turn(user_id, state.provider, state.workspace_id) is not None:
        state_labels.append("Last Turn")

    lines = [
        "Switch impact:",
        "- Old bot buttons and pending inputs will be cleared.",
    ]
    if state_labels:
        lines.append(f"- Current workspace state that will stay behind: {', '.join(state_labels)}.")
    else:
        lines.append("- Any Context Bundle, Last Request, or Last Turn from this workspace will stay behind.")
    lines.append("- Rebuild context in the target workspace before you ask.")
    return lines


def _switch_workspace_success_detail_text() -> str:
    return (
        "Everyone now lands in the selected workspace. "
        "Workspace-specific context does not follow the switch. "
        "Rebuild context in the new workspace before you ask."
    )


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
            _no_model_mode_controls_text(),
        )
        return

    text, markup = _build_model_mode_view(
        user_id=update.effective_user.id,
        session_id=session.session_id,
        provider=state.provider,
        workspace_label=_workspace_label(services, state.workspace_id),
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
    try:
        state, session, model_selection, mode_selection, created_session = await _load_model_mode_callback_state(
            services,
            ui_state,
            user_id=user_id,
            application=application,
        )
    except _ModelModeSessionCreationError:
        await _show_model_mode_action_recovery(
            query,
            services,
            ui_state,
            user_id=user_id,
            text=_session_creation_failed_text(),
            back_target=back_target,
        )
        return
    except Exception:
        if back_target == "status":
            await _show_runtime_status_from_callback(
                query,
                services,
                ui_state,
                user_id=user_id,
                notice=_model_mode_load_failed_text(),
            )
            return
        raise

    if model_selection is None and mode_selection is None:
        buttons: list[list[InlineKeyboardButton]] = []
        _append_status_recovery_button(
            buttons,
            ui_state=ui_state,
            user_id=user_id,
            back_target=back_target,
        )
        markup = None if not buttons else InlineKeyboardMarkup(buttons)
        await _edit_query_message(
            query,
            _no_model_mode_controls_text(),
            reply_markup=markup,
        )
        return

    text, markup = _build_model_mode_view(
        user_id=user_id,
        session_id=session.session_id,
        provider=state.provider,
        workspace_label=_workspace_label(services, state.workspace_id),
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


async def _load_model_mode_callback_state(
    services,
    ui_state: TelegramUiState,
    *,
    user_id: int,
    application,
):
    created_session = False
    state, session = await _with_active_store(
        services,
        lambda store: store.peek(user_id),
    )
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
        except Exception as exc:
            raise _ModelModeSessionCreationError() from exc
        created_session = True

    try:
        await session.ensure_started()
    except Exception as exc:
        raise _ModelModeSessionCreationError() from exc

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

    return (
        state,
        session,
        session.get_selection("model"),
        session.get_selection("mode"),
        created_session,
    )


async def _show_selection_detail_from_callback(
    query,
    services,
    ui_state: TelegramUiState,
    *,
    user_id: int,
    kind: str,
    value: str,
    application,
    back_target: str = "none",
    notice: str | None = None,
) -> None:
    try:
        state, session, model_selection, mode_selection, _created_session = await _load_model_mode_callback_state(
            services,
            ui_state,
            user_id=user_id,
            application=application,
        )
    except _ModelModeSessionCreationError:
        await _show_model_mode_action_recovery(
            query,
            services,
            ui_state,
            user_id=user_id,
            text=_session_creation_failed_text(),
            back_target=back_target,
        )
        return
    except Exception:
        if back_target == "status":
            await _show_runtime_status_from_callback(
                query,
                services,
                ui_state,
                user_id=user_id,
                notice=_model_mode_load_failed_text(),
            )
            return
        raise

    selection = model_selection if kind == "model" else mode_selection if kind == "mode" else None
    if selection is None:
        await _show_model_mode_menu_from_callback(
            query,
            services,
            ui_state,
            user_id=user_id,
            application=application,
            back_target=back_target,
            notice=f"{_selection_kind_label(kind)} selection is no longer available.",
        )
        return

    choice_index = next(
        (index for index, choice in enumerate(selection.choices) if choice.value == value),
        -1,
    )
    if choice_index < 0:
        await _show_model_mode_menu_from_callback(
            query,
            services,
            ui_state,
            user_id=user_id,
            application=application,
            back_target=back_target,
            notice=f"{_selection_kind_label(kind)} choice is no longer available.",
        )
        return

    text, markup = _build_selection_detail_view(
        session_id=session.session_id,
        selection=selection,
        choice=selection.choices[choice_index],
        choice_index=choice_index,
        provider=state.provider,
        workspace_label=_workspace_label(services, state.workspace_id),
        user_id=user_id,
        ui_state=ui_state,
        can_retry_last_turn=ui_state.get_last_turn(
            user_id,
            state.provider,
            state.workspace_id,
        )
        is not None,
        back_target=back_target,
        notice=notice,
    )
    await _edit_query_message(query, text, reply_markup=markup)


async def _retry_last_turn(
    update: Update,
    services,
    ui_state: TelegramUiState,
    *,
    application,
    after_turn_success=None,
    on_missing_replay_turn=None,
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
        if on_missing_replay_turn is not None:
            try:
                await on_missing_replay_turn()
                return
            except Exception:
                pass
        await _show_runtime_status(
            update,
            services,
            ui_state,
            notice=_no_previous_turn_text(),
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
    on_missing_replay_turn=None,
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
        if on_missing_replay_turn is not None:
            try:
                await on_missing_replay_turn()
                return
            except Exception:
                pass
        await _show_runtime_status(
            update,
            services,
            ui_state,
            notice=_no_previous_turn_text(),
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
        await query.answer(_unauthorized_text(), show_alert=True)
        return

    pending_upload_notice = _discard_pending_uploads_for_transition(
        ui_state,
        query.from_user.id,
    )
    target_name = resolve_provider_profile(provider).display_name
    await query.answer()
    await _edit_query_message(query, f"Switching to {target_name}...")
    try:
        switched = await asyncio.wait_for(
            services.switch_provider(provider),
            CALLBACK_OPERATION_TIMEOUT_SECONDS,
        )
    except Exception:
        try:
            await _show_switch_agent_menu_from_callback(
                query,
                services,
                ui_state,
                user_id=query.from_user.id,
                back_target=back_target,
                notice=_prefixed_notice_text(
                    pending_upload_notice,
                    _switch_agent_failed_text(),
                ),
            )
            return
        except Exception:
            await _edit_query_message(
                query,
                _prefixed_notice_text(
                    pending_upload_notice,
                    _switch_agent_failed_text(),
                ),
            )
            return

    ui_state.invalidate_runtime_bound_interactions()
    state = await services.snapshot_runtime_state()
    success_text = _prefixed_notice_text(
        pending_upload_notice,
        (
            f"Switched agent to {resolve_provider_profile(switched).display_name} "
            f"in {_workspace_label(services, state.workspace_id)}. "
            "Old bot buttons and pending inputs were cleared.\n"
            f"{_switch_agent_success_detail_text()}"
        ),
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
                return
            try:
                await _show_switch_agent_menu_from_callback(
                    query,
                    services,
                    ui_state,
                    user_id=query.from_user.id,
                    back_target=back_target,
                    notice=f"{success_text}\nRetried last turn on the new agent.",
                )
            except Exception:
                await _edit_query_message(
                    query,
                    f"{success_text}\nRetried last turn on the new agent.",
                )

        async def _on_retry_missing_replay_turn() -> None:
            if back_target == "status":
                await _show_runtime_status_from_callback(
                    query,
                    services,
                    ui_state,
                    user_id=query.from_user.id,
                    notice=f"{success_text}\n{_no_previous_turn_text()}",
                )
                return
            try:
                await _show_switch_agent_menu_from_callback(
                    query,
                    services,
                    ui_state,
                    user_id=query.from_user.id,
                    back_target=back_target,
                    notice=f"{success_text}\n{_no_previous_turn_text()}",
                )
            except Exception:
                await _edit_query_message(
                    query,
                    f"{success_text}\n{_no_previous_turn_text()}",
                )

        async def _on_retry_prepare_failure() -> None:
            if back_target == "status":
                await _show_runtime_status_from_callback(
                    query,
                    services,
                    ui_state,
                    user_id=query.from_user.id,
                    notice=f"{success_text}\n{_request_failed_text()}",
                )
                return
            try:
                await _show_switch_agent_menu_from_callback(
                    query,
                    services,
                    ui_state,
                    user_id=query.from_user.id,
                    back_target=back_target,
                    notice=f"{success_text}\n{_request_failed_text()}",
                )
            except Exception:
                await _edit_query_message(
                    query,
                    f"{success_text}\n{_request_failed_text()}",
                )

        async def _on_retry_turn_failure() -> None:
            if back_target == "status":
                await _show_runtime_status_from_callback(
                    query,
                    services,
                    ui_state,
                    user_id=query.from_user.id,
                    notice=f"{success_text}\n{_request_failed_text()}",
                )
                return
            try:
                await _show_switch_agent_menu_from_callback(
                    query,
                    services,
                    ui_state,
                    user_id=query.from_user.id,
                    back_target=back_target,
                    notice=f"{success_text}\n{_request_failed_text()}",
                )
            except Exception:
                await _edit_query_message(
                    query,
                    f"{success_text}\n{_request_failed_text()}",
                )

        await _retry_last_turn(
            _message_update_from_callback(query),
            services,
            ui_state,
            application=application,
            after_turn_success=_after_retry_success,
            on_missing_replay_turn=_on_retry_missing_replay_turn,
            on_prepare_failure=_on_retry_prepare_failure,
            on_turn_failure=_on_retry_turn_failure,
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
                return
            try:
                await _show_switch_agent_menu_from_callback(
                    query,
                    services,
                    ui_state,
                    user_id=query.from_user.id,
                    back_target=back_target,
                    notice=f"{success_text}\nForked last turn on the new agent.",
                )
            except Exception:
                await _edit_query_message(
                    query,
                    f"{success_text}\nForked last turn on the new agent.",
                )

        async def _on_fork_missing_replay_turn() -> None:
            if back_target == "status":
                await _show_runtime_status_from_callback(
                    query,
                    services,
                    ui_state,
                    user_id=query.from_user.id,
                    notice=f"{success_text}\n{_no_previous_turn_text()}",
                )
                return
            try:
                await _show_switch_agent_menu_from_callback(
                    query,
                    services,
                    ui_state,
                    user_id=query.from_user.id,
                    back_target=back_target,
                    notice=f"{success_text}\n{_no_previous_turn_text()}",
                )
            except Exception:
                await _edit_query_message(
                    query,
                    f"{success_text}\n{_no_previous_turn_text()}",
                )

        async def _on_fork_session_creation_failed() -> None:
            if back_target == "status":
                await _show_runtime_status_from_callback(
                    query,
                    services,
                    ui_state,
                    user_id=query.from_user.id,
                    notice=f"{success_text}\n{_session_creation_failed_text()}",
                )
                return
            try:
                await _show_switch_agent_menu_from_callback(
                    query,
                    services,
                    ui_state,
                    user_id=query.from_user.id,
                    back_target=back_target,
                    notice=f"{success_text}\n{_session_creation_failed_text()}",
                )
            except Exception:
                await _edit_query_message(
                    query,
                    f"{success_text}\n{_session_creation_failed_text()}",
                )

        async def _on_fork_turn_failure() -> None:
            if back_target == "status":
                await _show_runtime_status_from_callback(
                    query,
                    services,
                    ui_state,
                    user_id=query.from_user.id,
                    notice=f"{success_text}\n{_request_failed_text()}",
                )
                return
            try:
                await _show_switch_agent_menu_from_callback(
                    query,
                    services,
                    ui_state,
                    user_id=query.from_user.id,
                    back_target=back_target,
                    notice=f"{success_text}\n{_request_failed_text()}",
                )
            except Exception:
                await _edit_query_message(
                    query,
                    f"{success_text}\n{_request_failed_text()}",
                )

        await _fork_last_turn(
            _message_update_from_callback(query),
            services,
            ui_state,
            application=application,
            after_turn_success=_after_fork_success,
            on_missing_replay_turn=_on_fork_missing_replay_turn,
            on_session_creation_failed=_on_fork_session_creation_failed,
            on_turn_failure=_on_fork_turn_failure,
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
    try:
        await _show_switch_agent_menu_from_callback(
            query,
            services,
            ui_state,
            user_id=query.from_user.id,
            back_target=back_target,
            notice=success_text,
        )
    except Exception:
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
    pending_upload_notice = _discard_pending_uploads_for_transition(ui_state, user_id)
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
                    notice=_prefixed_notice_text(
                        pending_upload_notice,
                        _switch_session_failed_text(),
                    ),
                )
                return
            except Exception:
                pass
        try:
            await _show_session_history_from_callback(
                query,
                services,
                ui_state,
                user_id=user_id,
                page=page,
                back_target=back_target,
                notice=_prefixed_notice_text(
                    pending_upload_notice,
                    _switch_session_failed_text(),
                ),
            )
            return
        except Exception:
            pass
        await _edit_query_message(
            query,
            _prefixed_notice_text(
                pending_upload_notice,
                _switch_session_failed_text(),
            ),
        )
        return
    ui_state.invalidate_session_bound_interactions()
    await _sync_agent_commands_for_session(
        application,
        ui_state,
        user_id,
        session,
    )
    success_text = _prefixed_notice_text(
        pending_upload_notice,
        (
            f"Switched to session {session.session_id} on "
            f"{resolve_provider_profile(state.provider).display_name} in "
            f"{_workspace_label(services, state.workspace_id)}. "
            f"{_session_ready_notice_for_runtime(ui_state=ui_state, user_id=user_id, state=state)}"
        ),
    )
    if replay_after_switch:
        await _edit_query_message(
            query,
            f"{success_text}\nRetrying last turn in this session...",
        )
        if query.message is None:
            return
        if back_target == "status":
            async def _after_retry_success(state, session) -> None:
                await _show_runtime_status_from_callback(
                    query,
                    services,
                    ui_state,
                    user_id=user_id,
                    notice=f"{success_text}\nRetried last turn in this session.",
                )

            async def _on_retry_missing_replay_turn() -> None:
                await _show_runtime_status_from_callback(
                    query,
                    services,
                    ui_state,
                    user_id=user_id,
                    notice=f"{success_text}\n{_no_previous_turn_text()}",
                )

            async def _on_retry_prepare_failure() -> None:
                await _show_runtime_status_from_callback(
                    query,
                    services,
                    ui_state,
                    user_id=user_id,
                    notice=f"{success_text}\n{_request_failed_text()}",
                )

            async def _on_retry_turn_failure() -> None:
                await _show_runtime_status_from_callback(
                    query,
                    services,
                    ui_state,
                    user_id=user_id,
                    notice=f"{success_text}\n{_request_failed_text()}",
                )

            await _retry_last_turn(
                _message_update_from_callback(query),
                services,
                ui_state,
                application=application,
                after_turn_success=_after_retry_success,
                on_missing_replay_turn=_on_retry_missing_replay_turn,
                on_prepare_failure=_on_retry_prepare_failure,
                on_turn_failure=_on_retry_turn_failure,
            )
            return
        if query.message is not None:
            async def _after_retry_success(_state, _session) -> None:
                await _show_session_history_from_callback(
                    query,
                    services,
                    ui_state,
                    user_id=user_id,
                    page=page,
                    back_target=back_target,
                    notice=f"{success_text}\nRetried last turn in this session.",
                )

            async def _on_retry_missing_replay_turn() -> None:
                await _show_session_history_from_callback(
                    query,
                    services,
                    ui_state,
                    user_id=user_id,
                    page=page,
                    back_target=back_target,
                    notice=f"{success_text}\n{_no_previous_turn_text()}",
                )

            async def _on_retry_prepare_failure() -> None:
                await _show_session_history_from_callback(
                    query,
                    services,
                    ui_state,
                    user_id=user_id,
                    page=page,
                    back_target=back_target,
                    notice=f"{success_text}\n{_request_failed_text()}",
                )

            async def _on_retry_turn_failure() -> None:
                await _show_session_history_from_callback(
                    query,
                    services,
                    ui_state,
                    user_id=user_id,
                    page=page,
                    back_target=back_target,
                    notice=f"{success_text}\n{_request_failed_text()}",
                )

            await _retry_last_turn(
                _message_update_from_callback(query),
                services,
                ui_state,
                application=application,
                after_turn_success=_after_retry_success,
                on_missing_replay_turn=_on_retry_missing_replay_turn,
                on_prepare_failure=_on_retry_prepare_failure,
                on_turn_failure=_on_retry_turn_failure,
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
    await _show_session_history_from_callback(
        query,
        services,
        ui_state,
        user_id=user_id,
        page=page,
        back_target=back_target,
        notice=success_text,
    )


async def _fork_history_session_from_callback(
    query,
    services,
    ui_state: TelegramUiState,
    *,
    user_id: int,
    session_id: str,
    application,
    replay_after_fork: bool = False,
    page: int = 0,
    back_target: str = "none",
) -> None:
    await query.answer()
    await _edit_query_message(query, "Forking session...")
    try:
        state, session = await asyncio.wait_for(
            _with_active_store(
                services,
                lambda store: store.fork_history_session(user_id, session_id),
            ),
            CALLBACK_OPERATION_TIMEOUT_SECONDS,
        )
    except Exception:
        if back_target == "status":
            try:
                await _show_runtime_status_from_callback(
                    query,
                    services,
                    ui_state,
                    user_id=user_id,
                    notice=_fork_session_failed_text(),
                )
                return
            except Exception:
                pass
        try:
            await _show_session_history_from_callback(
                query,
                services,
                ui_state,
                user_id=user_id,
                page=page,
                back_target=back_target,
                notice=_fork_session_failed_text(),
            )
            return
        except Exception:
            pass
        await _edit_query_message(query, _fork_session_failed_text())
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
        f"Forked session {session.session_id} from {session_id} on "
        f"{resolve_provider_profile(state.provider).display_name} in "
        f"{_workspace_label(services, state.workspace_id)}. "
        f"{_session_ready_notice_for_runtime(ui_state=ui_state, user_id=user_id, state=state)}"
    )
    if replay_after_fork:
        await _edit_query_message(
            query,
            f"{success_text}\nRetrying last turn in this session...",
        )
        if query.message is not None and back_target == "status":
            async def _after_retry_success(state, session) -> None:
                await _show_runtime_status_from_callback(
                    query,
                    services,
                    ui_state,
                    user_id=user_id,
                    notice=f"{success_text}\nRetried last turn in this session.",
                )

            async def _on_retry_missing_replay_turn() -> None:
                await _show_runtime_status_from_callback(
                    query,
                    services,
                    ui_state,
                    user_id=user_id,
                    notice=f"{success_text}\n{_no_previous_turn_text()}",
                )

            async def _on_retry_prepare_failure() -> None:
                await _show_runtime_status_from_callback(
                    query,
                    services,
                    ui_state,
                    user_id=user_id,
                    notice=f"{success_text}\n{_request_failed_text()}",
                )

            async def _on_retry_turn_failure() -> None:
                await _show_runtime_status_from_callback(
                    query,
                    services,
                    ui_state,
                    user_id=user_id,
                    notice=f"{success_text}\n{_request_failed_text()}",
                )

            await _retry_last_turn(
                _message_update_from_callback(query),
                services,
                ui_state,
                application=application,
                after_turn_success=_after_retry_success,
                on_missing_replay_turn=_on_retry_missing_replay_turn,
                on_prepare_failure=_on_retry_prepare_failure,
                on_turn_failure=_on_retry_turn_failure,
            )
            return
        if query.message is not None:
            async def _after_retry_success(_state, _session) -> None:
                await _show_session_history_from_callback(
                    query,
                    services,
                    ui_state,
                    user_id=user_id,
                    page=page,
                    back_target=back_target,
                    notice=f"{success_text}\nRetried last turn in this session.",
                )

            async def _on_retry_missing_replay_turn() -> None:
                await _show_session_history_from_callback(
                    query,
                    services,
                    ui_state,
                    user_id=user_id,
                    page=page,
                    back_target=back_target,
                    notice=f"{success_text}\n{_no_previous_turn_text()}",
                )

            async def _on_retry_prepare_failure() -> None:
                await _show_session_history_from_callback(
                    query,
                    services,
                    ui_state,
                    user_id=user_id,
                    page=page,
                    back_target=back_target,
                    notice=f"{success_text}\n{_request_failed_text()}",
                )

            async def _on_retry_turn_failure() -> None:
                await _show_session_history_from_callback(
                    query,
                    services,
                    ui_state,
                    user_id=user_id,
                    page=page,
                    back_target=back_target,
                    notice=f"{success_text}\n{_request_failed_text()}",
                )

            await _retry_last_turn(
                _message_update_from_callback(query),
                services,
                ui_state,
                application=application,
                after_turn_success=_after_retry_success,
                on_missing_replay_turn=_on_retry_missing_replay_turn,
                on_prepare_failure=_on_retry_prepare_failure,
                on_turn_failure=_on_retry_turn_failure,
            )
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
    await _show_session_history_from_callback(
        query,
        services,
        ui_state,
        user_id=user_id,
        page=page,
        back_target=back_target,
        notice=success_text,
    )


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
        await query.answer(_unauthorized_text(), show_alert=True)
        return
    pending_upload_notice = _discard_pending_uploads_for_transition(ui_state, user_id)
    await query.answer()
    await _edit_query_message(query, "Switching to provider session...")
    back_target = str(payload.get("back_target", "history"))
    history_back_target = str(payload.get("history_back_target", "none"))
    try:
        state, session = await asyncio.wait_for(
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
                notice=_prefixed_notice_text(
                    pending_upload_notice,
                    _switch_provider_session_failed_text(),
                ),
            )
        except Exception:
            await _edit_query_message(
                query,
                _prefixed_notice_text(
                    pending_upload_notice,
                    _switch_provider_session_failed_text(),
                ),
            )
        return

    ui_state.invalidate_session_bound_interactions()
    await _sync_agent_commands_for_session(
        application,
        ui_state,
        user_id,
        session,
    )
    success_text = _prefixed_notice_text(
        pending_upload_notice,
        (
            f"Switched to provider session {payload['session_id']}. "
            f"{_session_ready_notice_for_runtime(ui_state=ui_state, user_id=user_id, state=state)}"
        ),
    )
    if replay_after_switch:
        await _edit_query_message(
            query,
            f"{success_text}\nRetrying last turn in this session...",
        )
        if query.message is None:
            return
        if back_target == "status":
            async def _after_retry_success(state, session) -> None:
                await _show_runtime_status_from_callback(
                    query,
                    services,
                    ui_state,
                    user_id=user_id,
                    notice=f"{success_text}\nRetried last turn in this session.",
                )

            async def _on_retry_missing_replay_turn() -> None:
                await _show_runtime_status_from_callback(
                    query,
                    services,
                    ui_state,
                    user_id=user_id,
                    notice=f"{success_text}\n{_no_previous_turn_text()}",
                )

            async def _on_retry_prepare_failure() -> None:
                await _show_runtime_status_from_callback(
                    query,
                    services,
                    ui_state,
                    user_id=user_id,
                    notice=f"{success_text}\n{_request_failed_text()}",
                )

            async def _on_retry_turn_failure() -> None:
                await _show_runtime_status_from_callback(
                    query,
                    services,
                    ui_state,
                    user_id=user_id,
                    notice=f"{success_text}\n{_request_failed_text()}",
                )

            await _retry_last_turn(
                _message_update_from_callback(query),
                services,
                ui_state,
                application=application,
                after_turn_success=_after_retry_success,
                on_missing_replay_turn=_on_retry_missing_replay_turn,
                on_prepare_failure=_on_retry_prepare_failure,
                on_turn_failure=_on_retry_turn_failure,
            )
            return
        async def _after_retry_success(_state, _session) -> None:
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
                notice=f"{success_text}\nRetried last turn in this session.",
            )

        async def _on_retry_missing_replay_turn() -> None:
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
                notice=f"{success_text}\n{_no_previous_turn_text()}",
            )

        async def _on_retry_prepare_failure() -> None:
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
                notice=f"{success_text}\n{_request_failed_text()}",
            )

        async def _on_retry_turn_failure() -> None:
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
                notice=f"{success_text}\n{_request_failed_text()}",
            )

        await _retry_last_turn(
            _message_update_from_callback(query),
            services,
            ui_state,
            application=application,
            after_turn_success=_after_retry_success,
            on_missing_replay_turn=_on_retry_missing_replay_turn,
            on_prepare_failure=_on_retry_prepare_failure,
            on_turn_failure=_on_retry_turn_failure,
        )
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


async def _fork_provider_session_from_callback(
    query,
    services,
    ui_state: TelegramUiState,
    *,
    user_id: int,
    payload: dict[str, Any],
    application,
    replay_after_fork: bool = False,
) -> None:
    if query.from_user is None or query.from_user.id != services.admin_user_id:
        await query.answer(_unauthorized_text(), show_alert=True)
        return
    pending_upload_notice = _discard_pending_uploads_for_transition(ui_state, user_id)
    await query.answer()
    await _edit_query_message(query, "Forking provider session...")
    back_target = str(payload.get("back_target", "history"))
    history_back_target = str(payload.get("history_back_target", "none"))
    try:
        state, session = await asyncio.wait_for(
            _with_active_store(
                services,
                lambda store: store.fork_provider_session(
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
                notice=_prefixed_notice_text(
                    pending_upload_notice,
                    _fork_provider_session_failed_text(),
                ),
            )
        except Exception:
            await _edit_query_message(
                query,
                _prefixed_notice_text(
                    pending_upload_notice,
                    _fork_provider_session_failed_text(),
                ),
            )
        return

    try:
        await state.session_store.record_session_usage(
            user_id,
            session,
            title_hint=payload.get("title"),
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
    success_text = _prefixed_notice_text(
        pending_upload_notice,
        (
            f"Forked provider session {payload['session_id']} into {session.session_id}. "
            f"{_session_ready_notice_for_runtime(ui_state=ui_state, user_id=user_id, state=state)}"
        ),
    )
    if replay_after_fork:
        await _edit_query_message(
            query,
            f"{success_text}\nRetrying last turn in this session...",
        )
        if query.message is not None and back_target == "status":
            async def _after_retry_success(state, session) -> None:
                await _show_runtime_status_from_callback(
                    query,
                    services,
                    ui_state,
                    user_id=user_id,
                    notice=f"{success_text}\nRetried last turn in this session.",
                )

            async def _on_retry_missing_replay_turn() -> None:
                await _show_runtime_status_from_callback(
                    query,
                    services,
                    ui_state,
                    user_id=user_id,
                    notice=f"{success_text}\n{_no_previous_turn_text()}",
                )

            async def _on_retry_prepare_failure() -> None:
                await _show_runtime_status_from_callback(
                    query,
                    services,
                    ui_state,
                    user_id=user_id,
                    notice=f"{success_text}\n{_request_failed_text()}",
                )

            async def _on_retry_turn_failure() -> None:
                await _show_runtime_status_from_callback(
                    query,
                    services,
                    ui_state,
                    user_id=user_id,
                    notice=f"{success_text}\n{_request_failed_text()}",
                )

            await _retry_last_turn(
                _message_update_from_callback(query),
                services,
                ui_state,
                application=application,
                after_turn_success=_after_retry_success,
                on_missing_replay_turn=_on_retry_missing_replay_turn,
                on_prepare_failure=_on_retry_prepare_failure,
                on_turn_failure=_on_retry_turn_failure,
            )
            return
        if query.message is not None:
            async def _after_retry_success(_state, _session) -> None:
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
                    notice=f"{success_text}\nRetried last turn in this session.",
                )

            async def _on_retry_missing_replay_turn() -> None:
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
                    notice=f"{success_text}\n{_no_previous_turn_text()}",
                )

            async def _on_retry_prepare_failure() -> None:
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
                    notice=f"{success_text}\n{_request_failed_text()}",
                )

            async def _on_retry_turn_failure() -> None:
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
                    notice=f"{success_text}\n{_request_failed_text()}",
                )

            await _retry_last_turn(
                _message_update_from_callback(query),
                services,
                ui_state,
                application=application,
                after_turn_success=_after_retry_success,
                on_missing_replay_turn=_on_retry_missing_replay_turn,
                on_prepare_failure=_on_retry_prepare_failure,
                on_turn_failure=_on_retry_turn_failure,
            )
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
        await _show_model_mode_action_recovery(
            query,
            services,
            ui_state,
            user_id=user_id,
            text=_no_active_session_text(),
            back_target=back_target,
        )
        return
    try:
        await session.set_selection(kind, value)
    except Exception:
        await _show_model_mode_action_recovery(
            query,
            services,
            ui_state,
            user_id=user_id,
            text=_selection_update_failed_text(),
            back_target=back_target,
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
                provider=state.provider,
                workspace_label=_workspace_label(services, state.workspace_id),
                model_selection=session.get_selection("model"),
                mode_selection=session.get_selection("mode"),
                ui_state=ui_state,
                can_retry_last_turn=False,
                back_target=back_target,
                notice=(
                    f"{updated_notice}\n"
                    f"{_no_previous_turn_text()}"
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
        if back_target == "status":
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
                    notice=f"{updated_notice}\n{_request_failed_text()}",
                )

            async def _on_retry_turn_failure() -> None:
                await _show_runtime_status_from_callback(
                    query,
                    services,
                    ui_state,
                    user_id=user_id,
                    notice=f"{updated_notice}\n{_request_failed_text()}",
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
            return
        async def _after_retry_success(_state, _session) -> None:
            await _restore_model_mode_menu_from_callback(
                query,
                services,
                ui_state,
                user_id=user_id,
                notice=f"{updated_notice}\nRetried last turn with the updated setting.",
                back_target=back_target,
            )

        async def _on_retry_prepare_failure() -> None:
            await _restore_model_mode_menu_from_callback(
                query,
                services,
                ui_state,
                user_id=user_id,
                notice=f"{updated_notice}\n{_request_failed_text()}",
                back_target=back_target,
            )

        async def _on_retry_turn_failure() -> None:
            await _restore_model_mode_menu_from_callback(
                query,
                services,
                ui_state,
                user_id=user_id,
                notice=f"{updated_notice}\n{_request_failed_text()}",
                back_target=back_target,
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
        return

    text, markup = _build_model_mode_view(
        user_id=user_id,
        session_id=session.session_id,
        provider=state.provider,
        workspace_label=_workspace_label(services, state.workspace_id),
        model_selection=session.get_selection("model"),
        mode_selection=session.get_selection("mode"),
        ui_state=ui_state,
        can_retry_last_turn=replay_turn is not None,
        back_target=back_target,
        notice=updated_notice,
    )
    await _edit_query_message(query, text, reply_markup=markup)


async def _show_model_mode_action_recovery(
    query,
    services,
    ui_state: TelegramUiState,
    *,
    user_id: int,
    text: str,
    back_target: str,
) -> None:
    if back_target == "status":
        await _show_runtime_status_from_callback(
            query,
            services,
            ui_state,
            user_id=user_id,
            notice=text,
        )
        return

    back_kwargs: dict[str, Any] = {}
    if back_target == "none":
        back_kwargs = {
            "back_label": "Open Bot Status",
            "back_action": "runtime_status_page",
        }

    await _show_navigation_failure(
        query,
        ui_state=ui_state,
        user_id=user_id,
        text=text,
        retry_label="Reopen Model / Mode",
        retry_action="model_mode_page",
        retry_payload={"back_target": back_target},
        back_target=back_target,
        **back_kwargs,
    )


async def _restore_model_mode_menu_from_callback(
    query,
    services,
    ui_state: TelegramUiState,
    *,
    user_id: int,
    notice: str,
    back_target: str,
) -> None:
    try:
        state, session = await _with_active_store(
            services,
            lambda store: store.peek(user_id),
        )
    except Exception:
        await _show_model_mode_action_recovery(
            query,
            services,
            ui_state,
            user_id=user_id,
            text=notice,
            back_target=back_target,
        )
        return
    if session is None:
        await _show_model_mode_action_recovery(
            query,
            services,
            ui_state,
            user_id=user_id,
            text=notice,
            back_target=back_target,
        )
        return
    text, markup = _build_model_mode_view(
        user_id=user_id,
        session_id=session.session_id,
        provider=state.provider,
        workspace_label=_workspace_label(services, state.workspace_id),
        model_selection=session.get_selection("model"),
        mode_selection=session.get_selection("mode"),
        ui_state=ui_state,
        can_retry_last_turn=ui_state.get_last_turn(
            user_id,
            state.provider,
            state.workspace_id,
        )
        is not None,
        back_target=back_target,
        notice=notice,
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
            notice=_no_active_session_text(),
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
            notice=_selection_update_failed_text(),
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
            notice=_no_active_session_text(),
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
            notice=_selection_update_failed_text(),
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
                f"{_no_previous_turn_text()}"
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
            notice=f"{updated_notice}\n{_request_failed_text()}",
        )

    async def _on_retry_turn_failure() -> None:
        await _show_runtime_status_from_callback(
            query,
            services,
            ui_state,
            user_id=user_id,
            notice=f"{updated_notice}\n{_request_failed_text()}",
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

    if action == "recover_run_last_request":
        await query.answer()
        if query.message is None or query.from_user is None:
            return
        state = await services.snapshot_runtime_state()
        last_request = ui_state.get_last_request(user_id, state.workspace_id)
        if last_request is None:
            await _reply_with_menu(
                query.message,
                services,
                user_id,
                _no_previous_request_text(),
            )
            return
        await _run_last_request_on_message(
            query.message,
            user_id,
            services,
            ui_state,
            last_request=last_request,
            provider=state.provider,
            workspace_id=state.workspace_id,
            application=application,
        )
        return

    if action == "recover_runtime_status":
        await query.answer()
        if query.message is None or query.from_user is None:
            return
        await _show_runtime_status(
            _message_update_from_callback(query),
            services,
            ui_state,
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

    if action == "recover_workspace_search":
        await query.answer()
        await _show_workspace_search_prompt_from_callback(
            query,
            ui_state,
            user_id=user_id,
            cancel_action="workspace_search_cancel",
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
            await _show_navigation_failure(
                query,
                ui_state=ui_state,
                user_id=user_id,
                text="Couldn't load Bot Status. Try again or use /start.",
                retry_action="runtime_status_page",
            )
        return

    if action == "runtime_status_open":
        await query.answer()
        target = str(payload.get("target", ""))
        back_target = str(payload.get("back_target", "status"))
        ui_state.clear_pending_text_action(user_id)
        try:
            if target == "history":
                await _show_session_history_from_callback(
                    query,
                    services,
                    ui_state,
                    user_id=user_id,
                    page=0,
                    back_target=back_target,
                )
                return
            if target == "commands":
                await _show_agent_commands_menu_from_callback(
                    query,
                    services,
                    ui_state,
                    user_id=user_id,
                    page=0,
                    back_target=back_target,
                )
                return
            if target == "session_info":
                await _show_session_info_from_callback(
                    query,
                    services,
                    ui_state,
                    user_id=user_id,
                    back_target=back_target,
                )
                return
            if target == "usage":
                await _show_usage_from_callback(
                    query,
                    services,
                    ui_state,
                    user_id=user_id,
                    back_target=back_target,
                )
                return
            if target == "last_request":
                await _show_last_request_from_callback(
                    query,
                    services,
                    ui_state,
                    user_id=user_id,
                    back_target=back_target,
                )
                return
            if target == "workspace_runtime":
                await _show_workspace_runtime_from_callback(
                    query,
                    services,
                    ui_state,
                    user_id=user_id,
                    back_target=back_target,
                )
                return
            if target == "last_turn":
                await _show_last_turn_from_callback(
                    query,
                    services,
                    ui_state,
                    user_id=user_id,
                    page=0,
                    back_target=back_target,
                )
                return
            if target == "plan":
                await _show_plan_from_callback(
                    query,
                    services,
                    ui_state,
                    user_id=user_id,
                    page=0,
                    back_target=back_target,
                )
                return
            if target == "tools":
                await _show_tool_activity_from_callback(
                    query,
                    services,
                    ui_state,
                    user_id=user_id,
                    page=0,
                    back_target=back_target,
                )
                return
            if target == "provider_sessions":
                if query.from_user is None or query.from_user.id != services.admin_user_id:
                    await query.answer(_unauthorized_text(), show_alert=True)
                    return
                await _show_provider_sessions_from_callback(
                    query,
                    services,
                    ui_state,
                    user_id=user_id,
                    cursor=None,
                    previous_cursors=(),
                    history_page=0,
                    back_target=back_target,
                    history_back_target=back_target,
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
                    back_target=back_target,
                )
                return
            if target == "search":
                pending_payload: dict[str, Any] = {"back_target": back_target}
                if query.message is not None:
                    pending_payload["source_message"] = query.message
                await _show_workspace_search_prompt_from_callback(
                    query,
                    ui_state,
                    user_id=user_id,
                    cancel_action="runtime_status_search_cancel",
                    pending_payload=pending_payload,
                )
                return
            if target == "changes":
                await _show_workspace_changes_from_callback(
                    query,
                    services,
                    ui_state,
                    user_id=user_id,
                    page=0,
                    back_target=back_target,
                )
                return
            if target == "bundle":
                await _show_context_bundle_from_callback(
                    query,
                    services,
                    ui_state,
                    user_id=user_id,
                    page=0,
                    back_target=back_target,
                )
                return
        except Exception:
            await _show_navigation_failure(
                query,
                ui_state=ui_state,
                user_id=user_id,
                text="Couldn't open that view. Try again or go back to Bot Status.",
                retry_action="runtime_status_open",
                retry_payload={"target": target, "back_target": back_target},
                back_target="status",
            )
            return
        return

    if action == "last_turn_page":
        await query.answer()
        try:
            await _show_last_turn_from_callback(
                query,
                services,
                ui_state,
                user_id=user_id,
                page=int(payload.get("page", 0)),
                back_target=str(payload.get("back_target", "none")),
            )
        except Exception:
            await _show_navigation_failure(
                query,
                ui_state=ui_state,
                user_id=user_id,
                text="Couldn't load the last turn. Try again or go back.",
                retry_action="last_turn_page",
                retry_payload={
                    "page": int(payload.get("page", 0)),
                    "back_target": str(payload.get("back_target", "none")),
                },
                back_target=str(payload.get("back_target", "none")),
            )
        return

    if action == "last_turn_open":
        await query.answer()
        try:
            await _show_last_turn_item_from_callback(
                query,
                services,
                ui_state,
                user_id=user_id,
                page=int(payload.get("page", 0)),
                item_index=int(payload.get("item_index", -1)),
                back_target=str(payload.get("back_target", "none")),
            )
        except Exception:
            await _show_navigation_failure(
                query,
                ui_state=ui_state,
                user_id=user_id,
                text="Couldn't load that replay item. Try again or go back.",
                retry_action="last_turn_open",
                retry_payload={
                    "page": int(payload.get("page", 0)),
                    "item_index": int(payload.get("item_index", -1)),
                    "back_target": str(payload.get("back_target", "none")),
                },
                back_target=str(payload.get("back_target", "none")),
            )
        return

    if action == "plan_page":
        await query.answer()
        try:
            await _show_plan_from_callback(
                query,
                services,
                ui_state,
                user_id=user_id,
                page=int(payload.get("page", 0)),
                back_target=str(payload.get("back_target", "none")),
            )
        except Exception:
            await _show_navigation_failure(
                query,
                ui_state=ui_state,
                user_id=user_id,
                text="Couldn't load the agent plan. Try again or go back.",
                retry_action="plan_page",
                retry_payload={
                    "page": int(payload.get("page", 0)),
                    "back_target": str(payload.get("back_target", "none")),
                },
                back_target=str(payload.get("back_target", "none")),
            )
        return

    if action == "plan_open":
        await query.answer()
        try:
            await _show_plan_detail_from_callback(
                query,
                services,
                ui_state,
                user_id=user_id,
                page=int(payload.get("page", 0)),
                plan_index=int(payload.get("plan_index", -1)),
                back_target=str(payload.get("back_target", "none")),
            )
        except Exception:
            await _show_navigation_failure(
                query,
                ui_state=ui_state,
                user_id=user_id,
                text="Couldn't load that plan entry. Try again or go back.",
                retry_action="plan_open",
                retry_payload={
                    "page": int(payload.get("page", 0)),
                    "plan_index": int(payload.get("plan_index", -1)),
                    "back_target": str(payload.get("back_target", "none")),
                },
                back_target=str(payload.get("back_target", "none")),
            )
        return

    if action == "tool_activity_page":
        await query.answer()
        try:
            await _show_tool_activity_from_callback(
                query,
                services,
                ui_state,
                user_id=user_id,
                page=int(payload.get("page", 0)),
                back_target=str(payload.get("back_target", "none")),
            )
        except Exception:
            await _show_navigation_failure(
                query,
                ui_state=ui_state,
                user_id=user_id,
                text="Couldn't load tool activity. Try again or go back.",
                retry_action="tool_activity_page",
                retry_payload={
                    "page": int(payload.get("page", 0)),
                    "back_target": str(payload.get("back_target", "none")),
                },
                back_target=str(payload.get("back_target", "none")),
            )
        return

    if action == "tool_activity_open":
        await query.answer()
        try:
            await _show_tool_activity_detail_from_callback(
                query,
                services,
                ui_state,
                user_id=user_id,
                page=int(payload.get("page", 0)),
                activity_index=int(payload.get("activity_index", -1)),
                back_target=str(payload.get("back_target", "none")),
            )
        except Exception:
            await _show_navigation_failure(
                query,
                ui_state=ui_state,
                user_id=user_id,
                text="Couldn't load that tool activity entry. Try again or go back.",
                retry_action="tool_activity_open",
                retry_payload={
                    "page": int(payload.get("page", 0)),
                    "activity_index": int(payload.get("activity_index", -1)),
                    "back_target": str(payload.get("back_target", "none")),
                },
                back_target=str(payload.get("back_target", "none")),
            )
        return

    if action == "tool_activity_open_file":
        await query.answer()
        try:
            await _show_tool_activity_file_preview_from_callback(
                query,
                services,
                ui_state,
                user_id=user_id,
                relative_path=str(payload["relative_path"]),
                page=int(payload.get("page", 0)),
                activity_index=int(payload.get("activity_index", -1)),
                back_target=str(payload.get("back_target", "none")),
            )
        except Exception:
            await _show_navigation_failure(
                query,
                ui_state=ui_state,
                user_id=user_id,
                text="Couldn't load that related file. Try again or go back.",
                retry_action="tool_activity_open_file",
                retry_payload={
                    "relative_path": str(payload["relative_path"]),
                    "page": int(payload.get("page", 0)),
                    "activity_index": int(payload.get("activity_index", -1)),
                    "back_target": str(payload.get("back_target", "none")),
                },
                back_target=str(payload.get("back_target", "none")),
            )
        return

    if action == "tool_activity_open_change":
        await query.answer()
        try:
            await _show_tool_activity_change_preview_from_callback(
                query,
                services,
                ui_state,
                user_id=user_id,
                relative_path=str(payload["relative_path"]),
                status_code=str(payload["status_code"]),
                page=int(payload.get("page", 0)),
                activity_index=int(payload.get("activity_index", -1)),
                back_target=str(payload.get("back_target", "none")),
            )
        except Exception:
            await _show_navigation_failure(
                query,
                ui_state=ui_state,
                user_id=user_id,
                text="Couldn't load that related change. Try again or go back.",
                retry_action="tool_activity_open_change",
                retry_payload={
                    "relative_path": str(payload["relative_path"]),
                    "status_code": str(payload["status_code"]),
                    "page": int(payload.get("page", 0)),
                    "activity_index": int(payload.get("activity_index", -1)),
                    "back_target": str(payload.get("back_target", "none")),
                },
                back_target=str(payload.get("back_target", "none")),
            )
        return

    if action == "workspace_runtime_open_server":
        await query.answer()
        try:
            await _show_workspace_runtime_server_from_callback(
                query,
                services,
                ui_state,
                user_id=user_id,
                server_index=int(payload.get("server_index", -1)),
                back_target=str(payload.get("back_target", "none")),
            )
        except Exception:
            await _show_navigation_failure(
                query,
                ui_state=ui_state,
                user_id=user_id,
                text="Couldn't load MCP server details. Try again or go back.",
                retry_action="workspace_runtime_open_server",
                retry_payload={
                    "server_index": int(payload.get("server_index", -1)),
                    "back_target": str(payload.get("back_target", "none")),
                },
                back_target=str(payload.get("back_target", "none")),
            )
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

            async def _on_retry_missing_replay_turn() -> None:
                await _show_runtime_status_from_callback(
                    query,
                    services,
                    ui_state,
                    user_id=user_id,
                    notice=_no_previous_turn_text(),
                )

            async def _on_retry_prepare_failure() -> None:
                await _show_runtime_status_from_callback(
                    query,
                    services,
                    ui_state,
                    user_id=user_id,
                    notice=_request_failed_text(),
                )

            async def _on_retry_turn_failure() -> None:
                await _show_runtime_status_from_callback(
                    query,
                    services,
                    ui_state,
                    user_id=user_id,
                    notice=_request_failed_text(),
                )

            await _retry_last_turn(
                update,
                services,
                ui_state,
                application=application,
                after_turn_success=_after_retry_success,
                on_missing_replay_turn=_on_retry_missing_replay_turn,
                on_prepare_failure=_on_retry_prepare_failure,
                on_turn_failure=_on_retry_turn_failure,
            )
            return
        if target == "run_last_request":
            state = await services.snapshot_runtime_state()
            last_request = ui_state.get_last_request(user_id, state.workspace_id)
            if last_request is None:
                await _show_runtime_status_from_callback(
                    query,
                    services,
                    ui_state,
                    user_id=user_id,
                    notice=_no_previous_request_text(),
                )
                return
            after_turn_success, on_prepare_failure, on_turn_failure = _status_turn_callbacks(
                query,
                services,
                ui_state,
                user_id=user_id,
                success_notice="Ran the last request.",
            )
            await _run_last_request_on_message(
                query.message,
                user_id,
                services,
                ui_state,
                last_request=last_request,
                provider=state.provider,
                workspace_id=state.workspace_id,
                application=application,
                after_turn_success=after_turn_success,
                on_prepare_failure=on_prepare_failure,
                on_turn_failure=on_turn_failure,
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

            async def _on_fork_missing_replay_turn() -> None:
                await _show_runtime_status_from_callback(
                    query,
                    services,
                    ui_state,
                    user_id=user_id,
                    notice=_no_previous_turn_text(),
                )

            async def _on_fork_session_creation_failed() -> None:
                await _show_runtime_status_from_callback(
                    query,
                    services,
                    ui_state,
                    user_id=user_id,
                    notice=_session_creation_failed_text(),
                )

            async def _on_fork_turn_failure() -> None:
                await _show_runtime_status_from_callback(
                    query,
                    services,
                    ui_state,
                    user_id=user_id,
                    notice=_request_failed_text(),
                )

            await _fork_last_turn(
                update,
                services,
                ui_state,
                application=application,
                after_turn_success=_after_fork_success,
                on_missing_replay_turn=_on_fork_missing_replay_turn,
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
                    _pending_input_cancel_notice(
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
                    notice=_no_previous_request_text(),
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
                    notice=_context_bundle_empty_text(),
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
                _pending_input_cancel_notice(
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
                    notice=_context_bundle_empty_text(),
                )
                return
            last_request_text = ui_state.get_last_request_text(user_id, state.workspace_id)
            if last_request_text is None:
                await _show_runtime_status_from_callback(
                    query,
                    services,
                    ui_state,
                    user_id=user_id,
                    notice=_no_previous_request_text(),
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
        if target == "fork_session":
            await _fork_live_session_from_callback(
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
                await _show_navigation_failure(
                    query,
                    ui_state=ui_state,
                    user_id=user_id,
                    text="Couldn't load Model / Mode. Try again or go back to Bot Status.",
                    retry_action="runtime_status_control",
                    retry_payload={"target": "model_mode"},
                    back_target="status",
                )
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
                await _show_navigation_failure(
                    query,
                    ui_state=ui_state,
                    user_id=user_id,
                    text="Couldn't load Switch Agent. Try again or go back to Bot Status.",
                    retry_action="runtime_status_control",
                    retry_payload={"target": "switch_agent"},
                    back_target="status",
                )
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
                await _show_navigation_failure(
                    query,
                    ui_state=ui_state,
                    user_id=user_id,
                    text="Couldn't load Switch Workspace. Try again or go back to Bot Status.",
                    retry_action="runtime_status_control",
                    retry_payload={"target": "switch_workspace"},
                    back_target="status",
                )
            return
        await query.answer(_unknown_action_text(), show_alert=True)
        return

    if action == "runtime_status_stop_turn":
        await query.answer()
        notice: str | None = None
        try:
            state = await services.snapshot_runtime_state()
            active_turn = ui_state.get_active_turn(
                user_id,
                provider=state.provider,
                workspace_id=state.workspace_id,
            )
            if active_turn is None:
                notice = "No active turn to stop."
            else:
                await _request_stop_active_turn(
                    ui_state,
                    user_id=user_id,
                    active_turn=active_turn,
                )
                notice = "Stop requested for the current turn."
            await _show_runtime_status_from_callback(
                query,
                services,
                ui_state,
                user_id=user_id,
                notice=notice,
            )
        except Exception:
            await _edit_query_message(
                query,
                _runtime_status_refresh_degraded_notice(notice)
                if notice is not None
                else _stop_turn_failed_text(),
            )
        return

    if action == "runtime_status_cancel_pending":
        await query.answer()
        cleared = ui_state.clear_pending_text_action(user_id)
        notice = "Pending input cancelled." if cleared is not None else "No pending input to cancel."
        try:
            await _show_runtime_status_from_callback(
                query,
                services,
                ui_state,
                user_id=user_id,
                notice=notice,
            )
        except Exception:
            await _edit_query_message(query, _runtime_status_refresh_degraded_notice(notice))
        return

    if action == "runtime_status_discard_pending_uploads":
        await query.answer()
        cleared = ui_state.cancel_pending_media_groups(user_id)
        notice = (
            _pending_media_group_cancelled_text(cleared)
            if cleared is not None
            else "No pending uploads to discard."
        )
        try:
            await _show_runtime_status_from_callback(
                query,
                services,
                ui_state,
                user_id=user_id,
                notice=notice,
            )
        except Exception:
            await _edit_query_message(query, _runtime_status_refresh_degraded_notice(notice))
        return

    if action == "runtime_status_command_cancel":
        await query.answer()
        ui_state.clear_pending_text_action(user_id)
        notice = "Command input cancelled."
        try:
            await _show_runtime_status_from_callback(
                query,
                services,
                ui_state,
                user_id=user_id,
                notice=notice,
            )
        except Exception:
            await _edit_query_message(query, _runtime_status_refresh_degraded_notice(notice))
        return

    if action == "runtime_status_start_bundle_chat":
        await query.answer()
        notice: str | None = None
        try:
            state = await services.snapshot_runtime_state()
            bundle = ui_state.get_context_bundle(user_id, state.provider, state.workspace_id)
            if bundle is None or not bundle.items:
                notice = _context_bundle_empty_text()
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
            await _edit_query_message(
                query,
                _runtime_status_refresh_degraded_notice(notice)
                if notice is not None
                else _bundle_chat_update_failed_text(),
            )
        return

    if action == "runtime_status_stop_bundle_chat":
        await query.answer()
        notice: str | None = None
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
            await _edit_query_message(
                query,
                _runtime_status_refresh_degraded_notice(notice)
                if notice is not None
                else _bundle_chat_update_failed_text(),
            )
        return

    if action == "runtime_status_search_cancel":
        await query.answer()
        ui_state.clear_pending_text_action(user_id)
        notice = "Search cancelled."
        try:
            await _show_runtime_status_from_callback(
                query,
                services,
                ui_state,
                user_id=user_id,
                notice=notice,
            )
        except Exception:
            await _edit_query_message(query, _runtime_status_refresh_degraded_notice(notice))
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
            await query.answer(_unauthorized_text(), show_alert=True)
            return
        workspace = services.config.agent.resolve_workspace(payload["workspace_id"])
        pending_upload_notice = _discard_pending_uploads_for_transition(ui_state, user_id)
        await query.answer()
        await _edit_query_message(query, f"Switching workspace to {workspace.label}...")
        try:
            await asyncio.wait_for(
                services.switch_workspace(workspace.id),
                CALLBACK_OPERATION_TIMEOUT_SECONDS,
            )
        except Exception:
            try:
                await _show_switch_workspace_menu_from_callback(
                    query,
                    services,
                    ui_state,
                    user_id=user_id,
                    back_target=str(payload.get("back_target", "none")),
                    notice=_prefixed_notice_text(
                        pending_upload_notice,
                        _switch_workspace_failed_text(),
                    ),
                )
            except Exception:
                await _edit_query_message(
                    query,
                    _prefixed_notice_text(
                        pending_upload_notice,
                        _switch_workspace_failed_text(),
                    ),
                )
            return
        ui_state.invalidate_runtime_bound_interactions()
        state = await services.snapshot_runtime_state()
        success_text = _prefixed_notice_text(
            pending_upload_notice,
            (
                f"Switched workspace to {workspace.label} on "
                f"{resolve_provider_profile(state.provider).display_name}. "
                "Old bot buttons and pending inputs were cleared.\n"
                f"{_switch_workspace_success_detail_text()}"
            ),
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
        try:
            await _show_switch_workspace_menu_from_callback(
                query,
                services,
                ui_state,
                user_id=user_id,
                back_target=str(payload.get("back_target", "none")),
                notice=success_text,
            )
        except Exception:
            await _edit_query_message(query, success_text)
        return

    if action == "history_page":
        await query.answer()
        state, history_state = await _with_active_store(
            services,
            lambda store: _load_history_view_state(store, user_id),
        )
        can_fork = await _resolve_runtime_session_fork_support(
            services,
            state=state,
            active_session_id=history_state.active_session_id,
            active_session_can_fork=history_state.active_session_can_fork,
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
            can_fork=can_fork,
            show_provider_sessions=user_id == services.admin_user_id,
            back_target=str(payload.get("back_target", "none")),
        )
        await _edit_query_message(query, text, reply_markup=markup)
        return

    if action == "history_open":
        await query.answer()
        try:
            await _show_history_entry_from_callback(
                query,
                services,
                ui_state,
                user_id=user_id,
                session_id=str(payload["session_id"]),
                page=int(payload.get("page", 0)),
                back_target=str(payload.get("back_target", "none")),
            )
        except Exception:
            await _show_navigation_failure(
                query,
                ui_state=ui_state,
                user_id=user_id,
                text="Couldn't load that session history entry. Try again or go back.",
                retry_action="history_open",
                retry_payload={
                    "session_id": str(payload["session_id"]),
                    "page": int(payload.get("page", 0)),
                    "back_target": str(payload.get("back_target", "none")),
                },
                back_label="Back to History",
                back_action="history_page",
                back_payload={
                    "page": int(payload.get("page", 0)),
                    "back_target": str(payload.get("back_target", "none")),
                },
            )
        return

    if action == "history_provider_sessions":
        if query.from_user is None or query.from_user.id != services.admin_user_id:
            await query.answer(_unauthorized_text(), show_alert=True)
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
            back_target = str(payload.get("back_target", "history"))
            await _show_navigation_failure(
                query,
                ui_state=ui_state,
                user_id=user_id,
                text="Couldn't load Provider Sessions. Try again or go back.",
                retry_action="history_provider_sessions",
                retry_payload={
                    "cursor": payload.get("cursor"),
                    "previous_cursors": tuple(payload.get("previous_cursors", ())),
                    "history_page": int(payload.get("history_page", 0)),
                    "back_target": back_target,
                    "history_back_target": str(payload.get("history_back_target", "none")),
                },
                back_label="Back to Bot Status" if back_target == "status" else "Back to History",
                back_action="runtime_status_page" if back_target == "status" else "history_page",
                back_payload=(
                    {}
                    if back_target == "status"
                    else {
                        "page": int(payload.get("history_page", 0)),
                        "back_target": str(payload.get("history_back_target", "none")),
                    }
                ),
            )
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

    if action == "history_fork":
        await _fork_history_session_from_callback(
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

    if action == "history_fork_retry_last_turn":
        await _fork_history_session_from_callback(
            query,
            services,
            ui_state,
            user_id=user_id,
            session_id=payload["session_id"],
            application=application,
            replay_after_fork=True,
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
            notice = _delete_session_failed_text()
        can_fork = await _resolve_runtime_session_fork_support(
            services,
            state=state,
            active_session_id=history_state.active_session_id,
            active_session_can_fork=history_state.active_session_can_fork,
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
            can_fork=can_fork,
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
            _pending_input_cancel_notice(
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
        can_fork = await _resolve_runtime_session_fork_support(
            services,
            state=state,
            active_session_id=history_state.active_session_id,
            active_session_can_fork=history_state.active_session_can_fork,
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
            can_fork=can_fork,
            notice="Rename cancelled.",
            show_provider_sessions=user_id == services.admin_user_id,
            back_target=str(payload.get("back_target", "none")),
        )
        await _edit_query_message(query, text, reply_markup=markup)
        return

    if action == "provider_sessions_page":
        if query.from_user is None or query.from_user.id != services.admin_user_id:
            await query.answer(_unauthorized_text(), show_alert=True)
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
            back_target = str(payload.get("back_target", "history"))
            await _show_navigation_failure(
                query,
                ui_state=ui_state,
                user_id=user_id,
                text="Couldn't load Provider Sessions. Try again or go back.",
                retry_action="provider_sessions_page",
                retry_payload={
                    "cursor": payload.get("cursor"),
                    "previous_cursors": tuple(payload.get("previous_cursors", ())),
                    "history_page": int(payload.get("history_page", 0)),
                    "back_target": back_target,
                    "history_back_target": str(payload.get("history_back_target", "none")),
                },
                back_label="Back to Bot Status" if back_target == "status" else "Back to History",
                back_action="runtime_status_page" if back_target == "status" else "history_page",
                back_payload=(
                    {}
                    if back_target == "status"
                    else {
                        "page": int(payload.get("history_page", 0)),
                        "back_target": str(payload.get("history_back_target", "none")),
                    }
                ),
            )
        return

    if action == "provider_session_open":
        if query.from_user is None or query.from_user.id != services.admin_user_id:
            await query.answer(_unauthorized_text(), show_alert=True)
            return
        await query.answer()
        try:
            await _show_provider_session_detail_from_callback(
                query,
                services,
                ui_state,
                user_id=user_id,
                session_id=str(payload["session_id"]),
                cursor=payload.get("cursor"),
                previous_cursors=tuple(payload.get("previous_cursors", ())),
                history_page=int(payload.get("history_page", 0)),
                back_target=str(payload.get("back_target", "history")),
                history_back_target=str(payload.get("history_back_target", "none")),
            )
        except Exception:
            await _show_navigation_failure(
                query,
                ui_state=ui_state,
                user_id=user_id,
                text="Couldn't load that provider session. Try again or go back.",
                retry_action="provider_session_open",
                retry_payload={
                    "session_id": str(payload["session_id"]),
                    "cursor": payload.get("cursor"),
                    "previous_cursors": tuple(payload.get("previous_cursors", ())),
                    "history_page": int(payload.get("history_page", 0)),
                    "back_target": str(payload.get("back_target", "history")),
                    "history_back_target": str(payload.get("history_back_target", "none")),
                },
                back_label="Back to Provider Sessions",
                back_action="provider_sessions_page",
                back_payload={
                    "cursor": payload.get("cursor"),
                    "previous_cursors": tuple(payload.get("previous_cursors", ())),
                    "history_page": int(payload.get("history_page", 0)),
                    "back_target": str(payload.get("back_target", "history")),
                    "history_back_target": str(payload.get("history_back_target", "none")),
                },
            )
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

    if action == "provider_session_fork":
        await _fork_provider_session_from_callback(
            query,
            services,
            ui_state,
            user_id=user_id,
            payload=payload,
            application=application,
        )
        return

    if action == "provider_session_fork_retry_last_turn":
        await _fork_provider_session_from_callback(
            query,
            services,
            ui_state,
            user_id=user_id,
            payload=payload,
            application=application,
            replay_after_fork=True,
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

    if action == "agent_command_open":
        await query.answer()
        try:
            await _show_agent_command_detail_from_callback(
                query,
                services,
                ui_state,
                user_id=user_id,
                page=int(payload.get("page", 0)),
                command_index=int(payload.get("command_index", -1)),
                back_target=str(payload.get("back_target", "none")),
            )
        except Exception:
            await _show_navigation_failure(
                query,
                ui_state=ui_state,
                user_id=user_id,
                text="Couldn't load that agent command. Try again or go back.",
                retry_action="agent_command_open",
                retry_payload={
                    "page": int(payload.get("page", 0)),
                    "command_index": int(payload.get("command_index", -1)),
                    "back_target": str(payload.get("back_target", "none")),
                },
                back_label="Back to Agent Commands",
                back_action="agent_commands_page",
                back_payload={
                    "page": int(payload.get("page", 0)),
                    "back_target": str(payload.get("back_target", "none")),
                },
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
                _pending_input_cancel_notice(
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

    if action == "model_mode_page":
        await query.answer()
        try:
            await _show_model_mode_menu_from_callback(
                query,
                services,
                ui_state,
                user_id=user_id,
                application=application,
                back_target=str(payload.get("back_target", "none")),
            )
        except Exception:
            await _show_navigation_failure(
                query,
                ui_state=ui_state,
                user_id=user_id,
                text="Couldn't load Model / Mode. Try again or go back.",
                retry_action="model_mode_page",
                retry_payload={"back_target": str(payload.get("back_target", "none"))},
                back_target=str(payload.get("back_target", "none")),
            )
        return

    if action == "selection_open":
        await query.answer()
        try:
            await _show_selection_detail_from_callback(
                query,
                services,
                ui_state,
                user_id=user_id,
                kind=str(payload["kind"]),
                value=str(payload["value"]),
                application=application,
                back_target=str(payload.get("back_target", "none")),
            )
        except Exception:
            await _show_navigation_failure(
                query,
                ui_state=ui_state,
                user_id=user_id,
                text="Couldn't load selection details. Try again or go back.",
                retry_action="selection_open",
                retry_payload={
                    "kind": str(payload["kind"]),
                    "value": str(payload["value"]),
                    "back_target": str(payload.get("back_target", "none")),
                },
                back_label="Back to Model / Mode",
                back_action="model_mode_page",
                back_payload={"back_target": str(payload.get("back_target", "none"))},
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
                notice=_no_previous_request_text(),
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
                notice=_no_previous_request_text(),
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
        await _edit_query_message(
            query,
            _workspace_search_cancelled_text(),
            reply_markup=_workspace_search_cancelled_markup(ui_state, user_id),
        )
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
                    notice=_no_previous_request_text(),
                )
            else:
                await _show_workspace_changes_from_callback(
                    query,
                    services,
                    ui_state,
                    user_id=user_id,
                    page=page,
                    back_target=back_target,
                    notice=_no_previous_request_text(),
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
                    notice=_request_failed_text(),
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
            _pending_input_cancel_notice(
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
            await query.answer(_no_previous_request_text(), show_alert=True)
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
            _pending_input_cancel_notice(
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
            await query.answer(_no_previous_request_text(), show_alert=True)
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
        if payload.get("source") == "tool_activity":
            await _show_tool_activity_change_preview_from_callback(
                query,
                services,
                ui_state,
                user_id=user_id,
                relative_path=payload["relative_path"],
                status_code=payload["status_code"],
                page=int(payload.get("page", 0)),
                activity_index=int(payload.get("activity_index", -1)),
                back_target=str(payload.get("back_target", "none")),
            )
            return
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
            await query.answer(_context_bundle_empty_text(), show_alert=True)
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
            await query.answer(_context_bundle_empty_text(), show_alert=True)
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
            await query.answer(_context_bundle_empty_text(), show_alert=True)
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
            _pending_input_cancel_notice(
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
                notice=_context_bundle_empty_text(),
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
                notice=_no_previous_request_text(),
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
        if payload.get("source") == "tool_activity":
            await _show_tool_activity_file_preview_from_callback(
                query,
                services,
                ui_state,
                user_id=user_id,
                relative_path=payload["relative_path"],
                page=int(payload.get("page", 0)),
                activity_index=int(payload.get("activity_index", -1)),
                back_target=str(payload.get("back_target", "none")),
            )
            return
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

    await query.answer(_unknown_action_text(), show_alert=True)


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
    active_turn = ui_state.get_active_turn(
        user_id,
        provider=provider,
        workspace_id=workspace_id,
    )
    pending_text_action = ui_state.get_pending_text_action(user_id)
    last_turn = ui_state.get_last_turn(user_id, provider, workspace_id)
    last_turn_available = last_turn is not None
    last_request = ui_state.get_last_request(user_id, workspace_id)
    last_request_text = None if last_request is None else last_request.text
    pending_media_group_stats = ui_state.pending_media_group_stats(user_id)
    bundle = ui_state.get_context_bundle(user_id, provider, workspace_id)
    bundle_count = 0 if bundle is None else len(bundle.items)
    bundle_chat_active = ui_state.context_bundle_chat_active(user_id, provider, workspace_id)
    workspace_reuse_summary = _workspace_reuse_summary_line(
        ui_state=ui_state,
        user_id=user_id,
        provider=provider,
        workspace_id=workspace_id,
    )
    workspace_changes_available = _status_workspace_changes_available(git_status)
    current_time = ui_state.current_time()

    lines = []
    if notice:
        lines.append(notice)
    lines.append(
        f"Bot status for {resolve_provider_profile(provider).display_name} in {workspace_label}"
    )
    lines.append(f"Workspace ID: {workspace_id}")
    lines.append(f"Path: {workspace_path}")
    lines.append(
        _interaction_status_line(
            session=session,
            active_turn=active_turn,
            pending_text_action=pending_text_action,
            pending_media_group_stats=pending_media_group_stats,
            bundle_count=bundle_count,
            bundle_chat_active=bundle_chat_active,
        )
    )
    lines.append(
        _recommended_next_step_line(
            session=session,
            active_turn=active_turn,
            pending_text_action=pending_text_action,
            pending_media_group_stats=pending_media_group_stats,
            bundle_count=bundle_count,
            bundle_chat_active=bundle_chat_active,
            last_request_available=last_request_text is not None,
            last_turn_available=last_turn_available,
        )
    )
    lines.append(
        _primary_controls_line(
            session=session,
            active_turn=active_turn,
            pending_text_action=pending_text_action,
            pending_media_group_stats=pending_media_group_stats,
            bundle_count=bundle_count,
            bundle_chat_active=bundle_chat_active,
            last_request_available=last_request_text is not None,
            last_turn_available=last_turn_available,
        )
    )
    lines.append("")

    runtime_lines: list[str] = []
    if session is None:
        runtime_lines.append("Session: none (will start on first request)")
    else:
        runtime_lines.append(f"Session: {session.session_id or 'pending'}")
        if session_title is not None:
            runtime_lines.append(f"Session title: {session_title}")
    runtime_lines.extend(_status_active_turn_lines(active_turn, now=current_time))

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
            runtime_lines.append(f"Model: {_current_choice_label(model_selection)}")
        if mode_selection is not None:
            runtime_lines.append(f"Mode: {_current_choice_label(mode_selection)}")
    usage_summary = _status_usage_summary(session)
    if usage_summary is not None:
        runtime_lines.append(f"Usage: {usage_summary}")
    runtime_lines.extend(_status_plan_preview_lines(session))
    plan_count = len(_plan_items(session))
    runtime_lines.extend(_status_tool_activity_preview_lines(session))
    tool_activity_count = len(_tool_activity_items(session))

    memory_lines = [f"Pending input: {_pending_text_action_label(pending_text_action)}"]
    pending_text_hint = _pending_text_action_hint_line(pending_text_action)
    if pending_text_hint is not None:
        memory_lines.append(pending_text_hint)
    if pending_media_group_stats is not None:
        memory_lines.append(f"Pending uploads: {_pending_media_group_summary(pending_media_group_stats)}")
    memory_lines.append(f"Local sessions: {history_count}")
    recent_history_entries, recent_history_total = _status_recent_history_entries(
        history_entries,
        current_session_id=None if session is None else session.session_id,
    )
    memory_lines.extend(
        _status_recent_session_preview_lines(
            recent_history_entries,
            total_count=recent_history_total,
        )
    )
    if last_turn is None:
        memory_lines.append("Last turn replay: none")
    else:
        replay_snippet = _status_text_snippet(last_turn.title_hint) or "untitled turn"
        memory_lines.append(f"Last turn replay: available ({replay_snippet})")
        if last_turn.provider != provider:
            memory_lines.append(
                "Last turn replay note: "
                + _last_turn_replay_note(
                    replay_turn=last_turn,
                    current_provider=provider,
                )
            )
    if last_request_text is None:
        memory_lines.append("Last request text: none")
    else:
        memory_lines.append(
            f"Last request text: {_status_text_snippet(last_request_text) or '[empty]'}"
        )
        memory_lines.append(f"Last request source: {_last_request_source_summary(last_request)}")
        if last_request is not None:
            recorded_request_provider = _last_request_recorded_provider(
                last_request,
                current_provider=provider,
            )
            if recorded_request_provider != provider:
                memory_lines.append(
                    "Last request replay note: "
                    + _last_request_replay_note(
                        last_request=last_request,
                        current_provider=provider,
                    )
                )

    workspace_lines = [
        f"Workspace changes: {_status_workspace_changes_summary(git_status)}",
        *_status_workspace_change_preview_lines(git_status),
        f"Context bundle: {bundle_count} item{'s' if bundle_count != 1 else ''}",
        f"Bundle chat: {'on' if bundle_chat_active else 'off'}",
        *_status_context_bundle_preview_lines(bundle),
    ]

    capability_lines: list[str] = []
    if session is None:
        capability_lines.append("Agent commands cached: unknown until a live session starts.")
    elif session.session_id is None:
        capability_lines.append("Agent commands cached: waiting for session start.")
    else:
        cached_commands = tuple(getattr(session, "available_commands", ()) or ())
        capability_lines.append(f"Agent commands cached: {len(cached_commands)}")
        capability_lines.extend(_status_agent_command_preview_lines(cached_commands))
        capabilities = getattr(session, "capabilities", None)
        if capabilities is not None:
            capability_lines.append(
                "Prompt input: "
                f"img={'yes' if getattr(capabilities, 'supports_image_prompt', False) else 'no'},"
                f"audio={'yes' if getattr(capabilities, 'supports_audio_prompt', False) else 'no'},"
                f"docs={'yes' if getattr(capabilities, 'supports_embedded_context_prompt', False) else 'no'}"
            )
            capability_lines.append(
                "Session control: "
                f"fork={'yes' if getattr(capabilities, 'can_fork', False) else 'no'},"
                f"list={'yes' if getattr(capabilities, 'can_list', False) else 'no'},"
                f"resume={'yes' if getattr(capabilities, 'can_resume', False) else 'no'}"
            )

    lines.append("")
    lines.append("Current runtime:")
    lines.extend(runtime_lines)
    lines.append("")
    lines.append("Resume and memory:")
    lines.extend(memory_lines)
    lines.append("")
    lines.append("Workspace context:")
    lines.extend(workspace_lines)
    lines.append("")
    lines.append("Agent capabilities:")
    lines.extend(capability_lines)
    lines.append("")
    lines.append("Controls:")
    lines.append(
        "Control center: use the buttons below for session recovery, history, files, changes, "
        "model / mode, agent commands, and workspace actions."
    )
    lines.append(
        "Main keyboard: keep high-frequency actions ready without filling the whole chat."
    )
    if workspace_reuse_summary is not None:
        lines.append(workspace_reuse_summary)
    if is_admin:
        lines.append("Admin switches stay on the main keyboard.")

    buttons = []
    primary_buttons = []
    primary_action_kind = "none"
    if active_turn is not None:
        primary_action_kind = "active_turn"
        primary_buttons.append(
            _callback_button(ui_state, user_id, "Stop Turn", "runtime_status_stop_turn")
        )
    elif pending_text_action is not None:
        primary_action_kind = "pending_input"
        primary_buttons.append(
            _callback_button(ui_state, user_id, "Cancel Pending Input", "runtime_status_cancel_pending")
        )
    elif pending_media_group_stats is not None:
        primary_action_kind = "pending_uploads"
        primary_buttons.append(
            _callback_button(
                ui_state,
                user_id,
                "Discard Pending Uploads",
                "runtime_status_discard_pending_uploads",
            )
        )
    elif bundle_count > 0:
        primary_action_kind = "bundle"
        primary_buttons.append(
            _callback_button(
                ui_state,
                user_id,
                "Ask Agent With Context",
                "runtime_status_control",
                target="context_bundle_ask",
            )
        )
        if last_request_text is not None:
            primary_buttons.append(
                _callback_button(
                    ui_state,
                    user_id,
                    "Bundle + Last Request",
                    "runtime_status_control",
                    target="context_bundle_ask_last_request",
                )
            )
        else:
            primary_buttons.append(
                _callback_button(
                    ui_state,
                    user_id,
                    "Context Bundle",
                    "runtime_status_open",
                    target="bundle",
                )
            )
    elif last_request is not None:
        primary_action_kind = "last_request"
        primary_buttons.extend(
            [
                _callback_button(
                    ui_state,
                    user_id,
                    "Run Last Request",
                    "runtime_status_control",
                    target="run_last_request",
                ),
                _callback_button(
                    ui_state,
                    user_id,
                    "Last Request",
                    "runtime_status_open",
                    target="last_request",
                ),
            ]
        )
    elif last_turn_available:
        primary_action_kind = "last_turn"
        primary_buttons.extend(
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
    if primary_buttons:
        buttons.append(primary_buttons)
    status_nav_row = [
        _callback_button(ui_state, user_id, "Refresh", "runtime_status_page"),
        _callback_button(ui_state, user_id, "Session History", "runtime_status_open", target="history"),
    ]
    if is_admin:
        status_nav_row.append(
            _callback_button(
                ui_state,
                user_id,
                "Provider Sessions",
                "runtime_status_open",
                target="provider_sessions",
            )
        )
    buttons.append(status_nav_row)
    buttons.extend(
        _status_recent_session_quick_buttons(
            ui_state,
            user_id=user_id,
            entries=recent_history_entries,
            can_retry_last_turn=last_turn_available,
        )
    )
    control_buttons = []
    if active_turn is not None and primary_action_kind != "active_turn":
        control_buttons.append(
            _callback_button(ui_state, user_id, "Stop Turn", "runtime_status_stop_turn")
        )
    if pending_text_action is not None and primary_action_kind != "pending_input":
        control_buttons.append(
            _callback_button(ui_state, user_id, "Cancel Pending Input", "runtime_status_cancel_pending")
        )
    if pending_media_group_stats is not None and primary_action_kind != "pending_uploads":
        control_buttons.append(
            _callback_button(
                ui_state,
                user_id,
                "Discard Pending Uploads",
                "runtime_status_discard_pending_uploads",
            )
        )
    if bundle_count > 0:
        if bundle_chat_active:
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
    if (
        session is not None
        and getattr(session, "session_id", None) is not None
        and bool(getattr(getattr(session, "capabilities", None), "can_fork", False))
    ):
        buttons.append(
            [
                _callback_button(
                    ui_state,
                    user_id,
                    "Fork Session",
                    "runtime_status_control",
                    target="fork_session",
                )
            ]
        )
    if last_turn_available and primary_action_kind != "last_turn":
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
    if last_turn_available:
        buttons.append(
            [
                _callback_button(
                    ui_state,
                    user_id,
                    "Last Turn",
                    "runtime_status_open",
                    target="last_turn",
                )
            ]
        )
    buttons.append(
        [
            _callback_button(
                ui_state,
                user_id,
                "Session Info",
                "runtime_status_open",
                target="session_info",
            ),
            _callback_button(
                ui_state,
                user_id,
                "Model / Mode",
                "runtime_status_control",
                target="model_mode",
            )
        ]
    )
    buttons.append(
        [
            _callback_button(
                ui_state,
                user_id,
                "Workspace Runtime",
                "runtime_status_open",
                target="workspace_runtime",
            )
        ]
    )
    if usage_summary is not None:
        buttons.append(
            [
                _callback_button(
                    ui_state,
                    user_id,
                    "Usage",
                    "runtime_status_open",
                    target="usage",
                )
            ]
        )
    if last_request is not None and primary_action_kind != "last_request":
        buttons.append(
            [
                _callback_button(
                    ui_state,
                    user_id,
                    "Run Last Request",
                    "runtime_status_control",
                    target="run_last_request",
                ),
                _callback_button(
                    ui_state,
                    user_id,
                    "Last Request",
                    "runtime_status_open",
                    target="last_request",
                ),
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
    if plan_count > 0:
        buttons.append(
            [
                _callback_button(
                    ui_state,
                    user_id,
                    "Agent Plan",
                    "runtime_status_open",
                    target="plan",
                )
            ]
        )
    if tool_activity_count > 0:
        buttons.append(
            [
                _callback_button(
                    ui_state,
                    user_id,
                    "Tool Activity",
                    "runtime_status_open",
                    target="tools",
                )
            ]
        )
    if bundle_count > 0:
        if primary_action_kind != "bundle":
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
    can_fork: bool = False,
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
        lines.append("No local session history yet.")
        lines.append(
            "Start a new session to create reusable checkpoints, or open Bot Status to keep "
            "working from the current runtime."
        )
        buttons.append(
            [
                _callback_button(
                    ui_state,
                    user_id,
                    "New Session",
                    "runtime_status_control",
                    target="new_session",
                )
            ]
        )
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
        _append_status_recovery_button(
            buttons,
            ui_state=ui_state,
            user_id=user_id,
            back_target=back_target,
        )
        markup = None if not buttons else InlineKeyboardMarkup(buttons)
        return "\n".join(lines), markup

    page_count = max(1, (len(entries) + HISTORY_PAGE_SIZE - 1) // HISTORY_PAGE_SIZE)
    page = min(max(page, 0), page_count - 1)
    start = page * HISTORY_PAGE_SIZE
    visible_entries = entries[start : start + HISTORY_PAGE_SIZE]
    can_retry_last_turn = ui_state.get_last_turn(user_id, provider, workspace_id) is not None
    _append_paged_list_summary_lines(
        lines,
        total_label="Local sessions",
        total_count=len(entries),
        start_index=start + 1,
        visible_count=len(visible_entries),
        page=page,
        page_count=page_count,
    )
    lines.extend(
        _session_action_guide_lines(
            run_summary="keeps working in that saved session",
            can_fork=can_fork,
            can_retry_last_turn=can_retry_last_turn,
        )
    )
    for offset, entry in enumerate(visible_entries, start=1):
        is_current = entry.session_id == active_session_id
        label = entry.title or entry.session_id
        if is_current:
            label = f"{label} [current]"
        lines.append(f"{start + offset}. {label}")
        lines.append(f"updated={entry.updated_at}")
        history_entry_payload = _history_entry_callback_payload(
            entry=entry,
            page=page,
            back_target=back_target,
        )
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
                        else history_entry_payload
                    ),
                ),
                _callback_button(
                    ui_state,
                    user_id,
                    f"Rename {start + offset}",
                    "history_rename",
                    **history_entry_payload,
                    title=entry.title or entry.session_id,
                ),
                _callback_button(
                    ui_state,
                    user_id,
                    f"Delete {start + offset}",
                    "history_delete",
                    **history_entry_payload,
                ),
            ]
        )
        buttons.append(
            [
                _callback_button(
                    ui_state,
                    user_id,
                    f"Open {start + offset}",
                    "history_open",
                    **history_entry_payload,
                ),
            ]
        )
        action_buttons = []
        if can_retry_last_turn and not is_current:
            action_buttons.append(
                _callback_button(
                    ui_state,
                    user_id,
                    f"Run+Retry {start + offset}",
                    "history_run_retry_last_turn",
                    **history_entry_payload,
                )
            )
        if can_fork:
            action_buttons.append(
                _callback_button(
                    ui_state,
                    user_id,
                    f"Fork {start + offset}",
                    "history_fork",
                    **history_entry_payload,
                )
            )
        if can_fork and can_retry_last_turn:
            action_buttons.append(
                _callback_button(
                    ui_state,
                    user_id,
                    f"Fork+Retry {start + offset}",
                    "history_fork_retry_last_turn",
                    **history_entry_payload,
                )
            )
        if action_buttons:
            buttons.append(action_buttons)

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


def _history_entry_callback_payload(
    *,
    entry,
    page: int,
    back_target: str,
) -> dict[str, Any]:
    return {
        "session_id": entry.session_id,
        "page": page,
        "back_target": back_target,
    }


def _build_history_entry_view(
    *,
    entry,
    provider: str,
    workspace_id: str,
    workspace_label: str,
    user_id: int,
    page: int,
    ui_state: TelegramUiState,
    active_session_id: str | None,
    can_fork: bool,
    back_target: str = "none",
    notice: str | None = None,
):
    lines = []
    if notice:
        lines.append(notice)

    lines.append(
        f"Session history entry for {resolve_provider_profile(provider).display_name} in {workspace_label}"
    )
    lines.append(f"Title: {_status_text_snippet(entry.title, limit=120) or '[untitled]'}")
    lines.append(f"Session: {entry.session_id}")
    lines.append(
        f"Current runtime session: {'yes' if entry.session_id == active_session_id else 'no'}"
    )
    lines.append(f"Cwd: {entry.cwd}")
    lines.append(f"Created: {entry.created_at}")
    lines.append(f"Updated: {entry.updated_at}")

    history_entry_payload = _history_entry_callback_payload(
        entry=entry,
        page=page,
        back_target=back_target,
    )
    is_current = entry.session_id == active_session_id
    can_retry_last_turn = ui_state.get_last_turn(user_id, provider, workspace_id) is not None
    lines.extend(
        _session_action_guide_lines(
            run_summary="keeps working in that saved session",
            can_fork=can_fork,
            can_retry_last_turn=can_retry_last_turn,
        )
    )

    buttons = [
        [
            _callback_button(
                ui_state,
                user_id,
                "Refresh",
                "history_open",
                **history_entry_payload,
            ),
            _callback_button(
                ui_state,
                user_id,
                "Back to History",
                "history_page",
                page=page,
                back_target=back_target,
            ),
        ],
        [
            _callback_button(
                ui_state,
                user_id,
                "Current Session" if is_current else "Run Session",
                "noop" if is_current else "history_run",
                **(
                    {"notice": "Already using this session."}
                    if is_current
                    else history_entry_payload
                ),
            )
        ],
    ]
    action_buttons = []
    if can_retry_last_turn and not is_current:
        action_buttons.append(
            _callback_button(
                ui_state,
                user_id,
                "Run+Retry Session",
                "history_run_retry_last_turn",
                **history_entry_payload,
            )
        )
    if can_fork:
        action_buttons.append(
            _callback_button(
                ui_state,
                user_id,
                "Fork Session",
                "history_fork",
                **history_entry_payload,
            )
        )
    if can_fork and can_retry_last_turn:
        action_buttons.append(
            _callback_button(
                ui_state,
                user_id,
                "Fork+Retry Session",
                "history_fork_retry_last_turn",
                **history_entry_payload,
            )
        )
    if action_buttons:
        buttons.append(action_buttons)

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
    can_fork: bool,
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
    lines.append(
        "Only sessions inside the current workspace are shown. "
        "This list comes from the provider, not the bot's local history."
    )

    buttons = []
    can_retry_last_turn = ui_state.get_last_turn(user_id, provider, workspace_id) is not None
    if not supported:
        lines.append("Provider session browsing is not available for this agent.")
        lines.append("Use Session History for bot-local checkpoints, or keep working from Bot Status.")
        if back_target != "status":
            buttons.append(
                [
                    _callback_button(
                        ui_state,
                        user_id,
                        "Open Bot Status",
                        "runtime_status_page",
                    )
                ]
            )
    elif not entries:
        lines.append("No provider sessions found.")
        lines.append(
            "Start or reuse a live session, then refresh here if the provider persists reusable sessions."
        )
        buttons.append(
            [
                _callback_button(
                    ui_state,
                    user_id,
                    "Refresh",
                    "provider_sessions_page",
                    cursor=cursor,
                    previous_cursors=previous_cursors,
                    history_page=history_page,
                    back_target=back_target,
                    history_back_target=history_back_target,
                )
            ]
        )
        if back_target != "status":
            buttons.append(
                [
                    _callback_button(
                        ui_state,
                        user_id,
                        "Open Bot Status",
                        "runtime_status_page",
                    )
                ]
            )
    else:
        lines.append(f"Loaded sessions on this page: {len(entries)}")
        if previous_cursors or next_cursor is not None:
            lines.append(f"Cursor page: {len(previous_cursors) + 1}")
        lines.extend(
            _session_action_guide_lines(
                run_summary="attaches this bot to that provider session and keeps working there",
                can_fork=can_fork,
                can_retry_last_turn=can_retry_last_turn,
            )
        )
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
            provider_session_payload = _provider_session_callback_payload(
                entry=entry,
                cursor=cursor,
                previous_cursors=previous_cursors,
                history_page=history_page,
                back_target=back_target,
                history_back_target=history_back_target,
            )
            buttons.append(
                [
                    _callback_button(
                        ui_state,
                        user_id,
                        f"Open {index}",
                        "provider_session_open",
                        **provider_session_payload,
                    ),
                    _callback_button(
                        ui_state,
                        user_id,
                        f"{'Current' if is_current else 'Run'} {index}",
                        "noop" if is_current else "provider_session_run",
                        **(
                            {"notice": "Already using this session."}
                            if is_current
                            else provider_session_payload
                        ),
                    )
                ]
            )
            action_buttons = []
            if can_retry_last_turn and not is_current:
                action_buttons.append(
                    _callback_button(
                        ui_state,
                        user_id,
                        f"Run+Retry {index}",
                        "provider_session_run_retry_last_turn",
                        **provider_session_payload,
                    )
                )
            if can_fork:
                action_buttons.append(
                    _callback_button(
                        ui_state,
                        user_id,
                        f"Fork {index}",
                        "provider_session_fork",
                        **provider_session_payload,
                    )
                )
            if can_fork and can_retry_last_turn:
                action_buttons.append(
                    _callback_button(
                        ui_state,
                        user_id,
                        f"Fork+Retry {index}",
                        "provider_session_fork_retry_last_turn",
                        **provider_session_payload,
                    )
                )
            if action_buttons:
                buttons.append(action_buttons)

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


def _build_session_info_view(
    *,
    provider: str,
    workspace_id: str,
    workspace_label: str,
    user_id: int,
    ui_state: TelegramUiState,
    session,
    session_title: str | None,
    back_target: str = "none",
    notice: str | None = None,
):
    lines = []
    if notice:
        lines.append(notice)

    lines.append(
        f"Session info for {resolve_provider_profile(provider).display_name} in {workspace_label}"
    )

    buttons: list[list[InlineKeyboardButton]] = [
        [
            _callback_button(
                ui_state,
                user_id,
                "Refresh",
                "runtime_status_open",
                target="session_info",
                back_target=back_target,
            )
        ]
    ]
    buttons.append(
        [
            _callback_button(
                ui_state,
                user_id,
                "Workspace Runtime",
                "runtime_status_open",
                target="workspace_runtime",
                back_target="session_info",
            )
        ]
    )

    if session is None:
        lines.append("No live session. A session will start on the first request.")
        _append_back_to_status_button(
            buttons,
            ui_state=ui_state,
            user_id=user_id,
            back_target=back_target,
        )
        return "\n".join(lines), InlineKeyboardMarkup(buttons)

    session_id = getattr(session, "session_id", None)
    lines.append(f"Session: {session_id or 'pending'}")
    if session_title is not None:
        lines.append(f"Title: {session_title}")

    session_updated_at = _status_text_snippet(getattr(session, "session_updated_at", None), limit=120)
    if session_updated_at is not None:
        lines.append(f"Updated: {session_updated_at}")

    get_selection = getattr(session, "get_selection", None)
    model_selection = None
    mode_selection = None
    if callable(get_selection):
        try:
            model_selection = get_selection("model")
        except Exception:
            model_selection = None
        try:
            mode_selection = get_selection("mode")
        except Exception:
            mode_selection = None

    model_line = _selection_summary_line("Model", model_selection)
    if model_line is not None:
        lines.append(model_line)
    mode_line = _selection_summary_line("Mode", mode_selection)
    if mode_line is not None:
        lines.append(mode_line)

    capabilities = getattr(session, "capabilities", None)
    if capabilities is not None:
        lines.append("Prompt capabilities:")
        lines.append(
            "image="
            f"{'yes' if getattr(capabilities, 'supports_image_prompt', False) else 'no'}, "
            "audio="
            f"{'yes' if getattr(capabilities, 'supports_audio_prompt', False) else 'no'}, "
            "embedded_context="
            f"{'yes' if getattr(capabilities, 'supports_embedded_context_prompt', False) else 'no'}"
        )
        lines.append("Session capabilities:")
        lines.append(
            "load="
            f"{'yes' if getattr(capabilities, 'can_load', False) else 'no'}, "
            "fork="
            f"{'yes' if getattr(capabilities, 'can_fork', False) else 'no'}, "
            "list="
            f"{'yes' if getattr(capabilities, 'can_list', False) else 'no'}, "
            "resume="
            f"{'yes' if getattr(capabilities, 'can_resume', False) else 'no'}"
        )

    usage_summary = _status_usage_summary(session)
    lines.append(f"Usage: {usage_summary or 'none'}")
    lines.append(f"Cached commands: {len(tuple(getattr(session, 'available_commands', ()) or ()))}")
    lines.append(f"Cached plan items: {len(_plan_items(session))}")
    lines.append(f"Cached tool activities: {len(_tool_activity_items(session))}")
    last_request = ui_state.get_last_request(user_id, workspace_id)

    if usage_summary is not None:
        buttons.append(
            [
                _callback_button(
                    ui_state,
                    user_id,
                    "Usage",
                    "runtime_status_open",
                    target="usage",
                    back_target="session_info",
                )
            ]
        )

    quick_buttons: list[InlineKeyboardButton] = []
    if last_request is not None:
        quick_buttons.append(
            _callback_button(
                ui_state,
                user_id,
                "Last Request",
                "runtime_status_open",
                target="last_request",
                back_target="session_info",
            )
        )
    if tuple(getattr(session, "available_commands", ()) or ()):
        quick_buttons.append(
            _callback_button(
                ui_state,
                user_id,
                "Agent Commands",
                "runtime_status_open",
                target="commands",
                back_target="session_info",
            )
        )
    if _plan_items(session):
        quick_buttons.append(
            _callback_button(
                ui_state,
                user_id,
                "Agent Plan",
                "runtime_status_open",
                target="plan",
                back_target="session_info",
            )
        )
    if _tool_activity_items(session):
        quick_buttons.append(
            _callback_button(
                ui_state,
                user_id,
                "Tool Activity",
                "runtime_status_open",
                target="tools",
                back_target="session_info",
            )
        )
    if ui_state.get_last_turn(user_id, provider, workspace_id) is not None:
        quick_buttons.append(
            _callback_button(
                ui_state,
                user_id,
                "Last Turn",
                "runtime_status_open",
                target="last_turn",
                back_target="session_info",
            )
        )
    if quick_buttons:
        buttons.append(quick_buttons)

    _append_back_to_status_button(
        buttons,
        ui_state=ui_state,
        user_id=user_id,
        back_target=back_target,
    )

    return "\n".join(lines), InlineKeyboardMarkup(buttons)


def _build_usage_view(
    *,
    provider: str,
    workspace_label: str,
    user_id: int,
    ui_state: TelegramUiState,
    session,
    session_title: str | None,
    back_target: str = "none",
    notice: str | None = None,
):
    lines = []
    if notice:
        lines.append(notice)

    lines.append(
        f"Usage for {resolve_provider_profile(provider).display_name} in {workspace_label}"
    )

    buttons: list[list[InlineKeyboardButton]] = []
    if session is None:
        lines.append("No live session. A session will start on the first request.")
        _append_back_to_status_button(
            buttons,
            ui_state=ui_state,
            user_id=user_id,
            back_target=back_target,
        )
        markup = None if not buttons else InlineKeyboardMarkup(buttons)
        return "\n".join(lines), markup

    session_id = getattr(session, "session_id", None)
    lines.append(f"Session: {session_id or 'pending'}")
    if session_title is not None:
        lines.append(f"Title: {session_title}")

    session_updated_at = _status_text_snippet(getattr(session, "session_updated_at", None), limit=120)
    if session_updated_at is not None:
        lines.append(f"Updated: {session_updated_at}")

    usage = getattr(session, "usage", None)
    if usage is None:
        lines.append("Snapshot: none")
        lines.append("No cached usage snapshot for this live session.")
        lines.append("This view only shows the latest ACP usage_update already cached by the bot.")
    else:
        lines.append("Snapshot: cached ACP usage_update")
        lines.append(f"Used: {usage.used}")
        lines.append(f"Window size: {usage.size}")
        remaining = _usage_remaining(usage)
        if remaining is not None:
            lines.append(f"Remaining: {remaining}")
        utilization = _usage_utilization_percent(usage)
        if utilization is not None:
            lines.append(f"Utilization: {utilization:.1f}%")
        lines.append(f"Cost: {_usage_cost_label(usage)}")

    _append_back_to_status_button(
        buttons,
        ui_state=ui_state,
        user_id=user_id,
        back_target=back_target,
    )

    return "\n".join(lines), InlineKeyboardMarkup(buttons)


def _build_last_request_view(
    *,
    last_request: _LastRequestText | None,
    last_turn_available: bool,
    current_provider: str,
    workspace_label: str,
    user_id: int,
    ui_state: TelegramUiState,
    back_target: str = "none",
    notice: str | None = None,
):
    lines = []
    if notice:
        lines.append(notice)

    lines.append(
        f"Last request for {resolve_provider_profile(current_provider).display_name} in {workspace_label}"
    )

    buttons: list[list[InlineKeyboardButton]] = []
    if last_request is None:
        lines.append("No request text is cached for this workspace.")
        _append_back_to_status_button(
            buttons,
            ui_state=ui_state,
            user_id=user_id,
            back_target=back_target,
        )
        markup = None if not buttons else InlineKeyboardMarkup(buttons)
        return "\n".join(lines), markup

    recorded_provider = last_request.provider or current_provider
    lines.append("Replay summary:")
    lines.append(f"Current provider: {_replay_provider_display_name(current_provider)}")
    lines.append(f"Recorded provider: {_replay_provider_display_name(recorded_provider)}")
    lines.append(f"Recorded workspace: {last_request.workspace_id}")
    lines.append(f"Source: {_last_request_source_summary(last_request)}")
    lines.append(
        "Replay note: "
        + _last_request_replay_note(
            last_request=last_request,
            current_provider=current_provider,
        )
    )
    lines.append(
        "Run Last Request sends only this text again in the current provider and workspace. "
        "It does not restore the original attachments or extra context."
    )
    if last_turn_available:
        lines.append(
            "Use Retry Last Turn or Fork Last Turn if you need the original attachments or "
            "extra context back."
        )
    lines.append(
        f"Text length: {len(last_request.text)} character{'s' if len(last_request.text) != 1 else ''}"
    )
    content, truncated = _last_turn_render_text_detail(last_request.text)
    lines.append("")
    lines.append("Request text:")
    lines.append(content or "[empty]")
    if truncated:
        lines.append(f"[content truncated to {LAST_TURN_TEXT_DETAIL_LIMIT} characters]")

    buttons.append(
        [
            _callback_button(
                ui_state,
                user_id,
                "Run Last Request",
                "runtime_status_control",
                target="run_last_request",
            )
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
    _append_back_to_status_button(
        buttons,
        ui_state=ui_state,
        user_id=user_id,
        back_target=back_target,
    )

    return "\n".join(lines), InlineKeyboardMarkup(buttons)


def _build_workspace_runtime_view(
    *,
    provider: str,
    workspace,
    workspace_path: str,
    user_id: int,
    ui_state: TelegramUiState,
    back_target: str = "none",
    notice: str | None = None,
):
    lines = []
    if notice:
        lines.append(notice)

    workspace_label = _status_text_snippet(getattr(workspace, "label", None), limit=120) or "Workspace"
    lines.append(
        f"Workspace runtime for {resolve_provider_profile(provider).display_name} in {workspace_label}"
    )
    lines.append(f"Workspace ID: {getattr(workspace, 'id', 'unknown')}")
    lines.append(f"Path: {workspace_path}")
    lines.append("ACP client tools:")
    lines.append("filesystem=yes (workspace-scoped text read/write)")
    lines.append("terminal=yes (workspace-scoped process bridge)")

    mcp_servers = tuple(getattr(workspace, "mcp_servers", ()) or ())
    if not mcp_servers:
        lines.append("Configured MCP servers: none")
        lines.append("Sessions in this runtime use only the bot client filesystem/terminal bridges.")
    else:
        lines.append(f"Configured MCP servers: {len(mcp_servers)}")
        visible_servers = mcp_servers[:WORKSPACE_RUNTIME_SERVER_PREVIEW_LIMIT]
        for index, server in enumerate(visible_servers, start=1):
            lines.append(f"{index}. {_workspace_runtime_server_summary(server)}")
        remaining = len(mcp_servers) - len(visible_servers)
        if remaining > 0:
            lines.append(f"... {remaining} more server{'s' if remaining != 1 else ''}")
        lines.append("New, loaded, resumed, and forked sessions inherit this MCP server set.")

    buttons: list[list[InlineKeyboardButton]] = []
    if mcp_servers:
        visible_servers = mcp_servers[:WORKSPACE_RUNTIME_SERVER_PREVIEW_LIMIT]
        for index, _server in enumerate(visible_servers, start=1):
            buttons.append(
                [
                    _callback_button(
                        ui_state,
                        user_id,
                        f"Open {index}",
                        "workspace_runtime_open_server",
                        server_index=index - 1,
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


def _build_workspace_runtime_server_view(
    *,
    provider: str,
    workspace,
    workspace_path: str,
    user_id: int,
    ui_state: TelegramUiState,
    server,
    server_index: int,
    server_count: int,
    back_target: str = "none",
    notice: str | None = None,
):
    lines = []
    if notice:
        lines.append(notice)

    workspace_label = _status_text_snippet(getattr(workspace, "label", None), limit=120) or "Workspace"
    lines.append(
        f"Workspace runtime for {resolve_provider_profile(provider).display_name} in {workspace_label}"
    )
    lines.append(f"Workspace ID: {getattr(workspace, 'id', 'unknown')}")
    lines.append(f"Path: {workspace_path}")
    lines.append(f"MCP server: {server_index + 1}/{server_count}")
    lines.append(f"Name: {_status_text_snippet(getattr(server, 'name', None), limit=120) or 'server'}")
    transport = _status_text_snippet(getattr(server, "transport", None), limit=40) or "unknown"
    lines.append(f"Transport: {transport}")

    if transport == "stdio":
        lines.append(f"Command: {_status_text_snippet(getattr(server, 'command', None), limit=200) or '[missing]'}")
        args = tuple(getattr(server, "args", ()) or ())
        if not args:
            lines.append("Args: none")
        else:
            lines.append(f"Args: {len(args)}")
            for index, arg in enumerate(args, start=1):
                lines.append(f"{index}. {_status_text_snippet(str(arg), limit=200) or '[empty]'}")
    else:
        lines.append(f"URL: {_status_text_snippet(getattr(server, 'url', None), limit=200) or '[missing]'}")

    env_items = tuple(getattr(server, "env", ()) or ())
    header_items = tuple(getattr(server, "headers", ()) or ())
    lines.append(f"Env vars: {len(env_items)}")
    if env_items:
        lines.append("Env keys:")
        for item in env_items:
            lines.append(_status_text_snippet(getattr(item, "name", None), limit=120) or "[empty]")
    lines.append(f"Headers: {len(header_items)}")
    if header_items:
        lines.append("Header keys:")
        for item in header_items:
            lines.append(_status_text_snippet(getattr(item, "name", None), limit=120) or "[empty]")

    buttons = [
        [
            _callback_button(
                ui_state,
                user_id,
                "Refresh",
                "workspace_runtime_open_server",
                server_index=server_index,
                back_target=back_target,
            ),
            _callback_button(
                ui_state,
                user_id,
                "Back to Workspace Runtime",
                "runtime_status_open",
                target="workspace_runtime",
                back_target=back_target,
            ),
        ]
    ]

    return "\n".join(lines), InlineKeyboardMarkup(buttons)


def _build_last_turn_view(
    *,
    replay_turn: _ReplayTurn | None,
    current_provider: str,
    workspace_label: str,
    user_id: int,
    page: int,
    ui_state: TelegramUiState,
    back_target: str = "none",
    notice: str | None = None,
):
    lines = []
    if notice:
        lines.append(notice)

    lines.append(
        f"Last turn for {resolve_provider_profile(current_provider).display_name} in {workspace_label}"
    )

    buttons: list[list[InlineKeyboardButton]] = []
    if replay_turn is None:
        lines.append("No replayable turn is cached.")
        _append_back_to_status_button(
            buttons,
            ui_state=ui_state,
            user_id=user_id,
            back_target=back_target,
        )
        markup = None if not buttons else InlineKeyboardMarkup(buttons)
        return "\n".join(lines), markup

    lines.append("Replay summary:")
    lines.append(f"Current provider: {_replay_provider_display_name(current_provider)}")
    lines.append(f"Recorded provider: {_replay_provider_display_name(replay_turn.provider)}")
    lines.append(f"Recorded workspace: {replay_turn.workspace_id}")
    lines.append(
        "Replay note: "
        + _last_turn_replay_note(
            replay_turn=replay_turn,
            current_provider=current_provider,
        )
    )
    lines.append(f"Title: {_status_text_snippet(replay_turn.title_hint, limit=120) or '[empty]'}")
    lines.append(
        "Retry Last Turn replays this saved payload, including any saved attachments or extra "
        "context, in the current live session."
    )
    lines.append(
        "Fork Last Turn starts a new session first, then replays the same payload there."
    )

    prompt_items = _replay_prompt_items(replay_turn)
    saved_context_items = tuple(getattr(replay_turn, "saved_context_items", ()) or ())
    if not prompt_items:
        lines.append("Prompt items: 0")
        lines.append(f"Saved context items: {len(saved_context_items)}")
        lines.extend(_last_turn_context_preview_lines(saved_context_items))
        lines.append("No replay payload items are available.")
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
        _append_back_to_status_button(
            buttons,
            ui_state=ui_state,
            user_id=user_id,
            back_target=back_target,
        )
        return "\n".join(lines), InlineKeyboardMarkup(buttons)

    page_count = max(1, (len(prompt_items) + LAST_TURN_PAGE_SIZE - 1) // LAST_TURN_PAGE_SIZE)
    page = min(max(page, 0), page_count - 1)
    start = page * LAST_TURN_PAGE_SIZE
    visible_items = prompt_items[start : start + LAST_TURN_PAGE_SIZE]
    _append_paged_list_summary_lines(
        lines,
        total_label="Prompt items",
        total_count=len(prompt_items),
        start_index=start + 1,
        visible_count=len(visible_items),
        page=page,
        page_count=page_count,
    )
    lines.append(f"Saved context items: {len(saved_context_items)}")
    lines.extend(_last_turn_context_preview_lines(saved_context_items))

    for offset, item in enumerate(visible_items, start=1):
        index = start + offset
        lines.append(f"{index}. {_last_turn_item_summary(item)}")
        buttons.append(
            [
                _callback_button(
                    ui_state,
                    user_id,
                    f"Open {index}",
                    "last_turn_open",
                    page=page,
                    item_index=index - 1,
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
                    "last_turn_page",
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
                    "last_turn_page",
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

    _append_back_to_status_button(
        buttons,
        ui_state=ui_state,
        user_id=user_id,
        back_target=back_target,
    )

    return "\n".join(lines), InlineKeyboardMarkup(buttons)


def _build_last_turn_item_view(
    *,
    replay_turn: _ReplayTurn,
    current_provider: str,
    workspace_label: str,
    item,
    item_index: int,
    total_count: int,
    user_id: int,
    page: int,
    ui_state: TelegramUiState,
    back_target: str = "none",
    notice: str | None = None,
):
    lines = []
    if notice:
        lines.append(notice)

    lines.append(
        f"Last turn for {resolve_provider_profile(current_provider).display_name} in {workspace_label}"
    )
    lines.append(f"Item: {item_index + 1}/{total_count}")
    lines.append(f"Current provider: {_replay_provider_display_name(current_provider)}")
    lines.append(f"Recorded provider: {_replay_provider_display_name(replay_turn.provider)}")
    lines.append(f"Recorded workspace: {replay_turn.workspace_id}")
    lines.append(
        "Replay note: "
        + _last_turn_replay_note(
            replay_turn=replay_turn,
            current_provider=current_provider,
        )
    )
    lines.append(f"Replay title: {_status_text_snippet(replay_turn.title_hint, limit=120) or '[empty]'}")
    lines.append(f"Kind: {_last_turn_item_kind_label(item)}")

    uri = getattr(item, "uri", None)
    if uri:
        lines.append(f"URI: {uri}")
    mime_type = getattr(item, "mime_type", None)
    if mime_type:
        lines.append(f"MIME type: {mime_type}")
    payload_size = _last_turn_payload_size_bytes(item)
    if payload_size is not None:
        lines.append(f"Payload size: {payload_size} byte{'s' if payload_size != 1 else ''}")

    if isinstance(item, PromptText):
        content, truncated = _last_turn_render_text_detail(item.text)
        lines.append("Content:")
        lines.append(content or "[empty]")
        if truncated:
            lines.append(f"[content truncated to {LAST_TURN_TEXT_DETAIL_LIMIT} characters]")
    elif isinstance(item, PromptTextResource):
        content, truncated = _last_turn_render_text_detail(item.text)
        lines.append("Resource content:")
        lines.append(content or "[empty]")
        if truncated:
            lines.append(f"[content truncated to {LAST_TURN_TEXT_DETAIL_LIMIT} characters]")

    buttons = [
        [
            _callback_button(
                ui_state,
                user_id,
                "Refresh",
                "last_turn_open",
                page=page,
                item_index=item_index,
                back_target=back_target,
            ),
            _callback_button(
                ui_state,
                user_id,
                "Back to Last Turn",
                "last_turn_page",
                page=page,
                back_target=back_target,
            ),
        ],
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
        ],
    ]

    return "\n".join(lines), InlineKeyboardMarkup(buttons)


def _build_plan_view(
    *,
    entries,
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
        f"Agent plan for {resolve_provider_profile(provider).display_name} in {workspace_label}"
    )
    lines.append(f"Session: {session_id or 'none'}")

    buttons = []
    if not entries:
        lines.append("No cached agent plan.")
        _append_back_to_status_button(
            buttons,
            ui_state=ui_state,
            user_id=user_id,
            back_target=back_target,
        )
        markup = None if not buttons else InlineKeyboardMarkup(buttons)
        return "\n".join(lines), markup

    page_count = max(1, (len(entries) + PLAN_PAGE_SIZE - 1) // PLAN_PAGE_SIZE)
    page = min(max(page, 0), page_count - 1)
    start = page * PLAN_PAGE_SIZE
    visible_entries = entries[start : start + PLAN_PAGE_SIZE]
    _append_paged_list_summary_lines(
        lines,
        total_label="Plan items",
        total_count=len(entries),
        start_index=start + 1,
        visible_count=len(visible_entries),
        page=page,
        page_count=page_count,
    )

    for offset, entry in enumerate(visible_entries, start=1):
        index = start + offset
        status = str(getattr(entry, "status", "pending"))
        content = _status_text_snippet(getattr(entry, "content", None), limit=120) or "[empty]"
        priority = _status_text_snippet(getattr(entry, "priority", None))
        summary = f"{_plan_status_prefix(status)} {content}"
        detail = None if priority is None else f"priority: {priority}"
        lines.append(f"{index}. {_status_summary_with_details(summary, detail)}")
        buttons.append(
            [
                _callback_button(
                    ui_state,
                    user_id,
                    f"Open {index}",
                    "plan_open",
                    page=page,
                    plan_index=index - 1,
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
                    "plan_page",
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
                    "plan_page",
                    page=page + 1,
                    back_target=back_target,
                )
            )
        if nav:
            buttons.append(nav)

    _append_back_to_status_button(
        buttons,
        ui_state=ui_state,
        user_id=user_id,
        back_target=back_target,
    )

    return "\n".join(lines), InlineKeyboardMarkup(buttons)


def _build_plan_detail_view(
    *,
    entry,
    plan_index: int,
    total_count: int,
    provider: str,
    workspace_label: str,
    user_id: int,
    page: int,
    ui_state: TelegramUiState,
    back_target: str = "none",
    notice: str | None = None,
):
    lines = []
    if notice:
        lines.append(notice)

    lines.append(
        f"Agent plan for {resolve_provider_profile(provider).display_name} in {workspace_label}"
    )
    lines.append(f"Item: {plan_index + 1}/{total_count}")
    lines.append(f"Status: {getattr(entry, 'status', 'pending')}")
    priority = _status_text_snippet(getattr(entry, "priority", None))
    if priority is not None:
        lines.append(f"Priority: {priority}")
    lines.append("Content:")
    content = getattr(entry, "content", None)
    if content is None:
        lines.append("[empty]")
    else:
        rendered = str(content)
        lines.append(rendered if rendered.strip() else "[empty]")

    buttons = [
        [
            _callback_button(
                ui_state,
                user_id,
                "Refresh",
                "plan_open",
                page=page,
                plan_index=plan_index,
                back_target=back_target,
            ),
            _callback_button(
                ui_state,
                user_id,
                "Back to Agent Plan",
                "plan_page",
                page=page,
                back_target=back_target,
            ),
        ]
    ]

    return "\n".join(lines), InlineKeyboardMarkup(buttons)


def _build_tool_activity_view(
    *,
    activities,
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
        f"Tool activity for {resolve_provider_profile(provider).display_name} in {workspace_label}"
    )
    lines.append(f"Session: {session_id or 'none'}")

    buttons = []
    if not activities:
        lines.append("No recent tool activity.")
        _append_back_to_status_button(
            buttons,
            ui_state=ui_state,
            user_id=user_id,
            back_target=back_target,
        )
        markup = None if not buttons else InlineKeyboardMarkup(buttons)
        return "\n".join(lines), markup

    page_count = max(1, (len(activities) + TOOL_ACTIVITY_PAGE_SIZE - 1) // TOOL_ACTIVITY_PAGE_SIZE)
    page = min(max(page, 0), page_count - 1)
    start = page * TOOL_ACTIVITY_PAGE_SIZE
    visible_activities = activities[start : start + TOOL_ACTIVITY_PAGE_SIZE]
    _append_paged_list_summary_lines(
        lines,
        total_label="Recent tools",
        total_count=len(activities),
        start_index=start + 1,
        visible_count=len(visible_activities),
        page=page,
        page_count=page_count,
    )

    for offset, activity in enumerate(visible_activities, start=1):
        index = start + offset
        title = _status_text_snippet(getattr(activity, "title", None)) or getattr(
            activity, "tool_call_id", "tool"
        )
        status = str(getattr(activity, "status", "pending"))
        summary = f"[{status}] {title}"
        detail_parts = []
        kind = _status_text_snippet(getattr(activity, "kind", None))
        if kind is not None:
            detail_parts.append(kind)
        for detail in tuple(getattr(activity, "details", ()) or ())[:2]:
            detail_snippet = _status_text_snippet(detail)
            if detail_snippet is not None:
                detail_parts.append(detail_snippet)
        lines.append(f"{index}. {_status_summary_with_details(summary, *detail_parts)}")
        buttons.append(
            [
                _callback_button(
                    ui_state,
                    user_id,
                    f"Open {index}",
                    "tool_activity_open",
                    page=page,
                    activity_index=index - 1,
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
                    "tool_activity_page",
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
                    "tool_activity_page",
                    page=page + 1,
                    back_target=back_target,
                )
            )
        if nav:
            buttons.append(nav)

    _append_back_to_status_button(
        buttons,
        ui_state=ui_state,
        user_id=user_id,
        back_target=back_target,
    )

    return "\n".join(lines), InlineKeyboardMarkup(buttons)


def _build_tool_activity_detail_view(
    *,
    activity,
    activity_index: int,
    total_count: int,
    provider: str,
    workspace_label: str,
    user_id: int,
    page: int,
    ui_state: TelegramUiState,
    openable_paths: tuple[str, ...],
    change_targets: tuple[tuple[str, str], ...],
    terminal_previews: tuple[_ToolActivityTerminalPreview, ...],
    back_target: str = "none",
    notice: str | None = None,
):
    lines = []
    if notice:
        lines.append(notice)

    lines.append(
        f"Tool activity for {resolve_provider_profile(provider).display_name} in {workspace_label}"
    )
    lines.append(f"Item: {activity_index + 1}/{total_count}")
    lines.append(
        f"Title: {_status_text_snippet(getattr(activity, 'title', None)) or getattr(activity, 'tool_call_id', 'tool')}"
    )
    lines.append(f"Status: {getattr(activity, 'status', 'pending')}")
    kind = _status_text_snippet(getattr(activity, "kind", None))
    if kind is not None:
        lines.append(f"Kind: {kind}")
    lines.append(f"Tool call: {getattr(activity, 'tool_call_id', 'tool')}")

    details = tuple(getattr(activity, "details", ()) or ())
    if details:
        lines.append("Details:")
        for index, detail in enumerate(details, start=1):
            lines.append(f"{index}. {detail}")

    content_types = tuple(getattr(activity, "content_types", ()) or ())
    if content_types:
        lines.append(f"Content: {', '.join(content_types)}")

    path_refs = tuple(getattr(activity, "path_refs", ()) or ())
    if path_refs:
        lines.append("Paths:")
        visible_refs = path_refs[:TOOL_ACTIVITY_PATH_BUTTON_LIMIT]
        for index, path_ref in enumerate(visible_refs, start=1):
            lines.append(f"{index}. {path_ref}")
        remaining_refs = len(path_refs) - len(visible_refs)
        if remaining_refs > 0:
            lines.append(f"... {remaining_refs} more path{'s' if remaining_refs != 1 else ''}")

    terminal_ids = tuple(getattr(activity, "terminal_ids", ()) or ())
    if terminal_ids:
        lines.append("Terminal preview:")
        if not terminal_previews:
            lines.append("1. Output unavailable.")
        else:
            for index, preview in enumerate(terminal_previews, start=1):
                lines.append(f"{index}. {preview.terminal_id} [{preview.status_label}]")
                if preview.output is None:
                    lines.append("output: [no output]")
                else:
                    output = preview.output
                    if preview.truncated:
                        output = f"{output}\n[output truncated]"
                    lines.append(f"output:\n{output}")
            remaining_terminals = len(terminal_ids) - len(terminal_previews)
            if remaining_terminals > 0:
                lines.append(
                    f"... {remaining_terminals} more terminal{'s' if remaining_terminals != 1 else ''}"
                )

    buttons = [
        [
            _callback_button(
                ui_state,
                user_id,
                "Refresh",
                "tool_activity_open",
                page=page,
                activity_index=activity_index,
                back_target=back_target,
            ),
            _callback_button(
                ui_state,
                user_id,
                "Back to Tool Activity",
                "tool_activity_page",
                page=page,
                back_target=back_target,
            ),
        ]
    ]

    for index, relative_path in enumerate(openable_paths[:TOOL_ACTIVITY_PATH_BUTTON_LIMIT], start=1):
        buttons.append(
            [
                _callback_button(
                    ui_state,
                    user_id,
                    f"Open File {index}",
                    "tool_activity_open_file",
                    relative_path=relative_path,
                    page=page,
                    activity_index=activity_index,
                    back_target=back_target,
                )
            ]
        )

    for index, (relative_path, status_code) in enumerate(
        change_targets[:TOOL_ACTIVITY_PATH_BUTTON_LIMIT],
        start=1,
    ):
        buttons.append(
            [
                _callback_button(
                    ui_state,
                    user_id,
                    f"Open Change {index}",
                    "tool_activity_open_change",
                    relative_path=relative_path,
                    status_code=status_code,
                    page=page,
                    activity_index=activity_index,
                    back_target=back_target,
                )
            ]
        )

    return "\n".join(lines), InlineKeyboardMarkup(buttons)


def _provider_session_callback_payload(
    *,
    entry,
    cursor: str | None,
    previous_cursors: tuple[str | None, ...],
    history_page: int,
    back_target: str,
    history_back_target: str,
) -> dict[str, Any]:
    return {
        "session_id": entry.session_id,
        "title": entry.title,
        "cursor": cursor,
        "previous_cursors": previous_cursors,
        "history_page": history_page,
        "back_target": back_target,
        "history_back_target": history_back_target,
    }


def _build_provider_session_detail_view(
    *,
    entry,
    provider: str,
    workspace_id: str,
    workspace_label: str,
    user_id: int,
    ui_state: TelegramUiState,
    active_session_id: str | None,
    can_fork: bool,
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
        f"Provider session for {resolve_provider_profile(provider).display_name} in {workspace_label}"
    )
    lines.append(f"Title: {_status_text_snippet(entry.title, limit=120) or '[untitled]'}")
    lines.append(f"Session: {entry.session_id}")
    lines.append(
        f"Current runtime session: {'yes' if entry.session_id == active_session_id else 'no'}"
    )
    lines.append(f"Workspace-relative cwd: {entry.cwd_label}")
    lines.append(f"Provider cwd: {entry.cwd}")
    lines.append(f"Updated: {entry.updated_at or 'unknown'}")

    provider_session_payload = _provider_session_callback_payload(
        entry=entry,
        cursor=cursor,
        previous_cursors=previous_cursors,
        history_page=history_page,
        back_target=back_target,
        history_back_target=history_back_target,
    )
    is_current = entry.session_id == active_session_id
    can_retry_last_turn = ui_state.get_last_turn(user_id, provider, workspace_id) is not None
    lines.extend(
        _session_action_guide_lines(
            run_summary="attaches this bot to that provider session and keeps working there",
            can_fork=can_fork,
            can_retry_last_turn=can_retry_last_turn,
        )
    )

    buttons = [
        [
            _callback_button(
                ui_state,
                user_id,
                "Refresh",
                "provider_session_open",
                **provider_session_payload,
            ),
            _callback_button(
                ui_state,
                user_id,
                "Back to Provider Sessions",
                "provider_sessions_page",
                cursor=cursor,
                previous_cursors=previous_cursors,
                history_page=history_page,
                back_target=back_target,
                history_back_target=history_back_target,
            ),
        ],
        [
            _callback_button(
                ui_state,
                user_id,
                "Current Session" if is_current else "Run Session",
                "noop" if is_current else "provider_session_run",
                **(
                    {"notice": "Already using this session."}
                    if is_current
                    else provider_session_payload
                ),
            )
        ],
    ]
    action_buttons = []
    if can_retry_last_turn and not is_current:
        action_buttons.append(
            _callback_button(
                ui_state,
                user_id,
                "Run+Retry Session",
                "provider_session_run_retry_last_turn",
                **provider_session_payload,
            )
        )
    if can_fork:
        action_buttons.append(
            _callback_button(
                ui_state,
                user_id,
                "Fork Session",
                "provider_session_fork",
                **provider_session_payload,
            )
        )
    if can_fork and can_retry_last_turn:
        action_buttons.append(
            _callback_button(
                ui_state,
                user_id,
                "Fork+Retry Session",
                "provider_session_fork_retry_last_turn",
                **provider_session_payload,
            )
        )
    if action_buttons:
        buttons.append(action_buttons)

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
        lines.append(
            "Command discovery may still be loading, or the current agent may not expose any "
            "slash commands."
        )
        buttons.append(
            [
                _callback_button(
                    ui_state,
                    user_id,
                    "Refresh",
                    "agent_commands_page",
                    page=0,
                    back_target=back_target,
                )
            ]
        )
        _append_status_recovery_button(
            buttons,
            ui_state=ui_state,
            user_id=user_id,
            back_target=back_target,
        )
        markup = None if not buttons else InlineKeyboardMarkup(buttons)
        return "\n".join(lines), markup

    page_count = max(1, (len(commands) + COMMAND_PAGE_SIZE - 1) // COMMAND_PAGE_SIZE)
    page = min(max(page, 0), page_count - 1)
    start = page * COMMAND_PAGE_SIZE
    visible_commands = commands[start : start + COMMAND_PAGE_SIZE]
    _append_paged_list_summary_lines(
        lines,
        total_label="Commands",
        total_count=len(commands),
        start_index=start + 1,
        visible_count=len(visible_commands),
        page=page,
        page_count=page_count,
    )

    for offset, command in enumerate(visible_commands, start=1):
        index = start + offset
        lines.append(f"{index}. {_agent_command_name(command.name)}")
        description = (command.description or "").strip()
        if description:
            lines.append(description)
        if command.hint:
            lines.append(f"args: {command.hint}")
        command_payload = _agent_command_callback_payload(
            command=command,
            page=page,
            command_index=index - 1,
            back_target=back_target,
        )
        buttons.append(
            [
                _callback_button(
                    ui_state,
                    user_id,
                    f"{'Args' if command.hint else 'Run'} {index}",
                    "agent_command_use",
                    **command_payload,
                ),
                _callback_button(
                    ui_state,
                    user_id,
                    f"Open {index}",
                    "agent_command_open",
                    page=page,
                    command_index=index - 1,
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

    _append_back_to_status_button(
        buttons,
        ui_state=ui_state,
        user_id=user_id,
        back_target=back_target,
    )

    return "\n".join(lines), InlineKeyboardMarkup(buttons)


def _agent_command_callback_payload(
    *,
    command,
    page: int,
    command_index: int,
    back_target: str,
) -> dict[str, Any]:
    return {
        "command_name": command.name,
        "hint": command.hint,
        "page": page,
        "command_index": command_index,
        "back_target": back_target,
    }


def _build_agent_command_detail_view(
    *,
    command,
    command_index: int,
    total_count: int,
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
        f"Agent command for {resolve_provider_profile(provider).display_name} in {workspace_label}"
    )
    lines.append(f"Command: {command_index + 1}/{total_count}")
    lines.append(f"Session: {session_id or 'none (will start on first command)'}")
    lines.append(f"Name: {_agent_command_name(command.name)}")
    description = (command.description or "").strip()
    if description:
        lines.append("Description:")
        lines.append(description)
    else:
        lines.append("Description: none")
    if command.hint:
        lines.append(f"Args hint: {command.hint}")
        lines.append(f"Example: {_agent_command_name(command.name)} <args>")
    else:
        lines.append("Args hint: none")
        lines.append(f"Example: {_agent_command_name(command.name)}")

    command_payload = _agent_command_callback_payload(
        command=command,
        page=page,
        command_index=command_index,
        back_target=back_target,
    )
    action_label = "Enter Args" if command.hint else "Run Command"
    buttons = [
        [
            _callback_button(
                ui_state,
                user_id,
                "Refresh",
                "agent_command_open",
                page=page,
                command_index=command_index,
                back_target=back_target,
            ),
            _callback_button(
                ui_state,
                user_id,
                "Back to Agent Commands",
                "agent_commands_page",
                page=page,
                back_target=back_target,
            ),
        ],
        [
            _callback_button(
                ui_state,
                user_id,
                action_label,
                "agent_command_use",
                **command_payload,
            )
        ],
    ]

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
    elif back_target == "session_info":
        button = _callback_button(
            ui_state,
            user_id,
            "Back to Session Info",
            "runtime_status_open",
            target="session_info",
            back_target="status",
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


def _append_status_recovery_button(
    buttons: list[list[InlineKeyboardButton]],
    *,
    ui_state: TelegramUiState,
    user_id: int,
    back_target: str,
    label: str = "Open Bot Status",
) -> None:
    existing_count = len(buttons)
    _append_back_to_status_button(
        buttons,
        ui_state=ui_state,
        user_id=user_id,
        back_target=back_target,
    )
    if len(buttons) != existing_count:
        return
    buttons.append(
        [
            _callback_button(
                ui_state,
                user_id,
                label,
                "runtime_status_page",
            )
        ]
    )


def _navigation_failure_markup(
    *,
    ui_state: TelegramUiState,
    user_id: int,
    retry_label: str = "Try Again",
    retry_action: str,
    retry_payload: dict[str, Any] | None = None,
    back_target: str = "none",
    back_label: str | None = None,
    back_action: str | None = None,
    back_payload: dict[str, Any] | None = None,
) -> InlineKeyboardMarkup:
    buttons = [
        [
            _callback_button(
                ui_state,
                user_id,
                retry_label,
                retry_action,
                **({} if retry_payload is None else dict(retry_payload)),
            )
        ]
    ]
    if back_label is not None and back_action is not None:
        buttons.append(
            [
                _callback_button(
                    ui_state,
                    user_id,
                    back_label,
                    back_action,
                    **({} if back_payload is None else dict(back_payload)),
                )
            ]
        )
    else:
        _append_back_to_status_button(
            buttons,
            ui_state=ui_state,
            user_id=user_id,
            back_target=back_target,
        )
    return InlineKeyboardMarkup(buttons)


async def _show_navigation_failure(
    query,
    *,
    ui_state: TelegramUiState,
    user_id: int,
    text: str,
    retry_label: str = "Try Again",
    retry_action: str,
    retry_payload: dict[str, Any] | None = None,
    back_target: str = "none",
    back_label: str | None = None,
    back_action: str | None = None,
    back_payload: dict[str, Any] | None = None,
) -> None:
    await _edit_query_message(
        query,
        text,
        reply_markup=_navigation_failure_markup(
            ui_state=ui_state,
            user_id=user_id,
            retry_label=retry_label,
            retry_action=retry_action,
            retry_payload=retry_payload,
            back_target=back_target,
            back_label=back_label,
            back_action=back_action,
            back_payload=back_payload,
        ),
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
        if listing.relative_path:
            lines.append("Go up, search the workspace, or open Bot Status to continue elsewhere.")
        else:
            lines.append("Search the workspace or open Bot Status to continue elsewhere.")
        buttons = []
        navigation_buttons = []
        if listing.relative_path:
            navigation_buttons.append(
                _callback_button(
                    ui_state,
                    user_id,
                    "Up",
                    "workspace_open_dir",
                    relative_path=_parent_relative_path(listing.relative_path),
                )
            )
        navigation_buttons.append(
            _callback_button(
                ui_state,
                user_id,
                "Workspace Search",
                "recover_workspace_search",
            )
        )
        buttons.append(navigation_buttons)
        _append_status_recovery_button(
            buttons,
            ui_state=ui_state,
            user_id=user_id,
            back_target=back_target,
        )
        markup = None if not buttons else InlineKeyboardMarkup(buttons)
        return "\n".join(lines), markup

    page, page_count, visible_entries = _visible_workspace_entries(listing, page)
    _append_paged_list_summary_lines(
        lines,
        total_label="Entries",
        total_count=len(listing.entries),
        start_index=page * WORKSPACE_PAGE_SIZE + 1,
        visible_count=len(visible_entries),
        page=page,
        page_count=page_count,
    )

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

    _append_action_guide_lines(
        lines,
        entries=_workspace_collection_action_guide_entries(
            ask_label="Ask Agent With Visible Files",
            subject_summary="the files shown on this page",
            bundle_chat_label="Start Bundle Chat With Visible Files",
            add_label="Add Visible Files to Context",
            has_last_request=last_request_text is not None,
        ),
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
        lines.append(
            "Try a broader query, search again, or open Workspace Files to browse manually."
        )
        buttons: list[list[InlineKeyboardButton]] = []
        buttons.append(
            [
                _callback_button(
                    ui_state,
                    user_id,
                    "Search Again",
                    "recover_workspace_search",
                ),
                _callback_button(
                    ui_state,
                    user_id,
                    "Workspace Files",
                    "runtime_status_open",
                    target="files",
                ),
            ]
        )
        _append_status_recovery_button(
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
    _append_paged_list_summary_lines(
        lines,
        total_label="Matches",
        total_count=len(search_results.matches),
        start_index=start + 1,
        visible_count=len(visible_matches),
        page=page,
        page_count=page_count,
    )

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

    _append_action_guide_lines(
        lines,
        entries=_workspace_collection_action_guide_entries(
            ask_label="Ask Agent With Matching Files",
            subject_summary="the matching files shown on this page",
            bundle_chat_label="Start Bundle Chat With Matching Files",
            add_label="Add Matching Files to Context",
            has_last_request=last_request_text is not None,
        ),
    )

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
        lines.append(
            "Use Workspace Files or Workspace Search when you still need local project context."
        )
        buttons: list[list[InlineKeyboardButton]] = []
        buttons.append(
            [
                _callback_button(
                    ui_state,
                    user_id,
                    "Workspace Files",
                    "runtime_status_open",
                    target="files",
                ),
                _callback_button(
                    ui_state,
                    user_id,
                    "Workspace Search",
                    "runtime_status_open",
                    target="search",
                ),
            ]
        )
        _append_status_recovery_button(
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
        lines.append(
            "Browse files, search the workspace, or send a fresh request if you are ready to keep going."
        )
        buttons = []
        buttons.append(
            [
                _callback_button(
                    ui_state,
                    user_id,
                    "Workspace Files",
                    "runtime_status_open",
                    target="files",
                ),
                _callback_button(
                    ui_state,
                    user_id,
                    "Workspace Search",
                    "runtime_status_open",
                    target="search",
                ),
            ]
        )
        _append_status_recovery_button(
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
    _append_paged_list_summary_lines(
        lines,
        total_label="Changes",
        total_count=len(git_status.entries),
        start_index=start + 1,
        visible_count=len(visible_entries),
        page=page,
        page_count=page_count,
    )

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

    _append_action_guide_lines(
        lines,
        entries=_workspace_collection_action_guide_entries(
            ask_label="Ask Agent With Current Changes",
            subject_summary="the changes shown on this page",
            bundle_chat_label="Start Bundle Chat With Changes",
            add_label="Add All Changes to Context",
            has_last_request=last_request_text is not None,
        ),
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
        lines.append(_context_bundle_empty_text())
        lines.append(
            "Add files from Workspace Files or Search, or add current Git changes, then come "
            "back here to reuse that context."
        )
        buttons: list[list[InlineKeyboardButton]] = []
        buttons.append(
            [
                _callback_button(
                    ui_state,
                    user_id,
                    "Workspace Files",
                    "runtime_status_open",
                    target="files",
                ),
                _callback_button(
                    ui_state,
                    user_id,
                    "Workspace Search",
                    "runtime_status_open",
                    target="search",
                ),
            ]
        )
        buttons.append(
            [
                _callback_button(
                    ui_state,
                    user_id,
                    "Workspace Changes",
                    "runtime_status_open",
                    target="changes",
                )
            ]
        )
        _append_restore_source_or_status_button(
            buttons,
            ui_state=ui_state,
            user_id=user_id,
            back_target=back_target,
            source_restore_action=source_restore_action,
            source_restore_payload=source_restore_payload,
            source_back_label=source_back_label,
        )
        if source_restore_action is None and back_target == "none":
            _append_status_recovery_button(
                buttons,
                ui_state=ui_state,
                user_id=user_id,
                back_target=back_target,
            )
        markup = None if not buttons else InlineKeyboardMarkup(buttons)
        return "\n".join(lines), markup

    lines.append(f"Items: {len(bundle.items)}")
    lines.append(f"Bundle chat: {'on' if bundle_chat_active else 'off'}")
    page_count = max(1, (len(bundle.items) + CONTEXT_BUNDLE_PAGE_SIZE - 1) // CONTEXT_BUNDLE_PAGE_SIZE)
    page = min(max(page, 0), page_count - 1)
    start = page * CONTEXT_BUNDLE_PAGE_SIZE
    visible_items = bundle.items[start : start + CONTEXT_BUNDLE_PAGE_SIZE]
    if page_count > 1:
        lines.append(f"Showing: {start + 1}-{start + len(visible_items)} of {len(bundle.items)}")
        lines.append(f"Page: {page + 1}/{page_count}")

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

    lines.append("")
    lines.append("Ask Agent With Context starts a fresh turn with these items.")
    if last_request_text is not None:
        lines.append("Ask With Last Request reuses the saved request text with this bundle.")
    if bundle_chat_active:
        lines.append(
            "Bundle chat is on, so your next plain text message will include this bundle automatically."
        )
    else:
        lines.append(
            "Start Bundle Chat if you want your next plain text message to include this bundle automatically."
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
    action_guide_entries: tuple[tuple[str, str], ...] = (),
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

    _append_action_guide_lines(lines, entries=action_guide_entries)

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
    action_guide_entries: tuple[tuple[str, str], ...] = (),
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

    _append_action_guide_lines(lines, entries=action_guide_entries)

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
    provider: str,
    workspace_label: str,
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
    lines.append(
        f"Model / Mode for {resolve_provider_profile(provider).display_name} in {workspace_label}"
    )
    lines.append(f"Session: {session_id or 'pending'}")
    current_setup = _model_mode_current_setup_line(
        model_selection=model_selection,
        mode_selection=mode_selection,
    )
    if current_setup is not None:
        lines.append(current_setup)
    if model_selection is None and mode_selection is not None:
        lines.append(
            "Model controls are not exposed in this session. Use the available mode controls "
            "below, or keep chatting normally if you do not need to change it."
        )
    if mode_selection is None and model_selection is not None:
        lines.append(
            "Mode controls are not exposed in this session. Use the available model controls "
            "below, or keep chatting normally if you do not need to change it."
        )
    lines.append("This updates the current live session in place.")
    if can_retry_last_turn:
        lines.append(
            "Shortcut: use ...+Retry to rerun the last turn immediately with the updated setting."
        )
    else:
        lines.append("Open a choice first if you want to inspect its details before switching.")
    lines.append("")
    buttons = []

    if model_selection is not None:
        lines.extend(_selection_overview_lines(prefix="Model", selection=model_selection))
        lines.append("")
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
        lines.extend(_selection_overview_lines(prefix="Mode", selection=mode_selection))
        lines.append("")
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


def _model_mode_current_setup_line(*, model_selection, mode_selection) -> str | None:
    parts = []
    if model_selection is not None:
        parts.append(f"model={_current_choice_label(model_selection)}")
    if mode_selection is not None:
        parts.append(f"mode={_current_choice_label(mode_selection)}")
    if not parts:
        return None
    return "Current setup: " + ", ".join(parts)


def _selection_overview_lines(*, prefix: str, selection) -> list[str]:
    lines = [f"{prefix} choices:"]
    for choice_index, choice in enumerate(selection.choices, start=1):
        current_suffix = " [current]" if choice.value == selection.current_value else ""
        lines.append(f"{choice_index}. {choice.label}{current_suffix}")
    lines.append(f"Tap {prefix}: ... to switch now, or Open {prefix} N for details.")
    return lines


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
    for choice_index, choice in enumerate(selection.choices, start=1):
        selection_payload = _selection_choice_payload(
            selection=selection,
            choice=choice,
            back_target=back_target,
        )
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
        else:
            row = [
                _callback_button(
                    ui_state,
                    user_id,
                    f"{prefix}: {choice.label}",
                    "set_selection",
                    **selection_payload,
                )
            ]
            if can_retry_last_turn:
                row.append(
                    _callback_button(
                        ui_state,
                        user_id,
                        f"{prefix}+Retry: {choice.label}",
                        "set_selection_retry_last_turn",
                        **selection_payload,
                    )
                )
            buttons.append(row)
        buttons.append(
            [
                _callback_button(
                    ui_state,
                    user_id,
                    f"Open {prefix} {choice_index}",
                    "selection_open",
                    **selection_payload,
                )
            ]
        )
    return buttons


def _selection_choice_payload(
    *,
    selection,
    choice,
    back_target: str,
) -> dict[str, Any]:
    return {
        "kind": selection.kind,
        "value": choice.value,
        "back_target": back_target,
    }


def _selection_kind_label(kind: str) -> str:
    if kind == "model":
        return "Model"
    if kind == "mode":
        return "Mode"
    return kind.title()


def _build_selection_detail_view(
    *,
    session_id: str | None,
    selection,
    choice,
    choice_index: int,
    provider: str,
    workspace_label: str,
    user_id: int,
    ui_state: TelegramUiState,
    can_retry_last_turn: bool,
    back_target: str = "none",
    notice: str | None = None,
):
    lines = []
    if notice:
        lines.append(notice)

    kind_label = _selection_kind_label(selection.kind)
    is_current = choice.value == selection.current_value
    lines.append(
        f"{kind_label} choice for {resolve_provider_profile(provider).display_name} in {workspace_label}"
    )
    lines.append(f"Session: {session_id or 'pending'}")
    lines.append(f"Choice: {choice_index + 1}/{len(selection.choices)}")
    lines.append(f"Current selection: {_current_choice_label(selection)}")
    lines.append(f"This choice is current: {'yes' if is_current else 'no'}")
    lines.append(f"Label: {choice.label}")
    lines.append(f"Value: {choice.value}")
    if selection.config_id:
        lines.append(f"Config option: {selection.config_id}")
    description = _status_text_snippet(getattr(choice, "description", None), limit=400)
    if description is None:
        lines.append("Description: none")
    else:
        lines.append("Description:")
        lines.append(description)
    lines.append("Effect: this updates the current live session in place.")
    if is_current:
        lines.append(
            f"Recommended next step: go back to Model / Mode, or inspect another {kind_label.lower()} choice."
        )
    elif can_retry_last_turn:
        lines.append(
            f"Recommended next step: tap Use {kind_label} to switch now, or Use {kind_label} + Retry "
            "to rerun the last turn immediately."
        )
    else:
        lines.append(
            f"Recommended next step: tap Use {kind_label} to switch now, or go back to compare another choice."
        )

    selection_payload = _selection_choice_payload(
        selection=selection,
        choice=choice,
        back_target=back_target,
    )
    buttons = [
        [
            _callback_button(
                ui_state,
                user_id,
                "Refresh",
                "selection_open",
                **selection_payload,
            ),
            _callback_button(
                ui_state,
                user_id,
                "Back to Model / Mode",
                "model_mode_page",
                back_target=back_target,
            ),
        ],
        [
            _callback_button(
                ui_state,
                user_id,
                f"Current {kind_label}" if is_current else f"Use {kind_label}",
                "noop" if is_current else "set_selection",
                **(
                    {"notice": f"Already using {choice.label}."}
                    if is_current
                    else selection_payload
                ),
            )
        ],
    ]
    if can_retry_last_turn and not is_current:
        buttons.append(
            [
                _callback_button(
                    ui_state,
                    user_id,
                    f"Use {kind_label} + Retry",
                    "set_selection_retry_last_turn",
                    **selection_payload,
                )
            ]
        )

    return "\n".join(lines), InlineKeyboardMarkup(buttons)


def _current_choice_label(selection) -> str:
    for choice in selection.choices:
        if choice.value == selection.current_value:
            return choice.label
    return selection.current_value


async def _edit_query_message(query, text: str, *, reply_markup=None) -> None:
    if query.message is not None:
        await query.message.edit_text(text, reply_markup=reply_markup)
