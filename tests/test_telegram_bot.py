import asyncio
from types import SimpleNamespace

from acp.helpers import update_agent_message_text

from talk2agent.session_store import RetiredSessionStoreError


def run(coro):
    return asyncio.run(coro)


class FakePlaceholder:
    def __init__(self):
        self.edit_calls = []
        self.reply_calls = []

    async def edit_text(self, text):
        self.edit_calls.append(text)

    async def reply_text(self, text):
        self.reply_calls.append(text)
        return FakePlaceholder()


class FakeIncomingMessage:
    def __init__(self, text):
        self.text = text
        self.reply_calls = []
        self.placeholders = []

    async def reply_text(self, text):
        self.reply_calls.append(text)
        placeholder = FakePlaceholder()
        if text == "Thinking...":
            self.placeholders.append(placeholder)
        return placeholder


class FakeUpdate:
    def __init__(self, user_id, text):
        self.effective_user = SimpleNamespace(id=user_id)
        self.message = FakeIncomingMessage(text)


class FakeResponse:
    def __init__(self, stop_reason="completed"):
        self.stop_reason = stop_reason


class FakeSession:
    def __init__(self, session_id="session-123", stop_reason="completed", error=None):
        self.session_id = session_id
        self.stop_reason = stop_reason
        self.error = error
        self.prompts = []
        self.close_calls = 0
        self.closed = False

    async def run_turn(self, prompt_text, stream):
        self.prompts.append(prompt_text)
        await stream.on_update(update_agent_message_text("hello "))
        await stream.on_update(update_agent_message_text("world"))
        if self.error is not None:
            raise self.error
        return FakeResponse(stop_reason=self.stop_reason)

    async def close(self):
        self.close_calls += 1
        self.closed = True


class FakeSessionStore:
    def __init__(
        self,
        session,
        *,
        peek_session=...,
        close_idle_error=None,
        get_or_create_error=None,
        reset_error=None,
        peek_error=None,
        retired_once_on_peek=False,
        retired_once_on_get=False,
        retired_once_on_reset=False,
    ):
        self.session = session
        self.peek_session = session if peek_session is ... else peek_session
        self.close_idle_error = close_idle_error
        self.get_or_create_error = get_or_create_error
        self.reset_error = reset_error
        self.peek_error = peek_error
        self.retired_once_on_peek = retired_once_on_peek
        self.retired_once_on_get = retired_once_on_get
        self.retired_once_on_reset = retired_once_on_reset
        self.close_idle_calls = []
        self.peek_calls = []
        self.get_or_create_calls = []
        self.reset_calls = []
        self.invalidate_calls = []
        self.close_all_calls = 0

    async def close_idle_sessions(self, now):
        self.close_idle_calls.append(now)
        if self.close_idle_error is not None:
            raise self.close_idle_error

    async def peek(self, user_id):
        self.peek_calls.append(user_id)
        if self.retired_once_on_peek:
            self.retired_once_on_peek = False
            raise RetiredSessionStoreError("session store retired")
        if self.peek_error is not None:
            raise self.peek_error
        return self.peek_session

    async def get_or_create(self, user_id):
        self.get_or_create_calls.append(user_id)
        if self.retired_once_on_get:
            self.retired_once_on_get = False
            raise RetiredSessionStoreError("session store retired")
        if self.get_or_create_error is not None:
            raise self.get_or_create_error
        return self.session

    async def reset(self, user_id):
        self.reset_calls.append(user_id)
        if self.retired_once_on_reset:
            self.retired_once_on_reset = False
            raise RetiredSessionStoreError("session store retired")
        if self.reset_error is not None:
            raise self.reset_error
        return self.session

    async def invalidate(self, user_id, session):
        self.invalidate_calls.append((user_id, session))
        if self.session is session:
            self.session = None
        await session.close()

    async def close_all(self):
        self.close_all_calls += 1


def make_context(*args):
    return SimpleNamespace(args=list(args))


def make_services(
    session=None,
    *,
    allowed_user_ids=None,
    provider="claude",
    retried_provider=None,
    admin_user_id=123,
    switch_error=None,
    session_store=None,
    peek_session=...,
    close_idle_error=None,
    get_or_create_error=None,
    reset_error=None,
    peek_error=None,
    retired_once_on_peek=False,
    retired_once_on_get=False,
    retired_once_on_reset=False,
):
    if session is None:
        session = FakeSession()
    if allowed_user_ids is None:
        allowed_user_ids = {123}
    if session_store is None:
        session_store = FakeSessionStore(
            session,
            peek_session=peek_session,
            close_idle_error=close_idle_error,
            get_or_create_error=get_or_create_error,
            reset_error=reset_error,
            peek_error=peek_error,
        )

    stale_store = None
    if retired_once_on_peek or retired_once_on_get or retired_once_on_reset:
        stale_store = FakeSessionStore(
            session,
            peek_session=peek_session,
            retired_once_on_peek=retired_once_on_peek,
            retired_once_on_get=retired_once_on_get,
            retired_once_on_reset=retired_once_on_reset,
        )

    snapshots = []
    if stale_store is not None:
        snapshots.append(SimpleNamespace(provider=provider, session_store=stale_store))
        snapshots.append(
            SimpleNamespace(provider=retried_provider or provider, session_store=session_store)
        )
    else:
        snapshots.append(SimpleNamespace(provider=provider, session_store=session_store))

    config = SimpleNamespace(runtime=SimpleNamespace(stream_edit_interval_ms=0))
    services = SimpleNamespace(
        config=config,
        allowed_user_ids=set(allowed_user_ids),
        admin_user_id=admin_user_id,
        session_store=session_store,
        stale_store=stale_store,
        final_session=session,
        snapshot_calls=0,
        switch_provider_calls=[],
    )

    async def snapshot_runtime_state():
        services.snapshot_calls += 1
        index = min(services.snapshot_calls - 1, len(snapshots) - 1)
        return snapshots[index]

    async def switch_provider(value):
        services.switch_provider_calls.append(value)
        if switch_error is not None:
            raise switch_error
        return value

    services.snapshot_runtime_state = snapshot_runtime_state
    services.switch_provider = switch_provider
    return services


def test_handle_text_rejects_unauthorized_user():
    from talk2agent.bots.telegram_bot import handle_text

    update = FakeUpdate(user_id=999, text="hi")
    services = make_services(allowed_user_ids={123})

    run(handle_text(update, None, services))

    assert update.message.reply_calls == ["Unauthorized user."]
    assert services.session_store.close_idle_calls == []
    assert services.session_store.get_or_create_calls == []


def test_handle_new_resets_session_and_reports_pending_id():
    from talk2agent.bots.telegram_bot import handle_new

    session = FakeSession(session_id=None)
    update = FakeUpdate(user_id=123, text="/new")
    services = make_services(session=session)

    run(handle_new(update, None, services))

    assert services.session_store.reset_calls == [123]
    assert update.message.reply_calls == ["Started new session: pending"]


def test_handle_new_replies_failure_when_reset_raises():
    from talk2agent.bots.telegram_bot import handle_new

    update = FakeUpdate(user_id=123, text="/new")
    services = make_services(reset_error=RuntimeError("reset failed"))

    run(handle_new(update, None, services))

    assert services.session_store.reset_calls == [123]
    assert update.message.reply_calls == ["Request failed."]


def test_handle_new_retries_once_when_snapshot_hits_retired_store():
    from talk2agent.bots.telegram_bot import handle_new

    update = FakeUpdate(user_id=123, text="/new")
    services = make_services(retired_once_on_reset=True)

    run(handle_new(update, None, services))

    assert services.snapshot_calls == 2
    assert services.session_store.reset_calls == [123]
    assert update.message.reply_calls == ["Started new session: session-123"]


def test_handle_text_runs_turn_and_streams_placeholder_updates():
    from talk2agent.bots.telegram_bot import handle_text

    session = FakeSession(session_id="session-abc", stop_reason="end_turn")
    update = FakeUpdate(user_id=123, text="hello")
    services = make_services(session=session)

    run(handle_text(update, None, services))

    assert services.session_store.close_idle_calls
    assert services.session_store.get_or_create_calls == [123]
    assert session.prompts == ["hello"]
    assert update.message.reply_calls == ["Thinking..."]
    assert update.message.placeholders[0].edit_calls == ["hello ", "hello world"]


def test_handle_text_direct_replies_failure_when_idle_pruning_raises():
    from talk2agent.bots.telegram_bot import handle_text

    update = FakeUpdate(user_id=123, text="hello")
    services = make_services(close_idle_error=RuntimeError("idle failed"))

    run(handle_text(update, None, services))

    assert update.message.reply_calls == ["Request failed."]
    assert update.message.placeholders == []
    assert services.session_store.get_or_create_calls == []


def test_handle_text_direct_replies_failure_when_session_acquisition_raises():
    from talk2agent.bots.telegram_bot import handle_text

    update = FakeUpdate(user_id=123, text="hello")
    services = make_services(get_or_create_error=RuntimeError("create failed"))

    run(handle_text(update, None, services))

    assert update.message.reply_calls == ["Request failed."]
    assert update.message.placeholders == []
    assert services.session_store.get_or_create_calls == [123]


def test_handle_text_invalidates_failed_session_and_edits_placeholder():
    from talk2agent.bots.telegram_bot import handle_text

    session = FakeSession(error=RuntimeError("boom"))
    update = FakeUpdate(user_id=123, text="hello")
    services = make_services(session=session)

    run(handle_text(update, None, services))

    assert session.prompts == ["hello"]
    assert session.close_calls == 1
    assert services.session_store.session is None
    assert services.session_store.invalidate_calls == [(123, session)]
    assert update.message.placeholders[0].edit_calls[-1] == "Request failed."


def test_handle_text_retries_once_when_snapshot_hits_retired_store():
    from talk2agent.bots.telegram_bot import handle_text

    update = FakeUpdate(user_id=123, text="hello")
    services = make_services(provider="gemini", retired_once_on_get=True)

    run(handle_text(update, None, services))

    assert services.snapshot_calls == 2
    assert services.final_session.prompts == ["hello"]


def test_handle_status_reports_provider_and_none_without_creating_session():
    from talk2agent.bots.telegram_bot import handle_status

    update = FakeUpdate(user_id=123, text="/status")
    services = make_services(provider="gemini", peek_session=None)

    run(handle_status(update, None, services))

    assert services.session_store.peek_calls == [123]
    assert services.session_store.get_or_create_calls == []
    assert update.message.reply_calls == ["provider=gemini session_id=none"]


def test_handle_status_retries_once_and_uses_provider_from_fresh_snapshot():
    from talk2agent.bots.telegram_bot import handle_status

    update = FakeUpdate(user_id=123, text="/status")
    services = make_services(
        provider="codex",
        retried_provider="gemini",
        peek_session=None,
        retired_once_on_peek=True,
    )

    run(handle_status(update, None, services))

    assert services.snapshot_calls == 2
    assert update.message.reply_calls == ["provider=gemini session_id=none"]


def test_handle_status_replies_failure_when_session_lookup_raises():
    from talk2agent.bots.telegram_bot import handle_status

    update = FakeUpdate(user_id=123, text="/status")
    services = make_services(peek_error=RuntimeError("lookup failed"))

    run(handle_status(update, None, services))

    assert services.session_store.peek_calls == [123]
    assert update.message.reply_calls == ["Request failed."]


def test_provider_command_requires_admin():
    from talk2agent.bots.telegram_bot import handle_provider

    update = FakeUpdate(user_id=123, text="/provider codex")
    services = make_services(admin_user_id=999)

    run(handle_provider(update, make_context("codex"), services))

    assert update.message.reply_calls == ["Unauthorized user."]


def test_provider_command_switches_provider_for_admin():
    from talk2agent.bots.telegram_bot import handle_provider

    update = FakeUpdate(user_id=123, text="/provider codex")
    services = make_services(admin_user_id=123)

    run(handle_provider(update, make_context("codex"), services))

    assert services.switch_provider_calls == ["codex"]
    assert update.message.reply_calls == ["provider=codex"]


def test_provider_command_replies_with_usage_for_invalid_provider():
    from talk2agent.bots.telegram_bot import handle_provider

    update = FakeUpdate(user_id=123, text="/provider nope")
    services = make_services(admin_user_id=123, switch_error=ValueError("unsupported provider"))

    run(handle_provider(update, make_context("nope"), services))

    assert update.message.reply_calls == ["Usage: /provider <claude|codex|gemini>"]


def test_cli_start_loads_config_and_runs_app(monkeypatch):
    import talk2agent.cli as cli

    calls = []
    config = object()

    def fake_load_config(path):
        calls.append(("load", str(path)))
        return config

    def fake_run_app(value):
        calls.append(("run_app", value))
        return 0

    monkeypatch.setattr(cli, "load_config", fake_load_config)
    monkeypatch.setattr(cli, "run_app", fake_run_app)

    assert cli.main(["start", "--config", "bot.yaml"]) == 0
    assert calls == [("load", "bot.yaml"), ("run_app", config)]
