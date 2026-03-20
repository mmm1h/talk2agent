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
        self.edit_calls = []
        self.reply_calls = []

    async def edit_text(self, text):
        self.edit_calls.append(text)

    async def reply_text(self, text):
        self.reply_calls.append(text)


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


def test_on_update_edits_placeholder_when_interval_elapsed():
    from talk2agent.bots.telegram_stream import TelegramTurnStream

    clock = FakeClock()
    message = FakeMessage()
    stream = TelegramTurnStream(
        placeholder=message,
        clock=clock,
        edit_interval=1.0,
    )

    async def scenario():
        clock.now = 0.2
        await stream.on_update(update_agent_message_text("hello "))
        clock.now = 1.3
        await stream.on_update(update_agent_message_text("world"))

    run(scenario())

    assert message.edit_calls == ["hello world"]
    assert message.reply_calls == []


def test_on_update_keeps_preview_moving_after_text_limit_is_reached():
    from talk2agent.bots.telegram_stream import TelegramTurnStream

    clock = FakeClock()
    message = FakeMessage()
    stream = TelegramTurnStream(
        placeholder=message,
        clock=clock,
        edit_interval=0.0,
        text_limit=4,
    )

    async def scenario():
        await stream.on_update(update_agent_message_text("abcd"))
        await stream.on_update(update_agent_message_text("ef"))
        await stream.on_update(update_agent_message_text("gh"))

    run(scenario())

    assert message.edit_calls == ["abcd", "cdef", "efgh"]


def test_finish_sends_overflow_chunks_with_reply_text():
    from talk2agent.bots.telegram_stream import TelegramTurnStream

    clock = FakeClock()
    message = FakeMessage()
    stream = TelegramTurnStream(
        placeholder=message,
        clock=clock,
        edit_interval=60.0,
        text_limit=4,
    )

    async def scenario():
        await stream.on_update(update_agent_message_text("abcdefghij"))
        await stream.finish(stop_reason="completed")

    run(scenario())

    assert message.edit_calls == ["abcd"]
    assert message.reply_calls == ["efgh", "ij"]


def test_finish_uses_empty_response_fallback_when_no_text_buffered():
    from talk2agent.bots.telegram_stream import TelegramTurnStream

    message = FakeMessage()
    stream = TelegramTurnStream(placeholder=message)

    run(stream.finish(stop_reason="completed"))

    assert message.edit_calls == ["[empty response] stop_reason=completed"]
    assert message.reply_calls == []


def test_fail_replaces_placeholder_with_error_text():
    from talk2agent.bots.telegram_stream import TelegramTurnStream

    message = FakeMessage()
    stream = TelegramTurnStream(placeholder=message)

    run(stream.fail("Request failed."))

    assert message.edit_calls == ["Request failed."]
