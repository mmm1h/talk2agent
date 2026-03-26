from __future__ import annotations

import secrets
import time

from acp.schema import AgentMessageChunk, AgentPlanUpdate, ToolCallProgress, ToolCallStart, UsageUpdate
from talk2agent.acp.tool_activity import render_tool_update_text


PLAN_PREVIEW_LIMIT = 5
TRUNCATED_PREVIEW_PREFIX = "..."
TRUNCATED_PREVIEW_MIN_LIMIT = 32
TRUNCATED_PREVIEW_SEPARATOR_SCAN_LIMIT = 80
INITIAL_DRAFT_TEXT = (
    "Working on your request...\n"
    "I'll stream progress here. Use /cancel or Cancel / Stop to interrupt."
)
FALLBACK_PROGRESS_TEXT = (
    "Working on your request...\n"
    "Streaming preview is unavailable right now, so I'll send the final reply when it's ready. "
    "Use /cancel or Cancel / Stop to interrupt."
)


def _normalize_inline_text(text):
    return " ".join(str(text).split())


def _plan_entry_prefix(status):
    if status == "completed":
        return "[x]"
    if status == "in_progress":
        return "[>]"
    return "[ ]"


def render_usage_text(update):
    if not isinstance(update, UsageUpdate):
        return None

    parts = [f"used={update.used}", f"size={update.size}"]
    cost = getattr(update, "cost", None)
    amount = getattr(cost, "amount", None)
    currency = getattr(cost, "currency", None)
    if amount is not None and currency:
        parts.append(f"cost={amount:.2f} {currency}")
    elif amount is not None:
        parts.append(f"cost={amount:.2f}")
    return f"[usage] {' '.join(parts)}"


def render_update_text(update):
    if isinstance(update, AgentMessageChunk):
        return getattr(update.content, "text", None)

    if isinstance(update, AgentPlanUpdate):
        entries = tuple(getattr(update, "entries", ()) or ())
        if not entries:
            return "\n[plan] empty\n"

        lines = ["\n[plan]\n"]
        for index, entry in enumerate(entries[:PLAN_PREVIEW_LIMIT], start=1):
            content = _normalize_inline_text(getattr(entry, "content", ""))
            if not content:
                continue
            lines.append(
                f"{index}. {_plan_entry_prefix(getattr(entry, 'status', 'pending'))} {content}\n"
            )
        remaining = len(entries) - PLAN_PREVIEW_LIMIT
        if remaining > 0:
            lines.append(f"... {remaining} more\n")
        return "".join(lines)

    tool_text = render_tool_update_text(update)
    if tool_text is not None:
        return tool_text

    return None


def _preferred_split_index(text, limit):
    for separator in ("\n\n", "\n", " "):
        separator_index = text.rfind(separator, 0, limit + 1)
        if separator_index > 0:
            return separator_index, separator_index + len(separator)
    return limit, limit


def split_telegram_text(text, limit=4000):
    if limit <= 0:
        raise ValueError("limit must be positive")

    if text == "":
        return [""]

    chunks = []
    remaining = text
    while len(remaining) > limit:
        split_at, next_start = _preferred_split_index(remaining, limit)
        chunk = remaining[:split_at]
        if chunk == "":
            chunk = remaining[:limit]
            next_start = limit
        chunks.append(chunk)
        remaining = remaining[next_start:]
    chunks.append(remaining)
    return chunks


class TelegramTurnStream:
    def __init__(
        self,
        message,
        *,
        clock=None,
        edit_interval=0.75,
        text_limit=4000,
        draft_id: int | None = None,
    ):
        self._message = message
        self._clock = time.monotonic if clock is None else clock
        self._edit_interval = edit_interval
        self._text_limit = text_limit
        self._draft_id = draft_id or (secrets.randbelow(2_147_483_647) + 1)
        self._fragments = []
        self._last_edit_at = self._clock()
        self._last_draft_text = None
        self._draft_started = False
        self._draft_enabled = True
        self._progress_notice_sent = False
        self._usage_footer = None

    async def start(self):
        await self._send_draft(INITIAL_DRAFT_TEXT)
        if not self._draft_started:
            await self._send_progress_notice(FALLBACK_PROGRESS_TEXT)

    async def on_update(self, update):
        usage_footer = render_usage_text(update)
        if usage_footer is not None:
            self._usage_footer = usage_footer
            return

        fragment = render_update_text(update)
        if not fragment:
            return

        self._fragments.append(fragment)
        if self._clock() - self._last_edit_at < self._edit_interval:
            return

        await self._send_draft(self._preview_text())

    async def finish(self, stop_reason, *, reply_markup=None):
        text = self._full_text()
        if self._usage_footer:
            if text:
                separator = "" if text.endswith("\n") else "\n"
                text = f"{text}{separator}{self._usage_footer}"
            else:
                text = self._usage_footer
        if text == "":
            if stop_reason == "cancelled":
                text = "Turn cancelled. Send a new request when ready."
            elif stop_reason == "completed":
                text = "The agent finished without a visible reply. Open Bot Status for details or try again."
            else:
                text = (
                    "The agent finished without a visible reply. "
                    f"Open Bot Status for details or try again. (stop_reason={stop_reason})"
                )

        chunks = split_telegram_text(text, limit=self._text_limit)
        last_chunk_index = len(chunks) - 1
        for index, chunk in enumerate(chunks):
            await self._message.reply_text(
                chunk,
                reply_markup=reply_markup if index == last_chunk_index else None,
            )

    async def fail(self, text, reply_markup=None):
        await self._message.reply_text(text, reply_markup=reply_markup)

    def _full_text(self):
        return "".join(self._fragments)

    def _preview_text(self):
        text = self._full_text()
        if len(text) <= self._text_limit:
            return text
        if self._text_limit < TRUNCATED_PREVIEW_MIN_LIMIT:
            return text[-self._text_limit :]

        tail = text[-(self._text_limit - len(TRUNCATED_PREVIEW_PREFIX)) :]
        scan_limit = min(len(tail), TRUNCATED_PREVIEW_SEPARATOR_SCAN_LIMIT)
        for separator in ("\n\n", "\n", " "):
            separator_index = tail.find(separator)
            if 0 < separator_index < scan_limit:
                tail = tail[separator_index + len(separator) :]
                break
        return f"{TRUNCATED_PREVIEW_PREFIX}{tail}"

    async def _send_draft(self, text):
        if not self._draft_enabled or text == self._last_draft_text:
            return

        try:
            await self._message.reply_text_draft(self._draft_id, text)
        except Exception:
            self._draft_enabled = False
            return

        self._draft_started = True
        self._last_draft_text = text
        self._last_edit_at = self._clock()

    async def _send_progress_notice(self, text):
        if self._progress_notice_sent:
            return
        await self._message.reply_text(text)
        self._progress_notice_sent = True
