from __future__ import annotations

import secrets
import time

from acp.schema import AgentMessageChunk, ToolCallProgress, ToolCallStart


def render_update_text(update):
    if isinstance(update, AgentMessageChunk):
        return getattr(update.content, "text", None)

    if isinstance(update, ToolCallStart):
        return f"\n[tool] {update.title}\n"

    if isinstance(update, ToolCallProgress) and update.status == "completed":
        title = update.title or update.toolCallId
        return f"[tool completed] {title}\n"

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

    async def start(self):
        await self._send_draft("Thinking...")

    async def on_update(self, update):
        fragment = render_update_text(update)
        if not fragment:
            return

        self._fragments.append(fragment)
        if self._clock() - self._last_edit_at < self._edit_interval:
            return

        await self._send_draft(self._preview_text())

    async def finish(self, stop_reason):
        text = self._full_text()
        if text == "":
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
