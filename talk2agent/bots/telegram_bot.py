from __future__ import annotations

import asyncio
import base64
import json
import logging
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


logger = logging.getLogger(__name__)


BUTTON_NEW_SESSION = "新建会话"
BUTTON_BOT_STATUS = "状态中心"
BUTTON_HELP = "帮助"
BUTTON_CANCEL_OR_STOP = "取消 / 停止"
BUTTON_RETRY_LAST_TURN = "重试上一轮"
BUTTON_FORK_LAST_TURN = "分叉上一轮"
BUTTON_SESSION_HISTORY = "会话历史"
BUTTON_AGENT_COMMANDS = "Agent 命令"
BUTTON_MODEL_MODE = "模型 / 模式"
BUTTON_WORKSPACE_FILES = "工作区文件"
BUTTON_WORKSPACE_SEARCH = "工作区搜索"
BUTTON_WORKSPACE_CHANGES = "工作区变更"
BUTTON_CONTEXT_BUNDLE = "上下文包"
BUTTON_RESTART_AGENT = "重启 Agent"
BUTTON_SWITCH_AGENT = "切换 Agent"
BUTTON_SWITCH_WORKSPACE = "切换工作区"

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
    (START_COMMAND, "恢复欢迎页与主键盘"),
    (STATUS_COMMAND, "打开状态中心，查看运行态、恢复入口与工作区上下文"),
    (HELP_COMMAND, "查看快速帮助、术语说明与恢复入口"),
    (CANCEL_COMMAND, "取消待输入、停止当前回合或退出 Bundle Chat"),
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
LOG_TEXT_SNIPPET_LIMIT = 120
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


_BUTTON_LABEL_LOCALIZATIONS = {
    "Stop Turn": "停止当前回合",
    "Cancel Pending Input": "取消待输入",
    "Discard Pending Uploads": "丢弃待上传",
    "Bundle + Last Request": "上下文包 + 上次请求",
    "Ask Agent With Context": "带上下文提问",
    "Context Bundle": "上下文包",
    "Run Last Request": "重跑上次请求",
    "Last Request": "上次请求",
    "Retry Last Turn": "重试上一轮",
    "Fork Last Turn": "分叉上一轮",
    "Refresh": "刷新",
    "Session History": "会话历史",
    "Provider Sessions": "Provider 会话",
    "Switch Agent": "切换 Agent",
    "Switch Workspace": "切换工作区",
    "Stop Bundle Chat": "停止 Bundle Chat",
    "Start Bundle Chat": "开启 Bundle Chat",
    "New Session": "新建会话",
    "Restart Agent": "重启 Agent",
    "Fork Session": "分叉会话",
    "Last Turn": "上一轮详情",
    "Session Info": "会话信息",
    "Model / Mode": "模型 / 模式",
    "Workspace Runtime": "工作区运行态",
    "Usage": "用量",
    "Agent Plan": "Agent 计划",
    "Tool Activity": "工具活动",
    "Clear Bundle": "清空上下文包",
    "Ask With Last Request": "用上次请求提问",
    "Agent Commands": "Agent 命令",
    "Workspace Files": "工作区文件",
    "Workspace Search": "工作区搜索",
    "Workspace Changes": "工作区变更",
    "Search Again": "再搜一次",
    "Try Again": "再试一次",
    "Cancel Ask": "取消提问",
    "Cancel Search": "取消搜索",
    "Cancel Command": "取消命令",
    "Current Session": "当前会话",
    "Run Session": "进入会话",
    "Rename Session": "重命名会话",
    "Delete Session": "删除会话",
    "Run+Retry Session": "进入并重试",
    "Fork+Retry Session": "进入并分叉",
    "Enter Args": "填写参数",
    "Run Command": "执行命令",
    "Current Model": "当前模型",
    "Current Mode": "当前模式",
    "Use Model": "使用模型",
    "Use Mode": "使用模式",
    "Use Model + Retry": "使用模型并重试",
    "Use Mode + Retry": "使用模式并重试",
    "Ask Agent With Visible Files": "带可见文件提问",
    "Start Bundle Chat With Visible Files": "用可见文件开启 Bundle Chat",
    "Add Visible Files to Context": "可见文件加入上下文",
    "Ask Agent With Matching Files": "带匹配文件提问",
    "Start Bundle Chat With Matching Files": "用匹配文件开启 Bundle Chat",
    "Add Matching Files to Context": "匹配文件加入上下文",
    "Ask Agent About File": "针对文件提问",
    "Start Bundle Chat With File": "用文件开启 Bundle Chat",
    "Add File to Context": "文件加入上下文",
    "Ask Agent About Change": "针对变更提问",
    "Start Bundle Chat With Change": "用变更开启 Bundle Chat",
    "Add Change to Context": "变更加入上下文",
    "Ask Agent With Current Changes": "带当前变更提问",
    "Start Bundle Chat With Changes": "用变更开启 Bundle Chat",
    "Add All Changes to Context": "全部变更加入上下文",
    "Open Workspace Changes": "打开工作区变更",
    "Reopen Model / Mode": "重新打开模型 / 模式",
    "Back": "返回",
    "Up": "上一级",
    "Prev": "上一页",
    "Next": "下一页",
}
_BUTTON_LABEL_SUBJECT_LOCALIZATIONS = {
    "Bot Status": "状态中心",
    "Context Bundle": "上下文包",
    "History": "会话历史",
    "Provider Sessions": "Provider 会话",
    "Switch Agent": "切换 Agent",
    "Switch Workspace": "切换工作区",
    "Workspace Runtime": "工作区运行态",
    "Session Info": "会话信息",
    "Agent Commands": "Agent 命令",
    "Agent Plan": "Agent 计划",
    "Tool Activity": "工具活动",
    "Folder": "文件夹",
    "File": "文件",
    "Search": "搜索结果",
    "Changes": "变更列表",
    "Change": "变更详情",
    "Change Update": "变更更新",
    "Model / Mode": "模型 / 模式",
    "Last Turn": "上一轮详情",
}


def _localized_button_subject(text: str) -> str:
    return _BUTTON_LABEL_SUBJECT_LOCALIZATIONS.get(text, text)


def _localized_button_text(text: str) -> str:
    localized = _BUTTON_LABEL_LOCALIZATIONS.get(text)
    if localized is not None:
        return localized
    if text.startswith("Current Model: "):
        return f"当前模型：{text[len('Current Model: '):]}"
    if text.startswith("Current Mode: "):
        return f"当前模式：{text[len('Current Mode: '):]}"
    if text.startswith("Current "):
        return f"当前 {text[len('Current '):]}"
    if text.startswith("Model: "):
        return f"模型：{text[len('Model: '):]}"
    if text.startswith("Mode: "):
        return f"模式：{text[len('Mode: '):]}"
    if text.startswith("Current: "):
        return f"当前：{text[len('Current: '):]}"
    if text.startswith("Switch to "):
        return f"切到 {text[len('Switch to '):]}"
    if text.startswith("Switch+Retry "):
        return f"切换并重试 {text[len('Switch+Retry '):]}"
    if text.startswith("Switch "):
        return f"切换到 {text[len('Switch '):]}"
    if text.startswith("Retry on "):
        return f"在 {text[len('Retry on '):]} 上重试"
    if text.startswith("Fork on "):
        return f"在 {text[len('Fork on '):]} 上分叉"
    if text.startswith("Back to "):
        return f"返回{_localized_button_subject(text[len('Back to '):])}"
    if text.startswith("Open Model "):
        return f"查看模型 {text[len('Open Model '):]}"
    if text.startswith("Open Mode "):
        return f"查看模式 {text[len('Open Mode '):]}"
    if text.startswith("Open File "):
        return f"打开文件 {text[len('Open File '):]}"
    if text.startswith("Open Change "):
        return f"打开变更 {text[len('Open Change '):]}"
    if text.startswith("Open "):
        subject = text[len("Open ") :]
        localized_subject = _localized_button_subject(subject)
        if localized_subject == subject and re.fullmatch(r"\d+", subject):
            return f"打开 {subject}"
        return f"打开{localized_subject}"
    if text.startswith("Run+Retry "):
        return f"执行并重试 {text[len('Run+Retry '):]}"
    if text.startswith("Fork+Retry "):
        return f"分叉并重试 {text[len('Fork+Retry '):]}"
    if text.startswith("Run "):
        return f"执行 {text[len('Run '):]}"
    if text.startswith("Fork "):
        return f"分叉 {text[len('Fork '):]}"
    if text.startswith("Delete "):
        return f"删除 {text[len('Delete '):]}"
    if text.startswith("Rename "):
        return f"重命名 {text[len('Rename '):]}"
    if text.startswith("Remove "):
        return f"移除 {text[len('Remove '):]}"
    if text.startswith("Args "):
        return f"参数 {text[len('Args '):]}"
    if text.startswith("Model+Retry: "):
        return f"模型并重试：{text[len('Model+Retry: '):]}"
    if text.startswith("Mode+Retry: "):
        return f"模式并重试：{text[len('Mode+Retry: '):]}"
    return text


def _with_cn_hint(en_text: str, cn_text: str | None = None) -> str:
    if not cn_text:
        return en_text
    return f"{cn_text}\n{en_text}"


def _view_heading(en_text: str, cn_text: str) -> str:
    return _with_cn_hint(en_text, cn_text)


def _kv_hint(
    label_en: str,
    value_en: Any,
    label_cn: str,
    value_cn: Any | None = None,
) -> str:
    cn_value = value_en if value_cn is None else value_cn
    return _with_cn_hint(
        f"{label_en}: {value_en}",
        f"{label_cn}：{cn_value}",
    )


def _cn_yes_no(value: bool) -> str:
    return "是" if value else "否"


def _cn_on_off(value: bool) -> str:
    return "开启" if value else "关闭"


_PAGED_LIST_TOTAL_LABEL_LOCALIZATIONS = {
    "Local sessions": "本地会话",
    "Prompt items": "输入项",
    "Plan items": "计划项",
    "Recent tools": "最近工具",
    "Commands": "命令数",
    "Entries": "条目数",
    "Matches": "匹配结果",
    "Changes": "变更数",
}


def _localized_total_label(text: str) -> str:
    return _PAGED_LIST_TOTAL_LABEL_LOCALIZATIONS.get(text, text)


def _log_text_snippet(text: Any, *, limit: int = LOG_TEXT_SNIPPET_LIMIT) -> str | None:
    if text is None:
        return None
    normalized = " ".join(str(text).split())
    if not normalized:
        return None
    if len(normalized) <= limit:
        return normalized
    return f"{normalized[: max(0, limit - 3)]}..."


def _message_kind_for_log(message) -> str:
    if getattr(message, "text", None):
        text = str(message.text)
        if text.startswith("/"):
            return "command"
        return "text"
    if getattr(message, "photo", None):
        return "photo"
    if getattr(message, "document", None) is not None:
        return "document"
    if getattr(message, "voice", None) is not None:
        return "voice"
    if getattr(message, "audio", None) is not None:
        return "audio"
    if getattr(message, "video", None) is not None:
        return "video"
    if getattr(message, "sticker", None) is not None:
        return "sticker"
    if getattr(message, "location", None) is not None:
        return "location"
    if getattr(message, "contact", None) is not None:
        return "contact"
    if getattr(message, "poll", None) is not None:
        return "poll"
    return "message"


def _message_log_fields(message, *, user_id: int | None = None) -> dict[str, Any]:
    chat = getattr(message, "chat", None)
    fields = {
        "user_id": user_id,
        "chat_id": getattr(message, "chat_id", None) or getattr(chat, "id", None),
        "message_id": getattr(message, "message_id", None),
        "kind": _message_kind_for_log(message),
        "text": _log_text_snippet(getattr(message, "text", None)),
        "caption": _log_text_snippet(getattr(message, "caption", None)),
        "media_group_id": getattr(message, "media_group_id", None),
    }
    return {key: value for key, value in fields.items() if value is not None}


def _update_log_fields(update: Update | Any) -> dict[str, Any]:
    user = getattr(update, "effective_user", None)
    fields = {
        "update_id": getattr(update, "update_id", None),
        "user_id": getattr(user, "id", None),
    }
    query = getattr(update, "callback_query", None)
    if query is not None:
        fields["callback_data"] = _log_text_snippet(getattr(query, "data", None))
        message = getattr(query, "message", None)
        if message is not None:
            fields.update(_message_log_fields(message, user_id=fields["user_id"]))
        fields["kind"] = "callback_query"
        return {key: value for key, value in fields.items() if value is not None}

    message = getattr(update, "message", None)
    if message is not None:
        fields.update(_message_log_fields(message, user_id=fields["user_id"]))
    return {key: value for key, value in fields.items() if value is not None}


def _runtime_log_fields(state) -> dict[str, Any]:
    return {
        "provider": getattr(state, "provider", None),
        "workspace_id": getattr(state, "workspace_id", None),
        "workspace_path": getattr(state, "workspace_path", None),
    }


def _session_log_fields(session) -> dict[str, Any]:
    return {"session_id": getattr(session, "session_id", None)}


def _log_fields_text(fields: dict[str, Any]) -> str:
    return json.dumps(
        {key: value for key, value in fields.items() if value is not None},
        ensure_ascii=True,
        sort_keys=True,
        default=str,
    )


def _log_telegram_event(
    event: str,
    *,
    level: int = logging.INFO,
    update: Update | Any | None = None,
    message=None,
    user_id: int | None = None,
    state=None,
    session=None,
    **extra: Any,
) -> None:
    fields: dict[str, Any] = {}
    if update is not None:
        fields.update(_update_log_fields(update))
    elif message is not None:
        fields.update(_message_log_fields(message, user_id=user_id))
    if state is not None:
        fields.update(_runtime_log_fields(state))
    if session is not None:
        fields.update(_session_log_fields(session))
    for key, value in extra.items():
        if value is not None:
            fields[key] = value
    logger.log(level, "telegram_%s %s", event, _log_fields_text(fields))


def _log_telegram_exception(
    event: str,
    error: BaseException,
    *,
    level: int = logging.ERROR,
    update: Update | Any | None = None,
    message=None,
    user_id: int | None = None,
    state=None,
    session=None,
    **extra: Any,
) -> None:
    fields: dict[str, Any] = {}
    if update is not None:
        fields.update(_update_log_fields(update))
    elif message is not None:
        fields.update(_message_log_fields(message, user_id=user_id))
    if state is not None:
        fields.update(_runtime_log_fields(state))
    if session is not None:
        fields.update(_session_log_fields(session))
    for key, value in extra.items():
        if value is not None:
            fields[key] = value
    logger.log(
        level,
        "telegram_%s %s",
        event,
        _log_fields_text(fields),
        exc_info=(type(error), error, error.__traceback__),
    )


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
        self._ignored_media_group_ttl_seconds = max(5.0, media_group_settle_seconds * 5.0)
        self._clock = time.monotonic if clock is None else clock
        self._actions: dict[str, _CallbackAction] = {}
        self._pending_text_actions: dict[int, _PendingTextAction] = {}
        self._agent_command_aliases: dict[int, dict[str, str]] = {}
        self._context_bundles: dict[int, _ContextBundle] = {}
        self._active_context_bundle_chats: dict[int, _ActiveContextBundleChat] = {}
        self._last_turns: dict[int, _ReplayTurn] = {}
        self._last_request_texts: dict[int, _LastRequestText] = {}
        self._media_groups: dict[tuple[int, str], _MediaGroupBuffer] = {}
        self._ignored_media_groups: dict[tuple[int, str], float] = {}
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
        self._ignored_media_groups.clear()

    def _cancel_media_group_tasks_for_user(self, user_id: int) -> None:
        for key in [item for item in self._media_groups if item[0] == user_id]:
            buffer = self._media_groups.pop(key, None)
            if buffer is not None and buffer.task is not None:
                buffer.task.cancel()
        for key in [item for item in self._ignored_media_groups if item[0] == user_id]:
            self._ignored_media_groups.pop(key, None)

    def invalidate_session_bound_interactions_for_user(self, user_id: int) -> None:
        self._prune()
        for token in [
            item_token
            for item_token, action in self._actions.items()
            if action.user_id == user_id
        ]:
            self._actions.pop(token, None)
        self._pending_text_actions.pop(user_id, None)
        self._agent_command_aliases.pop(user_id, None)
        self._cancel_media_group_tasks_for_user(user_id)

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

    def ignore_media_group(self, user_id: int, media_group_id: str) -> bool:
        self._prune()
        key = (user_id, media_group_id)
        already_ignored = key in self._ignored_media_groups
        self._ignored_media_groups[key] = (
            self._clock() + self._ignored_media_group_ttl_seconds
        )
        return not already_ignored

    def media_group_ignored(self, user_id: int, media_group_id: str) -> bool:
        self._prune()
        return (user_id, media_group_id) in self._ignored_media_groups

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
        expired_media_groups = [
            key
            for key, expires_at in self._ignored_media_groups.items()
            if expires_at <= now
        ]
        for key in expired_media_groups:
            self._ignored_media_groups.pop(key, None)


def _bind_services_ui_state(services, ui_state: TelegramUiState) -> None:
    try:
        setattr(services, "_telegram_ui_state", ui_state)
    except Exception:
        pass


def _main_menu_rows(*, include_replay_row: bool) -> list[list[str]]:
    rows = [[BUTTON_NEW_SESSION, BUTTON_BOT_STATUS]]
    if include_replay_row:
        rows.append([BUTTON_RETRY_LAST_TURN, BUTTON_FORK_LAST_TURN])
    rows.extend(
        [
            [BUTTON_WORKSPACE_SEARCH, BUTTON_CONTEXT_BUNDLE],
            [BUTTON_HELP, BUTTON_CANCEL_OR_STOP],
        ]
    )
    return rows


async def _main_menu_markup(user_id: int, services) -> ReplyKeyboardMarkup:
    include_replay_row = True
    ui_state = getattr(services, "_telegram_ui_state", None)
    if ui_state is not None:
        try:
            state = await services.snapshot_runtime_state()
        except Exception:
            state = None
        if state is not None:
            include_replay_row = (
                ui_state.get_last_turn(user_id, state.provider, state.workspace_id) is not None
            )
    return ReplyKeyboardMarkup(
        _main_menu_rows(include_replay_row=include_replay_row),
        resize_keyboard=True,
        is_persistent=True,
    )


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
            except Exception as exc:
                _log_telegram_exception(
                    "wait_for_available_commands_failed",
                    exc,
                    user_id=user_id,
                    session=session,
                )
                commands = tuple(getattr(session, "available_commands", ()) or ())
    try:
        await _sync_agent_commands_for_user(application, ui_state, user_id, commands)
    except Exception as exc:
        _log_telegram_exception(
            "sync_agent_commands_for_session_failed",
            exc,
            user_id=user_id,
            session=session,
        )
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
    except Exception as exc:
        _log_telegram_exception(
            "discover_agent_commands_failed",
            exc,
            user_id=user_id,
        )
        commands = ()
    try:
        await _sync_agent_commands_for_user(application, ui_state, user_id, commands)
    except Exception as exc:
        _log_telegram_exception(
            "sync_discovered_agent_commands_failed",
            exc,
            user_id=user_id,
        )
        pass


async def _clear_session_bound_ui_after_session_loss(
    application,
    services,
    ui_state: TelegramUiState,
    user_id: int,
) -> None:
    ui_state.invalidate_session_bound_interactions_for_user(user_id)
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
    except Exception as exc:
        _log_telegram_exception("discover_agent_commands_for_all_users_failed", exc)
        commands = ()
    for user_id in services.allowed_user_ids:
        await _sync_agent_commands_for_user(application, ui_state, user_id, commands)


async def _reply_with_menu(message, services, user_id: int, text: str, *, reply_markup=None):
    markup = await _main_menu_markup(user_id, services) if reply_markup is None else reply_markup
    await message.reply_text(text, reply_markup=markup)


def _stale_callback_recovery_text() -> str:
    return _with_cn_hint(
        "That menu is out of date. Restored the current keyboard. "
        "Open Bot Status for the latest controls, or use /start for the welcome screen.",
        "这张菜单已经过期。我已经把当前主键盘恢复给你。"
        "如果你要看最新控制项，就打开状态中心；如果你想回欢迎页，就用 /start。",
    )


async def _reply_stale_callback_recovery(query, services, user_id: int) -> None:
    message = getattr(query, "message", None)
    if message is None:
        return
    try:
        await _reply_with_menu(message, services, user_id, _stale_callback_recovery_text())
    except Exception:
        pass


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
    return _with_cn_hint(
        "Access denied. Ask the operator to allow your Telegram user ID.",
        "访问被拒绝：请联系操作者把你的 Telegram 用户 ID 加入白名单。",
    )


def _unknown_action_text() -> str:
    return _with_cn_hint(
        "This action is no longer available because that menu is out of date. "
        "Reopen the latest menu or use /start.",
        "这个动作已经失效，因为对应菜单过期了。"
        "请重新打开最新菜单，或直接使用 /start。",
    )


def _button_not_for_you_text() -> str:
    return _with_cn_hint(
        "This button belongs to another user. Reopen the menu from your own chat or use /start there.",
        "这个按钮属于另一位用户。请在你自己的聊天里重新打开菜单，或在那里使用 /start。",
    )


async def _reply_unauthorized(update: Update) -> None:
    _log_telegram_event("unauthorized", level=logging.WARNING, update=update)
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
    return _with_cn_hint(
        "Request failed. Try again, use /start, or open Bot Status.",
        "请求失败。请重试，或使用 /start / 打开状态中心继续恢复。",
    )


def _expired_button_text() -> str:
    return _with_cn_hint(
        "This button has expired because that menu is out of date. "
        "Reopen the latest menu or use /start.",
        "这个按钮已经过期，因为对应菜单不是最新的。"
        "请重新打开最新菜单，或直接使用 /start。",
    )


def _context_bundle_empty_text() -> str:
    return _with_cn_hint(
        "Context bundle is empty. Add files or changes first.",
        "上下文包当前为空。请先添加文件或工作区变更。",
    )


def _no_previous_request_text() -> str:
    return _with_cn_hint(
        "No previous request is available in this workspace yet. Send a new request first.",
        "当前工作区还没有可复用的上次请求。请先发送一条新请求。",
    )


def _no_previous_turn_text() -> str:
    return _with_cn_hint(
        "No previous turn is available yet. Send a new request first, then try again.",
        "当前还没有可复用的上一轮。请先发送一条新请求，再回来重试。",
    )


def _no_active_session_text() -> str:
    return _with_cn_hint(
        "No active session. Send text or an attachment to start one.",
        "当前没有活跃会话。直接发送文本或附件即可开始。",
    )


def _switch_session_failed_text() -> str:
    return _with_cn_hint(
        "Couldn't switch to that session. Try again, reopen Session History, or start a new session.",
        "切换到该会话失败。请重试，重新打开会话历史，或直接新建会话。",
    )


def _fork_session_failed_text() -> str:
    return _with_cn_hint(
        "Couldn't fork that session. Try again or start a new session.",
        "分叉该会话失败。请重试，或直接新建会话。",
    )


def _switch_provider_session_failed_text() -> str:
    return _with_cn_hint(
        "Couldn't switch to that provider session. Try again or reopen Provider Sessions.",
        "切换到该 Provider 会话失败。请重试，或重新打开 Provider 会话列表。",
    )


def _fork_provider_session_failed_text() -> str:
    return _with_cn_hint(
        "Couldn't fork that provider session. Try again or reopen Provider Sessions.",
        "分叉该 Provider 会话失败。请重试，或重新打开 Provider 会话列表。",
    )


def _selection_update_failed_text() -> str:
    return _with_cn_hint(
        "Couldn't update model or mode. Try again or reopen Model / Mode.",
        "更新模型或模式失败。请重试，或重新打开模型 / 模式。",
    )


def _model_mode_load_failed_text() -> str:
    return _with_cn_hint(
        "Couldn't load Model / Mode. Try again or go back to Bot Status.",
        "加载模型 / 模式失败。请重试，或返回状态中心。",
    )


def _session_creation_failed_text() -> str:
    return _with_cn_hint(
        "Couldn't start a session. Try again, use /start, or open Bot Status.",
        "启动会话失败。请重试，或使用 /start / 打开状态中心继续恢复。",
    )


def _switch_agent_failed_text() -> str:
    return _with_cn_hint(
        "Couldn't switch agent. Try again or choose another agent.",
        "切换 Agent 失败。请重试，或改选另一个 Agent。",
    )


def _switch_workspace_failed_text() -> str:
    return _with_cn_hint(
        "Couldn't switch workspace. Try again or choose another workspace.",
        "切换工作区失败。请重试，或改选另一个工作区。",
    )


def _runtime_status_refresh_failed_text() -> str:
    return _with_cn_hint(
        "Couldn't refresh Bot Status. Reopen Bot Status to confirm the latest state.",
        "刷新状态中心失败。请重新打开状态中心确认最新状态。",
    )


def _runtime_status_refresh_degraded_notice(notice: str) -> str:
    return _prefixed_notice_text(
        notice,
        _with_cn_hint(
            "Reopen Bot Status to confirm the latest state.",
            "请重新打开状态中心确认最新状态。",
        ),
    )


def _stop_turn_failed_text() -> str:
    return _with_cn_hint(
        "Couldn't stop the current turn. Try again or reopen Bot Status.",
        "停止当前回合失败。请重试，或重新打开状态中心。",
    )


def _bundle_chat_update_failed_text() -> str:
    return _with_cn_hint(
        "Couldn't update bundle chat. Reopen Bot Status and try again.",
        "更新 Bundle Chat 失败。请重新打开状态中心后再试。",
    )


def _delete_session_failed_text() -> str:
    return _with_cn_hint(
        "Couldn't delete that session. Try again or reopen Session History.",
        "删除该会话失败。请重试，或重新打开会话历史。",
    )


def _empty_media_group_text() -> str:
    return _with_cn_hint(
        "Telegram didn't deliver any usable attachments from that album. "
        "Send the album again. Nothing was sent to the agent.",
        "Telegram 这次没有从相册里送达任何可用附件。"
        "请重新发送这个相册；当前没有任何内容发给 agent。",
    )


def _unsupported_attachment_for_turn_text() -> str:
    return _with_cn_hint(
        "This attachment type can't be sent in this chat flow. Send a photo, document, audio, "
        "voice note, or video instead, use /help for supported flows, or use /start to reopen "
        "the main keyboard. Nothing was sent to the agent.",
        "当前聊天流里不支持这类附件。请改发图片、文档、音频、语音或视频；"
        "需要查看支持流程请用 /help，想恢复主键盘请用 /start。"
        "这次没有任何内容发给 agent。",
    )


def _attachment_too_large_text() -> str:
    limit_mib = ATTACHMENT_MAX_BYTES // (1024 * 1024)
    return _with_cn_hint(
        f"This attachment is larger than the {limit_mib} MiB bot limit. "
        "Send a smaller file or compress it before retrying. Nothing was sent to the agent.",
        f"这个附件超过了 bot 的 {limit_mib} MiB 大小限制。"
        "请压缩后重发，或换一个更小的文件；这次没有任何内容发给 agent。",
    )


def _workspace_fallback_save_failed_text() -> str:
    return _with_cn_hint(
        "Couldn't save the attachment into the current workspace for fallback handling. "
        "Try again or send a different file if possible. Nothing was sent to the agent.",
        "无法把附件保存到当前工作区作为降级处理。"
        "请重试，或换一个文件；这次没有任何内容发给 agent。",
    )


def _saved_attachment_notice_text(
    saved_context_items: tuple[_ContextBundleItem, ...],
    *,
    recovery: bool,
) -> str:
    count = len(saved_context_items)
    if count <= 0:
        raise ValueError("saved attachment notice requires at least one context item")

    saved_summary_en = "this saved item" if count == 1 else f"these {count} saved items"
    saved_summary_cn = "这项已保存内容" if count == 1 else f"这 {count} 项已保存内容"
    if count == 1:
        lines = [
            _with_cn_hint(
                "This request did not finish, but the attachment was saved in the workspace and "
                "added to Context Bundle."
                if recovery
                else (
                    "This attachment couldn't be sent directly to the current agent, so it was "
                    "saved in the workspace and added to Context Bundle."
                ),
                "这次请求虽然没有完整结束，但这个附件已经保存到工作区，并加入了 Context Bundle。"
                if recovery
                else "这个附件暂时不能直接发给当前 Agent，所以我已把它保存到工作区，并加入了 Context Bundle。",
            )
        ]
        lines.append(
            _with_cn_hint(
                "You can continue without uploading it again."
                if recovery
                else "You can reuse it in follow-up turns.",
                "后续继续时不需要重新上传这个附件。"
                if recovery
                else "后续回合里你可以直接继续复用这个附件。",
            )
        )
    else:
        lines = [
            _with_cn_hint(
                (
                    f"The request did not finish, but these {count} attachments were saved in the "
                    "workspace and added to Context Bundle."
                )
                if recovery
                else (
                    f"These {count} attachments couldn't be sent directly to the current agent, so they "
                    "were saved in the workspace and added to Context Bundle."
                ),
                f"这次请求虽然没有完整结束，但这 {count} 个附件都已保存到工作区，并加入了 Context Bundle。"
                if recovery
                else f"这 {count} 个附件暂时不能直接发给当前 Agent，所以我已把它们保存到工作区，并加入了 Context Bundle。",
            )
        ]
        lines.append(
            _with_cn_hint(
                "You can continue without uploading them again."
                if recovery
                else "You can reuse them in follow-up turns.",
                "后续继续时不需要重新上传这些附件。"
                if recovery
                else "后续回合里你可以直接继续复用这些附件。",
            )
        )

    lines.append(_with_cn_hint("Saved items:", "已保存内容："))
    preview_items = saved_context_items[:3]
    for index, item in enumerate(preview_items, start=1):
        lines.append(f"{index}. {_context_bundle_item_label(item)}")
    remaining = count - len(preview_items)
    if remaining > 0:
        lines.append(
            _with_cn_hint(
                f"... {remaining} more {_count_noun(remaining, 'item', 'items')}",
                f"……另外还有 {remaining} 项",
            )
        )

    lines.append(
        _with_cn_hint(
            "Recommended next step: Ask Agent With Context to continue now, or start Bundle Chat "
            f"so the next plain-text message carries {saved_summary_en} automatically.",
            "建议下一步：如果你想现在继续，就直接点“带上下文提问”；如果你想让下一条纯文本自动带上"
            f"{saved_summary_cn}，就开启 Bundle Chat。",
        )
    )
    lines.append(
        _with_cn_hint(
            "Open Context Bundle if you want to inspect or trim the saved items, or open Bot "
            "Status for the full control center.",
            "如果你想检查或整理这些内容，就打开上下文包；如果你要看完整恢复入口，就回状态中心。",
        )
    )
    return "\n".join(lines)


def _saved_attachment_notice_markup(
    ui_state: TelegramUiState,
    user_id: int,
    *,
    provider: str,
    workspace_id: str,
) -> InlineKeyboardMarkup:
    bundle_chat_active = ui_state.context_bundle_chat_active(user_id, provider, workspace_id)
    return InlineKeyboardMarkup(
        [
            [
                _callback_button(
                    ui_state,
                    user_id,
                    "Ask Agent With Context",
                    "runtime_status_control",
                    target="context_bundle_ask",
                ),
                _callback_button(
                    ui_state,
                    user_id,
                    "Stop Bundle Chat" if bundle_chat_active else "Start Bundle Chat",
                    "runtime_status_stop_bundle_chat"
                    if bundle_chat_active
                    else "runtime_status_start_bundle_chat",
                ),
            ],
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
    provider: str,
    workspace_id: str,
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
            reply_markup=_saved_attachment_notice_markup(
                ui_state,
                user_id,
                provider=provider,
                workspace_id=workspace_id,
            ),
        )
    except Exception:
        pass


def _workspace_search_cancelled_text() -> str:
    return _with_cn_hint(
        "Search cancelled. Use Workspace Search to search again or open Bot Status when ready.",
        "搜索已取消。准备好后可以重新打开工作区搜索，或先回状态中心。",
    )


def _unsupported_message_subject(message) -> tuple[str, str, str]:
    if getattr(message, "sticker", None) is not None:
        return "Stickers", "aren't", "贴纸"
    if getattr(message, "location", None) is not None:
        return "Locations", "aren't", "位置"
    if getattr(message, "contact", None) is not None:
        return "Contacts", "aren't", "联系人"
    if getattr(message, "venue", None) is not None:
        return "Venues", "aren't", "地点卡片"
    if getattr(message, "poll", None) is not None:
        return "Polls", "aren't", "投票"
    if getattr(message, "animation", None) is not None:
        return "GIF 或动图", "aren't", "GIF 或动图"
    if getattr(message, "video_note", None) is not None:
        return "Video notes", "aren't", "视频圆消息"
    if getattr(message, "dice", None) is not None:
        return "Dice messages", "aren't", "骰子消息"
    return "This Telegram message type", "isn't", "这种 Telegram 消息类型"


def _unsupported_message_text(message, *, bundle_chat_active: bool) -> str:
    subject, verb, subject_cn = _unsupported_message_subject(message)
    if bundle_chat_active:
        return _with_cn_hint(
            f"{subject} {verb} supported in this chat yet. Send plain text next to keep using "
            "the current context bundle, or send a photo, document, audio, or video instead. "
            "Use /help for supported flows, or use /start to reopen the main keyboard.",
            f"当前暂不支持发送{subject_cn}。如果你想继续沿用当前上下文包，下一条请改发纯文本；"
            "或者改发图片、文档、音频或视频。需要查看支持流程请用 /help；"
            "想恢复主键盘请用 /start。",
        )
    return _with_cn_hint(
        f"{subject} {verb} supported in this chat yet. Send plain text, photo, document, "
        "audio, or video instead, use /help for supported flows, or use /start to reopen the "
        "main keyboard.",
        f"当前暂不支持发送{subject_cn}。请改发纯文本、图片、文档、音频或视频；"
        "需要查看支持流程请用 /help；想恢复主键盘请用 /start。",
    )


def _empty_text_message() -> str:
    return _with_cn_hint(
        "This message was empty after trimming whitespace. "
        "Send text or an attachment when ready. Nothing was sent to the agent.",
        "这条消息去掉空白后是空的。准备好后请发送文本或附件；"
        "这次没有任何内容发给 agent。",
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


async def _reply_blocked_by_active_turn(
    message,
    services,
    ui_state: TelegramUiState,
    *,
    user_id: int,
) -> bool:
    active_turn = ui_state.get_active_turn(user_id)
    if active_turn is None:
        return False
    await _reply_with_menu(
        message,
        services,
        user_id,
        _turn_busy_notice(active_turn),
        reply_markup=_active_turn_notice_markup(ui_state, user_id),
    )
    return True


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


def _pending_text_action_waiting_hint_cn(pending_text_action: _PendingTextAction | None) -> str:
    if pending_text_action is None:
        return "下一条直接发文本"

    action = pending_text_action.action
    if action == "rename_history":
        return "下一条发送新的会话标题"
    if action == "run_agent_command":
        return "下一条发送命令参数"
    if action == "workspace_search":
        return "下一条发送搜索词"
    if action == "workspace_file_agent_prompt":
        return "下一条发送你想围绕这个文件提的问题"
    if action == "workspace_change_agent_prompt":
        return "下一条发送你想围绕这条变更提的问题"
    if action == "context_bundle_agent_prompt":
        return "下一条发送你想围绕这份上下文包提的问题"
    if action == "context_items_agent_prompt":
        return "下一条发送你想围绕这组选定上下文提的问题"
    return "下一条发送文本"


def _waiting_for_plain_text_notice(
    pending_text_action: _PendingTextAction | None = None,
) -> str:
    if pending_text_action is None:
        return _with_cn_hint(
            "The current action is waiting for plain text. Send text or send /cancel to back "
            "out. Nothing was sent to the agent.",
            "当前动作正在等待纯文本。请继续发送文本，或用 /cancel 退出；"
            "这次没有任何内容发给 agent。",
        )
    return _with_cn_hint(
        f"{_pending_text_action_label(pending_text_action)} is waiting for plain text. "
        f"{_pending_text_action_waiting_hint(pending_text_action)}, or send /cancel to back out. "
        "Nothing was sent to the agent.",
        f"{_pending_text_action_label_cn(pending_text_action)} 正在等待纯文本。"
        f"{_pending_text_action_waiting_hint_cn(pending_text_action)}，或用 /cancel 退出；"
        "这次没有任何内容发给 agent。",
    )


def _pending_media_group_summary(stats: _PendingMediaGroupStats) -> str:
    group_label = "attachment group" if stats.group_count == 1 else "attachment groups"
    item_label = "item" if stats.item_count == 1 else "items"
    return f"{stats.group_count} {group_label} ({stats.item_count} {item_label})"


def _pending_media_group_summary_cn(stats: _PendingMediaGroupStats) -> str:
    return f"{stats.group_count} 组附件（{stats.item_count} 项）"


def _pending_media_group_status_line(stats: _PendingMediaGroupStats) -> str:
    item_label = "attachment" if stats.item_count == 1 else "attachments"
    if stats.group_count == 1:
        return _with_cn_hint(
            f"Status: collecting {stats.item_count} {item_label} from a pending Telegram album.",
            f"当前状态：正在收集这个 Telegram 相册里的 {stats.item_count} 个附件。",
        )
    return _with_cn_hint(
        "Status: collecting "
        f"{stats.item_count} {item_label} across {_pending_media_group_summary(stats)}.",
        (
            "当前状态：正在收集待发送附件，"
            f"共 {_pending_media_group_summary_cn(stats)}。"
        ),
    )


def _pending_media_group_next_step_line(stats: _PendingMediaGroupStats) -> str:
    item_label = "it" if stats.item_count == 1 else "them"
    return _with_cn_hint(
        "Recommended next step: wait for the attachments to finish collecting, or use /cancel "
        f"or Cancel / Stop to discard {item_label} before anything reaches the agent.",
        (
            "建议下一步：先等附件收齐；如果你想止损，"
            f"就在真正发给 agent 之前用 /cancel 或主键盘“取消 / 停止”丢弃{ '它' if stats.item_count == 1 else '它们'}。"
        ),
    )


def _pending_media_group_blocked_input_text(stats: _PendingMediaGroupStats) -> str:
    item_label = "it" if stats.item_count == 1 else "them"
    album_label = (
        "a pending Telegram album"
        if stats.group_count == 1
        else "pending Telegram albums"
    )
    return _with_cn_hint(
        f"Still collecting {_pending_media_group_summary(stats)} from {album_label}. "
        f"Wait for {item_label} to finish, or use /cancel or Cancel / Stop to discard the "
        "pending uploads first. This new message was not sent to the agent.",
        "待发送附件仍在收集中。"
        f"当前共 {_pending_media_group_summary_cn(stats)}；"
        "请等它们收齐，或先用 /cancel / 主键盘“取消 / 停止”把待上传内容丢弃。"
        "这条新消息没有发给 agent。",
    )


def _pending_media_group_cancelled_text(stats: _PendingMediaGroupStats) -> str:
    if stats.group_count == 1:
        return _with_cn_hint(
            "Discarded pending attachment group "
            f"({stats.item_count} {'item' if stats.item_count == 1 else 'items'}). "
            "Nothing was sent to the agent.",
            "已丢弃待发送附件组"
            f"（{stats.item_count} {'项' if stats.item_count == 1 else '项'}）。"
            "没有任何内容发给 agent。",
        )
    return _with_cn_hint(
        f"Discarded pending {_pending_media_group_summary(stats)}. Nothing was sent to the agent.",
        f"已丢弃待发送附件（共 {stats.group_count} 组、{stats.item_count} 项）。"
        "没有任何内容发给 agent。",
    )


def _cancelled_pending_input_text(
    pending_text_action: _PendingTextAction,
    *,
    nothing_sent: bool,
) -> str:
    en_text = f"Cancelled pending input: {_pending_text_action_label(pending_text_action)}."
    cn_text = f"已取消待输入：{_pending_text_action_label(pending_text_action)}。"
    if nothing_sent:
        en_text = f"{en_text} Nothing was sent to the agent."
        cn_text = f"{cn_text} 这次没有任何内容发给 agent。"
    return _with_cn_hint(en_text, cn_text)


def _stop_requested_notice_text() -> str:
    return _with_cn_hint(
        "Stop requested for the current turn.",
        "已请求停止当前回合。",
    )


def _bundle_chat_disabled_text() -> str:
    return _with_cn_hint(
        "Bundle chat disabled. New plain text messages will use the normal session again.",
        "Bundle Chat 已关闭。后续新的纯文本消息会回到普通会话。",
    )


def _bundle_chat_already_off_text() -> str:
    return _with_cn_hint(
        "Bundle chat is already off.",
        "Bundle Chat 本来就是关闭状态。",
    )


def _search_cancelled_notice_text() -> str:
    return _with_cn_hint(
        "Search cancelled.",
        "搜索已取消。",
    )


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
            return _with_cn_hint(
                f"Status: stopping {title}.",
                f"当前状态：正在停止 {title}。",
            )
        return _with_cn_hint(
            f"Status: running {title}.",
            f"当前状态：正在处理 {title}。",
        )
    if pending_text_action is not None:
        return _with_cn_hint(
            "Status: waiting for plain text for "
            f"{_pending_text_action_label(pending_text_action)}.",
            (
                "当前状态：等待你继续发送纯文本，"
                f"用于{_pending_text_action_label_cn(pending_text_action)}。"
            ),
        )
    if pending_media_group_stats is not None:
        return _pending_media_group_status_line(pending_media_group_stats)
    if bundle_chat_active and bundle_count > 0:
        item_summary = _status_item_count_summary(bundle_count) or "current bundle"
        item_summary_cn = _status_item_count_summary_cn(bundle_count) or "当前上下文包"
        return _with_cn_hint(
            "Status: bundle chat is on. "
            f"Your next plain text message will use the current context bundle ({item_summary}).",
            (
                "当前状态：Bundle Chat 已开启。"
                f"你下一条纯文本会自动带上当前上下文包（{item_summary_cn}）。"
            ),
        )
    if session is None:
        return _with_cn_hint(
            "Status: ready. Your first text or attachment will start a session.",
            "当前状态：已就绪。你发送的第一条文本或附件会自动启动会话。",
        )
    return _with_cn_hint(
        "Status: ready. The current live session is idle.",
        "当前状态：已就绪。当前 live session 正空闲，随时可以继续。",
    )


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
    entrypoint_shortcuts: bool = False,
) -> str:
    if active_turn is not None:
        return _with_cn_hint(
            "Recommended next step: wait for the reply, or use /cancel or Cancel / Stop to "
            "interrupt.",
            "建议下一步：先等回复，或用 /cancel / 主键盘“取消 / 停止”立即打断。",
        )
    if pending_text_action is not None:
        return _with_cn_hint(
            "Recommended next step: send the plain text for "
            f"{_pending_text_action_label(pending_text_action)}, or use /cancel to back out.",
            (
                "建议下一步：继续发送当前所需的纯文本，"
                f"用于{_pending_text_action_label_cn(pending_text_action)}；如果不想继续，用 /cancel 退出。"
            ),
        )
    if pending_media_group_stats is not None:
        return _pending_media_group_next_step_line(pending_media_group_stats)
    if bundle_chat_active and bundle_count > 0:
        if last_request_available:
            return _with_cn_hint(
                "Recommended next step: send plain text to continue with this bundle, or tap "
                "Bundle + Last Request to replay the previous request with the same context.",
                "建议下一步：直接发纯文本继续当前上下文，或点 Bundle + Last Request 用同一份上下文重放上一条请求。",
            )
        return _with_cn_hint(
            "Recommended next step: send plain text to continue with this bundle, or stop "
            "bundle chat if you want a normal turn.",
            "建议下一步：直接发纯文本继续当前上下文；如果想回到普通回合，先停掉 Bundle Chat。",
        )
    if bundle_count > 0:
        if last_request_available:
            return _with_cn_hint(
                "Recommended next step: tap Ask Agent With Context or Bundle + Last Request, "
                "or send a fresh request.",
                "建议下一步：优先点 Ask Agent With Context 或 Bundle + Last Request，也可以直接发一条全新请求。",
            )
        return _with_cn_hint(
            "Recommended next step: tap Ask Agent With Context, or send a fresh request.",
            "建议下一步：先点 Ask Agent With Context，或直接发送一条新请求。",
        )
    if last_request_available and last_turn_available:
        if entrypoint_shortcuts:
            return _with_cn_hint(
                "Recommended next step: use Quick actions below to run the last request again "
                "or reuse the previous turn, or send a fresh request.",
                "建议下一步：优先用下方 Quick actions 重跑上一条请求或复用上一轮，再决定是否发一条新请求。",
            )
        return _with_cn_hint(
            "Recommended next step: run the last request again from Bot Status, reuse the "
            "previous turn with Retry Last Turn / Fork Last Turn, or send a fresh request.",
            "建议下一步：去 Bot Status 里重跑上一条请求，或用 Retry / Fork Last Turn 复用上一轮；也可以直接开始新请求。",
        )
    if last_request_available:
        if session is None:
            if entrypoint_shortcuts:
                return _with_cn_hint(
                    "Recommended next step: use Quick actions below to run the last request "
                    "again, send text or an attachment, or use Workspace Search / Context "
                    "Bundle before you ask.",
                    "建议下一步：优先用下方 Quick actions 重跑上一条请求；也可以直接发文本 / 附件，或先做工作区搜索 / 整理上下文包。",
                )
            return _with_cn_hint(
                "Recommended next step: run the last request again from Bot Status, send text "
                "or an attachment, or use Workspace Search / Context Bundle before you ask.",
                "建议下一步：去 Bot Status 重跑上一条请求，或直接发文本 / 附件；如果想准备得更充分，就先用工作区搜索 / 上下文包。",
            )
        if entrypoint_shortcuts:
            return _with_cn_hint(
                "Recommended next step: use Quick actions below to run the last request again, "
                "send text or an attachment, or open Bot Status if you want files, changes, "
                "or history first.",
                "建议下一步：优先用下方 Quick actions 重跑上一条请求；如果你想先看文件、变更或历史，再打开 Bot Status。",
            )
        return _with_cn_hint(
            "Recommended next step: run the last request again from Bot Status, send text or "
            "an attachment, or open Bot Status if you want files, changes, or history first.",
            "建议下一步：去 Bot Status 重跑上一条请求，或直接发文本 / 附件；如果要先看文件、变更或历史，也还是从 Bot Status 进入。",
        )
    if last_turn_available:
        if entrypoint_shortcuts:
            return _with_cn_hint(
                "Recommended next step: send a fresh request, or reuse the previous turn from "
                "Quick actions below.",
                "建议下一步：直接发一条全新请求，或用下方 Quick actions 复用上一轮。",
            )
        return _with_cn_hint(
            "Recommended next step: send a fresh request, or reuse the previous turn with "
            "Retry Last Turn / Fork Last Turn.",
            "建议下一步：直接发一条全新请求，或用 Retry / Fork Last Turn 复用上一轮。",
        )
    if session is None:
        return _with_cn_hint(
            "Recommended next step: send text or an attachment, or use Workspace Search / "
            "Context Bundle before you ask.",
            "建议下一步：直接发文本或附件；如果你想先准备上下文，就先用工作区搜索或上下文包。",
        )
    return _with_cn_hint(
        "Recommended next step: send text or an attachment, or open Bot Status if you want "
        "files, changes, or history first.",
        "建议下一步：直接发文本或附件继续；如果你想先看文件、变更或历史，就先打开 Bot Status。",
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
    entrypoint_shortcuts: bool = False,
) -> str:
    if active_turn is not None:
        if entrypoint_shortcuts:
            return _with_cn_hint(
                "Primary controls right now: Stop Turn below, or use /cancel from chat.",
                "当前优先操作：直接点下方 Stop Turn，或在聊天里用 /cancel。",
            )
        return _with_cn_hint(
            "Primary controls right now: Stop Turn in Bot Status, or use /cancel from chat.",
            "当前优先操作：去 Bot Status 点 Stop Turn，或在聊天里用 /cancel。",
        )
    if pending_text_action is not None:
        if entrypoint_shortcuts:
            return _with_cn_hint(
                "Primary controls right now: send the expected text next, or use Cancel Pending "
                "Input below.",
                "当前优先操作：先补上这条应发送的纯文本，或直接点下方“取消待输入”。",
            )
        return _with_cn_hint(
            "Primary controls right now: send the expected text next, or use Cancel Pending Input "
            "in Bot Status.",
            "当前优先操作：先补上这条应发送的纯文本，或去 Bot Status 里点“取消待输入”。",
        )
    if pending_media_group_stats is not None:
        if entrypoint_shortcuts:
            return _with_cn_hint(
                "Primary controls right now: wait for the album to finish, or use Discard Pending "
                "Uploads below.",
                "当前优先操作：先等相册收齐，或直接点下方 Discard Pending Uploads。",
            )
        return _with_cn_hint(
            "Primary controls right now: wait for the album to finish, or use Discard Pending "
            "Uploads in Bot Status.",
            "当前优先操作：先等相册收齐，或去 Bot Status 里点 Discard Pending Uploads。",
        )
    if bundle_chat_active and bundle_count > 0:
        if last_request_available:
            if entrypoint_shortcuts:
                return _with_cn_hint(
                    "Primary controls right now: send plain text, use Bundle + Last Request "
                    "below, or stop bundle chat below.",
                    "当前优先操作：直接发纯文本、点下方 Bundle + Last Request，或直接停掉 Bundle Chat。",
                )
            return _with_cn_hint(
                "Primary controls right now: send plain text, use Bundle + Last Request, or stop "
                "bundle chat from Bot Status.",
                "当前优先操作：直接发纯文本、使用 Bundle + Last Request，或去 Bot Status 停掉 Bundle Chat。",
            )
        if entrypoint_shortcuts:
            return _with_cn_hint(
                "Primary controls right now: send plain text, Ask Agent With Context below, or "
                "stop bundle chat below.",
                "当前优先操作：直接发纯文本、点下方 Ask Agent With Context，或直接停掉 Bundle Chat。",
            )
        return _with_cn_hint(
            "Primary controls right now: send plain text, Ask Agent With Context, or stop bundle "
            "chat from Bot Status.",
            "当前优先操作：直接发纯文本、使用 Ask Agent With Context，或去 Bot Status 停掉 Bundle Chat。",
        )
    if bundle_count > 0:
        if last_request_available:
            if entrypoint_shortcuts:
                return _with_cn_hint(
                    "Primary controls right now: Ask Agent With Context, Bundle + Last Request, "
                    "or Context Bundle below.",
                    "当前优先操作：先点 Ask Agent With Context、Bundle + Last Request，或打开下方 Context Bundle。",
                )
            return _with_cn_hint(
                "Primary controls right now: Ask Agent With Context, Bundle + Last Request, or "
                "Context Bundle.",
                "当前优先操作：先用 Ask Agent With Context、Bundle + Last Request，或打开 Context Bundle。",
            )
        if entrypoint_shortcuts:
            return _with_cn_hint(
                "Primary controls right now: Ask Agent With Context or Context Bundle below.",
                "当前优先操作：先点下方 Ask Agent With Context 或 Context Bundle。",
            )
        return _with_cn_hint(
            "Primary controls right now: Ask Agent With Context or Context Bundle.",
            "当前优先操作：先用 Ask Agent With Context 或打开 Context Bundle。",
        )
    if last_request_available and last_turn_available:
        return _with_cn_hint(
            "Primary controls right now: Run Last Request, Retry Last Turn, Fork Last Turn, "
            "or send a fresh request.",
            "当前优先操作：Run Last Request、Retry Last Turn、Fork Last Turn 都可直接继续，也可以发一条全新请求。",
        )
    if last_request_available:
        if session is None:
            if entrypoint_shortcuts:
                return _with_cn_hint(
                    "Primary controls right now: Run Last Request below, send text or an "
                    "attachment, or use Workspace Search / Context Bundle first.",
                    "当前优先操作：点下方 Run Last Request，或直接发文本 / 附件；想准备上下文时先用工作区搜索 / 上下文包。",
                )
            return _with_cn_hint(
                "Primary controls right now: Run Last Request, send text or an attachment, or "
                "use Workspace Search / Context Bundle first.",
                "当前优先操作：Run Last Request、直接发文本 / 附件都可以；想准备上下文时先用工作区搜索 / 上下文包。",
            )
        if entrypoint_shortcuts:
            return _with_cn_hint(
                "Primary controls right now: Run Last Request below, send text or an attachment, "
                "or open Bot Status for files, changes, and context prep.",
                "当前优先操作：点下方 Run Last Request，或直接发文本 / 附件；如果要先看文件、变更或准备上下文，就打开 Bot Status。",
            )
        return _with_cn_hint(
            "Primary controls right now: Run Last Request, send text or an attachment, or "
            "open Bot Status for files, changes, and context prep.",
            "当前优先操作：Run Last Request、直接发文本 / 附件都可以；如果要先看文件、变更或准备上下文，就打开 Bot Status。",
        )
    if last_turn_available:
        if entrypoint_shortcuts:
            return _with_cn_hint(
                "Primary controls right now: Retry Last Turn, Fork Last Turn, or send a fresh "
                "request.",
                "当前优先操作：Retry Last Turn、Fork Last Turn 都能继续，也可以直接发一条新请求。",
            )
        return _with_cn_hint(
            "Primary controls right now: Retry Last Turn, Fork Last Turn, or send a fresh request.",
            "当前优先操作：Retry Last Turn、Fork Last Turn 都能继续，也可以直接发一条新请求。",
        )
    if session is None:
        return _with_cn_hint(
            "Primary controls right now: send text or an attachment, or use Workspace Search "
            "/ Context Bundle first.",
            "当前优先操作：直接发文本 / 附件，或先用工作区搜索 / 上下文包准备上下文。",
        )
    return _with_cn_hint(
        "Primary controls right now: send text or an attachment, or open Bot Status for files, "
        "changes, and context prep.",
        "当前优先操作：直接发文本 / 附件，或先打开 Bot Status 查看文件、变更和上下文准备入口。",
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

    lines = ["Resume snapshot:", "恢复快照：这里列出当前 workspace 里可直接继续复用的内容。"]
    if last_request is not None:
        lines.append(
            _with_cn_hint(
                f"Last request: {_status_text_snippet(last_request.text, limit=120) or '[empty]'}",
                f"上次请求：{_status_text_snippet(last_request.text, limit=120) or '[empty]'}",
            )
        )
        lines.append(
            _with_cn_hint(
                f"Last request source: {_last_request_source_summary(last_request)}",
                f"请求来源：{_last_request_source_summary_cn(last_request)}",
            )
        )
        lines.append(
            _with_cn_hint(
                "Replay text only: "
                + _last_request_replay_note(
                    last_request=last_request,
                    current_provider=provider,
                ),
                "仅重放文本："
                + _last_request_replay_note_cn(
                    last_request=last_request,
                    current_provider=provider,
                ),
            )
        )
    if last_turn is not None:
        replay_snippet = _status_text_snippet(last_turn.title_hint) or "untitled turn"
        lines.append(
            _with_cn_hint(
                f"Last turn replay: available ({replay_snippet})",
                f"上一轮回放：可用（{replay_snippet}）",
            )
        )
        lines.append(
            _with_cn_hint(
                "Replay full payload: "
                + _last_turn_replay_note(
                    replay_turn=last_turn,
                    current_provider=provider,
                ),
                "完整回放："
                + _last_turn_replay_note_cn(
                    replay_turn=last_turn,
                    current_provider=provider,
                ),
            )
        )
    if bundle_count > 0:
        bundle_summary = _status_item_count_summary(bundle_count) or "current bundle"
        bundle_summary_cn = _status_item_count_summary_cn(bundle_count) or "当前上下文包"
        if bundle_chat_active:
            lines.append(
                _with_cn_hint(
                    "Context bundle ready: "
                    f"{bundle_summary}; bundle chat is on, so your next plain text message will include it.",
                    f"上下文包已就绪：{bundle_summary_cn}；Bundle Chat 已开启，你下一条纯文本会自动带上它。",
                )
            )
        else:
            lines.append(
                _with_cn_hint(
                    "Context bundle ready: "
                    f"{bundle_summary}; use Context Bundle or Bot Status to send it with your next request.",
                    f"上下文包已就绪：{bundle_summary_cn}；下一次提问前可从 Context Bundle 或 Bot Status 里把它带上。",
                )
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


def _status_primary_action_guide_entry(
    *,
    active_turn: _ActiveTurn | None,
    pending_text_action: _PendingTextAction | None,
    pending_media_group_stats: _PendingMediaGroupStats | None,
    bundle_count: int,
    bundle_chat_active: bool,
    last_request_available: bool,
    last_turn_available: bool,
) -> tuple[str, str, str] | None:
    if active_turn is not None:
        return (
            "Stop Turn",
            "interrupts the request that's already running without leaving Bot Status.",
            "会在不离开状态中心的前提下，立即打断当前正在运行的请求。",
        )
    if pending_text_action is not None:
        return (
            "Cancel Pending Input",
            "clears the waiting plain-text action before you choose another path.",
            "会先清掉当前等待中的纯文本输入，再让你改走别的路径。",
        )
    if pending_media_group_stats is not None:
        return (
            "Discard Pending Uploads",
            "drops the still-collecting Telegram album before anything reaches the agent.",
            "会在附件组真正发给 Agent 之前，直接丢弃仍在收集中的 Telegram 相册。",
        )
    if bundle_count > 0:
        labels = []
        if last_request_available:
            labels.append("Bundle + Last Request")
        labels.append("Ask Agent With Context")
        labels.append("Stop Bundle Chat" if bundle_chat_active else "Start Bundle Chat")
        if last_request_available:
            return (
                _join_label_series(labels),
                "let you keep working with the current bundle, either by reusing the saved request or by sending fresh text.",
                "让你围绕当前上下文包继续工作，可以复用已保存请求，也可以直接发送新文本。",
            )
        return (
            _join_label_series(labels),
            "let you start a bundled turn right away or control whether the next plain-text message carries that bundle automatically.",
            "让你立刻带着上下文包发起新回合，或控制下一条纯文本是否自动携带这份上下文。",
        )
    if last_request_available and last_turn_available:
        return (
            _join_label_series(["Run Last Request", "Retry Last Turn", "Fork Last Turn"]),
            "let you choose between replaying only the saved text or restoring the full saved payload.",
            "让你在“只重跑已保存文本”和“恢复完整保存 payload”之间快速做选择。",
        )
    if last_request_available:
        return (
            "Run Last Request",
            "replays only the saved request text in the current provider and workspace.",
            "只会在当前 Provider 和工作区里重跑已保存的请求文本。",
        )
    if last_turn_available:
        return (
            _join_label_series(["Retry Last Turn", "Fork Last Turn"]),
            "let you replay the full saved payload in the current or a forked live session.",
            "让你在当前 live session 或新分叉的 live session 里重放完整保存 payload。",
        )
    return None


def _status_navigation_action_guide_entry(*, is_admin: bool) -> tuple[str, str, str]:
    labels = ["Refresh", "Session History"]
    if is_admin:
        labels.append("Provider Sessions")
    return (
        _join_label_series(labels),
        "let you refresh this snapshot or open saved sessions when you want to resume existing work.",
        "让你刷新当前快照，或在想接回已有工作时打开已保存会话。",
    )


def _status_lifecycle_action_guide_entry(*, can_fork_session: bool) -> tuple[str, str, str]:
    labels = ["New Session", "Restart Agent"]
    if can_fork_session:
        labels.append("Fork Session")
        summary = "give you a clean path when you want to reset, restart, or branch the current session."
        cn_summary = "在你想重置、重启，或从当前会话分出新分支时，给你一条更干净的继续路径。"
    else:
        summary = "give you a clean path when you want to reset or restart the current session."
        cn_summary = "在你想重置或重启当前会话时，给你一条更干净的继续路径。"
    return _join_label_series(labels), summary, cn_summary


def _status_tuning_action_guide_entry(*, live_session_available: bool) -> tuple[str, str, str]:
    if live_session_available:
        return (
            _join_label_series(["Model / Mode", "Agent Commands"]),
            "let you adjust the live session setup or run agent-exposed commands without leaving the control center.",
            "让你不离开控制中心，就能调整 live session 的模型 / 模式，或执行 Agent 暴露的命令。",
        )
    return (
        _join_label_series(["Model / Mode", "Agent Commands"]),
        "open the live-session tuning and command surfaces once a session is available.",
        "在 live session 可用之后，打开模型 / 模式调优与命令入口。",
    )


def _status_inspection_action_guide_entry(
    *,
    usage_available: bool,
    last_request_available: bool,
    last_turn_available: bool,
    plan_count: int,
    tool_activity_count: int,
) -> tuple[str, str, str]:
    labels = ["Session Info", "Workspace Runtime"]
    if usage_available:
        labels.append("Usage")
    if last_request_available:
        labels.append("Last Request")
    if last_turn_available:
        labels.append("Last Turn")
    if plan_count > 0:
        labels.append("Agent Plan")
    if tool_activity_count > 0:
        labels.append("Tool Activity")
    return (
        _join_label_series(labels),
        "keep you in read-only views while you inspect runtime state, saved replays, plans, or recent tool use.",
        "让你在只读视图里排查运行态、回放数据、计划和近期工具活动，不会误触发新会话。",
    )


def _status_workspace_action_guide_entry() -> tuple[str, str, str]:
    return (
        _join_label_series(
            ["Workspace Files", "Workspace Search", "Workspace Changes", "Context Bundle"]
        ),
        "open focused workspace surfaces so you can browse local context or carry it into the next turn.",
        "打开工作区专项视图，方便你浏览本地上下文，或把它们带入下一轮提问。",
    )


def _status_admin_switch_action_guide_entry() -> tuple[str, str, str]:
    return (
        _join_label_series(["Switch Agent", "Switch Workspace"]),
        "change the shared runtime for every Telegram user, so treat them as global admin controls.",
        "会改动所有 Telegram 用户共用的运行时，所以必须当作全局管理员开关来使用。",
    )


def _status_action_guide_entries(
    *,
    active_turn: _ActiveTurn | None,
    pending_text_action: _PendingTextAction | None,
    pending_media_group_stats: _PendingMediaGroupStats | None,
    bundle_count: int,
    bundle_chat_active: bool,
    last_request_available: bool,
    last_turn_available: bool,
    is_admin: bool,
    can_fork_session: bool,
    live_session_available: bool,
    usage_available: bool,
    plan_count: int,
    tool_activity_count: int,
) -> tuple[tuple[str, ...], ...]:
    entries: list[tuple[str, ...]] = []
    primary_entry = _status_primary_action_guide_entry(
        active_turn=active_turn,
        pending_text_action=pending_text_action,
        pending_media_group_stats=pending_media_group_stats,
        bundle_count=bundle_count,
        bundle_chat_active=bundle_chat_active,
        last_request_available=last_request_available,
        last_turn_available=last_turn_available,
    )
    if primary_entry is not None:
        entries.append(primary_entry)
    entries.append(_status_navigation_action_guide_entry(is_admin=is_admin))
    entries.append(_status_lifecycle_action_guide_entry(can_fork_session=can_fork_session))
    entries.append(_status_tuning_action_guide_entry(live_session_available=live_session_available))
    entries.append(
        _status_inspection_action_guide_entry(
            usage_available=usage_available,
            last_request_available=last_request_available,
            last_turn_available=last_turn_available,
            plan_count=plan_count,
            tool_activity_count=tool_activity_count,
        )
    )
    entries.append(_status_workspace_action_guide_entry())
    if is_admin:
        entries.append(_status_admin_switch_action_guide_entry())
    return tuple(entries)


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
    localized_labels = [
        {
            "Last Request": "上次请求",
            "Last Turn": "上一轮回放",
            "Context Bundle": "上下文包",
        }.get(label, label)
        for label in labels
    ]
    localized_summary = "、".join(localized_labels[:-1])
    if len(localized_labels) == 1:
        localized_summary = localized_labels[0]
    elif len(localized_labels) == 2:
        localized_summary = f"{localized_labels[0]} 和 {localized_labels[1]}"
    else:
        localized_summary = f"{localized_summary} 和 {localized_labels[-1]}"
    return _with_cn_hint(
        f"Reusable in this workspace: {_join_label_series(labels)}.",
        f"当前工作区仍可复用：{localized_summary}。",
    )


def _workspace_recovery_actions(
    *,
    ui_state: TelegramUiState,
    user_id: int,
    provider: str,
    workspace_id: str,
    back_target: str,
    empty_recommendation: str | None = None,
) -> tuple[list[str], list[list[InlineKeyboardButton]]]:
    last_request = ui_state.get_last_request(user_id, workspace_id)
    last_turn = ui_state.get_last_turn(user_id, provider, workspace_id)
    bundle = ui_state.get_context_bundle(user_id, provider, workspace_id)
    bundle_count = 0 if bundle is None else len(bundle.items)
    bundle_chat_active = ui_state.context_bundle_chat_active(user_id, provider, workspace_id)
    restore_back_target = "status" if back_target == "none" else back_target

    has_last_request = last_request is not None
    has_last_turn = last_turn is not None
    recommendation = _workspace_recovery_next_step_line(
        has_last_request=has_last_request,
        has_last_turn=has_last_turn,
        bundle_count=bundle_count,
        bundle_chat_active=bundle_chat_active,
        empty_recommendation=empty_recommendation,
    )

    if not has_last_request and not has_last_turn and bundle_count <= 0:
        return (
            [recommendation],
            [],
        )

    lines = []
    reuse_summary = _workspace_reuse_summary_line(
        ui_state=ui_state,
        user_id=user_id,
        provider=provider,
        workspace_id=workspace_id,
    )
    if reuse_summary is not None:
        lines.append(reuse_summary)
    lines.append(recommendation)
    lines.append(
        _with_cn_hint(
            "Recovery options:",
            "恢复选项：下面这些按钮就是当前 workspace 里还能直接继续工作的最短路径。",
        )
    )

    buttons: list[list[InlineKeyboardButton]] = []
    if last_request is not None:
        lines.append(
            _with_cn_hint(
                "Run Last Request reuses the saved text in the current provider and workspace, "
                "starting a live session if needed.",
                "Run Last Request 会在当前 Provider 和工作区里复用已保存文本；如果需要，也会自动重新拉起 live session。",
            )
        )
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
    if last_turn is not None:
        lines.append(
            _with_cn_hint(
                "Retry / Fork Last Turn can rebuild the saved payload in this workspace.",
                "Retry / Fork Last Turn 可以在当前工作区里重建保存下来的上一轮 payload。",
            )
        )
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
    if bundle_count > 0:
        bundle_summary = _status_item_count_summary(bundle_count) or "current bundle"
        if last_request is not None:
            lines.append(
                _with_cn_hint(
                    "Context bundle ready: "
                    f"{bundle_summary}. Ask Agent With Context waits for your next plain-text message, "
                    "and Bundle + Last Request reuses the saved text with that bundle.",
                    f"上下文包已就绪：{bundle_summary}。Ask Agent With Context 会等待你下一条纯文本，"
                    "Bundle + Last Request 则会用这份上下文包复用已保存请求。",
                )
            )
        else:
            lines.append(
                _with_cn_hint(
                    "Context bundle ready: "
                    f"{bundle_summary}. Ask Agent With Context waits for your next plain-text message "
                    "and uses that bundle.",
                    f"上下文包已就绪：{bundle_summary}。Ask Agent With Context 会等待你下一条纯文本，"
                    "并自动带上这份上下文包。",
                )
            )
        if bundle_chat_active:
            lines.append(
                _with_cn_hint(
                    "Bundle chat is already on, so a fresh plain text message would include that bundle automatically.",
                    "Bundle Chat 已开启，所以你直接发送新的纯文本消息时也会自动带上这份上下文包。",
                )
            )
            buttons.append(
                [
                    _callback_button(
                        ui_state,
                        user_id,
                        "Stop Bundle Chat",
                        "runtime_status_stop_bundle_chat",
                    )
                ]
            )
        else:
            lines.append(
                _with_cn_hint(
                    "Start Bundle Chat keeps this bundle attached to later plain-text messages until you stop it.",
                    "Start Bundle Chat 会让后续纯文本持续携带这份上下文包，直到你主动停掉它。",
                )
            )
            buttons.append(
                [
                    _callback_button(
                        ui_state,
                        user_id,
                        "Start Bundle Chat",
                        "runtime_status_start_bundle_chat",
                    )
                ]
            )
        bundle_buttons = [
            _callback_button(
                ui_state,
                user_id,
                "Ask Agent With Context",
                "runtime_status_control",
                target="context_bundle_ask",
            )
        ]
        if last_request is not None:
            bundle_buttons.append(
                _callback_button(
                    ui_state,
                    user_id,
                    "Bundle + Last Request",
                    "runtime_status_control",
                    target="context_bundle_ask_last_request",
                )
            )
        else:
            bundle_buttons.append(
                _callback_button(
                    ui_state,
                    user_id,
                    "Open Context Bundle",
                    "runtime_status_open",
                    target="bundle",
                    back_target=restore_back_target,
                )
            )
        buttons.append(bundle_buttons)
        if last_request is not None:
            buttons.append(
                [
                    _callback_button(
                        ui_state,
                        user_id,
                        "Open Context Bundle",
                        "runtime_status_open",
                        target="bundle",
                        back_target=restore_back_target,
                    )
                ]
            )
    return lines, buttons


def _workspace_recovery_next_step_line(
    *,
    has_last_request: bool,
    has_last_turn: bool,
    bundle_count: int,
    bundle_chat_active: bool,
    empty_recommendation: str | None = None,
) -> str:
    if bundle_chat_active and bundle_count > 0:
        if has_last_request:
            return _with_cn_hint(
                "Recommended next step: send plain text to continue with the current bundle, or "
                "use Bundle + Last Request if you want to replay the saved request with it.",
                "建议下一步：直接发纯文本继续当前上下文包，或用 Bundle + Last Request 重放已保存请求。",
            )
        return _with_cn_hint(
            "Recommended next step: send plain text to continue with the current bundle, or Ask "
            "Agent With Context if you want to keep reusing it deliberately.",
            "建议下一步：直接发纯文本继续当前上下文包，或用 Ask Agent With Context 更明确地带着它继续。",
        )
    if has_last_request and bundle_count > 0:
        if has_last_turn:
            return _with_cn_hint(
                "Recommended next step: use Bundle + Last Request to reuse the saved request with "
                "the current bundle, or Retry / Fork Last Turn if you need the saved payload back.",
                "建议下一步：先用 Bundle + Last Request 把已保存请求和当前上下文包一起复用；如果你需要完整 payload，再用 Retry / Fork Last Turn。",
            )
        return _with_cn_hint(
            "Recommended next step: use Bundle + Last Request to reuse the saved request with the "
            "current bundle, or Ask Agent With Context if you want to send new text with it.",
            "建议下一步：先用 Bundle + Last Request 复用已保存请求；如果你想带着当前上下文包发新问题，就用 Ask Agent With Context。",
        )
    if has_last_turn and bundle_count > 0:
        return _with_cn_hint(
            "Recommended next step: Retry / Fork Last Turn if you need the saved payload back, or "
            "Ask Agent With Context to keep working with the current bundle.",
            "建议下一步：如果你需要完整 payload，就用 Retry / Fork Last Turn；如果想带着当前上下文包继续，就用 Ask Agent With Context。",
        )
    if has_last_request and has_last_turn:
        return _with_cn_hint(
            "Recommended next step: Run Last Request if the saved text is enough, or Retry / Fork "
            "Last Turn if you need the full saved payload.",
            "建议下一步：如果保存下来的文本已经够用，就点 Run Last Request；如果你要完整 payload，就用 Retry / Fork Last Turn。",
        )
    if has_last_turn:
        return _with_cn_hint(
            "Recommended next step: Retry / Fork Last Turn to reuse the saved payload, or send a "
            "fresh request if you want a clean turn.",
            "建议下一步：用 Retry / Fork Last Turn 复用已保存 payload；如果你想彻底开新问题，就直接发送新请求。",
        )
    if has_last_request:
        return _with_cn_hint(
            "Recommended next step: Run Last Request to reuse the saved text, or send a fresh "
            "request if you want to branch.",
            "建议下一步：用 Run Last Request 复用已保存文本；如果你想走一条新分支，就直接发送新请求。",
        )
    if bundle_count > 0:
        return _with_cn_hint(
            "Recommended next step: Ask Agent With Context to keep working with the current "
            "bundle, or open Context Bundle to review it first.",
            "建议下一步：用 Ask Agent With Context 带着当前上下文继续，或先打开 Context Bundle 复查内容。",
        )
    return empty_recommendation or _with_cn_hint(
        "Recommended next step: send text or an attachment from chat to start a live session, or "
        "use the buttons below to go back.",
        "建议下一步：直接发送文本或附件启动 live session，或用下方按钮回到其他入口。",
    )


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
            _with_cn_hint(
                "Bundle chat is still on, so your next plain text message will include the current context bundle.",
                "Bundle Chat 仍处于开启状态，所以下一条纯文本会自动带上当前上下文包。",
            )
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
        _with_cn_hint(
            "Main keyboard focus: New Session and Bot Status first, then Retry / Fork Last Turn.",
            "主键盘优先保留高频入口：先新建会话和打开状态中心，再处理上一轮的重试 / 分叉。",
        ),
        _with_cn_hint(
            "Context prep row: Workspace Search and Context Bundle stay one tap away before you ask.",
            "上下文准备单独占一行：在真正发问前，工作区搜索和上下文包都保持一跳可达。",
        ),
        _with_cn_hint(
            (
                "Advanced actions live in Bot Status: Session History, Model / Mode, Agent "
                "Commands, Workspace Files/Changes, Restart Agent, and admin-only runtime "
                "switches."
            ),
            "高阶动作统一收进 Bot Status：会话历史、模型 / 模式、Agent 命令、工作区文件 / 变更、重启 Agent，以及管理员专用的运行态切换都不再塞进常驻键盘。",
        ),
        _with_cn_hint(
            (
                "Recovery row: Help and Cancel / Stop stay on the keyboard, and /start, /status, "
                "/help, and /cancel still work if Telegram hides it."
            ),
            "恢复行固定保留帮助和取消 / 停止；即使 Telegram 把主键盘折叠了，/start、/status、/help、/cancel 也始终可用。",
        ),
    ]
    if is_admin:
        lines.append(
            _with_cn_hint(
                "Admin-only shared-runtime switches live in Bot Status so they stay reachable "
                "without turning the persistent keyboard into a dangerous control surface.",
                "管理员专用的共享运行态切换仍放在 Bot Status，既保证可达，也避免把常驻键盘做成高风险控制面板。",
            )
        )
    return lines


def _start_quick_path_lines() -> list[str]:
    return [
        _with_cn_hint(
            "1. Ask right now: send plain text or an attachment.",
            "1. 现在就提问：直接发送文本或附件。",
        ),
        _with_cn_hint(
            "2. Prepare context first: use Workspace Search or Context Bundle.",
            "2. 先准备上下文：用工作区搜索或上下文包。",
        ),
        _with_cn_hint(
            (
                "3. Recover or branch work: open Bot Status for Last Request, Last Turn, "
                "history, model / mode, and session actions."
            ),
            "3. 恢复或分叉已有工作：打开 Bot Status 查看 Last Request、Last Turn、历史、模型 / 模式和会话动作。",
        ),
    ]


def _help_common_task_lines() -> list[str]:
    return [
        _with_cn_hint(
            "1. Ask a fresh question: send text or an attachment.",
            "1. 发起一条新问题：直接发送文本或附件。",
        ),
        _with_cn_hint(
            (
                "2. Prepare reusable local context: use Workspace Search or Workspace Files / "
                "Changes, then keep it in Context Bundle if you want to reuse it."
            ),
            "2. 准备可复用的本地上下文：先用工作区搜索或工作区文件 / 变更，再把需要反复使用的内容放进 Context Bundle。",
        ),
        _with_cn_hint(
            "3. Replay only the saved request text: Run Last Request.",
            "3. 只重跑上一条请求文本：使用 Run Last Request。",
        ),
        _with_cn_hint(
            (
                "4. Replay the full saved turn payload: Retry Last Turn. Use Fork Last Turn to do "
                "that in a new session."
            ),
            "4. 重放上一整轮 payload：使用 Retry Last Turn；如果想另开一条分支，就用 Fork Last Turn。",
        ),
        _with_cn_hint(
            (
                "5. Recover, inspect, or switch setup: Bot Status for history, model / mode, "
                "agent commands, new session, and restart."
            ),
            "5. 做恢复、检查或调整设置：统一从 Bot Status 进入历史、模型 / 模式、Agent 命令、新建会话和重启。",
        ),
    ]


def _help_core_concept_lines() -> list[str]:
    return [
        _with_cn_hint(
            "Context Bundle keeps selected files, changes, and fallback attachments ready across turns.",
            "Context Bundle 会把你选中的文件、变更和降级保存的附件持续保留，供后续多轮复用。",
        ),
        _with_cn_hint(
            (
                "Bundle chat means your next plain text message will automatically include the "
                "current context bundle until you stop it."
            ),
            "Bundle Chat 的意思是：从现在起，你后续发送的纯文本都会自动带上当前上下文包，直到你主动停掉它。",
        ),
    ]


def _session_ready_notice_text(*, extra_lines: tuple[str, ...] = ()) -> str:
    lines = [
        _with_cn_hint(
            (
                "You're ready for the next request. Old bot buttons and pending inputs tied to "
                "the previous session were cleared."
            ),
            "已为下一次请求准备就绪。上一会话遗留的旧按钮和待输入状态都已清理。",
        )
    ]
    lines.extend(line for line in extra_lines if line)
    return "\n".join(lines)


def _new_session_success_text(
    session_id: str,
    *,
    extra_lines: tuple[str, ...] = (),
) -> str:
    return (
        _with_cn_hint(
            f"Started new session: {session_id}",
            f"已新建会话：{session_id}",
        )
        + "\n"
        + _session_ready_notice_text(extra_lines=extra_lines)
    )


def _restart_agent_success_text(
    session_id: str,
    *,
    extra_lines: tuple[str, ...] = (),
) -> str:
    return (
        _with_cn_hint(
            f"Restarted agent: {session_id}",
            f"已重启 Agent：{session_id}",
        )
        + "\n"
        + _session_ready_notice_text(extra_lines=extra_lines)
    )


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

    provider_label = resolve_provider_profile(provider).display_name

    lines = [
        _with_cn_hint(
            f"Welcome to Talk2Agent for {provider_label} in {workspace_label}.",
            f"欢迎使用 {workspace_label} 中的 Talk2Agent（{provider_label}）。",
        ),
        _with_cn_hint(
            f"Workspace ID: {workspace_id}",
            f"当前工作区：{workspace_label}（ID: {workspace_id}）。",
        ),
        _with_cn_hint(
            "This entry page does not create a session implicitly. It helps you resume work, "
            "recover controls, or prepare context before you ask.",
            "欢迎页说明：这里不会隐式创建新会话，而是优先帮你接回上一段工作、找回控制入口，或先准备上下文。",
        ),
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
            entrypoint_shortcuts=True,
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
            entrypoint_shortcuts=True,
        ),
        "",
    ]

    if session is None:
        lines.append(
            _with_cn_hint(
                "Session: none yet. Your first text or attachment will start one.",
                "会话概览：还没有 live session；你发出的第一条文本或附件会自动开始。",
            )
        )
    else:
        lines.append(
            _with_cn_hint(
                f"Session: {session.session_id or 'pending'}",
                f"会话概览：当前 live session 为 {session.session_id or 'pending'}，除非你主动新建或重启，否则会继续沿用。",
            )
        )
        session_title = _status_text_snippet(getattr(session, "session_title", None), limit=120)
        if session_title is not None:
            lines.append(
                _with_cn_hint(
                    f"Session title: {session_title}",
                    f"会话标题：{session_title}",
                )
            )

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

    lines.append(
        _with_cn_hint(
            f"Pending input: {_pending_text_action_label(pending_text_action)}",
            f"待输入状态：{_pending_text_action_label_cn(pending_text_action)}",
        )
    )
    if pending_media_group_stats is not None:
        pending_upload_summary = _pending_media_group_summary(pending_media_group_stats)
        lines.append(
            _with_cn_hint(
                f"Pending uploads: {pending_upload_summary}",
                f"待上传附件：{_pending_media_group_summary_cn(pending_media_group_stats)}",
            )
        )

    if bundle_count == 0:
        lines.append(_with_cn_hint("Context bundle: empty", "上下文包：当前为空。"))
    else:
        bundle_chat_state = "bundle chat on" if bundle_chat_active else "bundle chat off"
        lines.append(
            _with_cn_hint(
                f"Context bundle: {bundle_count} item{'s' if bundle_count != 1 else ''} ({bundle_chat_state})",
                (
                    "上下文包："
                    f"{bundle_count} 项（Bundle Chat 已{_cn_on_off(bundle_chat_active)}）。"
                ),
            )
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
    lines.append(
        _with_cn_hint(
            "Quick paths:",
            "快速路径：如果你不确定先点哪里，就按下面三条最短路径走。",
        )
    )
    lines.extend(_start_quick_path_lines())
    lines.append("")
    lines.append(
        _with_cn_hint(
            "Keyboard layout:",
            "主键盘说明：主键盘只保留手机端最高频动作，避免把整屏都占满。",
        )
    )
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

    provider_label = resolve_provider_profile(provider).display_name

    lines = [
        _with_cn_hint(
            f"Talk2Agent help for {provider_label} in {workspace_label}.",
            f"帮助页：{workspace_label} 中 {provider_label} 的快速使用说明。",
        ),
        _with_cn_hint(
            f"Workspace ID: {workspace_id}",
            f"当前工作区：{workspace_label}（ID: {workspace_id}）。",
        ),
        _with_cn_hint(
            "Use this page when you're new here, forgot the terms, or just want to confirm the "
            "next step quickly.",
            "页面用途：第一次使用、忘了术语，或不确定下一步怎么走时，都先回来这里。",
        ),
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
            entrypoint_shortcuts=True,
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
            entrypoint_shortcuts=True,
        ),
        "",
    ]

    if session is None:
        lines.append(
            _with_cn_hint(
                "Session: none yet. Send text or an attachment to start one.",
                "会话概览：当前还没有 live session，直接发文本或附件即可开始。",
            )
        )
    else:
        lines.append(
            _with_cn_hint(
                f"Session: {session.session_id or 'pending'}",
                f"会话概览：当前 live session 为 {session.session_id or 'pending'}；帮助页只做说明，不会改动它。",
            )
        )

    lines.extend(_status_active_turn_lines(active_turn))
    lines.append(
        _with_cn_hint(
            f"Pending input: {_pending_text_action_label(pending_text_action)}",
            f"待输入状态：{_pending_text_action_label_cn(pending_text_action)}",
        )
    )
    if pending_media_group_stats is not None:
        pending_upload_summary = _pending_media_group_summary(pending_media_group_stats)
        lines.append(
            _with_cn_hint(
                f"Pending uploads: {pending_upload_summary}",
                f"待上传附件：{_pending_media_group_summary_cn(pending_media_group_stats)}",
            )
        )
    lines.append(
        _with_cn_hint(
            f"Context bundle: {bundle_count} item{'s' if bundle_count != 1 else ''}",
            f"上下文包：{bundle_count} 项。",
        )
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
    lines.append(
        _with_cn_hint(
            "Common tasks:",
            "常见任务：先按目标选路径，不必先把所有运行时细节看完。",
        )
    )
    lines.extend(_help_common_task_lines())
    lines.append("")
    lines.append(
        _with_cn_hint(
            "Core concepts:",
            "核心概念：下面这两个词最影响你之后的恢复方式和上下文复用。",
        )
    )
    lines.extend(_help_core_concept_lines())
    lines.append("")
    lines.append(
        _with_cn_hint(
            "Keyboard:",
            "主键盘：高频按钮常驻；低频但重要的高级动作统一放进 Bot Status。",
        )
    )
    lines.extend(_main_keyboard_priority_lines(is_admin=is_admin))
    lines.append("")
    lines.append(
        _with_cn_hint(
            "Recovery:",
            "恢复提醒：这些 slash 命令在 Telegram 折叠主键盘时依然可靠。",
        )
    )
    lines.append(
        _with_cn_hint(
            "/start restores the welcome screen and the full keyboard.",
            "/start 会恢复欢迎页和完整主键盘。",
        )
    )
    lines.append(
        _with_cn_hint(
            "/status opens Bot Status even when the keyboard is hidden.",
            "/status 会在主键盘被折叠时直接打开状态中心。",
        )
    )
    lines.append(
        _with_cn_hint(
            "Help or /help reopens this guide without changing the current session.",
            "帮助按钮或 /help 会重新打开这份指南，但不会改动当前会话。",
        )
    )
    lines.append(
        _with_cn_hint(
            "Cancel / Stop or /cancel backs out of pending input, stops a running turn, or leaves "
            "bundle chat.",
            "取消 / 停止或 /cancel 会退出待输入、打断运行中回合，或离开 Bundle Chat。",
        )
    )

    return "\n".join(lines)


def _entrypoint_quick_actions_view(
    *,
    provider: str,
    workspace_id: str,
    user_id: int,
    ui_state: TelegramUiState,
) -> tuple[str, InlineKeyboardMarkup] | None:
    active_turn = ui_state.get_active_turn(
        user_id,
        provider=provider,
        workspace_id=workspace_id,
    )
    if active_turn is not None:
        title = _status_text_snippet(active_turn.title_hint, limit=120) or "current request"
        return (
            _with_cn_hint(
                "Quick actions for the current turn:\n"
                f"{title} is still running. Stop it here, or open Bot Status to watch progress.",
                (
                    "当前回合快捷操作：\n"
                    f"{title} 仍在运行。如果你现在最想止损，就直接在这里停掉；如果想先看运行进度，再进状态中心。"
                ),
            ),
            _active_turn_notice_markup(ui_state, user_id),
        )

    pending_text_action = ui_state.get_pending_text_action(user_id)
    if pending_text_action is not None:
        return (
            _with_cn_hint(
                "Quick actions for pending input:\n"
                f"{_pending_text_action_label(pending_text_action)} is waiting for plain text. "
                "Cancel it here, or send the expected text next.",
                (
                    "待输入快捷操作：\n"
                    f"{_pending_text_action_label(pending_text_action)} 正在等你补充纯文本。你可以直接取消，也可以立刻补上这条消息。"
                ),
            ),
            _pending_input_notice_markup(ui_state, user_id),
        )

    pending_media_group_stats = ui_state.pending_media_group_stats(user_id)
    if pending_media_group_stats is not None:
        return (
            _with_cn_hint(
                "Quick actions for pending uploads:\n"
                f"{_pending_media_group_summary(pending_media_group_stats)} is still collecting. "
                "Discard it here, or open Bot Status for the full runtime view.",
                (
                    "待上传附件快捷操作：\n"
                    f"{_pending_media_group_summary(pending_media_group_stats)} 仍在收集中；如果不想继续，现在就丢弃，否则去状态中心看完整运行态。"
                ),
            ),
            _pending_uploads_notice_markup(ui_state, user_id),
        )

    last_request = ui_state.get_last_request(user_id, workspace_id)
    last_turn = ui_state.get_last_turn(user_id, provider, workspace_id)
    bundle = ui_state.get_context_bundle(user_id, provider, workspace_id)
    bundle_count = 0 if bundle is None else len(bundle.items)
    bundle_chat_active = ui_state.context_bundle_chat_active(user_id, provider, workspace_id)
    if last_request is None and last_turn is None and bundle_count <= 0:
        return None

    lines = [
        _with_cn_hint(
            "Quick actions for getting back to work:",
            "恢复快捷操作：这里优先放继续上一段工作的最短路径，完整控制台仍在状态中心。",
        )
    ]
    rows: list[tuple[tuple[str, str, dict[str, Any]], ...]] = []

    if last_turn is not None:
        lines.append(
            _with_cn_hint(
                "Retry / Fork Last Turn replays the full saved payload in the current workspace.",
                "Retry / Fork Last Turn：会在当前工作区里重放整轮保存下来的 payload。",
            )
        )
        rows.append(
            (
                ("Retry Last Turn", "runtime_status_control", {"target": "retry_last_turn"}),
                ("Fork Last Turn", "runtime_status_control", {"target": "fork_last_turn"}),
            )
        )

    if last_request is not None:
        lines.append(
            _with_cn_hint(
                "Run Last Request replays only the saved request text.",
                "Run Last Request：只会重跑保存下来的请求文本，不会自动带回原附件或原上下文。",
            )
        )
        if bundle_count > 0:
            rows.append(
                (
                    ("Run Last Request", "runtime_status_control", {"target": "run_last_request"}),
                    (
                        "Bundle + Last Request",
                        "runtime_status_control",
                        {"target": "context_bundle_ask_last_request"},
                    ),
                )
            )
        else:
            rows.append(
                (("Run Last Request", "runtime_status_control", {"target": "run_last_request"}),)
            )

    if bundle_count > 0:
        lines.append(
            _with_cn_hint(
                "Ask Agent With Context waits for your next plain-text question and adds the current context bundle.",
                "Ask Agent With Context：会等待你输入下一条纯文本问题，并自动附上当前上下文包。",
            )
        )
        rows.append(
            (
                ("Ask Agent With Context", "runtime_status_control", {"target": "context_bundle_ask"}),
                ("Open Context Bundle", "runtime_status_open", {"target": "bundle"}),
            )
        )
        if bundle_chat_active:
            lines.append(
                _with_cn_hint(
                    "Bundle chat is already on, so the next plain text message will include that bundle automatically.",
                    "Bundle Chat 已开启，所以你下一条纯文本会自动带上当前上下文包。",
                )
            )
            rows.append((("Stop Bundle Chat", "runtime_status_stop_bundle_chat", {}),))
        else:
            lines.append(
                _with_cn_hint(
                    "Start Bundle Chat if you want later plain-text messages to keep carrying that bundle until you stop it.",
                    "如果你想让后续纯文本持续携带这份上下文包，直到你主动停掉，就开启 Bundle Chat。",
                )
            )
            rows.append((("Start Bundle Chat", "runtime_status_start_bundle_chat", {}),))

    lines.append(
        _with_cn_hint(
            "Open Bot Status if you need history, files, changes, model / mode, or the full control center.",
            "如果你需要历史、文件、变更、模型 / 模式，或更完整的控制视图，就打开状态中心。",
        )
    )
    rows.append((("Open Bot Status", "runtime_status_page", {}),))

    return "\n".join(lines), _inline_notice_markup(ui_state, user_id, *rows)


async def _reply_entrypoint_quick_actions(
    message,
    *,
    provider: str,
    workspace_id: str,
    user_id: int,
    ui_state: TelegramUiState,
) -> None:
    view = _entrypoint_quick_actions_view(
        provider=provider,
        workspace_id=workspace_id,
        user_id=user_id,
        ui_state=ui_state,
    )
    if view is None:
        return
    text, markup = view
    await message.reply_text(text, reply_markup=markup)


def _post_cancel_fallback_view(
    ui_state: TelegramUiState,
    user_id: int,
) -> tuple[str, InlineKeyboardMarkup]:
    return (
        _with_cn_hint(
            "Quick actions for getting back to work:\n"
            "Send text or an attachment when ready, open Bot Status for the full control center, "
            "or start a New Session if you want a clean slate.",
            (
                "恢复快捷操作：\n"
                "准备好后就直接发文本或附件；如果你需要完整控制台，就打开状态中心；如果你想彻底重来，就新建会话。"
            ),
        ),
        _inline_notice_markup(
            ui_state,
            user_id,
            (
                ("Open Bot Status", "runtime_status_page", {}),
                ("New Session", "recover_new_session", {}),
            ),
        ),
    )


def _stop_requested_follow_up_view(
    ui_state: TelegramUiState,
    user_id: int,
) -> tuple[str, InlineKeyboardMarkup]:
    return (
        _with_cn_hint(
            "Quick action while the turn winds down:\n"
            "Open Bot Status to watch the stop request and confirm when the session is ready again.",
            (
                "停止请求已发出：\n"
                "打开状态中心查看停止过程，并在会话重新可用时确认下一步。"
            ),
        ),
        _status_only_notice_markup(ui_state, user_id),
    )


async def _reply_cancel_follow_up(
    message,
    services,
    ui_state: TelegramUiState,
    *,
    user_id: int,
    cancelled_pending_text_action: _PendingTextAction | None = None,
    cancelled_pending_uploads: bool = False,
    stop_requested: bool = False,
) -> None:
    view: tuple[str, InlineKeyboardMarkup] | None = None
    if stop_requested:
        view = _stop_requested_follow_up_view(ui_state, user_id)
    elif (
        cancelled_pending_text_action is not None
        and cancelled_pending_text_action.action == "workspace_search"
        and not cancelled_pending_uploads
    ):
        view = (
            _workspace_search_cancelled_text(),
            _workspace_search_cancelled_markup(ui_state, user_id),
        )
    else:
        try:
            state = await services.snapshot_runtime_state()
        except Exception:
            state = None
        if state is not None:
            view = _entrypoint_quick_actions_view(
                provider=state.provider,
                workspace_id=state.workspace_id,
                user_id=user_id,
                ui_state=ui_state,
            )
    if view is None:
        view = _post_cancel_fallback_view(ui_state, user_id)

    text, markup = view
    try:
        await message.reply_text(text, reply_markup=markup)
    except Exception:
        pass


async def handle_start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    services,
    ui_state: TelegramUiState,
) -> None:
    del context
    _bind_services_ui_state(services, ui_state)

    if update.message is None:
        return
    _log_telegram_event("start_received", update=update)
    if not _is_authorized(update, services):
        await _reply_unauthorized(update)
        return

    user_id = update.effective_user.id
    try:
        state, session = await _with_active_store(
            services,
            lambda store: store.peek(user_id),
        )
    except Exception as exc:
        _log_telegram_exception("start_failed", exc, update=update)
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
    await _reply_entrypoint_quick_actions(
        update.message,
        provider=state.provider,
        workspace_id=state.workspace_id,
        user_id=user_id,
        ui_state=ui_state,
    )


async def handle_help(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    services,
    ui_state: TelegramUiState,
) -> None:
    del context
    _bind_services_ui_state(services, ui_state)

    if update.message is None:
        return
    _log_telegram_event("help_received", update=update)
    if not _is_authorized(update, services):
        await _reply_unauthorized(update)
        return

    user_id = update.effective_user.id
    try:
        state, session = await _with_active_store(
            services,
            lambda store: store.peek(user_id),
        )
    except Exception as exc:
        _log_telegram_exception("help_failed", exc, update=update)
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
    await _reply_entrypoint_quick_actions(
        update.message,
        provider=state.provider,
        workspace_id=state.workspace_id,
        user_id=user_id,
        ui_state=ui_state,
    )


async def handle_status(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    services,
    ui_state: TelegramUiState,
) -> None:
    del context
    _bind_services_ui_state(services, ui_state)
    _log_telegram_event("status_received", update=update)
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
    _bind_services_ui_state(services, ui_state)
    if update.message is None:
        return
    _log_telegram_event("cancel_received", update=update)
    if not _is_authorized(update, services):
        await _reply_unauthorized(update)
        return

    user_id = update.effective_user.id
    pending_text_action = ui_state.clear_pending_text_action(user_id)
    pending_media_group_stats = ui_state.cancel_pending_media_groups(user_id)
    if pending_text_action is not None or pending_media_group_stats is not None:
        notice_parts = []
        if pending_text_action is not None:
            notice_parts.append(
                _cancelled_pending_input_text(
                    pending_text_action,
                    nothing_sent=pending_media_group_stats is None,
                )
            )
        if pending_media_group_stats is not None:
            notice_parts.append(_pending_media_group_cancelled_text(pending_media_group_stats))
        await _reply_with_menu(
            update.message,
            services,
            user_id,
            " ".join(notice_parts),
        )
        await _reply_cancel_follow_up(
            update.message,
            services,
            ui_state,
            user_id=user_id,
            cancelled_pending_text_action=pending_text_action,
            cancelled_pending_uploads=pending_media_group_stats is not None,
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
        except Exception as exc:
            _log_telegram_exception(
                "cancel_stop_requested_failed",
                exc,
                update=update,
                session=active_turn.session,
            )
            await _reply_request_failed(update, services)
            return
        await _reply_with_menu(
            update.message,
            services,
            user_id,
            _with_cn_hint(
                "Stop requested for the current turn. Open Bot Status to track progress.",
                "已请求停止当前回合。你可以打开状态中心继续观察停止进度。",
            ),
        )
        await _reply_cancel_follow_up(
            update.message,
            services,
            ui_state,
            user_id=user_id,
            stop_requested=True,
        )
        return

    try:
        state = await services.snapshot_runtime_state()
    except Exception as exc:
        if ui_state.resolve_agent_command(user_id, CANCEL_COMMAND) is not None:
            await handle_agent_command(update, context, services, ui_state)
            return
        _log_telegram_exception("cancel_runtime_snapshot_failed", exc, update=update)
        await _reply_request_failed(update, services)
        return

    if ui_state.context_bundle_chat_active(user_id, state.provider, state.workspace_id):
        ui_state.disable_context_bundle_chat(user_id)
        await _reply_with_menu(
            update.message,
            services,
            user_id,
            _bundle_chat_disabled_text(),
        )
        await _reply_cancel_follow_up(
            update.message,
            services,
            ui_state,
            user_id=user_id,
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
    await _reply_cancel_follow_up(
        update.message,
        services,
        ui_state,
        user_id=user_id,
    )


async def handle_text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    services,
    ui_state: TelegramUiState,
) -> None:
    _bind_services_ui_state(services, ui_state)
    if update.message is None:
        return
    _log_telegram_event("text_received", update=update)
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
        except Exception as exc:
            _log_telegram_exception("pending_text_action_failed", exc, update=update)
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

    if await _reply_blocked_by_active_turn(
        update.message,
        services,
        ui_state,
        user_id=user_id,
    ):
        return

    stripped_text = text.strip()
    if not stripped_text:
        await _reply_with_menu(
            update.message,
            services,
            user_id,
            _empty_text_message(),
        )
        return

    try:
        state = await services.snapshot_runtime_state()
    except Exception as exc:
        _log_telegram_exception("text_runtime_snapshot_failed", exc, update=update)
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
                _with_cn_hint(
                    "Context bundle chat was turned off because the current bundle is empty.",
                    "当前上下文包已经为空，所以 Bundle Chat 已自动关闭。",
                ),
            )
            return

        ui_state.set_last_request_text(
            user_id,
            state.workspace_id,
            stripped_text,
            provider=state.provider,
            source_summary=_last_request_bundle_chat_source_summary(len(bundle.items)),
        )
        await _run_agent_prompt_turn_on_message(
            update.message,
            user_id,
            services,
            ui_state,
            _context_bundle_agent_prompt(tuple(bundle.items), stripped_text),
            title_hint=stripped_text,
            application=None if context is None else context.application,
        )
        return

    ui_state.set_last_request_text(
        user_id,
        state.workspace_id,
        stripped_text,
        provider=state.provider,
        source_summary=_last_request_plain_text_source_summary(),
    )
    await _run_agent_text_turn(
        update,
        services,
        ui_state,
        stripped_text,
        application=None if context is None else context.application,
    )


async def handle_attachment(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    services,
    ui_state: TelegramUiState,
) -> None:
    _bind_services_ui_state(services, ui_state)
    if update.message is None:
        return
    _log_telegram_event("attachment_received", update=update)
    if not _is_authorized(update, services):
        await _reply_unauthorized(update)
        return

    user_id = update.effective_user.id
    media_group_id = getattr(update.message, "media_group_id", None)
    if media_group_id:
        media_group_id = str(media_group_id)
        if ui_state.media_group_ignored(user_id, media_group_id):
            return
        pending_text_action = ui_state.get_pending_text_action(user_id)
        if pending_text_action is not None:
            if ui_state.ignore_media_group(user_id, media_group_id):
                await _reply_with_menu(
                    update.message,
                    services,
                    user_id,
                    _waiting_for_plain_text_notice(pending_text_action),
                    reply_markup=_pending_input_notice_markup(ui_state, user_id),
                )
            return
        active_turn = ui_state.get_active_turn(user_id)
        if active_turn is not None:
            if ui_state.ignore_media_group(user_id, media_group_id):
                await _reply_with_menu(
                    update.message,
                    services,
                    user_id,
                    _turn_busy_notice(active_turn),
                    reply_markup=_active_turn_notice_markup(ui_state, user_id),
                )
            return
        _queue_media_group_attachment(
            message=update.message,
            user_id=user_id,
            media_group_id=media_group_id,
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

    if await _reply_blocked_by_active_turn(
        update.message,
        services,
        ui_state,
        user_id=user_id,
    ):
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
    except Exception as exc:
        _log_telegram_exception("attachment_prompt_build_failed", exc, update=update)
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
    _bind_services_ui_state(services, ui_state)

    if update.message is None:
        return
    _log_telegram_event("unsupported_message_received", update=update)
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
    except Exception as exc:
        _log_telegram_exception(
            "unsupported_message_runtime_snapshot_failed",
            exc,
            update=update,
            level=logging.WARNING,
        )
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
    _bind_services_ui_state(services, ui_state)
    if update.message is None:
        return
    _log_telegram_event("agent_command_received", update=update)
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
    _log_telegram_event("debug_status_received", update=update)
    if not _is_authorized(update, services):
        await _reply_unauthorized(update)
        return

    try:
        state, session = await _with_active_store(
            services,
            lambda store: store.peek(update.effective_user.id),
        )
    except Exception as exc:
        _log_telegram_exception("debug_status_failed", exc, update=update)
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
    except Exception as exc:
        _log_telegram_exception("status_view_failed", exc, update=update)
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
        workspace_id=state.workspace_id,
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
            notice=_with_cn_hint(
                "MCP server is no longer available in this workspace runtime.",
                "这个 MCP server 在当前工作区运行时里已经不可用了。",
            ),
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
        workspace_id=state.workspace_id,
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
            notice=_with_cn_hint(
                "Selected replay item is no longer available.",
                "你刚选中的重放条目已经不可用了。",
            ),
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
        workspace_id=state.workspace_id,
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
            notice=_with_cn_hint(
                "Selected plan entry is no longer available.",
                "你刚选中的计划项已经不可用了。",
            ),
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
        workspace_id=state.workspace_id,
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
            notice=_with_cn_hint(
                "Selected tool activity is no longer available.",
                "你刚选中的工具活动已经不可用了。",
            ),
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


def _pending_text_action_label_cn(pending_text_action: _PendingTextAction | None) -> str:
    if pending_text_action is None:
        return "无"

    action = pending_text_action.action
    payload = pending_text_action.payload
    if action == "rename_history":
        return _status_summary_with_details(
            "重命名会话标题",
            _status_text_snippet(str(payload.get("session_id", ""))),
        )
    if action == "run_agent_command":
        return _status_summary_with_details(
            "填写命令参数",
            _agent_command_name(str(payload.get("command_name", "command"))),
        )
    if action == "workspace_search":
        return "工作区搜索"
    if action == "workspace_file_agent_prompt":
        return _status_summary_with_details(
            "针对工作区文件提问",
            _status_text_snippet(str(payload.get("relative_path", ""))),
        )
    if action == "workspace_change_agent_prompt":
        return _status_summary_with_details(
            "针对工作区变更提问",
            _status_text_snippet(str(payload.get("relative_path", ""))),
        )
    if action == "context_bundle_agent_prompt":
        items = payload.get("items")
        item_count = len(items) if isinstance(items, (list, tuple)) else 0
        return _status_summary_with_details(
            "针对上下文包提问",
            _status_item_count_summary_cn(item_count),
        )
    if action == "context_items_agent_prompt":
        items = payload.get("items")
        item_count = len(items) if isinstance(items, (list, tuple)) else 0
        return _status_summary_with_details(
            "针对选定上下文提问",
            _status_text_snippet(str(payload.get("prompt_label", ""))),
            _status_item_count_summary_cn(item_count),
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
        return _with_cn_hint(
            "Another request is already running. "
            "Send /cancel to stop it, open Bot Status to inspect progress, or wait for it to "
            "finish. This new message was not sent to the agent.",
            "当前已有另一条请求在运行。"
            "你可以用 /cancel 停止它，打开状态中心查看进度，或等它自然结束。"
            "这条新消息没有发给 agent。",
        )
    title = _status_text_snippet(active_turn.title_hint) or "current request"
    return _with_cn_hint(
        f"Another request is already running ({title}). "
        "Send /cancel to stop it, open Bot Status to inspect progress, or wait for it to finish. "
        "This new message was not sent to the agent.",
        f"当前已有另一条请求在运行（{title}）。"
        "你可以用 /cancel 停止它，打开状态中心查看进度，或等它自然结束。"
        "这条新消息没有发给 agent。",
    )


def _status_active_turn_lines(
    active_turn: _ActiveTurn | None,
    *,
    now: float | None = None,
) -> list[str]:
    if active_turn is None:
        return [_with_cn_hint("Turn: idle", "回合：空闲")]

    details = [_status_text_snippet(active_turn.title_hint) or "current request"]
    session_id = None if active_turn.session is None else getattr(active_turn.session, "session_id", None)
    if session_id:
        details.append(session_id)
    status = "stop requested" if active_turn.stop_requested else "running"
    status_cn = "停止中" if active_turn.stop_requested else "运行中"
    lines = [
        _with_cn_hint(
            f"Turn: {status} ({', '.join(details)})",
            f"回合：{status_cn}（{', '.join(details)}）",
        )
    ]
    if now is not None:
        elapsed = _format_elapsed_duration(now - active_turn.started_at)
        lines.append(_with_cn_hint(f"Turn elapsed: {elapsed}", f"已运行：{elapsed}"))
    return lines


def _pending_text_action_hint_line(
    pending_text_action: _PendingTextAction | None,
) -> str | None:
    if pending_text_action is None:
        return None
    waiting_hint = _pending_text_action_waiting_hint(pending_text_action)
    waiting_hint_cn = _pending_text_action_waiting_hint_cn(pending_text_action)
    return _with_cn_hint(
        f"Next plain text: {waiting_hint}.",
        f"下一条纯文本：{waiting_hint_cn}。",
    )


def _status_item_count_summary(count: int) -> str | None:
    if count <= 0:
        return None
    return f"{count} item{'s' if count != 1 else ''}"


def _status_item_count_summary_cn(count: int) -> str | None:
    if count <= 0:
        return None
    return f"{count} 项"


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


def _status_usage_summary_cn(session) -> str | None:
    if session is None:
        return None
    usage = getattr(session, "usage", None)
    if usage is None:
        return None

    parts = [f"已用 {usage.used}", f"容量 {usage.size}"]
    amount = getattr(usage, "cost_amount", None)
    currency = getattr(usage, "cost_currency", None)
    if amount is not None and currency:
        parts.append(f"费用 {amount:.2f} {currency}")
    elif amount is not None:
        parts.append(f"费用 {amount:.2f}")
    return "，".join(parts)


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


def _last_request_source_summary_cn(last_request: _LastRequestText | None) -> str:
    summary = _last_request_source_summary(last_request)
    if summary == "plain text":
        return "纯文本"
    if summary == "last request replay":
        return "上次请求回放"
    if summary.startswith("bundle chat"):
        return summary.replace("bundle chat", "Bundle Chat").replace(" items", " 项").replace(" item", " 项")
    if summary.startswith("workspace file request"):
        return "工作区文件提问" + summary[len("workspace file request") :]
    if summary.startswith("workspace change request"):
        return "工作区变更提问" + summary[len("workspace change request") :]
    if summary.startswith("selected context request"):
        detail = summary[len("selected context request") :]
        detail = detail.replace(" items", " 项").replace(" item", " 项")
        return "选定上下文提问" + detail
    if summary.startswith("context bundle request"):
        detail = summary[len("context bundle request") :]
        detail = detail.replace(" items", " 项").replace(" item", " 项")
        return "上下文包提问" + detail
    return summary


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

    lines = [
        _with_cn_hint(
            f"Agent plan: {len(entries)} item{'s' if len(entries) != 1 else ''}",
            f"Agent 计划：{len(entries)} 项",
        ),
        _with_cn_hint("Plan preview:", "计划预览："),
    ]
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
        lines.append(
            _with_cn_hint(
                f"... {remaining} more item{'s' if remaining != 1 else ''}",
                f"……另外还有 {remaining} 项",
            )
        )
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
    label_cn = {"Model": "模型", "Mode": "模式"}.get(label, label)
    return _with_cn_hint(
        f"{label}: {current_label} ({choice_count} choice{'s' if choice_count != 1 else ''})",
        f"{label_cn}：{current_label}（{choice_count} 个选项）",
    )


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


def _last_request_replay_note_cn(
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
        return f"Run Last Request 会把这段文本重新发到当前工作区里的 {current_display}。"
    return (
        f"这条请求最初记录在 {recorded_display}，但这次 Run Last Request 会把它发到"
        f"当前工作区里的 {current_display}。"
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


def _last_turn_replay_note_cn(
    *,
    replay_turn: _ReplayTurn,
    current_provider: str,
) -> str:
    current_display = _replay_provider_display_name(current_provider)
    recorded_display = _replay_provider_display_name(replay_turn.provider)
    if replay_turn.provider == current_provider:
        return (
            f"Retry Last Turn / Fork Last Turn 会把这轮保存的 payload 重放到"
            f"当前工作区里的 {current_display}。"
        )
    return (
        f"这轮 payload 最初记录在 {recorded_display}，但这次 Retry Last Turn / Fork Last Turn "
        f"会把它重放到当前工作区里的 {current_display}；如果附件能力不同，bot 会先做适配。"
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

    lines = [
        _with_cn_hint(f"Recent tools: {len(activities)}", f"最近工具：{len(activities)}"),
        _with_cn_hint("Tool preview:", "工具预览："),
    ]
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
        lines.append(
            _with_cn_hint(
                f"... {remaining} more item{'s' if remaining != 1 else ''}",
                f"……另外还有 {remaining} 项",
            )
        )
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
    lines = [_with_cn_hint("Bundle preview:", "上下文包预览：")]
    visible_items = bundle.items[:limit]
    for index, item in enumerate(visible_items, start=1):
        item_label = _status_text_snippet(_context_bundle_item_label(item))
        lines.append(f"{index}. {item_label or _context_bundle_item_label(item)}")
    remaining = len(bundle.items) - len(visible_items)
    if remaining > 0:
        lines.append(
            _with_cn_hint(
                f"... {remaining} more item{'s' if remaining != 1 else ''}",
                f"……另外还有 {remaining} 项",
            )
        )
    return lines


def _status_agent_command_preview_lines(
    commands,
    *,
    limit: int = STATUS_COMMAND_PREVIEW_LIMIT,
) -> list[str]:
    if not commands:
        return []
    lines = [_with_cn_hint("Command preview:", "命令预览：")]
    visible_commands = tuple(commands[:limit])
    for index, command in enumerate(visible_commands, start=1):
        label = _agent_command_name(command.name)
        if command.hint:
            label = f"{label} args: {command.hint}"
        lines.append(f"{index}. {_status_text_snippet(label) or label}")
    remaining = len(commands) - len(visible_commands)
    if remaining > 0:
        lines.append(
            _with_cn_hint(
                f"... {remaining} more command{'s' if remaining != 1 else ''}",
                f"……另外还有 {remaining} 条命令",
            )
        )
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


def _status_workspace_changes_summary_cn(git_status) -> str:
    if git_status is None:
        return "暂不可用"
    if not getattr(git_status, "is_git_repo", False):
        return "不是 Git 仓库"
    change_count = len(getattr(git_status, "entries", ()))
    if change_count <= 0:
        return "工作树干净"
    return f"{change_count} 条变更"


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

    lines = [_with_cn_hint("Workspace change preview:", "工作区变更预览：")]
    visible_entries = entries[:limit]
    for index, entry in enumerate(visible_entries, start=1):
        path_label = _status_text_snippet(entry.display_path)
        lines.append(
            f"{index}. [{_workspace_change_status_label(entry.status_code)}] "
            f"{path_label or entry.display_path}"
        )
    remaining = len(entries) - len(visible_entries)
    if remaining > 0:
        lines.append(
            _with_cn_hint(
                f"... {remaining} more change{'s' if remaining != 1 else ''}",
                f"……另外还有 {remaining} 条变更",
            )
        )
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
    lines = [_with_cn_hint("Recent sessions:", "最近会话：")]
    for index, entry in enumerate(entries, start=1):
        lines.append(f"{index}. {_status_recent_session_label(entry)}")
    remaining = total_count - len(entries)
    if remaining > 0:
        lines.append(
            _with_cn_hint(
                f"... {remaining} more session{'s' if remaining != 1 else ''}",
                f"……另外还有 {remaining} 条会话",
            )
        )
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
    _bind_services_ui_state(services, ui_state)
    query = update.callback_query
    if query is None:
        return
    _log_telegram_event("callback_received", update=update)
    if not _is_authorized(update, services):
        _log_telegram_event("callback_unauthorized", level=logging.WARNING, update=update)
        await query.answer(_unauthorized_text(), show_alert=True)
        return

    data = query.data or ""
    if not data.startswith(CALLBACK_PREFIX):
        await query.answer(_unknown_action_text(), show_alert=True)
        if update.effective_user is not None:
            await _reply_stale_callback_recovery(query, services, update.effective_user.id)
        return

    token = data[len(CALLBACK_PREFIX) :]
    callback_action = ui_state.get(token)
    if callback_action is None:
        await query.answer(_expired_button_text(), show_alert=True)
        if update.effective_user is not None:
            await _reply_stale_callback_recovery(query, services, update.effective_user.id)
        return
    if update.effective_user is None or callback_action.user_id != update.effective_user.id:
        await query.answer(_button_not_for_you_text(), show_alert=True)
        return

    callback_action = ui_state.pop(token)
    if callback_action is None:
        await query.answer(_expired_button_text(), show_alert=True)
        if update.effective_user is not None:
            await _reply_stale_callback_recovery(query, services, update.effective_user.id)
        return

    try:
        await _dispatch_callback_action(
            query,
            services,
            ui_state,
            callback_action,
            application=None if context is None else context.application,
        )
    except Exception as exc:
        _log_telegram_exception(
            "callback_dispatch_failed",
            exc,
            update=update,
            action=callback_action.action,
        )
        try:
            await query.answer(_request_failed_text(), show_alert=True)
        except Exception:
            pass


async def _handle_application_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    error = getattr(context, "error", None)
    if error is None:
        _log_telegram_event(
            "application_error_without_exception",
            level=logging.ERROR,
            raw_update_type=None if update is None else type(update).__name__,
        )
        return
    if isinstance(update, Update):
        _log_telegram_exception("application_error", error, update=update)
        return
    _log_telegram_exception(
        "application_error",
        error,
        raw_update_type=None if update is None else type(update).__name__,
    )


def build_telegram_application(config, services) -> Application:
    ui_state = TelegramUiState()
    _bind_services_ui_state(services, ui_state)

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
    application.add_error_handler(_handle_application_error)
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
            notice=_with_cn_hint(
                "Renamed session.",
                "会话已重命名。",
            ),
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

    if await _reply_blocked_by_active_turn(
        lead_message,
        services,
        ui_state,
        user_id=user_id,
    ):
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
            provider=state.provider,
            workspace_id=state.workspace_id,
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
            provider=turn_state.provider,
            workspace_id=turn_state.workspace_id,
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
        except Exception as exc:
            _log_telegram_exception(
                "turn_runtime_snapshot_failed",
                exc,
                message=message,
                user_id=user_id,
                title_hint=_log_text_snippet(title_hint),
            )
            if on_prepare_failure is not None:
                try:
                    await on_prepare_failure()
                    return
                except Exception as callback_exc:
                    _log_telegram_exception(
                        "turn_prepare_failure_callback_failed",
                        callback_exc,
                        message=message,
                        user_id=user_id,
                        title_hint=_log_text_snippet(title_hint),
                    )
            await message.reply_text(
                _request_failed_text(),
                reply_markup=await _main_menu_markup(user_id, services),
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
    except Exception as exc:
        _log_telegram_exception(
            "turn_prepare_failed",
            exc,
            message=message,
            user_id=user_id,
            title_hint=_log_text_snippet(title_hint),
        )
        if on_prepare_failure is not None:
            try:
                await on_prepare_failure()
                return
            except Exception as callback_exc:
                _log_telegram_exception(
                    "turn_prepare_failure_callback_failed",
                    callback_exc,
                    message=message,
                    user_id=user_id,
                    title_hint=_log_text_snippet(title_hint),
                )
        await message.reply_text(
            _request_failed_text(),
            reply_markup=await _main_menu_markup(user_id, services),
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
        _log_telegram_event(
            "turn_cancelled",
            message=message,
            user_id=user_id,
            state=state,
            session=session,
            title_hint=_log_text_snippet(title_hint),
        )
        await stream.finish(stop_reason="cancelled")
        await _invoke_turn_failure_callback(on_turn_failure)
        return
    except UnsupportedPromptContentError as exc:
        _log_telegram_exception(
            "turn_unsupported_prompt_content",
            exc,
            message=message,
            user_id=user_id,
            state=state,
            session=session,
            title_hint=_log_text_snippet(title_hint),
        )
        await stream.fail(_unsupported_prompt_content_message(state.provider, exc))
        await _invoke_turn_failure_callback(on_turn_failure)
        return
    except AttachmentPromptError as exc:
        _log_telegram_exception(
            "turn_attachment_prompt_error",
            exc,
            message=message,
            user_id=user_id,
            state=state,
            session=session,
            title_hint=_log_text_snippet(title_hint),
        )
        await stream.fail(
            str(exc),
            reply_markup=_status_only_notice_markup(ui_state, user_id),
        )
        await _invoke_turn_failure_callback(on_turn_failure)
        return
    except Exception as exc:
        _log_telegram_exception(
            "turn_runner_failed",
            exc,
            message=message,
            user_id=user_id,
            state=state,
            session=session,
            title_hint=_log_text_snippet(title_hint),
        )
        invalidate = getattr(state.session_store, "invalidate", None)
        session_lost = False
        try:
            if invalidate is not None:
                await invalidate(user_id, session)
                session_lost = True
            else:
                await session.close()
                session_lost = True
        except Exception as invalidate_exc:
            _log_telegram_exception(
                "turn_failure_cleanup_failed",
                invalidate_exc,
                message=message,
                user_id=user_id,
                state=state,
                session=session,
                title_hint=_log_text_snippet(title_hint),
                level=logging.WARNING,
            )
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
    except Exception as exc:
        _log_telegram_exception(
            "record_session_usage_failed",
            exc,
            message=message,
            user_id=user_id,
            state=state,
            session=session,
            title_hint=_log_text_snippet(title_hint),
            level=logging.WARNING,
        )

    workspace_changes_follow_up_git_status = _workspace_changes_follow_up_git_status(
        before_workspace_git_status,
        _safe_read_workspace_git_status(state.workspace_path),
    )

    final_reply_markup = None
    if workspace_changes_follow_up_git_status is None:
        final_reply_markup = _completed_turn_reply_markup(
            ui_state,
            user_id=user_id,
            provider=state.provider,
            workspace_id=state.workspace_id,
        )

    await stream.finish(
        stop_reason=response.stop_reason,
        reply_markup=final_reply_markup,
    )

    if after_success is not None:
        try:
            await after_success(state)
        except Exception as exc:
            _log_telegram_exception(
                "turn_after_success_failed",
                exc,
                message=message,
                user_id=user_id,
                state=state,
                session=session,
                title_hint=_log_text_snippet(title_hint),
                level=logging.WARNING,
            )

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
        except Exception as exc:
            _log_telegram_exception(
                "turn_after_turn_success_failed",
                exc,
                message=message,
                user_id=user_id,
                state=state,
                session=session,
                title_hint=_log_text_snippet(title_hint),
                level=logging.WARNING,
            )


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
        workspace_id=state.workspace_id,
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
        workspace_id=state.workspace_id,
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
            notice=_with_cn_hint(
                "Agent command is no longer available.",
                "这个 Agent 命令当前已经不可用了。",
            ),
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
        next_step_line=_workspace_item_preview_next_step_line(
            ask_label="Ask Agent About File",
            subject_label="this file",
            secondary_label="Add File to Context",
            secondary_summary="you want to save it for later reuse",
            has_last_request=last_request_text is not None,
        ),
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
        next_step_line=_workspace_item_preview_next_step_line(
            ask_label="Ask Agent About File",
            subject_label="this file",
            secondary_label="Add File to Context",
            secondary_summary="you want to save it for later reuse",
            has_last_request=last_request_text is not None,
        ),
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
        return _with_cn_hint(
            "No workspace changes to add.",
            "当前没有可加入上下文包的工作区变更。",
        )
    if added_count == total:
        return _with_cn_hint(
            f"Added {added_count} {_count_noun(added_count, 'change', 'changes')} to context bundle.",
            f"已将 {added_count} 项工作区变更加入上下文包。",
        )
    if added_count == 0:
        return _with_cn_hint(
            f"All {duplicate_count} {_count_noun(duplicate_count, 'change', 'changes')} "
            "are already in the context bundle.",
            f"这 {duplicate_count} 项工作区变更本来就在上下文包里。",
        )
    return _with_cn_hint(
        f"Added {added_count} {_count_noun(added_count, 'change', 'changes')} to context bundle. "
        f"{duplicate_count} {_count_noun(duplicate_count, 'change', 'changes')} "
        f"{'was' if duplicate_count == 1 else 'were'} already present.",
        f"已新增 {added_count} 项工作区变更到上下文包；另有 {duplicate_count} 项本来就在里面。",
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
        return _with_cn_hint(
            f"{base_notice} Bundle chat stays on.",
            "以上变更已就绪，Bundle Chat 会继续保持开启。",
        )
    return _with_cn_hint(
        f"{base_notice} Bundle chat enabled.",
        "以上变更已就绪，并已开启 Bundle Chat。",
    )


def _single_context_item_add_to_bundle_notice(*, item_kind: str, added: bool) -> str:
    noun = "file" if item_kind == "file" else "change"
    if added:
        return _with_cn_hint(
            f"Added {noun} to context bundle.",
            f"已将这项{'文件' if item_kind == 'file' else '变更'}加入上下文包。",
        )
    return _with_cn_hint(
        f"{noun.capitalize()} is already in the context bundle.",
        f"这项{'文件' if item_kind == 'file' else '变更'}本来就在上下文包里。",
    )


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
        return _with_cn_hint(
            f"{base_notice} Bundle chat stays on.",
            "这项内容已就绪，Bundle Chat 会继续保持开启。",
        )
    return _with_cn_hint(
        f"{base_notice} Bundle chat enabled.",
        "这项内容已就绪，并已开启 Bundle Chat。",
    )


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
        return _with_cn_hint(
            "No matching files to add.",
            "当前没有可加入上下文包的匹配文件。",
        )
    if added_count == total:
        return _with_cn_hint(
            f"Added {added_count} {_count_noun(added_count, 'file', 'files')} "
            "from search results to context bundle.",
            f"已将搜索结果里的 {added_count} 个文件加入上下文包。",
        )
    if added_count == 0:
        return _with_cn_hint(
            f"All {duplicate_count} {_count_noun(duplicate_count, 'file', 'files')} "
            "from search results are already in the context bundle.",
            f"搜索结果里的这 {duplicate_count} 个文件本来就在上下文包里。",
        )
    return _with_cn_hint(
        f"Added {added_count} {_count_noun(added_count, 'file', 'files')} "
        "from search results to context bundle. "
        f"{duplicate_count} {_count_noun(duplicate_count, 'file', 'files')} "
        f"{'was' if duplicate_count == 1 else 'were'} already present.",
        f"已将搜索结果里的 {added_count} 个文件加入上下文包；另有 {duplicate_count} 个本来就在里面。",
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
        return _with_cn_hint(
            f"{base_notice} Bundle chat stays on.",
            "以上搜索结果已就绪，Bundle Chat 会继续保持开启。",
        )
    return _with_cn_hint(
        f"{base_notice} Bundle chat enabled.",
        "以上搜索结果已就绪，并已开启 Bundle Chat。",
    )


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
        return _with_cn_hint(
            "No visible files to add.",
            "当前页没有可加入上下文包的可见文件。",
        )
    if added_count == total:
        return _with_cn_hint(
            f"Added {added_count} {_count_noun(added_count, 'file', 'files')} "
            "from workspace view to context bundle.",
            f"已将当前页的 {added_count} 个文件加入上下文包。",
        )
    if added_count == 0:
        return _with_cn_hint(
            f"All {duplicate_count} visible {_count_noun(duplicate_count, 'file', 'files')} "
            f"{'is' if duplicate_count == 1 else 'are'} already in the context bundle.",
            f"当前页这 {duplicate_count} 个可见文件本来就在上下文包里。",
        )
    return _with_cn_hint(
        f"Added {added_count} {_count_noun(added_count, 'file', 'files')} "
        "from workspace view to context bundle. "
        f"{duplicate_count} {_count_noun(duplicate_count, 'file', 'files')} "
        f"{'was' if duplicate_count == 1 else 'were'} already present.",
        f"已将当前页的 {added_count} 个文件加入上下文包；另有 {duplicate_count} 个本来就在里面。",
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
        return _with_cn_hint(
            f"{base_notice} Bundle chat stays on.",
            "以上文件已就绪，Bundle Chat 会继续保持开启。",
        )
    return _with_cn_hint(
        f"{base_notice} Bundle chat enabled.",
        "以上文件已就绪，并已开启 Bundle Chat。",
    )


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
    bundle = ui_state.get_context_bundle(user_id, provider, workspace_id)
    bundle_count = 0 if bundle is None else len(bundle.items)
    bundle_chat_active = ui_state.context_bundle_chat_active(user_id, provider, workspace_id)
    provider_label = resolve_provider_profile(provider).display_name

    lines = [
        _view_heading(
            f"Request recovery for {provider_label} in {workspace_label}",
            f"失败恢复：{workspace_label} 中的 {provider_label}",
        ),
        _with_cn_hint(
            "The current live session closed before this request finished. "
            "Use the recovery assets saved in this workspace to keep going.",
            "当前 live session 已在这次请求完成前关闭。你仍可以利用这个工作区里保存下来的恢复资产，直接接回工作。",
        ),
    ]
    reuse_summary = _workspace_reuse_summary_line(
        ui_state=ui_state,
        user_id=user_id,
        provider=provider,
        workspace_id=workspace_id,
    )
    if reuse_summary is not None:
        lines.append(reuse_summary)
    if last_turn is not None:
        lines.append(
            _with_cn_hint(
                "Recommended first step: Retry Last Turn to rerun the previous request, or open "
                "Bot Status if you want to inspect runtime and history first.",
                "建议第一步：优先用 Retry Last Turn 重放上一轮；如果你想先检查运行态和历史，再打开状态中心。",
            )
        )
    elif last_request is not None and bundle_count > 0:
        lines.append(
            _with_cn_hint(
                "Recommended first step: use Bundle + Last Request to reuse the saved request "
                "with the current bundle, or Run Last Request if the bundle is no longer needed.",
                "建议第一步：优先用 Bundle + Last Request 带着当前上下文包复用已保存请求；如果这份上下文已经不需要了，再单独用 Run Last Request。",
            )
        )
    elif last_request is not None:
        lines.append(
            _with_cn_hint(
                "Recommended first step: Run Last Request to replay the saved request text, or "
                "open Bot Status if you want to inspect runtime and history first.",
                "建议第一步：先用 Run Last Request 重放已保存请求文本；如果你想先检查运行态和历史，再打开状态中心。",
            )
        )
    elif bundle_count > 0:
        lines.append(
            _with_cn_hint(
                "Recommended first step: Ask Agent With Context to keep working with the current "
                "bundle, or open Context Bundle if you want to inspect it first.",
                "建议第一步：先用 Ask Agent With Context 带着当前上下文继续；如果你想先确认内容，再打开 Context Bundle。",
            )
        )
    else:
        lines.append(
            _with_cn_hint(
                "Recommended first step: Open Bot Status to inspect runtime and history, or "
                "start a New Session if you want a clean slate.",
                "建议第一步：先打开状态中心确认运行态和历史；如果你想彻底重来，就新建会话。",
            )
        )
    if last_request is not None:
        lines.append(
            _with_cn_hint(
                f"Last request: {_status_text_snippet(last_request.text, limit=120) or '[empty]'}",
                f"上次请求：{_status_text_snippet(last_request.text, limit=120) or '[empty]'}",
            )
        )
        lines.append(
            _with_cn_hint(
                f"Last request source: {_last_request_source_summary(last_request)}",
                f"请求来源：{_last_request_source_summary_cn(last_request)}",
            )
        )
    if bundle_count > 0:
        bundle_summary = _status_item_count_summary(bundle_count) or "current bundle"
        bundle_summary_cn = _status_item_count_summary_cn(bundle_count) or "当前上下文包"
        if last_request is not None:
            lines.append(
                _with_cn_hint(
                    "Context bundle ready: "
                    f"{bundle_summary}. Bundle + Last Request reuses the saved text with it, "
                    "and Ask Agent With Context waits for your next plain-text message.",
                    "上下文包已就绪："
                    f"{bundle_summary_cn}。Bundle + Last Request 会带着它复用已保存请求，"
                    "Ask Agent With Context 则会等待你输入下一条纯文本问题。",
                )
            )
        else:
            lines.append(
                _with_cn_hint(
                    "Context bundle ready: "
                    f"{bundle_summary}. Ask Agent With Context waits for your next plain-text message.",
                    "上下文包已就绪："
                    f"{bundle_summary_cn}。Ask Agent With Context 会等待你输入下一条纯文本问题。",
                )
            )
        if bundle_chat_active:
            lines.append(
                _with_cn_hint(
                    "Bundle chat is already on, so a fresh plain text message would include that "
                    "bundle automatically.",
                    "Bundle Chat 已开启，所以你直接发送新的纯文本时也会自动带上这份上下文包。",
                )
            )
        else:
            lines.append(
                _with_cn_hint(
                    "Start Bundle Chat if you want later plain-text messages to keep carrying "
                    "this bundle until you stop it.",
                    "如果你想让后续纯文本持续携带这份上下文包，直到你主动停掉，就开启 Bundle Chat。",
                )
            )
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
    elif bundle_count > 0:
        primary_buttons.append(
            _callback_button(
                ui_state,
                user_id,
                "Ask Agent With Context",
                "runtime_status_control",
                target="context_bundle_ask",
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
    if bundle_count > 0:
        if last_request is not None or last_turn is not None:
            bundle_buttons = [
                _callback_button(
                    ui_state,
                    user_id,
                    "Ask Agent With Context",
                    "runtime_status_control",
                    target="context_bundle_ask",
                )
            ]
        else:
            bundle_buttons = []
        if last_request is not None:
            bundle_buttons.append(
                _callback_button(
                    ui_state,
                    user_id,
                    "Bundle + Last Request",
                    "runtime_status_control",
                    target="context_bundle_ask_last_request",
                )
            )
        elif not bundle_buttons:
            bundle_buttons.append(
                _callback_button(
                    ui_state,
                    user_id,
                    "Open Context Bundle",
                    "runtime_status_open",
                    target="bundle",
                )
            )
        if bundle_buttons:
            buttons.append(bundle_buttons)
        buttons.append(
            [
                _callback_button(
                    ui_state,
                    user_id,
                    "Open Context Bundle",
                    "runtime_status_open",
                    target="bundle",
                ),
                _callback_button(
                    ui_state,
                    user_id,
                    "Stop Bundle Chat" if bundle_chat_active else "Start Bundle Chat",
                    "runtime_status_stop_bundle_chat"
                    if bundle_chat_active
                    else "runtime_status_start_bundle_chat",
                ),
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
    lines = [
        _with_cn_hint(
            f"Actions: Run {run_summary}.",
            f"操作说明：Run 会{run_summary}。",
        )
    ]
    if can_fork:
        lines.append(
            _with_cn_hint(
                "Fork creates a new live session branched from it.",
                "Fork 会基于它创建一个新的 live session 分支。",
            )
        )
    if can_retry_last_turn:
        retry_labels = "Run+Retry / Fork+Retry" if can_fork else "Run+Retry"
        lines.append(
            _with_cn_hint(
                f"{retry_labels} also replay the previous turn immediately after the switch.",
                f"{retry_labels} 还会在切换后立刻重放上一轮。",
            )
        )
    return lines


def _session_collection_next_step_line(
    *,
    can_fork: bool,
    can_retry_last_turn: bool,
) -> str:
    if can_retry_last_turn:
        retry_labels = "Run+Retry / Fork+Retry" if can_fork else "Run+Retry"
        return _with_cn_hint(
            "Recommended next step: open the session you want to inspect first, or use "
            f"{retry_labels} when you already know you want that session plus the previous turn.",
            f"建议下一步：先打开你想看的会话；如果你已经确定要切过去并立刻带上上一轮，就直接用 {retry_labels}。",
        )
    if can_fork:
        return _with_cn_hint(
            "Recommended next step: open the session you want to inspect first, or tap Run / "
            "Fork on the right one when you already know where to continue.",
            "建议下一步：先打开你想看的会话；如果你已经知道接下来要在哪条线上继续，就直接点对应的 Run / Fork。",
        )
    return _with_cn_hint(
        "Recommended next step: open the session you want to inspect first, or tap Run on the "
        "right one when you already know where to continue.",
        "建议下一步：先打开你想看的会话；如果你已经知道要在哪里继续，就直接点对应的 Run。",
    )


def _session_entry_next_step_line(
    *,
    is_current: bool,
    can_fork: bool,
    can_retry_last_turn: bool,
) -> str:
    if is_current:
        if can_fork:
            return _with_cn_hint(
                "Recommended next step: go back when you're ready to keep chatting here, or use "
                "Fork Session if you want a clean branch first.",
                "建议下一步：准备继续在这里聊天时就返回；如果你想先拉一条干净分支，就用 Fork Session。",
            )
        return _with_cn_hint(
            "Recommended next step: go back when you're ready to keep chatting here, or use "
            "Refresh if you just needed to verify the saved session details.",
            "建议下一步：准备继续在这里聊天时就返回；如果你只是来核对会话详情，就点 Refresh。",
        )
    if can_retry_last_turn:
        retry_labels = "Run+Retry / Fork+Retry" if can_fork else "Run+Retry"
        return _with_cn_hint(
            "Recommended next step: tap Run Session to continue there, or use "
            f"{retry_labels} if you also want the previous turn replayed immediately.",
            f"建议下一步：点 Run Session 继续这个会话；如果你还想立刻重放上一轮，就用 {retry_labels}。",
        )
    if can_fork:
        return _with_cn_hint(
            "Recommended next step: tap Run Session to continue there, or Fork Session if you "
            "want a clean branch first.",
            "建议下一步：点 Run Session 继续这个会话；如果你想先拉一条干净分支，就用 Fork Session。",
        )
    return _with_cn_hint(
        "Recommended next step: tap Run Session to continue there, or go back to compare "
        "another session.",
        "建议下一步：点 Run Session 继续这个会话；如果你还想比对其他会话，就先返回。",
    )


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
    lines.append(
        _with_cn_hint(
            f"{total_label}: {total_count}",
            f"{_localized_total_label(total_label)}：{total_count}",
        )
    )
    if visible_count > 0 and page_count > 1:
        end_index = start_index + visible_count - 1
        lines.append(
            _with_cn_hint(
                f"Showing: {start_index}-{end_index} of {total_count}",
                f"当前显示：第 {start_index}-{end_index} 项，共 {total_count} 项。",
            )
        )
    if page_count > 1:
        lines.append(
            _with_cn_hint(
                f"Page: {page + 1}/{page_count}",
                f"页码：{page + 1}/{page_count}",
            )
        )


def _append_action_guide_lines(
    lines: list[str],
    *,
    entries: tuple[tuple[str, ...], ...],
) -> None:
    if not entries:
        return
    lines.append("")
    lines.append(
        _with_cn_hint(
            "Action guide:",
            "操作说明：先看每组按钮解决什么问题，再决定点哪个动作。",
        )
    )
    for entry in entries:
        if len(entry) >= 3:
            label, en_summary, cn_summary = entry[0], entry[1], entry[2]
            lines.append(
                _with_cn_hint(
                    f"- {label} {en_summary}",
                    f"- {label} {cn_summary}",
                )
            )
            continue
        label, summary = entry[0], entry[1]
        lines.append(f"- {label} {summary}")


def _append_chunked_button_rows(
    buttons: list[list[InlineKeyboardButton]],
    row_buttons: list[InlineKeyboardButton],
    *,
    row_size: int = 2,
) -> None:
    if not row_buttons:
        return
    for start in range(0, len(row_buttons), row_size):
        buttons.append(row_buttons[start : start + row_size])


def _workspace_collection_action_guide_entries(
    *,
    ask_label: str,
    subject_summary: str,
    bundle_chat_label: str,
    add_label: str,
    has_last_request: bool,
) -> tuple[tuple[str, ...], ...]:
    entries = [
        (
            ask_label,
            f"starts a fresh turn using {subject_summary}.",
            f"会基于{subject_summary}发起一条全新的提问。",
        ),
    ]
    if has_last_request:
        entries.append(
            (
                "Ask With Last Request",
                f"reuses the saved request text with {subject_summary}.",
                f"会把已保存的请求文本和{subject_summary}一起复用。",
            )
        )
    entries.extend(
        [
            (
                bundle_chat_label,
                f"keeps {subject_summary} attached to your next plain text messages.",
                f"会把{subject_summary}持续挂到你接下来发出的纯文本消息上。",
            ),
            (
                add_label,
                f"saves {subject_summary} to Context Bundle without sending anything yet.",
                f"会先把{subject_summary}存进上下文包，但暂时不会发给 Agent。",
            ),
        ]
    )
    return tuple(entries)


def _workspace_collection_next_step_line(
    *,
    inspect_summary: str,
    ask_label: str,
    add_label: str,
    has_last_request: bool,
) -> str:
    if has_last_request:
        return _with_cn_hint(
            f"Recommended next step: {inspect_summary}, or use Ask With Last Request / "
            f"{add_label} when this page already covers what you need.",
            f"建议下一步：先{inspect_summary}；如果这一页已经覆盖了你要的上下文，就直接用 Ask With Last Request / {add_label}。",
        )
    return _with_cn_hint(
        f"Recommended next step: {inspect_summary}, or use {ask_label} / "
        f"{add_label} when this page already covers what you need.",
        f"建议下一步：先{inspect_summary}；如果这一页已经够用了，就直接用 {ask_label} / {add_label}。",
    )


def _workspace_item_preview_next_step_line(
    *,
    ask_label: str,
    subject_label: str,
    secondary_label: str,
    secondary_summary: str,
    has_last_request: bool,
) -> str:
    if has_last_request:
        return _with_cn_hint(
            f"Recommended next step: Ask With Last Request if the saved text already fits "
            f"{subject_label}, or use {ask_label} when you want to send fresh instructions.",
            f"建议下一步：如果已保存文本已经适合{subject_label}，就用 Ask With Last Request；如果你想补充新指令，再用 {ask_label}。",
        )
    return _with_cn_hint(
        f"Recommended next step: use {ask_label} if {subject_label} already covers what you need, "
        f"or {secondary_label} if {secondary_summary}.",
        f"建议下一步：如果{subject_label}已经覆盖你要问的内容，就直接用 {ask_label}；如果你想先留作后续复用，就用 {secondary_label}。",
    )


def _workspace_runtime_next_step_line(*, has_mcp_servers: bool) -> str:
    if has_mcp_servers:
        return _with_cn_hint(
            "Recommended next step: open an MCP server first if you need transport or config-key "
            "details, or go back when the runtime wiring already looks right.",
            "建议下一步：如果你要核对传输或配置键细节，就先打开具体 MCP server；如果运行时接线看起来没问题，就直接返回。",
        )
    return _with_cn_hint(
        "Recommended next step: go back to Bot Status if the built-in filesystem / terminal "
        "tools are enough, or reopen this page later when you need to verify runtime wiring.",
        "建议下一步：如果内建文件系统 / 终端工具已经够用，就返回状态中心；等你真要核对运行时接线时再回来。",
    )


def _workspace_runtime_server_next_step_line() -> str:
    return _with_cn_hint(
        "Recommended next step: refresh if you just changed runtime config, or go back to "
        "compare another MCP server.",
        "建议下一步：如果你刚改过运行时配置，就先刷新；否则返回去对比其他 MCP server。",
    )


def _workspace_item_action_guide_entries(
    *,
    ask_label: str,
    subject_summary: str,
    secondary_label: str,
    secondary_summary: str,
    has_last_request: bool,
    bundle_chat_label: str | None = None,
    bundle_chat_summary: str | None = None,
) -> tuple[tuple[str, ...], ...]:
    entries = [
        (
            ask_label,
            f"starts a fresh turn about {subject_summary}.",
            f"会围绕{subject_summary}发起一条全新的提问。",
        ),
    ]
    if has_last_request:
        entries.append(
            (
                "Ask With Last Request",
                f"reuses the saved request text with {subject_summary}.",
                f"会把已保存的请求文本和{subject_summary}一起复用。",
            )
        )
    if bundle_chat_label is not None and bundle_chat_summary is not None:
        entries.append(
            (
                bundle_chat_label,
                bundle_chat_summary,
                f"会把{subject_summary}挂到你接下来发出的纯文本消息上。",
            )
        )
    entries.append(
        (
            secondary_label,
            secondary_summary,
            f"会先把{subject_summary}留在本地上下文包里，方便稍后复用。",
        )
    )
    return tuple(entries)


def _context_bundle_next_step_line(
    *,
    bundle_chat_active: bool,
    has_last_request: bool,
) -> str:
    if bundle_chat_active and has_last_request:
        return _with_cn_hint(
            "Recommended next step: Ask With Last Request if you want to reuse the saved text "
            "with this bundle, or send plain text from chat to keep bundle chat going.",
            "建议下一步：如果你想把已保存文本和这份上下文包一起复用，就用 Ask With Last Request；如果你只是继续聊，就直接发送纯文本保持 Bundle Chat 运行。",
        )
    if bundle_chat_active:
        return _with_cn_hint(
            "Recommended next step: send plain text from chat to keep using this bundle, or Ask "
            "Agent With Context if you want to launch the next turn from here.",
            "建议下一步：直接在聊天里发送纯文本，继续带着这份上下文包；如果你想从这个页面显式发起下一轮，就用 Ask Agent With Context。",
        )
    if has_last_request:
        return _with_cn_hint(
            "Recommended next step: Ask With Last Request to reuse the saved text with this "
            "bundle, or Ask Agent With Context if you want to send fresh text instead.",
            "建议下一步：如果你想复用已保存文本，就用 Ask With Last Request；如果你要发一条新的问题，就用 Ask Agent With Context。",
        )
    return _with_cn_hint(
        "Recommended next step: Ask Agent With Context to work with this bundle, or Start Bundle "
        "Chat if you want the next plain-text message to carry it.",
        "建议下一步：用 Ask Agent With Context 带着这份上下文包开始提问；如果你只想让下一条纯文本自动携带它，就开启 Bundle Chat。",
    )


def _last_request_next_step_line(*, last_turn_available: bool) -> str:
    if last_turn_available:
        return _with_cn_hint(
            "Recommended next step: Run Last Request if the saved text is enough, or Retry / Fork "
            "Last Turn if you need the original payload back.",
            "建议下一步：如果已保存文本已经够用，就用 Run Last Request；如果你需要原始 payload，就用 Retry / Fork Last Turn。",
        )
    return _with_cn_hint(
        "Recommended next step: Run Last Request if the saved text is still enough, or go back to "
        "Bot Status if you want fresh workspace context first.",
        "建议下一步：如果已保存文本仍然适用，就直接用 Run Last Request；如果你想先补最新工作区上下文，就回状态中心。",
    )


def _last_turn_next_step_line() -> str:
    return _with_cn_hint(
        "Recommended next step: Retry Last Turn if you want the same payload in the current live "
        "session, or Fork Last Turn if you want a clean branch first.",
        "建议下一步：如果你想在当前 live session 里重放同一份 payload，就用 Retry Last Turn；如果你想先开干净分支，就用 Fork Last Turn。",
    )


def _agent_commands_next_step_line(*, has_args_commands: bool) -> str:
    if has_args_commands:
        return _with_cn_hint(
            "Recommended next step: run a command directly if you already know it, or open one "
            "first to confirm its args and example.",
            "建议下一步：如果你已经知道要跑哪个命令，就直接执行；如果还想确认参数和示例，就先点开查看。",
        )
    return _with_cn_hint(
        "Recommended next step: run a command directly if you already know it, or open one first "
        "if you want to review what it does.",
        "建议下一步：如果你已经确认命令，就直接执行；如果还想回顾它的作用，就先点开看详情。",
    )


def _agent_command_action_guide_entries(
    *,
    has_args_commands: bool,
) -> tuple[tuple[str, ...], ...]:
    entries = [
        (
            "Run N",
            "starts that slash command immediately when no extra args are needed.",
            "在命令不需要额外参数时，会立即执行对应 slash 命令。",
        ),
    ]
    if has_args_commands:
        entries.append(
            (
                "Args N",
                "waits for your next plain-text message and uses it as command arguments.",
                "会等待你下一条纯文本，并把它当作命令参数使用。",
            )
        )
    entries.append(
        (
            "Open N",
            "shows the full description and example before you run it.",
            "会先展示完整说明和示例，再决定要不要执行。",
        )
    )
    return tuple(entries)


def _agent_command_detail_next_step_line(*, requires_args: bool) -> str:
    if requires_args:
        return _with_cn_hint(
            "Recommended next step: tap Enter Args if you already know what to send, or go back "
            "to compare another command first.",
            "建议下一步：如果你已经知道要传什么参数，就点 Enter Args；如果还想比对其他命令，就先返回。",
        )
    return _with_cn_hint(
        "Recommended next step: tap Run Command if this is the command you need, or go back to "
        "compare another command first.",
        "建议下一步：如果这就是你要的命令，就点 Run Command；如果还想比对其他命令，就先返回。",
    )


def _model_mode_next_step_line(*, can_retry_last_turn: bool) -> str:
    if can_retry_last_turn:
        return _with_cn_hint(
            "Recommended next step: open a choice first if you want to compare details, or "
            "switch directly and use ...+Retry when the saved Last Turn should rerun under the "
            "new setting.",
            "建议下一步：如果你想先比细节，就先打开某个选项；如果已经确定，并且想让保存的上一轮在新设置下重跑，就直接切换并用 ...+Retry。",
        )
    return _with_cn_hint(
        "Recommended next step: open a choice first if you want to compare details, or switch "
        "directly when you already know the setting you need.",
        "建议下一步：如果你想先比细节，就先打开某个选项；如果已经知道自己要哪个设置，就直接切换。",
    )


def _model_mode_action_guide_entries(
    *,
    can_retry_last_turn: bool,
) -> tuple[tuple[str, ...], ...]:
    entries = [
        (
            "Model: ... / Mode: ...",
            "switches the current live session without rerunning anything.",
            "会切换当前 live session 的设置，但不会自动重跑任何内容。",
        )
    ]
    if can_retry_last_turn:
        entries.append(
            (
                "Model+Retry: ... / Mode+Retry: ...",
                "switches first, then reruns the saved Last Turn immediately.",
                "会先切换设置，再立刻重跑保存下来的上一轮。",
            )
        )
    entries.append(
        (
            "Open Model N / Open Mode N",
            "shows description and scope before you switch.",
            "会先展示说明和影响范围，再决定是否切换。",
        )
    )
    return tuple(entries)


def _plan_next_step_line(entries) -> str:
    if any(str(getattr(entry, "status", "pending")) == "in_progress" for entry in entries):
        return _with_cn_hint(
            "Recommended next step: open the in-progress item first if you want the current plan "
            "focus, or refresh later if the agent is still updating it.",
            "建议下一步：如果你要先抓当前计划重点，就先打开进行中的项；如果 Agent 还在更新，就稍后再刷新。",
        )
    return _with_cn_hint(
        "Recommended next step: open the item you want in full, or refresh later if you expect "
        "the plan to change.",
        "建议下一步：先打开你最关心的计划项看全文；如果你预计计划还会变，就稍后再刷新。",
    )


def _plan_detail_next_step_line(*, status: str) -> str:
    if status == "in_progress":
        return _with_cn_hint(
            "Recommended next step: refresh if the agent is still working on this step, or go "
            "back to compare it with the rest of the plan.",
            "建议下一步：如果 Agent 还在推进这一步，就先刷新；如果你想和其他计划项对比，就返回列表。",
        )
    if status == "completed":
        return _with_cn_hint(
            "Recommended next step: go back to the plan list for unfinished items, or refresh if "
            "you expect the agent to revise this step.",
            "建议下一步：返回计划列表看未完成项；如果你预计 Agent 还会修订这一步，就先刷新。",
        )
    return _with_cn_hint(
        "Recommended next step: go back to the plan list if you want the rest of the sequence, "
        "or refresh if this step may still update.",
        "建议下一步：如果你想看完整顺序，就返回计划列表；如果这一步还可能更新，就先刷新。",
    )


def _tool_activity_next_step_line(activities) -> str:
    if any(
        str(getattr(activity, "status", "pending")) in {"pending", "in_progress", "running"}
        for activity in activities
    ):
        return _with_cn_hint(
            "Recommended next step: open the item you care about if you need files, diffs, or "
            "terminal output, or refresh later if the agent is still working.",
            "建议下一步：如果你要看文件、diff 或终端输出，就打开对应项；如果 Agent 还在跑，就稍后刷新。",
        )
    return _with_cn_hint(
        "Recommended next step: open the item you care about if you need files, diffs, or "
        "terminal output, or go back when the summary here is enough.",
        "建议下一步：如果你要看文件、diff 或终端输出，就打开对应项；如果这里的摘要已经够用，就直接返回。",
    )


def _tool_activity_detail_next_step_line(
    *,
    status: str,
    has_openable_paths: bool,
    has_change_targets: bool,
    has_terminal_preview: bool,
) -> str:
    still_running = status in {"pending", "in_progress", "running"}
    if has_openable_paths and has_change_targets:
        return _with_cn_hint(
            "Recommended next step: open the related file or change if you want the concrete "
            "artifact, or refresh if this activity is still moving."
            if still_running
            else "Recommended next step: open the related file or change if you want the concrete "
            "artifact, or go back to compare another activity.",
            "建议下一步：如果你要看具体文件或变更，就打开相关项；如果这条活动还在推进，就先刷新，否则返回去比较其他活动。",
        )
    if has_openable_paths:
        return _with_cn_hint(
            "Recommended next step: open the related file if you want the concrete artifact, or "
            "refresh if this activity is still moving."
            if still_running
            else "Recommended next step: open the related file if you want the concrete artifact, "
            "or go back to compare another activity.",
            "建议下一步：如果你要看具体文件，就打开相关文件；如果这条活动还在推进，就先刷新，否则返回去比较其他活动。",
        )
    if has_change_targets:
        return _with_cn_hint(
            "Recommended next step: open the related change if you want the concrete diff, or "
            "refresh if this activity is still moving."
            if still_running
            else "Recommended next step: open the related change if you want the concrete diff, or "
            "go back to compare another activity.",
            "建议下一步：如果你要看具体 diff，就打开相关变更；如果这条活动还在推进，就先刷新，否则返回去比较其他活动。",
        )
    if has_terminal_preview:
        return _with_cn_hint(
            "Recommended next step: review the terminal preview here, or refresh if this command "
            "is still running."
            if still_running
            else "Recommended next step: review the terminal preview here, or go back to compare "
            "another activity.",
            "建议下一步：如果你主要关心终端输出，就先看这里的预览；如果命令还在跑就刷新，否则返回去比较其他活动。",
        )
    return _with_cn_hint(
        "Recommended next step: refresh if this activity is still moving, or go back to compare "
        "another activity."
        if still_running
        else "Recommended next step: go back to compare another activity, or return to Bot Status "
        "if this summary is enough.",
        "建议下一步：如果这条活动还在推进就先刷新；如果已经看够了，就返回去比较其他活动，或直接回状态中心。",
    )


def _no_model_mode_controls_text() -> str:
    return _with_cn_hint(
        "This agent does not expose model or mode controls in the current session. "
        "Keep chatting normally, restart the agent if you expected new controls, or open Bot "
        "Status for the rest of the runtime tools.",
        "当前会话里的这个 Agent 没有暴露模型或模式控制。你可以继续正常聊天；如果你本来预期这里该有新控制项，就重启 Agent；否则去状态中心使用其他运行时工具。",
    )


def _completed_turn_reply_markup(
    ui_state: TelegramUiState,
    *,
    user_id: int,
    provider: str,
    workspace_id: str,
) -> InlineKeyboardMarkup:
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
                "Fork Last Turn",
                "recover_fork_last_turn",
            ),
        ]
    ]
    bundle = ui_state.get_context_bundle(user_id, provider, workspace_id)
    if bundle is not None and bundle.items:
        label, action = (
            ("Stop Bundle Chat", "recover_stop_bundle_chat")
            if ui_state.context_bundle_chat_active(user_id, provider, workspace_id)
            else ("Start Bundle Chat", "recover_start_bundle_chat")
        )
        buttons.append(
            [
                _callback_button(
                    ui_state,
                    user_id,
                    label,
                    action,
                ),
                _callback_button(
                    ui_state,
                    user_id,
                    "Open Context Bundle",
                    "recover_context_bundle",
                ),
            ]
        )
    buttons.append(
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
        ]
    )
    return InlineKeyboardMarkup(buttons)


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
        next_step_line=_workspace_item_preview_next_step_line(
            ask_label="Ask Agent About Change",
            subject_label="this change",
            secondary_label="Add Change to Context",
            secondary_summary="you want to save it for later reuse",
            has_last_request=last_request_text is not None,
        ),
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
        next_step_line=_workspace_item_preview_next_step_line(
            ask_label="Ask Agent About File",
            subject_label="this file",
            secondary_label="Add File to Context",
            secondary_summary="you want to save it for later reuse",
            has_last_request=last_request_text is not None,
        ),
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
        next_step_line=_workspace_item_preview_next_step_line(
            ask_label="Ask Agent About Change",
            subject_label="this change",
            secondary_label="Add Change to Context",
            secondary_summary="you want to save it for later reuse",
            has_last_request=last_request_text is not None,
        ),
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
        next_step_line=_workspace_item_preview_next_step_line(
            ask_label="Ask Agent About File",
            subject_label="this bundled file",
            secondary_label="Remove From Context",
            secondary_summary="you want to trim the saved bundle instead",
            has_last_request=last_request_text is not None,
        ),
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
        next_step_line=_workspace_item_preview_next_step_line(
            ask_label="Ask Agent About Change",
            subject_label="this bundled change",
            secondary_label="Remove From Context",
            secondary_summary="you want to trim the saved bundle instead",
            has_last_request=last_request_text is not None,
        ),
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

    await _reply_context_bundle_view(
        update.message,
        services,
        ui_state,
        state=state,
        user_id=update.effective_user.id,
    )


async def _reply_context_bundle_view(
    message,
    services,
    ui_state: TelegramUiState,
    *,
    state,
    user_id: int,
    notice: str | None = None,
    back_target: str = "none",
) -> None:

    bundle = ui_state.get_context_bundle(
        user_id,
        state.provider,
        state.workspace_id,
    )
    text, markup = _build_context_bundle_view(
        bundle=bundle,
        provider=state.provider,
        workspace_label=_workspace_label(services, state.workspace_id),
        user_id=user_id,
        page=0,
        ui_state=ui_state,
        last_request_text=ui_state.get_last_request_text(user_id, state.workspace_id),
        bundle_chat_active=ui_state.context_bundle_chat_active(
            user_id,
            state.provider,
            state.workspace_id,
        ),
        notice=notice,
        back_target=back_target,
    )
    await message.reply_text(text, reply_markup=markup)


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
    ui_state.invalidate_session_bound_interactions_for_user(update.effective_user.id)
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
    ui_state.invalidate_session_bound_interactions_for_user(user_id)
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
    ui_state.invalidate_session_bound_interactions_for_user(user_id)
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
    ui_state.invalidate_session_bound_interactions_for_user(user_id)
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
    ui_state.invalidate_session_bound_interactions_for_user(user_id)
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
            notice=_with_cn_hint(
                "Session no longer exists in local history.",
                "这条会话已经不在本地历史里了。",
            ),
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
            notice=_with_cn_hint(
                "Provider session no longer exists on this page.",
                "这条 Provider 会话已经不在当前页里了。",
            ),
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
    lines.append(
        _view_heading(
            f"Switch agent for {resolve_provider_profile(state.provider).display_name} in "
            f"{_workspace_label(services, state.workspace_id)}",
            "切换 Agent：当前共享运行时目标预览",
        )
    )
    lines.append(
        _kv_hint(
            "Current provider",
            resolve_provider_profile(state.provider).display_name,
            "当前 Provider",
        )
    )
    lines.append(_kv_hint("Workspace", _workspace_label(services, state.workspace_id), "当前工作区"))
    lines.append(
        _with_cn_hint(
            "Admin action: this changes the shared agent runtime for every Telegram user.",
            "管理员提示：这里改动的是所有 Telegram 用户共享的 Agent 运行时。",
        )
    )
    lines.extend(
        _switch_agent_impact_lines(
            state=state,
            user_id=user_id,
            ui_state=ui_state,
            replay_turn=replay_turn,
        )
    )
    lines.append(_kv_hint("Available agents", len(provider_profiles), "可切换 Agent"))
    if replay_turn is not None:
        lines.append(
            _with_cn_hint(
                "Open a provider below to review the switch. The next screen lets you switch now, "
                "retry the last turn there, or fork it on the new agent.",
                "下一步：先打开下方某个 Provider 看切换影响。进入详情页后，你可以直接切换，也可以在新 Agent 上重试 / 分叉上一轮。",
            )
        )
    else:
        lines.append(
            _with_cn_hint(
                "Open a provider below to review the switch impact before you confirm it.",
                "下一步：先打开下方某个 Provider 看清切换影响，再决定是否确认。",
            )
        )
    lines.append(_with_cn_hint("Provider capabilities:", "Provider 能力概览："))
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
                        "switch_provider_review",
                        provider=profile.provider,
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


def _switch_agent_impact_lines(
    *,
    state,
    user_id: int,
    ui_state: TelegramUiState,
    replay_turn,
) -> list[str]:
    lines = [
        _with_cn_hint("Switch impact:", "切换影响："),
        _with_cn_hint(
            "- Old bot buttons and pending inputs will be cleared.",
            "- 旧菜单按钮和待输入状态都会被清理。",
        ),
    ]
    bundle = ui_state.get_context_bundle(user_id, state.provider, state.workspace_id)
    bundle_count = 0 if bundle is None else len(bundle.items)
    if bundle_count > 0:
        lines.append(
            _with_cn_hint(
                "- Context bundle "
                f"({_status_item_count_summary(bundle_count)}) stays with the current agent runtime "
                "and won't follow the switch.",
                "- 当前上下文包 "
                f"（{_status_item_count_summary(bundle_count)}）会留在旧 Agent 运行时，不会跟着切过去。",
            )
        )
    else:
        lines.append(
            _with_cn_hint(
                "- Context bundle does not follow an agent switch.",
                "- 上下文包不会跟随 Agent 切换。",
            )
        )
    if replay_turn is not None:
        replay_label = _status_text_snippet(replay_turn.title_hint, limit=80) or "untitled turn"
        lines.append(
            _with_cn_hint(
                f"- Last Turn stays available in this workspace: {replay_label}",
                f"- 上一轮详情会继续保留在这个工作区里：{replay_label}",
            )
        )
        return lines
    if ui_state.get_last_request_text(user_id, state.workspace_id) is not None:
        lines.append(
            _with_cn_hint(
                "- Last Request stays available in this workspace after the switch.",
                "- 上次请求会在这个工作区里继续可用，不会因为切 Agent 而丢失。",
            )
        )
        return lines
    lines.append(
        _with_cn_hint(
            "- After switching, send a fresh request or open Bot Status to keep going.",
            "- 切换完成后，你可以直接发一条新请求，或先回状态中心继续操作。",
        )
    )
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
    lines.append(
        _view_heading(
            f"Switch workspace for {resolve_provider_profile(state.provider).display_name}",
            "切换工作区：当前共享运行时目标预览",
        )
    )
    lines.append(
        _kv_hint(
            "Current provider",
            resolve_provider_profile(state.provider).display_name,
            "当前 Provider",
        )
    )
    lines.append(_kv_hint("Current workspace", _workspace_label(services, state.workspace_id), "当前工作区"))
    lines.append(
        _with_cn_hint(
            "Admin action: this changes the shared workspace for every Telegram user.",
            "管理员提示：这里改动的是所有 Telegram 用户共享的工作区。",
        )
    )
    lines.append(
        _with_cn_hint(
            "Only configured workspaces are listed below.",
            "下方只会显示配置里允许切换的工作区。",
        )
    )
    lines.extend(
        _switch_workspace_impact_lines(
            state=state,
            user_id=user_id,
            ui_state=ui_state,
        )
    )
    workspaces = tuple(services.config.agent.workspaces)
    lines.append(_kv_hint("Configured workspaces", len(workspaces), "可切换工作区"))
    lines.append(
        _with_cn_hint(
            "Open a workspace below to review what stays behind before you confirm the switch.",
            "下一步：先打开下方某个工作区，看清哪些内容会留在旧工作区，再决定是否确认切换。",
        )
    )
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
                        "switch_workspace_review",
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
    return _with_cn_hint(
        "Everyone now lands in the selected agent runtime for this workspace. "
        "Context bundle does not follow an agent switch. "
        "Last Turn and Last Request stay reusable in this workspace when available.",
        "当前工作区里的所有用户都会进入新选择的 Agent 运行时。"
        "Context Bundle 不会跟随 Agent 切换；如果有 Last Turn / Last Request，"
        "它们仍然可以继续在当前工作区复用。",
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
        _with_cn_hint("Switch impact:", "切换影响："),
        _with_cn_hint(
            "- Old bot buttons and pending inputs will be cleared.",
            "- 旧菜单按钮和待输入状态都会被清理。",
        ),
    ]
    if state_labels:
        lines.append(
            _with_cn_hint(
                f"- Current workspace state that will stay behind: {', '.join(state_labels)}.",
                f"- 会留在当前工作区、不会跟随切走的内容有：{', '.join(state_labels)}。",
            )
        )
    else:
        lines.append(
            _with_cn_hint(
                "- Any Context Bundle, Last Request, or Last Turn from this workspace will stay behind.",
                "- 当前工作区里的 Context Bundle、Last Request 和 Last Turn 都不会跟着切过去。",
            )
        )
    lines.append(
        _with_cn_hint(
            "- Rebuild context in the target workspace before you ask.",
            "- 到目标工作区后，请先重新整理上下文，再开始提问。",
        )
    )
    return lines


def _switch_workspace_success_detail_text() -> str:
    return _with_cn_hint(
        "Everyone now lands in the selected workspace. "
        "Workspace-specific context does not follow the switch. "
        "Rebuild context in the new workspace before you ask.",
        "当前所有用户都会进入新选择的工作区。"
        "工作区相关的上下文不会自动跟着切过去；开始提问前，请先在新工作区重新整理上下文。",
    )


def _build_switch_provider_review_view(
    *,
    state,
    services,
    capability_summaries,
    provider: str,
    user_id: int,
    ui_state: TelegramUiState,
    replay_turn,
    back_target: str = "none",
    notice: str | None = None,
):
    profile = resolve_provider_profile(provider)
    summary = capability_summaries.get(provider)
    is_current = provider == state.provider
    is_available = summary is not None and getattr(summary, "available", False)

    lines = []
    if notice:
        lines.append(notice)
    lines.append(
        _view_heading(
            f"Switch agent review: {profile.display_name}",
            f"切换 Agent 复核：{profile.display_name}",
        )
    )
    lines.append(
        _kv_hint(
            "Current provider",
            resolve_provider_profile(state.provider).display_name,
            "当前 Provider",
        )
    )
    lines.append(_kv_hint("Workspace", _workspace_label(services, state.workspace_id), "当前工作区"))
    lines.append(
        _with_cn_hint(
            "Admin action: confirming here changes the shared agent runtime for every Telegram user.",
            "管理员提示：在这里确认后，会立即切换所有 Telegram 用户共享的 Agent 运行时。",
        )
    )
    lines.append(_with_cn_hint("Target capability summary:", "目标能力概览："))
    lines.append(_format_provider_capability_summary(profile, summary, is_current=is_current))
    lines.extend(
        _switch_agent_impact_lines(
            state=state,
            user_id=user_id,
            ui_state=ui_state,
            replay_turn=replay_turn,
        )
    )

    buttons = []
    if is_current:
        lines.append(
            _with_cn_hint(
                "This agent is already active. Go back if you want to review another target.",
                "这个 Agent 已经是当前运行时；如果你想比较别的目标，就返回上一页。",
            )
        )
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
    elif not is_available:
        lines.append(
            _with_cn_hint(
                "Recommended next step: choose another agent, or fix this provider and reopen Switch Agent.",
                "建议下一步：先改选别的 Agent，或修好这个 Provider 后再重新打开 Switch Agent。",
            )
        )
    else:
        if replay_turn is not None:
            lines.append(
                _with_cn_hint(
                    "Recommended next step: switch now if you want everyone on this agent, or use "
                    "Retry / Fork to move the shared runtime and immediately replay the last turn.",
                    "建议下一步：如果你要让所有人都切到这个 Agent，就直接切换；如果你还想立刻重放上一轮，就用 Retry / Fork 一步完成。",
                )
            )
        else:
            lines.append(
                _with_cn_hint(
                    "Recommended next step: switch now if you want everyone to move to this agent.",
                    "建议下一步：如果你要让所有人都迁到这个 Agent，就直接切换。",
                )
            )
        buttons.append(
            [
                _callback_button(
                    ui_state,
                    user_id,
                    f"Switch to {profile.display_name}",
                    "switch_provider",
                    provider=provider,
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
                        provider=provider,
                        back_target=back_target,
                    ),
                    _callback_button(
                        ui_state,
                        user_id,
                        f"Fork on {profile.display_name}",
                        "switch_provider_fork_last_turn",
                        provider=provider,
                        back_target=back_target,
                    ),
                ]
            )
    buttons.append(
        [
            _callback_button(
                ui_state,
                user_id,
                "Back to Switch Agent",
                "switch_agent_page",
                back_target=back_target,
            )
        ]
    )
    if back_target == "status":
        _append_back_to_status_button(
            buttons,
            ui_state=ui_state,
            user_id=user_id,
            back_target=back_target,
        )
    return "\n".join(lines), InlineKeyboardMarkup(buttons)


def _build_switch_workspace_review_view(
    *,
    state,
    services,
    workspace,
    user_id: int,
    ui_state: TelegramUiState,
    back_target: str = "none",
    notice: str | None = None,
):
    is_current = workspace.id == state.workspace_id
    lines = []
    if notice:
        lines.append(notice)
    lines.append(
        _view_heading(
            f"Switch workspace review: {workspace.label}",
            f"切换工作区复核：{workspace.label}",
        )
    )
    lines.append(
        _kv_hint(
            "Current provider",
            resolve_provider_profile(state.provider).display_name,
            "当前 Provider",
        )
    )
    lines.append(_kv_hint("Current workspace", _workspace_label(services, state.workspace_id), "当前工作区"))
    lines.append(
        _with_cn_hint(
            "Admin action: confirming here changes the shared workspace for every Telegram user.",
            "管理员提示：在这里确认后，会立即切换所有 Telegram 用户共享的工作区。",
        )
    )
    lines.append(_kv_hint("Target workspace ID", workspace.id, "目标工作区 ID"))
    lines.extend(
        _switch_workspace_impact_lines(
            state=state,
            user_id=user_id,
            ui_state=ui_state,
        )
    )

    buttons = []
    if is_current:
        lines.append(
            _with_cn_hint(
                "This workspace is already active. Go back if you want to review another target.",
                "这个工作区已经是当前运行时；如果你想比较别的目标，就返回上一页。",
            )
        )
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
    else:
        lines.append(
            _with_cn_hint(
                "Recommended next step: switch now if you want everyone to land in this workspace.",
                "建议下一步：如果你要让所有人都落到这个工作区，就直接切换。",
            )
        )
        buttons.append(
            [
                _callback_button(
                    ui_state,
                    user_id,
                    f"Switch to {workspace.label}",
                    "switch_workspace",
                    workspace_id=workspace.id,
                    back_target=back_target,
                )
            ]
        )
    buttons.append(
        [
            _callback_button(
                ui_state,
                user_id,
                "Back to Switch Workspace",
                "switch_workspace_page",
                back_target=back_target,
            )
        ]
    )
    if back_target == "status":
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


async def _show_switch_provider_review_from_callback(
    query,
    services,
    ui_state: TelegramUiState,
    *,
    user_id: int,
    provider: str,
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
    text, markup = _build_switch_provider_review_view(
        state=state,
        services=services,
        capability_summaries=capability_summaries,
        provider=provider,
        user_id=user_id,
        ui_state=ui_state,
        replay_turn=replay_turn,
        back_target=back_target,
        notice=notice,
    )
    await _edit_query_message(query, text, reply_markup=markup)


async def _show_switch_workspace_review_from_callback(
    query,
    services,
    ui_state: TelegramUiState,
    *,
    user_id: int,
    workspace_id: str,
    back_target: str = "none",
    notice: str | None = None,
) -> None:
    state = await services.snapshot_runtime_state()
    workspace = services.config.agent.resolve_workspace(workspace_id)
    text, markup = _build_switch_workspace_review_view(
        state=state,
        services=services,
        workspace=workspace,
        user_id=user_id,
        ui_state=ui_state,
        back_target=back_target,
        notice=notice,
    )
    await _edit_query_message(query, text, reply_markup=markup)


async def _show_switch_provider_result_from_callback(
    query,
    services,
    ui_state: TelegramUiState,
    *,
    user_id: int,
    provider: str,
    back_target: str,
    notice: str,
) -> None:
    if back_target == "status":
        await _show_runtime_status_from_callback(
            query,
            services,
            ui_state,
            user_id=user_id,
            notice=notice,
        )
        return
    try:
        await _show_switch_provider_review_from_callback(
            query,
            services,
            ui_state,
            user_id=user_id,
            provider=provider,
            back_target=back_target,
            notice=notice,
        )
    except Exception:
        await _edit_query_message(query, notice)


async def _show_switch_workspace_result_from_callback(
    query,
    services,
    ui_state: TelegramUiState,
    *,
    user_id: int,
    workspace_id: str,
    back_target: str,
    notice: str,
) -> None:
    if back_target == "status":
        await _show_runtime_status_from_callback(
            query,
            services,
            ui_state,
            user_id=user_id,
            notice=notice,
        )
        return
    try:
        await _show_switch_workspace_review_from_callback(
            query,
            services,
            ui_state,
            user_id=user_id,
            workspace_id=workspace_id,
            back_target=back_target,
            notice=notice,
        )
    except Exception:
        await _edit_query_message(query, notice)


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

    ui_state.invalidate_session_bound_interactions_for_user(update.effective_user.id)
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
        await _show_switch_provider_result_from_callback(
            query,
            services,
            ui_state,
            user_id=query.from_user.id,
            provider=provider,
            back_target=back_target,
            notice=_prefixed_notice_text(
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
            await _show_switch_provider_result_from_callback(
                query,
                services,
                ui_state,
                user_id=query.from_user.id,
                provider=provider,
                back_target=back_target,
                notice=f"{success_text}\nRetried last turn on the new agent.",
            )

        async def _on_retry_missing_replay_turn() -> None:
            await _show_switch_provider_result_from_callback(
                query,
                services,
                ui_state,
                user_id=query.from_user.id,
                provider=provider,
                back_target=back_target,
                notice=f"{success_text}\n{_no_previous_turn_text()}",
            )

        async def _on_retry_prepare_failure() -> None:
            await _show_switch_provider_result_from_callback(
                query,
                services,
                ui_state,
                user_id=query.from_user.id,
                provider=provider,
                back_target=back_target,
                notice=f"{success_text}\n{_request_failed_text()}",
            )

        async def _on_retry_turn_failure() -> None:
            await _show_switch_provider_result_from_callback(
                query,
                services,
                ui_state,
                user_id=query.from_user.id,
                provider=provider,
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
    if replay_action == "fork_last_turn":
        await _edit_query_message(
            query,
            f"{success_text}\nForking last turn on the new agent...",
        )
        if query.message is None:
            return

        async def _after_fork_success(state, session) -> None:
            await _show_switch_provider_result_from_callback(
                query,
                services,
                ui_state,
                user_id=query.from_user.id,
                provider=provider,
                back_target=back_target,
                notice=f"{success_text}\nForked last turn on the new agent.",
            )

        async def _on_fork_missing_replay_turn() -> None:
            await _show_switch_provider_result_from_callback(
                query,
                services,
                ui_state,
                user_id=query.from_user.id,
                provider=provider,
                back_target=back_target,
                notice=f"{success_text}\n{_no_previous_turn_text()}",
            )

        async def _on_fork_session_creation_failed() -> None:
            await _show_switch_provider_result_from_callback(
                query,
                services,
                ui_state,
                user_id=query.from_user.id,
                provider=provider,
                back_target=back_target,
                notice=f"{success_text}\n{_session_creation_failed_text()}",
            )

        async def _on_fork_turn_failure() -> None:
            await _show_switch_provider_result_from_callback(
                query,
                services,
                ui_state,
                user_id=query.from_user.id,
                provider=provider,
                back_target=back_target,
                notice=f"{success_text}\n{_request_failed_text()}",
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

    await _show_switch_provider_result_from_callback(
        query,
        services,
        ui_state,
        user_id=query.from_user.id,
        provider=provider,
        back_target=back_target,
        notice=success_text,
    )


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
    ui_state.invalidate_session_bound_interactions_for_user(user_id)
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
    ui_state.invalidate_session_bound_interactions_for_user(user_id)
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

    ui_state.invalidate_session_bound_interactions_for_user(user_id)
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
    ui_state.invalidate_session_bound_interactions_for_user(user_id)
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

    if action == "recover_context_bundle":
        await query.answer()
        if query.message is None or query.from_user is None:
            return
        try:
            state = await services.snapshot_runtime_state()
        except Exception:
            await _reply_request_failed(_message_update_from_callback(query), services)
            return
        await _reply_context_bundle_view(
            query.message,
            services,
            ui_state,
            state=state,
            user_id=user_id,
            back_target="status",
        )
        return

    if action == "recover_start_bundle_chat":
        await query.answer()
        if query.message is None or query.from_user is None:
            return
        try:
            state = await services.snapshot_runtime_state()
        except Exception:
            await _reply_request_failed(_message_update_from_callback(query), services)
            return
        bundle = ui_state.get_context_bundle(user_id, state.provider, state.workspace_id)
        if bundle is None or not bundle.items:
            notice = _context_bundle_empty_text()
        elif ui_state.context_bundle_chat_active(user_id, state.provider, state.workspace_id):
            notice = _with_cn_hint(
                "Bundle chat is already on.",
                "Bundle Chat 已经处于开启状态。",
            )
        else:
            ui_state.enable_context_bundle_chat(user_id, state.provider, state.workspace_id)
            notice = _with_cn_hint(
                "Bundle chat enabled. New plain text messages will use the current context bundle.",
                "Bundle Chat 已开启。后续新的纯文本消息会自动带上当前上下文包。",
            )
        await _reply_context_bundle_view(
            query.message,
            services,
            ui_state,
            state=state,
            user_id=user_id,
            notice=notice,
            back_target="status",
        )
        return

    if action == "recover_stop_bundle_chat":
        await query.answer()
        if query.message is None or query.from_user is None:
            return
        try:
            state = await services.snapshot_runtime_state()
        except Exception:
            await _reply_request_failed(_message_update_from_callback(query), services)
            return
        if ui_state.context_bundle_chat_active(user_id, state.provider, state.workspace_id):
            ui_state.disable_context_bundle_chat(user_id)
            notice = _bundle_chat_disabled_text()
        else:
            notice = _bundle_chat_already_off_text()
        await _reply_context_bundle_view(
            query.message,
            services,
            ui_state,
            state=state,
            user_id=user_id,
            notice=notice,
            back_target="status",
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

    if action == "switch_agent_page":
        await query.answer()
        await _show_switch_agent_menu_from_callback(
            query,
            services,
            ui_state,
            user_id=user_id,
            back_target=str(payload.get("back_target", "none")),
        )
        return

    if action == "switch_workspace_page":
        await query.answer()
        await _show_switch_workspace_menu_from_callback(
            query,
            services,
            ui_state,
            user_id=user_id,
            back_target=str(payload.get("back_target", "none")),
        )
        return

    if action == "switch_provider_review":
        await query.answer()
        await _show_switch_provider_review_from_callback(
            query,
            services,
            ui_state,
            user_id=user_id,
            provider=str(payload["provider"]),
            back_target=str(payload.get("back_target", "none")),
        )
        return

    if action == "switch_workspace_review":
        await query.answer()
        await _show_switch_workspace_review_from_callback(
            query,
            services,
            ui_state,
            user_id=user_id,
            workspace_id=str(payload["workspace_id"]),
            back_target=str(payload.get("back_target", "none")),
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
                text=_with_cn_hint(
                    "Couldn't load Bot Status. Try again or use /start.",
                    "加载状态中心失败。请重试，或使用 /start 恢复。",
                ),
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
                text=_with_cn_hint(
                    "Couldn't open that view. Try again or go back to Bot Status.",
                    "打开这个视图失败。请重试，或返回状态中心。",
                ),
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
                text=_with_cn_hint(
                    "Couldn't load the last turn. Try again or go back.",
                    "加载上一轮失败。请重试，或先返回上一层。",
                ),
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
                text=_with_cn_hint(
                    "Couldn't load that replay item. Try again or go back.",
                    "加载这条重放内容失败。请重试，或先返回上一层。",
                ),
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
                text=_with_cn_hint(
                    "Couldn't load the agent plan. Try again or go back.",
                    "加载 Agent 计划失败。请重试，或先返回上一层。",
                ),
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
                text=_with_cn_hint(
                    "Couldn't load that plan entry. Try again or go back.",
                    "加载这条计划项失败。请重试，或先返回上一层。",
                ),
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
                text=_with_cn_hint(
                    "Couldn't load tool activity. Try again or go back.",
                    "加载工具活动失败。请重试，或先返回上一层。",
                ),
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
                text=_with_cn_hint(
                    "Couldn't load that tool activity entry. Try again or go back.",
                    "加载这条工具活动失败。请重试，或先返回上一层。",
                ),
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
                text=_with_cn_hint(
                    "Couldn't load that related file. Try again or go back.",
                    "加载相关文件失败。请重试，或先返回上一层。",
                ),
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
                text=_with_cn_hint(
                    "Couldn't load that related change. Try again or go back.",
                    "加载相关变更失败。请重试，或先返回上一层。",
                ),
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
                text=_with_cn_hint(
                    "Couldn't load MCP server details. Try again or go back.",
                    "加载 MCP server 详情失败。请重试，或先返回上一层。",
                ),
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
                    notice=_with_cn_hint(
                        "Retried last turn.",
                        "已重试上一轮。",
                    ),
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
                success_notice=_with_cn_hint(
                    "Ran the last request.",
                    "已运行上次请求。",
                ),
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
                    notice=_with_cn_hint(
                        "Forked last turn into a new session.",
                        "已把上一轮分叉到新会话。",
                    ),
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
                    notice=_with_cn_hint(
                        "No agent command is available.",
                        "当前没有可用的 Agent 命令。",
                    ),
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
                    notice=_with_cn_hint(
                        "No workspace changes to ask about.",
                        "当前没有可提问的工作区变更。",
                    ),
                )
                return
            await _begin_context_items_ask_from_callback(
                query,
                ui_state,
                user_id=user_id,
                items=items,
                prompt_label="current workspace changes",
                empty_notice=_with_cn_hint(
                    "No workspace changes to ask about.",
                    "当前没有可提问的工作区变更。",
                ),
                prompt_text=(
                    "Send your request about the current workspace changes as the next plain text message.\n"
                    "The agent will inspect the current Git changes from the local workspace."
                ),
                restore_action="runtime_status_page",
                restore_payload={"back_target": "status"},
                cancel_notice=_with_cn_hint(
                    "Workspace changes request cancelled.",
                    "已取消针对当前工作区变更的提问。",
                ),
                status_success_notice=_with_cn_hint(
                    "Asked agent about current workspace changes.",
                    "已向 Agent 发起关于当前工作区变更的提问。",
                ),
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
                    notice=_with_cn_hint(
                        "No workspace changes to ask about.",
                        "当前没有可提问的工作区变更。",
                    ),
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
                success_notice=_with_cn_hint(
                    "Asked agent with the last request about current workspace changes.",
                    "已用上次请求向 Agent 询问当前工作区变更。",
                ),
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
                    notice=_with_cn_hint(
                        "No workspace changes to add.",
                        "当前没有可加入上下文的工作区变更。",
                    ),
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
                    notice=_with_cn_hint(
                        "No workspace changes to add.",
                        "当前没有可加入上下文的工作区变更。",
                    ),
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
                status_success_notice=_with_cn_hint(
                    "Asked agent with the current context bundle.",
                    "已带着当前上下文包向 Agent 发起提问。",
                ),
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
                                notice=_with_cn_hint(
                                    "Context bundle request cancelled.",
                                    "已取消这次上下文包提问。",
                                ),
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
                success_notice=_with_cn_hint(
                    "Asked agent with the last request using the current context bundle.",
                    "已用上次请求并携带当前上下文包向 Agent 发起提问。",
                ),
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
                    text=_model_mode_load_failed_text(),
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
                    text=_with_cn_hint(
                        "Couldn't load Switch Agent. Try again or go back to Bot Status.",
                        "加载切换 Agent 视图失败。请重试，或返回状态中心。",
                    ),
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
                    text=_with_cn_hint(
                        "Couldn't load Switch Workspace. Try again or go back to Bot Status.",
                        "加载切换工作区视图失败。请重试，或返回状态中心。",
                    ),
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
                notice = _with_cn_hint(
                    "No active turn to stop.",
                    "当前没有可停止的回合。",
                )
            else:
                await _request_stop_active_turn(
                    ui_state,
                    user_id=user_id,
                    active_turn=active_turn,
                )
                notice = _stop_requested_notice_text()
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
                notice = _with_cn_hint(
                    "Bundle chat is already on.",
                    "Bundle Chat 已经处于开启状态。",
                )
            else:
                ui_state.enable_context_bundle_chat(user_id, state.provider, state.workspace_id)
                notice = _with_cn_hint(
                    "Bundle chat enabled.",
                    "Bundle Chat 已开启。",
                )
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
                notice = _bundle_chat_disabled_text()
            else:
                notice = _bundle_chat_already_off_text()
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
        notice = _search_cancelled_notice_text()
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
        back_target = str(payload.get("back_target", "none"))
        pending_upload_notice = _discard_pending_uploads_for_transition(ui_state, user_id)
        await query.answer()
        await _edit_query_message(query, f"Switching workspace to {workspace.label}...")
        try:
            await asyncio.wait_for(
                services.switch_workspace(workspace.id),
                CALLBACK_OPERATION_TIMEOUT_SECONDS,
            )
        except Exception:
            await _show_switch_workspace_result_from_callback(
                query,
                services,
                ui_state,
                user_id=user_id,
                workspace_id=workspace.id,
                back_target=back_target,
                notice=_prefixed_notice_text(
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
        await _show_switch_workspace_result_from_callback(
            query,
            services,
            ui_state,
            user_id=user_id,
            workspace_id=workspace.id,
            back_target=back_target,
            notice=success_text,
        )
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
                text=_with_cn_hint(
                    "Couldn't load that session history entry. Try again or go back.",
                    "加载这条会话历史失败。请重试，或先返回上一层。",
                ),
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
                text=_with_cn_hint(
                    "Couldn't load Provider Sessions. Try again or go back.",
                    "加载 Provider 会话列表失败。请重试，或先返回上一层。",
                ),
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
            ui_state.invalidate_session_bound_interactions_for_user(user_id)
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
            notice=_with_cn_hint(
                "Rename cancelled.",
                "已取消重命名。",
            ),
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
                text=_with_cn_hint(
                    "Couldn't load Provider Sessions. Try again or go back.",
                    "加载 Provider 会话列表失败。请重试，或先返回上一层。",
                ),
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
                text=_with_cn_hint(
                    "Couldn't load that provider session. Try again or go back.",
                    "加载这条 Provider 会话失败。请重试，或先返回上一层。",
                ),
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
                text=_with_cn_hint(
                    "Couldn't load that agent command. Try again or go back.",
                    "加载这个 Agent 命令失败。请重试，或先返回上一层。",
                ),
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
            notice=_with_cn_hint(
                "Command input cancelled.",
                "已取消命令输入。",
            ),
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
                text=_with_cn_hint(
                    "Couldn't load Model / Mode. Try again or go back.",
                    "加载模型 / 模式失败。请重试，或先返回上一层。",
                ),
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
                text=_with_cn_hint(
                    "Couldn't load selection details. Try again or go back.",
                    "加载这个选项详情失败。请重试，或先返回上一层。",
                ),
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
                notice=_with_cn_hint(
                    "No visible files to add.",
                    "当前没有可加入上下文的可见文件。",
                ),
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
                notice=_with_cn_hint(
                    "No visible files to add.",
                    "当前没有可加入上下文的可见文件。",
                ),
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
                notice=_with_cn_hint(
                    "No visible files to ask about.",
                    "当前没有可提问的可见文件。",
                ),
            )
            return

        await _begin_context_items_ask_from_callback(
            query,
            ui_state,
            user_id=user_id,
            items=items,
            prompt_label="visible workspace files",
            empty_notice=_with_cn_hint(
                "No visible files to ask about.",
                "当前没有可提问的可见文件。",
            ),
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
            cancel_notice=_with_cn_hint(
                "Visible files request cancelled.",
                "已取消针对可见文件的提问。",
            ),
            status_success_notice=_with_cn_hint(
                "Asked agent about visible workspace files.",
                "已向 Agent 发起关于可见文件的提问。",
            ),
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
                notice=_with_cn_hint(
                    "No visible files to ask about.",
                    "当前没有可提问的可见文件。",
                ),
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
                success_notice=_with_cn_hint(
                    "Asked agent with the last request about visible workspace files.",
                    "已用上次请求向 Agent 询问可见文件。",
                ),
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
                notice=_with_cn_hint(
                    "No matching files to add.",
                    "当前没有可加入上下文的匹配文件。",
                ),
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
                notice=_with_cn_hint(
                    "No matching files to add.",
                    "当前没有可加入上下文的匹配文件。",
                ),
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
                notice=_with_cn_hint(
                    "No matching files to ask about.",
                    "当前没有可提问的匹配文件。",
                ),
            )
            return

        await _begin_context_items_ask_from_callback(
            query,
            ui_state,
            user_id=user_id,
            items=items,
            prompt_label="matching workspace files",
            empty_notice=_with_cn_hint(
                "No matching files to ask about.",
                "当前没有可提问的匹配文件。",
            ),
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
            cancel_notice=_with_cn_hint(
                "Matching files request cancelled.",
                "已取消针对匹配文件的提问。",
            ),
            status_success_notice=_with_cn_hint(
                "Asked agent about matching workspace files.",
                "已向 Agent 发起关于匹配文件的提问。",
            ),
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
                notice=_with_cn_hint(
                    "No matching files to ask about.",
                    "当前没有可提问的匹配文件。",
                ),
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
                success_notice=_with_cn_hint(
                    "Asked agent with the last request about matching workspace files.",
                    "已用上次请求向 Agent 询问匹配文件。",
                ),
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
                notice=_with_cn_hint(
                    "No workspace changes to add.",
                    "当前没有可加入上下文的工作区变更。",
                ),
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
                notice=_with_cn_hint(
                    "No workspace changes to add.",
                    "当前没有可加入上下文的工作区变更。",
                ),
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
                    notice=_with_cn_hint(
                        "No workspace changes to ask about.",
                        "当前没有可提问的工作区变更。",
                    ),
                )
            else:
                await _show_workspace_changes_from_callback(
                    query,
                    services,
                    ui_state,
                    user_id=user_id,
                    page=page,
                    back_target=str(payload.get("back_target", "none")),
                    notice=_with_cn_hint(
                        "No workspace changes to ask about.",
                        "当前没有可提问的工作区变更。",
                    ),
                )
            return

        await _begin_context_items_ask_from_callback(
            query,
            ui_state,
            user_id=user_id,
            items=items,
            prompt_label="current workspace changes",
            empty_notice=_with_cn_hint(
                "No workspace changes to ask about.",
                "当前没有可提问的工作区变更。",
            ),
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
            cancel_notice=_with_cn_hint(
                "Workspace changes request cancelled.",
                "已取消针对当前工作区变更的提问。",
            ),
            status_success_notice=_with_cn_hint(
                "Asked agent about current workspace changes.",
                "已向 Agent 发起关于当前工作区变更的提问。",
            ),
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
                    notice=_with_cn_hint(
                        "No workspace changes to ask about.",
                        "当前没有可提问的工作区变更。",
                    ),
                )
            else:
                await _show_workspace_changes_from_callback(
                    query,
                    services,
                    ui_state,
                    user_id=user_id,
                    page=page,
                    back_target=back_target,
                    notice=_with_cn_hint(
                        "No workspace changes to ask about.",
                        "当前没有可提问的工作区变更。",
                    ),
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
                success_notice=_with_cn_hint(
                    "Asked agent with the last request about current workspace changes.",
                    "已用上次请求向 Agent 询问当前工作区变更。",
                ),
            )
        elif source == "follow_up":
            async def _after_follow_up_success(_state, _session) -> None:
                await _show_workspace_changes_follow_up_from_callback(
                    query,
                    services,
                    ui_state,
                    user_id=user_id,
                    notice=_with_cn_hint(
                        "Asked agent with the last request about current workspace changes.",
                        "已用上次请求向 Agent 询问当前工作区变更。",
                    ),
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
                success_notice=_with_cn_hint(
                    "Asked agent with the last request about this file.",
                    "已用上次请求向 Agent 询问这个文件。",
                ),
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
                success_notice=_with_cn_hint(
                    "Asked agent with the last request about this change.",
                    "已用上次请求向 Agent 询问这条变更。",
                ),
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
                _with_cn_hint(
                    "Removed item from context bundle. Bundle chat was turned off because the bundle is empty.",
                    "已从上下文包移除这项内容；由于上下文包已经为空，Bundle Chat 也已自动关闭。",
                )
                if bundle is None and was_bundle_chat_active
                else _with_cn_hint(
                    "Removed item from context bundle.",
                    "已从上下文包移除这项内容。",
                )
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
                _with_cn_hint(
                    "Removed item from context bundle. Bundle chat was turned off because the bundle is empty.",
                    "已从上下文包移除这项内容；由于上下文包已经为空，Bundle Chat 也已自动关闭。",
                )
                if bundle is None and was_bundle_chat_active
                else _with_cn_hint(
                    "Removed item from context bundle.",
                    "已从上下文包移除这项内容。",
                )
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
            notice=(
                _with_cn_hint(
                    "Cleared context bundle. Bundle chat was turned off.",
                    "已清空上下文包，并自动关闭 Bundle Chat。",
                )
                if was_bundle_chat_active
                else _with_cn_hint(
                    "Cleared context bundle.",
                    "已清空上下文包。",
                )
            ),
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
            notice=_with_cn_hint(
                "Bundle chat enabled. New plain text messages will use the current context bundle.",
                "Bundle Chat 已开启。后续新的纯文本消息会自动带上当前上下文包。",
            ),
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
            notice=_bundle_chat_disabled_text(),
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
                success_notice=_with_cn_hint(
                    "Asked agent with the last request using the current context bundle.",
                    "已用上次请求并携带当前上下文包向 Agent 发起提问。",
                ),
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
            notice=_with_cn_hint(
                "Context bundle request cancelled.",
                "已取消这次上下文包提问。",
            ),
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
    return InlineKeyboardButton(
        text=_localized_button_text(text),
        callback_data=f"{CALLBACK_PREFIX}{token}",
    )


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
    session_has_live_id = session is not None and getattr(session, "session_id", None) is not None
    can_fork_session = session_has_live_id and bool(
        getattr(getattr(session, "capabilities", None), "can_fork", False)
    )
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
        _view_heading(
            f"Bot status for {resolve_provider_profile(provider).display_name} in {workspace_label}",
            f"状态中心：{workspace_label} 中的 {resolve_provider_profile(provider).display_name}",
        )
    )
    lines.append("状态中心：这里是只读总览和高级控制台，不会隐式创建新会话。")
    lines.append(_kv_hint("Workspace ID", workspace_id, "工作区 ID"))
    lines.append(_kv_hint("Path", workspace_path, "路径"))
    lines.append(f"工作区概览：{workspace_label}，路径为 {workspace_path}。")
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
        runtime_lines.append(
            _with_cn_hint(
                "Session: none (will start on first request)",
                "会话：无（第一条请求时自动开始）",
            )
        )
    else:
        runtime_lines.append(
            _with_cn_hint(
                f"Session: {session.session_id or 'pending'}",
                f"会话：{session.session_id or 'pending'}",
            )
        )
        if session_title is not None:
            runtime_lines.append(
                _with_cn_hint(
                    f"Session title: {session_title}",
                    f"会话标题：{session_title}",
                )
            )
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
            runtime_lines.append(
                _with_cn_hint(
                    f"Model: {_current_choice_label(model_selection)}",
                    f"模型：{_current_choice_label(model_selection)}",
                )
            )
        if mode_selection is not None:
            runtime_lines.append(
                _with_cn_hint(
                    f"Mode: {_current_choice_label(mode_selection)}",
                    f"模式：{_current_choice_label(mode_selection)}",
                )
            )
    usage_summary = _status_usage_summary(session)
    usage_summary_cn = _status_usage_summary_cn(session)
    if usage_summary is not None:
        runtime_lines.append(
            _with_cn_hint(
                f"Usage: {usage_summary}",
                f"用量：{usage_summary_cn or usage_summary}",
            )
        )
    runtime_lines.extend(_status_plan_preview_lines(session))
    plan_count = len(_plan_items(session))
    runtime_lines.extend(_status_tool_activity_preview_lines(session))
    tool_activity_count = len(_tool_activity_items(session))

    memory_lines = [
        _with_cn_hint(
            f"Pending input: {_pending_text_action_label(pending_text_action)}",
            f"待输入：{_pending_text_action_label_cn(pending_text_action)}",
        )
    ]
    pending_text_hint = _pending_text_action_hint_line(pending_text_action)
    if pending_text_hint is not None:
        memory_lines.append(pending_text_hint)
    if pending_media_group_stats is not None:
        memory_lines.append(
            _with_cn_hint(
                f"Pending uploads: {_pending_media_group_summary(pending_media_group_stats)}",
                f"待上传附件：{_pending_media_group_summary_cn(pending_media_group_stats)}",
            )
        )
    memory_lines.append(
        _with_cn_hint(f"Local sessions: {history_count}", f"本地会话：{history_count}")
    )
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
        memory_lines.append(_with_cn_hint("Last turn replay: none", "上一轮回放：无"))
    else:
        replay_snippet = _status_text_snippet(last_turn.title_hint) or "untitled turn"
        memory_lines.append(
            _with_cn_hint(
                f"Last turn replay: available ({replay_snippet})",
                f"上一轮回放：可用（{replay_snippet}）",
            )
        )
        if last_turn.provider != provider:
            memory_lines.append(
                _with_cn_hint(
                    "Last turn replay note: "
                    + _last_turn_replay_note(
                        replay_turn=last_turn,
                        current_provider=provider,
                    ),
                    "上一轮回放提示："
                    + _last_turn_replay_note_cn(
                        replay_turn=last_turn,
                        current_provider=provider,
                    ),
                )
            )
    if last_request_text is None:
        memory_lines.append(_with_cn_hint("Last request text: none", "上次请求文本：无"))
    else:
        memory_lines.append(
            _with_cn_hint(
                f"Last request text: {_status_text_snippet(last_request_text) or '[empty]'}",
                f"上次请求文本：{_status_text_snippet(last_request_text) or '[empty]'}",
            )
        )
        memory_lines.append(
            _with_cn_hint(
                f"Last request source: {_last_request_source_summary(last_request)}",
                f"上次请求来源：{_last_request_source_summary_cn(last_request)}",
            )
        )
        if last_request is not None:
            recorded_request_provider = _last_request_recorded_provider(
                last_request,
                current_provider=provider,
            )
            if recorded_request_provider != provider:
                memory_lines.append(
                    _with_cn_hint(
                        "Last request replay note: "
                        + _last_request_replay_note(
                            last_request=last_request,
                            current_provider=provider,
                        ),
                        "上次请求回放提示："
                        + _last_request_replay_note_cn(
                            last_request=last_request,
                            current_provider=provider,
                        ),
                    )
                )

    workspace_lines = [
        _with_cn_hint(
            f"Workspace changes: {_status_workspace_changes_summary(git_status)}",
            f"工作区变更：{_status_workspace_changes_summary_cn(git_status)}",
        ),
        *_status_workspace_change_preview_lines(git_status),
        _with_cn_hint(
            f"Context bundle: {bundle_count} item{'s' if bundle_count != 1 else ''}",
            f"上下文包：{bundle_count} 项",
        ),
        _with_cn_hint(
            f"Bundle chat: {'on' if bundle_chat_active else 'off'}",
            f"Bundle Chat：{_cn_on_off(bundle_chat_active)}",
        ),
        *_status_context_bundle_preview_lines(bundle),
    ]

    capability_lines: list[str] = []
    if session is None:
        capability_lines.append(
            _with_cn_hint(
                "Agent commands cached: unknown until a live session starts.",
                "Agent 命令缓存：要等 live session 启动后才能确定。",
            )
        )
    elif session.session_id is None:
        capability_lines.append(
            _with_cn_hint(
                "Agent commands cached: waiting for session start.",
                "Agent 命令缓存：等待会话真正启动后加载。",
            )
        )
    else:
        cached_commands = tuple(getattr(session, "available_commands", ()) or ())
        capability_lines.append(
            _with_cn_hint(
                f"Agent commands cached: {len(cached_commands)}",
                f"Agent 命令缓存：{len(cached_commands)} 条",
            )
        )
        capability_lines.extend(_status_agent_command_preview_lines(cached_commands))
        capabilities = getattr(session, "capabilities", None)
        if capabilities is not None:
            capability_lines.append(
                _with_cn_hint(
                    "Prompt input: "
                    f"img={'yes' if getattr(capabilities, 'supports_image_prompt', False) else 'no'},"
                    f"audio={'yes' if getattr(capabilities, 'supports_audio_prompt', False) else 'no'},"
                    f"docs={'yes' if getattr(capabilities, 'supports_embedded_context_prompt', False) else 'no'}",
                    "输入能力："
                    f"图片={_cn_yes_no(getattr(capabilities, 'supports_image_prompt', False))}，"
                    f"音频={_cn_yes_no(getattr(capabilities, 'supports_audio_prompt', False))}，"
                    f"文档上下文={_cn_yes_no(getattr(capabilities, 'supports_embedded_context_prompt', False))}",
                )
            )
            capability_lines.append(
                _with_cn_hint(
                    "Session control: "
                    f"fork={'yes' if getattr(capabilities, 'can_fork', False) else 'no'},"
                    f"list={'yes' if getattr(capabilities, 'can_list', False) else 'no'},"
                    f"resume={'yes' if getattr(capabilities, 'can_resume', False) else 'no'}",
                    "会话控制："
                    f"分叉={_cn_yes_no(getattr(capabilities, 'can_fork', False))}，"
                    f"列举={_cn_yes_no(getattr(capabilities, 'can_list', False))}，"
                    f"接管={_cn_yes_no(getattr(capabilities, 'can_resume', False))}",
                )
            )

    lines.append("")
    lines.append(_with_cn_hint("Current runtime:", "当前运行态："))
    lines.append("当前运行态：这里汇总 live session、模型 / 模式、用量，以及正在运行的回合。")
    lines.extend(runtime_lines)
    lines.append("")
    lines.append(_with_cn_hint("Resume and memory:", "恢复与记忆："))
    lines.append("恢复与记忆：这里放的是本地可复用内容，例如历史、Last Request、Last Turn 和待输入状态。")
    lines.extend(memory_lines)
    lines.append("")
    lines.append(_with_cn_hint("Workspace context:", "工作区上下文："))
    lines.append("工作区上下文：先看变更和 Context Bundle，再决定是直接提问还是继续整理上下文。")
    lines.extend(workspace_lines)
    lines.append("")
    lines.append(_with_cn_hint("Agent capabilities:", "Agent 能力："))
    lines.append("Agent 能力：这里说明当前 provider / session 暴露了哪些命令、输入能力和会话控制能力。")
    lines.extend(capability_lines)
    lines.append("")
    lines.append(_with_cn_hint("Controls:", "可用操作："))
    lines.append("控制中心说明：按钮按恢复、检查、调优和工作区动作分组，不需要从头到尾逐个试。")
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
        lines.append(
            "Admin-only shared-runtime switches stay here instead of the persistent keyboard."
        )
    _append_action_guide_lines(
        lines,
        entries=_status_action_guide_entries(
            active_turn=active_turn,
            pending_text_action=pending_text_action,
            pending_media_group_stats=pending_media_group_stats,
            bundle_count=bundle_count,
            bundle_chat_active=bundle_chat_active,
            last_request_available=last_request_text is not None,
            last_turn_available=last_turn_available,
            is_admin=is_admin,
            can_fork_session=can_fork_session,
            live_session_available=session_has_live_id,
            usage_available=usage_summary is not None,
            plan_count=plan_count,
            tool_activity_count=tool_activity_count,
        ),
    )

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
            primary_buttons.append(
                _callback_button(
                    ui_state,
                    user_id,
                    "Ask Agent With Context",
                    "runtime_status_control",
                    target="context_bundle_ask",
                )
            )
        else:
            primary_buttons.append(
                _callback_button(
                    ui_state,
                    user_id,
                    "Ask Agent With Context",
                    "runtime_status_control",
                    target="context_bundle_ask",
                )
            )
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
        _append_chunked_button_rows(buttons, primary_buttons)
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
    _append_chunked_button_rows(buttons, status_nav_row)
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
        _append_chunked_button_rows(buttons, control_buttons)
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
        can_fork_session
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
    if session_has_live_id:
        buttons.extend(
            _status_selection_quick_rows(
                ui_state,
                user_id=user_id,
                model_selection=model_selection,
                mode_selection=mode_selection,
                can_retry_last_turn=last_turn_available,
            )
        )
    if session_has_live_id:
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
            if last_request_text is not None:
                bundle_buttons = [
                    _callback_button(
                        ui_state,
                        user_id,
                        "Bundle + Last Request",
                        "runtime_status_control",
                        target="context_bundle_ask_last_request",
                    )
                ]
                bundle_buttons.append(
                    _callback_button(
                        ui_state,
                        user_id,
                        "Ask Agent With Context",
                        "runtime_status_control",
                        target="context_bundle_ask",
                    )
                )
            else:
                bundle_buttons = [
                    _callback_button(
                        ui_state,
                        user_id,
                        "Ask Agent With Context",
                        "runtime_status_control",
                        target="context_bundle_ask",
                    )
                ]
            _append_chunked_button_rows(buttons, bundle_buttons)
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
        _append_chunked_button_rows(buttons, change_buttons)
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
        _view_heading(
            f"Session history for {resolve_provider_profile(provider).display_name} in {workspace_label}",
            f"会话历史：{workspace_label} 中的 {resolve_provider_profile(provider).display_name}",
        )
    )
    lines.append(
        _with_cn_hint(
            "History is bot-local workspace memory for resuming saved checkpoints.",
            "会话历史：这里保存的是 bot 本地的工作区检查点，适合接回之前的进度。",
        )
    )
    buttons = []
    if not entries:
        lines.append(
            _with_cn_hint(
                "No local session history yet.",
                "当前还没有本地会话历史。",
            )
        )
        lines.append(
            _with_cn_hint(
                "Start a new session to create reusable checkpoints, or open Bot Status to keep "
                "working from the current runtime.",
                "如果你想开始积累可复用检查点，就先新建会话；如果你只是继续当前运行时，就回状态中心。",
            )
        )
        recovery_lines, recovery_buttons = _workspace_recovery_actions(
            ui_state=ui_state,
            user_id=user_id,
            provider=provider,
            workspace_id=workspace_id,
            back_target=back_target,
        )
        lines.extend(recovery_lines)
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
        buttons.extend(recovery_buttons)
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
    lines.append(
        _session_collection_next_step_line(
            can_fork=can_fork,
            can_retry_last_turn=can_retry_last_turn,
        )
    )
    lines.extend(
        _session_action_guide_lines(
            run_summary="keeps working in that saved session",
            can_fork=can_fork,
            can_retry_last_turn=can_retry_last_turn,
        )
    )
    lines.append(
        _with_cn_hint(
            "Open a session first if you want timestamps or the local-only Rename / Delete actions.",
            "如果你要看时间戳，或使用仅本地生效的重命名 / 删除，就先打开具体会话。",
        )
    )
    for offset, entry in enumerate(visible_entries, start=1):
        is_current = entry.session_id == active_session_id
        label = entry.title or entry.session_id
        if is_current:
            label = f"{label} [当前会话]"
        lines.append(f"{start + offset}. {label}")
        lines.append(_kv_hint("Updated", entry.updated_at, "更新时间"))
        history_entry_payload = _history_entry_callback_payload(
            entry=entry,
            page=page,
            back_target=back_target,
        )
        entry_buttons = [
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
                f"Open {start + offset}",
                "history_open",
                **history_entry_payload,
            ),
        ]
        _append_chunked_button_rows(
            buttons,
            entry_buttons,
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
            _append_chunked_button_rows(buttons, action_buttons)

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
        _view_heading(
            f"Session history entry for {resolve_provider_profile(provider).display_name} in {workspace_label}",
            f"会话历史详情：{workspace_label} 中的 {resolve_provider_profile(provider).display_name}",
        )
    )
    lines.append(_kv_hint("Title", _status_text_snippet(entry.title, limit=120) or "[untitled]", "标题", _status_text_snippet(entry.title, limit=120) or "[未命名]"))
    lines.append(_kv_hint("Session", entry.session_id, "会话"))
    lines.append(
        _kv_hint(
            "Current runtime session",
            "yes" if entry.session_id == active_session_id else "no",
            "当前运行态会话",
            _cn_yes_no(entry.session_id == active_session_id),
        )
    )
    lines.append(_kv_hint("Cwd", entry.cwd, "工作目录"))
    lines.append(_kv_hint("Created", entry.created_at, "创建时间"))
    lines.append(_kv_hint("Updated", entry.updated_at, "更新时间"))
    lines.append(
        _with_cn_hint(
            "Management: Rename updates only this bot-local title. Delete removes only this bot-local checkpoint.",
            "管理说明：重命名只会改这个 bot 本地标题；删除也只会移除这个 bot 本地检查点。",
        )
    )
    lines.append(
        _with_cn_hint(
            "Provider-owned sessions are not renamed or deleted from here.",
            "这里不会去改名或删除 Provider 侧持有的原始会话。",
        )
    )

    history_entry_payload = _history_entry_callback_payload(
        entry=entry,
        page=page,
        back_target=back_target,
    )
    is_current = entry.session_id == active_session_id
    can_retry_last_turn = ui_state.get_last_turn(user_id, provider, workspace_id) is not None
    lines.append(
        _session_entry_next_step_line(
            is_current=is_current,
            can_fork=can_fork,
            can_retry_last_turn=can_retry_last_turn,
        )
    )
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
        [
            _callback_button(
                ui_state,
                user_id,
                "Rename Session",
                "history_rename",
                **history_entry_payload,
                title=entry.title or entry.session_id,
            ),
            _callback_button(
                ui_state,
                user_id,
                "Delete Session",
                "history_delete",
                **history_entry_payload,
            ),
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
        _append_chunked_button_rows(buttons, action_buttons)

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
        _view_heading(
            f"Provider sessions for {resolve_provider_profile(provider).display_name} in {workspace_label}",
            f"Provider 会话：{workspace_label} 中的 {resolve_provider_profile(provider).display_name}",
        )
    )
    lines.append(
        _with_cn_hint(
            "Only sessions inside the current workspace are shown. "
            "This list comes from the provider, not the bot's local history.",
            "这里只显示当前工作区里的 Provider 会话；它来自 Provider，不是 bot 的本地历史。",
        )
    )

    buttons = []
    can_retry_last_turn = ui_state.get_last_turn(user_id, provider, workspace_id) is not None
    if not supported:
        lines.append(
            _with_cn_hint(
                "Provider session browsing is not available for this agent.",
                "当前 Agent 不支持浏览 Provider 会话。",
            )
        )
        lines.append(
            _with_cn_hint(
                "Use Session History for bot-local checkpoints, or keep working from Bot Status.",
                "如果你要接回 bot 本地检查点，就用会话历史；如果只是继续当前运行态，就回状态中心。",
            )
        )
        recovery_lines, recovery_buttons = _workspace_recovery_actions(
            ui_state=ui_state,
            user_id=user_id,
            provider=provider,
            workspace_id=workspace_id,
            back_target=back_target,
        )
        lines.extend(recovery_lines)
        buttons.extend(recovery_buttons)
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
        lines.append(_with_cn_hint("No provider sessions found.", "当前没有可浏览的 Provider 会话。"))
        lines.append(
            _with_cn_hint(
                "Start or reuse a live session, then refresh here if the provider persists reusable sessions.",
                "先启动或复用一条 live session；如果 Provider 会持久化可复用会话，再回来刷新这里。",
            )
        )
        recovery_lines, recovery_buttons = _workspace_recovery_actions(
            ui_state=ui_state,
            user_id=user_id,
            provider=provider,
            workspace_id=workspace_id,
            back_target=back_target,
        )
        lines.extend(recovery_lines)
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
        buttons.extend(recovery_buttons)
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
        lines.append(_kv_hint("Loaded sessions on this page", len(entries), "本页已加载会话"))
        if previous_cursors or next_cursor is not None:
            lines.append(_kv_hint("Cursor page", len(previous_cursors) + 1, "游标页"))
        lines.append(
            _session_collection_next_step_line(
                can_fork=can_fork,
                can_retry_last_turn=can_retry_last_turn,
            )
        )
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
                label = f"{label} [当前会话]"
            lines.append(f"{index}. {label}")
            if entry.cwd_label != ".":
                lines.append(_kv_hint("Cwd", entry.cwd_label, "工作目录"))
            lines.append(_kv_hint("Session", entry.session_id, "会话"))
            if entry.updated_at:
                lines.append(_kv_hint("Updated", entry.updated_at, "更新时间"))
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
                _append_chunked_button_rows(buttons, action_buttons)

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
        _view_heading(
            f"Session info for {resolve_provider_profile(provider).display_name} in {workspace_label}",
            f"会话信息：{workspace_label} 中的 {resolve_provider_profile(provider).display_name}",
        )
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
        lines.append(
            _with_cn_hint(
                "No live session. A session will start on the first request.",
                "当前还没有 live session；首条请求会自动创建。",
            )
        )
        recovery_lines, recovery_buttons = _workspace_recovery_actions(
            ui_state=ui_state,
            user_id=user_id,
            provider=provider,
            workspace_id=workspace_id,
            back_target="session_info",
        )
        if recovery_lines:
            lines.extend(recovery_lines)
        buttons.extend(recovery_buttons)
        _append_back_to_status_button(
            buttons,
            ui_state=ui_state,
            user_id=user_id,
            back_target=back_target,
        )
        return "\n".join(lines), InlineKeyboardMarkup(buttons)

    session_id = getattr(session, "session_id", None)
    lines.append(_kv_hint("Session", session_id or "pending", "会话"))
    if session_title is not None:
        lines.append(_kv_hint("Title", session_title, "标题"))

    session_updated_at = _status_text_snippet(getattr(session, "session_updated_at", None), limit=120)
    if session_updated_at is not None:
        lines.append(_kv_hint("Updated", session_updated_at, "更新时间"))

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
        lines.append(_with_cn_hint("Prompt capabilities:", "输入能力："))
        lines.append(
            _with_cn_hint(
                "image="
                f"{'yes' if getattr(capabilities, 'supports_image_prompt', False) else 'no'}, "
                "audio="
                f"{'yes' if getattr(capabilities, 'supports_audio_prompt', False) else 'no'}, "
                "embedded_context="
                f"{'yes' if getattr(capabilities, 'supports_embedded_context_prompt', False) else 'no'}",
                "图片="
                f"{_cn_yes_no(getattr(capabilities, 'supports_image_prompt', False))}，"
                "音频="
                f"{_cn_yes_no(getattr(capabilities, 'supports_audio_prompt', False))}，"
                "嵌入上下文="
                f"{_cn_yes_no(getattr(capabilities, 'supports_embedded_context_prompt', False))}",
            )
        )
        lines.append(_with_cn_hint("Session capabilities:", "会话能力："))
        lines.append(
            _with_cn_hint(
                "load="
                f"{'yes' if getattr(capabilities, 'can_load', False) else 'no'}, "
                "fork="
                f"{'yes' if getattr(capabilities, 'can_fork', False) else 'no'}, "
                "list="
                f"{'yes' if getattr(capabilities, 'can_list', False) else 'no'}, "
                "resume="
                f"{'yes' if getattr(capabilities, 'can_resume', False) else 'no'}",
                "加载="
                f"{_cn_yes_no(getattr(capabilities, 'can_load', False))}，"
                "分叉="
                f"{_cn_yes_no(getattr(capabilities, 'can_fork', False))}，"
                "枚举="
                f"{_cn_yes_no(getattr(capabilities, 'can_list', False))}，"
                "恢复="
                f"{_cn_yes_no(getattr(capabilities, 'can_resume', False))}",
            )
        )

    usage_summary = _status_usage_summary(session)
    lines.append(_kv_hint("Usage", usage_summary or "none", "用量", usage_summary or "无"))
    lines.append(
        _kv_hint(
            "Cached commands",
            len(tuple(getattr(session, "available_commands", ()) or ())),
            "已缓存命令",
        )
    )
    lines.append(_kv_hint("Cached plan items", len(_plan_items(session)), "已缓存计划项"))
    lines.append(_kv_hint("Cached tool activities", len(_tool_activity_items(session)), "已缓存工具活动"))
    last_request = ui_state.get_last_request(user_id, workspace_id)
    last_turn = ui_state.get_last_turn(user_id, provider, workspace_id)

    recovery_lines, recovery_buttons = _workspace_recovery_actions(
        ui_state=ui_state,
        user_id=user_id,
        provider=provider,
        workspace_id=workspace_id,
        back_target="session_info",
    )
    if recovery_buttons:
        lines.append("")
        lines.extend(recovery_lines)
        buttons.extend(recovery_buttons)
    else:
        lines.append(
            _with_cn_hint(
                "Recommended next step: send text or an attachment from chat to keep working, or "
                "use Usage / Workspace Runtime below if you want more runtime detail first.",
                "建议下一步：直接从聊天里继续发文本或附件；如果你想先看更细的运行态，再去下面的用量或工作区运行态。",
            )
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
    if last_turn is not None:
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
    if quick_buttons:
        _append_chunked_button_rows(buttons, quick_buttons)

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
        _view_heading(
            f"Usage for {resolve_provider_profile(provider).display_name} in {workspace_label}",
            f"用量信息：{workspace_label} 中的 {resolve_provider_profile(provider).display_name}",
        )
    )

    buttons: list[list[InlineKeyboardButton]] = []
    if session is None:
        lines.append(
            _with_cn_hint(
                "No live session. A session will start on the first request.",
                "当前还没有 live session；首条请求会自动创建。",
            )
        )
        recovery_lines, recovery_buttons = _workspace_recovery_actions(
            ui_state=ui_state,
            user_id=user_id,
            provider=provider,
            workspace_id=workspace_id,
            back_target=back_target,
        )
        lines.extend(recovery_lines)
        buttons.extend(recovery_buttons)
        _append_back_to_status_button(
            buttons,
            ui_state=ui_state,
            user_id=user_id,
            back_target=back_target,
        )
        markup = None if not buttons else InlineKeyboardMarkup(buttons)
        return "\n".join(lines), markup

    session_id = getattr(session, "session_id", None)
    usage_nav_buttons = [
        _callback_button(
            ui_state,
            user_id,
            "Refresh",
            "runtime_status_open",
            target="usage",
            back_target=back_target,
        )
    ]
    if back_target != "session_info":
        usage_nav_buttons.append(
            _callback_button(
                ui_state,
                user_id,
                "Session Info",
                "runtime_status_open",
                target="session_info",
                back_target="usage",
            )
        )
    buttons.append(usage_nav_buttons)

    lines.append(_kv_hint("Session", session_id or "pending", "会话"))
    if session_title is not None:
        lines.append(_kv_hint("Title", session_title, "标题"))

    session_updated_at = _status_text_snippet(getattr(session, "session_updated_at", None), limit=120)
    if session_updated_at is not None:
        lines.append(_kv_hint("Updated", session_updated_at, "更新时间"))

    usage = getattr(session, "usage", None)
    if usage is None:
        lines.append(_kv_hint("Snapshot", "none", "快照", "无"))
        lines.append(
            _with_cn_hint(
                "No cached usage snapshot for this live session.",
                "当前 live session 还没有缓存任何用量快照。",
            )
        )
        lines.append(
            _with_cn_hint(
                "This view only shows the latest ACP usage_update already cached by the bot.",
                "这个页面只展示 bot 目前已经缓存下来的最新 ACP usage_update。",
            )
        )
    else:
        lines.append(
            _with_cn_hint(
                "Snapshot: cached ACP usage_update",
                "快照：已缓存 ACP usage_update",
            )
        )
        lines.append(_kv_hint("Used", usage.used, "已用"))
        lines.append(_kv_hint("Window size", usage.size, "窗口大小"))
        remaining = _usage_remaining(usage)
        if remaining is not None:
            lines.append(_kv_hint("Remaining", remaining, "剩余"))
        utilization = _usage_utilization_percent(usage)
        if utilization is not None:
            lines.append(_kv_hint("Utilization", f"{utilization:.1f}%", "利用率"))
        lines.append(_kv_hint("Cost", _usage_cost_label(usage), "成本"))

    recovery_lines, recovery_buttons = _workspace_recovery_actions(
        ui_state=ui_state,
        user_id=user_id,
        provider=provider,
        workspace_id=workspace_id,
        back_target="usage",
    )
    if recovery_buttons:
        lines.append("")
        lines.extend(recovery_lines)
        buttons.extend(recovery_buttons)
    elif usage is None:
        lines.append(
            _with_cn_hint(
                "Recommended next step: send a request that produces a reply first, or go back to "
                "Session Info / Bot Status if you want the wider runtime snapshot.",
                "建议下一步：先发起一条能产出回复的请求；如果你想看更完整的运行态，再回会话信息或状态中心。",
            )
        )
    else:
        lines.append(
            _with_cn_hint(
                "Recommended next step: keep chatting if you just needed a usage snapshot, or open "
                "Session Info if you want the wider runtime snapshot.",
                "建议下一步：如果你只是确认用量，就继续聊天；如果你想看更完整的运行态，再打开会话信息。",
            )
        )

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
    workspace_id: str,
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
        _view_heading(
            f"Last request for {resolve_provider_profile(current_provider).display_name} in {workspace_label}",
            f"上次请求：{workspace_label} 中的 {resolve_provider_profile(current_provider).display_name}",
        )
    )

    buttons: list[list[InlineKeyboardButton]] = []
    if last_request is None:
        lines.append(
            _with_cn_hint(
                "No request text is cached for this workspace.",
                "当前工作区还没有缓存任何请求文本。",
            )
        )
        recovery_lines, recovery_buttons = _workspace_recovery_actions(
            ui_state=ui_state,
            user_id=user_id,
            provider=current_provider,
            workspace_id=workspace_id,
            back_target=back_target,
            empty_recommendation=(
                _with_cn_hint(
                    "Recommended next step: send a fresh request from chat, or use the buttons below "
                    "to go back.",
                    "建议下一步：直接在聊天里发一条新请求，或用下方按钮回到其他入口。",
                )
            ),
        )
        lines.extend(recovery_lines)
        buttons.extend(recovery_buttons)
        _append_back_to_status_button(
            buttons,
            ui_state=ui_state,
            user_id=user_id,
            back_target=back_target,
        )
        markup = None if not buttons else InlineKeyboardMarkup(buttons)
        return "\n".join(lines), markup

    recorded_provider = last_request.provider or current_provider
    lines.append(_with_cn_hint("Replay summary:", "重放概览："))
    lines.append(_kv_hint("Current provider", _replay_provider_display_name(current_provider), "当前 Provider"))
    lines.append(_kv_hint("Recorded provider", _replay_provider_display_name(recorded_provider), "记录时 Provider"))
    lines.append(_kv_hint("Recorded workspace", last_request.workspace_id, "记录时工作区"))
    lines.append(_kv_hint("Source", _last_request_source_summary(last_request), "来源"))
    lines.append(
        _with_cn_hint(
            "Replay note: "
            + _last_request_replay_note(
                last_request=last_request,
                current_provider=current_provider,
            ),
            "重放说明："
            + _last_request_replay_note(
                last_request=last_request,
                current_provider=current_provider,
            ),
        )
    )
    lines.append(
        _with_cn_hint(
            "Run Last Request sends only this text again in the current provider and workspace. "
            "It does not restore the original attachments or extra context.",
            "Run Last Request 只会在当前 Provider 和工作区重发这段文本，不会自动恢复原附件或额外上下文。",
        )
    )
    if last_turn_available:
        lines.append(
            _with_cn_hint(
                "Use Retry Last Turn or Fork Last Turn if you need the original attachments or "
                "extra context back.",
                "如果你需要把原附件或额外上下文一起带回，就用 Retry Last Turn 或 Fork Last Turn。",
            )
        )
    lines.append(_last_request_next_step_line(last_turn_available=last_turn_available))
    lines.append(
        _kv_hint(
            "Text length",
            f"{len(last_request.text)} character{'s' if len(last_request.text) != 1 else ''}",
            "文本长度",
            f"{len(last_request.text)} 字符",
        )
    )
    content, truncated = _last_turn_render_text_detail(last_request.text)
    lines.append("")
    lines.append(_with_cn_hint("Request text:", "请求文本："))
    lines.append(content or "[empty]")
    if truncated:
        lines.append(
            _with_cn_hint(
                f"[content truncated to {LAST_TURN_TEXT_DETAIL_LIMIT} characters]",
                f"[内容已截断到 {LAST_TURN_TEXT_DETAIL_LIMIT} 个字符]",
            )
        )

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
        _view_heading(
            f"Workspace runtime for {resolve_provider_profile(provider).display_name} in {workspace_label}",
            f"工作区运行态：{workspace_label} 中的 {resolve_provider_profile(provider).display_name}",
        )
    )
    lines.append(_kv_hint("Workspace ID", getattr(workspace, "id", "unknown"), "工作区 ID"))
    lines.append(_kv_hint("Path", workspace_path, "路径"))
    lines.append(_with_cn_hint("ACP client tools:", "ACP 客户端工具："))
    lines.append(
        _with_cn_hint(
            "filesystem=yes (workspace-scoped text read/write)",
            "filesystem=是（受当前工作区约束的文本读写）",
        )
    )
    lines.append(
        _with_cn_hint(
            "terminal=yes (workspace-scoped process bridge)",
            "terminal=是（受当前工作区约束的进程桥接）",
        )
    )

    mcp_servers = tuple(getattr(workspace, "mcp_servers", ()) or ())
    if not mcp_servers:
        lines.append(_kv_hint("Configured MCP servers", "none", "已配置 MCP server", "无"))
        lines.append(
            _with_cn_hint(
                "Sessions in this runtime use only the bot client filesystem/terminal bridges.",
                "这个运行态中的会话目前只使用 bot 内置的文件系统 / 终端桥接。",
            )
        )
    else:
        lines.append(_kv_hint("Configured MCP servers", len(mcp_servers), "已配置 MCP server"))
        visible_servers = mcp_servers[:WORKSPACE_RUNTIME_SERVER_PREVIEW_LIMIT]
        for index, server in enumerate(visible_servers, start=1):
            lines.append(f"{index}. {_workspace_runtime_server_summary(server)}")
        remaining = len(mcp_servers) - len(visible_servers)
        if remaining > 0:
            lines.append(f"... {remaining} more server{'s' if remaining != 1 else ''}")
        lines.append(
            _with_cn_hint(
                "New, loaded, resumed, and forked sessions inherit this MCP server set.",
                "新建、加载、恢复和分叉出来的会话都会继承这组 MCP server 配置。",
            )
        )
    lines.append(_workspace_runtime_next_step_line(has_mcp_servers=bool(mcp_servers)))

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
        _view_heading(
            f"Workspace runtime for {resolve_provider_profile(provider).display_name} in {workspace_label}",
            f"工作区运行态详情：{workspace_label} 中的 {resolve_provider_profile(provider).display_name}",
        )
    )
    lines.append(_kv_hint("Workspace ID", getattr(workspace, "id", "unknown"), "工作区 ID"))
    lines.append(_kv_hint("Path", workspace_path, "路径"))
    lines.append(_kv_hint("MCP server", f"{server_index + 1}/{server_count}", "MCP server"))
    lines.append(
        _kv_hint(
            "Name",
            _status_text_snippet(getattr(server, "name", None), limit=120) or "server",
            "名称",
            _status_text_snippet(getattr(server, "name", None), limit=120) or "server",
        )
    )
    transport = _status_text_snippet(getattr(server, "transport", None), limit=40) or "unknown"
    lines.append(_kv_hint("Transport", transport, "传输方式", transport))

    if transport == "stdio":
        lines.append(
            _kv_hint(
                "Command",
                _status_text_snippet(getattr(server, "command", None), limit=200) or "[missing]",
                "命令",
                _status_text_snippet(getattr(server, "command", None), limit=200) or "[缺失]",
            )
        )
        args = tuple(getattr(server, "args", ()) or ())
        if not args:
            lines.append(_kv_hint("Args", "none", "参数", "无"))
        else:
            lines.append(_kv_hint("Args", len(args), "参数"))
            for index, arg in enumerate(args, start=1):
                lines.append(f"{index}. {_status_text_snippet(str(arg), limit=200) or '[empty]'}")
    else:
        lines.append(
            _kv_hint(
                "URL",
                _status_text_snippet(getattr(server, "url", None), limit=200) or "[missing]",
                "URL",
                _status_text_snippet(getattr(server, "url", None), limit=200) or "[缺失]",
            )
        )

    env_items = tuple(getattr(server, "env", ()) or ())
    header_items = tuple(getattr(server, "headers", ()) or ())
    lines.append(_kv_hint("Env vars", len(env_items), "环境变量"))
    if env_items:
        lines.append(_with_cn_hint("Env keys:", "环境变量键："))
        for item in env_items:
            lines.append(_status_text_snippet(getattr(item, "name", None), limit=120) or "[empty]")
    lines.append(_kv_hint("Headers", len(header_items), "请求头"))
    if header_items:
        lines.append(_with_cn_hint("Header keys:", "请求头键："))
        for item in header_items:
            lines.append(_status_text_snippet(getattr(item, "name", None), limit=120) or "[empty]")
    lines.append(_workspace_runtime_server_next_step_line())

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
    workspace_id: str,
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
        _view_heading(
            f"Last turn for {resolve_provider_profile(current_provider).display_name} in {workspace_label}",
            f"上一轮详情：{workspace_label} 中的 {resolve_provider_profile(current_provider).display_name}",
        )
    )

    buttons: list[list[InlineKeyboardButton]] = []
    if replay_turn is None:
        lines.append(
            _with_cn_hint(
                "No replayable turn is cached.",
                "当前还没有可重放的上一轮。",
            )
        )
        recovery_lines, recovery_buttons = _workspace_recovery_actions(
            ui_state=ui_state,
            user_id=user_id,
            provider=current_provider,
            workspace_id=workspace_id,
            back_target=back_target,
            empty_recommendation=(
                _with_cn_hint(
                    "Recommended next step: send a request that finishes a turn, or use the buttons "
                    "below to go back.",
                    "建议下一步：先发起一条能够完整结束回合的请求，或用下方按钮回到其他入口。",
                )
            ),
        )
        lines.extend(recovery_lines)
        buttons.extend(recovery_buttons)
        _append_back_to_status_button(
            buttons,
            ui_state=ui_state,
            user_id=user_id,
            back_target=back_target,
        )
        markup = None if not buttons else InlineKeyboardMarkup(buttons)
        return "\n".join(lines), markup

    lines.append(_with_cn_hint("Replay summary:", "重放概览："))
    lines.append(_kv_hint("Current provider", _replay_provider_display_name(current_provider), "当前 Provider"))
    lines.append(_kv_hint("Recorded provider", _replay_provider_display_name(replay_turn.provider), "记录时 Provider"))
    lines.append(_kv_hint("Recorded workspace", replay_turn.workspace_id, "记录时工作区"))
    lines.append(
        _with_cn_hint(
            "Replay note: "
            + _last_turn_replay_note(
                replay_turn=replay_turn,
                current_provider=current_provider,
            ),
            "重放说明："
            + _last_turn_replay_note(
                replay_turn=replay_turn,
                current_provider=current_provider,
            ),
        )
    )
    lines.append(
        _kv_hint(
            "Title",
            _status_text_snippet(replay_turn.title_hint, limit=120) or "[empty]",
            "标题",
            _status_text_snippet(replay_turn.title_hint, limit=120) or "[空]",
        )
    )
    lines.append(
        _with_cn_hint(
            "Retry Last Turn replays this saved payload, including any saved attachments or extra "
            "context, in the current live session.",
            "Retry Last Turn 会在当前 live session 里重放这份已保存 payload，包括附件和额外上下文。",
        )
    )
    lines.append(
        _with_cn_hint(
            "Fork Last Turn starts a new session first, then replays the same payload there.",
            "Fork Last Turn 会先新建会话，再把同一份 payload 重放到新会话里。",
        )
    )
    lines.append(_last_turn_next_step_line())

    prompt_items = _replay_prompt_items(replay_turn)
    saved_context_items = tuple(getattr(replay_turn, "saved_context_items", ()) or ())
    if not prompt_items:
        lines.append(_kv_hint("Prompt items", 0, "输入项"))
        lines.append(_kv_hint("Saved context items", len(saved_context_items), "已保存上下文项"))
        lines.extend(_last_turn_context_preview_lines(saved_context_items))
        lines.append(
            _with_cn_hint(
                "No replay payload items are available.",
                "当前没有可重放的 payload 条目。",
            )
        )
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
    lines.append(_kv_hint("Saved context items", len(saved_context_items), "已保存上下文项"))
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
        _view_heading(
            f"Last turn for {resolve_provider_profile(current_provider).display_name} in {workspace_label}",
            f"上一轮条目：{workspace_label} 中的 {resolve_provider_profile(current_provider).display_name}",
        )
    )
    lines.append(_kv_hint("Item", f"{item_index + 1}/{total_count}", "条目"))
    lines.append(_kv_hint("Current provider", _replay_provider_display_name(current_provider), "当前 Provider"))
    lines.append(_kv_hint("Recorded provider", _replay_provider_display_name(replay_turn.provider), "记录时 Provider"))
    lines.append(_kv_hint("Recorded workspace", replay_turn.workspace_id, "记录时工作区"))
    lines.append(
        _with_cn_hint(
            "Replay note: "
            + _last_turn_replay_note(
                replay_turn=replay_turn,
                current_provider=current_provider,
            ),
            "重放说明："
            + _last_turn_replay_note(
                replay_turn=replay_turn,
                current_provider=current_provider,
            ),
        )
    )
    lines.append(
        _kv_hint(
            "Replay title",
            _status_text_snippet(replay_turn.title_hint, limit=120) or "[empty]",
            "重放标题",
            _status_text_snippet(replay_turn.title_hint, limit=120) or "[空]",
        )
    )
    lines.append(_kv_hint("Kind", _last_turn_item_kind_label(item), "类型"))

    uri = getattr(item, "uri", None)
    if uri:
        lines.append(_kv_hint("URI", uri, "URI"))
    mime_type = getattr(item, "mime_type", None)
    if mime_type:
        lines.append(_kv_hint("MIME type", mime_type, "MIME 类型"))
    payload_size = _last_turn_payload_size_bytes(item)
    if payload_size is not None:
        lines.append(
            _kv_hint(
                "Payload size",
                f"{payload_size} byte{'s' if payload_size != 1 else ''}",
                "负载大小",
                f"{payload_size} 字节",
            )
        )

    if isinstance(item, PromptText):
        content, truncated = _last_turn_render_text_detail(item.text)
        lines.append(_with_cn_hint("Content:", "内容："))
        lines.append(content or "[empty]")
        if truncated:
            lines.append(
                _with_cn_hint(
                    f"[content truncated to {LAST_TURN_TEXT_DETAIL_LIMIT} characters]",
                    f"[内容已截断到 {LAST_TURN_TEXT_DETAIL_LIMIT} 个字符]",
                )
            )
    elif isinstance(item, PromptTextResource):
        content, truncated = _last_turn_render_text_detail(item.text)
        lines.append(_with_cn_hint("Resource content:", "资源内容："))
        lines.append(content or "[empty]")
        if truncated:
            lines.append(
                _with_cn_hint(
                    f"[content truncated to {LAST_TURN_TEXT_DETAIL_LIMIT} characters]",
                    f"[内容已截断到 {LAST_TURN_TEXT_DETAIL_LIMIT} 个字符]",
                )
            )

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
    workspace_id: str,
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
        _view_heading(
            f"Agent plan for {resolve_provider_profile(provider).display_name} in {workspace_label}",
            f"Agent 计划：{workspace_label} 中的 {resolve_provider_profile(provider).display_name}",
        )
    )
    lines.append(_kv_hint("Session", session_id or "none", "会话", session_id or "无"))

    buttons = []
    if not entries:
        lines.append(_with_cn_hint("No cached agent plan.", "当前还没有缓存的 Agent 计划。"))
        lines.append(
            _with_cn_hint(
                "Plans appear here after the agent publishes structured plan updates for this session.",
                "只有当 agent 在这条会话里产出结构化计划更新后，这里才会出现内容。",
            )
        )
        recovery_lines, recovery_buttons = _workspace_recovery_actions(
            ui_state=ui_state,
            user_id=user_id,
            provider=provider,
            workspace_id=workspace_id,
            back_target=back_target,
            empty_recommendation=(
                _with_cn_hint(
                    "Recommended next step: send a request that needs planning, refresh this page "
                    "later, or use the buttons below to go back.",
                    "建议下一步：先发起一条需要规划的请求，稍后回来刷新，或用下方按钮返回其他入口。",
                )
            ),
        )
        lines.extend(recovery_lines)
        buttons.append(
            [
                _callback_button(
                    ui_state,
                    user_id,
                    "Refresh",
                    "plan_page",
                    page=0,
                    back_target=back_target,
                )
            ]
        )
        buttons.extend(recovery_buttons)
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
    lines.append(_plan_next_step_line(entries))

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
        _view_heading(
            f"Agent plan for {resolve_provider_profile(provider).display_name} in {workspace_label}",
            f"Agent 计划详情：{workspace_label} 中的 {resolve_provider_profile(provider).display_name}",
        )
    )
    lines.append(_kv_hint("Item", f"{plan_index + 1}/{total_count}", "条目"))
    lines.append(_kv_hint("Status", getattr(entry, "status", "pending"), "状态"))
    priority = _status_text_snippet(getattr(entry, "priority", None))
    if priority is not None:
        lines.append(_kv_hint("Priority", priority, "优先级"))
    lines.append(_plan_detail_next_step_line(status=str(getattr(entry, "status", "pending"))))
    lines.append(_with_cn_hint("Content:", "内容："))
    content = getattr(entry, "content", None)
    if content is None:
        lines.append(_with_cn_hint("[empty]", "[空]"))
    else:
        rendered = str(content)
        lines.append(rendered if rendered.strip() else _with_cn_hint("[empty]", "[空]"))

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
    workspace_id: str,
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
        _view_heading(
            f"Tool activity for {resolve_provider_profile(provider).display_name} in {workspace_label}",
            f"工具活动：{workspace_label} 中的 {resolve_provider_profile(provider).display_name}",
        )
    )
    lines.append(_kv_hint("Session", session_id or "none", "会话", session_id or "无"))

    buttons = []
    if not activities:
        lines.append(_with_cn_hint("No recent tool activity.", "最近还没有工具活动。"))
        lines.append(
            _with_cn_hint(
                "Tool activity appears here after the agent uses terminal, files, or other tools in "
                "this session.",
                "只有当 agent 在这条会话里使用了终端、文件或其他工具后，这里才会出现工具活动。",
            )
        )
        recovery_lines, recovery_buttons = _workspace_recovery_actions(
            ui_state=ui_state,
            user_id=user_id,
            provider=provider,
            workspace_id=workspace_id,
            back_target=back_target,
            empty_recommendation=(
                _with_cn_hint(
                    "Recommended next step: send a request that needs tool use, refresh this page "
                    "later, or use the buttons below to go back.",
                    "建议下一步：先发起一条需要用到工具的请求，稍后回来刷新，或用下方按钮回到其他入口。",
                )
            ),
        )
        lines.extend(recovery_lines)
        buttons.append(
            [
                _callback_button(
                    ui_state,
                    user_id,
                    "Refresh",
                    "tool_activity_page",
                    page=0,
                    back_target=back_target,
                )
            ]
        )
        buttons.extend(recovery_buttons)
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
    lines.append(_tool_activity_next_step_line(activities))

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
        _view_heading(
            f"Tool activity for {resolve_provider_profile(provider).display_name} in {workspace_label}",
            f"工具活动详情：{workspace_label} 中的 {resolve_provider_profile(provider).display_name}",
        )
    )
    lines.append(_kv_hint("Item", f"{activity_index + 1}/{total_count}", "条目"))
    lines.append(
        _kv_hint(
            "Title",
            _status_text_snippet(getattr(activity, "title", None)) or getattr(activity, "tool_call_id", "tool"),
            "标题",
        )
    )
    lines.append(_kv_hint("Status", getattr(activity, "status", "pending"), "状态"))
    kind = _status_text_snippet(getattr(activity, "kind", None))
    if kind is not None:
        lines.append(_kv_hint("Kind", kind, "类型"))
    lines.append(_kv_hint("Tool call", getattr(activity, "tool_call_id", "tool"), "工具调用"))
    lines.append(
        _tool_activity_detail_next_step_line(
            status=str(getattr(activity, "status", "pending")),
            has_openable_paths=bool(openable_paths),
            has_change_targets=bool(change_targets),
            has_terminal_preview=bool(terminal_previews or tuple(getattr(activity, "terminal_ids", ()) or ())),
        )
    )

    details = tuple(getattr(activity, "details", ()) or ())
    if details:
        lines.append(_with_cn_hint("Details:", "详情："))
        for index, detail in enumerate(details, start=1):
            lines.append(f"{index}. {detail}")

    content_types = tuple(getattr(activity, "content_types", ()) or ())
    if content_types:
        lines.append(_kv_hint("Content", ", ".join(content_types), "内容"))

    path_refs = tuple(getattr(activity, "path_refs", ()) or ())
    if path_refs:
        lines.append(_with_cn_hint("Paths:", "路径："))
        visible_refs = path_refs[:TOOL_ACTIVITY_PATH_BUTTON_LIMIT]
        for index, path_ref in enumerate(visible_refs, start=1):
            lines.append(f"{index}. {path_ref}")
        remaining_refs = len(path_refs) - len(visible_refs)
        if remaining_refs > 0:
            lines.append(f"... {remaining_refs} more path{'s' if remaining_refs != 1 else ''}")

    terminal_ids = tuple(getattr(activity, "terminal_ids", ()) or ())
    if terminal_ids:
        lines.append(_with_cn_hint("Terminal preview:", "终端预览："))
        if not terminal_previews:
            lines.append(_with_cn_hint("1. Output unavailable.", "1. 当前没有可用输出。"))
        else:
            for index, preview in enumerate(terminal_previews, start=1):
                lines.append(f"{index}. {preview.terminal_id} [{preview.status_label}]")
                if preview.output is None:
                    lines.append(_with_cn_hint("output: [no output]", "输出：[暂无输出]"))
                else:
                    output = preview.output
                    if preview.truncated:
                        output = f"{output}\n{_with_cn_hint('[output truncated]', '[输出已截断]')}"
                    lines.append(_with_cn_hint(f"output:\n{output}", f"输出：\n{output}"))
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
        _view_heading(
            f"Provider session for {resolve_provider_profile(provider).display_name} in {workspace_label}",
            f"Provider 会话详情：{workspace_label} 中的 {resolve_provider_profile(provider).display_name}",
        )
    )
    lines.append(
        _kv_hint(
            "Title",
            _status_text_snippet(entry.title, limit=120) or "[untitled]",
            "标题",
            _status_text_snippet(entry.title, limit=120) or "[未命名]",
        )
    )
    lines.append(_kv_hint("Session", entry.session_id, "会话"))
    lines.append(
        _kv_hint(
            "Current runtime session",
            "yes" if entry.session_id == active_session_id else "no",
            "当前运行态会话",
            _cn_yes_no(entry.session_id == active_session_id),
        )
    )
    lines.append(_kv_hint("Workspace-relative cwd", entry.cwd_label, "相对工作区目录"))
    lines.append(_kv_hint("Provider cwd", entry.cwd, "Provider 工作目录"))
    lines.append(
        _kv_hint(
            "Updated",
            entry.updated_at or "unknown",
            "更新时间",
            entry.updated_at or "未知",
        )
    )

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
    lines.append(
        _session_entry_next_step_line(
            is_current=is_current,
            can_fork=can_fork,
            can_retry_last_turn=can_retry_last_turn,
        )
    )
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
        _append_chunked_button_rows(buttons, action_buttons)

    return "\n".join(lines), InlineKeyboardMarkup(buttons)


def _build_agent_commands_view(
    *,
    commands,
    provider: str,
    workspace_id: str,
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
        _view_heading(
            f"Agent commands for {resolve_provider_profile(provider).display_name} in {workspace_label}",
            f"Agent 命令：{workspace_label} 中的 {resolve_provider_profile(provider).display_name}",
        )
    )
    lines.append(
        _kv_hint(
            "Session",
            session_id or "none (will start on first command)",
            "会话",
            session_id or "无（首条命令会自动创建）",
        )
    )

    buttons = []
    if not commands:
        lines.append(_with_cn_hint("No agent commands available.", "当前没有可用的 Agent 命令。"))
        lines.append(
            _with_cn_hint(
                "Command discovery may still be loading, or the current agent may not expose any "
                "slash commands.",
                "命令发现可能还在进行中，或者当前 Agent 本身就不暴露任何 slash 命令。",
            )
        )
        recovery_lines, recovery_buttons = _workspace_recovery_actions(
            ui_state=ui_state,
            user_id=user_id,
            provider=provider,
            workspace_id=workspace_id,
            back_target=back_target,
        )
        lines.extend(recovery_lines)
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
        buttons.extend(recovery_buttons)
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
    has_args_commands = any(bool(command.hint) for command in commands)
    lines.append(_agent_commands_next_step_line(has_args_commands=has_args_commands))

    for offset, command in enumerate(visible_commands, start=1):
        index = start + offset
        lines.append(f"{index}. {_agent_command_name(command.name)}")
        description = (command.description or "").strip()
        if description:
            lines.append(description)
        if command.hint:
            lines.append(_kv_hint("Args", command.hint, "参数"))
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

    _append_action_guide_lines(
        lines,
        entries=_agent_command_action_guide_entries(has_args_commands=has_args_commands),
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
        _view_heading(
            f"Agent command for {resolve_provider_profile(provider).display_name} in {workspace_label}",
            f"Agent 命令详情：{workspace_label} 中的 {resolve_provider_profile(provider).display_name}",
        )
    )
    lines.append(_kv_hint("Command", f"{command_index + 1}/{total_count}", "命令"))
    lines.append(
        _kv_hint(
            "Session",
            session_id or "none (will start on first command)",
            "会话",
            session_id or "无（首条命令会自动创建）",
        )
    )
    lines.append(_kv_hint("Name", _agent_command_name(command.name), "名称"))
    description = (command.description or "").strip()
    if description:
        lines.append(_with_cn_hint("Description:", "说明："))
        lines.append(description)
    else:
        lines.append(_kv_hint("Description", "none", "说明", "无"))
    if command.hint:
        lines.append(_kv_hint("Args hint", command.hint, "参数提示"))
        lines.append(_kv_hint("Example", f"{_agent_command_name(command.name)} <args>", "示例"))
    else:
        lines.append(_kv_hint("Args hint", "none", "参数提示", "无"))
        lines.append(_kv_hint("Example", _agent_command_name(command.name), "示例"))
    lines.append(_agent_command_detail_next_step_line(requires_args=bool(command.hint)))

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
        _view_heading(
            f"Workspace files for {resolve_provider_profile(provider).display_name} in {workspace_label}",
            f"工作区文件：{workspace_label} 中的 {resolve_provider_profile(provider).display_name}",
        )
    )
    lines.append(_kv_hint("Path", listing.relative_path or ".", "路径"))
    lines.append(
        _with_cn_hint(
            "Browse folders first, then decide whether to inspect, ask, or add files to context.",
            "工作区文件：先浏览目录，再决定是查看、提问，还是把文件加入上下文。",
        )
    )

    if not listing.entries:
        lines.append(_with_cn_hint("[empty directory]", "[空目录]"))
        if listing.relative_path:
            lines.append(
                _with_cn_hint(
                    "Go up, search the workspace, or open Bot Status to continue elsewhere.",
                    "你可以先回上一级、改用工作区搜索，或回状态中心从别的入口继续。",
                )
            )
        else:
            lines.append(
                _with_cn_hint(
                    "Search the workspace or open Bot Status to continue elsewhere.",
                    "你可以改用工作区搜索，或回状态中心从别的入口继续。",
                )
            )
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
    visible_file_paths = _visible_workspace_file_paths(listing, page)
    if visible_file_paths:
        lines.append(
            _workspace_collection_next_step_line(
                inspect_summary="open a file first if you want to inspect it",
                ask_label="Ask Agent With Visible Files",
                add_label="Add Visible Files to Context",
                has_last_request=last_request_text is not None,
            )
        )
    else:
        lines.append(
            _with_cn_hint(
                "Recommended next step: open a folder first if you want to keep browsing, or use "
                "Workspace Search if you already know what to look for.",
                "建议下一步：如果你还想继续浏览，就先打开一个目录；如果你已经知道要找什么，就直接用工作区搜索。",
            )
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

    if visible_file_paths:
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
                    _callback_button(
                        ui_state,
                        user_id,
                        "Ask Agent With Visible Files",
                        "workspace_page_ask_agent",
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
                        "Start Bundle Chat With Visible Files",
                        "workspace_page_start_bundle_chat",
                        relative_path=listing.relative_path,
                        page=page,
                        back_target=back_target,
                        **bundle_source_payload,
                    ),
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
        else:
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
        _view_heading(
            f"Workspace search for {resolve_provider_profile(provider).display_name} in {workspace_label}",
            f"工作区搜索：{workspace_label} 中的 {resolve_provider_profile(provider).display_name}",
        )
    )
    lines.append(_kv_hint("Query", search_results.query, "搜索词"))
    lines.append(
        _with_cn_hint(
            "Search results help you decide whether to inspect a file, ask directly, or save matching files into context.",
            "工作区搜索：先用匹配结果缩小范围，再决定是查看文件、直接提问，还是把匹配文件加入上下文。",
        )
    )

    if not search_results.matches:
        lines.append(
            _with_cn_hint(
                "No matches found.",
                "没有找到匹配结果。",
            )
        )
        lines.append(
            _with_cn_hint(
                "Try a broader query, search again, or open Workspace Files to browse manually.",
                "你可以换个更宽的关键词再搜一次，或打开工作区文件手动浏览。",
            )
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
    lines.append(
        _workspace_collection_next_step_line(
            inspect_summary="open a match first if you want to inspect it",
            ask_label="Ask Agent With Matching Files",
            add_label="Add Matching Files to Context",
            has_last_request=last_request_text is not None,
        )
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
        lines.append(_with_cn_hint("[results truncated]", "[结果已截断]"))

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
                _callback_button(
                    ui_state,
                    user_id,
                    "Ask Agent With Matching Files",
                    "workspace_search_ask_agent",
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
                    "Start Bundle Chat With Matching Files",
                    "workspace_search_start_bundle_chat",
                    query_text=search_results.query,
                    page=page,
                    back_target=back_target,
                    **bundle_source_payload,
                ),
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
    else:
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
        _view_heading(
            f"Workspace changes for {resolve_provider_profile(provider).display_name} in {workspace_label}",
            f"工作区变更：{workspace_label} 中的 {resolve_provider_profile(provider).display_name}",
        )
    )
    lines.append(
        _with_cn_hint(
            "This page focuses on current Git changes so you can inspect diffs, ask about them, or carry them into context.",
            "工作区变更：这里专门看当前 Git 变更，方便你查 diff、围绕它提问，或把它们带进上下文。",
        )
    )

    if not git_status.is_git_repo:
        lines.append(
            _with_cn_hint(
                "Current workspace is not a Git repository.",
                "当前工作区不是 Git 仓库。",
            )
        )
        lines.append(
            _with_cn_hint(
                "Use Workspace Files or Workspace Search when you still need local project context.",
                "如果你仍然需要项目本地上下文，可以改用工作区文件或工作区搜索。",
            )
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

    lines.append(_kv_hint("Branch", git_status.branch_line or "unknown", "分支", git_status.branch_line or "未知"))
    if not git_status.entries:
        lines.append(
            _with_cn_hint(
                "No working tree changes.",
                "当前工作树没有变更。",
            )
        )
        lines.append(
            _with_cn_hint(
                "Browse files, search the workspace, or send a fresh request if you are ready to keep going.",
                "如果你已经准备继续，可以去看文件、搜工作区，或直接发送一条新请求。",
            )
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
    lines.append(
        _workspace_collection_next_step_line(
            inspect_summary="open a change first if you want to inspect the diff",
            ask_label="Ask Agent With Current Changes",
            add_label="Add All Changes to Context",
            has_last_request=last_request_text is not None,
        )
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
                _callback_button(
                    ui_state,
                    user_id,
                    "Ask Agent With Current Changes",
                    "workspace_changes_ask_agent",
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
                    "Start Bundle Chat With Changes",
                    "workspace_changes_start_bundle_chat",
                    page=page,
                    back_target=back_target,
                    **bundle_source_payload,
                ),
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
    else:
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
        _view_heading(
            f"Context bundle for {resolve_provider_profile(provider).display_name} in {workspace_label}",
            f"上下文包：{workspace_label} 中的 {resolve_provider_profile(provider).display_name}",
        )
    )
    lines.append(
        _with_cn_hint(
            "Bundle is reusable local context that you can inspect, trim, or carry into the next turn.",
            "上下文包：这里放的是可复用的本地上下文，你可以检查、裁剪，或把它带进下一轮提问。",
        )
    )

    if bundle is None or not bundle.items:
        lines.append(_context_bundle_empty_text())
        lines.append(
            _with_cn_hint(
                "Add files from Workspace Files or Search, or add current Git changes, then come "
                "back here to reuse that context.",
                "先从工作区文件 / 搜索里加文件，或把当前 Git 变更加进来，然后再回这里统一复用。",
            )
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

    lines.append(_kv_hint("Items", len(bundle.items), "条目数"))
    lines.append(
        _kv_hint(
            "Bundle chat",
            "on" if bundle_chat_active else "off",
            "Bundle Chat",
            "已开启" if bundle_chat_active else "未开启",
        )
    )
    page_count = max(1, (len(bundle.items) + CONTEXT_BUNDLE_PAGE_SIZE - 1) // CONTEXT_BUNDLE_PAGE_SIZE)
    page = min(max(page, 0), page_count - 1)
    start = page * CONTEXT_BUNDLE_PAGE_SIZE
    visible_items = bundle.items[start : start + CONTEXT_BUNDLE_PAGE_SIZE]
    if page_count > 1:
        lines.append(f"Showing: {start + 1}-{start + len(visible_items)} of {len(bundle.items)}")
        lines.append(f"Page: {page + 1}/{page_count}")
    lines.append(
        _context_bundle_next_step_line(
            bundle_chat_active=bundle_chat_active,
            has_last_request=last_request_text is not None,
        )
    )

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
    lines.append(
        _with_cn_hint(
            "Ask Agent With Context starts a fresh turn with these items.",
            "Ask Agent With Context：会带着这些上下文项发起一条全新的提问。",
        )
    )
    if last_request_text is not None:
        lines.append(
            _with_cn_hint(
                "Ask With Last Request reuses the saved request text with this bundle.",
                "Ask With Last Request：会把已保存请求文本和这份上下文包一起复用。",
            )
        )
    if bundle_chat_active:
        lines.append(
            _with_cn_hint(
                "Bundle chat is on, so your next plain text message will include this bundle automatically.",
                "Bundle Chat 当前已开启，所以你接下来发送的纯文本会自动带上这份上下文包。",
            )
        )
    else:
        lines.append(
            _with_cn_hint(
                "Start Bundle Chat if you want your next plain text message to include this bundle automatically.",
                "如果你想让下一条纯文本自动带上这份上下文包，就开启 Bundle Chat。",
            )
        )

    primary_buttons = []
    if last_request_text is not None:
        primary_buttons.append(
            _callback_button(
                ui_state,
                user_id,
                "Ask With Last Request",
                "context_bundle_ask_last_request",
                page=page,
                back_target=back_target,
                **source_payload,
            )
        )
    primary_buttons.append(
        _callback_button(
            ui_state,
            user_id,
            "Ask Agent With Context",
            "context_bundle_ask",
            page=page,
            back_target=back_target,
            **source_payload,
        )
    )
    if last_request_text is None:
        primary_buttons.append(
            _callback_button(
                ui_state,
                user_id,
                "Stop Bundle Chat" if bundle_chat_active else "Start Bundle Chat",
                "context_bundle_chat_disable" if bundle_chat_active else "context_bundle_chat_enable",
                page=page,
                back_target=back_target,
                **source_payload,
            )
        )
    buttons.append(primary_buttons)
    if last_request_text is not None:
        buttons.append(
            [
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
    next_step_line: str | None = None,
    action_guide_entries: tuple[tuple[str, str], ...] = (),
    supplemental_buttons: tuple[tuple[str, str, dict[str, Any]], ...] = (),
):
    lines = [
        _view_heading(
            f"Workspace file for {resolve_provider_profile(provider).display_name} in {workspace_label}",
            f"工作区文件预览：{workspace_label} 中的 {resolve_provider_profile(provider).display_name}",
        ),
        _kv_hint("Path", preview.relative_path, "路径"),
    ]
    if preview.is_binary:
        lines.append(preview.text)
    else:
        lines.append(preview.text)
        if preview.truncated:
            lines.append(_with_cn_hint("[preview truncated]", "[预览已截断]"))

    if next_step_line is not None:
        lines.append(next_step_line)
    _append_action_guide_lines(lines, entries=action_guide_entries)

    if last_request_text is not None:
        buttons = [
            [
                _callback_button(
                    ui_state,
                    user_id,
                    "Ask With Last Request",
                    "workspace_file_ask_last_request",
                    **quick_ask_payload,
                ),
                _callback_button(
                    ui_state,
                    user_id,
                    "Ask Agent About File",
                    "workspace_file_ask_agent",
                    **ask_payload,
                ),
            ],
            [
                _callback_button(
                    ui_state,
                    user_id,
                    secondary_button_label,
                    secondary_button_action,
                    **secondary_button_payload,
                )
            ],
        ]
    else:
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
    next_step_line: str | None = None,
    action_guide_entries: tuple[tuple[str, str], ...] = (),
    supplemental_buttons: tuple[tuple[str, str, dict[str, Any]], ...] = (),
):
    lines = [
        _view_heading(
            f"Workspace change for {resolve_provider_profile(provider).display_name} in {workspace_label}",
            f"工作区变更预览：{workspace_label} 中的 {resolve_provider_profile(provider).display_name}",
        ),
        _kv_hint("Path", diff_preview.relative_path, "路径"),
        _kv_hint("Status", diff_preview.status_code, "状态"),
        diff_preview.text,
    ]
    if diff_preview.truncated:
        lines.append(_with_cn_hint("[diff preview truncated]", "[diff 预览已截断]"))

    if next_step_line is not None:
        lines.append(next_step_line)
    _append_action_guide_lines(lines, entries=action_guide_entries)

    if last_request_text is not None:
        buttons = [
            [
                _callback_button(
                    ui_state,
                    user_id,
                    "Ask With Last Request",
                    "workspace_change_ask_last_request",
                    **quick_ask_payload,
                ),
                _callback_button(
                    ui_state,
                    user_id,
                    "Ask Agent About Change",
                    "workspace_change_ask_agent",
                    **ask_payload,
                ),
            ],
            [
                _callback_button(
                    ui_state,
                    user_id,
                    secondary_button_label,
                    secondary_button_action,
                    **secondary_button_payload,
                )
            ],
        ]
    else:
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
        _view_heading(
            f"Model / Mode for {resolve_provider_profile(provider).display_name} in {workspace_label}",
            f"模型 / 模式：{workspace_label} 中的 {resolve_provider_profile(provider).display_name}",
        )
    )
    lines.append(_kv_hint("Session", session_id or "pending", "会话"))
    current_setup = _model_mode_current_setup_line(
        model_selection=model_selection,
        mode_selection=mode_selection,
    )
    if current_setup is not None:
        lines.append(current_setup)
    if model_selection is None and mode_selection is not None:
        lines.append(
            _with_cn_hint(
                "Model controls are not exposed in this session. Use the available mode controls "
                "below, or keep chatting normally if you do not need to change it.",
                "当前 session 没有暴露模型控制。你可以继续使用下面可用的模式控制，或者直接正常聊天。",
            )
        )
    if mode_selection is None and model_selection is not None:
        lines.append(
            _with_cn_hint(
                "Mode controls are not exposed in this session. Use the available model controls "
                "below, or keep chatting normally if you do not need to change it.",
                "当前 session 没有暴露模式控制。你可以继续使用下面可用的模型控制，或者直接正常聊天。",
            )
        )
    lines.append(_model_mode_next_step_line(can_retry_last_turn=can_retry_last_turn))
    lines.append(_with_cn_hint("This updates the current live session in place.", "这些设置会直接更新当前 live session。"))
    if can_retry_last_turn:
        lines.append(
            _with_cn_hint(
                "Shortcut: use ...+Retry to rerun the last turn immediately with the updated setting.",
                "快捷方式：可以直接用 ...+Retry 在新设置下立刻重跑上一轮。",
            )
        )
    else:
        lines.append(
            _with_cn_hint(
                "Open a choice first if you want to inspect its details before switching.",
                "如果你想先比较细节，再点开具体选项。",
            )
        )
    lines.append("")
    buttons = []

    if model_selection is not None:
        lines.extend(
            _selection_overview_lines(
                prefix="Model",
                selection=model_selection,
                can_retry_last_turn=can_retry_last_turn,
            )
        )
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
        lines.extend(
            _selection_overview_lines(
                prefix="Mode",
                selection=mode_selection,
                can_retry_last_turn=can_retry_last_turn,
            )
        )
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

    _append_action_guide_lines(
        lines,
        entries=_model_mode_action_guide_entries(can_retry_last_turn=can_retry_last_turn),
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
    parts_cn = []
    if model_selection is not None:
        parts.append(f"model={_current_choice_label(model_selection)}")
        parts_cn.append(f"模型={_current_choice_label(model_selection)}")
    if mode_selection is not None:
        parts.append(f"mode={_current_choice_label(mode_selection)}")
        parts_cn.append(f"模式={_current_choice_label(mode_selection)}")
    if not parts:
        return None
    return _with_cn_hint(
        "Current setup: " + ", ".join(parts),
        "当前设置：" + "，".join(parts_cn),
    )


def _selection_overview_lines(
    *,
    prefix: str,
    selection,
    can_retry_last_turn: bool,
) -> list[str]:
    prefix_cn = _selection_kind_label_cn(prefix.lower())
    lines = [_with_cn_hint(f"{prefix} choices:", f"{prefix_cn}选项：")]
    for choice_index, choice in enumerate(selection.choices, start=1):
        current_suffix = " [当前]" if choice.value == selection.current_value else ""
        lines.append(f"{choice_index}. {choice.label}{current_suffix}")
    if can_retry_last_turn:
        lines.append(
            _with_cn_hint(
                f"Tap {prefix}: ... to switch now, use {prefix}+Retry: ... to rerun the last turn, "
                f"or Open {prefix} N for details.",
                f"可以直接点 {prefix_cn}：... 立即切换，也可以用 {prefix_cn}+Retry: ... 重跑上一轮，或先打开具体 {prefix_cn} 查看详情。",
            )
        )
    else:
        lines.append(
            _with_cn_hint(
                f"Tap {prefix}: ... to switch now, or Open {prefix} N for details.",
                f"可以直接点 {prefix_cn}：... 立即切换，或先打开具体 {prefix_cn} 查看详情。",
            )
        )
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


def _selection_kind_label_cn(kind: str) -> str:
    if kind == "model":
        return "模型"
    if kind == "mode":
        return "模式"
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
    kind_label_cn = _selection_kind_label_cn(selection.kind)
    is_current = choice.value == selection.current_value
    lines.append(
        _view_heading(
            f"{kind_label} choice for {resolve_provider_profile(provider).display_name} in {workspace_label}",
            f"{kind_label_cn}选项详情：{workspace_label} 中的 {resolve_provider_profile(provider).display_name}",
        )
    )
    lines.append(_kv_hint("Session", session_id or "pending", "会话"))
    lines.append(_kv_hint("Choice", f"{choice_index + 1}/{len(selection.choices)}", "选项"))
    lines.append(_kv_hint("Current selection", _current_choice_label(selection), "当前选择"))
    lines.append(
        _kv_hint(
            "This choice is current",
            "yes" if is_current else "no",
            "当前是否已生效",
            _cn_yes_no(is_current),
        )
    )
    lines.append(_kv_hint("Label", choice.label, "标签"))
    lines.append(_kv_hint("Value", choice.value, "值"))
    if selection.config_id:
        lines.append(_kv_hint("Config option", selection.config_id, "配置项"))
    description = _status_text_snippet(getattr(choice, "description", None), limit=400)
    if description is None:
        lines.append(_kv_hint("Description", "none", "说明", "无"))
    else:
        lines.append(_with_cn_hint("Description:", "说明："))
        lines.append(description)
    lines.append(
        _with_cn_hint(
            "Effect: this updates the current live session in place.",
            "作用：会直接更新当前 live session。",
        )
    )
    if is_current:
        lines.append(
            _with_cn_hint(
                f"Recommended next step: go back to Model / Mode, or inspect another {kind_label.lower()} choice.",
                f"建议下一步：返回模型 / 模式，或继续比较其他{kind_label_cn}选项。",
            )
        )
    elif can_retry_last_turn:
        lines.append(
            _with_cn_hint(
                f"Recommended next step: tap Use {kind_label} to switch now, or Use {kind_label} + Retry "
                "to rerun the last turn immediately.",
                f"建议下一步：直接点 Use {kind_label} 立即切换，或用 Use {kind_label} + Retry 立刻重跑上一轮。",
            )
        )
    else:
        lines.append(
            _with_cn_hint(
                f"Recommended next step: tap Use {kind_label} to switch now, or go back to compare another choice.",
                f"建议下一步：直接点 Use {kind_label} 立即切换，或返回去比较其他选项。",
            )
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
