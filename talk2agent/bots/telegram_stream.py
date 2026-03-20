from __future__ import annotations

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
        placeholder,
        *,
        clock=None,
        edit_interval=0.75,
        text_limit=4000,
    ):
        self._placeholder = placeholder
        self._clock = time.monotonic if clock is None else clock
        self._edit_interval = edit_interval
        self._text_limit = text_limit
        self._fragments = []
        self._last_edit_at = self._clock()
        self._last_placeholder_text = None

    async def on_update(self, update):
        fragment = render_update_text(update)
        if not fragment:
            return

        self._fragments.append(fragment)
        if self._clock() - self._last_edit_at < self._edit_interval:
            return

        await self._edit_placeholder(self._preview_text())

    async def finish(self, stop_reason):
        text = self._full_text()
        if text == "":
            text = f"[empty response] stop_reason={stop_reason}"

        chunks = split_telegram_text(text, limit=self._text_limit)
        await self._edit_placeholder(chunks[0])

        for chunk in chunks[1:]:
            await self._placeholder.reply_text(chunk)

    async def fail(self, text):
        await self._placeholder.edit_text(text)
        self._last_placeholder_text = text
        self._last_edit_at = self._clock()

    def _full_text(self):
        return "".join(self._fragments)

    def _preview_text(self):
        text = self._full_text()
        if len(text) <= self._text_limit:
            return text
        return text[-self._text_limit :]

    async def _edit_placeholder(self, text):
        if text == self._last_placeholder_text:
            return

        await self._placeholder.edit_text(text)
        self._last_placeholder_text = text
        self._last_edit_at = self._clock()
