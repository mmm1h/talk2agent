import asyncio

from acp.helpers import update_agent_message_text
from acp.schema import ToolCallProgress, ToolCallStart


def run(coro):
    return asyncio.run(coro)


class FakeClock:
    def __init__(self, now=0.0):
        self.now = now

    def __call__(self):
        return self.now


class FakeMessage:
    def __init__(self):
        self.reply_calls = []
        self.reply_markups = []
        self.draft_calls = []

    async def reply_text(self, text, reply_markup=None):
        self.reply_calls.append(text)
        self.reply_markups.append(reply_markup)
        return FakeMessage()

    async def reply_text_draft(self, draft_id, text):
        self.draft_calls.append((draft_id, text))
        return True


def test_render_update_text_returns_agent_message_text():
    from talk2agent.bots.telegram_stream import render_update_text

    assert render_update_text(update_agent_message_text("hello from agent")) == "hello from agent"


def test_render_update_text_formats_tool_events():
    from talk2agent.bots.telegram_stream import render_update_text

    start = ToolCallStart(
        sessionUpdate="tool_call",
        toolCallId="tool-1",
        title="Search docs",
        status="in_progress",
    )
    progress = ToolCallProgress(
        sessionUpdate="tool_call_update",
        toolCallId="tool-1",
        title="Search docs",
        status="completed",
    )
    ignored = ToolCallProgress(
        sessionUpdate="tool_call_update",
        toolCallId="tool-1",
        title="Search docs",
        status="in_progress",
    )

    assert render_update_text(start) == "\n[tool] Search docs\n"
    assert render_update_text(progress) == "[tool completed] Search docs\n"
    assert render_update_text(ignored) is None


def test_split_telegram_text_chunks_long_text_to_limit():
    from talk2agent.bots.telegram_stream import split_telegram_text

    assert split_telegram_text("abcdefghij", limit=4) == ["abcd", "efgh", "ij"]


def test_start_and_updates_send_draft_messages():
    from talk2agent.bots.telegram_stream import TelegramTurnStream

    clock = FakeClock()
    message = FakeMessage()
    stream = TelegramTurnStream(
        message=message,
        clock=clock,
        edit_interval=1.0,
        draft_id=99,
    )

    async def scenario():
        await stream.start()
        clock.now = 0.2
        await stream.on_update(update_agent_message_text("hello "))
        clock.now = 1.3
        await stream.on_update(update_agent_message_text("world"))

    run(scenario())

    assert message.draft_calls == [(99, "Thinking..."), (99, "hello world")]
    assert message.reply_calls == []


def test_on_update_keeps_preview_moving_after_text_limit_is_reached():
    from talk2agent.bots.telegram_stream import TelegramTurnStream

    clock = FakeClock()
    message = FakeMessage()
    stream = TelegramTurnStream(
        message=message,
        clock=clock,
        edit_interval=0.0,
        text_limit=4,
        draft_id=99,
    )

    async def scenario():
        await stream.on_update(update_agent_message_text("abcd"))
        await stream.on_update(update_agent_message_text("ef"))
        await stream.on_update(update_agent_message_text("gh"))

    run(scenario())

    assert message.draft_calls == [(99, "abcd"), (99, "cdef"), (99, "efgh")]


def test_finish_sends_overflow_chunks_with_reply_text():
    from talk2agent.bots.telegram_stream import TelegramTurnStream

    message = FakeMessage()
    stream = TelegramTurnStream(
        message=message,
        edit_interval=60.0,
        text_limit=4,
    )

    async def scenario():
        await stream.on_update(update_agent_message_text("abcdefghij"))
        await stream.finish(stop_reason="completed")

    run(scenario())

    assert message.reply_calls == ["abcd", "efgh", "ij"]


def test_finish_uses_empty_response_fallback_when_no_text_buffered():
    from talk2agent.bots.telegram_stream import TelegramTurnStream

    message = FakeMessage()
    stream = TelegramTurnStream(message=message)

    run(stream.finish(stop_reason="completed"))

    assert message.reply_calls == ["[empty response] stop_reason=completed"]


def test_fail_sends_error_message():
    from talk2agent.bots.telegram_stream import TelegramTurnStream

    message = FakeMessage()
    stream = TelegramTurnStream(message=message)

    run(stream.fail("Request failed."))

    assert message.reply_calls == ["Request failed."]


def test_fail_sends_error_message_with_reply_markup():
    from telegram import InlineKeyboardMarkup

    from talk2agent.bots.telegram_stream import TelegramTurnStream

    message = FakeMessage()
    stream = TelegramTurnStream(message=message)
    markup = InlineKeyboardMarkup([])

    run(stream.fail("Request failed.", reply_markup=markup))

    assert message.reply_calls == ["Request failed."]
    assert message.reply_markups == [markup]
