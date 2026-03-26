import asyncio

from acp.helpers import update_agent_message_text
from acp.schema import (
    AgentPlanUpdate,
    Cost,
    PlanEntry,
    ToolCallLocation,
    ToolCallProgress,
    ToolCallStart,
    UsageUpdate,
)


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


class FailingDraftMessage(FakeMessage):
    async def reply_text_draft(self, draft_id, text):
        raise RuntimeError("draft unavailable")


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


def test_render_update_text_formats_tool_event_details():
    from talk2agent.bots.telegram_stream import render_update_text

    start = ToolCallStart(
        sessionUpdate="tool_call",
        toolCallId="tool-1",
        title="Run tests",
        kind="execute",
        status="in_progress",
        rawInput={"command": "python -m pytest -q"},
        locations=[ToolCallLocation(path="tests/test_app.py", line=12)],
    )
    completed = ToolCallProgress(
        sessionUpdate="tool_call_update",
        toolCallId="tool-1",
        title="Run tests",
        kind="execute",
        status="completed",
        rawInput={"command": "python -m pytest -q"},
        locations=[ToolCallLocation(path="tests/test_app.py", line=12)],
    )

    assert render_update_text(start) == (
        "\n[tool] Run tests [execute]\n"
        "cmd: python -m pytest -q\n"
        "paths: tests/test_app.py:12\n"
    )
    assert render_update_text(completed) == (
        "[tool completed] Run tests [execute]\n"
        "cmd: python -m pytest -q\n"
        "paths: tests/test_app.py:12\n"
    )


def test_render_update_text_formats_plan_updates():
    from talk2agent.bots.telegram_stream import render_update_text

    update = AgentPlanUpdate(
        sessionUpdate="plan",
        entries=[
            PlanEntry(
                content="Audit runtime status",
                status="in_progress",
                priority="high",
            ),
            PlanEntry(
                content="Update Telegram tests",
                status="pending",
                priority="medium",
            ),
        ],
    )

    assert render_update_text(update) == (
        "\n[plan]\n"
        "1. [>] Audit runtime status\n"
        "2. [ ] Update Telegram tests\n"
    )


def test_split_telegram_text_chunks_long_text_to_limit():
    from talk2agent.bots.telegram_stream import split_telegram_text

    assert split_telegram_text("abcdefghij", limit=4) == ["abcd", "efgh", "ij"]


def test_split_telegram_text_prefers_newlines_and_spaces_when_possible():
    from talk2agent.bots.telegram_stream import split_telegram_text

    assert split_telegram_text("alpha beta gamma", limit=10) == ["alpha beta", "gamma"]
    assert split_telegram_text("line1\nline2\nline3", limit=11) == ["line1\nline2", "line3"]


def test_start_and_updates_send_draft_messages():
    from talk2agent.bots.telegram_stream import INITIAL_DRAFT_TEXT, TelegramTurnStream

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

    assert message.draft_calls == [(99, INITIAL_DRAFT_TEXT), (99, "hello world")]
    assert message.reply_calls == []


def test_start_sends_plain_progress_notice_when_draft_preview_is_unavailable():
    from talk2agent.bots.telegram_stream import FALLBACK_PROGRESS_TEXT, TelegramTurnStream

    message = FailingDraftMessage()
    stream = TelegramTurnStream(message=message)

    run(stream.start())

    assert message.draft_calls == []
    assert message.reply_calls == [FALLBACK_PROGRESS_TEXT]


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


def test_on_update_marks_truncated_preview_for_large_limits():
    from talk2agent.bots.telegram_stream import TelegramTurnStream

    clock = FakeClock()
    message = FakeMessage()
    stream = TelegramTurnStream(
        message=message,
        clock=clock,
        edit_interval=0.0,
        text_limit=32,
        draft_id=99,
    )

    async def scenario():
        await stream.on_update(update_agent_message_text("alpha beta gamma delta "))
        await stream.on_update(update_agent_message_text("epsilon zeta eta theta"))

    run(scenario())

    assert message.draft_calls[0] == (99, "alpha beta gamma delta ")
    assert message.draft_calls[1][0] == 99
    assert message.draft_calls[1][1].startswith("...")
    assert message.draft_calls[1][1].endswith("epsilon zeta eta theta")


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


def test_finish_attaches_reply_markup_to_last_chunk_only():
    from telegram import InlineKeyboardMarkup

    from talk2agent.bots.telegram_stream import TelegramTurnStream

    message = FakeMessage()
    stream = TelegramTurnStream(
        message=message,
        edit_interval=60.0,
        text_limit=4,
    )
    markup = InlineKeyboardMarkup([])

    async def scenario():
        await stream.on_update(update_agent_message_text("abcdefghij"))
        await stream.finish(stop_reason="completed", reply_markup=markup)

    run(scenario())

    assert message.reply_calls == ["abcd", "efgh", "ij"]
    assert message.reply_markups == [None, None, markup]


def test_finish_appends_last_usage_update_once():
    from talk2agent.bots.telegram_stream import TelegramTurnStream

    message = FakeMessage()
    stream = TelegramTurnStream(message=message)

    async def scenario():
        await stream.on_update(update_agent_message_text("hello from agent"))
        await stream.on_update(
            UsageUpdate(
                sessionUpdate="usage_update",
                used=128,
                size=1024,
                cost=Cost(amount=0.1, currency="USD"),
            )
        )
        await stream.finish(stop_reason="completed")

    run(scenario())

    assert message.reply_calls == ["hello from agent\n[usage] used=128 size=1024 cost=0.10 USD"]


def test_finish_uses_empty_response_fallback_when_no_text_buffered():
    from talk2agent.bots.telegram_stream import TelegramTurnStream

    message = FakeMessage()
    stream = TelegramTurnStream(message=message)

    run(stream.finish(stop_reason="completed"))

    assert message.reply_calls == [
        "The agent finished without a visible reply. Open Bot Status for details or try again."
    ]


def test_finish_uses_cancelled_fallback_when_turn_is_cancelled_without_text():
    from talk2agent.bots.telegram_stream import TelegramTurnStream

    message = FakeMessage()
    stream = TelegramTurnStream(message=message)

    run(stream.finish(stop_reason="cancelled"))

    assert message.reply_calls == ["Turn cancelled. Send a new request when ready."]


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
