from __future__ import annotations

import secrets
import time

from acp.schema import AgentMessageChunk, AgentPlanUpdate, ToolCallProgress, ToolCallStart, UsageUpdate
from talk2agent.acp.tool_activity import render_tool_update_text


PLAN_PREVIEW_LIMIT = 5


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


def split_telegram_text(text, limit=4000):
    if limit <= 0:
        raise ValueError("limit must be positive")

    if text == "":
        return [""]

    return [text[index : index + limit] for index in range(0, len(text), limit)]


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
        self._usage_footer = None

    async def start(self):
        await self._send_draft("Thinking...")

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

    async def finish(self, stop_reason):
        text = self._full_text()
        if self._usage_footer:
            if text:
                separator = "" if text.endswith("\n") else "\n"
                text = f"{text}{separator}{self._usage_footer}"
            else:
                text = self._usage_footer
        if text == "":
            if stop_reason == "cancelled":
                text = "Turn cancelled."
            else:
                text = f"[empty response] stop_reason={stop_reason}"

        chunks = split_telegram_text(text, limit=self._text_limit)
        for chunk in chunks:
            await self._message.reply_text(chunk)

    async def fail(self, text, reply_markup=None):
        await self._message.reply_text(text, reply_markup=reply_markup)

    def _full_text(self):
        return "".join(self._fragments)

    def _preview_text(self):
        text = self._full_text()
        if len(text) <= self._text_limit:
            return text
        return text[-self._text_limit :]

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
