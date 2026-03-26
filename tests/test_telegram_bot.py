import asyncio
import base64
from dataclasses import replace
from types import SimpleNamespace

from acp.helpers import update_agent_message_text

from talk2agent.config import McpServerConfig, NameValueConfig, WorkspaceConfig
from talk2agent.session_history import SessionHistoryEntry
from talk2agent.session_store import RetiredSessionStoreError


def run(coro):
    return asyncio.run(coro)


class FakeBot:
    def __init__(self):
        self.set_my_commands_calls = []

    async def set_my_commands(self, commands, scope=None):
        self.set_my_commands_calls.append((commands, scope))
        return True


class FakeApplication:
    def __init__(self):
        self.bot = FakeBot()


class FakeAsyncApplication(FakeApplication):
    def __init__(self):
        super().__init__()
        self.tasks = []

    def create_task(self, coro):
        task = asyncio.create_task(coro)
        self.tasks.append(task)
        return task

    async def wait_for_tasks(self):
        if not self.tasks:
            return
        tasks = tuple(self.tasks)
        self.tasks.clear()
        await asyncio.gather(*tasks, return_exceptions=True)


class FakeIncomingMessage:
    def __init__(
        self,
        text=None,
        *,
        caption=None,
        photo=None,
        document=None,
        voice=None,
        audio=None,
        video=None,
        media_group_id=None,
    ):
        self.text = text
        self.caption = caption
        self.photo = [] if photo is None else list(photo)
        self.document = document
        self.voice = voice
        self.audio = audio
        self.video = video
        self.media_group_id = media_group_id
        self.reply_calls = []
        self.reply_markups = []
        self.draft_calls = []
        self.edit_calls = []

    async def reply_text(self, text, reply_markup=None):
        self.reply_calls.append(text)
        self.reply_markups.append(reply_markup)
        return FakeIncomingMessage(text)

    async def reply_text_draft(self, draft_id, text):
        self.draft_calls.append((draft_id, text))
        return True

    async def edit_text(self, text, reply_markup=None):
        self.edit_calls.append((text, reply_markup))


class FakeCallbackQuery:
    def __init__(self, user_id, data, message):
        self.from_user = SimpleNamespace(id=user_id)
        self.data = data
        self.message = message
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


class FakeUpdate:
    def __init__(self, user_id, text=None, *, message=None):
        self.effective_user = SimpleNamespace(id=user_id)
        self.message = FakeIncomingMessage(text) if message is None else message
        self.callback_query = None


class FakeCallbackUpdate:
    def __init__(self, user_id, data, message=None):
        self.effective_user = SimpleNamespace(id=user_id)
        self.message = None
        self.callback_query = FakeCallbackQuery(
            user_id,
            data,
            message or FakeIncomingMessage("callback"),
        )


class FakeResponse:
    def __init__(self, stop_reason="completed"):
        self.stop_reason = stop_reason


class FakeTelegramFile:
    def __init__(self, payload):
        self.payload = bytearray(payload)

    async def download_as_bytearray(self):
        return bytearray(self.payload)


class FakePhotoSize:
    def __init__(self, *, file_unique_id="photo-1", payload=b"photo-bytes", file_size=None):
        self.file_unique_id = file_unique_id
        self.payload = payload
        self.file_size = len(payload) if file_size is None else file_size

    async def get_file(self):
        return FakeTelegramFile(self.payload)


class FakeDocument:
    def __init__(
        self,
        *,
        file_name="notes.md",
        mime_type="text/markdown",
        file_unique_id="doc-1",
        file_id="doc-file-1",
        payload=b"# Notes\n",
        file_size=None,
    ):
        self.file_name = file_name
        self.mime_type = mime_type
        self.file_unique_id = file_unique_id
        self.file_id = file_id
        self.payload = payload
        self.file_size = len(payload) if file_size is None else file_size

    async def get_file(self):
        return FakeTelegramFile(self.payload)


class FakeVoice:
    def __init__(
        self,
        *,
        mime_type="audio/ogg",
        file_unique_id="voice-1",
        payload=b"voice-bytes",
        file_size=None,
    ):
        self.mime_type = mime_type
        self.file_unique_id = file_unique_id
        self.payload = payload
        self.file_size = len(payload) if file_size is None else file_size

    async def get_file(self):
        return FakeTelegramFile(self.payload)


class FakeAudio:
    def __init__(
        self,
        *,
        title="standup",
        file_name="standup.mp3",
        mime_type="audio/mpeg",
        file_unique_id="audio-1",
        payload=b"audio-bytes",
        file_size=None,
    ):
        self.title = title
        self.file_name = file_name
        self.mime_type = mime_type
        self.file_unique_id = file_unique_id
        self.payload = payload
        self.file_size = len(payload) if file_size is None else file_size

    async def get_file(self):
        return FakeTelegramFile(self.payload)


class FakeVideo:
    def __init__(
        self,
        *,
        file_name="clip.mp4",
        mime_type="video/mp4",
        file_unique_id="video-1",
        file_id="video-file-1",
        payload=b"video-bytes",
        file_size=None,
    ):
        self.file_name = file_name
        self.mime_type = mime_type
        self.file_unique_id = file_unique_id
        self.file_id = file_id
        self.payload = payload
        self.file_size = len(payload) if file_size is None else file_size

    async def get_file(self):
        return FakeTelegramFile(self.payload)


class FakeSelection:
    def __init__(self, kind, current_value, choices, config_id=None):
        self.kind = kind
        self.current_value = current_value
        self.choices = choices
        self.config_id = config_id


class FakeChoice:
    def __init__(self, value, label):
        self.value = value
        self.label = label
        self.description = None


class FakeCommand:
    def __init__(self, name, description, hint=None):
        self.name = name
        self.description = description
        self.hint = hint


class FakeSession:
    def __init__(
        self,
        session_id="session-123",
        stop_reason="completed",
        error=None,
        available_commands=None,
        *,
        raise_before_stream=False,
        session_title=None,
        session_updated_at=None,
        usage=None,
        plan_entries=None,
        recent_tool_activities=None,
        terminal_outputs=None,
    ):
        self.session_id = session_id
        self.stop_reason = stop_reason
        self.error = error
        self.raise_before_stream = raise_before_stream
        self.session_title = session_title
        self.session_updated_at = session_updated_at
        self.usage = usage
        self.plan_entries = tuple(() if plan_entries is None else plan_entries)
        self.recent_tool_activities = tuple(
            () if recent_tool_activities is None else recent_tool_activities
        )
        self.terminal_outputs = {} if terminal_outputs is None else dict(terminal_outputs)
        self.available_commands = tuple(() if available_commands is None else available_commands)
        self.capabilities = SimpleNamespace(
            supports_image_prompt=True,
            supports_audio_prompt=True,
            supports_embedded_context_prompt=True,
            can_fork=True,
            can_list=True,
            can_resume=True,
        )
        self.prompts = []
        self.prompt_items = []
        self.close_calls = 0
        self.closed = False
        self.ensure_started_calls = 0
        self.cancel_turn_calls = 0
        self.set_selection_calls = []
        self.selections = {
            "model": FakeSelection(
                "model",
                "gpt-5.4",
                (FakeChoice("gpt-5.4", "GPT-5.4"), FakeChoice("gpt-5.4-mini", "GPT-5.4 Mini")),
                config_id="model",
            ),
            "mode": FakeSelection(
                "mode",
                "xhigh",
                (FakeChoice("xhigh", "xhigh"), FakeChoice("low", "low")),
                config_id="mode",
            ),
        }

    async def ensure_started(self):
        self.ensure_started_calls += 1
        if self.error is not None and not self.prompts:
            raise self.error
        return None

    async def run_turn(self, prompt_text, stream):
        self.prompts.append(prompt_text)
        if self.raise_before_stream and self.error is not None:
            raise self.error
        await self._emit_stream(stream)
        return FakeResponse(stop_reason=self.stop_reason)

    async def run_prompt(self, prompt_items, stream):
        self.prompt_items.append(tuple(prompt_items))
        if self.raise_before_stream and self.error is not None:
            raise self.error
        await self._emit_stream(stream)
        return FakeResponse(stop_reason=self.stop_reason)

    async def _emit_stream(self, stream):
        await stream.on_update(update_agent_message_text("hello "))
        await stream.on_update(update_agent_message_text("world"))
        if self.error is not None:
            raise self.error

    async def close(self):
        self.close_calls += 1
        self.closed = True

    async def cancel_turn(self):
        self.cancel_turn_calls += 1
        return False

    async def read_terminal_output(self, terminal_id):
        return self.terminal_outputs.get(terminal_id)

    def get_selection(self, kind):
        return self.selections.get(kind)

    async def set_selection(self, kind, value):
        self.set_selection_calls.append((kind, value))
        selection = self.selections[kind]
        selection.current_value = value
        return selection


class BlockingCancelableSession(FakeSession):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._turn_started = asyncio.Event()
        self._turn_cancelled = asyncio.Event()

    async def wait_until_started(self):
        await self._turn_started.wait()

    async def run_turn(self, prompt_text, stream):
        self.prompts.append(prompt_text)
        self._turn_started.set()
        await self._turn_cancelled.wait()
        return FakeResponse(stop_reason="cancelled")

    async def cancel_turn(self):
        self.cancel_turn_calls += 1
        self._turn_cancelled.set()
        return True


class FakeSessionStore:
    def __init__(
        self,
        session,
        *,
        peek_session=...,
        history_entries=None,
        close_idle_error=None,
        get_or_create_error=None,
        reset_error=None,
        restart_error=None,
        fork_live_error=None,
        fork_history_error=None,
        fork_provider_error=None,
        peek_error=None,
        list_history_error=None,
        activate_history_error=None,
        rename_history_error=None,
        delete_history_result=True,
        delete_history_error=None,
        retired_once_on_peek=False,
        retired_once_on_get=False,
        retired_once_on_reset=False,
    ):
        self.session = session
        self.peek_session = session if peek_session is ... else peek_session
        self.history_entries = [] if history_entries is None else list(history_entries)
        self.close_idle_error = close_idle_error
        self.get_or_create_error = get_or_create_error
        self.reset_error = reset_error
        self.restart_error = restart_error
        self.fork_live_error = fork_live_error
        self.fork_history_error = fork_history_error
        self.fork_provider_error = fork_provider_error
        self.peek_error = peek_error
        self.list_history_error = list_history_error
        self.activate_history_error = activate_history_error
        self.rename_history_error = rename_history_error
        self.delete_history_result = delete_history_result
        self.delete_history_error = delete_history_error
        self.retired_once_on_peek = retired_once_on_peek
        self.retired_once_on_get = retired_once_on_get
        self.retired_once_on_reset = retired_once_on_reset
        self.close_idle_calls = []
        self.peek_calls = []
        self.get_or_create_calls = []
        self.reset_calls = []
        self.restart_calls = []
        self.fork_live_calls = []
        self.fork_history_calls = []
        self.fork_provider_calls = []
        self.record_session_usage_calls = []
        self.invalidate_calls = []
        self.activate_history_calls = []
        self.activate_provider_calls = []
        self.rename_history_calls = []
        self.delete_history_calls = []

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

    async def restart(self, user_id):
        self.restart_calls.append(user_id)
        if self.restart_error is not None:
            raise self.restart_error
        return self.session

    async def fork_live_session(self, user_id):
        self.fork_live_calls.append(user_id)
        if self.fork_live_error is not None:
            raise self.fork_live_error
        self.session.session_id = f"fork-{self.session.session_id}"
        return self.session

    async def fork_history_session(self, user_id, session_id):
        self.fork_history_calls.append((user_id, session_id))
        if self.fork_history_error is not None:
            raise self.fork_history_error
        forked_session_id = f"fork-{session_id}"
        self.session.session_id = forked_session_id
        title = None
        for entry in self.history_entries:
            if entry.session_id == session_id:
                title = entry.title
                break
        self.history_entries.insert(0, build_history_entry(forked_session_id, title or forked_session_id))
        return self.session

    async def fork_provider_session(self, user_id, session_id, *, title_hint=None):
        self.fork_provider_calls.append((user_id, session_id, title_hint))
        if self.fork_provider_error is not None:
            raise self.fork_provider_error
        self.session.session_id = f"fork-{session_id}"
        return self.session

    async def list_history(self, user_id):
        if self.list_history_error is not None:
            raise self.list_history_error
        return list(self.history_entries)

    async def activate_history_session(self, user_id, session_id):
        self.activate_history_calls.append((user_id, session_id))
        if self.activate_history_error is not None:
            raise self.activate_history_error
        self.session.session_id = session_id
        return self.session

    async def activate_provider_session(self, user_id, session_id, *, title_hint=None):
        self.activate_provider_calls.append((user_id, session_id, title_hint))
        if self.activate_history_error is not None:
            raise self.activate_history_error
        self.session.session_id = session_id
        return self.session

    async def rename_history(self, user_id, session_id, title):
        self.rename_history_calls.append((user_id, session_id, title))
        if self.rename_history_error is not None:
            raise self.rename_history_error
        for index, entry in enumerate(self.history_entries):
            if entry.session_id == session_id:
                renamed = replace(entry, title=title)
                self.history_entries[index] = renamed
                return renamed
        raise KeyError(session_id)

    async def delete_history(self, user_id, session_id):
        self.delete_history_calls.append((user_id, session_id))
        if self.delete_history_error is not None:
            raise self.delete_history_error
        self.history_entries = [
            entry for entry in self.history_entries if entry.session_id != session_id
        ]
        active_session = self.peek_session
        if active_session is not None and active_session.session_id == session_id:
            self.peek_session = None
            await active_session.close()
        return self.delete_history_result

    async def record_session_usage(self, user_id, session, *, title_hint=None):
        self.record_session_usage_calls.append((user_id, session.session_id, title_hint))

    async def invalidate(self, user_id, session):
        self.invalidate_calls.append((user_id, session))
        if self.peek_session is session:
            self.peek_session = None
        await session.close()


def make_context(*args, application=None):
    return SimpleNamespace(args=list(args), application=application)


def find_inline_button(markup, text):
    for row in markup.inline_keyboard:
        for button in row:
            if button.text == text:
                return button
    raise AssertionError(f"button not found: {text}")


def make_services(
    session=None,
    *,
    allowed_user_ids=None,
    provider="claude",
    workspace_id="default",
    workspace_path="F:/workspace",
    admin_user_id=123,
    switch_error=None,
    switch_workspace_error=None,
    session_store=None,
    peek_session=...,
    history_entries=None,
    close_idle_error=None,
    get_or_create_error=None,
    reset_error=None,
    restart_error=None,
    fork_live_error=None,
    fork_history_error=None,
    fork_provider_error=None,
    peek_error=None,
    list_history_error=None,
    activate_history_error=None,
    rename_history_error=None,
    delete_history_result=True,
    delete_history_error=None,
    provider_session_pages=None,
    list_provider_sessions_error=None,
    provider_capabilities=None,
    workspaces=None,
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
            history_entries=history_entries,
            close_idle_error=close_idle_error,
            get_or_create_error=get_or_create_error,
            reset_error=reset_error,
            restart_error=restart_error,
            fork_live_error=fork_live_error,
            fork_history_error=fork_history_error,
            fork_provider_error=fork_provider_error,
            peek_error=peek_error,
            list_history_error=list_history_error,
            activate_history_error=activate_history_error,
            rename_history_error=rename_history_error,
            delete_history_result=delete_history_result,
            delete_history_error=delete_history_error,
        )

    stale_store = None
    if retired_once_on_peek or retired_once_on_get or retired_once_on_reset:
        stale_store = FakeSessionStore(
            session,
            peek_session=peek_session,
            history_entries=history_entries,
            retired_once_on_peek=retired_once_on_peek,
            retired_once_on_get=retired_once_on_get,
            retired_once_on_reset=retired_once_on_reset,
        )

    snapshots = []
    if stale_store is not None:
        snapshots.append(
            SimpleNamespace(
                provider=provider,
                workspace_id=workspace_id,
                workspace_path=workspace_path,
                session_store=stale_store,
            )
        )
        snapshots.append(
            SimpleNamespace(
                provider=provider,
                workspace_id=workspace_id,
                workspace_path=workspace_path,
                session_store=session_store,
            )
        )
    else:
        snapshots.append(
            SimpleNamespace(
                provider=provider,
                workspace_id=workspace_id,
                workspace_path=workspace_path,
                session_store=session_store,
            )
        )

    if workspaces is None:
        workspaces = [
            WorkspaceConfig(id="default", label="Default Workspace", path=workspace_path),
            WorkspaceConfig(id="alt", label="Alt Workspace", path="F:/alt"),
        ]
    workspace_map = {workspace.id: workspace for workspace in workspaces}

    config = SimpleNamespace(
        runtime=SimpleNamespace(stream_edit_interval_ms=0),
        agent=SimpleNamespace(
            workspaces=list(workspaces),
            resolve_workspace=lambda workspace_id, _workspace_map=workspace_map: _workspace_map[workspace_id],
        ),
    )
    services = SimpleNamespace(
        config=config,
        allowed_user_ids=set(allowed_user_ids),
        admin_user_id=admin_user_id,
        final_session=session,
        snapshot_calls=0,
        switch_provider_calls=[],
        switch_workspace_calls=[],
        discover_agent_commands_calls=[],
        discover_provider_capabilities_calls=[],
        list_provider_sessions_calls=[],
        bind_telegram_command_menu_updater=lambda updater: asyncio.sleep(0),
        refresh_telegram_command_menu=lambda: asyncio.sleep(0),
    )

    async def snapshot_runtime_state():
        services.snapshot_calls += 1
        index = min(services.snapshot_calls - 1, len(snapshots) - 1)
        return snapshots[index]

    async def switch_provider(value):
        services.switch_provider_calls.append(value)
        if switch_error is not None:
            raise switch_error
        snapshots.append(
            SimpleNamespace(
                provider=value,
                workspace_id=snapshots[-1].workspace_id,
                workspace_path=snapshots[-1].workspace_path,
                session_store=session_store,
            )
        )
        return value

    async def switch_workspace(value):
        services.switch_workspace_calls.append(value)
        if switch_workspace_error is not None:
            raise switch_workspace_error
        workspace = config.agent.resolve_workspace(value)
        snapshots.append(
            SimpleNamespace(
                provider=snapshots[-1].provider,
                workspace_id=workspace.id,
                workspace_path=workspace.path,
                session_store=session_store,
            )
        )
        return value

    async def discover_agent_commands(timeout_seconds=2.0):
        services.discover_agent_commands_calls.append(timeout_seconds)
        return tuple(session.available_commands)

    async def list_provider_sessions(cursor=None):
        services.list_provider_sessions_calls.append(cursor)
        if list_provider_sessions_error is not None:
            raise list_provider_sessions_error
        pages = {} if provider_session_pages is None else dict(provider_session_pages)
        return pages.get(
            cursor,
            SimpleNamespace(entries=tuple(), next_cursor=None, supported=True),
        )

    async def discover_provider_capabilities(value, *, workspace_id=None):
        services.discover_provider_capabilities_calls.append((value, workspace_id))
        summaries = (
            {
                "claude": SimpleNamespace(
                    provider="claude",
                    available=True,
                    supports_image_prompt=True,
                    supports_audio_prompt=False,
                    supports_embedded_context_prompt=True,
                    can_fork_sessions=True,
                    can_list_sessions=True,
                    can_resume_sessions=True,
                    error=None,
                ),
                "codex": SimpleNamespace(
                    provider="codex",
                    available=True,
                    supports_image_prompt=True,
                    supports_audio_prompt=True,
                    supports_embedded_context_prompt=True,
                    can_fork_sessions=True,
                    can_list_sessions=True,
                    can_resume_sessions=True,
                    error=None,
                ),
                "gemini": SimpleNamespace(
                    provider="gemini",
                    available=True,
                    supports_image_prompt=True,
                    supports_audio_prompt=True,
                    supports_embedded_context_prompt=False,
                    can_fork_sessions=False,
                    can_list_sessions=False,
                    can_resume_sessions=False,
                    error=None,
                ),
            }
            if provider_capabilities is None
            else dict(provider_capabilities)
        )
        return summaries[value]

    services.snapshot_runtime_state = snapshot_runtime_state
    services.switch_provider = switch_provider
    services.switch_workspace = switch_workspace
    services.discover_agent_commands = discover_agent_commands
    services.discover_provider_capabilities = discover_provider_capabilities
    services.list_provider_sessions = list_provider_sessions
    return services, session_store


def build_history_entry(session_id, title):
    return SessionHistoryEntry(
        provider="codex",
        telegram_user_id=123,
        session_id=session_id,
        title=title,
        cwd="F:/workspace",
        created_at="2026-03-20T00:00:00+00:00",
        updated_at="2026-03-20T00:00:00+00:00",
    )


def build_provider_session(session_id, title, *, cwd_label=".", updated_at="2026-03-26T00:00:00+00:00"):
    return SimpleNamespace(
        session_id=session_id,
        title=title,
        cwd="F:/workspace" if cwd_label == "." else f"F:/workspace/{cwd_label}",
        cwd_label=cwd_label,
        updated_at=updated_at,
    )


def test_handle_text_rejects_unauthorized_user():
    from talk2agent.bots.telegram_bot import TelegramUiState, handle_text

    update = FakeUpdate(user_id=999, text="hi")
    services, store = make_services(allowed_user_ids={123})

    run(handle_text(update, None, services, TelegramUiState()))

    assert update.message.reply_calls == ["Unauthorized user."]
    assert store.close_idle_calls == []
    assert store.get_or_create_calls == []


def test_new_session_button_starts_session_and_reports_session_id():
    from talk2agent.bots.telegram_bot import BUTTON_NEW_SESSION, TelegramUiState, handle_text

    update = FakeUpdate(user_id=123, text=BUTTON_NEW_SESSION)
    services, store = make_services()

    run(handle_text(update, None, services, TelegramUiState()))

    assert store.reset_calls == [123]
    assert store.record_session_usage_calls == [(123, "session-123", None)]
    assert update.message.reply_calls == [
        "Started new session: session-123\n"
        "Old bot buttons and pending inputs tied to the previous session were cleared."
    ]


def test_new_session_clears_session_bound_interactions_preserves_bundle_chat_and_syncs_commands():
    from talk2agent.bots.telegram_bot import (
        BUTTON_NEW_SESSION,
        CALLBACK_PREFIX,
        TelegramUiState,
        _ContextBundleItem,
        handle_callback_query,
        handle_text,
    )

    ui_state = TelegramUiState()
    ui_state.set_pending_text_action(123, "workspace_search")
    ui_state.add_context_item(
        123,
        "claude",
        "default",
        _ContextBundleItem(kind="file", relative_path="notes.txt"),
    )
    assert ui_state.enable_context_bundle_chat(123, "claude", "default") is True
    stale_token = ui_state.create(123, "workspace_page", relative_path="", page=0)

    application = FakeApplication()
    session = FakeSession(available_commands=[FakeCommand("status", "Show status")])
    update = FakeUpdate(user_id=123, text=BUTTON_NEW_SESSION)
    services, store = make_services(session=session)

    run(handle_text(update, make_context(application=application), services, ui_state))

    assert store.reset_calls == [123]
    assert ui_state.get_pending_text_action(123) is None
    assert ui_state.context_bundle_chat_active(123, "claude", "default") is True
    assert [command.command for command in application.bot.set_my_commands_calls[0][0]] == ["status"]

    stale_update = FakeCallbackUpdate(123, f"{CALLBACK_PREFIX}{stale_token}", message=FakeIncomingMessage("stale"))
    run(handle_callback_query(stale_update, None, services, ui_state))
    assert stale_update.callback_query.answers == [("This button has expired.", True)]


def test_restart_agent_clears_session_bound_interactions_and_syncs_commands():
    from talk2agent.bots.telegram_bot import BUTTON_RESTART_AGENT, TelegramUiState, handle_text

    ui_state = TelegramUiState()
    ui_state.set_pending_text_action(123, "workspace_search")
    application = FakeApplication()
    session = FakeSession(available_commands=[FakeCommand("status", "Show status")])
    update = FakeUpdate(user_id=123, text=BUTTON_RESTART_AGENT)
    services, store = make_services(session=session)

    run(handle_text(update, make_context(application=application), services, ui_state))

    assert store.restart_calls == [123]
    assert store.record_session_usage_calls == [(123, "session-123", None)]
    assert ui_state.get_pending_text_action(123) is None
    assert update.message.reply_calls == [
        "Restarted agent: session-123\n"
        "Old bot buttons and pending inputs tied to the previous session were cleared."
    ]
    assert [command.command for command in application.bot.set_my_commands_calls[0][0]] == ["status"]


def test_handle_text_runs_turn_uses_draft_stream_and_records_history_usage():
    from talk2agent.bots.telegram_bot import TelegramUiState, handle_text

    session = FakeSession(session_id="session-abc", stop_reason="end_turn")
    update = FakeUpdate(user_id=123, text="hello")
    services, store = make_services(session=session)

    run(handle_text(update, make_context(application=FakeApplication()), services, TelegramUiState()))

    assert store.close_idle_calls
    assert store.get_or_create_calls == [123]
    assert session.prompts == ["hello"]
    assert [text for _, text in update.message.draft_calls] == ["Thinking...", "hello ", "hello world"]
    assert update.message.reply_calls == ["hello world"]
    assert store.record_session_usage_calls == [(123, "session-abc", "hello")]


def test_retry_last_turn_button_replays_previous_text_turn():
    from talk2agent.bots.telegram_bot import BUTTON_RETRY_LAST_TURN, TelegramUiState, handle_text

    ui_state = TelegramUiState()
    session = FakeSession(session_id="session-abc", stop_reason="end_turn")
    first_update = FakeUpdate(user_id=123, text="hello")
    retry_update = FakeUpdate(user_id=123, text=BUTTON_RETRY_LAST_TURN)
    services, store = make_services(session=session)

    run(handle_text(first_update, make_context(application=FakeApplication()), services, ui_state))
    run(handle_text(retry_update, make_context(application=FakeApplication()), services, ui_state))

    assert session.prompts == ["hello"]
    assert len(session.prompt_items) == 1
    assert session.prompt_items[0][0].text == "hello"
    assert retry_update.message.reply_calls == ["hello world"]
    assert store.record_session_usage_calls == [
        (123, "session-abc", "hello"),
        (123, "session-abc", "hello"),
    ]


def test_retry_last_turn_button_without_previous_turn_shows_notice():
    from talk2agent.bots.telegram_bot import BUTTON_RETRY_LAST_TURN, TelegramUiState, handle_text

    update = FakeUpdate(user_id=123, text=BUTTON_RETRY_LAST_TURN)
    services, _ = make_services()

    run(handle_text(update, None, services, TelegramUiState()))

    assert update.message.reply_calls == [
        "No previous turn is available to retry for the current provider and workspace."
    ]


def test_fork_last_turn_button_replays_previous_text_turn_in_new_session():
    from talk2agent.bots.telegram_bot import (
        BUTTON_FORK_LAST_TURN,
        CALLBACK_PREFIX,
        TelegramUiState,
        handle_callback_query,
        handle_text,
    )

    ui_state = TelegramUiState()
    session = FakeSession(session_id="session-abc", stop_reason="end_turn", available_commands=[FakeCommand("status", "Show status")])
    first_update = FakeUpdate(user_id=123, text="hello")
    fork_update = FakeUpdate(user_id=123, text=BUTTON_FORK_LAST_TURN)
    application = FakeApplication()
    services, store = make_services(session=session)

    run(handle_text(first_update, make_context(application=application), services, ui_state))

    ui_state.set_agent_command_aliases(123, {"old_status": "old_status"})
    stale_token = ui_state.create(123, "workspace_page", relative_path="", page=0)

    run(handle_text(fork_update, make_context(application=application), services, ui_state))

    assert store.reset_calls == [123]
    assert session.prompts == ["hello"]
    assert len(session.prompt_items) == 1
    assert session.prompt_items[0][0].text == "hello"
    assert fork_update.message.reply_calls == ["hello world"]
    assert ui_state.resolve_agent_command(123, "old_status") is None
    assert ui_state.resolve_agent_command(123, "status") == "status"
    assert ui_state.get(stale_token) is None
    assert [command.command for command in application.bot.set_my_commands_calls[-1][0]] == ["status"]

    stale_update = FakeCallbackUpdate(
        123,
        f"{CALLBACK_PREFIX}{stale_token}",
        message=FakeIncomingMessage("stale"),
    )
    run(handle_callback_query(stale_update, None, services, ui_state))
    assert stale_update.callback_query.answers == [("This button has expired.", True)]
    assert store.record_session_usage_calls == [
        (123, "session-abc", "hello"),
        (123, "session-abc", "hello"),
    ]


def test_fork_last_turn_button_without_previous_turn_shows_notice():
    from talk2agent.bots.telegram_bot import BUTTON_FORK_LAST_TURN, TelegramUiState, handle_text

    update = FakeUpdate(user_id=123, text=BUTTON_FORK_LAST_TURN)
    services, _ = make_services()

    run(handle_text(update, None, services, TelegramUiState()))

    assert update.message.reply_calls == [
        "No previous turn is available to fork for the current provider and workspace."
    ]


def test_retry_last_turn_survives_provider_switch_in_same_workspace():
    from talk2agent.bots.telegram_bot import BUTTON_RETRY_LAST_TURN, TelegramUiState, handle_text

    ui_state = TelegramUiState()
    session = FakeSession(session_id="session-abc", stop_reason="end_turn")
    first_update = FakeUpdate(user_id=123, text="hello")
    retry_update = FakeUpdate(user_id=123, text=BUTTON_RETRY_LAST_TURN)
    services, store = make_services(session=session, provider="codex")

    run(handle_text(first_update, make_context(application=FakeApplication()), services, ui_state))

    async def switched_snapshot_runtime_state():
        return SimpleNamespace(
            provider="gemini",
            workspace_id="default",
            workspace_path="F:/workspace",
            session_store=store,
        )

    services.snapshot_runtime_state = switched_snapshot_runtime_state

    run(handle_text(retry_update, make_context(application=FakeApplication()), services, ui_state))

    assert session.prompts == ["hello"]
    assert len(session.prompt_items) == 1
    assert session.prompt_items[0][0].text == "hello"
    assert retry_update.message.reply_calls == ["hello world"]
    assert store.record_session_usage_calls == [
        (123, "session-abc", "hello"),
        (123, "session-abc", "hello"),
    ]


def test_handle_agent_command_restores_alias_before_running_turn():
    from talk2agent.bots.telegram_bot import TelegramUiState, handle_agent_command

    ui_state = TelegramUiState()
    ui_state.set_agent_command_aliases(123, {"status": "model"})
    update = FakeUpdate(user_id=123, text="/status low")
    services, _ = make_services()

    run(handle_agent_command(update, None, services, ui_state))

    assert services.final_session.prompts == ["/model low"]


def test_handle_text_replies_with_workspace_changes_follow_up_when_git_status_updates(monkeypatch):
    from talk2agent.bots import telegram_bot
    from talk2agent.bots.telegram_bot import TelegramUiState, handle_text
    from talk2agent.workspace_git import WorkspaceGitStatus, WorkspaceGitStatusEntry

    statuses = [
        WorkspaceGitStatus(
            is_git_repo=True,
            branch_line="main",
            entries=(),
        ),
        WorkspaceGitStatus(
            is_git_repo=True,
            branch_line="main",
            entries=(WorkspaceGitStatusEntry(" M", "src/app.py", "src/app.py"),),
        ),
    ]

    def fake_read_workspace_git_status(_path):
        return statuses.pop(0)

    monkeypatch.setattr(telegram_bot, "read_workspace_git_status", fake_read_workspace_git_status)

    update = FakeUpdate(user_id=123, text="make a change")
    services, _ = make_services(session=FakeSession(session_id="session-abc"))

    run(handle_text(update, None, services, TelegramUiState()))

    assert update.message.reply_calls[0] == "hello world"
    assert update.message.reply_calls[1].startswith(
        "Workspace changes updated for Claude Code in Default Workspace\nBranch: main\nCurrent changes: 1"
    )
    follow_up_markup = update.message.reply_markups[1]
    assert find_inline_button(follow_up_markup, "Open Workspace Changes")
    assert find_inline_button(follow_up_markup, "Ask Agent With Current Changes")
    assert find_inline_button(follow_up_markup, "Ask With Last Request")
    assert find_inline_button(follow_up_markup, "Start Bundle Chat With Changes")
    assert find_inline_button(follow_up_markup, "Add All Changes to Context")
    assert find_inline_button(follow_up_markup, "Open Context Bundle")


def test_workspace_changes_follow_up_can_start_agent_turn(monkeypatch):
    from talk2agent.bots import telegram_bot
    from talk2agent.bots.telegram_bot import (
        TelegramUiState,
        _context_items_agent_prompt,
        _ContextBundleItem,
        handle_callback_query,
        handle_text,
    )
    from talk2agent.workspace_git import WorkspaceGitStatus, WorkspaceGitStatusEntry

    changed_status = WorkspaceGitStatus(
        is_git_repo=True,
        branch_line="main",
        entries=(
            WorkspaceGitStatusEntry(" M", "src/app.py", "src/app.py"),
            WorkspaceGitStatusEntry("??", "notes.txt", "notes.txt"),
        ),
    )
    statuses = [
        WorkspaceGitStatus(
            is_git_repo=True,
            branch_line="main",
            entries=(),
        ),
        changed_status,
        changed_status,
        changed_status,
        changed_status,
        changed_status,
    ]

    def fake_read_workspace_git_status(_path):
        return statuses.pop(0)

    monkeypatch.setattr(telegram_bot, "read_workspace_git_status", fake_read_workspace_git_status)

    ui_state = TelegramUiState()
    session = FakeSession(session_id="session-abc")
    services, store = make_services(session=session)
    update = FakeUpdate(user_id=123, text="make a change")

    run(handle_text(update, None, services, ui_state))

    follow_up_markup = update.message.reply_markups[1]
    ask_button = find_inline_button(follow_up_markup, "Ask Agent With Current Changes")
    ask_update = FakeCallbackUpdate(123, ask_button.callback_data, message=FakeIncomingMessage("follow-up"))
    run(handle_callback_query(ask_update, None, services, ui_state))

    assert ask_update.callback_query.message.edit_calls[-1][0].startswith(
        "Send your request about the current workspace changes as the next plain text message."
    )

    request_update = FakeUpdate(user_id=123, text="Review the new changes.")
    run(handle_text(request_update, None, services, ui_state))

    expected_prompt = _context_items_agent_prompt(
        (
            _ContextBundleItem(kind="change", relative_path="src/app.py", status_code=" M"),
            _ContextBundleItem(kind="change", relative_path="notes.txt", status_code="??"),
        ),
        "Review the new changes.",
        context_label="current workspace changes",
    )
    assert session.prompts == ["make a change", expected_prompt]
    assert request_update.message.reply_calls == ["hello world"]
    assert store.record_session_usage_calls == [
        (123, "session-abc", "make a change"),
        (123, "session-abc", expected_prompt),
    ]
    final_text, final_markup = ask_update.callback_query.message.edit_calls[-1]
    assert final_text.startswith(
        "Asked agent about current workspace changes.\n"
        "Workspace changes updated for Claude Code in Default Workspace"
    )
    assert find_inline_button(final_markup, "Ask Agent With Current Changes")


def test_workspace_changes_follow_up_cancel_restores_follow_up(monkeypatch):
    from talk2agent.bots import telegram_bot
    from talk2agent.bots.telegram_bot import TelegramUiState, handle_callback_query, handle_text
    from talk2agent.workspace_git import WorkspaceGitStatus, WorkspaceGitStatusEntry

    changed_status = WorkspaceGitStatus(
        is_git_repo=True,
        branch_line="main",
        entries=(WorkspaceGitStatusEntry(" M", "src/app.py", "src/app.py"),),
    )
    statuses = [
        WorkspaceGitStatus(
            is_git_repo=True,
            branch_line="main",
            entries=(),
        ),
        changed_status,
        changed_status,
        changed_status,
    ]

    monkeypatch.setattr(telegram_bot, "read_workspace_git_status", lambda _path: statuses.pop(0))

    ui_state = TelegramUiState()
    update = FakeUpdate(user_id=123, text="make a change")
    services, _ = make_services(session=FakeSession(session_id="session-abc"))

    run(handle_text(update, None, services, ui_state))

    follow_up_message = FakeIncomingMessage("follow-up")
    ask_button = find_inline_button(update.message.reply_markups[1], "Ask Agent With Current Changes")
    ask_update = FakeCallbackUpdate(123, ask_button.callback_data, message=follow_up_message)
    run(handle_callback_query(ask_update, None, services, ui_state))

    cancel_button = find_inline_button(follow_up_message.edit_calls[-1][1], "Cancel Ask")
    cancel_update = FakeCallbackUpdate(123, cancel_button.callback_data, message=follow_up_message)
    run(handle_callback_query(cancel_update, None, services, ui_state))

    restored_text, restored_markup = follow_up_message.edit_calls[-1]
    assert restored_text.startswith(
        "Workspace changes request cancelled.\n"
        "Workspace changes updated for Claude Code in Default Workspace"
    )
    assert find_inline_button(restored_markup, "Ask Agent With Current Changes")


def test_workspace_changes_follow_up_can_ask_with_last_request(monkeypatch):
    from talk2agent.bots import telegram_bot
    from talk2agent.bots.telegram_bot import (
        TelegramUiState,
        _ContextBundleItem,
        _context_items_agent_prompt,
        handle_callback_query,
        handle_text,
    )
    from talk2agent.workspace_git import WorkspaceGitStatus, WorkspaceGitStatusEntry

    changed_status = WorkspaceGitStatus(
        is_git_repo=True,
        branch_line="main",
        entries=(WorkspaceGitStatusEntry(" M", "src/app.py", "src/app.py"),),
    )
    statuses = [
        WorkspaceGitStatus(
            is_git_repo=True,
            branch_line="main",
            entries=(),
        ),
        changed_status,
        changed_status,
        changed_status,
        changed_status,
        changed_status,
    ]

    monkeypatch.setattr(telegram_bot, "read_workspace_git_status", lambda _path: statuses.pop(0))

    ui_state = TelegramUiState()
    session = FakeSession(session_id="session-abc")
    services, store = make_services(session=session)
    update = FakeUpdate(user_id=123, text="make a change")

    run(handle_text(update, None, services, ui_state))

    follow_up_message = FakeIncomingMessage("follow-up")
    ask_button = find_inline_button(update.message.reply_markups[1], "Ask With Last Request")
    ask_update = FakeCallbackUpdate(123, ask_button.callback_data, message=follow_up_message)
    run(handle_callback_query(ask_update, None, services, ui_state))

    expected_prompt = _context_items_agent_prompt(
        (_ContextBundleItem(kind="change", relative_path="src/app.py", status_code=" M"),),
        "make a change",
        context_label="current workspace changes",
    )
    assert session.prompts == ["make a change", expected_prompt]
    assert follow_up_message.reply_calls == ["hello world"]
    assert store.record_session_usage_calls == [
        (123, "session-abc", "make a change"),
        (123, "session-abc", expected_prompt),
    ]
    final_text, final_markup = follow_up_message.edit_calls[-1]
    assert final_text.startswith(
        "Asked agent with the last request about current workspace changes.\n"
        "Workspace changes updated for Claude Code in Default Workspace"
    )
    assert find_inline_button(final_markup, "Ask Agent With Current Changes")


def test_workspace_changes_follow_up_can_start_bundle_chat(monkeypatch):
    from talk2agent.bots import telegram_bot
    from talk2agent.bots.telegram_bot import (
        TelegramUiState,
        _context_bundle_agent_prompt,
        _ContextBundleItem,
        handle_callback_query,
        handle_text,
    )
    from talk2agent.workspace_git import WorkspaceGitStatus, WorkspaceGitStatusEntry

    statuses = [
        WorkspaceGitStatus(
            is_git_repo=True,
            branch_line="main",
            entries=(),
        ),
        WorkspaceGitStatus(
            is_git_repo=True,
            branch_line="main",
            entries=(
                WorkspaceGitStatusEntry(" M", "src/app.py", "src/app.py"),
                WorkspaceGitStatusEntry("??", "notes.txt", "notes.txt"),
            ),
        ),
        WorkspaceGitStatus(
            is_git_repo=True,
            branch_line="main",
            entries=(
                WorkspaceGitStatusEntry(" M", "src/app.py", "src/app.py"),
                WorkspaceGitStatusEntry("??", "notes.txt", "notes.txt"),
            ),
        ),
    ]

    def fake_read_workspace_git_status(_path):
        return statuses.pop(0)

    monkeypatch.setattr(telegram_bot, "read_workspace_git_status", fake_read_workspace_git_status)

    ui_state = TelegramUiState()
    session = FakeSession(session_id="session-abc")
    services, store = make_services(session=session)
    update = FakeUpdate(user_id=123, text="make a change")

    run(handle_text(update, None, services, ui_state))

    follow_up_markup = update.message.reply_markups[1]
    start_button = find_inline_button(follow_up_markup, "Start Bundle Chat With Changes")
    start_update = FakeCallbackUpdate(123, start_button.callback_data, message=FakeIncomingMessage("follow-up"))
    run(handle_callback_query(start_update, None, services, ui_state))

    enabled_text, _ = start_update.callback_query.message.edit_calls[-1]
    assert enabled_text.startswith(
        "Added 2 changes to context bundle. Bundle chat enabled.\n"
        "Context bundle for Claude Code in Default Workspace\nItems: 2\nBundle chat: on"
    )

    request_update = FakeUpdate(user_id=123, text="Keep iterating on these changes.")
    run(handle_text(request_update, None, services, ui_state))

    expected_prompt = _context_bundle_agent_prompt(
        (
            _ContextBundleItem(kind="change", relative_path="src/app.py", status_code=" M"),
            _ContextBundleItem(kind="change", relative_path="notes.txt", status_code="??"),
        ),
        "Keep iterating on these changes.",
    )
    assert session.prompts == ["make a change", expected_prompt]
    assert request_update.message.reply_calls == ["hello world"]
    assert store.record_session_usage_calls == [
        (123, "session-abc", "make a change"),
        (123, "session-abc", "Keep iterating on these changes."),
    ]


def test_workspace_changes_follow_up_open_workspace_changes_can_go_back(monkeypatch):
    from talk2agent.bots import telegram_bot
    from talk2agent.bots.telegram_bot import TelegramUiState, handle_callback_query, handle_text
    from talk2agent.workspace_git import WorkspaceGitStatus, WorkspaceGitStatusEntry

    changed_status = WorkspaceGitStatus(
        is_git_repo=True,
        branch_line="main",
        entries=(WorkspaceGitStatusEntry(" M", "src/app.py", "src/app.py"),),
    )
    statuses = [
        WorkspaceGitStatus(
            is_git_repo=True,
            branch_line="main",
            entries=(),
        ),
        changed_status,
        changed_status,
        changed_status,
    ]

    monkeypatch.setattr(telegram_bot, "read_workspace_git_status", lambda _path: statuses.pop(0))

    ui_state = TelegramUiState()
    update = FakeUpdate(user_id=123, text="make a change")
    services, _ = make_services(session=FakeSession(session_id="session-abc"))

    run(handle_text(update, None, services, ui_state))

    follow_up_message = FakeIncomingMessage("follow-up")
    open_button = find_inline_button(update.message.reply_markups[1], "Open Workspace Changes")
    open_update = FakeCallbackUpdate(123, open_button.callback_data, message=follow_up_message)
    run(handle_callback_query(open_update, None, services, ui_state))

    changes_text, changes_markup = follow_up_message.edit_calls[-1]
    assert changes_text.startswith(
        "Workspace changes for Claude Code in Default Workspace\nBranch: main"
    )
    back_button = find_inline_button(changes_markup, "Back to Change Update")

    back_update = FakeCallbackUpdate(123, back_button.callback_data, message=follow_up_message)
    run(handle_callback_query(back_update, None, services, ui_state))

    restored_text, restored_markup = follow_up_message.edit_calls[-1]
    assert restored_text.startswith(
        "Workspace changes updated for Claude Code in Default Workspace\nBranch: main\nCurrent changes: 1"
    )
    assert find_inline_button(restored_markup, "Open Workspace Changes")


def test_workspace_changes_follow_up_open_context_bundle_can_go_back(monkeypatch):
    from talk2agent.bots import telegram_bot
    from talk2agent.bots.telegram_bot import TelegramUiState, handle_callback_query, handle_text
    from talk2agent.workspace_git import WorkspaceGitStatus, WorkspaceGitStatusEntry

    changed_status = WorkspaceGitStatus(
        is_git_repo=True,
        branch_line="main",
        entries=(WorkspaceGitStatusEntry(" M", "src/app.py", "src/app.py"),),
    )
    statuses = [
        WorkspaceGitStatus(
            is_git_repo=True,
            branch_line="main",
            entries=(),
        ),
        changed_status,
        changed_status,
    ]

    monkeypatch.setattr(telegram_bot, "read_workspace_git_status", lambda _path: statuses.pop(0))

    ui_state = TelegramUiState()
    update = FakeUpdate(user_id=123, text="make a change")
    services, _ = make_services(session=FakeSession(session_id="session-abc"))

    run(handle_text(update, None, services, ui_state))

    follow_up_message = FakeIncomingMessage("follow-up")
    bundle_button = find_inline_button(update.message.reply_markups[1], "Open Context Bundle")
    bundle_update = FakeCallbackUpdate(123, bundle_button.callback_data, message=follow_up_message)
    run(handle_callback_query(bundle_update, None, services, ui_state))

    bundle_text, bundle_markup = follow_up_message.edit_calls[-1]
    assert bundle_text == (
        "Context bundle for Claude Code in Default Workspace\n"
        "Context bundle is empty."
    )
    back_button = find_inline_button(bundle_markup, "Back to Change Update")

    back_update = FakeCallbackUpdate(123, back_button.callback_data, message=follow_up_message)
    run(handle_callback_query(back_update, None, services, ui_state))

    restored_text, restored_markup = follow_up_message.edit_calls[-1]
    assert restored_text.startswith(
        "Workspace changes updated for Claude Code in Default Workspace\nBranch: main\nCurrent changes: 1"
    )
    assert find_inline_button(restored_markup, "Open Context Bundle")


def test_workspace_changes_follow_up_start_bundle_chat_can_go_back(monkeypatch):
    from talk2agent.bots import telegram_bot
    from talk2agent.bots.telegram_bot import TelegramUiState, handle_callback_query, handle_text
    from talk2agent.workspace_git import WorkspaceGitStatus, WorkspaceGitStatusEntry

    changed_status = WorkspaceGitStatus(
        is_git_repo=True,
        branch_line="main",
        entries=(
            WorkspaceGitStatusEntry(" M", "src/app.py", "src/app.py"),
            WorkspaceGitStatusEntry("??", "notes.txt", "notes.txt"),
        ),
    )
    statuses = [
        WorkspaceGitStatus(
            is_git_repo=True,
            branch_line="main",
            entries=(),
        ),
        changed_status,
        changed_status,
        changed_status,
    ]

    monkeypatch.setattr(telegram_bot, "read_workspace_git_status", lambda _path: statuses.pop(0))

    ui_state = TelegramUiState()
    update = FakeUpdate(user_id=123, text="make a change")
    services, _ = make_services(session=FakeSession(session_id="session-abc"))

    run(handle_text(update, None, services, ui_state))

    follow_up_message = FakeIncomingMessage("follow-up")
    start_button = find_inline_button(update.message.reply_markups[1], "Start Bundle Chat With Changes")
    start_update = FakeCallbackUpdate(123, start_button.callback_data, message=follow_up_message)
    run(handle_callback_query(start_update, None, services, ui_state))

    bundle_text, bundle_markup = follow_up_message.edit_calls[-1]
    assert bundle_text.startswith(
        "Added 2 changes to context bundle. Bundle chat enabled.\n"
        "Context bundle for Claude Code in Default Workspace\nItems: 2\nBundle chat: on"
    )
    back_button = find_inline_button(bundle_markup, "Back to Change Update")

    back_update = FakeCallbackUpdate(123, back_button.callback_data, message=follow_up_message)
    run(handle_callback_query(back_update, None, services, ui_state))

    restored_text, restored_markup = follow_up_message.edit_calls[-1]
    assert restored_text.startswith(
        "Workspace changes updated for Claude Code in Default Workspace\nBranch: main\nCurrent changes: 2"
    )
    assert find_inline_button(restored_markup, "Start Bundle Chat With Changes")


def test_handle_text_skips_workspace_changes_follow_up_when_git_status_is_unchanged(monkeypatch):
    from talk2agent.bots import telegram_bot
    from talk2agent.bots.telegram_bot import TelegramUiState, handle_text
    from talk2agent.workspace_git import WorkspaceGitStatus, WorkspaceGitStatusEntry

    status = WorkspaceGitStatus(
        is_git_repo=True,
        branch_line="main",
        entries=(WorkspaceGitStatusEntry(" M", "src/app.py", "src/app.py"),),
    )
    monkeypatch.setattr(telegram_bot, "read_workspace_git_status", lambda _path: status)

    update = FakeUpdate(user_id=123, text="make a change")
    services, _ = make_services(session=FakeSession(session_id="session-abc"))

    run(handle_text(update, None, services, TelegramUiState()))

    assert update.message.reply_calls == ["hello world"]


def test_debug_status_reports_provider_workspace_and_session_id():
    from talk2agent.bots.telegram_bot import handle_debug_status

    update = FakeUpdate(user_id=123, text="/debug_status")
    services, store = make_services(provider="gemini", workspace_id="alt", workspace_path="F:/alt")

    run(handle_debug_status(update, None, services))

    assert store.peek_calls == [123]
    assert update.message.reply_calls == [
        "provider=gemini workspace_id=alt workspace=Alt Workspace cwd=F:/alt session_id=session-123 prompt_caps=img=yes,audio=yes,docs=yes session_caps=fork=yes,list=yes,resume=yes"
    ]


def test_bot_status_shows_runtime_summary_and_shortcuts():
    from talk2agent.acp.agent_session import PromptText
    from talk2agent.bots.telegram_bot import (
        BUTTON_BOT_STATUS,
        TelegramUiState,
        _ContextBundleItem,
        _ReplayTurn,
        handle_text,
    )

    ui_state = TelegramUiState()
    ui_state.set_pending_text_action(123, "workspace_search")
    ui_state.set_last_request_text(123, "default", "search for adapter")
    ui_state.set_last_turn(
        123,
        _ReplayTurn(
            provider="codex",
            workspace_id="default",
            prompt_items=(PromptText("hello"),),
            title_hint="hello",
        ),
    )
    ui_state.add_context_item(
        123,
        "codex",
        "default",
        _ContextBundleItem(kind="file", relative_path="notes.txt"),
    )
    ui_state.enable_context_bundle_chat(123, "codex", "default")

    update = FakeUpdate(user_id=123, text=BUTTON_BOT_STATUS)
    services, store = make_services(
        provider="codex",
        peek_session=None,
        history_entries=[
            build_history_entry("session-1", "First"),
            build_history_entry("session-2", "Second"),
        ],
    )

    run(handle_text(update, None, services, ui_state))

    text = update.message.reply_calls[0]
    assert text.startswith("Bot status for Codex in Default Workspace")
    assert "Workspace ID: default" in text
    assert "Path: F:/workspace" in text
    assert "Session: none (will start on first request)" in text
    assert "Pending input: Workspace search" in text
    assert "Local sessions: 2" in text
    assert "Last turn replay: available (hello)" in text
    assert "Last request text: search for adapter" in text
    assert "Context bundle: 1 item" in text
    assert "Bundle chat: on" in text
    assert "Bundle preview:" in text
    assert "1. [file] notes.txt" in text
    assert "Agent commands cached: unknown until a live session starts." in text
    assert services.discover_agent_commands_calls == []
    assert store.get_or_create_calls == []

    markup = update.message.reply_markups[0]
    assert find_inline_button(markup, "Refresh")
    assert find_inline_button(markup, "Session History")
    assert find_inline_button(markup, "Provider Sessions")
    assert find_inline_button(markup, "Cancel Pending Input")
    assert find_inline_button(markup, "Stop Bundle Chat")
    assert find_inline_button(markup, "Ask Agent With Context")
    assert find_inline_button(markup, "Bundle + Last Request")
    assert find_inline_button(markup, "Clear Bundle")
    assert find_inline_button(markup, "New Session")
    assert find_inline_button(markup, "Retry Last Turn")
    assert find_inline_button(markup, "Fork Last Turn")
    assert find_inline_button(markup, "Last Request")
    assert find_inline_button(markup, "Model / Mode")
    assert find_inline_button(markup, "Restart Agent")
    assert find_inline_button(markup, "Switch Agent")
    assert find_inline_button(markup, "Switch Workspace")
    assert find_inline_button(markup, "Agent Commands")
    assert find_inline_button(markup, "Workspace Search")
    assert find_inline_button(markup, "Workspace Runtime")


def test_bot_status_shows_workspace_git_preview_for_dirty_repo(monkeypatch):
    from talk2agent.bots import telegram_bot
    from talk2agent.bots.telegram_bot import BUTTON_BOT_STATUS, TelegramUiState, handle_text
    from talk2agent.workspace_git import WorkspaceGitStatus, WorkspaceGitStatusEntry

    monkeypatch.setattr(
        telegram_bot,
        "read_workspace_git_status",
        lambda _path: WorkspaceGitStatus(
            is_git_repo=True,
            branch_line="main",
            entries=(
                WorkspaceGitStatusEntry(" M", "src/app.py", "src/app.py"),
                WorkspaceGitStatusEntry("??", "notes.txt", "notes.txt"),
                WorkspaceGitStatusEntry("R ", "new.py", "old.py -> new.py"),
                WorkspaceGitStatusEntry("A ", "README.md", "README.md"),
            ),
        ),
    )

    update = FakeUpdate(user_id=123, text=BUTTON_BOT_STATUS)
    services, store = make_services(provider="codex", peek_session=None)

    run(handle_text(update, None, services, TelegramUiState()))

    text = update.message.reply_calls[0]
    assert "Workspace changes: 4 changes" in text
    assert "Workspace change preview:" in text
    assert "1. [M] src/app.py" in text
    assert "2. [??] notes.txt" in text
    assert "3. [R] old.py -> new.py" in text
    assert "... 1 more change" in text
    assert store.get_or_create_calls == []


def test_bot_status_shows_stop_turn_and_can_cancel_running_turn():
    from talk2agent.bots.telegram_bot import BUTTON_BOT_STATUS, TelegramUiState, handle_callback_query, handle_text

    async def scenario():
        session = BlockingCancelableSession(session_id="session-abc")
        ui_state = TelegramUiState()
        services, _ = make_services(provider="codex", session=session)
        application = FakeAsyncApplication()

        start_update = FakeUpdate(user_id=123, text="Long task")
        await handle_text(start_update, make_context(application=application), services, ui_state)
        await session.wait_until_started()

        status_update = FakeUpdate(user_id=123, text=BUTTON_BOT_STATUS)
        await handle_text(status_update, make_context(application=application), services, ui_state)

        status_text = status_update.message.reply_calls[0]
        assert "Turn: running" in status_text
        stop_button = find_inline_button(status_update.message.reply_markups[0], "Stop Turn")

        stop_update = FakeCallbackUpdate(
            123,
            stop_button.callback_data,
            message=status_update.message,
        )
        await handle_callback_query(
            stop_update,
            make_context(application=application),
            services,
            ui_state,
        )

        assert session.cancel_turn_calls == 1
        edited_text = status_update.message.edit_calls[-1][0]
        assert edited_text.startswith(
            "Stop requested for the current turn.\nBot status for Codex in Default Workspace"
        )
        assert "Turn: stop requested" in edited_text

        await application.wait_for_tasks()

        assert start_update.message.reply_calls == ["Turn cancelled."]
        assert ui_state.get_active_turn(123) is None

    run(scenario())


def test_handle_text_rejects_new_turn_while_background_turn_running():
    from talk2agent.bots.telegram_bot import TelegramUiState, handle_text

    async def scenario():
        session = BlockingCancelableSession(session_id="session-abc")
        ui_state = TelegramUiState()
        services, _ = make_services(provider="codex", session=session)
        application = FakeAsyncApplication()

        first_update = FakeUpdate(user_id=123, text="Long task")
        await handle_text(first_update, make_context(application=application), services, ui_state)
        await session.wait_until_started()

        second_update = FakeUpdate(user_id=123, text="Second task")
        await handle_text(second_update, make_context(application=application), services, ui_state)

        assert second_update.message.reply_calls == [
            (
                "Another request is already running (Long task). "
                "Open Bot Status to stop it or wait for it to finish."
            )
        ]

        session._turn_cancelled.set()
        await application.wait_for_tasks()

    run(scenario())


def test_bot_status_shows_workspace_change_quick_actions_when_available(monkeypatch):
    from talk2agent.bots import telegram_bot
    from talk2agent.bots.telegram_bot import BUTTON_BOT_STATUS, TelegramUiState, handle_text
    from talk2agent.workspace_git import WorkspaceGitStatus, WorkspaceGitStatusEntry

    monkeypatch.setattr(
        telegram_bot,
        "read_workspace_git_status",
        lambda _path: WorkspaceGitStatus(
            is_git_repo=True,
            branch_line="main",
            entries=(WorkspaceGitStatusEntry(" M", "src/app.py", "src/app.py"),),
        ),
    )

    ui_state = TelegramUiState()
    ui_state.set_last_request_text(123, "default", "Review the current change set.")
    update = FakeUpdate(user_id=123, text=BUTTON_BOT_STATUS)
    services, _ = make_services(provider="codex", peek_session=None)

    run(handle_text(update, None, services, ui_state))

    markup = update.message.reply_markups[0]
    assert find_inline_button(markup, "Ask Agent With Current Changes")
    assert find_inline_button(markup, "Ask With Last Request")
    assert find_inline_button(markup, "Start Bundle Chat With Changes")
    assert find_inline_button(markup, "Add All Changes to Context")


def test_bot_status_shows_workspace_git_clean_summary(monkeypatch):
    from talk2agent.bots import telegram_bot
    from talk2agent.bots.telegram_bot import BUTTON_BOT_STATUS, TelegramUiState, handle_text
    from talk2agent.workspace_git import WorkspaceGitStatus

    monkeypatch.setattr(
        telegram_bot,
        "read_workspace_git_status",
        lambda _path: WorkspaceGitStatus(
            is_git_repo=True,
            branch_line="main",
            entries=(),
        ),
    )

    update = FakeUpdate(user_id=123, text=BUTTON_BOT_STATUS)
    services, _ = make_services(provider="codex", peek_session=None)

    run(handle_text(update, None, services, TelegramUiState()))

    text = update.message.reply_calls[0]
    assert "Workspace changes: clean" in text
    assert "Workspace change preview:" not in text
    labels = [button.text for row in update.message.reply_markups[0].inline_keyboard for button in row]
    assert "Ask Agent With Current Changes" not in labels
    assert "Start Bundle Chat With Changes" not in labels


def test_bot_status_shows_workspace_git_not_repo_summary(monkeypatch):
    from talk2agent.bots import telegram_bot
    from talk2agent.bots.telegram_bot import BUTTON_BOT_STATUS, TelegramUiState, handle_text
    from talk2agent.workspace_git import WorkspaceGitStatus

    monkeypatch.setattr(
        telegram_bot,
        "read_workspace_git_status",
        lambda _path: WorkspaceGitStatus(
            is_git_repo=False,
            branch_line=None,
            entries=(),
        ),
    )

    update = FakeUpdate(user_id=123, text=BUTTON_BOT_STATUS)
    services, _ = make_services(provider="codex", peek_session=None)

    run(handle_text(update, None, services, TelegramUiState()))

    text = update.message.reply_calls[0]
    assert "Workspace changes: not a git repo" in text
    assert "Workspace change preview:" not in text


def test_bot_status_keeps_working_when_workspace_git_status_read_fails(monkeypatch):
    from talk2agent.bots import telegram_bot
    from talk2agent.bots.telegram_bot import BUTTON_BOT_STATUS, TelegramUiState, handle_text

    def _raise(_path):
        raise RuntimeError("git failed")

    monkeypatch.setattr(telegram_bot, "read_workspace_git_status", _raise)

    update = FakeUpdate(user_id=123, text=BUTTON_BOT_STATUS)
    services, _ = make_services(provider="codex", peek_session=None)

    run(handle_text(update, None, services, TelegramUiState()))

    text = update.message.reply_calls[0]
    assert text.startswith("Bot status for Codex in Default Workspace")
    assert "Workspace changes: unavailable" in text
    assert "Workspace change preview:" not in text


def test_bot_status_workspace_changes_direct_ask_cancel_returns_to_status(monkeypatch):
    from talk2agent.bots import telegram_bot
    from talk2agent.bots.telegram_bot import (
        BUTTON_BOT_STATUS,
        TelegramUiState,
        handle_callback_query,
        handle_text,
    )
    from talk2agent.workspace_git import WorkspaceGitStatus, WorkspaceGitStatusEntry

    monkeypatch.setattr(
        telegram_bot,
        "read_workspace_git_status",
        lambda _path: WorkspaceGitStatus(
            is_git_repo=True,
            branch_line="main",
            entries=(WorkspaceGitStatusEntry(" M", "src/app.py", "src/app.py"),),
        ),
    )

    ui_state = TelegramUiState()
    update = FakeUpdate(user_id=123, text=BUTTON_BOT_STATUS)
    services, _ = make_services(provider="codex")

    run(handle_text(update, None, services, ui_state))

    callback_message = FakeIncomingMessage("status")
    ask_button = find_inline_button(update.message.reply_markups[0], "Ask Agent With Current Changes")
    ask_update = FakeCallbackUpdate(123, ask_button.callback_data, message=callback_message)
    run(handle_callback_query(ask_update, None, services, ui_state))

    prompt_text, prompt_markup = callback_message.edit_calls[-1]
    assert prompt_text.startswith(
        "Send your request about the current workspace changes as the next plain text message."
    )

    cancel_button = find_inline_button(prompt_markup, "Cancel Ask")
    cancel_update = FakeCallbackUpdate(123, cancel_button.callback_data, message=callback_message)
    run(handle_callback_query(cancel_update, None, services, ui_state))

    restored_text, restored_markup = callback_message.edit_calls[-1]
    assert restored_text.startswith(
        "Workspace changes request cancelled.\nBot status for Codex in Default Workspace"
    )
    assert find_inline_button(restored_markup, "Ask Agent With Current Changes")


def test_bot_status_workspace_changes_direct_ask_with_last_request_returns_to_status(monkeypatch):
    from talk2agent.bots import telegram_bot
    from talk2agent.bots.telegram_bot import (
        BUTTON_BOT_STATUS,
        TelegramUiState,
        _ContextBundleItem,
        _context_items_agent_prompt,
        handle_callback_query,
        handle_text,
    )
    from talk2agent.workspace_git import WorkspaceGitStatus, WorkspaceGitStatusEntry

    monkeypatch.setattr(
        telegram_bot,
        "read_workspace_git_status",
        lambda _path: WorkspaceGitStatus(
            is_git_repo=True,
            branch_line="main",
            entries=(WorkspaceGitStatusEntry(" M", "src/app.py", "src/app.py"),),
        ),
    )

    ui_state = TelegramUiState()
    ui_state.set_last_request_text(123, "default", "Review the current change set.")
    session = FakeSession(session_id="session-abc")
    services, store = make_services(provider="codex", session=session)

    update = FakeUpdate(user_id=123, text=BUTTON_BOT_STATUS)
    run(handle_text(update, None, services, ui_state))

    callback_message = FakeIncomingMessage("status")
    ask_button = find_inline_button(update.message.reply_markups[0], "Ask With Last Request")
    ask_update = FakeCallbackUpdate(123, ask_button.callback_data, message=callback_message)
    run(handle_callback_query(ask_update, None, services, ui_state))

    expected_prompt = _context_items_agent_prompt(
        (_ContextBundleItem(kind="change", relative_path="src/app.py", status_code=" M"),),
        "Review the current change set.",
        context_label="current workspace changes",
    )
    assert session.prompts == [expected_prompt]
    assert callback_message.reply_calls == ["hello world"]
    assert store.record_session_usage_calls == [(123, "session-abc", expected_prompt)]
    final_text, final_markup = callback_message.edit_calls[-1]
    assert final_text.startswith(
        "Asked agent with the last request about current workspace changes.\n"
        "Bot status for Codex in Default Workspace"
    )
    assert find_inline_button(final_markup, "Ask Agent With Current Changes")


def test_bot_status_workspace_changes_direct_add_all_returns_to_status(monkeypatch):
    from talk2agent.bots import telegram_bot
    from talk2agent.bots.telegram_bot import (
        BUTTON_BOT_STATUS,
        TelegramUiState,
        handle_callback_query,
        handle_text,
    )
    from talk2agent.workspace_git import WorkspaceGitStatus, WorkspaceGitStatusEntry

    monkeypatch.setattr(
        telegram_bot,
        "read_workspace_git_status",
        lambda _path: WorkspaceGitStatus(
            is_git_repo=True,
            branch_line="main",
            entries=(
                WorkspaceGitStatusEntry(" M", "src/app.py", "src/app.py"),
                WorkspaceGitStatusEntry("??", "notes.txt", "notes.txt"),
            ),
        ),
    )

    ui_state = TelegramUiState()
    update = FakeUpdate(user_id=123, text=BUTTON_BOT_STATUS)
    services, _ = make_services(provider="codex")

    run(handle_text(update, None, services, ui_state))

    callback_message = FakeIncomingMessage("status")
    add_button = find_inline_button(update.message.reply_markups[0], "Add All Changes to Context")
    add_update = FakeCallbackUpdate(123, add_button.callback_data, message=callback_message)
    run(handle_callback_query(add_update, None, services, ui_state))

    bundle = ui_state.get_context_bundle(123, "codex", "default")
    assert bundle is not None
    assert len(bundle.items) == 2
    final_text, final_markup = callback_message.edit_calls[-1]
    assert final_text.startswith(
        "Added 2 changes to context bundle.\nBot status for Codex in Default Workspace"
    )
    assert "Context bundle: 2 items" in final_text
    assert "Bundle chat: off" in final_text
    assert "1. [change M] src/app.py" in final_text
    assert find_inline_button(final_markup, "Start Bundle Chat")


def test_bot_status_workspace_changes_direct_start_bundle_chat_returns_to_status(monkeypatch):
    from talk2agent.bots import telegram_bot
    from talk2agent.bots.telegram_bot import (
        BUTTON_BOT_STATUS,
        TelegramUiState,
        handle_callback_query,
        handle_text,
    )
    from talk2agent.workspace_git import WorkspaceGitStatus, WorkspaceGitStatusEntry

    monkeypatch.setattr(
        telegram_bot,
        "read_workspace_git_status",
        lambda _path: WorkspaceGitStatus(
            is_git_repo=True,
            branch_line="main",
            entries=(
                WorkspaceGitStatusEntry(" M", "src/app.py", "src/app.py"),
                WorkspaceGitStatusEntry("??", "notes.txt", "notes.txt"),
            ),
        ),
    )

    ui_state = TelegramUiState()
    update = FakeUpdate(user_id=123, text=BUTTON_BOT_STATUS)
    services, _ = make_services(provider="codex")

    run(handle_text(update, None, services, ui_state))

    callback_message = FakeIncomingMessage("status")
    start_button = find_inline_button(update.message.reply_markups[0], "Start Bundle Chat With Changes")
    start_update = FakeCallbackUpdate(123, start_button.callback_data, message=callback_message)
    run(handle_callback_query(start_update, None, services, ui_state))

    assert ui_state.context_bundle_chat_active(123, "codex", "default") is True
    final_text, final_markup = callback_message.edit_calls[-1]
    assert final_text.startswith(
        "Added 2 changes to context bundle. Bundle chat enabled.\n"
        "Bot status for Codex in Default Workspace"
    )
    assert "Context bundle: 2 items" in final_text
    assert "Bundle chat: on" in final_text
    assert "1. [change M] src/app.py" in final_text
    assert find_inline_button(final_markup, "Stop Bundle Chat")


def test_bot_status_truncates_and_normalizes_last_turn_and_request_summaries():
    from talk2agent.acp.agent_session import PromptText
    from talk2agent.bots.telegram_bot import BUTTON_BOT_STATUS, TelegramUiState, _ReplayTurn, handle_text

    ui_state = TelegramUiState()
    ui_state.set_last_request_text(
        123,
        "default",
        "Investigate   the\nworkspace search results and summarize the critical differences between files.",
    )
    ui_state.set_last_turn(
        123,
        _ReplayTurn(
            provider="codex",
            workspace_id="default",
            prompt_items=(PromptText("hello"),),
            title_hint="Review\nthis extremely long replay title that should be normalized and truncated for status display readability.",
        ),
    )

    update = FakeUpdate(user_id=123, text=BUTTON_BOT_STATUS)
    services, _ = make_services(provider="codex", peek_session=None)

    run(handle_text(update, None, services, ui_state))

    text = update.message.reply_calls[0]
    assert "Last turn replay: available (Review this extremely long replay title" in text
    assert "truncat...)" in text
    assert "Last request text: Investigate the workspace search results and summarize the critical" in text
    assert "differenc..." in text


def test_handle_text_records_last_request_source_for_bundle_chat():
    from talk2agent.bots.telegram_bot import (
        TelegramUiState,
        _ContextBundleItem,
        handle_text,
    )

    ui_state = TelegramUiState()
    ui_state.add_context_item(
        123,
        "codex",
        "default",
        _ContextBundleItem(kind="file", relative_path="notes.txt"),
    )
    ui_state.enable_context_bundle_chat(123, "codex", "default")
    update = FakeUpdate(user_id=123, text="Keep going with this bundle.")
    services, _ = make_services(provider="codex", session=FakeSession(session_id="session-abc"))

    run(handle_text(update, None, services, ui_state))

    last_request = ui_state.get_last_request(123, "default")
    assert last_request is not None
    assert last_request.provider == "codex"
    assert last_request.source_summary == "bundle chat (1 item)"
    assert last_request.text == "Keep going with this bundle."


def test_pending_text_action_label_includes_target_details():
    from talk2agent.bots.telegram_bot import (
        TelegramUiState,
        _ContextBundleItem,
        _pending_text_action_label,
    )

    ui_state = TelegramUiState()

    ui_state.set_pending_text_action(123, "workspace_file_agent_prompt", relative_path="src/app.py")
    assert _pending_text_action_label(ui_state.get_pending_text_action(123)) == (
        "Workspace file request (src/app.py)"
    )

    ui_state.set_pending_text_action(123, "workspace_change_agent_prompt", relative_path="src/app.py")
    assert _pending_text_action_label(ui_state.get_pending_text_action(123)) == (
        "Workspace change request (src/app.py)"
    )

    ui_state.set_pending_text_action(123, "rename_history", session_id="session-1")
    assert _pending_text_action_label(ui_state.get_pending_text_action(123)) == (
        "Rename session title (session-1)"
    )

    ui_state.set_pending_text_action(
        123,
        "context_bundle_agent_prompt",
        items=(_ContextBundleItem(kind="file", relative_path="notes.txt"),),
    )
    assert _pending_text_action_label(ui_state.get_pending_text_action(123)) == (
        "Context bundle request (1 item)"
    )

    ui_state.set_pending_text_action(
        123,
        "context_items_agent_prompt",
        prompt_label="matching workspace files",
        items=(
            _ContextBundleItem(kind="file", relative_path="README.md"),
            _ContextBundleItem(kind="file", relative_path="src/app.py"),
        ),
    )
    assert _pending_text_action_label(ui_state.get_pending_text_action(123)) == (
        "Selected context request (matching workspace files, 2 items)"
    )


def test_bot_status_shows_bundle_preview_with_remaining_count():
    from talk2agent.bots.telegram_bot import BUTTON_BOT_STATUS, TelegramUiState, _ContextBundleItem, handle_text

    ui_state = TelegramUiState()
    ui_state.add_context_item(
        123,
        "codex",
        "default",
        _ContextBundleItem(kind="file", relative_path="notes.txt"),
    )
    ui_state.add_context_item(
        123,
        "codex",
        "default",
        _ContextBundleItem(kind="change", relative_path="src/app.py", status_code=" M"),
    )
    ui_state.add_context_item(
        123,
        "codex",
        "default",
        _ContextBundleItem(kind="file", relative_path="README.md"),
    )
    ui_state.add_context_item(
        123,
        "codex",
        "default",
        _ContextBundleItem(kind="file", relative_path="docs/very-long-file-name-for-bundle-preview-status.txt"),
    )

    update = FakeUpdate(user_id=123, text=BUTTON_BOT_STATUS)
    services, _ = make_services(provider="codex", peek_session=None)

    run(handle_text(update, None, services, ui_state))

    text = update.message.reply_calls[0]
    assert "Context bundle: 4 items" in text
    assert "Bundle preview:" in text
    assert "1. [file] notes.txt" in text
    assert "2. [change M] src/app.py" in text
    assert "3. [file] README.md" in text
    assert "... 1 more item" in text


def test_bot_status_shows_current_session_title_from_local_history():
    from talk2agent.bots.telegram_bot import BUTTON_BOT_STATUS, TelegramUiState, handle_text

    session = FakeSession(session_id="session-2")
    update = FakeUpdate(user_id=123, text=BUTTON_BOT_STATUS)
    services, _ = make_services(
        provider="codex",
        session=session,
        history_entries=[
            build_history_entry("session-1", "Earlier Thread"),
            build_history_entry("session-2", "Active Workspace Refactor"),
        ],
    )

    run(handle_text(update, None, services, TelegramUiState()))

    text = update.message.reply_calls[0]
    assert "Session: session-2" in text
    assert "Session title: Active Workspace Refactor" in text


def test_bot_status_omits_session_title_when_live_session_is_not_in_local_history():
    from talk2agent.bots.telegram_bot import BUTTON_BOT_STATUS, TelegramUiState, handle_text

    session = FakeSession(session_id="session-live")
    update = FakeUpdate(user_id=123, text=BUTTON_BOT_STATUS)
    services, _ = make_services(
        provider="codex",
        session=session,
        history_entries=[build_history_entry("session-1", "Earlier Thread")],
    )

    run(handle_text(update, None, services, TelegramUiState()))

    text = update.message.reply_calls[0]
    assert "Session: session-live" in text
    assert "Session title:" not in text


def test_bot_status_falls_back_to_provider_session_title_when_history_has_no_match():
    from talk2agent.bots.telegram_bot import BUTTON_BOT_STATUS, TelegramUiState, handle_text

    session = FakeSession(session_id="session-live", session_title="Provider Native Thread")
    update = FakeUpdate(user_id=123, text=BUTTON_BOT_STATUS)
    services, _ = make_services(
        provider="codex",
        session=session,
        history_entries=[build_history_entry("session-1", "Earlier Thread")],
    )

    run(handle_text(update, None, services, TelegramUiState()))

    text = update.message.reply_calls[0]
    assert "Session: session-live" in text
    assert "Session title: Provider Native Thread" in text


def test_bot_status_shows_usage_and_plan_preview_when_live_session_has_cached_updates():
    from talk2agent.bots.telegram_bot import BUTTON_BOT_STATUS, TelegramUiState, handle_text

    session = FakeSession(
        session_id="session-live",
        usage=SimpleNamespace(
            used=512,
            size=4096,
            cost_amount=0.42,
            cost_currency="USD",
        ),
        plan_entries=(
            SimpleNamespace(content="Audit the runtime status view", status="in_progress"),
            SimpleNamespace(content="Update Telegram bot tests", status="pending"),
        ),
    )
    update = FakeUpdate(user_id=123, text=BUTTON_BOT_STATUS)
    services, _ = make_services(provider="codex", session=session)

    run(handle_text(update, None, services, TelegramUiState()))

    text = update.message.reply_calls[0]
    assert "Usage: used=512 size=4096 cost=0.42 USD" in text
    assert "Agent plan: 2 items" in text
    assert "Plan preview:" in text
    assert "1. [>] Audit the runtime status view" in text
    assert "2. [ ] Update Telegram bot tests" in text


def test_bot_status_can_open_session_info_and_back_to_status():
    from talk2agent.bots.telegram_bot import (
        BUTTON_BOT_STATUS,
        TelegramUiState,
        handle_callback_query,
        handle_text,
    )

    session = FakeSession(
        session_id="session-live",
        session_title="Provider Native Thread",
        session_updated_at="2026-03-26T09:30:00Z",
        usage=SimpleNamespace(
            used=512,
            size=4096,
            cost_amount=0.42,
            cost_currency="USD",
        ),
        plan_entries=(
            SimpleNamespace(content="Audit the runtime status view", status="in_progress", priority="high"),
        ),
        recent_tool_activities=(
            SimpleNamespace(
                tool_call_id="tool-1",
                title="Run tests",
                status="completed",
                kind="execute",
                details=("cmd: python -m pytest -q",),
            ),
        ),
        available_commands=(FakeCommand("plan", "Plan work"),),
    )
    ui_state = TelegramUiState()
    update = FakeUpdate(user_id=123, text=BUTTON_BOT_STATUS)
    services, _ = make_services(provider="codex", session=session)

    run(handle_text(update, None, services, ui_state))

    info_button = find_inline_button(update.message.reply_markups[0], "Session Info")
    callback_message = FakeIncomingMessage("status")
    info_update = FakeCallbackUpdate(123, info_button.callback_data, message=callback_message)
    run(handle_callback_query(info_update, None, services, ui_state))

    info_text, info_markup = callback_message.edit_calls[-1]
    assert info_text.startswith("Session info for Codex in Default Workspace")
    assert "Session: session-live" in info_text
    assert "Title: Provider Native Thread" in info_text
    assert "Updated: 2026-03-26T09:30:00Z" in info_text
    assert "Model: GPT-5.4 (2 choices)" in info_text
    assert "Mode: xhigh (2 choices)" in info_text
    assert "Prompt capabilities:" in info_text
    assert "Session capabilities:" in info_text
    assert "Usage: used=512 size=4096 cost=0.42 USD" in info_text
    assert "Cached commands: 1" in info_text
    assert "Cached plan items: 1" in info_text
    assert "Cached tool activities: 1" in info_text
    assert find_inline_button(info_markup, "Usage")
    assert find_inline_button(info_markup, "Workspace Runtime")
    assert find_inline_button(info_markup, "Agent Commands")
    assert find_inline_button(info_markup, "Agent Plan")
    assert find_inline_button(info_markup, "Tool Activity")

    back_button = find_inline_button(info_markup, "Back to Bot Status")
    back_update = FakeCallbackUpdate(123, back_button.callback_data, message=callback_message)
    run(handle_callback_query(back_update, None, services, ui_state))

    restored_text, restored_markup = callback_message.edit_calls[-1]
    assert restored_text.startswith("Bot status for Codex in Default Workspace")
    assert find_inline_button(restored_markup, "Session Info")


def test_bot_status_can_open_usage_and_back_to_status():
    from talk2agent.bots.telegram_bot import (
        BUTTON_BOT_STATUS,
        TelegramUiState,
        handle_callback_query,
        handle_text,
    )

    session = FakeSession(
        session_id="session-live",
        session_title="Provider Native Thread",
        session_updated_at="2026-03-26T09:30:00Z",
        usage=SimpleNamespace(
            used=512,
            size=4096,
            cost_amount=0.42,
            cost_currency="USD",
        ),
    )
    ui_state = TelegramUiState()
    update = FakeUpdate(user_id=123, text=BUTTON_BOT_STATUS)
    services, _ = make_services(provider="codex", session=session)

    run(handle_text(update, None, services, ui_state))

    usage_button = find_inline_button(update.message.reply_markups[0], "Usage")
    callback_message = FakeIncomingMessage("status")
    usage_update = FakeCallbackUpdate(123, usage_button.callback_data, message=callback_message)
    run(handle_callback_query(usage_update, None, services, ui_state))

    usage_text, usage_markup = callback_message.edit_calls[-1]
    assert usage_text.startswith("Usage for Codex in Default Workspace")
    assert "Session: session-live" in usage_text
    assert "Title: Provider Native Thread" in usage_text
    assert "Updated: 2026-03-26T09:30:00Z" in usage_text
    assert "Snapshot: cached ACP usage_update" in usage_text
    assert "Used: 512" in usage_text
    assert "Window size: 4096" in usage_text
    assert "Remaining: 3584" in usage_text
    assert "Utilization: 12.5%" in usage_text
    assert "Cost: 0.42 USD" in usage_text

    back_button = find_inline_button(usage_markup, "Back to Bot Status")
    back_update = FakeCallbackUpdate(123, back_button.callback_data, message=callback_message)
    run(handle_callback_query(back_update, None, services, ui_state))

    restored_text, restored_markup = callback_message.edit_calls[-1]
    assert restored_text.startswith("Bot status for Codex in Default Workspace")
    assert find_inline_button(restored_markup, "Usage")


def test_bot_status_can_open_workspace_runtime_and_back_to_status():
    from talk2agent.bots.telegram_bot import (
        BUTTON_BOT_STATUS,
        TelegramUiState,
        handle_callback_query,
        handle_text,
    )

    ui_state = TelegramUiState()
    update = FakeUpdate(user_id=123, text=BUTTON_BOT_STATUS)
    services, store = make_services(provider="codex", peek_session=None)

    run(handle_text(update, None, services, ui_state))

    runtime_button = find_inline_button(update.message.reply_markups[0], "Workspace Runtime")
    callback_message = FakeIncomingMessage("status")
    runtime_update = FakeCallbackUpdate(123, runtime_button.callback_data, message=callback_message)
    run(handle_callback_query(runtime_update, None, services, ui_state))

    runtime_text, runtime_markup = callback_message.edit_calls[-1]
    assert runtime_text.startswith("Workspace runtime for Codex in Default Workspace")
    assert "Workspace ID: default" in runtime_text
    assert "Path: F:/workspace" in runtime_text
    assert "ACP client tools:" in runtime_text
    assert "filesystem=yes (workspace-scoped text read/write)" in runtime_text
    assert "terminal=yes (workspace-scoped process bridge)" in runtime_text
    assert "Configured MCP servers: none" in runtime_text
    assert "Sessions in this runtime use only the bot client filesystem/terminal bridges." in runtime_text
    assert store.get_or_create_calls == []

    back_button = find_inline_button(runtime_markup, "Back to Bot Status")
    back_update = FakeCallbackUpdate(123, back_button.callback_data, message=callback_message)
    run(handle_callback_query(back_update, None, services, ui_state))

    restored_text, restored_markup = callback_message.edit_calls[-1]
    assert restored_text.startswith("Bot status for Codex in Default Workspace")
    assert find_inline_button(restored_markup, "Workspace Runtime")


def test_bot_status_workspace_runtime_can_open_mcp_server_detail():
    from talk2agent.bots.telegram_bot import (
        BUTTON_BOT_STATUS,
        TelegramUiState,
        handle_callback_query,
        handle_text,
    )

    workspaces = [
        WorkspaceConfig(
            id="default",
            label="Default Workspace",
            path="F:/workspace",
            mcp_servers=[
                McpServerConfig(
                    name="docs",
                    transport="stdio",
                    command="uvx",
                    args=["docs-mcp", "--workspace", "."],
                    env=[NameValueConfig(name="API_KEY", value="secret")],
                ),
                McpServerConfig(
                    name="search",
                    transport="http",
                    url="https://example.com/mcp",
                    headers=[NameValueConfig(name="Authorization", value="Bearer token")],
                ),
            ],
        ),
        WorkspaceConfig(id="alt", label="Alt Workspace", path="F:/alt"),
    ]
    ui_state = TelegramUiState()
    update = FakeUpdate(user_id=123, text=BUTTON_BOT_STATUS)
    services, _ = make_services(provider="codex", peek_session=None, workspaces=workspaces)

    run(handle_text(update, None, services, ui_state))

    runtime_button = find_inline_button(update.message.reply_markups[0], "Workspace Runtime")
    callback_message = FakeIncomingMessage("status")
    runtime_update = FakeCallbackUpdate(123, runtime_button.callback_data, message=callback_message)
    run(handle_callback_query(runtime_update, None, services, ui_state))

    runtime_text, runtime_markup = callback_message.edit_calls[-1]
    assert "Configured MCP servers: 2" in runtime_text
    open_button = find_inline_button(runtime_markup, "Open 1")

    open_update = FakeCallbackUpdate(123, open_button.callback_data, message=callback_message)
    run(handle_callback_query(open_update, None, services, ui_state))

    detail_text, detail_markup = callback_message.edit_calls[-1]
    assert detail_text.startswith("Workspace runtime for Codex in Default Workspace")
    assert "MCP server: 1/2" in detail_text
    assert "Name: docs" in detail_text
    assert "Transport: stdio" in detail_text
    assert "Command: uvx" in detail_text
    assert "Args: 3" in detail_text
    assert "1. docs-mcp" in detail_text
    assert "2. --workspace" in detail_text
    assert "3. ." in detail_text
    assert "Env vars: 1" in detail_text
    assert "Env keys:" in detail_text
    assert "API_KEY" in detail_text
    assert "Headers: 0" in detail_text
    assert "secret" not in detail_text
    assert find_inline_button(detail_markup, "Back to Workspace Runtime")

    back_button = find_inline_button(detail_markup, "Back to Workspace Runtime")
    back_update = FakeCallbackUpdate(123, back_button.callback_data, message=callback_message)
    run(handle_callback_query(back_update, None, services, ui_state))

    restored_text, restored_markup = callback_message.edit_calls[-1]
    assert restored_text.startswith("Workspace runtime for Codex in Default Workspace")
    assert find_inline_button(restored_markup, "Open 1")


def test_bot_status_can_open_last_request_and_back_to_status():
    from talk2agent.bots.telegram_bot import (
        BUTTON_BOT_STATUS,
        TelegramUiState,
        handle_callback_query,
        handle_text,
    )

    ui_state = TelegramUiState()
    ui_state.set_last_request_text(
        123,
        "default",
        "Review the workspace changes.\nFocus on failing tests first.",
        provider="claude",
        source_summary="selected context request (current workspace changes, 1 item)",
    )
    update = FakeUpdate(user_id=123, text=BUTTON_BOT_STATUS)
    services, _ = make_services(provider="codex", peek_session=None)

    run(handle_text(update, None, services, ui_state))

    request_button = find_inline_button(update.message.reply_markups[0], "Last Request")
    callback_message = FakeIncomingMessage("status")
    request_update = FakeCallbackUpdate(123, request_button.callback_data, message=callback_message)
    run(handle_callback_query(request_update, None, services, ui_state))

    request_text, request_markup = callback_message.edit_calls[-1]
    assert request_text.startswith("Last request for Codex in Default Workspace")
    assert "Recorded provider: Claude Code" in request_text
    assert "Recorded workspace: default" in request_text
    assert "Source: selected context request (current workspace changes, 1 item)" in request_text
    assert "Text length: 59 characters" in request_text
    assert "Request text:" in request_text
    assert "Review the workspace changes.\nFocus on failing tests first." in request_text

    back_button = find_inline_button(request_markup, "Back to Bot Status")
    back_update = FakeCallbackUpdate(123, back_button.callback_data, message=callback_message)
    run(handle_callback_query(back_update, None, services, ui_state))

    restored_text, restored_markup = callback_message.edit_calls[-1]
    assert restored_text.startswith("Bot status for Codex in Default Workspace")
    assert find_inline_button(restored_markup, "Last Request")


def test_bot_status_session_info_without_live_session_does_not_create_one():
    from talk2agent.bots.telegram_bot import (
        BUTTON_BOT_STATUS,
        TelegramUiState,
        handle_callback_query,
        handle_text,
    )

    ui_state = TelegramUiState()
    update = FakeUpdate(user_id=123, text=BUTTON_BOT_STATUS)
    services, store = make_services(provider="codex", peek_session=None)

    run(handle_text(update, None, services, ui_state))

    info_button = find_inline_button(update.message.reply_markups[0], "Session Info")
    callback_message = FakeIncomingMessage("status")
    info_update = FakeCallbackUpdate(123, info_button.callback_data, message=callback_message)
    run(handle_callback_query(info_update, None, services, ui_state))

    info_text, info_markup = callback_message.edit_calls[-1]
    assert info_text.startswith("Session info for Codex in Default Workspace")
    assert "No live session. A session will start on the first request." in info_text
    assert find_inline_button(info_markup, "Workspace Runtime")
    assert find_inline_button(info_markup, "Back to Bot Status")
    assert store.get_or_create_calls == []


def test_bot_status_can_open_last_turn_and_back_to_status():
    from talk2agent.acp.agent_session import PromptBlobResource, PromptText, PromptTextResource
    from talk2agent.bots.telegram_bot import (
        BUTTON_BOT_STATUS,
        TelegramUiState,
        _ContextBundleItem,
        _ReplayTurn,
        handle_callback_query,
        handle_text,
    )

    ui_state = TelegramUiState()
    ui_state.set_last_turn(
        123,
        _ReplayTurn(
            provider="claude",
            workspace_id="default",
            prompt_items=(
                PromptText("Review failing tests"),
                PromptTextResource(
                    uri="telegram://document/doc-1/notes.md",
                    text="# Notes\nhello",
                    mime_type="text/markdown",
                ),
                PromptBlobResource(
                    uri="telegram://video/video-1/clip.mp4",
                    blob=base64.b64encode(b"video-bytes").decode("ascii"),
                    mime_type="video/mp4",
                ),
            ),
            title_hint="Review failing tests with attachment",
            saved_context_items=(
                _ContextBundleItem(kind="file", relative_path=".talk2agent/telegram-inbox/notes.md"),
                _ContextBundleItem(kind="change", relative_path="src/app.py", status_code="M "),
            ),
        ),
    )
    update = FakeUpdate(user_id=123, text=BUTTON_BOT_STATUS)
    services, _ = make_services(provider="codex", peek_session=None)

    run(handle_text(update, None, services, ui_state))

    last_turn_button = find_inline_button(update.message.reply_markups[0], "Last Turn")
    callback_message = FakeIncomingMessage("status")
    last_turn_update = FakeCallbackUpdate(123, last_turn_button.callback_data, message=callback_message)
    run(handle_callback_query(last_turn_update, None, services, ui_state))

    last_turn_text, last_turn_markup = callback_message.edit_calls[-1]
    assert last_turn_text.startswith("Last turn for Codex in Default Workspace")
    assert "Recorded provider: Claude Code" in last_turn_text
    assert "Recorded workspace: default" in last_turn_text
    assert "Prompt items: 3" in last_turn_text
    assert "Saved context items: 2" in last_turn_text
    assert "Saved context preview:" in last_turn_text
    assert "1. [file] .talk2agent/telegram-inbox/notes.md" in last_turn_text
    assert "2. [change M ] src/app.py" in last_turn_text
    assert "1. [text] Review failing tests" in last_turn_text
    assert find_inline_button(last_turn_markup, "Retry Last Turn")
    assert find_inline_button(last_turn_markup, "Fork Last Turn")

    back_button = find_inline_button(last_turn_markup, "Back to Bot Status")
    back_update = FakeCallbackUpdate(123, back_button.callback_data, message=callback_message)
    run(handle_callback_query(back_update, None, services, ui_state))

    restored_text, restored_markup = callback_message.edit_calls[-1]
    assert restored_text.startswith("Bot status for Codex in Default Workspace")
    assert find_inline_button(restored_markup, "Last Turn")


def test_last_turn_item_detail_can_open_and_back():
    from talk2agent.acp.agent_session import PromptBlobResource, PromptText, PromptTextResource
    from talk2agent.bots.telegram_bot import (
        BUTTON_BOT_STATUS,
        TelegramUiState,
        _ReplayTurn,
        handle_callback_query,
        handle_text,
    )

    ui_state = TelegramUiState()
    ui_state.set_last_turn(
        123,
        _ReplayTurn(
            provider="codex",
            workspace_id="default",
            prompt_items=(
                PromptText("Review failing tests"),
                PromptTextResource(
                    uri="telegram://document/doc-1/notes.md",
                    text="# Notes\nhello",
                    mime_type="text/markdown",
                ),
                PromptBlobResource(
                    uri="telegram://video/video-1/clip.mp4",
                    blob=base64.b64encode(b"video-bytes").decode("ascii"),
                    mime_type="video/mp4",
                ),
            ),
            title_hint="Review failing tests with attachment",
        ),
    )
    update = FakeUpdate(user_id=123, text=BUTTON_BOT_STATUS)
    services, _ = make_services(provider="codex", peek_session=None)

    run(handle_text(update, None, services, ui_state))

    last_turn_button = find_inline_button(update.message.reply_markups[0], "Last Turn")
    callback_message = FakeIncomingMessage("status")
    last_turn_update = FakeCallbackUpdate(123, last_turn_button.callback_data, message=callback_message)
    run(handle_callback_query(last_turn_update, None, services, ui_state))

    open_button = find_inline_button(callback_message.edit_calls[-1][1], "Open 2")
    open_update = FakeCallbackUpdate(123, open_button.callback_data, message=callback_message)
    run(handle_callback_query(open_update, None, services, ui_state))

    detail_text, detail_markup = callback_message.edit_calls[-1]
    assert detail_text.startswith("Last turn for Codex in Default Workspace")
    assert "Item: 2/3" in detail_text
    assert "Kind: text resource" in detail_text
    assert "URI: telegram://document/doc-1/notes.md" in detail_text
    assert "MIME type: text/markdown" in detail_text
    assert "Payload size: 13 bytes" in detail_text
    assert "Resource content:" in detail_text
    assert "# Notes\nhello" in detail_text

    back_button = find_inline_button(detail_markup, "Back to Last Turn")
    back_update = FakeCallbackUpdate(123, back_button.callback_data, message=callback_message)
    run(handle_callback_query(back_update, None, services, ui_state))

    restored_text, restored_markup = callback_message.edit_calls[-1]
    assert restored_text.startswith("Last turn for Codex in Default Workspace")
    assert find_inline_button(restored_markup, "Open 2")


def test_session_info_can_open_agent_commands_and_back_to_session_info():
    from talk2agent.bots.telegram_bot import (
        BUTTON_BOT_STATUS,
        TelegramUiState,
        handle_callback_query,
        handle_text,
    )

    session = FakeSession(
        session_id="session-live",
        available_commands=(
            FakeCommand("plan", "Plan work"),
            FakeCommand("review", "Review changes", hint="path"),
        ),
    )
    ui_state = TelegramUiState()
    update = FakeUpdate(user_id=123, text=BUTTON_BOT_STATUS)
    services, _ = make_services(provider="codex", session=session)

    run(handle_text(update, None, services, ui_state))

    info_button = find_inline_button(update.message.reply_markups[0], "Session Info")
    callback_message = FakeIncomingMessage("status")
    info_update = FakeCallbackUpdate(123, info_button.callback_data, message=callback_message)
    run(handle_callback_query(info_update, None, services, ui_state))

    commands_button = find_inline_button(callback_message.edit_calls[-1][1], "Agent Commands")
    commands_update = FakeCallbackUpdate(123, commands_button.callback_data, message=callback_message)
    run(handle_callback_query(commands_update, None, services, ui_state))

    commands_text, commands_markup = callback_message.edit_calls[-1]
    assert commands_text.startswith("Agent commands for Codex in Default Workspace")
    back_button = find_inline_button(commands_markup, "Back to Session Info")

    back_update = FakeCallbackUpdate(123, back_button.callback_data, message=callback_message)
    run(handle_callback_query(back_update, None, services, ui_state))

    restored_text, restored_markup = callback_message.edit_calls[-1]
    assert restored_text.startswith("Session info for Codex in Default Workspace")
    assert find_inline_button(restored_markup, "Agent Commands")


def test_session_info_can_open_agent_command_detail_and_back_to_session_info():
    from talk2agent.bots.telegram_bot import (
        BUTTON_BOT_STATUS,
        TelegramUiState,
        handle_callback_query,
        handle_text,
    )

    session = FakeSession(
        session_id="session-live",
        available_commands=(
            FakeCommand("plan", "Plan work"),
            FakeCommand("review", "Review changes", hint="path"),
        ),
    )
    ui_state = TelegramUiState()
    update = FakeUpdate(user_id=123, text=BUTTON_BOT_STATUS)
    services, _ = make_services(provider="codex", session=session)

    run(handle_text(update, None, services, ui_state))

    info_button = find_inline_button(update.message.reply_markups[0], "Session Info")
    callback_message = FakeIncomingMessage("status")
    info_update = FakeCallbackUpdate(123, info_button.callback_data, message=callback_message)
    run(handle_callback_query(info_update, None, services, ui_state))

    commands_button = find_inline_button(callback_message.edit_calls[-1][1], "Agent Commands")
    commands_update = FakeCallbackUpdate(123, commands_button.callback_data, message=callback_message)
    run(handle_callback_query(commands_update, None, services, ui_state))

    open_button = find_inline_button(callback_message.edit_calls[-1][1], "Open 2")
    open_update = FakeCallbackUpdate(123, open_button.callback_data, message=callback_message)
    run(handle_callback_query(open_update, None, services, ui_state))

    detail_text, detail_markup = callback_message.edit_calls[-1]
    assert detail_text.startswith("Agent command for Codex in Default Workspace")
    assert "Command: 2/2" in detail_text
    assert "Session: session-live" in detail_text
    assert "Name: /review" in detail_text
    assert "Description:" in detail_text
    assert "Review changes" in detail_text
    assert "Args hint: path" in detail_text
    assert "Example: /review <args>" in detail_text
    assert find_inline_button(detail_markup, "Enter Args")

    back_button = find_inline_button(detail_markup, "Back to Agent Commands")
    back_update = FakeCallbackUpdate(123, back_button.callback_data, message=callback_message)
    run(handle_callback_query(back_update, None, services, ui_state))

    commands_text, commands_markup = callback_message.edit_calls[-1]
    assert commands_text.startswith("Agent commands for Codex in Default Workspace")
    back_to_info_button = find_inline_button(commands_markup, "Back to Session Info")

    back_to_info_update = FakeCallbackUpdate(
        123,
        back_to_info_button.callback_data,
        message=callback_message,
    )
    run(handle_callback_query(back_to_info_update, None, services, ui_state))

    restored_text, restored_markup = callback_message.edit_calls[-1]
    assert restored_text.startswith("Session info for Codex in Default Workspace")
    assert find_inline_button(restored_markup, "Agent Commands")


def test_session_info_can_open_last_turn_and_back_to_session_info():
    from talk2agent.acp.agent_session import PromptText
    from talk2agent.bots.telegram_bot import (
        BUTTON_BOT_STATUS,
        TelegramUiState,
        _ReplayTurn,
        handle_callback_query,
        handle_text,
    )

    session = FakeSession(session_id="session-live")
    ui_state = TelegramUiState()
    ui_state.set_last_turn(
        123,
        _ReplayTurn(
            provider="codex",
            workspace_id="default",
            prompt_items=(PromptText("hello"),),
            title_hint="hello",
        ),
    )
    update = FakeUpdate(user_id=123, text=BUTTON_BOT_STATUS)
    services, _ = make_services(provider="codex", session=session)

    run(handle_text(update, None, services, ui_state))

    info_button = find_inline_button(update.message.reply_markups[0], "Session Info")
    callback_message = FakeIncomingMessage("status")
    info_update = FakeCallbackUpdate(123, info_button.callback_data, message=callback_message)
    run(handle_callback_query(info_update, None, services, ui_state))

    last_turn_button = find_inline_button(callback_message.edit_calls[-1][1], "Last Turn")
    last_turn_update = FakeCallbackUpdate(123, last_turn_button.callback_data, message=callback_message)
    run(handle_callback_query(last_turn_update, None, services, ui_state))

    last_turn_text, last_turn_markup = callback_message.edit_calls[-1]
    assert last_turn_text.startswith("Last turn for Codex in Default Workspace")
    back_button = find_inline_button(last_turn_markup, "Back to Session Info")

    back_update = FakeCallbackUpdate(123, back_button.callback_data, message=callback_message)
    run(handle_callback_query(back_update, None, services, ui_state))

    restored_text, restored_markup = callback_message.edit_calls[-1]
    assert restored_text.startswith("Session info for Codex in Default Workspace")
    assert find_inline_button(restored_markup, "Last Turn")


def test_session_info_can_open_usage_and_back_to_session_info():
    from talk2agent.bots.telegram_bot import (
        BUTTON_BOT_STATUS,
        TelegramUiState,
        handle_callback_query,
        handle_text,
    )

    session = FakeSession(
        session_id="session-live",
        session_title="Provider Native Thread",
        usage=SimpleNamespace(
            used=512,
            size=4096,
            cost_amount=0.42,
            cost_currency="USD",
        ),
    )
    ui_state = TelegramUiState()
    update = FakeUpdate(user_id=123, text=BUTTON_BOT_STATUS)
    services, _ = make_services(provider="codex", session=session)

    run(handle_text(update, None, services, ui_state))

    info_button = find_inline_button(update.message.reply_markups[0], "Session Info")
    callback_message = FakeIncomingMessage("status")
    info_update = FakeCallbackUpdate(123, info_button.callback_data, message=callback_message)
    run(handle_callback_query(info_update, None, services, ui_state))

    usage_button = find_inline_button(callback_message.edit_calls[-1][1], "Usage")
    usage_update = FakeCallbackUpdate(123, usage_button.callback_data, message=callback_message)
    run(handle_callback_query(usage_update, None, services, ui_state))

    usage_text, usage_markup = callback_message.edit_calls[-1]
    assert usage_text.startswith("Usage for Codex in Default Workspace")
    back_button = find_inline_button(usage_markup, "Back to Session Info")

    back_update = FakeCallbackUpdate(123, back_button.callback_data, message=callback_message)
    run(handle_callback_query(back_update, None, services, ui_state))

    restored_text, restored_markup = callback_message.edit_calls[-1]
    assert restored_text.startswith("Session info for Codex in Default Workspace")
    assert find_inline_button(restored_markup, "Usage")


def test_session_info_can_open_workspace_runtime_and_back_to_session_info():
    from talk2agent.bots.telegram_bot import (
        BUTTON_BOT_STATUS,
        TelegramUiState,
        handle_callback_query,
        handle_text,
    )

    workspaces = [
        WorkspaceConfig(
            id="default",
            label="Default Workspace",
            path="F:/workspace",
            mcp_servers=[
                McpServerConfig(
                    name="docs",
                    transport="stdio",
                    command="uvx",
                    args=["docs-mcp"],
                    env=[NameValueConfig(name="API_KEY", value="secret")],
                ),
                McpServerConfig(
                    name="search",
                    transport="http",
                    url="https://example.com/mcp",
                    headers=[NameValueConfig(name="Authorization", value="Bearer token")],
                ),
            ],
        ),
        WorkspaceConfig(id="alt", label="Alt Workspace", path="F:/alt"),
    ]
    session = FakeSession(session_id="session-live")
    ui_state = TelegramUiState()
    update = FakeUpdate(user_id=123, text=BUTTON_BOT_STATUS)
    services, _ = make_services(provider="codex", session=session, workspaces=workspaces)

    run(handle_text(update, None, services, ui_state))

    info_button = find_inline_button(update.message.reply_markups[0], "Session Info")
    callback_message = FakeIncomingMessage("status")
    info_update = FakeCallbackUpdate(123, info_button.callback_data, message=callback_message)
    run(handle_callback_query(info_update, None, services, ui_state))

    runtime_button = find_inline_button(callback_message.edit_calls[-1][1], "Workspace Runtime")
    runtime_update = FakeCallbackUpdate(123, runtime_button.callback_data, message=callback_message)
    run(handle_callback_query(runtime_update, None, services, ui_state))

    runtime_text, runtime_markup = callback_message.edit_calls[-1]
    assert runtime_text.startswith("Workspace runtime for Codex in Default Workspace")
    assert "Configured MCP servers: 2" in runtime_text
    assert "1. [stdio] docs (uvx docs-mcp, env: 1)" in runtime_text
    assert "2. [http] search (https://example.com/mcp, headers: 1)" in runtime_text
    assert "New, loaded, resumed, and forked sessions inherit this MCP server set." in runtime_text

    back_button = find_inline_button(runtime_markup, "Back to Session Info")
    back_update = FakeCallbackUpdate(123, back_button.callback_data, message=callback_message)
    run(handle_callback_query(back_update, None, services, ui_state))

    restored_text, restored_markup = callback_message.edit_calls[-1]
    assert restored_text.startswith("Session info for Codex in Default Workspace")
    assert find_inline_button(restored_markup, "Workspace Runtime")


def test_session_info_workspace_runtime_can_open_http_mcp_server_detail():
    from talk2agent.bots.telegram_bot import (
        BUTTON_BOT_STATUS,
        TelegramUiState,
        handle_callback_query,
        handle_text,
    )

    workspaces = [
        WorkspaceConfig(
            id="default",
            label="Default Workspace",
            path="F:/workspace",
            mcp_servers=[
                McpServerConfig(
                    name="docs",
                    transport="stdio",
                    command="uvx",
                    args=["docs-mcp"],
                    env=[NameValueConfig(name="API_KEY", value="secret")],
                ),
                McpServerConfig(
                    name="search",
                    transport="http",
                    url="https://example.com/mcp",
                    headers=[
                        NameValueConfig(name="Authorization", value="Bearer token"),
                        NameValueConfig(name="X-Trace-Id", value="trace"),
                    ],
                ),
            ],
        ),
        WorkspaceConfig(id="alt", label="Alt Workspace", path="F:/alt"),
    ]
    session = FakeSession(session_id="session-live")
    ui_state = TelegramUiState()
    update = FakeUpdate(user_id=123, text=BUTTON_BOT_STATUS)
    services, _ = make_services(provider="codex", session=session, workspaces=workspaces)

    run(handle_text(update, None, services, ui_state))

    info_button = find_inline_button(update.message.reply_markups[0], "Session Info")
    callback_message = FakeIncomingMessage("status")
    info_update = FakeCallbackUpdate(123, info_button.callback_data, message=callback_message)
    run(handle_callback_query(info_update, None, services, ui_state))

    runtime_button = find_inline_button(callback_message.edit_calls[-1][1], "Workspace Runtime")
    runtime_update = FakeCallbackUpdate(123, runtime_button.callback_data, message=callback_message)
    run(handle_callback_query(runtime_update, None, services, ui_state))

    open_button = find_inline_button(callback_message.edit_calls[-1][1], "Open 2")
    open_update = FakeCallbackUpdate(123, open_button.callback_data, message=callback_message)
    run(handle_callback_query(open_update, None, services, ui_state))

    detail_text, detail_markup = callback_message.edit_calls[-1]
    assert detail_text.startswith("Workspace runtime for Codex in Default Workspace")
    assert "MCP server: 2/2" in detail_text
    assert "Name: search" in detail_text
    assert "Transport: http" in detail_text
    assert "URL: https://example.com/mcp" in detail_text
    assert "Env vars: 0" in detail_text
    assert "Headers: 2" in detail_text
    assert "Header keys:" in detail_text
    assert "Authorization" in detail_text
    assert "X-Trace-Id" in detail_text
    assert "Bearer token" not in detail_text
    assert "trace" not in detail_text

    back_button = find_inline_button(detail_markup, "Back to Workspace Runtime")
    back_update = FakeCallbackUpdate(123, back_button.callback_data, message=callback_message)
    run(handle_callback_query(back_update, None, services, ui_state))

    runtime_text, runtime_markup = callback_message.edit_calls[-1]
    assert runtime_text.startswith("Workspace runtime for Codex in Default Workspace")
    back_to_info_button = find_inline_button(runtime_markup, "Back to Session Info")

    back_to_info_update = FakeCallbackUpdate(
        123,
        back_to_info_button.callback_data,
        message=callback_message,
    )
    run(handle_callback_query(back_to_info_update, None, services, ui_state))

    restored_text, restored_markup = callback_message.edit_calls[-1]
    assert restored_text.startswith("Session info for Codex in Default Workspace")
    assert find_inline_button(restored_markup, "Workspace Runtime")


def test_session_info_can_open_last_request_and_back_to_session_info():
    from talk2agent.bots.telegram_bot import (
        BUTTON_BOT_STATUS,
        TelegramUiState,
        handle_callback_query,
        handle_text,
    )

    session = FakeSession(session_id="session-live")
    ui_state = TelegramUiState()
    ui_state.set_last_request_text(
        123,
        "default",
        "Keep going with the saved context.",
        provider="codex",
        source_summary="context bundle request (2 items)",
    )
    update = FakeUpdate(user_id=123, text=BUTTON_BOT_STATUS)
    services, _ = make_services(provider="codex", session=session)

    run(handle_text(update, None, services, ui_state))

    info_button = find_inline_button(update.message.reply_markups[0], "Session Info")
    callback_message = FakeIncomingMessage("status")
    info_update = FakeCallbackUpdate(123, info_button.callback_data, message=callback_message)
    run(handle_callback_query(info_update, None, services, ui_state))

    info_markup = callback_message.edit_calls[-1][1]
    request_button = find_inline_button(info_markup, "Last Request")
    request_update = FakeCallbackUpdate(123, request_button.callback_data, message=callback_message)
    run(handle_callback_query(request_update, None, services, ui_state))

    request_text, request_markup = callback_message.edit_calls[-1]
    assert request_text.startswith("Last request for Codex in Default Workspace")
    assert "Source: context bundle request (2 items)" in request_text
    back_button = find_inline_button(request_markup, "Back to Session Info")

    back_update = FakeCallbackUpdate(123, back_button.callback_data, message=callback_message)
    run(handle_callback_query(back_update, None, services, ui_state))

    restored_text, restored_markup = callback_message.edit_calls[-1]
    assert restored_text.startswith("Session info for Codex in Default Workspace")
    assert find_inline_button(restored_markup, "Last Request")


def test_session_info_can_open_agent_plan_detail_and_return_to_session_info():
    from talk2agent.bots.telegram_bot import (
        BUTTON_BOT_STATUS,
        TelegramUiState,
        handle_callback_query,
        handle_text,
    )

    session = FakeSession(
        session_id="session-live",
        plan_entries=(
            SimpleNamespace(
                content="Audit the runtime status view and confirm every callback path restores correctly.",
                status="in_progress",
                priority="high",
            ),
        ),
    )
    ui_state = TelegramUiState()
    update = FakeUpdate(user_id=123, text=BUTTON_BOT_STATUS)
    services, _ = make_services(provider="codex", session=session)

    run(handle_text(update, None, services, ui_state))

    info_button = find_inline_button(update.message.reply_markups[0], "Session Info")
    callback_message = FakeIncomingMessage("status")
    info_update = FakeCallbackUpdate(123, info_button.callback_data, message=callback_message)
    run(handle_callback_query(info_update, None, services, ui_state))

    plan_button = find_inline_button(callback_message.edit_calls[-1][1], "Agent Plan")
    plan_update = FakeCallbackUpdate(123, plan_button.callback_data, message=callback_message)
    run(handle_callback_query(plan_update, None, services, ui_state))

    open_button = find_inline_button(callback_message.edit_calls[-1][1], "Open 1")
    open_update = FakeCallbackUpdate(123, open_button.callback_data, message=callback_message)
    run(handle_callback_query(open_update, None, services, ui_state))

    detail_text, detail_markup = callback_message.edit_calls[-1]
    assert detail_text.startswith("Agent plan for Codex in Default Workspace")
    back_to_plan_button = find_inline_button(detail_markup, "Back to Agent Plan")

    back_to_plan_update = FakeCallbackUpdate(
        123,
        back_to_plan_button.callback_data,
        message=callback_message,
    )
    run(handle_callback_query(back_to_plan_update, None, services, ui_state))

    plan_text, plan_markup = callback_message.edit_calls[-1]
    assert plan_text.startswith("Agent plan for Codex in Default Workspace")
    back_to_info_button = find_inline_button(plan_markup, "Back to Session Info")

    back_to_info_update = FakeCallbackUpdate(
        123,
        back_to_info_button.callback_data,
        message=callback_message,
    )
    run(handle_callback_query(back_to_info_update, None, services, ui_state))

    restored_text, restored_markup = callback_message.edit_calls[-1]
    assert restored_text.startswith("Session info for Codex in Default Workspace")
    assert find_inline_button(restored_markup, "Agent Plan")


def test_bot_status_can_open_agent_plan_and_back_to_status():
    from talk2agent.bots.telegram_bot import (
        BUTTON_BOT_STATUS,
        TelegramUiState,
        handle_callback_query,
        handle_text,
    )

    session = FakeSession(
        session_id="session-live",
        plan_entries=(
            SimpleNamespace(
                content="Audit the runtime status view",
                status="in_progress",
                priority="high",
            ),
        ),
    )
    ui_state = TelegramUiState()
    update = FakeUpdate(user_id=123, text=BUTTON_BOT_STATUS)
    services, _ = make_services(provider="codex", session=session)

    run(handle_text(update, None, services, ui_state))

    plan_button = find_inline_button(update.message.reply_markups[0], "Agent Plan")
    callback_message = FakeIncomingMessage("status")
    plan_update = FakeCallbackUpdate(123, plan_button.callback_data, message=callback_message)
    run(handle_callback_query(plan_update, None, services, ui_state))

    plan_text, plan_markup = callback_message.edit_calls[-1]
    assert plan_text.startswith("Agent plan for Codex in Default Workspace")
    assert "1. [>] Audit the runtime status view (priority: high)" in plan_text
    back_button = find_inline_button(plan_markup, "Back to Bot Status")

    back_update = FakeCallbackUpdate(123, back_button.callback_data, message=callback_message)
    run(handle_callback_query(back_update, None, services, ui_state))

    restored_text, restored_markup = callback_message.edit_calls[-1]
    assert restored_text.startswith("Bot status for Codex in Default Workspace")
    assert find_inline_button(restored_markup, "Agent Plan")


def test_agent_plan_detail_can_open_and_back():
    from talk2agent.bots.telegram_bot import (
        BUTTON_BOT_STATUS,
        TelegramUiState,
        handle_callback_query,
        handle_text,
    )

    session = FakeSession(
        session_id="session-live",
        plan_entries=(
            SimpleNamespace(
                content="Audit the runtime status view and confirm every status callback restores correctly.",
                status="completed",
                priority="high",
            ),
            SimpleNamespace(
                content="Update Telegram bot tests for the new Agent Plan drill-down.",
                status="pending",
                priority="medium",
            ),
        ),
    )
    ui_state = TelegramUiState()
    update = FakeUpdate(user_id=123, text=BUTTON_BOT_STATUS)
    services, _ = make_services(provider="codex", session=session)

    run(handle_text(update, None, services, ui_state))

    plan_button = find_inline_button(update.message.reply_markups[0], "Agent Plan")
    callback_message = FakeIncomingMessage("status")
    plan_update = FakeCallbackUpdate(123, plan_button.callback_data, message=callback_message)
    run(handle_callback_query(plan_update, None, services, ui_state))

    open_button = find_inline_button(callback_message.edit_calls[-1][1], "Open 1")
    detail_update = FakeCallbackUpdate(123, open_button.callback_data, message=callback_message)
    run(handle_callback_query(detail_update, None, services, ui_state))

    detail_text, detail_markup = callback_message.edit_calls[-1]
    assert detail_text.startswith("Agent plan for Codex in Default Workspace")
    assert "Item: 1/2" in detail_text
    assert "Status: completed" in detail_text
    assert "Priority: high" in detail_text
    assert "Content:" in detail_text
    assert "Audit the runtime status view and confirm every status callback restores correctly." in detail_text

    back_button = find_inline_button(detail_markup, "Back to Agent Plan")
    back_update = FakeCallbackUpdate(123, back_button.callback_data, message=callback_message)
    run(handle_callback_query(back_update, None, services, ui_state))

    restored_text, restored_markup = callback_message.edit_calls[-1]
    assert restored_text.startswith("Agent plan for Codex in Default Workspace")
    assert find_inline_button(restored_markup, "Open 1")


def test_bot_status_shows_recent_tool_activity_preview():
    from talk2agent.bots.telegram_bot import BUTTON_BOT_STATUS, TelegramUiState, handle_text

    session = FakeSession(
        session_id="session-live",
        recent_tool_activities=(
            SimpleNamespace(
                tool_call_id="tool-1",
                title="Run tests",
                status="completed",
                kind="execute",
                details=("cmd: python -m pytest -q", "paths: tests/test_app.py:12"),
            ),
            SimpleNamespace(
                tool_call_id="tool-2",
                title="Read file",
                status="in_progress",
                kind="read",
                details=("target: talk2agent/app.py",),
            ),
        ),
    )
    update = FakeUpdate(user_id=123, text=BUTTON_BOT_STATUS)
    services, _ = make_services(provider="codex", session=session)

    run(handle_text(update, None, services, TelegramUiState()))

    text = update.message.reply_calls[0]
    assert "Recent tools: 2" in text
    assert "Tool preview:" in text
    assert "1. [completed] Run tests (execute, cmd: python -m pytest -q, paths: tests/test_app.py:12)" in text
    assert "2. [in_progress] Read file (read, target: talk2agent/app.py)" in text


def test_bot_status_can_open_tool_activity_and_back_to_status():
    from talk2agent.bots.telegram_bot import (
        BUTTON_BOT_STATUS,
        TelegramUiState,
        handle_callback_query,
        handle_text,
    )

    session = FakeSession(
        session_id="session-live",
        recent_tool_activities=(
            SimpleNamespace(
                tool_call_id="tool-1",
                title="Run tests",
                status="completed",
                kind="execute",
                details=("cmd: python -m pytest -q",),
            ),
        ),
    )
    ui_state = TelegramUiState()
    update = FakeUpdate(user_id=123, text=BUTTON_BOT_STATUS)
    services, _ = make_services(provider="codex", session=session)

    run(handle_text(update, None, services, ui_state))

    tool_button = find_inline_button(update.message.reply_markups[0], "Tool Activity")
    callback_message = FakeIncomingMessage("status")
    tool_update = FakeCallbackUpdate(123, tool_button.callback_data, message=callback_message)
    run(handle_callback_query(tool_update, None, services, ui_state))

    tool_text, tool_markup = callback_message.edit_calls[-1]
    assert tool_text.startswith("Tool activity for Codex in Default Workspace")
    assert "1. [completed] Run tests (execute, cmd: python -m pytest -q)" in tool_text
    back_button = find_inline_button(tool_markup, "Back to Bot Status")

    back_update = FakeCallbackUpdate(123, back_button.callback_data, message=callback_message)
    run(handle_callback_query(back_update, None, services, ui_state))

    restored_text, restored_markup = callback_message.edit_calls[-1]
    assert restored_text.startswith("Bot status for Codex in Default Workspace")
    assert find_inline_button(restored_markup, "Tool Activity")


def test_tool_activity_detail_can_open_file_preview_and_back(monkeypatch, tmp_path):
    from talk2agent.bots import telegram_bot
    from talk2agent.bots.telegram_bot import (
        BUTTON_BOT_STATUS,
        TelegramUiState,
        handle_callback_query,
        handle_text,
    )
    from talk2agent.workspace_git import WorkspaceGitStatus, WorkspaceGitStatusEntry

    target = tmp_path / "tests" / "test_app.py"
    target.parent.mkdir(parents=True)
    target.write_text("print('hello')\n", encoding="utf-8")

    monkeypatch.setattr(
        telegram_bot,
        "read_workspace_git_status",
        lambda _path: WorkspaceGitStatus(
            is_git_repo=True,
            branch_line="main",
            entries=(
                WorkspaceGitStatusEntry("M ", "tests/test_app.py", "tests/test_app.py"),
            ),
        ),
    )

    session = FakeSession(
        session_id="session-live",
        recent_tool_activities=(
            SimpleNamespace(
                tool_call_id="tool-1",
                title="Run tests",
                status="completed",
                kind="execute",
                details=("cmd: python -m pytest -q", "paths: tests/test_app.py:12"),
                path_refs=("tests/test_app.py:12",),
                paths=("tests/test_app.py",),
                terminal_ids=("term-1",),
                content_types=("diff",),
            ),
        ),
        terminal_outputs={
            "term-1": SimpleNamespace(
                output="hello\nworld",
                truncated=False,
                exit_status=SimpleNamespace(exit_code=0, signal=None),
            )
        },
    )
    ui_state = TelegramUiState()
    update = FakeUpdate(user_id=123, text=BUTTON_BOT_STATUS)
    services, _ = make_services(provider="codex", session=session, workspace_path=str(tmp_path))

    run(handle_text(update, None, services, ui_state))

    tool_button = find_inline_button(update.message.reply_markups[0], "Tool Activity")
    callback_message = FakeIncomingMessage("status")
    tool_update = FakeCallbackUpdate(123, tool_button.callback_data, message=callback_message)
    run(handle_callback_query(tool_update, None, services, ui_state))

    open_button = find_inline_button(callback_message.edit_calls[-1][1], "Open 1")
    detail_update = FakeCallbackUpdate(123, open_button.callback_data, message=callback_message)
    run(handle_callback_query(detail_update, None, services, ui_state))

    detail_text, detail_markup = callback_message.edit_calls[-1]
    assert detail_text.startswith("Tool activity for Codex in Default Workspace")
    assert "Item: 1/1" in detail_text
    assert "1. term-1 [exit=0]" in detail_text
    assert "output:\nhello\nworld" in detail_text
    assert find_inline_button(detail_markup, "Open File 1")
    assert find_inline_button(detail_markup, "Open Change 1")

    file_button = find_inline_button(detail_markup, "Open File 1")
    file_update = FakeCallbackUpdate(123, file_button.callback_data, message=callback_message)
    run(handle_callback_query(file_update, None, services, ui_state))

    file_text, file_markup = callback_message.edit_calls[-1]
    assert file_text.startswith("Workspace file for Codex in Default Workspace")
    assert "Path: tests/test_app.py" in file_text

    back_button = find_inline_button(file_markup, "Back to Tool Activity")
    back_update = FakeCallbackUpdate(123, back_button.callback_data, message=callback_message)
    run(handle_callback_query(back_update, None, services, ui_state))

    restored_text, restored_markup = callback_message.edit_calls[-1]
    assert restored_text.startswith("Tool activity for Codex in Default Workspace")
    assert find_inline_button(restored_markup, "Open File 1")


def test_tool_activity_detail_can_open_change_preview_and_back(monkeypatch, tmp_path):
    from talk2agent.bots import telegram_bot
    from talk2agent.bots.telegram_bot import (
        BUTTON_BOT_STATUS,
        TelegramUiState,
        handle_callback_query,
        handle_text,
    )
    from talk2agent.workspace_git import WorkspaceGitStatus, WorkspaceGitStatusEntry

    target = tmp_path / "tests" / "test_app.py"
    target.parent.mkdir(parents=True)
    target.write_text("print('hello')\n", encoding="utf-8")

    monkeypatch.setattr(
        telegram_bot,
        "read_workspace_git_status",
        lambda _path: WorkspaceGitStatus(
            is_git_repo=True,
            branch_line="main",
            entries=(
                WorkspaceGitStatusEntry("M ", "tests/test_app.py", "tests/test_app.py"),
            ),
        ),
    )
    monkeypatch.setattr(
        telegram_bot,
        "read_workspace_git_diff_preview",
        lambda _root, relative_path, status_code: SimpleNamespace(
            relative_path=relative_path,
            status_code=status_code,
            text="diff --git a/tests/test_app.py b/tests/test_app.py",
            truncated=False,
        ),
    )

    session = FakeSession(
        session_id="session-live",
        recent_tool_activities=(
            SimpleNamespace(
                tool_call_id="tool-1",
                title="Run tests",
                status="completed",
                kind="execute",
                details=("cmd: python -m pytest -q", "paths: tests/test_app.py:12"),
                path_refs=("tests/test_app.py:12",),
                paths=("tests/test_app.py",),
                terminal_ids=(),
                content_types=("diff",),
            ),
        ),
    )
    ui_state = TelegramUiState()
    update = FakeUpdate(user_id=123, text=BUTTON_BOT_STATUS)
    services, _ = make_services(provider="codex", session=session, workspace_path=str(tmp_path))

    run(handle_text(update, None, services, ui_state))

    tool_button = find_inline_button(update.message.reply_markups[0], "Tool Activity")
    callback_message = FakeIncomingMessage("status")
    tool_update = FakeCallbackUpdate(123, tool_button.callback_data, message=callback_message)
    run(handle_callback_query(tool_update, None, services, ui_state))

    open_button = find_inline_button(callback_message.edit_calls[-1][1], "Open 1")
    detail_update = FakeCallbackUpdate(123, open_button.callback_data, message=callback_message)
    run(handle_callback_query(detail_update, None, services, ui_state))

    change_button = find_inline_button(callback_message.edit_calls[-1][1], "Open Change 1")
    change_update = FakeCallbackUpdate(123, change_button.callback_data, message=callback_message)
    run(handle_callback_query(change_update, None, services, ui_state))

    change_text, change_markup = callback_message.edit_calls[-1]
    assert change_text.startswith("Workspace change for Codex in Default Workspace")
    assert "Path: tests/test_app.py" in change_text
    assert "Status: M " in change_text

    back_button = find_inline_button(change_markup, "Back to Tool Activity")
    back_update = FakeCallbackUpdate(123, back_button.callback_data, message=callback_message)
    run(handle_callback_query(back_update, None, services, ui_state))

    restored_text, restored_markup = callback_message.edit_calls[-1]
    assert restored_text.startswith("Tool activity for Codex in Default Workspace")
    assert find_inline_button(restored_markup, "Open Change 1")


def test_bot_status_shows_recent_sessions_preview_and_quick_buttons():
    from talk2agent.acp.agent_session import PromptText
    from talk2agent.bots.telegram_bot import BUTTON_BOT_STATUS, TelegramUiState, _ReplayTurn, handle_text

    ui_state = TelegramUiState()
    ui_state.set_last_turn(
        123,
        _ReplayTurn(
            provider="codex",
            workspace_id="default",
            prompt_items=(PromptText("hello"),),
            title_hint="hello",
        ),
    )
    session = FakeSession(session_id="session-live")
    update = FakeUpdate(user_id=123, text=BUTTON_BOT_STATUS)
    services, _ = make_services(
        provider="codex",
        session=session,
        history_entries=[
            build_history_entry("session-live", "Active Thread"),
            build_history_entry("session-1", "First"),
            build_history_entry("session-2", "Second"),
            build_history_entry("session-3", "Third"),
        ],
    )

    run(handle_text(update, None, services, ui_state))

    text = update.message.reply_calls[0]
    assert "Recent sessions:" in text
    assert "1. First (session-1)" in text
    assert "2. Second (session-2)" in text
    assert "... 1 more session" in text
    markup = update.message.reply_markups[0]
    assert find_inline_button(markup, "Switch 1")
    assert find_inline_button(markup, "Switch+Retry 1")
    assert find_inline_button(markup, "Switch 2")
    assert find_inline_button(markup, "Switch+Retry 2")


def test_bot_status_shows_command_preview_for_live_session():
    from talk2agent.bots.telegram_bot import BUTTON_BOT_STATUS, TelegramUiState, handle_text

    session = FakeSession(
        session_id="session-abc",
        available_commands=[
            FakeCommand("status", "Show status"),
            FakeCommand("model", "Switch model", hint="model id"),
            FakeCommand("review", "Review current worktree"),
            FakeCommand("test", "Run tests"),
        ],
    )
    update = FakeUpdate(user_id=123, text=BUTTON_BOT_STATUS)
    services, _ = make_services(provider="codex", session=session)

    run(handle_text(update, None, services, TelegramUiState()))

    text = update.message.reply_calls[0]
    assert "Session: session-abc" in text
    assert "Agent commands cached: 4" in text
    assert "Command preview:" in text
    assert "1. /status" in text
    assert "2. /model args: model id" in text
    assert "3. /review" in text
    assert "... 1 more command" in text
    markup = update.message.reply_markups[0]
    assert find_inline_button(markup, "Run /status")
    assert find_inline_button(markup, "Args /model")
    assert find_inline_button(markup, "Run /review")
    assert find_inline_button(markup, "Model: GPT-5.4 Mini")
    assert find_inline_button(markup, "Mode: low")


def test_bot_status_agent_command_quick_run_returns_to_status():
    from talk2agent.bots.telegram_bot import (
        BUTTON_BOT_STATUS,
        TelegramUiState,
        handle_callback_query,
        handle_text,
    )

    session = FakeSession(
        session_id="session-abc",
        available_commands=[FakeCommand("status", "Show status")],
    )
    ui_state = TelegramUiState()
    update = FakeUpdate(user_id=123, text=BUTTON_BOT_STATUS)
    callback_message = FakeIncomingMessage("status")
    services, store = make_services(provider="codex", session=session)

    run(handle_text(update, None, services, ui_state))

    run_button = find_inline_button(update.message.reply_markups[0], "Run /status")
    run_update = FakeCallbackUpdate(123, run_button.callback_data, message=callback_message)
    run(handle_callback_query(run_update, make_context(application=FakeApplication()), services, ui_state))

    assert session.prompts == ["/status"]
    assert callback_message.reply_calls == ["hello world"]
    assert store.record_session_usage_calls == [(123, "session-abc", "/status")]
    final_text, final_markup = callback_message.edit_calls[-1]
    assert final_text.startswith("Ran /status.\nBot status for Codex in Default Workspace")
    assert find_inline_button(final_markup, "Run /status")


def test_bot_status_agent_command_quick_args_cancel_returns_to_status():
    from talk2agent.bots.telegram_bot import (
        BUTTON_BOT_STATUS,
        TelegramUiState,
        handle_callback_query,
        handle_text,
    )

    session = FakeSession(
        session_id="session-abc",
        available_commands=[FakeCommand("model", "Switch model", hint="model id")],
    )
    ui_state = TelegramUiState()
    update = FakeUpdate(user_id=123, text=BUTTON_BOT_STATUS)
    callback_message = FakeIncomingMessage("status")
    services, _ = make_services(provider="codex", session=session)

    run(handle_text(update, None, services, ui_state))

    args_button = find_inline_button(update.message.reply_markups[0], "Args /model")
    args_update = FakeCallbackUpdate(123, args_button.callback_data, message=callback_message)
    run(handle_callback_query(args_update, None, services, ui_state))

    prompt_text, prompt_markup = callback_message.edit_calls[-1]
    assert prompt_text.startswith("Send arguments for /model as your next plain text message.")

    cancel_button = find_inline_button(prompt_markup, "Cancel Command")
    cancel_update = FakeCallbackUpdate(123, cancel_button.callback_data, message=callback_message)
    run(handle_callback_query(cancel_update, None, services, ui_state))

    restored_text, restored_markup = callback_message.edit_calls[-1]
    assert restored_text.startswith(
        "Command input cancelled.\nBot status for Codex in Default Workspace"
    )
    assert find_inline_button(restored_markup, "Args /model")


def test_bot_status_agent_command_quick_args_run_returns_to_status():
    from talk2agent.bots.telegram_bot import (
        BUTTON_BOT_STATUS,
        TelegramUiState,
        handle_callback_query,
        handle_text,
    )

    session = FakeSession(
        session_id="session-abc",
        available_commands=[FakeCommand("model", "Switch model", hint="model id")],
    )
    ui_state = TelegramUiState()
    update = FakeUpdate(user_id=123, text=BUTTON_BOT_STATUS)
    callback_message = FakeIncomingMessage("status")
    services, store = make_services(provider="codex", session=session)

    run(handle_text(update, None, services, ui_state))

    args_button = find_inline_button(update.message.reply_markups[0], "Args /model")
    args_update = FakeCallbackUpdate(123, args_button.callback_data, message=callback_message)
    run(handle_callback_query(args_update, None, services, ui_state))

    request_update = FakeUpdate(user_id=123, text="gpt-5.4-mini")
    run(handle_text(request_update, None, services, ui_state))

    assert session.prompts == ["/model gpt-5.4-mini"]
    assert request_update.message.reply_calls == ["hello world"]
    assert store.record_session_usage_calls == [(123, "session-abc", "/model gpt-5.4-mini")]
    final_text, final_markup = callback_message.edit_calls[-1]
    assert final_text.startswith("Ran /model.\nBot status for Codex in Default Workspace")
    assert find_inline_button(final_markup, "Args /model")


def test_bot_status_model_quick_selection_returns_to_status():
    from talk2agent.bots.telegram_bot import (
        BUTTON_BOT_STATUS,
        TelegramUiState,
        handle_callback_query,
        handle_text,
    )

    ui_state = TelegramUiState()
    update = FakeUpdate(user_id=123, text=BUTTON_BOT_STATUS)
    callback_message = FakeIncomingMessage("status")
    services, store = make_services(provider="codex")

    run(handle_text(update, None, services, ui_state))

    select_button = find_inline_button(update.message.reply_markups[0], "Model: GPT-5.4 Mini")
    select_update = FakeCallbackUpdate(123, select_button.callback_data, message=callback_message)
    run(handle_callback_query(select_update, make_context(application=FakeApplication()), services, ui_state))

    assert services.final_session.set_selection_calls == [("model", "gpt-5.4-mini")]
    assert store.record_session_usage_calls == [(123, "session-123", None)]
    final_text, final_markup = callback_message.edit_calls[-1]
    assert final_text.startswith("Updated model to GPT-5.4 Mini.\nBot status for Codex in Default Workspace")
    assert "Model: GPT-5.4 Mini" in final_text
    assert find_inline_button(final_markup, "Model: GPT-5.4")


def test_bot_status_mode_quick_selection_returns_to_status():
    from talk2agent.bots.telegram_bot import (
        BUTTON_BOT_STATUS,
        TelegramUiState,
        handle_callback_query,
        handle_text,
    )

    ui_state = TelegramUiState()
    update = FakeUpdate(user_id=123, text=BUTTON_BOT_STATUS)
    callback_message = FakeIncomingMessage("status")
    services, store = make_services(provider="codex")

    run(handle_text(update, None, services, ui_state))

    select_button = find_inline_button(update.message.reply_markups[0], "Mode: low")
    select_update = FakeCallbackUpdate(123, select_button.callback_data, message=callback_message)
    run(handle_callback_query(select_update, make_context(application=FakeApplication()), services, ui_state))

    assert services.final_session.set_selection_calls == [("mode", "low")]
    assert store.record_session_usage_calls == [(123, "session-123", None)]
    final_text, final_markup = callback_message.edit_calls[-1]
    assert final_text.startswith("Updated mode to low.\nBot status for Codex in Default Workspace")
    assert "Mode: low" in final_text
    assert find_inline_button(final_markup, "Mode: xhigh")


def test_bot_status_recent_session_quick_switch_returns_to_status():
    from talk2agent.bots.telegram_bot import (
        BUTTON_BOT_STATUS,
        TelegramUiState,
        handle_callback_query,
        handle_text,
    )

    session = FakeSession(session_id="session-live")
    ui_state = TelegramUiState()
    update = FakeUpdate(user_id=123, text=BUTTON_BOT_STATUS)
    callback_message = FakeIncomingMessage("status")
    services, store = make_services(
        provider="codex",
        session=session,
        history_entries=[
            build_history_entry("session-live", "Active Thread"),
            build_history_entry("session-1", "First"),
        ],
    )

    run(handle_text(update, None, services, ui_state))

    switch_button = find_inline_button(update.message.reply_markups[0], "Switch 1")
    switch_update = FakeCallbackUpdate(123, switch_button.callback_data, message=callback_message)
    run(handle_callback_query(switch_update, make_context(application=FakeApplication()), services, ui_state))

    assert store.activate_history_calls == [(123, "session-1")]
    final_text, final_markup = callback_message.edit_calls[-1]
    assert final_text.startswith(
        "Switched to session session-1 on Codex in Default Workspace. "
        "Old bot buttons and pending inputs tied to the previous session were cleared.\n"
        "Bot status for Codex in Default Workspace"
    )
    assert "Session: session-1" in final_text
    assert find_inline_button(final_markup, "Session History")


def test_bot_status_recent_session_quick_retry_returns_to_status():
    from talk2agent.acp.agent_session import PromptText
    from talk2agent.bots.telegram_bot import (
        BUTTON_BOT_STATUS,
        TelegramUiState,
        _ReplayTurn,
        handle_callback_query,
        handle_text,
    )

    ui_state = TelegramUiState()
    ui_state.set_last_turn(
        123,
        _ReplayTurn(
            provider="codex",
            workspace_id="default",
            prompt_items=(PromptText("hello"),),
            title_hint="hello",
        ),
    )
    session = FakeSession(session_id="session-live")
    update = FakeUpdate(user_id=123, text=BUTTON_BOT_STATUS)
    callback_message = FakeIncomingMessage("status")
    services, store = make_services(
        provider="codex",
        session=session,
        history_entries=[
            build_history_entry("session-live", "Active Thread"),
            build_history_entry("session-1", "First"),
        ],
    )

    run(handle_text(update, None, services, ui_state))

    retry_button = find_inline_button(update.message.reply_markups[0], "Switch+Retry 1")
    retry_update = FakeCallbackUpdate(123, retry_button.callback_data, message=callback_message)
    run(handle_callback_query(retry_update, make_context(application=FakeApplication()), services, ui_state))

    assert store.activate_history_calls == [(123, "session-1")]
    assert session.prompt_items == [(PromptText("hello"),)]
    assert callback_message.reply_calls == ["hello world"]
    final_text, final_markup = callback_message.edit_calls[-1]
    assert final_text.startswith(
        "Switched to session session-1 on Codex in Default Workspace. "
        "Old bot buttons and pending inputs tied to the previous session were cleared.\n"
        "Retried last turn in this session.\n"
        "Bot status for Codex in Default Workspace"
    )
    assert "Session: session-1" in final_text
    assert find_inline_button(final_markup, "Session History")


def test_bot_status_recent_session_quick_switch_failure_restores_status():
    from talk2agent.bots.telegram_bot import (
        BUTTON_BOT_STATUS,
        TelegramUiState,
        handle_callback_query,
        handle_text,
    )

    session = FakeSession(session_id="session-live")
    ui_state = TelegramUiState()
    update = FakeUpdate(user_id=123, text=BUTTON_BOT_STATUS)
    callback_message = FakeIncomingMessage("status")
    services, _ = make_services(
        provider="codex",
        session=session,
        history_entries=[
            build_history_entry("session-live", "Active Thread"),
            build_history_entry("session-1", "First"),
        ],
        activate_history_error=RuntimeError("boom"),
    )

    run(handle_text(update, None, services, ui_state))

    switch_button = find_inline_button(update.message.reply_markups[0], "Switch 1")
    switch_update = FakeCallbackUpdate(123, switch_button.callback_data, message=callback_message)
    run(handle_callback_query(switch_update, None, services, ui_state))

    final_text, final_markup = callback_message.edit_calls[-1]
    assert final_text.startswith("Failed to switch session.\nBot status for Codex in Default Workspace")
    assert find_inline_button(final_markup, "Switch 1")


def test_bot_status_shows_selection_retry_quick_buttons_when_last_turn_exists():
    from talk2agent.acp.agent_session import PromptText
    from talk2agent.bots.telegram_bot import (
        BUTTON_BOT_STATUS,
        TelegramUiState,
        _ReplayTurn,
        handle_text,
    )

    ui_state = TelegramUiState()
    ui_state.set_last_turn(
        123,
        _ReplayTurn(
            provider="codex",
            workspace_id="default",
            prompt_items=(PromptText("hello"),),
            title_hint="hello",
        ),
    )
    update = FakeUpdate(user_id=123, text=BUTTON_BOT_STATUS)
    services, _ = make_services(provider="codex")

    run(handle_text(update, None, services, ui_state))

    markup = update.message.reply_markups[0]
    assert find_inline_button(markup, "Model+Retry: GPT-5.4 Mini")
    assert find_inline_button(markup, "Mode+Retry: low")


def test_bot_status_model_quick_retry_returns_to_status():
    from talk2agent.acp.agent_session import PromptText
    from talk2agent.bots.telegram_bot import (
        BUTTON_BOT_STATUS,
        TelegramUiState,
        _ReplayTurn,
        handle_callback_query,
        handle_text,
    )

    ui_state = TelegramUiState()
    ui_state.set_last_turn(
        123,
        _ReplayTurn(
            provider="codex",
            workspace_id="default",
            prompt_items=(PromptText("hello"),),
            title_hint="hello",
        ),
    )
    update = FakeUpdate(user_id=123, text=BUTTON_BOT_STATUS)
    callback_message = FakeIncomingMessage("status")
    services, store = make_services(provider="codex")

    run(handle_text(update, None, services, ui_state))

    retry_button = find_inline_button(update.message.reply_markups[0], "Model+Retry: GPT-5.4 Mini")
    retry_update = FakeCallbackUpdate(123, retry_button.callback_data, message=callback_message)
    run(handle_callback_query(retry_update, make_context(application=FakeApplication()), services, ui_state))

    assert services.final_session.set_selection_calls == [("model", "gpt-5.4-mini")]
    assert services.final_session.prompt_items == [(PromptText("hello"),)]
    assert callback_message.reply_calls == ["hello world"]
    assert store.record_session_usage_calls == [
        (123, "session-123", None),
        (123, "session-123", "hello"),
    ]
    final_text, final_markup = callback_message.edit_calls[-1]
    assert final_text.startswith(
        "Updated model to GPT-5.4 Mini.\n"
        "Retried last turn with the updated setting.\n"
        "Bot status for Codex in Default Workspace"
    )
    assert "Model: GPT-5.4 Mini" in final_text
    assert find_inline_button(final_markup, "Model+Retry: GPT-5.4")


def test_bot_status_can_open_session_history_from_callback():
    from talk2agent.bots.telegram_bot import (
        BUTTON_BOT_STATUS,
        TelegramUiState,
        handle_callback_query,
        handle_text,
    )

    ui_state = TelegramUiState()
    ui_state.set_pending_text_action(123, "workspace_search")
    update = FakeUpdate(user_id=123, text=BUTTON_BOT_STATUS)
    services, _ = make_services(
        provider="codex",
        history_entries=[build_history_entry("session-1", "First")],
    )

    run(handle_text(update, None, services, ui_state))

    history_button = find_inline_button(update.message.reply_markups[0], "Session History")
    history_update = FakeCallbackUpdate(
        123,
        history_button.callback_data,
        message=FakeIncomingMessage("status"),
    )
    run(handle_callback_query(history_update, None, services, ui_state))

    assert ui_state.get_pending_text_action(123) is None
    edited_text, edited_markup = history_update.callback_query.message.edit_calls[-1]
    assert edited_text.startswith("Session history for Codex in Default Workspace")
    assert find_inline_button(edited_markup, "Run 1")
    assert find_inline_button(edited_markup, "Back to Bot Status")


def test_bot_status_session_history_back_to_status():
    from talk2agent.bots.telegram_bot import (
        BUTTON_BOT_STATUS,
        TelegramUiState,
        handle_callback_query,
        handle_text,
    )

    ui_state = TelegramUiState()
    update = FakeUpdate(user_id=123, text=BUTTON_BOT_STATUS)
    services, _ = make_services(
        provider="codex",
        history_entries=[build_history_entry("session-1", "First")],
    )

    run(handle_text(update, None, services, ui_state))

    callback_message = FakeIncomingMessage("status")
    history_button = find_inline_button(update.message.reply_markups[0], "Session History")
    history_update = FakeCallbackUpdate(123, history_button.callback_data, message=callback_message)
    run(handle_callback_query(history_update, None, services, ui_state))

    back_button = find_inline_button(callback_message.edit_calls[-1][1], "Back to Bot Status")
    back_update = FakeCallbackUpdate(123, back_button.callback_data, message=callback_message)
    run(handle_callback_query(back_update, None, services, ui_state))

    restored_text, restored_markup = callback_message.edit_calls[-1]
    assert restored_text.startswith("Bot status for Codex in Default Workspace")
    assert find_inline_button(restored_markup, "Session History")


def test_bot_status_hides_provider_sessions_for_non_admin():
    from talk2agent.bots.telegram_bot import BUTTON_BOT_STATUS, TelegramUiState, handle_text

    update = FakeUpdate(user_id=456, text=BUTTON_BOT_STATUS)
    services, _ = make_services(
        allowed_user_ids={123, 456},
        admin_user_id=123,
    )

    run(handle_text(update, None, services, TelegramUiState()))

    labels = [button.text for row in update.message.reply_markups[0].inline_keyboard for button in row]
    assert "Provider Sessions" not in labels


def test_bot_status_can_open_provider_sessions_and_back_to_status():
    from talk2agent.bots.telegram_bot import (
        BUTTON_BOT_STATUS,
        TelegramUiState,
        handle_callback_query,
        handle_text,
    )

    provider_page = SimpleNamespace(
        entries=(build_provider_session("desktop-session", "Desktop Flow", cwd_label="src"),),
        next_cursor=None,
        supported=True,
    )
    ui_state = TelegramUiState()
    update = FakeUpdate(user_id=123, text=BUTTON_BOT_STATUS)
    services, _ = make_services(provider="codex", provider_session_pages={None: provider_page})

    run(handle_text(update, None, services, ui_state))

    provider_button = find_inline_button(update.message.reply_markups[0], "Provider Sessions")
    callback_message = FakeIncomingMessage("status")
    provider_update = FakeCallbackUpdate(123, provider_button.callback_data, message=callback_message)
    run(handle_callback_query(provider_update, None, services, ui_state))

    provider_text, provider_markup = callback_message.edit_calls[-1]
    assert provider_text.startswith("Provider sessions for Codex in Default Workspace")
    back_button = find_inline_button(provider_markup, "Back to Bot Status")

    back_update = FakeCallbackUpdate(123, back_button.callback_data, message=callback_message)
    run(handle_callback_query(back_update, None, services, ui_state))

    restored_text, restored_markup = callback_message.edit_calls[-1]
    assert restored_text.startswith("Bot status for Codex in Default Workspace")
    assert find_inline_button(restored_markup, "Provider Sessions")


def test_bot_status_provider_session_detail_shows_fields_and_actions():
    from talk2agent.acp.agent_session import PromptText
    from talk2agent.bots.telegram_bot import (
        BUTTON_BOT_STATUS,
        TelegramUiState,
        _ReplayTurn,
        handle_callback_query,
        handle_text,
    )

    provider_page = SimpleNamespace(
        entries=(build_provider_session("desktop-session", "Desktop Flow", cwd_label="src"),),
        next_cursor=None,
        supported=True,
    )
    ui_state = TelegramUiState()
    ui_state.set_last_turn(
        123,
        _ReplayTurn(
            provider="codex",
            workspace_id="default",
            prompt_items=(PromptText("hello"),),
            title_hint="hello",
        ),
    )
    update = FakeUpdate(user_id=123, text=BUTTON_BOT_STATUS)
    services, _ = make_services(provider="codex", provider_session_pages={None: provider_page})

    run(handle_text(update, None, services, ui_state))

    provider_button = find_inline_button(update.message.reply_markups[0], "Provider Sessions")
    callback_message = FakeIncomingMessage("status")
    provider_update = FakeCallbackUpdate(123, provider_button.callback_data, message=callback_message)
    run(handle_callback_query(provider_update, None, services, ui_state))

    open_button = find_inline_button(callback_message.edit_calls[-1][1], "Open 1")
    open_update = FakeCallbackUpdate(123, open_button.callback_data, message=callback_message)
    run(handle_callback_query(open_update, None, services, ui_state))

    detail_text, detail_markup = callback_message.edit_calls[-1]
    assert detail_text.startswith("Provider session for Codex in Default Workspace")
    assert "Title: Desktop Flow" in detail_text
    assert "Session: desktop-session" in detail_text
    assert "Current runtime session: no" in detail_text
    assert "Workspace-relative cwd: src" in detail_text
    assert "Provider cwd: F:/workspace/src" in detail_text
    assert "Updated: 2026-03-26T00:00:00+00:00" in detail_text
    assert find_inline_button(detail_markup, "Refresh")
    assert find_inline_button(detail_markup, "Back to Provider Sessions")
    assert find_inline_button(detail_markup, "Run Session")
    assert find_inline_button(detail_markup, "Run+Retry Session")
    assert find_inline_button(detail_markup, "Fork Session")
    assert find_inline_button(detail_markup, "Fork+Retry Session")

    back_to_provider_button = find_inline_button(detail_markup, "Back to Provider Sessions")
    back_to_provider_update = FakeCallbackUpdate(
        123,
        back_to_provider_button.callback_data,
        message=callback_message,
    )
    run(handle_callback_query(back_to_provider_update, None, services, ui_state))

    provider_text, provider_markup = callback_message.edit_calls[-1]
    assert provider_text.startswith("Provider sessions for Codex in Default Workspace")
    back_to_status_button = find_inline_button(provider_markup, "Back to Bot Status")

    back_to_status_update = FakeCallbackUpdate(
        123,
        back_to_status_button.callback_data,
        message=callback_message,
    )
    run(handle_callback_query(back_to_status_update, None, services, ui_state))

    restored_text, restored_markup = callback_message.edit_calls[-1]
    assert restored_text.startswith("Bot status for Codex in Default Workspace")
    assert find_inline_button(restored_markup, "Provider Sessions")


def test_bot_status_provider_session_run_returns_to_status():
    from talk2agent.bots.telegram_bot import (
        BUTTON_BOT_STATUS,
        TelegramUiState,
        handle_callback_query,
        handle_text,
    )

    provider_page = SimpleNamespace(
        entries=(build_provider_session("desktop-session", "Desktop Flow", cwd_label="src"),),
        next_cursor=None,
        supported=True,
    )
    ui_state = TelegramUiState()
    update = FakeUpdate(user_id=123, text=BUTTON_BOT_STATUS)
    application = FakeApplication()
    services, store = make_services(
        provider="codex",
        session=FakeSession(available_commands=[FakeCommand("status", "Show status")]),
        provider_session_pages={None: provider_page},
    )

    run(handle_text(update, None, services, ui_state))

    provider_button = find_inline_button(update.message.reply_markups[0], "Provider Sessions")
    callback_message = FakeIncomingMessage("status")
    provider_update = FakeCallbackUpdate(123, provider_button.callback_data, message=callback_message)
    run(handle_callback_query(provider_update, None, services, ui_state))

    run_button = find_inline_button(callback_message.edit_calls[-1][1], "Run 1")
    run_update = FakeCallbackUpdate(123, run_button.callback_data, message=callback_message)
    run(handle_callback_query(run_update, make_context(application=application), services, ui_state))

    assert store.activate_provider_calls == [(123, "desktop-session", "Desktop Flow")]
    final_text, final_markup = callback_message.edit_calls[-1]
    assert final_text.startswith(
        "Switched to provider session desktop-session. "
        "Old bot buttons and pending inputs tied to the previous session were cleared.\n"
        "Bot status for Codex in Default Workspace"
    )
    assert "Session: desktop-session" in final_text
    assert find_inline_button(final_markup, "Provider Sessions")


def test_bot_status_provider_session_run_retry_returns_to_status():
    from talk2agent.acp.agent_session import PromptText
    from talk2agent.bots.telegram_bot import (
        BUTTON_BOT_STATUS,
        TelegramUiState,
        _ReplayTurn,
        handle_callback_query,
        handle_text,
    )

    provider_page = SimpleNamespace(
        entries=(build_provider_session("desktop-session", "Desktop Flow", cwd_label="src"),),
        next_cursor=None,
        supported=True,
    )
    ui_state = TelegramUiState()
    ui_state.set_last_turn(
        123,
        _ReplayTurn(
            provider="codex",
            workspace_id="default",
            prompt_items=(PromptText("hello"),),
            title_hint="hello",
        ),
    )
    session = FakeSession(
        session_id="session-live",
        available_commands=[FakeCommand("status", "Show status")],
    )
    update = FakeUpdate(user_id=123, text=BUTTON_BOT_STATUS)
    callback_message = FakeIncomingMessage("status")
    services, store = make_services(
        provider="codex",
        session=session,
        provider_session_pages={None: provider_page},
    )

    run(handle_text(update, None, services, ui_state))

    provider_button = find_inline_button(update.message.reply_markups[0], "Provider Sessions")
    provider_update = FakeCallbackUpdate(123, provider_button.callback_data, message=callback_message)
    run(handle_callback_query(provider_update, None, services, ui_state))

    retry_button = find_inline_button(callback_message.edit_calls[-1][1], "Run+Retry 1")
    retry_update = FakeCallbackUpdate(123, retry_button.callback_data, message=callback_message)
    run(handle_callback_query(retry_update, make_context(application=FakeApplication()), services, ui_state))

    assert store.activate_provider_calls == [(123, "desktop-session", "Desktop Flow")]
    assert session.prompt_items == [(PromptText("hello"),)]
    assert callback_message.reply_calls == ["hello world"]
    final_text, final_markup = callback_message.edit_calls[-1]
    assert final_text.startswith(
        "Switched to provider session desktop-session. "
        "Old bot buttons and pending inputs tied to the previous session were cleared.\n"
        "Retried last turn in this session.\n"
        "Bot status for Codex in Default Workspace"
    )
    assert "Session: desktop-session" in final_text
    assert find_inline_button(final_markup, "Provider Sessions")


def test_bot_status_provider_session_fork_returns_to_status():
    from talk2agent.bots.telegram_bot import (
        BUTTON_BOT_STATUS,
        TelegramUiState,
        handle_callback_query,
        handle_text,
    )

    provider_page = SimpleNamespace(
        entries=(build_provider_session("desktop-session", "Desktop Flow", cwd_label="src"),),
        next_cursor=None,
        supported=True,
    )
    ui_state = TelegramUiState()
    update = FakeUpdate(user_id=123, text=BUTTON_BOT_STATUS)
    application = FakeApplication()
    services, store = make_services(
        provider="codex",
        session=FakeSession(available_commands=[FakeCommand("status", "Show status")]),
        provider_session_pages={None: provider_page},
    )

    run(handle_text(update, None, services, ui_state))

    provider_button = find_inline_button(update.message.reply_markups[0], "Provider Sessions")
    callback_message = FakeIncomingMessage("status")
    provider_update = FakeCallbackUpdate(123, provider_button.callback_data, message=callback_message)
    run(handle_callback_query(provider_update, None, services, ui_state))

    fork_button = find_inline_button(callback_message.edit_calls[-1][1], "Fork 1")
    fork_update = FakeCallbackUpdate(123, fork_button.callback_data, message=callback_message)
    run(handle_callback_query(fork_update, make_context(application=application), services, ui_state))

    assert store.fork_provider_calls == [(123, "desktop-session", "Desktop Flow")]
    assert store.record_session_usage_calls == [(123, "fork-desktop-session", "Desktop Flow")]
    final_text, final_markup = callback_message.edit_calls[-1]
    assert final_text.startswith(
        "Forked provider session desktop-session into fork-desktop-session. "
        "Old bot buttons and pending inputs tied to the previous session were cleared.\n"
        "Bot status for Codex in Default Workspace"
    )
    assert "Session: fork-desktop-session" in final_text
    assert find_inline_button(final_markup, "Provider Sessions")


def test_bot_status_provider_session_fork_retry_returns_to_status():
    from talk2agent.acp.agent_session import PromptText
    from talk2agent.bots.telegram_bot import (
        BUTTON_BOT_STATUS,
        TelegramUiState,
        _ReplayTurn,
        handle_callback_query,
        handle_text,
    )

    provider_page = SimpleNamespace(
        entries=(build_provider_session("desktop-session", "Desktop Flow", cwd_label="src"),),
        next_cursor=None,
        supported=True,
    )
    ui_state = TelegramUiState()
    ui_state.set_last_turn(
        123,
        _ReplayTurn(
            provider="codex",
            workspace_id="default",
            prompt_items=(PromptText("hello"),),
            title_hint="hello",
        ),
    )
    session = FakeSession(
        session_id="session-live",
        available_commands=[FakeCommand("status", "Show status")],
    )
    update = FakeUpdate(user_id=123, text=BUTTON_BOT_STATUS)
    callback_message = FakeIncomingMessage("status")
    services, store = make_services(
        provider="codex",
        session=session,
        provider_session_pages={None: provider_page},
    )

    run(handle_text(update, None, services, ui_state))

    provider_button = find_inline_button(update.message.reply_markups[0], "Provider Sessions")
    provider_update = FakeCallbackUpdate(123, provider_button.callback_data, message=callback_message)
    run(handle_callback_query(provider_update, None, services, ui_state))

    retry_button = find_inline_button(callback_message.edit_calls[-1][1], "Fork+Retry 1")
    retry_update = FakeCallbackUpdate(123, retry_button.callback_data, message=callback_message)
    run(handle_callback_query(retry_update, make_context(application=FakeApplication()), services, ui_state))

    assert store.fork_provider_calls == [(123, "desktop-session", "Desktop Flow")]
    assert session.prompt_items == [(PromptText("hello"),)]
    assert callback_message.reply_calls == ["hello world"]
    assert store.record_session_usage_calls == [
        (123, "fork-desktop-session", "Desktop Flow"),
        (123, "fork-desktop-session", "hello"),
    ]
    final_text, final_markup = callback_message.edit_calls[-1]
    assert final_text.startswith(
        "Forked provider session desktop-session into fork-desktop-session. "
        "Old bot buttons and pending inputs tied to the previous session were cleared.\n"
        "Retried last turn in this session.\n"
        "Bot status for Codex in Default Workspace"
    )
    assert "Session: fork-desktop-session" in final_text
    assert find_inline_button(final_markup, "Provider Sessions")


def test_bot_status_provider_session_run_failure_restores_provider_sessions_with_back_to_status():
    from talk2agent.bots.telegram_bot import (
        BUTTON_BOT_STATUS,
        TelegramUiState,
        handle_callback_query,
        handle_text,
    )

    provider_page = SimpleNamespace(
        entries=(build_provider_session("desktop-session", "Desktop Flow", cwd_label="src"),),
        next_cursor=None,
        supported=True,
    )
    ui_state = TelegramUiState()
    update = FakeUpdate(user_id=123, text=BUTTON_BOT_STATUS)
    services, _ = make_services(
        provider="codex",
        provider_session_pages={None: provider_page},
        activate_history_error=RuntimeError("boom"),
    )

    run(handle_text(update, None, services, ui_state))

    callback_message = FakeIncomingMessage("status")
    provider_button = find_inline_button(update.message.reply_markups[0], "Provider Sessions")
    provider_update = FakeCallbackUpdate(123, provider_button.callback_data, message=callback_message)
    run(handle_callback_query(provider_update, None, services, ui_state))

    run_button = find_inline_button(callback_message.edit_calls[-1][1], "Run 1")
    run_update = FakeCallbackUpdate(123, run_button.callback_data, message=callback_message)
    run(handle_callback_query(run_update, None, services, ui_state))

    final_text, final_markup = callback_message.edit_calls[-1]
    assert final_text.startswith(
        "Failed to switch provider session.\nProvider sessions for Codex in Default Workspace"
    )
    assert find_inline_button(final_markup, "Back to Bot Status")


def test_bot_status_history_provider_session_run_keeps_back_chain_to_status():
    from talk2agent.bots.telegram_bot import (
        BUTTON_BOT_STATUS,
        TelegramUiState,
        handle_callback_query,
        handle_text,
    )

    provider_page = SimpleNamespace(
        entries=(build_provider_session("desktop-session", "Desktop Flow", cwd_label="src"),),
        next_cursor=None,
        supported=True,
    )
    ui_state = TelegramUiState()
    update = FakeUpdate(user_id=123, text=BUTTON_BOT_STATUS)
    application = FakeApplication()
    services, store = make_services(
        provider="codex",
        session=FakeSession(available_commands=[FakeCommand("status", "Show status")]),
        history_entries=[build_history_entry("session-1", "First")],
        provider_session_pages={None: provider_page},
    )

    run(handle_text(update, None, services, ui_state))

    callback_message = FakeIncomingMessage("status")
    history_button = find_inline_button(update.message.reply_markups[0], "Session History")
    history_update = FakeCallbackUpdate(123, history_button.callback_data, message=callback_message)
    run(handle_callback_query(history_update, None, services, ui_state))

    provider_button = find_inline_button(callback_message.edit_calls[-1][1], "Provider Sessions")
    provider_update = FakeCallbackUpdate(123, provider_button.callback_data, message=callback_message)
    run(handle_callback_query(provider_update, None, services, ui_state))

    run_button = find_inline_button(callback_message.edit_calls[-1][1], "Run 1")
    run_update = FakeCallbackUpdate(123, run_button.callback_data, message=callback_message)
    run(handle_callback_query(run_update, make_context(application=application), services, ui_state))

    assert store.activate_provider_calls == [(123, "desktop-session", "Desktop Flow")]
    final_text, final_markup = callback_message.edit_calls[-1]
    assert final_text.startswith(
        "Switched to provider session desktop-session. "
        "Old bot buttons and pending inputs tied to the previous session were cleared."
    )
    back_to_history_button = find_inline_button(final_markup, "Back to History")

    back_to_history_update = FakeCallbackUpdate(
        123,
        back_to_history_button.callback_data,
        message=callback_message,
    )
    run(handle_callback_query(back_to_history_update, None, services, ui_state))

    history_text, history_markup = callback_message.edit_calls[-1]
    assert history_text.startswith("Session history for Codex in Default Workspace")
    back_to_status_button = find_inline_button(history_markup, "Back to Bot Status")

    back_to_status_update = FakeCallbackUpdate(
        123,
        back_to_status_button.callback_data,
        message=callback_message,
    )
    run(handle_callback_query(back_to_status_update, None, services, ui_state))

    status_text, status_markup = callback_message.edit_calls[-1]
    assert status_text.startswith("Bot status for Codex in Default Workspace")
    assert find_inline_button(status_markup, "Provider Sessions")


def test_bot_status_session_history_detail_shows_fields_and_actions():
    from talk2agent.acp.agent_session import PromptText
    from talk2agent.bots.telegram_bot import (
        BUTTON_BOT_STATUS,
        TelegramUiState,
        _ReplayTurn,
        handle_callback_query,
        handle_text,
    )

    ui_state = TelegramUiState()
    ui_state.set_last_turn(
        123,
        _ReplayTurn(
            provider="codex",
            workspace_id="default",
            prompt_items=(PromptText("hello"),),
            title_hint="hello",
        ),
    )
    update = FakeUpdate(user_id=123, text=BUTTON_BOT_STATUS)
    services, _ = make_services(
        provider="codex",
        history_entries=[build_history_entry("session-1", "First")],
    )

    run(handle_text(update, None, services, ui_state))

    callback_message = FakeIncomingMessage("status")
    history_button = find_inline_button(update.message.reply_markups[0], "Session History")
    history_update = FakeCallbackUpdate(123, history_button.callback_data, message=callback_message)
    run(handle_callback_query(history_update, None, services, ui_state))

    open_button = find_inline_button(callback_message.edit_calls[-1][1], "Open 1")
    open_update = FakeCallbackUpdate(123, open_button.callback_data, message=callback_message)
    run(handle_callback_query(open_update, None, services, ui_state))

    detail_text, detail_markup = callback_message.edit_calls[-1]
    assert detail_text.startswith("Session history entry for Codex in Default Workspace")
    assert "Title: First" in detail_text
    assert "Session: session-1" in detail_text
    assert "Current runtime session: no" in detail_text
    assert "Cwd: F:/workspace" in detail_text
    assert "Created: 2026-03-20T00:00:00+00:00" in detail_text
    assert "Updated: 2026-03-20T00:00:00+00:00" in detail_text
    assert find_inline_button(detail_markup, "Refresh")
    assert find_inline_button(detail_markup, "Back to History")
    assert find_inline_button(detail_markup, "Run Session")
    assert find_inline_button(detail_markup, "Run+Retry Session")
    assert find_inline_button(detail_markup, "Fork Session")
    assert find_inline_button(detail_markup, "Fork+Retry Session")

    back_to_history_button = find_inline_button(detail_markup, "Back to History")
    back_to_history_update = FakeCallbackUpdate(
        123,
        back_to_history_button.callback_data,
        message=callback_message,
    )
    run(handle_callback_query(back_to_history_update, None, services, ui_state))

    history_text, history_markup = callback_message.edit_calls[-1]
    assert history_text.startswith("Session history for Codex in Default Workspace")
    back_to_status_button = find_inline_button(history_markup, "Back to Bot Status")

    back_to_status_update = FakeCallbackUpdate(
        123,
        back_to_status_button.callback_data,
        message=callback_message,
    )
    run(handle_callback_query(back_to_status_update, None, services, ui_state))

    status_text, status_markup = callback_message.edit_calls[-1]
    assert status_text.startswith("Bot status for Codex in Default Workspace")
    assert find_inline_button(status_markup, "Session History")


def test_bot_status_session_history_run_returns_to_status():
    from talk2agent.bots.telegram_bot import (
        BUTTON_BOT_STATUS,
        TelegramUiState,
        handle_callback_query,
        handle_text,
    )

    ui_state = TelegramUiState()
    update = FakeUpdate(user_id=123, text=BUTTON_BOT_STATUS)
    application = FakeApplication()
    session = FakeSession(
        session_id="session-live",
        available_commands=[FakeCommand("model", "Switch model", hint="model id")],
    )
    services, store = make_services(
        provider="codex",
        session=session,
        history_entries=[build_history_entry("session-1", "First")],
    )

    run(handle_text(update, None, services, ui_state))

    callback_message = FakeIncomingMessage("status")
    history_button = find_inline_button(update.message.reply_markups[0], "Session History")
    history_update = FakeCallbackUpdate(123, history_button.callback_data, message=callback_message)
    run(handle_callback_query(history_update, None, services, ui_state))

    run_button = find_inline_button(callback_message.edit_calls[-1][1], "Run 1")
    run_update = FakeCallbackUpdate(123, run_button.callback_data, message=callback_message)
    run(handle_callback_query(run_update, make_context(application=application), services, ui_state))

    assert store.activate_history_calls == [(123, "session-1")]
    final_text, final_markup = callback_message.edit_calls[-1]
    assert final_text.startswith(
        "Switched to session session-1 on Codex in Default Workspace. "
        "Old bot buttons and pending inputs tied to the previous session were cleared.\n"
        "Bot status for Codex in Default Workspace"
    )
    assert "Session: session-1" in final_text
    assert find_inline_button(final_markup, "Session History")


def test_bot_status_session_history_run_retry_returns_to_status():
    from talk2agent.acp.agent_session import PromptText
    from talk2agent.bots.telegram_bot import (
        BUTTON_BOT_STATUS,
        TelegramUiState,
        _ReplayTurn,
        handle_callback_query,
        handle_text,
    )

    ui_state = TelegramUiState()
    ui_state.set_last_turn(
        123,
        _ReplayTurn(
            provider="codex",
            workspace_id="default",
            prompt_items=(PromptText("hello"),),
            title_hint="hello",
        ),
    )
    update = FakeUpdate(user_id=123, text=BUTTON_BOT_STATUS)
    callback_message = FakeIncomingMessage("status")
    session = FakeSession(
        session_id="session-live",
        available_commands=[FakeCommand("model", "Switch model", hint="model id")],
    )
    services, store = make_services(
        provider="codex",
        session=session,
        history_entries=[build_history_entry("session-1", "First")],
    )

    run(handle_text(update, None, services, ui_state))

    history_button = find_inline_button(update.message.reply_markups[0], "Session History")
    history_update = FakeCallbackUpdate(123, history_button.callback_data, message=callback_message)
    run(handle_callback_query(history_update, None, services, ui_state))

    retry_button = find_inline_button(callback_message.edit_calls[-1][1], "Run+Retry 1")
    retry_update = FakeCallbackUpdate(123, retry_button.callback_data, message=callback_message)
    run(handle_callback_query(retry_update, make_context(application=FakeApplication()), services, ui_state))

    assert store.activate_history_calls == [(123, "session-1")]
    assert session.prompt_items == [(PromptText("hello"),)]
    assert callback_message.reply_calls == ["hello world"]
    final_text, final_markup = callback_message.edit_calls[-1]
    assert final_text.startswith(
        "Switched to session session-1 on Codex in Default Workspace. "
        "Old bot buttons and pending inputs tied to the previous session were cleared.\n"
        "Retried last turn in this session.\n"
        "Bot status for Codex in Default Workspace"
    )
    assert "Session: session-1" in final_text
    assert find_inline_button(final_markup, "Session History")


def test_bot_status_session_history_fork_returns_to_status():
    from talk2agent.bots.telegram_bot import (
        BUTTON_BOT_STATUS,
        TelegramUiState,
        handle_callback_query,
        handle_text,
    )

    ui_state = TelegramUiState()
    update = FakeUpdate(user_id=123, text=BUTTON_BOT_STATUS)
    callback_message = FakeIncomingMessage("status")
    session = FakeSession(
        session_id="session-live",
        available_commands=[FakeCommand("model", "Switch model", hint="model id")],
    )
    services, store = make_services(
        provider="codex",
        session=session,
        history_entries=[build_history_entry("session-1", "First")],
    )

    run(handle_text(update, None, services, ui_state))

    history_button = find_inline_button(update.message.reply_markups[0], "Session History")
    history_update = FakeCallbackUpdate(123, history_button.callback_data, message=callback_message)
    run(handle_callback_query(history_update, None, services, ui_state))

    fork_button = find_inline_button(callback_message.edit_calls[-1][1], "Fork 1")
    fork_update = FakeCallbackUpdate(123, fork_button.callback_data, message=callback_message)
    run(handle_callback_query(fork_update, make_context(application=FakeApplication()), services, ui_state))

    assert store.fork_history_calls == [(123, "session-1")]
    assert store.record_session_usage_calls == [(123, "fork-session-1", None)]
    final_text, final_markup = callback_message.edit_calls[-1]
    assert final_text.startswith(
        "Forked session fork-session-1 from session-1 on Codex in Default Workspace. "
        "Old bot buttons and pending inputs tied to the previous session were cleared.\n"
        "Bot status for Codex in Default Workspace"
    )
    assert "Session: fork-session-1" in final_text
    assert find_inline_button(final_markup, "Session History")


def test_bot_status_session_history_fork_retry_returns_to_status():
    from talk2agent.acp.agent_session import PromptText
    from talk2agent.bots.telegram_bot import (
        BUTTON_BOT_STATUS,
        TelegramUiState,
        _ReplayTurn,
        handle_callback_query,
        handle_text,
    )

    ui_state = TelegramUiState()
    ui_state.set_last_turn(
        123,
        _ReplayTurn(
            provider="codex",
            workspace_id="default",
            prompt_items=(PromptText("hello"),),
            title_hint="hello",
        ),
    )
    update = FakeUpdate(user_id=123, text=BUTTON_BOT_STATUS)
    callback_message = FakeIncomingMessage("status")
    session = FakeSession(
        session_id="session-live",
        available_commands=[FakeCommand("model", "Switch model", hint="model id")],
    )
    services, store = make_services(
        provider="codex",
        session=session,
        history_entries=[build_history_entry("session-1", "First")],
    )

    run(handle_text(update, None, services, ui_state))

    history_button = find_inline_button(update.message.reply_markups[0], "Session History")
    history_update = FakeCallbackUpdate(123, history_button.callback_data, message=callback_message)
    run(handle_callback_query(history_update, None, services, ui_state))

    retry_button = find_inline_button(callback_message.edit_calls[-1][1], "Fork+Retry 1")
    retry_update = FakeCallbackUpdate(123, retry_button.callback_data, message=callback_message)
    run(handle_callback_query(retry_update, make_context(application=FakeApplication()), services, ui_state))

    assert store.fork_history_calls == [(123, "session-1")]
    assert session.prompt_items == [(PromptText("hello"),)]
    assert callback_message.reply_calls == ["hello world"]
    assert store.record_session_usage_calls == [
        (123, "fork-session-1", None),
        (123, "fork-session-1", "hello"),
    ]
    final_text, final_markup = callback_message.edit_calls[-1]
    assert final_text.startswith(
        "Forked session fork-session-1 from session-1 on Codex in Default Workspace. "
        "Old bot buttons and pending inputs tied to the previous session were cleared.\n"
        "Retried last turn in this session.\n"
        "Bot status for Codex in Default Workspace"
    )
    assert "Session: fork-session-1" in final_text
    assert find_inline_button(final_markup, "Session History")


def test_bot_status_session_history_run_failure_restores_history_with_back_to_status():
    from talk2agent.bots.telegram_bot import (
        BUTTON_BOT_STATUS,
        TelegramUiState,
        handle_callback_query,
        handle_text,
    )

    ui_state = TelegramUiState()
    update = FakeUpdate(user_id=123, text=BUTTON_BOT_STATUS)
    services, _ = make_services(
        provider="codex",
        history_entries=[build_history_entry("session-1", "First")],
        activate_history_error=RuntimeError("boom"),
    )

    run(handle_text(update, None, services, ui_state))

    callback_message = FakeIncomingMessage("status")
    history_button = find_inline_button(update.message.reply_markups[0], "Session History")
    history_update = FakeCallbackUpdate(123, history_button.callback_data, message=callback_message)
    run(handle_callback_query(history_update, None, services, ui_state))

    run_button = find_inline_button(callback_message.edit_calls[-1][1], "Run 1")
    run_update = FakeCallbackUpdate(123, run_button.callback_data, message=callback_message)
    run(handle_callback_query(run_update, None, services, ui_state))

    final_text, final_markup = callback_message.edit_calls[-1]
    assert final_text.startswith(
        "Failed to switch session.\nSession history for Codex in Default Workspace"
    )
    assert find_inline_button(final_markup, "Back to Bot Status")


def test_bot_status_can_cancel_pending_input():
    from talk2agent.bots.telegram_bot import (
        BUTTON_BOT_STATUS,
        TelegramUiState,
        handle_callback_query,
        handle_text,
    )

    ui_state = TelegramUiState()
    ui_state.set_pending_text_action(123, "workspace_search")
    update = FakeUpdate(user_id=123, text=BUTTON_BOT_STATUS)
    services, _ = make_services(provider="codex")

    run(handle_text(update, None, services, ui_state))

    cancel_button = find_inline_button(update.message.reply_markups[0], "Cancel Pending Input")
    callback_message = FakeIncomingMessage("status")
    cancel_update = FakeCallbackUpdate(123, cancel_button.callback_data, message=callback_message)
    run(handle_callback_query(cancel_update, None, services, ui_state))

    assert ui_state.get_pending_text_action(123) is None
    edited_text, edited_markup = callback_message.edit_calls[-1]
    assert edited_text.startswith("Pending input cancelled.\nBot status for Codex in Default Workspace")
    assert "Pending input: none" in edited_text
    labels = [button.text for row in edited_markup.inline_keyboard for button in row]
    assert "Cancel Pending Input" not in labels


def test_bot_status_can_start_and_stop_bundle_chat():
    from talk2agent.bots.telegram_bot import (
        BUTTON_BOT_STATUS,
        TelegramUiState,
        _ContextBundleItem,
        handle_callback_query,
        handle_text,
    )

    ui_state = TelegramUiState()
    ui_state.add_context_item(
        123,
        "codex",
        "default",
        _ContextBundleItem(kind="file", relative_path="notes.txt"),
    )
    update = FakeUpdate(user_id=123, text=BUTTON_BOT_STATUS)
    services, _ = make_services(provider="codex")

    run(handle_text(update, None, services, ui_state))

    start_button = find_inline_button(update.message.reply_markups[0], "Start Bundle Chat")
    callback_message = FakeIncomingMessage("status")
    start_update = FakeCallbackUpdate(123, start_button.callback_data, message=callback_message)
    run(handle_callback_query(start_update, None, services, ui_state))

    assert ui_state.context_bundle_chat_active(123, "codex", "default") is True
    started_text, started_markup = callback_message.edit_calls[-1]
    assert started_text.startswith("Bundle chat enabled.\nBot status for Codex in Default Workspace")
    assert "Bundle chat: on" in started_text
    stop_button = find_inline_button(started_markup, "Stop Bundle Chat")

    stop_update = FakeCallbackUpdate(123, stop_button.callback_data, message=callback_message)
    run(handle_callback_query(stop_update, None, services, ui_state))

    assert ui_state.context_bundle_chat_active(123, "codex", "default") is False
    stopped_text, stopped_markup = callback_message.edit_calls[-1]
    assert stopped_text.startswith("Bundle chat disabled.\nBot status for Codex in Default Workspace")
    assert "Bundle chat: off" in stopped_text
    assert find_inline_button(stopped_markup, "Start Bundle Chat")


def test_bot_status_new_session_control_refreshes_status_inline():
    from talk2agent.bots.telegram_bot import (
        BUTTON_BOT_STATUS,
        TelegramUiState,
        handle_callback_query,
        handle_text,
    )

    ui_state = TelegramUiState()
    ui_state.set_pending_text_action(123, "workspace_search")
    update = FakeUpdate(user_id=123, text=BUTTON_BOT_STATUS)
    application = FakeApplication()
    session = FakeSession(available_commands=[FakeCommand("status", "Show status")])
    services, store = make_services(provider="codex", session=session)

    run(handle_text(update, None, services, ui_state))

    callback_message = FakeIncomingMessage("status")
    new_session_button = find_inline_button(update.message.reply_markups[0], "New Session")
    new_session_update = FakeCallbackUpdate(123, new_session_button.callback_data, message=callback_message)
    run(handle_callback_query(new_session_update, make_context(application=application), services, ui_state))

    assert store.reset_calls == [123]
    assert ui_state.get_pending_text_action(123) is None
    assert callback_message.reply_calls == []
    final_text, final_markup = callback_message.edit_calls[-1]
    assert final_text.startswith(
        "Started new session: session-123\n"
        "Old bot buttons and pending inputs tied to the previous session were cleared.\n"
        "Bot status for Codex in Default Workspace"
    )
    assert "Session: session-123" in final_text
    assert find_inline_button(final_markup, "New Session")
    assert [command.command for command in application.bot.set_my_commands_calls[0][0]] == ["status"]


def test_bot_status_restart_agent_control_refreshes_status_inline():
    from talk2agent.bots.telegram_bot import (
        BUTTON_BOT_STATUS,
        TelegramUiState,
        handle_callback_query,
        handle_text,
    )

    ui_state = TelegramUiState()
    ui_state.set_pending_text_action(123, "workspace_search")
    update = FakeUpdate(user_id=123, text=BUTTON_BOT_STATUS)
    application = FakeApplication()
    session = FakeSession(available_commands=[FakeCommand("status", "Show status")])
    services, store = make_services(provider="codex", session=session)

    run(handle_text(update, None, services, ui_state))

    callback_message = FakeIncomingMessage("status")
    restart_button = find_inline_button(update.message.reply_markups[0], "Restart Agent")
    restart_update = FakeCallbackUpdate(123, restart_button.callback_data, message=callback_message)
    run(handle_callback_query(restart_update, make_context(application=application), services, ui_state))

    assert store.restart_calls == [123]
    assert ui_state.get_pending_text_action(123) is None
    assert callback_message.reply_calls == []
    final_text, final_markup = callback_message.edit_calls[-1]
    assert final_text.startswith(
        "Restarted agent: session-123\n"
        "Old bot buttons and pending inputs tied to the previous session were cleared.\n"
        "Bot status for Codex in Default Workspace"
    )
    assert "Session: session-123" in final_text
    assert find_inline_button(final_markup, "Restart Agent")
    assert [command.command for command in application.bot.set_my_commands_calls[0][0]] == ["status"]


def test_bot_status_fork_session_control_refreshes_status_inline():
    from talk2agent.bots.telegram_bot import (
        BUTTON_BOT_STATUS,
        TelegramUiState,
        handle_callback_query,
        handle_text,
    )

    ui_state = TelegramUiState()
    ui_state.set_pending_text_action(123, "workspace_search")
    update = FakeUpdate(user_id=123, text=BUTTON_BOT_STATUS)
    application = FakeApplication()
    session = FakeSession(
        session_id="session-123",
        session_title="Active Thread",
        available_commands=[FakeCommand("status", "Show status")],
    )
    services, store = make_services(provider="codex", session=session)

    run(handle_text(update, None, services, ui_state))

    callback_message = FakeIncomingMessage("status")
    fork_button = find_inline_button(update.message.reply_markups[0], "Fork Session")
    fork_update = FakeCallbackUpdate(123, fork_button.callback_data, message=callback_message)
    run(handle_callback_query(fork_update, make_context(application=application), services, ui_state))

    assert store.fork_live_calls == [123]
    assert ui_state.get_pending_text_action(123) is None
    final_text, final_markup = callback_message.edit_calls[-1]
    assert final_text.startswith(
        "Forked session: fork-session-123\n"
        "Old bot buttons and pending inputs tied to the previous session were cleared.\n"
        "Bot status for Codex in Default Workspace"
    )
    assert "Session: fork-session-123" in final_text
    assert find_inline_button(final_markup, "Fork Session")
    assert [command.command for command in application.bot.set_my_commands_calls[0][0]] == ["status"]


def test_bot_status_hides_fork_session_control_when_provider_cannot_fork():
    from talk2agent.bots.telegram_bot import BUTTON_BOT_STATUS, TelegramUiState, handle_text

    session = FakeSession(session_id="session-123")
    session.capabilities.can_fork = False
    update = FakeUpdate(user_id=123, text=BUTTON_BOT_STATUS)
    services, _ = make_services(provider="codex", session=session)

    run(handle_text(update, None, services, TelegramUiState()))

    labels = [button.text for row in update.message.reply_markups[0].inline_keyboard for button in row]
    assert "Fork Session" not in labels


def test_bot_status_fork_session_failure_restores_status():
    from talk2agent.bots.telegram_bot import (
        BUTTON_BOT_STATUS,
        TelegramUiState,
        handle_callback_query,
        handle_text,
    )

    ui_state = TelegramUiState()
    update = FakeUpdate(user_id=123, text=BUTTON_BOT_STATUS)
    callback_message = FakeIncomingMessage("status")
    services, store = make_services(
        provider="codex",
        session=FakeSession(session_id="session-123"),
        fork_live_error=RuntimeError("boom"),
    )

    run(handle_text(update, None, services, ui_state))

    fork_button = find_inline_button(update.message.reply_markups[0], "Fork Session")
    callback_update = FakeCallbackUpdate(123, fork_button.callback_data, message=callback_message)
    run(handle_callback_query(callback_update, None, services, ui_state))

    assert store.fork_live_calls == [123]
    final_text, final_markup = callback_message.edit_calls[-1]
    assert final_text.startswith("Failed to fork session.\nBot status for Codex in Default Workspace")
    assert find_inline_button(final_markup, "Fork Session")


def test_bot_status_model_mode_control_clears_pending_and_starts_session():
    from talk2agent.bots.telegram_bot import (
        BUTTON_BOT_STATUS,
        TelegramUiState,
        handle_callback_query,
        handle_text,
    )

    ui_state = TelegramUiState()
    ui_state.set_pending_text_action(123, "workspace_search")
    update = FakeUpdate(user_id=123, text=BUTTON_BOT_STATUS)
    application = FakeApplication()
    session = FakeSession(available_commands=[FakeCommand("status", "Show status")])
    services, store = make_services(session=session, peek_session=None)

    run(handle_text(update, None, services, ui_state))

    model_mode_button = find_inline_button(update.message.reply_markups[0], "Model / Mode")
    callback_message = FakeIncomingMessage("status")
    callback_update = FakeCallbackUpdate(123, model_mode_button.callback_data, message=callback_message)
    run(handle_callback_query(callback_update, make_context(application=application), services, ui_state))

    assert ui_state.get_pending_text_action(123) is None
    assert store.get_or_create_calls == [123]
    edited_text, edited_markup = callback_message.edit_calls[-1]
    assert edited_text.startswith(
        "Started session for model / mode controls.\nSession: session-123"
    )
    assert find_inline_button(edited_markup, "Back to Bot Status")
    assert [command.command for command in application.bot.set_my_commands_calls[0][0]] == ["status"]


def test_bot_status_model_mode_control_creation_failure_restores_status():
    from talk2agent.bots.telegram_bot import (
        BUTTON_BOT_STATUS,
        TelegramUiState,
        handle_callback_query,
        handle_text,
    )

    ui_state = TelegramUiState()
    update = FakeUpdate(user_id=123, text=BUTTON_BOT_STATUS)
    callback_message = FakeIncomingMessage("status")
    services, store = make_services(
        provider="codex",
        peek_session=None,
        get_or_create_error=RuntimeError("boom"),
    )

    run(handle_text(update, None, services, ui_state))

    model_mode_button = find_inline_button(update.message.reply_markups[0], "Model / Mode")
    callback_update = FakeCallbackUpdate(123, model_mode_button.callback_data, message=callback_message)
    run(handle_callback_query(callback_update, None, services, ui_state))

    assert store.get_or_create_calls == [123]
    final_text, final_markup = callback_message.edit_calls[-1]
    assert final_text.startswith("session creation failed\nBot status for Codex in Default Workspace")
    assert find_inline_button(final_markup, "Model / Mode")


def test_bot_status_model_mode_selection_keeps_back_to_status():
    from talk2agent.bots.telegram_bot import (
        BUTTON_BOT_STATUS,
        TelegramUiState,
        handle_callback_query,
        handle_text,
    )

    ui_state = TelegramUiState()
    update = FakeUpdate(user_id=123, text=BUTTON_BOT_STATUS)
    services, store = make_services(provider="codex")

    run(handle_text(update, None, services, ui_state))

    callback_message = FakeIncomingMessage("status")
    model_mode_button = find_inline_button(update.message.reply_markups[0], "Model / Mode")
    model_mode_update = FakeCallbackUpdate(123, model_mode_button.callback_data, message=callback_message)
    run(handle_callback_query(model_mode_update, None, services, ui_state))

    select_button = find_inline_button(callback_message.edit_calls[-1][1], "Model: GPT-5.4 Mini")
    select_update = FakeCallbackUpdate(123, select_button.callback_data, message=callback_message)
    run(handle_callback_query(select_update, None, services, ui_state))

    assert services.final_session.set_selection_calls == [("model", "gpt-5.4-mini")]
    assert store.record_session_usage_calls == [(123, "session-123", None)]
    final_text, final_markup = callback_message.edit_calls[-1]
    assert final_text.startswith("Updated model to GPT-5.4 Mini.\nSession: session-123")
    assert find_inline_button(final_markup, "Back to Bot Status")

    back_update = FakeCallbackUpdate(
        123,
        find_inline_button(final_markup, "Back to Bot Status").callback_data,
        message=callback_message,
    )
    run(handle_callback_query(back_update, None, services, ui_state))

    status_text, status_markup = callback_message.edit_calls[-1]
    assert status_text.startswith("Bot status for Codex in Default Workspace")
    assert find_inline_button(status_markup, "Model / Mode")


def test_bot_status_model_mode_choice_detail_keeps_back_to_status():
    from talk2agent.bots.telegram_bot import (
        BUTTON_BOT_STATUS,
        TelegramUiState,
        handle_callback_query,
        handle_text,
    )

    ui_state = TelegramUiState()
    update = FakeUpdate(user_id=123, text=BUTTON_BOT_STATUS)
    services, _ = make_services(provider="codex")
    services.final_session.selections["mode"].choices[1].description = "Lower effort mode for faster iterations."

    run(handle_text(update, None, services, ui_state))

    callback_message = FakeIncomingMessage("status")
    model_mode_button = find_inline_button(update.message.reply_markups[0], "Model / Mode")
    model_mode_update = FakeCallbackUpdate(123, model_mode_button.callback_data, message=callback_message)
    run(handle_callback_query(model_mode_update, None, services, ui_state))

    open_button = find_inline_button(callback_message.edit_calls[-1][1], "Open Mode 2")
    open_update = FakeCallbackUpdate(123, open_button.callback_data, message=callback_message)
    run(handle_callback_query(open_update, None, services, ui_state))

    detail_text, detail_markup = callback_message.edit_calls[-1]
    assert detail_text.startswith("Mode choice for Codex in Default Workspace")
    assert "Session: session-123" in detail_text
    assert "Choice: 2/2" in detail_text
    assert "Current selection: xhigh" in detail_text
    assert "This choice is current: no" in detail_text
    assert "Label: low" in detail_text
    assert "Value: low" in detail_text
    assert "Description:" in detail_text
    assert "Lower effort mode for faster iterations." in detail_text
    assert find_inline_button(detail_markup, "Use Mode")

    back_button = find_inline_button(detail_markup, "Back to Model / Mode")
    back_update = FakeCallbackUpdate(123, back_button.callback_data, message=callback_message)
    run(handle_callback_query(back_update, None, services, ui_state))

    selection_text, selection_markup = callback_message.edit_calls[-1]
    assert selection_text.startswith("Session: session-123")
    assert find_inline_button(selection_markup, "Back to Bot Status")

    back_to_status_update = FakeCallbackUpdate(
        123,
        find_inline_button(selection_markup, "Back to Bot Status").callback_data,
        message=callback_message,
    )
    run(handle_callback_query(back_to_status_update, None, services, ui_state))

    status_text, status_markup = callback_message.edit_calls[-1]
    assert status_text.startswith("Bot status for Codex in Default Workspace")
    assert find_inline_button(status_markup, "Model / Mode")


def test_bot_status_model_mode_choice_detail_creation_failure_restores_status():
    from talk2agent.bots.telegram_bot import (
        BUTTON_BOT_STATUS,
        TelegramUiState,
        handle_callback_query,
        handle_text,
    )

    ui_state = TelegramUiState()
    update = FakeUpdate(user_id=123, text=BUTTON_BOT_STATUS)
    services, store = make_services(provider="codex")

    run(handle_text(update, None, services, ui_state))

    callback_message = FakeIncomingMessage("status")
    model_mode_button = find_inline_button(update.message.reply_markups[0], "Model / Mode")
    model_mode_update = FakeCallbackUpdate(123, model_mode_button.callback_data, message=callback_message)
    run(handle_callback_query(model_mode_update, None, services, ui_state))

    store.peek_session = None
    store.get_or_create_error = RuntimeError("boom")

    open_button = find_inline_button(callback_message.edit_calls[-1][1], "Open Model 2")
    open_update = FakeCallbackUpdate(123, open_button.callback_data, message=callback_message)
    run(handle_callback_query(open_update, None, services, ui_state))

    final_text, final_markup = callback_message.edit_calls[-1]
    assert final_text.startswith("session creation failed\nBot status for Codex in Default Workspace")
    assert find_inline_button(final_markup, "Model / Mode")


def test_bot_status_model_mode_retry_returns_to_status():
    from talk2agent.acp.agent_session import PromptText
    from talk2agent.bots.telegram_bot import (
        BUTTON_BOT_STATUS,
        TelegramUiState,
        _ReplayTurn,
        handle_callback_query,
        handle_text,
    )

    ui_state = TelegramUiState()
    ui_state.set_last_turn(
        123,
        _ReplayTurn(
            provider="codex",
            workspace_id="default",
            prompt_items=(PromptText("hello"),),
            title_hint="hello",
        ),
    )
    update = FakeUpdate(user_id=123, text=BUTTON_BOT_STATUS)
    callback_message = FakeIncomingMessage("status")
    services, store = make_services(provider="codex")

    run(handle_text(update, None, services, ui_state))

    model_mode_button = find_inline_button(update.message.reply_markups[0], "Model / Mode")
    model_mode_update = FakeCallbackUpdate(123, model_mode_button.callback_data, message=callback_message)
    run(handle_callback_query(model_mode_update, make_context(application=FakeApplication()), services, ui_state))

    retry_button = find_inline_button(callback_message.edit_calls[-1][1], "Model+Retry: GPT-5.4 Mini")
    retry_update = FakeCallbackUpdate(123, retry_button.callback_data, message=callback_message)
    run(handle_callback_query(retry_update, make_context(application=FakeApplication()), services, ui_state))

    assert services.final_session.set_selection_calls == [("model", "gpt-5.4-mini")]
    assert services.final_session.prompt_items == [(PromptText("hello"),)]
    assert callback_message.reply_calls == ["hello world"]
    assert store.record_session_usage_calls == [
        (123, "session-123", None),
        (123, "session-123", "hello"),
    ]
    final_text, final_markup = callback_message.edit_calls[-1]
    assert final_text.startswith(
        "Updated model to GPT-5.4 Mini.\n"
        "Retried last turn with the updated setting.\n"
        "Bot status for Codex in Default Workspace"
    )
    assert "Session: session-123" in final_text
    assert find_inline_button(final_markup, "Model / Mode")


def test_bot_status_retry_last_turn_control_returns_to_status():
    from talk2agent.acp.agent_session import PromptText
    from talk2agent.bots.telegram_bot import (
        BUTTON_BOT_STATUS,
        TelegramUiState,
        _ReplayTurn,
        handle_callback_query,
        handle_text,
    )

    ui_state = TelegramUiState()
    ui_state.set_pending_text_action(123, "workspace_search")
    ui_state.set_last_turn(
        123,
        _ReplayTurn(
            provider="codex",
            workspace_id="default",
            prompt_items=(PromptText("hello"),),
            title_hint="hello",
        ),
    )
    update = FakeUpdate(user_id=123, text=BUTTON_BOT_STATUS)
    services, store = make_services(provider="codex")

    run(handle_text(update, None, services, ui_state))

    retry_button = find_inline_button(update.message.reply_markups[0], "Retry Last Turn")
    callback_message = FakeIncomingMessage("status")
    callback_update = FakeCallbackUpdate(123, retry_button.callback_data, message=callback_message)
    run(handle_callback_query(callback_update, None, services, ui_state))

    assert ui_state.get_pending_text_action(123) is None
    assert services.final_session.prompt_items == [(PromptText("hello"),)]
    assert callback_message.reply_calls == ["hello world"]
    assert store.record_session_usage_calls == [(123, "session-123", "hello")]
    final_text, final_markup = callback_message.edit_calls[-1]
    assert final_text.startswith(
        "Retried last turn.\nBot status for Codex in Default Workspace"
    )
    assert "Pending input: none" in final_text
    assert find_inline_button(final_markup, "Retry Last Turn")


def test_bot_status_retry_last_turn_failure_restores_status():
    from talk2agent.acp.agent_session import PromptText
    from talk2agent.bots.telegram_bot import (
        BUTTON_BOT_STATUS,
        TelegramUiState,
        _ReplayTurn,
        handle_callback_query,
        handle_text,
    )

    ui_state = TelegramUiState()
    ui_state.set_last_turn(
        123,
        _ReplayTurn(
            provider="codex",
            workspace_id="default",
            prompt_items=(PromptText("hello"),),
            title_hint="hello",
        ),
    )
    update = FakeUpdate(user_id=123, text=BUTTON_BOT_STATUS)
    callback_message = FakeIncomingMessage("status")
    services, store = make_services(provider="codex", get_or_create_error=RuntimeError("boom"))

    run(handle_text(update, None, services, ui_state))

    retry_button = find_inline_button(update.message.reply_markups[0], "Retry Last Turn")
    retry_update = FakeCallbackUpdate(123, retry_button.callback_data, message=callback_message)
    run(handle_callback_query(retry_update, None, services, ui_state))

    assert store.get_or_create_calls == [123]
    assert callback_message.reply_calls == []
    final_text, final_markup = callback_message.edit_calls[-1]
    assert final_text.startswith("Request failed.\nBot status for Codex in Default Workspace")
    assert find_inline_button(final_markup, "Retry Last Turn")


def test_bot_status_retry_last_turn_runtime_failure_restores_status():
    from talk2agent.acp.agent_session import PromptText
    from talk2agent.bots.telegram_bot import (
        BUTTON_BOT_STATUS,
        TelegramUiState,
        _ReplayTurn,
        handle_callback_query,
        handle_text,
    )

    ui_state = TelegramUiState()
    ui_state.set_last_turn(
        123,
        _ReplayTurn(
            provider="codex",
            workspace_id="default",
            prompt_items=(PromptText("hello"),),
            title_hint="hello",
        ),
    )
    update = FakeUpdate(user_id=123, text=BUTTON_BOT_STATUS)
    callback_message = FakeIncomingMessage("status")
    session = FakeSession(error=RuntimeError("boom"), raise_before_stream=True)
    session.prompts.append("already-started")
    services, store = make_services(provider="codex", session=session)

    run(handle_text(update, None, services, ui_state))

    retry_button = find_inline_button(update.message.reply_markups[0], "Retry Last Turn")
    retry_update = FakeCallbackUpdate(123, retry_button.callback_data, message=callback_message)
    run(handle_callback_query(retry_update, None, services, ui_state))

    assert store.invalidate_calls == [(123, session)]
    assert callback_message.reply_calls[0].startswith(
        "Request failed. The current live session for Codex in Default Workspace was closed."
    )
    final_text, final_markup = callback_message.edit_calls[-1]
    assert final_text.startswith("Request failed.\nBot status for Codex in Default Workspace")
    assert find_inline_button(final_markup, "Retry Last Turn")


def test_bot_status_fork_last_turn_control_returns_to_status():
    from talk2agent.acp.agent_session import PromptText
    from talk2agent.bots.telegram_bot import (
        BUTTON_BOT_STATUS,
        TelegramUiState,
        _ReplayTurn,
        handle_callback_query,
        handle_text,
    )

    ui_state = TelegramUiState()
    ui_state.set_pending_text_action(123, "workspace_search")
    ui_state.set_last_turn(
        123,
        _ReplayTurn(
            provider="codex",
            workspace_id="default",
            prompt_items=(PromptText("hello"),),
            title_hint="hello",
        ),
    )
    update = FakeUpdate(user_id=123, text=BUTTON_BOT_STATUS)
    callback_message = FakeIncomingMessage("status")
    application = FakeApplication()
    session = FakeSession(
        available_commands=[
            FakeCommand("status", "Show status"),
            FakeCommand("model", "Switch model", hint="model id"),
        ]
    )
    services, store = make_services(provider="codex", session=session)

    run(handle_text(update, None, services, ui_state))

    fork_button = find_inline_button(update.message.reply_markups[0], "Fork Last Turn")
    fork_update = FakeCallbackUpdate(123, fork_button.callback_data, message=callback_message)
    run(handle_callback_query(fork_update, make_context(application=application), services, ui_state))

    assert store.reset_calls == [123]
    assert ui_state.get_pending_text_action(123) is None
    assert services.final_session.prompt_items == [(PromptText("hello"),)]
    assert callback_message.reply_calls == ["hello world"]
    assert store.record_session_usage_calls == [(123, "session-123", "hello")]
    final_text, final_markup = callback_message.edit_calls[-1]
    assert final_text.startswith(
        "Forked last turn into a new session.\nBot status for Codex in Default Workspace"
    )
    assert "Session: session-123" in final_text
    assert find_inline_button(final_markup, "Fork Last Turn")
    assert [command.command for command in application.bot.set_my_commands_calls[0][0]] == ["status", "model"]


def test_bot_status_fork_last_turn_creation_failure_restores_status():
    from talk2agent.acp.agent_session import PromptText
    from talk2agent.bots.telegram_bot import (
        BUTTON_BOT_STATUS,
        TelegramUiState,
        _ReplayTurn,
        handle_callback_query,
        handle_text,
    )

    ui_state = TelegramUiState()
    ui_state.set_last_turn(
        123,
        _ReplayTurn(
            provider="codex",
            workspace_id="default",
            prompt_items=(PromptText("hello"),),
            title_hint="hello",
        ),
    )
    update = FakeUpdate(user_id=123, text=BUTTON_BOT_STATUS)
    callback_message = FakeIncomingMessage("status")
    services, store = make_services(provider="codex", reset_error=RuntimeError("boom"))

    run(handle_text(update, None, services, ui_state))

    fork_button = find_inline_button(update.message.reply_markups[0], "Fork Last Turn")
    fork_update = FakeCallbackUpdate(123, fork_button.callback_data, message=callback_message)
    run(handle_callback_query(fork_update, None, services, ui_state))

    assert store.reset_calls == [123]
    assert callback_message.reply_calls == []
    final_text, final_markup = callback_message.edit_calls[-1]
    assert final_text.startswith("session creation failed\nBot status for Codex in Default Workspace")
    assert find_inline_button(final_markup, "Fork Last Turn")


def test_bot_status_fork_last_turn_runtime_failure_restores_status():
    from talk2agent.acp.agent_session import PromptText
    from talk2agent.bots.telegram_bot import (
        BUTTON_BOT_STATUS,
        TelegramUiState,
        _ReplayTurn,
        handle_callback_query,
        handle_text,
    )

    ui_state = TelegramUiState()
    ui_state.set_last_turn(
        123,
        _ReplayTurn(
            provider="codex",
            workspace_id="default",
            prompt_items=(PromptText("hello"),),
            title_hint="hello",
        ),
    )
    update = FakeUpdate(user_id=123, text=BUTTON_BOT_STATUS)
    callback_message = FakeIncomingMessage("status")
    session = FakeSession(error=RuntimeError("boom"), raise_before_stream=True)
    session.prompts.append("already-started")
    services, store = make_services(provider="codex", session=session)

    run(handle_text(update, None, services, ui_state))

    fork_button = find_inline_button(update.message.reply_markups[0], "Fork Last Turn")
    fork_update = FakeCallbackUpdate(123, fork_button.callback_data, message=callback_message)
    run(handle_callback_query(fork_update, None, services, ui_state))

    assert store.reset_calls == [123]
    assert store.invalidate_calls == [(123, session)]
    assert callback_message.reply_calls[0].startswith(
        "Request failed. The current live session for Codex in Default Workspace was closed."
    )
    final_text, final_markup = callback_message.edit_calls[-1]
    assert final_text.startswith("Request failed.\nBot status for Codex in Default Workspace")
    assert find_inline_button(final_markup, "Fork Last Turn")


def test_bot_status_switch_agent_retry_returns_to_status():
    from talk2agent.acp.agent_session import PromptText
    from talk2agent.bots.telegram_bot import (
        BUTTON_BOT_STATUS,
        TelegramUiState,
        _ReplayTurn,
        handle_callback_query,
        handle_text,
    )

    ui_state = TelegramUiState()
    ui_state.set_last_turn(
        123,
        _ReplayTurn(
            provider="codex",
            workspace_id="default",
            prompt_items=(PromptText("hello"),),
            title_hint="hello",
        ),
    )
    update = FakeUpdate(user_id=123, text=BUTTON_BOT_STATUS)
    callback_message = FakeIncomingMessage("status")
    services, store = make_services(provider="codex", admin_user_id=123)

    run(handle_text(update, None, services, ui_state))

    switch_button = find_inline_button(update.message.reply_markups[0], "Switch Agent")
    switch_update = FakeCallbackUpdate(123, switch_button.callback_data, message=callback_message)
    run(handle_callback_query(switch_update, None, services, ui_state))

    retry_button = find_inline_button(callback_message.edit_calls[-1][1], "Retry on Gemini CLI")
    retry_update = FakeCallbackUpdate(123, retry_button.callback_data, message=callback_message)
    run(handle_callback_query(retry_update, make_context(application=FakeApplication()), services, ui_state))

    assert services.switch_provider_calls == ["gemini"]
    assert services.final_session.prompt_items == [(PromptText("hello"),)]
    assert callback_message.reply_calls == ["hello world"]
    assert store.record_session_usage_calls == [(123, "session-123", "hello")]
    final_text, final_markup = callback_message.edit_calls[-1]
    assert final_text.startswith(
        "Switched agent to Gemini CLI in Default Workspace. "
        "Old bot buttons and pending inputs were cleared.\n"
        "Retried last turn on the new agent.\n"
        "Bot status for Gemini CLI in Default Workspace"
    )
    assert find_inline_button(final_markup, "Switch Agent")


def test_bot_status_switch_agent_fork_returns_to_status():
    from talk2agent.acp.agent_session import PromptText
    from talk2agent.bots.telegram_bot import (
        BUTTON_BOT_STATUS,
        TelegramUiState,
        _ReplayTurn,
        handle_callback_query,
        handle_text,
    )

    ui_state = TelegramUiState()
    ui_state.set_last_turn(
        123,
        _ReplayTurn(
            provider="codex",
            workspace_id="default",
            prompt_items=(PromptText("hello"),),
            title_hint="hello",
        ),
    )
    update = FakeUpdate(user_id=123, text=BUTTON_BOT_STATUS)
    callback_message = FakeIncomingMessage("status")
    services, store = make_services(provider="codex", admin_user_id=123)

    run(handle_text(update, None, services, ui_state))

    switch_button = find_inline_button(update.message.reply_markups[0], "Switch Agent")
    switch_update = FakeCallbackUpdate(123, switch_button.callback_data, message=callback_message)
    run(handle_callback_query(switch_update, None, services, ui_state))

    fork_button = find_inline_button(callback_message.edit_calls[-1][1], "Fork on Gemini CLI")
    fork_update = FakeCallbackUpdate(123, fork_button.callback_data, message=callback_message)
    run(handle_callback_query(fork_update, make_context(application=FakeApplication()), services, ui_state))

    assert services.switch_provider_calls == ["gemini"]
    assert store.reset_calls == [123]
    assert services.final_session.prompt_items == [(PromptText("hello"),)]
    assert callback_message.reply_calls == ["hello world"]
    assert store.record_session_usage_calls == [(123, "session-123", "hello")]
    final_text, final_markup = callback_message.edit_calls[-1]
    assert final_text.startswith(
        "Switched agent to Gemini CLI in Default Workspace. "
        "Old bot buttons and pending inputs were cleared.\n"
        "Forked last turn on the new agent.\n"
        "Bot status for Gemini CLI in Default Workspace"
    )
    assert find_inline_button(final_markup, "Switch Agent")


def test_bot_status_switch_agent_retry_failure_restores_status():
    from talk2agent.acp.agent_session import PromptText
    from talk2agent.bots.telegram_bot import (
        BUTTON_BOT_STATUS,
        TelegramUiState,
        _ReplayTurn,
        handle_callback_query,
        handle_text,
    )

    ui_state = TelegramUiState()
    ui_state.set_last_turn(
        123,
        _ReplayTurn(
            provider="codex",
            workspace_id="default",
            prompt_items=(PromptText("hello"),),
            title_hint="hello",
        ),
    )
    update = FakeUpdate(user_id=123, text=BUTTON_BOT_STATUS)
    callback_message = FakeIncomingMessage("status")
    services, store = make_services(
        provider="codex",
        admin_user_id=123,
        get_or_create_error=RuntimeError("boom"),
    )

    run(handle_text(update, None, services, ui_state))

    switch_button = find_inline_button(update.message.reply_markups[0], "Switch Agent")
    switch_update = FakeCallbackUpdate(123, switch_button.callback_data, message=callback_message)
    run(handle_callback_query(switch_update, None, services, ui_state))

    retry_button = find_inline_button(callback_message.edit_calls[-1][1], "Retry on Gemini CLI")
    retry_update = FakeCallbackUpdate(123, retry_button.callback_data, message=callback_message)
    run(handle_callback_query(retry_update, None, services, ui_state))

    assert services.switch_provider_calls == ["gemini"]
    assert store.get_or_create_calls == [123]
    assert callback_message.reply_calls == []
    final_text, final_markup = callback_message.edit_calls[-1]
    assert final_text.startswith(
        "Switched agent to Gemini CLI in Default Workspace. "
        "Old bot buttons and pending inputs were cleared.\n"
        "Request failed.\n"
        "Bot status for Gemini CLI in Default Workspace"
    )
    assert find_inline_button(final_markup, "Switch Agent")


def test_bot_status_switch_agent_fork_creation_failure_restores_status():
    from talk2agent.acp.agent_session import PromptText
    from talk2agent.bots.telegram_bot import (
        BUTTON_BOT_STATUS,
        TelegramUiState,
        _ReplayTurn,
        handle_callback_query,
        handle_text,
    )

    ui_state = TelegramUiState()
    ui_state.set_last_turn(
        123,
        _ReplayTurn(
            provider="codex",
            workspace_id="default",
            prompt_items=(PromptText("hello"),),
            title_hint="hello",
        ),
    )
    update = FakeUpdate(user_id=123, text=BUTTON_BOT_STATUS)
    callback_message = FakeIncomingMessage("status")
    services, store = make_services(
        provider="codex",
        admin_user_id=123,
        reset_error=RuntimeError("boom"),
    )

    run(handle_text(update, None, services, ui_state))

    switch_button = find_inline_button(update.message.reply_markups[0], "Switch Agent")
    switch_update = FakeCallbackUpdate(123, switch_button.callback_data, message=callback_message)
    run(handle_callback_query(switch_update, None, services, ui_state))

    fork_button = find_inline_button(callback_message.edit_calls[-1][1], "Fork on Gemini CLI")
    fork_update = FakeCallbackUpdate(123, fork_button.callback_data, message=callback_message)
    run(handle_callback_query(fork_update, None, services, ui_state))

    assert services.switch_provider_calls == ["gemini"]
    assert store.reset_calls == [123]
    assert callback_message.reply_calls == []
    final_text, final_markup = callback_message.edit_calls[-1]
    assert final_text.startswith(
        "Switched agent to Gemini CLI in Default Workspace. "
        "Old bot buttons and pending inputs were cleared.\n"
        "session creation failed\n"
        "Bot status for Gemini CLI in Default Workspace"
    )
    assert find_inline_button(final_markup, "Switch Agent")


def test_bot_status_can_start_workspace_search_and_cancel_back_to_status():
    from talk2agent.bots.telegram_bot import (
        BUTTON_BOT_STATUS,
        TelegramUiState,
        handle_callback_query,
        handle_text,
    )

    ui_state = TelegramUiState()
    update = FakeUpdate(user_id=123, text=BUTTON_BOT_STATUS)
    services, _ = make_services(provider="codex")

    run(handle_text(update, None, services, ui_state))

    search_button = find_inline_button(update.message.reply_markups[0], "Workspace Search")
    callback_message = FakeIncomingMessage("status")
    search_update = FakeCallbackUpdate(123, search_button.callback_data, message=callback_message)
    run(handle_callback_query(search_update, None, services, ui_state))

    pending = ui_state.get_pending_text_action(123)
    assert pending is not None
    assert pending.action == "workspace_search"

    prompt_text, prompt_markup = callback_message.edit_calls[-1]
    assert prompt_text.startswith("Send your workspace search query as the next plain text message.")

    cancel_button = find_inline_button(prompt_markup, "Cancel Search")
    cancel_update = FakeCallbackUpdate(123, cancel_button.callback_data, message=callback_message)
    run(handle_callback_query(cancel_update, None, services, ui_state))

    assert ui_state.get_pending_text_action(123) is None
    restored_text, restored_markup = callback_message.edit_calls[-1]
    assert restored_text.startswith("Search cancelled.\nBot status for Codex in Default Workspace")
    assert "Pending input: none" in restored_text
    assert find_inline_button(restored_markup, "Workspace Search")


def test_bot_status_workspace_files_preview_cancel_and_back_to_status(tmp_path):
    from talk2agent.bots.telegram_bot import (
        BUTTON_BOT_STATUS,
        TelegramUiState,
        handle_callback_query,
        handle_text,
    )

    (tmp_path / "README.md").write_text("hello\n", encoding="utf-8")

    ui_state = TelegramUiState()
    update = FakeUpdate(user_id=123, text=BUTTON_BOT_STATUS)
    services, _ = make_services(provider="codex", workspace_path=str(tmp_path))

    run(handle_text(update, None, services, ui_state))

    callback_message = FakeIncomingMessage("status")
    files_button = find_inline_button(update.message.reply_markups[0], "Workspace Files")
    files_update = FakeCallbackUpdate(123, files_button.callback_data, message=callback_message)
    run(handle_callback_query(files_update, None, services, ui_state))

    files_text, files_markup = callback_message.edit_calls[-1]
    assert files_text.startswith("Workspace files for Codex in Default Workspace\nPath: .")
    assert find_inline_button(files_markup, "Back to Bot Status")

    open_button = find_inline_button(files_markup, "README.md")
    open_update = FakeCallbackUpdate(123, open_button.callback_data, message=callback_message)
    run(handle_callback_query(open_update, None, services, ui_state))

    preview_text, preview_markup = callback_message.edit_calls[-1]
    assert preview_text.startswith("Workspace file for Codex in Default Workspace\nPath: README.md\nhello")

    ask_button = find_inline_button(preview_markup, "Ask Agent About File")
    ask_update = FakeCallbackUpdate(123, ask_button.callback_data, message=callback_message)
    run(handle_callback_query(ask_update, None, services, ui_state))

    cancel_button = find_inline_button(callback_message.edit_calls[-1][1], "Cancel Ask")
    cancel_update = FakeCallbackUpdate(123, cancel_button.callback_data, message=callback_message)
    run(handle_callback_query(cancel_update, None, services, ui_state))

    restored_preview_text, restored_preview_markup = callback_message.edit_calls[-1]
    assert restored_preview_text.startswith(
        "Workspace file for Codex in Default Workspace\nPath: README.md\nhello"
    )

    back_to_folder_button = find_inline_button(restored_preview_markup, "Back to Folder")
    back_to_folder_update = FakeCallbackUpdate(
        123,
        back_to_folder_button.callback_data,
        message=callback_message,
    )
    run(handle_callback_query(back_to_folder_update, None, services, ui_state))

    restored_files_text, restored_files_markup = callback_message.edit_calls[-1]
    assert restored_files_text.startswith("Workspace files for Codex in Default Workspace\nPath: .")
    back_to_status_button = find_inline_button(restored_files_markup, "Back to Bot Status")

    back_to_status_update = FakeCallbackUpdate(
        123,
        back_to_status_button.callback_data,
        message=callback_message,
    )
    run(handle_callback_query(back_to_status_update, None, services, ui_state))

    status_text, status_markup = callback_message.edit_calls[-1]
    assert status_text.startswith("Bot status for Codex in Default Workspace")
    assert find_inline_button(status_markup, "Workspace Files")


def test_bot_status_workspace_files_open_context_bundle_restores_folder_then_status(tmp_path):
    from talk2agent.bots.telegram_bot import (
        BUTTON_BOT_STATUS,
        TelegramUiState,
        handle_callback_query,
        handle_text,
    )

    (tmp_path / "README.md").write_text("hello\n", encoding="utf-8")

    ui_state = TelegramUiState()
    update = FakeUpdate(user_id=123, text=BUTTON_BOT_STATUS)
    services, _ = make_services(provider="codex", workspace_path=str(tmp_path))

    run(handle_text(update, None, services, ui_state))

    callback_message = FakeIncomingMessage("status")
    files_button = find_inline_button(update.message.reply_markups[0], "Workspace Files")
    files_update = FakeCallbackUpdate(123, files_button.callback_data, message=callback_message)
    run(handle_callback_query(files_update, None, services, ui_state))

    bundle_button = find_inline_button(callback_message.edit_calls[-1][1], "Open Context Bundle")
    bundle_update = FakeCallbackUpdate(123, bundle_button.callback_data, message=callback_message)
    run(handle_callback_query(bundle_update, None, services, ui_state))

    bundle_text, bundle_markup = callback_message.edit_calls[-1]
    assert bundle_text == (
        "Context bundle for Codex in Default Workspace\n"
        "Context bundle is empty."
    )
    back_to_folder_button = find_inline_button(bundle_markup, "Back to Folder")

    back_to_folder_update = FakeCallbackUpdate(
        123,
        back_to_folder_button.callback_data,
        message=callback_message,
    )
    run(handle_callback_query(back_to_folder_update, None, services, ui_state))

    restored_files_text, restored_files_markup = callback_message.edit_calls[-1]
    assert restored_files_text.startswith("Workspace files for Codex in Default Workspace\nPath: .")
    back_to_status_button = find_inline_button(restored_files_markup, "Back to Bot Status")

    back_to_status_update = FakeCallbackUpdate(
        123,
        back_to_status_button.callback_data,
        message=callback_message,
    )
    run(handle_callback_query(back_to_status_update, None, services, ui_state))

    status_text, status_markup = callback_message.edit_calls[-1]
    assert status_text.startswith("Bot status for Codex in Default Workspace")
    assert find_inline_button(status_markup, "Workspace Files")


def test_bot_status_workspace_files_ask_with_last_request_returns_to_status(tmp_path):
    from talk2agent.bots.telegram_bot import (
        BUTTON_BOT_STATUS,
        TelegramUiState,
        _ContextBundleItem,
        _context_items_agent_prompt,
        handle_callback_query,
        handle_text,
    )

    (tmp_path / "README.md").write_text("hello\n", encoding="utf-8")

    ui_state = TelegramUiState()
    session = FakeSession(session_id="session-abc")
    services, store = make_services(provider="codex", session=session, workspace_path=str(tmp_path))

    run(handle_text(FakeUpdate(user_id=123, text="Summarize this file."), None, services, ui_state))

    update = FakeUpdate(user_id=123, text=BUTTON_BOT_STATUS)
    run(handle_text(update, None, services, ui_state))

    callback_message = FakeIncomingMessage("status")
    files_button = find_inline_button(update.message.reply_markups[0], "Workspace Files")
    files_update = FakeCallbackUpdate(123, files_button.callback_data, message=callback_message)
    run(handle_callback_query(files_update, None, services, ui_state))

    ask_button = find_inline_button(callback_message.edit_calls[-1][1], "Ask With Last Request")
    ask_update = FakeCallbackUpdate(123, ask_button.callback_data, message=callback_message)
    run(handle_callback_query(ask_update, None, services, ui_state))

    expected_prompt = _context_items_agent_prompt(
        (_ContextBundleItem(kind="file", relative_path="README.md"),),
        "Summarize this file.",
        context_label="visible workspace files",
    )
    assert session.prompts == ["Summarize this file.", expected_prompt]
    assert callback_message.reply_calls == ["hello world"]
    assert store.record_session_usage_calls == [
        (123, "session-abc", "Summarize this file."),
        (123, "session-abc", expected_prompt),
    ]
    final_text, final_markup = callback_message.edit_calls[-1]
    assert final_text.startswith(
        "Asked agent with the last request about visible workspace files.\n"
        "Bot status for Codex in Default Workspace"
    )
    assert find_inline_button(final_markup, "Workspace Files")


def test_bot_status_workspace_file_preview_ask_with_last_request_returns_to_status(tmp_path):
    from talk2agent.bots.telegram_bot import (
        BUTTON_BOT_STATUS,
        TelegramUiState,
        _workspace_file_agent_prompt,
        handle_callback_query,
        handle_text,
    )

    (tmp_path / "README.md").write_text("hello\n", encoding="utf-8")

    ui_state = TelegramUiState()
    session = FakeSession(session_id="session-abc")
    services, store = make_services(provider="codex", session=session, workspace_path=str(tmp_path))

    run(handle_text(FakeUpdate(user_id=123, text="Explain this file."), None, services, ui_state))

    update = FakeUpdate(user_id=123, text=BUTTON_BOT_STATUS)
    run(handle_text(update, None, services, ui_state))

    callback_message = FakeIncomingMessage("status")
    files_button = find_inline_button(update.message.reply_markups[0], "Workspace Files")
    files_update = FakeCallbackUpdate(123, files_button.callback_data, message=callback_message)
    run(handle_callback_query(files_update, None, services, ui_state))

    open_button = find_inline_button(callback_message.edit_calls[-1][1], "README.md")
    open_update = FakeCallbackUpdate(123, open_button.callback_data, message=callback_message)
    run(handle_callback_query(open_update, None, services, ui_state))

    ask_button = find_inline_button(callback_message.edit_calls[-1][1], "Ask With Last Request")
    ask_update = FakeCallbackUpdate(123, ask_button.callback_data, message=callback_message)
    run(handle_callback_query(ask_update, None, services, ui_state))

    expected_prompt = _workspace_file_agent_prompt("README.md", "Explain this file.")
    assert session.prompts == ["Explain this file.", expected_prompt]
    assert callback_message.reply_calls == ["hello world"]
    assert store.record_session_usage_calls == [
        (123, "session-abc", "Explain this file."),
        (123, "session-abc", expected_prompt),
    ]
    final_text, final_markup = callback_message.edit_calls[-1]
    assert final_text.startswith(
        "Asked agent with the last request about this file.\n"
        "Bot status for Codex in Default Workspace"
    )
    assert find_inline_button(final_markup, "Workspace Files")


def test_bot_status_workspace_file_preview_ask_agent_returns_to_status(tmp_path):
    from talk2agent.bots.telegram_bot import (
        BUTTON_BOT_STATUS,
        TelegramUiState,
        _workspace_file_agent_prompt,
        handle_callback_query,
        handle_text,
    )

    (tmp_path / "README.md").write_text("hello\n", encoding="utf-8")

    ui_state = TelegramUiState()
    session = FakeSession(session_id="session-abc")
    services, store = make_services(provider="codex", session=session, workspace_path=str(tmp_path))

    update = FakeUpdate(user_id=123, text=BUTTON_BOT_STATUS)
    run(handle_text(update, None, services, ui_state))

    callback_message = FakeIncomingMessage("status")
    files_button = find_inline_button(update.message.reply_markups[0], "Workspace Files")
    files_update = FakeCallbackUpdate(123, files_button.callback_data, message=callback_message)
    run(handle_callback_query(files_update, None, services, ui_state))

    open_button = find_inline_button(callback_message.edit_calls[-1][1], "README.md")
    open_update = FakeCallbackUpdate(123, open_button.callback_data, message=callback_message)
    run(handle_callback_query(open_update, None, services, ui_state))

    ask_button = find_inline_button(callback_message.edit_calls[-1][1], "Ask Agent About File")
    ask_update = FakeCallbackUpdate(123, ask_button.callback_data, message=callback_message)
    run(handle_callback_query(ask_update, None, services, ui_state))

    request_update = FakeUpdate(user_id=123, text="Explain this file.")
    run(handle_text(request_update, None, services, ui_state))

    expected_prompt = _workspace_file_agent_prompt("README.md", "Explain this file.")
    assert session.prompts == [expected_prompt]
    assert request_update.message.reply_calls == ["hello world"]
    assert store.record_session_usage_calls == [(123, "session-abc", expected_prompt)]
    final_text, final_markup = callback_message.edit_calls[-1]
    assert final_text.startswith(
        "Asked agent about this file.\n"
        "Bot status for Codex in Default Workspace"
    )
    assert find_inline_button(final_markup, "Workspace Files")


def test_bot_status_workspace_file_preview_ask_agent_runtime_failure_restores_status(tmp_path):
    from talk2agent.bots.telegram_bot import (
        BUTTON_BOT_STATUS,
        TelegramUiState,
        handle_callback_query,
        handle_text,
    )

    (tmp_path / "README.md").write_text("hello\n", encoding="utf-8")

    ui_state = TelegramUiState()
    session = FakeSession(error=RuntimeError("boom"), raise_before_stream=True)
    services, store = make_services(provider="codex", session=session, workspace_path=str(tmp_path))

    update = FakeUpdate(user_id=123, text=BUTTON_BOT_STATUS)
    run(handle_text(update, None, services, ui_state))

    callback_message = FakeIncomingMessage("status")
    files_button = find_inline_button(update.message.reply_markups[0], "Workspace Files")
    files_update = FakeCallbackUpdate(123, files_button.callback_data, message=callback_message)
    run(handle_callback_query(files_update, None, services, ui_state))

    open_button = find_inline_button(callback_message.edit_calls[-1][1], "README.md")
    open_update = FakeCallbackUpdate(123, open_button.callback_data, message=callback_message)
    run(handle_callback_query(open_update, None, services, ui_state))

    ask_button = find_inline_button(callback_message.edit_calls[-1][1], "Ask Agent About File")
    ask_update = FakeCallbackUpdate(123, ask_button.callback_data, message=callback_message)
    run(handle_callback_query(ask_update, None, services, ui_state))

    request_update = FakeUpdate(user_id=123, text="Explain this file.")
    run(handle_text(request_update, None, services, ui_state))

    assert store.invalidate_calls == [(123, session)]
    assert request_update.message.reply_calls[0].startswith(
        "Request failed. The current live session for Codex in Default Workspace was closed."
    )
    final_text, final_markup = callback_message.edit_calls[-1]
    assert final_text.startswith("Request failed.\nBot status for Codex in Default Workspace")
    assert find_inline_button(final_markup, "Workspace Files")


def test_bot_status_workspace_search_results_can_open_and_back_to_status(tmp_path):
    from talk2agent.bots.telegram_bot import (
        BUTTON_BOT_STATUS,
        TelegramUiState,
        handle_callback_query,
        handle_text,
    )

    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("hello agent\n", encoding="utf-8")

    ui_state = TelegramUiState()
    update = FakeUpdate(user_id=123, text=BUTTON_BOT_STATUS)
    services, _ = make_services(provider="codex", workspace_path=str(tmp_path))

    run(handle_text(update, None, services, ui_state))

    callback_message = FakeIncomingMessage("status")
    search_button = find_inline_button(update.message.reply_markups[0], "Workspace Search")
    search_update = FakeCallbackUpdate(123, search_button.callback_data, message=callback_message)
    run(handle_callback_query(search_update, None, services, ui_state))

    query_update = FakeUpdate(user_id=123, text="agent")
    run(handle_text(query_update, None, services, ui_state))

    assert query_update.message.reply_calls == []
    search_text, search_markup = callback_message.edit_calls[-1]
    assert search_text.startswith("Workspace search for Codex in Default Workspace\nQuery: agent")
    assert find_inline_button(search_markup, "Back to Bot Status")

    open_button = find_inline_button(search_markup, "Open 1")
    preview_update = FakeCallbackUpdate(123, open_button.callback_data, message=callback_message)
    run(handle_callback_query(preview_update, None, services, ui_state))

    preview_text, preview_markup = callback_message.edit_calls[-1]
    assert preview_text.startswith("Workspace file for Codex in Default Workspace\nPath: src/app.py\nhello agent")

    back_to_search_button = find_inline_button(preview_markup, "Back to Search")
    back_to_search_update = FakeCallbackUpdate(
        123,
        back_to_search_button.callback_data,
        message=callback_message,
    )
    run(handle_callback_query(back_to_search_update, None, services, ui_state))

    restored_search_text, restored_search_markup = callback_message.edit_calls[-1]
    assert restored_search_text.startswith("Workspace search for Codex in Default Workspace\nQuery: agent")
    back_to_status_button = find_inline_button(restored_search_markup, "Back to Bot Status")

    back_to_status_update = FakeCallbackUpdate(
        123,
        back_to_status_button.callback_data,
        message=callback_message,
    )
    run(handle_callback_query(back_to_status_update, None, services, ui_state))

    status_text, status_markup = callback_message.edit_calls[-1]
    assert status_text.startswith("Bot status for Codex in Default Workspace")
    assert find_inline_button(status_markup, "Workspace Search")


def test_bot_status_workspace_search_ask_with_last_request_returns_to_status(tmp_path):
    from talk2agent.bots.telegram_bot import (
        BUTTON_BOT_STATUS,
        TelegramUiState,
        _ContextBundleItem,
        _context_items_agent_prompt,
        handle_callback_query,
        handle_text,
    )

    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("hello agent\n", encoding="utf-8")

    ui_state = TelegramUiState()
    session = FakeSession(session_id="session-abc")
    services, store = make_services(provider="codex", session=session, workspace_path=str(tmp_path))

    run(handle_text(FakeUpdate(user_id=123, text="Review the matching file."), None, services, ui_state))

    update = FakeUpdate(user_id=123, text=BUTTON_BOT_STATUS)
    run(handle_text(update, None, services, ui_state))

    callback_message = FakeIncomingMessage("status")
    search_button = find_inline_button(update.message.reply_markups[0], "Workspace Search")
    search_update = FakeCallbackUpdate(123, search_button.callback_data, message=callback_message)
    run(handle_callback_query(search_update, None, services, ui_state))

    query_update = FakeUpdate(user_id=123, text="agent")
    run(handle_text(query_update, None, services, ui_state))

    ask_button = find_inline_button(callback_message.edit_calls[-1][1], "Ask With Last Request")
    ask_update = FakeCallbackUpdate(123, ask_button.callback_data, message=callback_message)
    run(handle_callback_query(ask_update, None, services, ui_state))

    expected_prompt = _context_items_agent_prompt(
        (_ContextBundleItem(kind="file", relative_path="src/app.py"),),
        "Review the matching file.",
        context_label="matching workspace files",
    )
    assert session.prompts == ["Review the matching file.", expected_prompt]
    assert callback_message.reply_calls == ["hello world"]
    assert store.record_session_usage_calls == [
        (123, "session-abc", "Review the matching file."),
        (123, "session-abc", expected_prompt),
    ]
    final_text, final_markup = callback_message.edit_calls[-1]
    assert final_text.startswith(
        "Asked agent with the last request about matching workspace files.\n"
        "Bot status for Codex in Default Workspace"
    )
    assert find_inline_button(final_markup, "Workspace Search")


def test_bot_status_workspace_search_bundle_item_preview_can_go_back_to_search_then_status(tmp_path):
    from talk2agent.bots.telegram_bot import (
        BUTTON_BOT_STATUS,
        TelegramUiState,
        handle_callback_query,
        handle_text,
    )

    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("hello agent\n", encoding="utf-8")

    ui_state = TelegramUiState()
    update = FakeUpdate(user_id=123, text=BUTTON_BOT_STATUS)
    services, _ = make_services(provider="codex", workspace_path=str(tmp_path))

    run(handle_text(update, None, services, ui_state))

    callback_message = FakeIncomingMessage("status")
    search_button = find_inline_button(update.message.reply_markups[0], "Workspace Search")
    search_update = FakeCallbackUpdate(123, search_button.callback_data, message=callback_message)
    run(handle_callback_query(search_update, None, services, ui_state))

    query_update = FakeUpdate(user_id=123, text="agent")
    run(handle_text(query_update, None, services, ui_state))

    start_button = find_inline_button(
        callback_message.edit_calls[-1][1],
        "Start Bundle Chat With Matching Files",
    )
    start_update = FakeCallbackUpdate(123, start_button.callback_data, message=callback_message)
    run(handle_callback_query(start_update, None, services, ui_state))

    bundle_text, bundle_markup = callback_message.edit_calls[-1]
    assert bundle_text.startswith(
        "Added 1 file from search results to context bundle. Bundle chat enabled.\n"
        "Context bundle for Codex in Default Workspace\nItems: 1\nBundle chat: on"
    )
    assert find_inline_button(bundle_markup, "Back to Search")

    open_button = find_inline_button(bundle_markup, "Open 1")
    open_update = FakeCallbackUpdate(123, open_button.callback_data, message=callback_message)
    run(handle_callback_query(open_update, None, services, ui_state))

    preview_text, preview_markup = callback_message.edit_calls[-1]
    assert preview_text.startswith(
        "Workspace file for Codex in Default Workspace\nPath: src/app.py\nhello agent"
    )
    assert find_inline_button(preview_markup, "Back to Context Bundle")

    back_to_search_button = find_inline_button(preview_markup, "Back to Search")
    back_to_search_update = FakeCallbackUpdate(
        123,
        back_to_search_button.callback_data,
        message=callback_message,
    )
    run(handle_callback_query(back_to_search_update, None, services, ui_state))

    restored_search_text, restored_search_markup = callback_message.edit_calls[-1]
    assert restored_search_text.startswith(
        "Workspace search for Codex in Default Workspace\nQuery: agent"
    )
    back_to_status_button = find_inline_button(restored_search_markup, "Back to Bot Status")

    back_to_status_update = FakeCallbackUpdate(
        123,
        back_to_status_button.callback_data,
        message=callback_message,
    )
    run(handle_callback_query(back_to_status_update, None, services, ui_state))

    status_text, status_markup = callback_message.edit_calls[-1]
    assert status_text.startswith("Bot status for Codex in Default Workspace")
    assert find_inline_button(status_markup, "Workspace Search")


def test_bot_status_workspace_files_ask_agent_returns_to_status(tmp_path):
    from talk2agent.bots.telegram_bot import (
        BUTTON_BOT_STATUS,
        TelegramUiState,
        _ContextBundleItem,
        _context_items_agent_prompt,
        handle_callback_query,
        handle_text,
    )

    (tmp_path / "README.md").write_text("hello\n", encoding="utf-8")

    ui_state = TelegramUiState()
    session = FakeSession(session_id="session-abc")
    services, store = make_services(provider="codex", session=session, workspace_path=str(tmp_path))

    update = FakeUpdate(user_id=123, text=BUTTON_BOT_STATUS)
    run(handle_text(update, None, services, ui_state))

    callback_message = FakeIncomingMessage("status")
    files_button = find_inline_button(update.message.reply_markups[0], "Workspace Files")
    files_update = FakeCallbackUpdate(123, files_button.callback_data, message=callback_message)
    run(handle_callback_query(files_update, None, services, ui_state))

    ask_button = find_inline_button(callback_message.edit_calls[-1][1], "Ask Agent With Visible Files")
    ask_update = FakeCallbackUpdate(123, ask_button.callback_data, message=callback_message)
    run(handle_callback_query(ask_update, None, services, ui_state))

    request_update = FakeUpdate(user_id=123, text="Summarize the visible file.")
    run(handle_text(request_update, None, services, ui_state))

    expected_prompt = _context_items_agent_prompt(
        (_ContextBundleItem(kind="file", relative_path="README.md"),),
        "Summarize the visible file.",
        context_label="visible workspace files",
    )
    assert session.prompts == [expected_prompt]
    assert request_update.message.reply_calls == ["hello world"]
    assert store.record_session_usage_calls == [(123, "session-abc", expected_prompt)]
    final_text, final_markup = callback_message.edit_calls[-1]
    assert final_text.startswith(
        "Asked agent about visible workspace files.\n"
        "Bot status for Codex in Default Workspace"
    )
    assert find_inline_button(final_markup, "Workspace Files")


def test_bot_status_workspace_changes_preview_cancel_and_back_to_status(monkeypatch):
    from talk2agent.bots import telegram_bot
    from talk2agent.bots.telegram_bot import (
        BUTTON_BOT_STATUS,
        TelegramUiState,
        handle_callback_query,
        handle_text,
    )
    from talk2agent.workspace_git import WorkspaceGitDiffPreview, WorkspaceGitStatus, WorkspaceGitStatusEntry

    monkeypatch.setattr(
        telegram_bot,
        "read_workspace_git_status",
        lambda _path: WorkspaceGitStatus(
            is_git_repo=True,
            branch_line="main",
            entries=(WorkspaceGitStatusEntry(" M", "src/app.py", "src/app.py"),),
        ),
    )
    monkeypatch.setattr(
        telegram_bot,
        "read_workspace_git_diff_preview",
        lambda _root, relative_path, status_code: WorkspaceGitDiffPreview(
            relative_path=relative_path,
            status_code=status_code,
            text="diff --git a/src/app.py b/src/app.py",
            truncated=False,
        ),
    )

    ui_state = TelegramUiState()
    update = FakeUpdate(user_id=123, text=BUTTON_BOT_STATUS)
    services, _ = make_services(provider="codex")

    run(handle_text(update, None, services, ui_state))

    callback_message = FakeIncomingMessage("status")
    changes_button = find_inline_button(update.message.reply_markups[0], "Workspace Changes")
    changes_update = FakeCallbackUpdate(123, changes_button.callback_data, message=callback_message)
    run(handle_callback_query(changes_update, None, services, ui_state))

    changes_text, changes_markup = callback_message.edit_calls[-1]
    assert changes_text.startswith("Workspace changes for Codex in Default Workspace\nBranch: main")
    assert find_inline_button(changes_markup, "Back to Bot Status")

    open_button = find_inline_button(changes_markup, "Open 1")
    open_update = FakeCallbackUpdate(123, open_button.callback_data, message=callback_message)
    run(handle_callback_query(open_update, None, services, ui_state))

    preview_text, preview_markup = callback_message.edit_calls[-1]
    assert preview_text.startswith(
        "Workspace change for Codex in Default Workspace\nPath: src/app.py\nStatus:  M"
    )

    ask_button = find_inline_button(preview_markup, "Ask Agent About Change")
    ask_update = FakeCallbackUpdate(123, ask_button.callback_data, message=callback_message)
    run(handle_callback_query(ask_update, None, services, ui_state))

    cancel_button = find_inline_button(callback_message.edit_calls[-1][1], "Cancel Ask")
    cancel_update = FakeCallbackUpdate(123, cancel_button.callback_data, message=callback_message)
    run(handle_callback_query(cancel_update, None, services, ui_state))

    restored_preview_text, restored_preview_markup = callback_message.edit_calls[-1]
    assert restored_preview_text.startswith(
        "Workspace change for Codex in Default Workspace\nPath: src/app.py\nStatus:  M"
    )

    back_to_changes_button = find_inline_button(restored_preview_markup, "Back to Changes")
    back_to_changes_update = FakeCallbackUpdate(
        123,
        back_to_changes_button.callback_data,
        message=callback_message,
    )
    run(handle_callback_query(back_to_changes_update, None, services, ui_state))

    restored_changes_text, restored_changes_markup = callback_message.edit_calls[-1]
    assert restored_changes_text.startswith("Workspace changes for Codex in Default Workspace\nBranch: main")
    back_to_status_button = find_inline_button(restored_changes_markup, "Back to Bot Status")

    back_to_status_update = FakeCallbackUpdate(
        123,
        back_to_status_button.callback_data,
        message=callback_message,
    )
    run(handle_callback_query(back_to_status_update, None, services, ui_state))

    status_text, status_markup = callback_message.edit_calls[-1]
    assert status_text.startswith("Bot status for Codex in Default Workspace")
    assert find_inline_button(status_markup, "Workspace Changes")


def test_bot_status_workspace_changes_ask_with_last_request_returns_to_status(monkeypatch):
    from talk2agent.bots import telegram_bot
    from talk2agent.bots.telegram_bot import (
        BUTTON_BOT_STATUS,
        TelegramUiState,
        _ContextBundleItem,
        _context_items_agent_prompt,
        handle_callback_query,
        handle_text,
    )
    from talk2agent.workspace_git import WorkspaceGitStatus, WorkspaceGitStatusEntry

    monkeypatch.setattr(
        telegram_bot,
        "read_workspace_git_status",
        lambda _path: WorkspaceGitStatus(
            is_git_repo=True,
            branch_line="main",
            entries=(WorkspaceGitStatusEntry(" M", "src/app.py", "src/app.py"),),
        ),
    )

    ui_state = TelegramUiState()
    session = FakeSession(session_id="session-abc")
    services, store = make_services(provider="codex", session=session)

    run(handle_text(FakeUpdate(user_id=123, text="Review the current change set."), None, services, ui_state))

    update = FakeUpdate(user_id=123, text=BUTTON_BOT_STATUS)
    run(handle_text(update, None, services, ui_state))

    callback_message = FakeIncomingMessage("status")
    changes_button = find_inline_button(update.message.reply_markups[0], "Workspace Changes")
    changes_update = FakeCallbackUpdate(123, changes_button.callback_data, message=callback_message)
    run(handle_callback_query(changes_update, None, services, ui_state))

    ask_button = find_inline_button(callback_message.edit_calls[-1][1], "Ask With Last Request")
    ask_update = FakeCallbackUpdate(123, ask_button.callback_data, message=callback_message)
    run(handle_callback_query(ask_update, None, services, ui_state))

    expected_prompt = _context_items_agent_prompt(
        (_ContextBundleItem(kind="change", relative_path="src/app.py", status_code=" M"),),
        "Review the current change set.",
        context_label="current workspace changes",
    )
    assert session.prompts == ["Review the current change set.", expected_prompt]
    assert callback_message.reply_calls == ["hello world"]
    assert store.record_session_usage_calls == [
        (123, "session-abc", "Review the current change set."),
        (123, "session-abc", expected_prompt),
    ]
    final_text, final_markup = callback_message.edit_calls[-1]
    assert final_text.startswith(
        "Asked agent with the last request about current workspace changes.\n"
        "Bot status for Codex in Default Workspace"
    )
    assert find_inline_button(final_markup, "Workspace Changes")


def test_bot_status_workspace_change_preview_ask_with_last_request_returns_to_status(monkeypatch):
    from talk2agent.bots import telegram_bot
    from talk2agent.bots.telegram_bot import (
        BUTTON_BOT_STATUS,
        TelegramUiState,
        _workspace_change_agent_prompt,
        handle_callback_query,
        handle_text,
    )
    from talk2agent.workspace_git import WorkspaceGitDiffPreview, WorkspaceGitStatus, WorkspaceGitStatusEntry

    monkeypatch.setattr(
        telegram_bot,
        "read_workspace_git_status",
        lambda _path: WorkspaceGitStatus(
            is_git_repo=True,
            branch_line="main",
            entries=(WorkspaceGitStatusEntry(" M", "src/app.py", "src/app.py"),),
        ),
    )
    monkeypatch.setattr(
        telegram_bot,
        "read_workspace_git_diff_preview",
        lambda _root, relative_path, status_code: WorkspaceGitDiffPreview(
            relative_path=relative_path,
            status_code=status_code,
            text="diff --git a/src/app.py b/src/app.py",
            truncated=False,
        ),
    )

    ui_state = TelegramUiState()
    session = FakeSession(session_id="session-abc")
    services, store = make_services(provider="codex", session=session)

    run(handle_text(FakeUpdate(user_id=123, text="Explain this diff."), None, services, ui_state))

    update = FakeUpdate(user_id=123, text=BUTTON_BOT_STATUS)
    run(handle_text(update, None, services, ui_state))

    callback_message = FakeIncomingMessage("status")
    changes_button = find_inline_button(update.message.reply_markups[0], "Workspace Changes")
    changes_update = FakeCallbackUpdate(123, changes_button.callback_data, message=callback_message)
    run(handle_callback_query(changes_update, None, services, ui_state))

    open_button = find_inline_button(callback_message.edit_calls[-1][1], "Open 1")
    open_update = FakeCallbackUpdate(123, open_button.callback_data, message=callback_message)
    run(handle_callback_query(open_update, None, services, ui_state))

    ask_button = find_inline_button(callback_message.edit_calls[-1][1], "Ask With Last Request")
    ask_update = FakeCallbackUpdate(123, ask_button.callback_data, message=callback_message)
    run(handle_callback_query(ask_update, None, services, ui_state))

    expected_prompt = _workspace_change_agent_prompt("src/app.py", " M", "Explain this diff.")
    assert session.prompts == ["Explain this diff.", expected_prompt]
    assert callback_message.reply_calls == ["hello world"]
    assert store.record_session_usage_calls == [
        (123, "session-abc", "Explain this diff."),
        (123, "session-abc", expected_prompt),
    ]
    final_text, final_markup = callback_message.edit_calls[-1]
    assert final_text.startswith(
        "Asked agent with the last request about this change.\n"
        "Bot status for Codex in Default Workspace"
    )
    assert find_inline_button(final_markup, "Workspace Changes")


def test_bot_status_workspace_change_preview_ask_agent_returns_to_status(monkeypatch):
    from talk2agent.bots import telegram_bot
    from talk2agent.bots.telegram_bot import (
        BUTTON_BOT_STATUS,
        TelegramUiState,
        _workspace_change_agent_prompt,
        handle_callback_query,
        handle_text,
    )
    from talk2agent.workspace_git import WorkspaceGitDiffPreview, WorkspaceGitStatus, WorkspaceGitStatusEntry

    monkeypatch.setattr(
        telegram_bot,
        "read_workspace_git_status",
        lambda _path: WorkspaceGitStatus(
            is_git_repo=True,
            branch_line="main",
            entries=(WorkspaceGitStatusEntry(" M", "src/app.py", "src/app.py"),),
        ),
    )
    monkeypatch.setattr(
        telegram_bot,
        "read_workspace_git_diff_preview",
        lambda _root, relative_path, status_code: WorkspaceGitDiffPreview(
            relative_path=relative_path,
            status_code=status_code,
            text="diff --git a/src/app.py b/src/app.py",
            truncated=False,
        ),
    )

    ui_state = TelegramUiState()
    session = FakeSession(session_id="session-abc")
    services, store = make_services(provider="codex", session=session)

    update = FakeUpdate(user_id=123, text=BUTTON_BOT_STATUS)
    run(handle_text(update, None, services, ui_state))

    callback_message = FakeIncomingMessage("status")
    changes_button = find_inline_button(update.message.reply_markups[0], "Workspace Changes")
    changes_update = FakeCallbackUpdate(123, changes_button.callback_data, message=callback_message)
    run(handle_callback_query(changes_update, None, services, ui_state))

    open_button = find_inline_button(callback_message.edit_calls[-1][1], "Open 1")
    open_update = FakeCallbackUpdate(123, open_button.callback_data, message=callback_message)
    run(handle_callback_query(open_update, None, services, ui_state))

    ask_button = find_inline_button(callback_message.edit_calls[-1][1], "Ask Agent About Change")
    ask_update = FakeCallbackUpdate(123, ask_button.callback_data, message=callback_message)
    run(handle_callback_query(ask_update, None, services, ui_state))

    request_update = FakeUpdate(user_id=123, text="Explain this change.")
    run(handle_text(request_update, None, services, ui_state))

    expected_prompt = _workspace_change_agent_prompt("src/app.py", " M", "Explain this change.")
    assert session.prompts == [expected_prompt]
    assert request_update.message.reply_calls == ["hello world"]
    assert store.record_session_usage_calls == [(123, "session-abc", expected_prompt)]
    final_text, final_markup = callback_message.edit_calls[-1]
    assert final_text.startswith(
        "Asked agent about this change.\n"
        "Bot status for Codex in Default Workspace"
    )
    assert find_inline_button(final_markup, "Workspace Changes")


def test_bot_status_context_bundle_direct_ask_cancel_returns_to_status(tmp_path):
    from talk2agent.bots.telegram_bot import (
        BUTTON_BOT_STATUS,
        TelegramUiState,
        _ContextBundleItem,
        handle_callback_query,
        handle_text,
    )

    (tmp_path / "notes.txt").write_text("bundle note\n", encoding="utf-8")

    ui_state = TelegramUiState()
    ui_state.add_context_item(
        123,
        "codex",
        "default",
        _ContextBundleItem(kind="file", relative_path="notes.txt"),
    )
    update = FakeUpdate(user_id=123, text=BUTTON_BOT_STATUS)
    services, _ = make_services(provider="codex", workspace_path=str(tmp_path))

    run(handle_text(update, None, services, ui_state))

    callback_message = FakeIncomingMessage("status")
    ask_button = find_inline_button(update.message.reply_markups[0], "Ask Agent With Context")
    ask_update = FakeCallbackUpdate(123, ask_button.callback_data, message=callback_message)
    run(handle_callback_query(ask_update, None, services, ui_state))

    prompt_text, prompt_markup = callback_message.edit_calls[-1]
    assert prompt_text.startswith(
        "Send your request for the current context bundle as the next plain text message."
    )

    cancel_button = find_inline_button(prompt_markup, "Cancel Ask")
    cancel_update = FakeCallbackUpdate(123, cancel_button.callback_data, message=callback_message)
    run(handle_callback_query(cancel_update, None, services, ui_state))

    restored_text, restored_markup = callback_message.edit_calls[-1]
    assert restored_text.startswith(
        "Context bundle request cancelled.\nBot status for Codex in Default Workspace"
    )
    assert find_inline_button(restored_markup, "Ask Agent With Context")


def test_bot_status_context_bundle_direct_ask_with_last_request_returns_to_status(tmp_path):
    from talk2agent.bots.telegram_bot import (
        BUTTON_BOT_STATUS,
        TelegramUiState,
        _ContextBundleItem,
        _context_bundle_agent_prompt,
        handle_callback_query,
        handle_text,
    )

    (tmp_path / "notes.txt").write_text("bundle note\n", encoding="utf-8")

    ui_state = TelegramUiState()
    ui_state.add_context_item(
        123,
        "codex",
        "default",
        _ContextBundleItem(kind="file", relative_path="notes.txt"),
    )
    ui_state.set_last_request_text(123, "default", "Use the saved context.")
    session = FakeSession(session_id="session-abc")
    services, store = make_services(provider="codex", session=session, workspace_path=str(tmp_path))

    update = FakeUpdate(user_id=123, text=BUTTON_BOT_STATUS)
    run(handle_text(update, None, services, ui_state))

    callback_message = FakeIncomingMessage("status")
    ask_button = find_inline_button(update.message.reply_markups[0], "Bundle + Last Request")
    ask_update = FakeCallbackUpdate(123, ask_button.callback_data, message=callback_message)
    run(handle_callback_query(ask_update, None, services, ui_state))

    expected_prompt = _context_bundle_agent_prompt(
        (_ContextBundleItem(kind="file", relative_path="notes.txt"),),
        "Use the saved context.",
    )
    assert session.prompts == [expected_prompt]
    assert callback_message.reply_calls == ["hello world"]
    assert store.record_session_usage_calls == [(123, "session-abc", expected_prompt)]
    final_text, final_markup = callback_message.edit_calls[-1]
    assert final_text.startswith(
        "Asked agent with the last request using the current context bundle.\n"
        "Bot status for Codex in Default Workspace"
    )
    assert find_inline_button(final_markup, "Context Bundle")


def test_bot_status_context_bundle_direct_clear_returns_to_status():
    from talk2agent.bots.telegram_bot import (
        BUTTON_BOT_STATUS,
        TelegramUiState,
        _ContextBundleItem,
        handle_callback_query,
        handle_text,
    )

    ui_state = TelegramUiState()
    ui_state.add_context_item(
        123,
        "codex",
        "default",
        _ContextBundleItem(kind="file", relative_path="notes.txt"),
    )
    ui_state.enable_context_bundle_chat(123, "codex", "default")
    update = FakeUpdate(user_id=123, text=BUTTON_BOT_STATUS)
    services, _ = make_services(provider="codex")

    run(handle_text(update, None, services, ui_state))

    callback_message = FakeIncomingMessage("status")
    clear_button = find_inline_button(update.message.reply_markups[0], "Clear Bundle")
    clear_update = FakeCallbackUpdate(123, clear_button.callback_data, message=callback_message)
    run(handle_callback_query(clear_update, None, services, ui_state))

    assert ui_state.get_context_bundle(123, "codex", "default") is None
    assert ui_state.context_bundle_chat_active(123, "codex", "default") is False
    final_text, final_markup = callback_message.edit_calls[-1]
    assert final_text.startswith(
        "Cleared context bundle. Bundle chat was turned off.\n"
        "Bot status for Codex in Default Workspace"
    )
    assert "Context bundle: 0 items" in final_text
    assert "Bundle chat: off" in final_text
    labels = [button.text for row in final_markup.inline_keyboard for button in row]
    assert "Ask Agent With Context" not in labels
    assert "Clear Bundle" not in labels


def test_bot_status_context_bundle_empty_can_back_to_status():
    from talk2agent.bots.telegram_bot import (
        BUTTON_BOT_STATUS,
        TelegramUiState,
        handle_callback_query,
        handle_text,
    )

    ui_state = TelegramUiState()
    update = FakeUpdate(user_id=123, text=BUTTON_BOT_STATUS)
    services, _ = make_services(provider="codex")

    run(handle_text(update, None, services, ui_state))

    callback_message = FakeIncomingMessage("status")
    bundle_button = find_inline_button(update.message.reply_markups[0], "Context Bundle")
    bundle_update = FakeCallbackUpdate(123, bundle_button.callback_data, message=callback_message)
    run(handle_callback_query(bundle_update, None, services, ui_state))

    bundle_text, bundle_markup = callback_message.edit_calls[-1]
    assert bundle_text == (
        "Context bundle for Codex in Default Workspace\n"
        "Context bundle is empty."
    )
    back_button = find_inline_button(bundle_markup, "Back to Bot Status")

    back_update = FakeCallbackUpdate(123, back_button.callback_data, message=callback_message)
    run(handle_callback_query(back_update, None, services, ui_state))

    restored_text, restored_markup = callback_message.edit_calls[-1]
    assert restored_text.startswith("Bot status for Codex in Default Workspace")
    assert find_inline_button(restored_markup, "Context Bundle")


def test_bot_status_context_bundle_preview_cancel_and_back_to_status(tmp_path):
    from talk2agent.bots.telegram_bot import (
        BUTTON_BOT_STATUS,
        TelegramUiState,
        _ContextBundleItem,
        handle_callback_query,
        handle_text,
    )

    (tmp_path / "notes.txt").write_text("bundle note\n", encoding="utf-8")

    ui_state = TelegramUiState()
    ui_state.add_context_item(
        123,
        "codex",
        "default",
        _ContextBundleItem(kind="file", relative_path="notes.txt"),
    )
    update = FakeUpdate(user_id=123, text=BUTTON_BOT_STATUS)
    services, _ = make_services(provider="codex", workspace_path=str(tmp_path))

    run(handle_text(update, None, services, ui_state))

    callback_message = FakeIncomingMessage("status")
    bundle_button = find_inline_button(update.message.reply_markups[0], "Context Bundle")
    bundle_update = FakeCallbackUpdate(123, bundle_button.callback_data, message=callback_message)
    run(handle_callback_query(bundle_update, None, services, ui_state))

    bundle_text, bundle_markup = callback_message.edit_calls[-1]
    assert bundle_text.startswith(
        "Context bundle for Codex in Default Workspace\nItems: 1\nBundle chat: off\n1. [file] notes.txt"
    )
    assert find_inline_button(bundle_markup, "Back to Bot Status")

    open_button = find_inline_button(bundle_markup, "Open 1")
    open_update = FakeCallbackUpdate(123, open_button.callback_data, message=callback_message)
    run(handle_callback_query(open_update, None, services, ui_state))

    preview_text, preview_markup = callback_message.edit_calls[-1]
    assert preview_text.startswith("Workspace file for Codex in Default Workspace\nPath: notes.txt\nbundle note")

    ask_button = find_inline_button(preview_markup, "Ask Agent About File")
    ask_update = FakeCallbackUpdate(123, ask_button.callback_data, message=callback_message)
    run(handle_callback_query(ask_update, None, services, ui_state))

    cancel_button = find_inline_button(callback_message.edit_calls[-1][1], "Cancel Ask")
    cancel_update = FakeCallbackUpdate(123, cancel_button.callback_data, message=callback_message)
    run(handle_callback_query(cancel_update, None, services, ui_state))

    restored_preview_text, restored_preview_markup = callback_message.edit_calls[-1]
    assert restored_preview_text.startswith(
        "Workspace file for Codex in Default Workspace\nPath: notes.txt\nbundle note"
    )

    back_to_bundle_button = find_inline_button(restored_preview_markup, "Back to Context Bundle")
    back_to_bundle_update = FakeCallbackUpdate(
        123,
        back_to_bundle_button.callback_data,
        message=callback_message,
    )
    run(handle_callback_query(back_to_bundle_update, None, services, ui_state))

    restored_bundle_text, restored_bundle_markup = callback_message.edit_calls[-1]
    assert restored_bundle_text.startswith(
        "Context bundle for Codex in Default Workspace\nItems: 1\nBundle chat: off\n1. [file] notes.txt"
    )
    back_to_status_button = find_inline_button(restored_bundle_markup, "Back to Bot Status")

    back_to_status_update = FakeCallbackUpdate(
        123,
        back_to_status_button.callback_data,
        message=callback_message,
    )
    run(handle_callback_query(back_to_status_update, None, services, ui_state))

    status_text, status_markup = callback_message.edit_calls[-1]
    assert status_text.startswith("Bot status for Codex in Default Workspace")
    assert find_inline_button(status_markup, "Context Bundle")


def test_bot_status_context_bundle_ask_with_last_request_returns_to_status(tmp_path):
    from talk2agent.bots.telegram_bot import (
        BUTTON_BOT_STATUS,
        TelegramUiState,
        _ContextBundleItem,
        _context_bundle_agent_prompt,
        handle_callback_query,
        handle_text,
    )

    (tmp_path / "notes.txt").write_text("bundle note\n", encoding="utf-8")

    ui_state = TelegramUiState()
    ui_state.add_context_item(
        123,
        "codex",
        "default",
        _ContextBundleItem(kind="file", relative_path="notes.txt"),
    )
    session = FakeSession(session_id="session-abc")
    services, store = make_services(provider="codex", session=session, workspace_path=str(tmp_path))

    run(handle_text(FakeUpdate(user_id=123, text="Use the saved context."), None, services, ui_state))

    update = FakeUpdate(user_id=123, text=BUTTON_BOT_STATUS)
    run(handle_text(update, None, services, ui_state))

    callback_message = FakeIncomingMessage("status")
    bundle_button = find_inline_button(update.message.reply_markups[0], "Context Bundle")
    bundle_update = FakeCallbackUpdate(123, bundle_button.callback_data, message=callback_message)
    run(handle_callback_query(bundle_update, None, services, ui_state))

    ask_button = find_inline_button(callback_message.edit_calls[-1][1], "Ask With Last Request")
    ask_update = FakeCallbackUpdate(123, ask_button.callback_data, message=callback_message)
    run(handle_callback_query(ask_update, None, services, ui_state))

    expected_prompt = _context_bundle_agent_prompt(
        (_ContextBundleItem(kind="file", relative_path="notes.txt"),),
        "Use the saved context.",
    )
    assert session.prompts == ["Use the saved context.", expected_prompt]
    assert callback_message.reply_calls == ["hello world"]
    assert store.record_session_usage_calls == [
        (123, "session-abc", "Use the saved context."),
        (123, "session-abc", expected_prompt),
    ]
    final_text, final_markup = callback_message.edit_calls[-1]
    assert final_text.startswith(
        "Asked agent with the last request using the current context bundle.\n"
        "Bot status for Codex in Default Workspace"
    )
    assert find_inline_button(final_markup, "Context Bundle")


def test_bot_status_context_bundle_ask_returns_to_status(tmp_path):
    from talk2agent.bots.telegram_bot import (
        BUTTON_BOT_STATUS,
        TelegramUiState,
        _ContextBundleItem,
        _context_bundle_agent_prompt,
        handle_callback_query,
        handle_text,
    )

    (tmp_path / "notes.txt").write_text("bundle note\n", encoding="utf-8")

    ui_state = TelegramUiState()
    ui_state.add_context_item(
        123,
        "codex",
        "default",
        _ContextBundleItem(kind="file", relative_path="notes.txt"),
    )
    session = FakeSession(session_id="session-abc")
    services, store = make_services(provider="codex", session=session, workspace_path=str(tmp_path))

    update = FakeUpdate(user_id=123, text=BUTTON_BOT_STATUS)
    run(handle_text(update, None, services, ui_state))

    callback_message = FakeIncomingMessage("status")
    bundle_button = find_inline_button(update.message.reply_markups[0], "Context Bundle")
    bundle_update = FakeCallbackUpdate(123, bundle_button.callback_data, message=callback_message)
    run(handle_callback_query(bundle_update, None, services, ui_state))

    ask_button = find_inline_button(callback_message.edit_calls[-1][1], "Ask Agent With Context")
    ask_update = FakeCallbackUpdate(123, ask_button.callback_data, message=callback_message)
    run(handle_callback_query(ask_update, None, services, ui_state))

    request_update = FakeUpdate(user_id=123, text="Use the bundle.")
    run(handle_text(request_update, None, services, ui_state))

    expected_prompt = _context_bundle_agent_prompt(
        (_ContextBundleItem(kind="file", relative_path="notes.txt"),),
        "Use the bundle.",
    )
    assert session.prompts == [expected_prompt]
    assert request_update.message.reply_calls == ["hello world"]
    assert store.record_session_usage_calls == [(123, "session-abc", expected_prompt)]
    final_text, final_markup = callback_message.edit_calls[-1]
    assert final_text.startswith(
        "Asked agent with the current context bundle.\n"
        "Bot status for Codex in Default Workspace"
    )
    assert find_inline_button(final_markup, "Context Bundle")


def test_bot_status_switch_agent_can_open_and_back_to_status():
    from talk2agent.bots.telegram_bot import (
        BUTTON_BOT_STATUS,
        TelegramUiState,
        handle_callback_query,
        handle_text,
    )

    ui_state = TelegramUiState()
    update = FakeUpdate(user_id=123, text=BUTTON_BOT_STATUS)
    services, _ = make_services(provider="codex", admin_user_id=123)

    run(handle_text(update, None, services, ui_state))

    callback_message = FakeIncomingMessage("status")
    switch_button = find_inline_button(update.message.reply_markups[0], "Switch Agent")
    switch_update = FakeCallbackUpdate(123, switch_button.callback_data, message=callback_message)
    run(handle_callback_query(switch_update, None, services, ui_state))

    switch_text, switch_markup = callback_message.edit_calls[-1]
    assert switch_text.startswith("Current provider: Codex\nWorkspace: Default Workspace\nProvider capabilities:")
    assert find_inline_button(switch_markup, "Back to Bot Status")

    back_update = FakeCallbackUpdate(
        123,
        find_inline_button(switch_markup, "Back to Bot Status").callback_data,
        message=callback_message,
    )
    run(handle_callback_query(back_update, None, services, ui_state))

    status_text, status_markup = callback_message.edit_calls[-1]
    assert status_text.startswith("Bot status for Codex in Default Workspace")
    assert find_inline_button(status_markup, "Switch Agent")


def test_bot_status_switch_agent_success_returns_to_status():
    from talk2agent.bots.telegram_bot import (
        BUTTON_BOT_STATUS,
        TelegramUiState,
        handle_callback_query,
        handle_text,
    )

    ui_state = TelegramUiState()
    update = FakeUpdate(user_id=123, text=BUTTON_BOT_STATUS)
    services, _ = make_services(provider="codex", admin_user_id=123)

    run(handle_text(update, None, services, ui_state))

    callback_message = FakeIncomingMessage("status")
    switch_button = find_inline_button(update.message.reply_markups[0], "Switch Agent")
    switch_update = FakeCallbackUpdate(123, switch_button.callback_data, message=callback_message)
    run(handle_callback_query(switch_update, None, services, ui_state))

    provider_button = find_inline_button(callback_message.edit_calls[-1][1], "Gemini CLI")
    provider_update = FakeCallbackUpdate(123, provider_button.callback_data, message=callback_message)
    run(handle_callback_query(provider_update, None, services, ui_state))

    final_text, final_markup = callback_message.edit_calls[-1]
    assert final_text.startswith(
        "Switched agent to Gemini CLI in Default Workspace. "
        "Old bot buttons and pending inputs were cleared.\n"
        "Bot status for Gemini CLI in Default Workspace"
    )
    assert find_inline_button(final_markup, "Switch Agent")


def test_bot_status_switch_workspace_can_open_and_back_to_status():
    from talk2agent.bots.telegram_bot import (
        BUTTON_BOT_STATUS,
        TelegramUiState,
        handle_callback_query,
        handle_text,
    )

    ui_state = TelegramUiState()
    update = FakeUpdate(user_id=123, text=BUTTON_BOT_STATUS)
    services, _ = make_services(provider="codex", admin_user_id=123)

    run(handle_text(update, None, services, ui_state))

    callback_message = FakeIncomingMessage("status")
    switch_button = find_inline_button(update.message.reply_markups[0], "Switch Workspace")
    switch_update = FakeCallbackUpdate(123, switch_button.callback_data, message=callback_message)
    run(handle_callback_query(switch_update, None, services, ui_state))

    switch_text, switch_markup = callback_message.edit_calls[-1]
    assert switch_text == "Current workspace: Default Workspace"
    assert find_inline_button(switch_markup, "Back to Bot Status")

    back_update = FakeCallbackUpdate(
        123,
        find_inline_button(switch_markup, "Back to Bot Status").callback_data,
        message=callback_message,
    )
    run(handle_callback_query(back_update, None, services, ui_state))

    status_text, status_markup = callback_message.edit_calls[-1]
    assert status_text.startswith("Bot status for Codex in Default Workspace")
    assert find_inline_button(status_markup, "Switch Workspace")


def test_bot_status_switch_workspace_success_returns_to_status():
    from talk2agent.bots.telegram_bot import (
        BUTTON_BOT_STATUS,
        TelegramUiState,
        handle_callback_query,
        handle_text,
    )

    ui_state = TelegramUiState()
    update = FakeUpdate(user_id=123, text=BUTTON_BOT_STATUS)
    services, _ = make_services(provider="codex", admin_user_id=123)

    run(handle_text(update, None, services, ui_state))

    callback_message = FakeIncomingMessage("status")
    switch_button = find_inline_button(update.message.reply_markups[0], "Switch Workspace")
    switch_update = FakeCallbackUpdate(123, switch_button.callback_data, message=callback_message)
    run(handle_callback_query(switch_update, None, services, ui_state))

    workspace_button = find_inline_button(callback_message.edit_calls[-1][1], "Alt Workspace")
    workspace_update = FakeCallbackUpdate(123, workspace_button.callback_data, message=callback_message)
    run(handle_callback_query(workspace_update, None, services, ui_state))

    final_text, final_markup = callback_message.edit_calls[-1]
    assert final_text.startswith(
        "Switched workspace to Alt Workspace on Codex. "
        "Old bot buttons and pending inputs were cleared.\n"
        "Bot status for Codex in Alt Workspace"
    )
    assert find_inline_button(final_markup, "Switch Workspace")


def test_switch_agent_button_shows_provider_choices():
    from talk2agent.bots.telegram_bot import BUTTON_SWITCH_AGENT, TelegramUiState, handle_text

    update = FakeUpdate(user_id=123, text=BUTTON_SWITCH_AGENT)
    services, _ = make_services(provider="codex", admin_user_id=123)

    run(handle_text(update, None, services, TelegramUiState()))

    assert update.message.reply_calls == [
        (
            "Current provider: Codex\n"
            "Workspace: Default Workspace\n"
            "Provider capabilities:\n"
            "- Claude Code: img=yes audio=no docs=yes sessions=list/resume/fork\n"
            "* Codex [current]: img=yes audio=yes docs=yes sessions=list/resume/fork\n"
            "- Gemini CLI: img=yes audio=yes docs=no sessions=none"
        )
    ]
    assert services.discover_provider_capabilities_calls == [
        ("claude", "default"),
        ("codex", "default"),
        ("gemini", "default"),
    ]
    markup = update.message.reply_markups[0]
    labels = [button.text for row in markup.inline_keyboard for button in row]
    assert labels == ["Claude Code", "Current: Codex", "Gemini CLI"]


def test_switch_agent_button_shows_replay_shortcuts_when_last_turn_exists():
    from talk2agent.acp.agent_session import PromptText
    from talk2agent.bots.telegram_bot import BUTTON_SWITCH_AGENT, TelegramUiState, _ReplayTurn, handle_text

    ui_state = TelegramUiState()
    ui_state.set_last_turn(
        123,
        _ReplayTurn(
            provider="codex",
            workspace_id="default",
            prompt_items=(PromptText("hello"),),
            title_hint="hello",
        ),
    )
    update = FakeUpdate(user_id=123, text=BUTTON_SWITCH_AGENT)
    services, _ = make_services(provider="codex", admin_user_id=123)

    run(handle_text(update, None, services, ui_state))

    markup = update.message.reply_markups[0]
    labels = [button.text for row in markup.inline_keyboard for button in row]
    assert labels == [
        "Claude Code",
        "Retry on Claude Code",
        "Fork on Claude Code",
        "Current: Codex",
        "Gemini CLI",
        "Retry on Gemini CLI",
        "Fork on Gemini CLI",
    ]


def test_switch_agent_button_hides_replay_shortcuts_when_last_turn_workspace_differs():
    from talk2agent.acp.agent_session import PromptText
    from talk2agent.bots.telegram_bot import BUTTON_SWITCH_AGENT, TelegramUiState, _ReplayTurn, handle_text

    ui_state = TelegramUiState()
    ui_state.set_last_turn(
        123,
        _ReplayTurn(
            provider="codex",
            workspace_id="alt",
            prompt_items=(PromptText("hello"),),
            title_hint="hello",
        ),
    )
    update = FakeUpdate(user_id=123, text=BUTTON_SWITCH_AGENT)
    services, _ = make_services(provider="codex", admin_user_id=123)

    run(handle_text(update, None, services, ui_state))

    markup = update.message.reply_markups[0]
    labels = [button.text for row in markup.inline_keyboard for button in row]
    assert labels == ["Claude Code", "Current: Codex", "Gemini CLI"]


def test_switch_agent_button_shows_unavailable_provider_capability_summary():
    from talk2agent.bots.telegram_bot import BUTTON_SWITCH_AGENT, TelegramUiState, handle_text

    update = FakeUpdate(user_id=123, text=BUTTON_SWITCH_AGENT)
    services, _ = make_services(
        provider="codex",
        admin_user_id=123,
        provider_capabilities={
            "claude": SimpleNamespace(
                provider="claude",
                available=False,
                error="command missing",
            ),
                "codex": SimpleNamespace(
                    provider="codex",
                    available=True,
                    supports_image_prompt=True,
                    supports_audio_prompt=True,
                    supports_embedded_context_prompt=True,
                    can_fork_sessions=True,
                    can_list_sessions=True,
                    can_resume_sessions=True,
                    error=None,
                ),
            "gemini": SimpleNamespace(
                provider="gemini",
                available=False,
                error="session creation failed",
            ),
        },
    )

    run(handle_text(update, None, services, TelegramUiState()))

    assert update.message.reply_calls == [
        (
            "Current provider: Codex\n"
            "Workspace: Default Workspace\n"
            "Provider capabilities:\n"
            "- Claude Code: unavailable (command missing)\n"
            "* Codex [current]: img=yes audio=yes docs=yes sessions=list/resume/fork\n"
            "- Gemini CLI: unavailable (session creation failed)"
        )
    ]


def test_callback_switch_provider_current_provider_is_noop():
    from talk2agent.bots.telegram_bot import BUTTON_SWITCH_AGENT, TelegramUiState, handle_callback_query, handle_text

    ui_state = TelegramUiState()
    update = FakeUpdate(user_id=123, text=BUTTON_SWITCH_AGENT)
    services, _ = make_services(provider="codex", admin_user_id=123)

    run(handle_text(update, None, services, ui_state))

    markup = update.message.reply_markups[0]
    current_button = markup.inline_keyboard[1][0]
    callback_update = FakeCallbackUpdate(123, current_button.callback_data, message=FakeIncomingMessage("switch"))

    run(handle_callback_query(callback_update, None, services, ui_state))

    assert services.switch_provider_calls == []
    assert callback_update.callback_query.answers == [("Already using Codex.", False)]
    assert callback_update.callback_query.message.edit_calls == []


def test_callback_switch_provider_failure_edits_message_text():
    from talk2agent.bots.telegram_bot import CALLBACK_PREFIX, TelegramUiState, handle_callback_query

    ui_state = TelegramUiState()
    token = ui_state.create(123, "switch_provider", provider="gemini")
    message = FakeIncomingMessage("switch")
    update = FakeCallbackUpdate(123, f"{CALLBACK_PREFIX}{token}", message=message)
    services, _ = make_services(admin_user_id=123, switch_error=RuntimeError("boom"))

    run(handle_callback_query(update, None, services, ui_state))

    assert services.switch_provider_calls == ["gemini"]
    assert update.callback_query.answers == [(None, False)]
    assert [text for text, _ in message.edit_calls] == [
        "Switching to Gemini CLI...",
        "session creation failed",
    ]


def test_switch_workspace_button_shows_choices_and_switches():
    from talk2agent.bots.telegram_bot import BUTTON_SWITCH_WORKSPACE, TelegramUiState, handle_callback_query, handle_text

    ui_state = TelegramUiState()
    update = FakeUpdate(user_id=123, text=BUTTON_SWITCH_WORKSPACE)
    services, _ = make_services(provider="codex", admin_user_id=123)

    run(handle_text(update, None, services, ui_state))

    markup = update.message.reply_markups[0]
    alt_button = markup.inline_keyboard[1][0]
    callback_update = FakeCallbackUpdate(123, alt_button.callback_data, message=FakeIncomingMessage("workspace"))

    run(handle_callback_query(callback_update, None, services, ui_state))

    assert services.switch_workspace_calls == ["alt"]
    assert [text for text, _ in callback_update.callback_query.message.edit_calls] == [
        "Switching workspace to Alt Workspace...",
        "Switched workspace to Alt Workspace on Codex. Old bot buttons and pending inputs were cleared.",
    ]


def test_switch_provider_invalidates_stale_buttons_and_pending_inputs():
    from talk2agent.bots.telegram_bot import CALLBACK_PREFIX, TelegramUiState, handle_callback_query

    ui_state = TelegramUiState()
    ui_state.set_pending_text_action(123, "workspace_search")
    stale_token = ui_state.create(123, "workspace_page", relative_path="", page=0)
    switch_token = ui_state.create(123, "switch_provider", provider="gemini")
    message = FakeIncomingMessage("switch")
    update = FakeCallbackUpdate(123, f"{CALLBACK_PREFIX}{switch_token}", message=message)
    services, _ = make_services(admin_user_id=123)

    run(handle_callback_query(update, None, services, ui_state))

    assert ui_state.get_pending_text_action(123) is None
    assert [text for text, _ in message.edit_calls] == [
        "Switching to Gemini CLI...",
        "Switched agent to Gemini CLI in Default Workspace. Old bot buttons and pending inputs were cleared.",
    ]

    stale_update = FakeCallbackUpdate(123, f"{CALLBACK_PREFIX}{stale_token}", message=FakeIncomingMessage("stale"))
    run(handle_callback_query(stale_update, None, services, ui_state))

    assert stale_update.callback_query.answers == [("This button has expired.", True)]


def test_callback_switch_provider_fork_last_turn_switches_then_replays_in_new_session():
    from talk2agent.acp.agent_session import PromptText
    from talk2agent.bots.telegram_bot import (
        BUTTON_SWITCH_AGENT,
        TelegramUiState,
        _ReplayTurn,
        handle_callback_query,
        handle_text,
    )

    ui_state = TelegramUiState()
    ui_state.set_last_turn(
        123,
        _ReplayTurn(
            provider="codex",
            workspace_id="default",
            prompt_items=(PromptText("hello"),),
            title_hint="hello",
        ),
    )
    menu_update = FakeUpdate(user_id=123, text=BUTTON_SWITCH_AGENT)
    message = FakeIncomingMessage("switch")
    services, store = make_services(provider="codex", admin_user_id=123)

    run(handle_text(menu_update, None, services, ui_state))

    fork_button = find_inline_button(menu_update.message.reply_markups[0], "Fork on Gemini CLI")

    async def switched_snapshot_runtime_state():
        return SimpleNamespace(
            provider="gemini",
            workspace_id="default",
            workspace_path="F:/workspace",
            session_store=store,
        )

    services.snapshot_runtime_state = switched_snapshot_runtime_state

    callback_update = FakeCallbackUpdate(123, fork_button.callback_data, message=message)
    run(handle_callback_query(callback_update, make_context(application=FakeApplication()), services, ui_state))

    assert services.switch_provider_calls == ["gemini"]
    assert store.reset_calls == [123]
    assert [text for text, _ in message.edit_calls] == [
        "Switching to Gemini CLI...",
        (
            "Switched agent to Gemini CLI in Default Workspace. "
            "Old bot buttons and pending inputs were cleared.\n"
            "Forking last turn on the new agent..."
        ),
    ]
    assert len(services.final_session.prompt_items) == 1
    assert services.final_session.prompt_items[0] == (PromptText("hello"),)
    assert message.reply_calls == ["hello world"]
    assert store.record_session_usage_calls == [
        (123, "session-123", "hello"),
    ]
    replay_turn = ui_state.get_last_turn(123, "gemini", "default")
    assert replay_turn is not None
    assert replay_turn.provider == "gemini"


def test_invalidate_runtime_bound_interactions_clears_aliases_bundle_chat_and_media_groups():
    from talk2agent.bots.telegram_bot import TelegramUiState, _ContextBundleItem

    async def scenario():
        ui_state = TelegramUiState()
        ui_state.set_agent_command_aliases(123, {"model": "/model"})
        ui_state.set_pending_text_action(123, "workspace_search")
        ui_state.add_context_item(
            123,
            "claude",
            "default",
            _ContextBundleItem(kind="file", relative_path="notes.txt"),
        )
        assert ui_state.enable_context_bundle_chat(123, "claude", "default") is True
        ui_state.add_media_group_message(123, "group-1", FakeIncomingMessage(caption="photo"))
        task = asyncio.create_task(asyncio.sleep(10))
        ui_state.replace_media_group_task(123, "group-1", task)

        callback_token = ui_state.create(123, "workspace_page", relative_path="", page=0)
        ui_state.invalidate_runtime_bound_interactions()
        await asyncio.sleep(0)
        return ui_state, task, callback_token

    ui_state, task, callback_token = run(scenario())

    assert ui_state.resolve_agent_command(123, "model") is None
    assert ui_state.get_pending_text_action(123) is None
    assert ui_state.get(callback_token) is None
    assert ui_state.context_bundle_chat_active(123, "claude", "default") is False
    assert ui_state.pop_media_group_messages(123, "group-1") == ()
    assert task.cancelled()


def test_session_history_delete_refreshes_with_notice():
    from talk2agent.bots.telegram_bot import (
        BUTTON_SESSION_HISTORY,
        TelegramUiState,
        handle_callback_query,
        handle_text,
    )

    history_entries = [
        build_history_entry("session-1", "First"),
        build_history_entry("session-2", "Second"),
    ]
    ui_state = TelegramUiState()
    ui_state.set_pending_text_action(123, "workspace_search")
    stale_token = ui_state.create(123, "workspace_page", relative_path="", page=0)
    update = FakeUpdate(user_id=123, text=BUTTON_SESSION_HISTORY)
    services, store = make_services(provider="codex", history_entries=history_entries)

    run(handle_text(update, None, services, ui_state))

    first_markup = update.message.reply_markups[0]
    delete_button = first_markup.inline_keyboard[0][2]
    callback_update = FakeCallbackUpdate(123, delete_button.callback_data, message=FakeIncomingMessage("history"))

    run(handle_callback_query(callback_update, None, services, ui_state))

    assert store.delete_history_calls == [(123, "session-1")]
    edited_texts = [text for text, _ in callback_update.callback_query.message.edit_calls]
    assert edited_texts[0] == "Deleting session..."
    assert edited_texts[-1].startswith("Deleted session.\nSession history for Codex in Default Workspace")
    assert ui_state.get(stale_token) is not None


def test_session_history_delete_current_session_clears_session_bound_interactions_and_syncs_commands():
    from talk2agent.bots.telegram_bot import (
        BUTTON_SESSION_HISTORY,
        CALLBACK_PREFIX,
        TelegramUiState,
        _ContextBundleItem,
        handle_callback_query,
        handle_text,
    )

    async def scenario():
        ui_state = TelegramUiState()
        ui_state.set_pending_text_action(123, "workspace_search")
        ui_state.set_agent_command_aliases(123, {"old_status": "old_status"})
        ui_state.add_context_item(
            123,
            "codex",
            "default",
            _ContextBundleItem(kind="file", relative_path="notes.txt"),
        )
        assert ui_state.enable_context_bundle_chat(123, "codex", "default") is True
        ui_state.add_media_group_message(123, "group-1", FakeIncomingMessage(caption="photo"))
        task = asyncio.create_task(asyncio.sleep(10))
        ui_state.replace_media_group_task(123, "group-1", task)
        stale_token = ui_state.create(123, "workspace_page", relative_path="", page=0)

        history_entries = [
            build_history_entry("session-1", "First"),
            build_history_entry("session-2", "Second"),
        ]
        session = FakeSession(
            session_id="session-1",
            available_commands=[FakeCommand("status", "Show status")],
        )
        update = FakeUpdate(user_id=123, text=BUTTON_SESSION_HISTORY)
        application = FakeApplication()
        services, store = make_services(
            provider="codex",
            session=session,
            history_entries=history_entries,
        )

        await handle_text(update, make_context(application=application), services, ui_state)

        first_markup = update.message.reply_markups[0]
        delete_button = first_markup.inline_keyboard[0][2]
        callback_update = FakeCallbackUpdate(
            123,
            delete_button.callback_data,
            message=FakeIncomingMessage("history"),
        )

        await handle_callback_query(
            callback_update,
            make_context(application=application),
            services,
            ui_state,
        )
        await asyncio.sleep(0)
        stale_update = FakeCallbackUpdate(
            123,
            f"{CALLBACK_PREFIX}{stale_token}",
            message=FakeIncomingMessage("stale"),
        )
        await handle_callback_query(stale_update, None, services, ui_state)
        return (
            application,
            callback_update,
            session,
            stale_token,
            stale_update,
            store,
            task,
            ui_state,
        )

    (
        application,
        callback_update,
        session,
        stale_token,
        stale_update,
        store,
        task,
        ui_state,
    ) = run(scenario())

    assert store.delete_history_calls == [(123, "session-1")]
    assert store.peek_session is None
    assert session.closed is True
    assert ui_state.get_pending_text_action(123) is None
    assert ui_state.resolve_agent_command(123, "old_status") is None
    assert ui_state.resolve_agent_command(123, "status") == "status"
    assert ui_state.get(stale_token) is None
    assert ui_state.context_bundle_chat_active(123, "codex", "default") is True
    assert ui_state.pop_media_group_messages(123, "group-1") == ()
    assert task.cancelled()
    assert [command.command for command in application.bot.set_my_commands_calls[0][0]] == [
        "status",
    ]

    edited_texts = [text for text, _ in callback_update.callback_query.message.edit_calls]
    assert edited_texts[0] == "Deleting session..."
    assert edited_texts[-1].startswith(
        "Deleted session. Old bot buttons and pending inputs tied to that session were cleared.\n"
        "Session history for Codex in Default Workspace"
    )
    assert stale_update.callback_query.answers == [("This button has expired.", True)]


def test_session_history_run_failure_shows_failure_text():
    from talk2agent.bots.telegram_bot import (
        BUTTON_SESSION_HISTORY,
        TelegramUiState,
        handle_callback_query,
        handle_text,
    )

    ui_state = TelegramUiState()
    update = FakeUpdate(user_id=123, text=BUTTON_SESSION_HISTORY)
    services, _ = make_services(
        provider="codex",
        history_entries=[build_history_entry("session-1", "First")],
        activate_history_error=RuntimeError("boom"),
    )

    run(handle_text(update, None, services, ui_state))

    run_button = update.message.reply_markups[0].inline_keyboard[0][0]
    callback_update = FakeCallbackUpdate(123, run_button.callback_data, message=FakeIncomingMessage("history"))

    run(handle_callback_query(callback_update, None, services, ui_state))

    assert [text for text, _ in callback_update.callback_query.message.edit_calls] == [
        "Switching to session...",
        "Failed to switch session.",
    ]


def test_session_history_run_clears_session_bound_interactions_and_syncs_commands():
    from talk2agent.bots.telegram_bot import (
        BUTTON_SESSION_HISTORY,
        CALLBACK_PREFIX,
        TelegramUiState,
        _ContextBundleItem,
        handle_callback_query,
        handle_text,
    )

    ui_state = TelegramUiState()
    ui_state.set_pending_text_action(123, "workspace_search")
    ui_state.add_context_item(
        123,
        "codex",
        "default",
        _ContextBundleItem(kind="file", relative_path="notes.txt"),
    )
    assert ui_state.enable_context_bundle_chat(123, "codex", "default") is True
    stale_token = ui_state.create(123, "workspace_page", relative_path="", page=0)

    session = FakeSession(
        session_id="session-live",
        available_commands=[FakeCommand("model", "Switch model", hint="model id")],
    )
    update = FakeUpdate(user_id=123, text=BUTTON_SESSION_HISTORY)
    application = FakeApplication()
    services, store = make_services(
        provider="codex",
        session=session,
        history_entries=[build_history_entry("session-1", "First")],
    )

    run(handle_text(update, None, services, ui_state))

    run_button = update.message.reply_markups[0].inline_keyboard[0][0]
    callback_update = FakeCallbackUpdate(123, run_button.callback_data, message=FakeIncomingMessage("history"))
    run(handle_callback_query(callback_update, make_context(application=application), services, ui_state))

    assert store.activate_history_calls == [(123, "session-1")]
    assert ui_state.get_pending_text_action(123) is None
    assert ui_state.context_bundle_chat_active(123, "codex", "default") is True
    assert [command.command for command in application.bot.set_my_commands_calls[0][0]] == ["model"]
    assert callback_update.callback_query.message.edit_calls[-1][0] == (
        "Switched to session session-1 on Codex in Default Workspace. "
        "Old bot buttons and pending inputs tied to the previous session were cleared."
    )

    stale_update = FakeCallbackUpdate(123, f"{CALLBACK_PREFIX}{stale_token}", message=FakeIncomingMessage("stale"))
    run(handle_callback_query(stale_update, None, services, ui_state))
    assert stale_update.callback_query.answers == [("This button has expired.", True)]


def test_session_history_detail_from_reply_keyboard_can_open_and_back():
    from talk2agent.bots.telegram_bot import (
        BUTTON_SESSION_HISTORY,
        TelegramUiState,
        handle_callback_query,
        handle_text,
    )

    ui_state = TelegramUiState()
    update = FakeUpdate(user_id=123, text=BUTTON_SESSION_HISTORY)
    services, _ = make_services(
        provider="codex",
        history_entries=[build_history_entry("session-1", "First")],
    )

    run(handle_text(update, None, services, ui_state))

    callback_message = FakeIncomingMessage("history")
    open_button = find_inline_button(update.message.reply_markups[0], "Open 1")
    open_update = FakeCallbackUpdate(123, open_button.callback_data, message=callback_message)
    run(handle_callback_query(open_update, None, services, ui_state))

    detail_text, detail_markup = callback_message.edit_calls[-1]
    assert detail_text.startswith("Session history entry for Codex in Default Workspace")
    assert find_inline_button(detail_markup, "Back to History")

    back_button = find_inline_button(detail_markup, "Back to History")
    back_update = FakeCallbackUpdate(123, back_button.callback_data, message=callback_message)
    run(handle_callback_query(back_update, None, services, ui_state))

    history_text, history_markup = callback_message.edit_calls[-1]
    assert history_text.startswith("Session history for Codex in Default Workspace")
    assert find_inline_button(history_markup, "Open 1")


def test_session_history_marks_current_session_and_uses_noop_button():
    from talk2agent.bots.telegram_bot import BUTTON_SESSION_HISTORY, TelegramUiState, handle_callback_query, handle_text

    ui_state = TelegramUiState()
    session = FakeSession(session_id="session-1")
    update = FakeUpdate(user_id=123, text=BUTTON_SESSION_HISTORY)
    services, _ = make_services(
        provider="codex",
        session=session,
        history_entries=[build_history_entry("session-1", "First")],
    )

    run(handle_text(update, None, services, ui_state))

    assert "[current]" in update.message.reply_calls[0]
    current_button = update.message.reply_markups[0].inline_keyboard[0][0]
    callback_update = FakeCallbackUpdate(123, current_button.callback_data, message=FakeIncomingMessage("history"))

    run(handle_callback_query(callback_update, None, services, ui_state))

    assert callback_update.callback_query.answers == [("Already using this session.", False)]
    assert callback_update.callback_query.message.edit_calls == []


def test_session_history_detail_marks_current_session():
    from talk2agent.bots.telegram_bot import (
        BUTTON_SESSION_HISTORY,
        TelegramUiState,
        handle_callback_query,
        handle_text,
    )

    ui_state = TelegramUiState()
    session = FakeSession(session_id="session-1")
    update = FakeUpdate(user_id=123, text=BUTTON_SESSION_HISTORY)
    services, _ = make_services(
        provider="codex",
        session=session,
        history_entries=[build_history_entry("session-1", "First")],
    )

    run(handle_text(update, None, services, ui_state))

    callback_message = FakeIncomingMessage("history")
    open_button = find_inline_button(update.message.reply_markups[0], "Open 1")
    open_update = FakeCallbackUpdate(123, open_button.callback_data, message=callback_message)
    run(handle_callback_query(open_update, None, services, ui_state))

    detail_text, detail_markup = callback_message.edit_calls[-1]
    assert "Current runtime session: yes" in detail_text
    current_button = find_inline_button(detail_markup, "Current Session")

    current_update = FakeCallbackUpdate(123, current_button.callback_data, message=callback_message)
    run(handle_callback_query(current_update, None, services, ui_state))

    assert current_update.callback_query.answers == [("Already using this session.", False)]


def test_session_history_shows_run_retry_button_when_last_turn_exists():
    from talk2agent.acp.agent_session import PromptText
    from talk2agent.bots.telegram_bot import BUTTON_SESSION_HISTORY, TelegramUiState, _ReplayTurn, handle_text

    ui_state = TelegramUiState()
    ui_state.set_last_turn(
        123,
        _ReplayTurn(
            provider="codex",
            workspace_id="default",
            prompt_items=(PromptText("hello"),),
            title_hint="hello",
        ),
    )
    update = FakeUpdate(user_id=123, text=BUTTON_SESSION_HISTORY)
    services, _ = make_services(
        provider="codex",
        history_entries=[build_history_entry("session-1", "First")],
    )

    run(handle_text(update, None, services, ui_state))

    assert find_inline_button(update.message.reply_markups[0], "Run+Retry 1").text == "Run+Retry 1"


def test_session_history_run_retry_switches_session_and_replays_last_turn():
    from talk2agent.acp.agent_session import PromptText
    from talk2agent.bots.telegram_bot import (
        BUTTON_SESSION_HISTORY,
        TelegramUiState,
        _ReplayTurn,
        handle_callback_query,
        handle_text,
    )

    ui_state = TelegramUiState()
    ui_state.set_last_turn(
        123,
        _ReplayTurn(
            provider="codex",
            workspace_id="default",
            prompt_items=(PromptText("hello"),),
            title_hint="hello",
        ),
    )
    session = FakeSession(
        session_id="session-live",
        available_commands=[FakeCommand("model", "Switch model", hint="model id")],
    )
    update = FakeUpdate(user_id=123, text=BUTTON_SESSION_HISTORY)
    callback_message = FakeIncomingMessage("history")
    services, store = make_services(
        provider="codex",
        session=session,
        history_entries=[build_history_entry("session-1", "First")],
    )

    run(handle_text(update, None, services, ui_state))

    retry_button = find_inline_button(update.message.reply_markups[0], "Run+Retry 1")
    callback_update = FakeCallbackUpdate(123, retry_button.callback_data, message=callback_message)
    run(handle_callback_query(callback_update, make_context(application=FakeApplication()), services, ui_state))

    assert store.activate_history_calls == [(123, "session-1")]
    assert [text for text, _ in callback_message.edit_calls] == [
        "Switching to session...",
        (
            "Switched to session session-1 on Codex in Default Workspace. "
            "Old bot buttons and pending inputs tied to the previous session were cleared.\n"
            "Retrying last turn in this session..."
        ),
    ]
    assert session.prompt_items == [(PromptText("hello"),)]
    assert callback_message.reply_calls == ["hello world"]
    assert store.record_session_usage_calls == [(123, "session-1", "hello")]


def test_session_history_shows_fork_buttons_when_provider_supports_fork():
    from talk2agent.bots.telegram_bot import BUTTON_SESSION_HISTORY, TelegramUiState, handle_text

    update = FakeUpdate(user_id=123, text=BUTTON_SESSION_HISTORY)
    services, _ = make_services(
        provider="codex",
        history_entries=[build_history_entry("session-1", "First")],
    )

    run(handle_text(update, None, services, TelegramUiState()))

    assert find_inline_button(update.message.reply_markups[0], "Fork 1").text == "Fork 1"


def test_session_history_fork_refreshes_history_view_and_syncs_commands():
    from talk2agent.bots.telegram_bot import (
        BUTTON_SESSION_HISTORY,
        TelegramUiState,
        handle_callback_query,
        handle_text,
    )

    ui_state = TelegramUiState()
    ui_state.set_pending_text_action(123, "workspace_search")
    session = FakeSession(
        session_id="session-live",
        available_commands=[FakeCommand("model", "Switch model", hint="model id")],
    )
    update = FakeUpdate(user_id=123, text=BUTTON_SESSION_HISTORY)
    application = FakeApplication()
    services, store = make_services(
        provider="codex",
        session=session,
        history_entries=[build_history_entry("session-1", "First")],
    )

    run(handle_text(update, None, services, ui_state))

    fork_button = find_inline_button(update.message.reply_markups[0], "Fork 1")
    callback_update = FakeCallbackUpdate(123, fork_button.callback_data, message=FakeIncomingMessage("history"))
    run(handle_callback_query(callback_update, make_context(application=application), services, ui_state))

    assert store.fork_history_calls == [(123, "session-1")]
    assert store.record_session_usage_calls == [(123, "fork-session-1", None)]
    assert ui_state.get_pending_text_action(123) is None
    assert [command.command for command in application.bot.set_my_commands_calls[0][0]] == ["model"]
    final_text, final_markup = callback_update.callback_query.message.edit_calls[-1]
    assert final_text.startswith(
        "Forked session fork-session-1 from session-1 on Codex in Default Workspace. "
        "Old bot buttons and pending inputs tied to the previous session were cleared.\n"
        "Session history for Codex in Default Workspace"
    )
    assert "[current]" in final_text
    assert find_inline_button(final_markup, "Current 1")


def test_session_history_fork_retry_switches_session_and_replays_last_turn():
    from talk2agent.acp.agent_session import PromptText
    from talk2agent.bots.telegram_bot import (
        BUTTON_SESSION_HISTORY,
        TelegramUiState,
        _ReplayTurn,
        handle_callback_query,
        handle_text,
    )

    ui_state = TelegramUiState()
    ui_state.set_last_turn(
        123,
        _ReplayTurn(
            provider="codex",
            workspace_id="default",
            prompt_items=(PromptText("hello"),),
            title_hint="hello",
        ),
    )
    session = FakeSession(
        session_id="session-live",
        available_commands=[FakeCommand("model", "Switch model", hint="model id")],
    )
    update = FakeUpdate(user_id=123, text=BUTTON_SESSION_HISTORY)
    callback_message = FakeIncomingMessage("history")
    services, store = make_services(
        provider="codex",
        session=session,
        history_entries=[build_history_entry("session-1", "First")],
    )

    run(handle_text(update, None, services, ui_state))

    retry_button = find_inline_button(update.message.reply_markups[0], "Fork+Retry 1")
    callback_update = FakeCallbackUpdate(123, retry_button.callback_data, message=callback_message)
    run(handle_callback_query(callback_update, make_context(application=FakeApplication()), services, ui_state))

    assert store.fork_history_calls == [(123, "session-1")]
    assert [text for text, _ in callback_message.edit_calls[:2]] == [
        "Forking session...",
        (
            "Forked session fork-session-1 from session-1 on Codex in Default Workspace. "
            "Old bot buttons and pending inputs tied to the previous session were cleared.\n"
            "Retrying last turn in this session..."
        ),
    ]
    assert session.prompt_items == [(PromptText("hello"),)]
    assert callback_message.reply_calls == ["hello world"]
    assert store.record_session_usage_calls == [
        (123, "fork-session-1", None),
        (123, "fork-session-1", "hello"),
    ]
    final_text, final_markup = callback_message.edit_calls[-1]
    assert final_text.startswith(
        "Forked session fork-session-1 from session-1 on Codex in Default Workspace. "
        "Old bot buttons and pending inputs tied to the previous session were cleared.\n"
        "Retried last turn in this session.\n"
        "Session history for Codex in Default Workspace"
    )
    assert find_inline_button(final_markup, "Current 1")


def test_session_history_rename_uses_next_text_message():
    from talk2agent.bots.telegram_bot import BUTTON_SESSION_HISTORY, TelegramUiState, handle_callback_query, handle_text

    ui_state = TelegramUiState()
    history_entries = [build_history_entry("session-1", "First")]
    update = FakeUpdate(user_id=123, text=BUTTON_SESSION_HISTORY)
    services, store = make_services(provider="codex", history_entries=history_entries)

    run(handle_text(update, None, services, ui_state))

    rename_button = update.message.reply_markups[0].inline_keyboard[0][1]
    callback_update = FakeCallbackUpdate(123, rename_button.callback_data, message=FakeIncomingMessage("history"))
    run(handle_callback_query(callback_update, None, services, ui_state))

    assert callback_update.callback_query.message.edit_calls[-1][0].startswith(
        "Send the new session title as your next plain text message."
    )

    rename_update = FakeUpdate(user_id=123, text="Renamed Session")
    run(handle_text(rename_update, None, services, ui_state))

    assert store.rename_history_calls == [(123, "session-1", "Renamed Session")]
    assert rename_update.message.reply_calls[0].startswith(
        "Renamed session.\nSession history for Codex in Default Workspace"
    )
    assert "Renamed Session" in rename_update.message.reply_calls[0]


def test_session_history_shows_provider_sessions_button_for_admin():
    from talk2agent.bots.telegram_bot import BUTTON_SESSION_HISTORY, TelegramUiState, handle_text

    update = FakeUpdate(user_id=123, text=BUTTON_SESSION_HISTORY)
    services, _ = make_services(
        history_entries=[build_history_entry("session-1", "First")],
    )

    run(handle_text(update, None, services, TelegramUiState()))

    assert find_inline_button(update.message.reply_markups[0], "Provider Sessions").text == "Provider Sessions"


def test_session_history_hides_provider_sessions_button_for_non_admin():
    from talk2agent.bots.telegram_bot import BUTTON_SESSION_HISTORY, TelegramUiState, handle_text

    update = FakeUpdate(user_id=456, text=BUTTON_SESSION_HISTORY)
    services, _ = make_services(
        allowed_user_ids={123, 456},
        admin_user_id=123,
        history_entries=[build_history_entry("session-1", "First")],
    )

    run(handle_text(update, None, services, TelegramUiState()))

    labels = [button.text for row in update.message.reply_markups[0].inline_keyboard for button in row]
    assert "Provider Sessions" not in labels


def test_provider_sessions_can_be_browsed_and_attached_from_history():
    from talk2agent.bots.telegram_bot import (
        BUTTON_SESSION_HISTORY,
        CALLBACK_PREFIX,
        TelegramUiState,
        _ContextBundleItem,
        handle_callback_query,
        handle_text,
    )

    provider_page = SimpleNamespace(
        entries=(
            build_provider_session("desktop-session", "Desktop Flow", cwd_label="src"),
        ),
        next_cursor=None,
        supported=True,
    )
    update = FakeUpdate(user_id=123, text=BUTTON_SESSION_HISTORY)
    application = FakeApplication()
    services, store = make_services(
        provider="codex",
        session=FakeSession(available_commands=[FakeCommand("status", "Show status")]),
        history_entries=[build_history_entry("session-1", "First")],
        provider_session_pages={None: provider_page},
    )
    ui_state = TelegramUiState()
    ui_state.set_pending_text_action(123, "workspace_search")
    ui_state.add_context_item(
        123,
        "codex",
        "default",
        _ContextBundleItem(kind="file", relative_path="notes.txt"),
    )
    assert ui_state.enable_context_bundle_chat(123, "codex", "default") is True
    stale_token = ui_state.create(123, "workspace_page", relative_path="", page=0)

    run(handle_text(update, None, services, ui_state))

    provider_button = find_inline_button(update.message.reply_markups[0], "Provider Sessions")
    provider_update = FakeCallbackUpdate(123, provider_button.callback_data, message=FakeIncomingMessage("history"))
    run(handle_callback_query(provider_update, None, services, ui_state))

    assert provider_update.callback_query.message.edit_calls[-1][0].startswith(
        "Provider sessions for Codex in Default Workspace"
    )
    assert "Desktop Flow" in provider_update.callback_query.message.edit_calls[-1][0]
    assert "cwd=src" in provider_update.callback_query.message.edit_calls[-1][0]

    run_button = find_inline_button(provider_update.callback_query.message.edit_calls[-1][1], "Run 1")
    run_update = FakeCallbackUpdate(123, run_button.callback_data, message=provider_update.callback_query.message)
    run(handle_callback_query(run_update, make_context(application=application), services, ui_state))

    assert store.activate_provider_calls == [(123, "desktop-session", "Desktop Flow")]
    assert ui_state.get_pending_text_action(123) is None
    assert ui_state.context_bundle_chat_active(123, "codex", "default") is True
    assert [command.command for command in application.bot.set_my_commands_calls[0][0]] == ["status"]
    final_text = run_update.callback_query.message.edit_calls[-1][0]
    assert final_text.startswith(
        "Switched to provider session desktop-session. "
        "Old bot buttons and pending inputs tied to the previous session were cleared."
    )
    assert "Desktop Flow [current]" in final_text

    stale_update = FakeCallbackUpdate(123, f"{CALLBACK_PREFIX}{stale_token}", message=FakeIncomingMessage("stale"))
    run(handle_callback_query(stale_update, None, services, ui_state))
    assert stale_update.callback_query.answers == [("This button has expired.", True)]


def test_provider_session_detail_from_history_keeps_back_chain_to_status():
    from talk2agent.bots.telegram_bot import (
        BUTTON_BOT_STATUS,
        TelegramUiState,
        handle_callback_query,
        handle_text,
    )

    provider_page = SimpleNamespace(
        entries=(build_provider_session("desktop-session", "Desktop Flow", cwd_label="src"),),
        next_cursor=None,
        supported=True,
    )
    ui_state = TelegramUiState()
    update = FakeUpdate(user_id=123, text=BUTTON_BOT_STATUS)
    services, _ = make_services(
        provider="codex",
        history_entries=[build_history_entry("session-1", "First")],
        provider_session_pages={None: provider_page},
    )

    run(handle_text(update, None, services, ui_state))

    callback_message = FakeIncomingMessage("status")
    history_button = find_inline_button(update.message.reply_markups[0], "Session History")
    history_update = FakeCallbackUpdate(123, history_button.callback_data, message=callback_message)
    run(handle_callback_query(history_update, None, services, ui_state))

    provider_button = find_inline_button(callback_message.edit_calls[-1][1], "Provider Sessions")
    provider_update = FakeCallbackUpdate(123, provider_button.callback_data, message=callback_message)
    run(handle_callback_query(provider_update, None, services, ui_state))

    open_button = find_inline_button(callback_message.edit_calls[-1][1], "Open 1")
    open_update = FakeCallbackUpdate(123, open_button.callback_data, message=callback_message)
    run(handle_callback_query(open_update, None, services, ui_state))

    detail_text, detail_markup = callback_message.edit_calls[-1]
    assert detail_text.startswith("Provider session for Codex in Default Workspace")
    back_to_provider_button = find_inline_button(detail_markup, "Back to Provider Sessions")

    back_to_provider_update = FakeCallbackUpdate(
        123,
        back_to_provider_button.callback_data,
        message=callback_message,
    )
    run(handle_callback_query(back_to_provider_update, None, services, ui_state))

    provider_text, provider_markup = callback_message.edit_calls[-1]
    assert provider_text.startswith("Provider sessions for Codex in Default Workspace")
    back_to_history_button = find_inline_button(provider_markup, "Back to History")

    back_to_history_update = FakeCallbackUpdate(
        123,
        back_to_history_button.callback_data,
        message=callback_message,
    )
    run(handle_callback_query(back_to_history_update, None, services, ui_state))

    history_text, history_markup = callback_message.edit_calls[-1]
    assert history_text.startswith("Session history for Codex in Default Workspace")
    back_to_status_button = find_inline_button(history_markup, "Back to Bot Status")

    back_to_status_update = FakeCallbackUpdate(
        123,
        back_to_status_button.callback_data,
        message=callback_message,
    )
    run(handle_callback_query(back_to_status_update, None, services, ui_state))

    status_text, status_markup = callback_message.edit_calls[-1]
    assert status_text.startswith("Bot status for Codex in Default Workspace")
    assert find_inline_button(status_markup, "Provider Sessions")


def test_provider_sessions_show_run_retry_button_when_last_turn_exists():
    from talk2agent.acp.agent_session import PromptText
    from talk2agent.bots.telegram_bot import (
        BUTTON_SESSION_HISTORY,
        TelegramUiState,
        _ReplayTurn,
        handle_callback_query,
        handle_text,
    )

    provider_page = SimpleNamespace(
        entries=(build_provider_session("desktop-session", "Desktop Flow", cwd_label="src"),),
        next_cursor=None,
        supported=True,
    )
    ui_state = TelegramUiState()
    ui_state.set_last_turn(
        123,
        _ReplayTurn(
            provider="codex",
            workspace_id="default",
            prompt_items=(PromptText("hello"),),
            title_hint="hello",
        ),
    )
    update = FakeUpdate(user_id=123, text=BUTTON_SESSION_HISTORY)
    services, _ = make_services(
        provider="codex",
        history_entries=[build_history_entry("session-1", "First")],
        provider_session_pages={None: provider_page},
    )

    run(handle_text(update, None, services, ui_state))

    provider_button = find_inline_button(update.message.reply_markups[0], "Provider Sessions")
    provider_update = FakeCallbackUpdate(123, provider_button.callback_data, message=FakeIncomingMessage("history"))
    run(handle_callback_query(provider_update, None, services, ui_state))

    assert find_inline_button(provider_update.callback_query.message.edit_calls[-1][1], "Run+Retry 1").text == "Run+Retry 1"


def test_provider_session_run_retry_switches_session_and_replays_last_turn():
    from talk2agent.acp.agent_session import PromptText
    from talk2agent.bots.telegram_bot import (
        BUTTON_SESSION_HISTORY,
        TelegramUiState,
        _ReplayTurn,
        handle_callback_query,
        handle_text,
    )

    provider_page = SimpleNamespace(
        entries=(build_provider_session("desktop-session", "Desktop Flow", cwd_label="src"),),
        next_cursor=None,
        supported=True,
    )
    ui_state = TelegramUiState()
    ui_state.set_last_turn(
        123,
        _ReplayTurn(
            provider="codex",
            workspace_id="default",
            prompt_items=(PromptText("hello"),),
            title_hint="hello",
        ),
    )
    session = FakeSession(
        session_id="session-live",
        available_commands=[FakeCommand("status", "Show status")],
    )
    update = FakeUpdate(user_id=123, text=BUTTON_SESSION_HISTORY)
    callback_message = FakeIncomingMessage("history")
    services, store = make_services(
        provider="codex",
        session=session,
        history_entries=[build_history_entry("session-1", "First")],
        provider_session_pages={None: provider_page},
    )

    run(handle_text(update, None, services, ui_state))

    provider_button = find_inline_button(update.message.reply_markups[0], "Provider Sessions")
    provider_update = FakeCallbackUpdate(123, provider_button.callback_data, message=callback_message)
    run(handle_callback_query(provider_update, None, services, ui_state))

    retry_button = find_inline_button(provider_update.callback_query.message.edit_calls[-1][1], "Run+Retry 1")
    retry_update = FakeCallbackUpdate(123, retry_button.callback_data, message=callback_message)
    run(handle_callback_query(retry_update, make_context(application=FakeApplication()), services, ui_state))

    assert store.activate_provider_calls == [(123, "desktop-session", "Desktop Flow")]
    assert [text for text, _ in callback_message.edit_calls[-2:]] == [
        "Switching to provider session...",
        (
            "Switched to provider session desktop-session. "
            "Old bot buttons and pending inputs tied to the previous session were cleared.\n"
            "Retrying last turn in this session..."
        ),
    ]
    assert session.prompt_items == [(PromptText("hello"),)]
    assert callback_message.reply_calls == ["hello world"]
    assert store.record_session_usage_calls == [(123, "desktop-session", "hello")]


def test_provider_sessions_show_fork_buttons_when_provider_supports_fork():
    from talk2agent.bots.telegram_bot import (
        BUTTON_SESSION_HISTORY,
        TelegramUiState,
        handle_callback_query,
        handle_text,
    )

    provider_page = SimpleNamespace(
        entries=(build_provider_session("desktop-session", "Desktop Flow", cwd_label="src"),),
        next_cursor=None,
        supported=True,
    )
    update = FakeUpdate(user_id=123, text=BUTTON_SESSION_HISTORY)
    ui_state = TelegramUiState()
    services, _ = make_services(
        provider="codex",
        history_entries=[build_history_entry("session-1", "First")],
        provider_session_pages={None: provider_page},
    )

    run(handle_text(update, None, services, ui_state))

    provider_button = find_inline_button(update.message.reply_markups[0], "Provider Sessions")
    provider_update = FakeCallbackUpdate(123, provider_button.callback_data, message=FakeIncomingMessage("history"))
    run(handle_callback_query(provider_update, None, services, ui_state))

    assert find_inline_button(provider_update.callback_query.message.edit_calls[-1][1], "Fork 1").text == "Fork 1"


def test_provider_session_fork_refreshes_provider_view():
    from talk2agent.bots.telegram_bot import (
        BUTTON_SESSION_HISTORY,
        TelegramUiState,
        handle_callback_query,
        handle_text,
    )

    provider_page = SimpleNamespace(
        entries=(build_provider_session("desktop-session", "Desktop Flow", cwd_label="src"),),
        next_cursor=None,
        supported=True,
    )
    ui_state = TelegramUiState()
    ui_state.set_pending_text_action(123, "workspace_search")
    update = FakeUpdate(user_id=123, text=BUTTON_SESSION_HISTORY)
    application = FakeApplication()
    services, store = make_services(
        provider="codex",
        session=FakeSession(available_commands=[FakeCommand("status", "Show status")]),
        history_entries=[build_history_entry("session-1", "First")],
        provider_session_pages={None: provider_page},
    )

    run(handle_text(update, None, services, ui_state))

    provider_button = find_inline_button(update.message.reply_markups[0], "Provider Sessions")
    callback_message = FakeIncomingMessage("history")
    provider_update = FakeCallbackUpdate(123, provider_button.callback_data, message=callback_message)
    run(handle_callback_query(provider_update, None, services, ui_state))

    fork_button = find_inline_button(callback_message.edit_calls[-1][1], "Fork 1")
    fork_update = FakeCallbackUpdate(123, fork_button.callback_data, message=callback_message)
    run(handle_callback_query(fork_update, make_context(application=application), services, ui_state))

    assert store.fork_provider_calls == [(123, "desktop-session", "Desktop Flow")]
    assert store.record_session_usage_calls == [(123, "fork-desktop-session", "Desktop Flow")]
    assert ui_state.get_pending_text_action(123) is None
    assert [command.command for command in application.bot.set_my_commands_calls[0][0]] == ["status"]
    final_text, final_markup = callback_message.edit_calls[-1]
    assert final_text.startswith(
        "Forked provider session desktop-session into fork-desktop-session. "
        "Old bot buttons and pending inputs tied to the previous session were cleared.\n"
        "Provider sessions for Codex in Default Workspace"
    )
    assert find_inline_button(final_markup, "Fork 1")


def test_provider_session_fork_retry_switches_session_and_replays_last_turn():
    from talk2agent.acp.agent_session import PromptText
    from talk2agent.bots.telegram_bot import (
        BUTTON_SESSION_HISTORY,
        TelegramUiState,
        _ReplayTurn,
        handle_callback_query,
        handle_text,
    )

    provider_page = SimpleNamespace(
        entries=(build_provider_session("desktop-session", "Desktop Flow", cwd_label="src"),),
        next_cursor=None,
        supported=True,
    )
    ui_state = TelegramUiState()
    ui_state.set_last_turn(
        123,
        _ReplayTurn(
            provider="codex",
            workspace_id="default",
            prompt_items=(PromptText("hello"),),
            title_hint="hello",
        ),
    )
    session = FakeSession(
        session_id="session-live",
        available_commands=[FakeCommand("status", "Show status")],
    )
    update = FakeUpdate(user_id=123, text=BUTTON_SESSION_HISTORY)
    callback_message = FakeIncomingMessage("history")
    services, store = make_services(
        provider="codex",
        session=session,
        history_entries=[build_history_entry("session-1", "First")],
        provider_session_pages={None: provider_page},
    )

    run(handle_text(update, None, services, ui_state))

    provider_button = find_inline_button(update.message.reply_markups[0], "Provider Sessions")
    provider_update = FakeCallbackUpdate(123, provider_button.callback_data, message=callback_message)
    run(handle_callback_query(provider_update, None, services, ui_state))

    retry_button = find_inline_button(provider_update.callback_query.message.edit_calls[-1][1], "Fork+Retry 1")
    retry_update = FakeCallbackUpdate(123, retry_button.callback_data, message=callback_message)
    run(handle_callback_query(retry_update, make_context(application=FakeApplication()), services, ui_state))

    assert store.fork_provider_calls == [(123, "desktop-session", "Desktop Flow")]
    assert [text for text, _ in callback_message.edit_calls[-3:-1]] == [
        "Forking provider session...",
        (
            "Forked provider session desktop-session into fork-desktop-session. "
            "Old bot buttons and pending inputs tied to the previous session were cleared.\n"
            "Retrying last turn in this session..."
        ),
    ]
    assert session.prompt_items == [(PromptText("hello"),)]
    assert callback_message.reply_calls == ["hello world"]
    assert store.record_session_usage_calls == [
        (123, "fork-desktop-session", "Desktop Flow"),
        (123, "fork-desktop-session", "hello"),
    ]
    final_text, final_markup = callback_message.edit_calls[-1]
    assert final_text.startswith(
        "Forked provider session desktop-session into fork-desktop-session. "
        "Old bot buttons and pending inputs tied to the previous session were cleared.\n"
        "Retried last turn in this session.\n"
        "Provider sessions for Codex in Default Workspace"
    )
    assert find_inline_button(final_markup, "Fork+Retry 1")


def test_provider_sessions_show_unsupported_message():
    from talk2agent.bots.telegram_bot import BUTTON_SESSION_HISTORY, TelegramUiState, handle_callback_query, handle_text

    update = FakeUpdate(user_id=123, text=BUTTON_SESSION_HISTORY)
    services, _ = make_services(
        history_entries=[build_history_entry("session-1", "First")],
        provider_session_pages={
            None: SimpleNamespace(entries=tuple(), next_cursor=None, supported=False),
        },
    )
    ui_state = TelegramUiState()

    run(handle_text(update, None, services, ui_state))

    provider_button = find_inline_button(update.message.reply_markups[0], "Provider Sessions")
    provider_update = FakeCallbackUpdate(123, provider_button.callback_data, message=FakeIncomingMessage("history"))
    run(handle_callback_query(provider_update, None, services, ui_state))

    assert provider_update.callback_query.message.edit_calls[-1][0].startswith(
        "Provider sessions for Claude Code in Default Workspace"
    )
    assert "Provider session browsing is not available for this agent." in provider_update.callback_query.message.edit_calls[-1][0]


def test_agent_commands_button_shows_discovered_commands_without_live_session():
    from talk2agent.bots.telegram_bot import BUTTON_AGENT_COMMANDS, TelegramUiState, handle_text

    session = FakeSession(
        available_commands=[
            FakeCommand("status", "Show status"),
            FakeCommand("model", "Switch model", hint="model id"),
        ]
    )
    update = FakeUpdate(user_id=123, text=BUTTON_AGENT_COMMANDS)
    services, store = make_services(session=session, peek_session=None)

    run(handle_text(update, None, services, TelegramUiState()))

    assert store.peek_calls == [123]
    assert services.discover_agent_commands_calls == [2.0]
    assert update.message.reply_calls[0].startswith(
        "Agent commands for Claude Code in Default Workspace\nSession: none"
    )
    assert "/status" in update.message.reply_calls[0]
    assert "args: model id" in update.message.reply_calls[0]


def test_agent_commands_detail_can_open_without_live_session_and_back():
    from talk2agent.bots.telegram_bot import BUTTON_AGENT_COMMANDS, TelegramUiState, handle_callback_query, handle_text

    session = FakeSession(
        available_commands=[
            FakeCommand("status", "Show status"),
            FakeCommand("model", "Switch model", hint="model id"),
        ]
    )
    update = FakeUpdate(user_id=123, text=BUTTON_AGENT_COMMANDS)
    services, _ = make_services(session=session, peek_session=None)
    ui_state = TelegramUiState()

    run(handle_text(update, None, services, ui_state))

    callback_message = FakeIncomingMessage("commands")
    open_button = find_inline_button(update.message.reply_markups[0], "Open 2")
    open_update = FakeCallbackUpdate(123, open_button.callback_data, message=callback_message)
    run(handle_callback_query(open_update, None, services, ui_state))

    detail_text, detail_markup = callback_message.edit_calls[-1]
    assert detail_text.startswith("Agent command for Claude Code in Default Workspace")
    assert "Command: 2/2" in detail_text
    assert "Session: none (will start on first command)" in detail_text
    assert "Name: /model" in detail_text
    assert "Args hint: model id" in detail_text
    assert "Example: /model <args>" in detail_text
    assert find_inline_button(detail_markup, "Back to Agent Commands")

    back_button = find_inline_button(detail_markup, "Back to Agent Commands")
    back_update = FakeCallbackUpdate(123, back_button.callback_data, message=callback_message)
    run(handle_callback_query(back_update, None, services, ui_state))

    restored_text, restored_markup = callback_message.edit_calls[-1]
    assert restored_text.startswith("Agent commands for Claude Code in Default Workspace")
    assert find_inline_button(restored_markup, "Open 2")


def test_bot_status_agent_commands_can_open_and_back_to_status():
    from talk2agent.bots.telegram_bot import (
        BUTTON_BOT_STATUS,
        TelegramUiState,
        handle_callback_query,
        handle_text,
    )

    session = FakeSession(
        available_commands=[
            FakeCommand("status", "Show status"),
            FakeCommand("model", "Switch model", hint="model id"),
        ]
    )
    ui_state = TelegramUiState()
    update = FakeUpdate(user_id=123, text=BUTTON_BOT_STATUS)
    services, _ = make_services(provider="codex", session=session)

    run(handle_text(update, None, services, ui_state))

    callback_message = FakeIncomingMessage("status")
    commands_button = find_inline_button(update.message.reply_markups[0], "Agent Commands")
    commands_update = FakeCallbackUpdate(123, commands_button.callback_data, message=callback_message)
    run(handle_callback_query(commands_update, None, services, ui_state))

    commands_text, commands_markup = callback_message.edit_calls[-1]
    assert commands_text.startswith("Agent commands for Codex in Default Workspace")
    assert find_inline_button(commands_markup, "Back to Bot Status")

    back_update = FakeCallbackUpdate(
        123,
        find_inline_button(commands_markup, "Back to Bot Status").callback_data,
        message=callback_message,
    )
    run(handle_callback_query(back_update, None, services, ui_state))

    restored_text, restored_markup = callback_message.edit_calls[-1]
    assert restored_text.startswith("Bot status for Codex in Default Workspace")
    assert find_inline_button(restored_markup, "Agent Commands")


def test_bot_status_agent_command_detail_shows_fields_and_back_to_status():
    from talk2agent.bots.telegram_bot import (
        BUTTON_BOT_STATUS,
        TelegramUiState,
        handle_callback_query,
        handle_text,
    )

    session = FakeSession(
        session_id="session-abc",
        available_commands=[
            FakeCommand("status", "Show status"),
            FakeCommand("model", "Switch model", hint="model id"),
        ],
    )
    ui_state = TelegramUiState()
    update = FakeUpdate(user_id=123, text=BUTTON_BOT_STATUS)
    services, _ = make_services(provider="codex", session=session)

    run(handle_text(update, None, services, ui_state))

    callback_message = FakeIncomingMessage("status")
    commands_button = find_inline_button(update.message.reply_markups[0], "Agent Commands")
    commands_update = FakeCallbackUpdate(123, commands_button.callback_data, message=callback_message)
    run(handle_callback_query(commands_update, None, services, ui_state))

    open_button = find_inline_button(callback_message.edit_calls[-1][1], "Open 1")
    open_update = FakeCallbackUpdate(123, open_button.callback_data, message=callback_message)
    run(handle_callback_query(open_update, None, services, ui_state))

    detail_text, detail_markup = callback_message.edit_calls[-1]
    assert detail_text.startswith("Agent command for Codex in Default Workspace")
    assert "Command: 1/2" in detail_text
    assert "Session: session-abc" in detail_text
    assert "Name: /status" in detail_text
    assert "Description:" in detail_text
    assert "Show status" in detail_text
    assert "Args hint: none" in detail_text
    assert "Example: /status" in detail_text
    assert find_inline_button(detail_markup, "Run Command")

    back_button = find_inline_button(detail_markup, "Back to Agent Commands")
    back_update = FakeCallbackUpdate(123, back_button.callback_data, message=callback_message)
    run(handle_callback_query(back_update, None, services, ui_state))

    commands_text, commands_markup = callback_message.edit_calls[-1]
    assert commands_text.startswith("Agent commands for Codex in Default Workspace")
    back_to_status_button = find_inline_button(commands_markup, "Back to Bot Status")

    back_to_status_update = FakeCallbackUpdate(
        123,
        back_to_status_button.callback_data,
        message=callback_message,
    )
    run(handle_callback_query(back_to_status_update, None, services, ui_state))

    restored_text, restored_markup = callback_message.edit_calls[-1]
    assert restored_text.startswith("Bot status for Codex in Default Workspace")
    assert find_inline_button(restored_markup, "Agent Commands")


def test_bot_status_agent_commands_run_returns_to_status():
    from talk2agent.bots.telegram_bot import (
        BUTTON_BOT_STATUS,
        TelegramUiState,
        handle_callback_query,
        handle_text,
    )

    session = FakeSession(
        session_id="session-abc",
        available_commands=[FakeCommand("status", "Show status")],
    )
    ui_state = TelegramUiState()
    update = FakeUpdate(user_id=123, text=BUTTON_BOT_STATUS)
    callback_message = FakeIncomingMessage("status")
    services, store = make_services(provider="codex", session=session)

    run(handle_text(update, None, services, ui_state))

    commands_button = find_inline_button(update.message.reply_markups[0], "Agent Commands")
    commands_update = FakeCallbackUpdate(123, commands_button.callback_data, message=callback_message)
    run(handle_callback_query(commands_update, None, services, ui_state))

    run_button = find_inline_button(callback_message.edit_calls[-1][1], "Run 1")
    run_update = FakeCallbackUpdate(123, run_button.callback_data, message=callback_message)
    run(handle_callback_query(run_update, make_context(application=FakeApplication()), services, ui_state))

    assert session.prompts == ["/status"]
    assert callback_message.reply_calls == ["hello world"]
    assert store.record_session_usage_calls == [(123, "session-abc", "/status")]
    final_text, final_markup = callback_message.edit_calls[-1]
    assert final_text.startswith("Ran /status.\nBot status for Codex in Default Workspace")
    assert "Session: session-abc" in final_text
    assert find_inline_button(final_markup, "Agent Commands")


def test_bot_status_agent_commands_run_failure_restores_status():
    from talk2agent.bots.telegram_bot import (
        BUTTON_BOT_STATUS,
        TelegramUiState,
        handle_callback_query,
        handle_text,
    )

    session = FakeSession(
        session_id="session-abc",
        available_commands=[FakeCommand("status", "Show status")],
    )
    ui_state = TelegramUiState()
    update = FakeUpdate(user_id=123, text=BUTTON_BOT_STATUS)
    callback_message = FakeIncomingMessage("status")
    services, store = make_services(
        provider="codex",
        session=session,
        get_or_create_error=RuntimeError("boom"),
    )

    run(handle_text(update, None, services, ui_state))

    commands_button = find_inline_button(update.message.reply_markups[0], "Agent Commands")
    commands_update = FakeCallbackUpdate(123, commands_button.callback_data, message=callback_message)
    run(handle_callback_query(commands_update, None, services, ui_state))

    run_button = find_inline_button(callback_message.edit_calls[-1][1], "Run 1")
    run_update = FakeCallbackUpdate(123, run_button.callback_data, message=callback_message)
    run(handle_callback_query(run_update, None, services, ui_state))

    assert store.get_or_create_calls == [123]
    assert callback_message.reply_calls == []
    final_text, final_markup = callback_message.edit_calls[-1]
    assert final_text.startswith("Request failed.\nBot status for Codex in Default Workspace")
    assert find_inline_button(final_markup, "Agent Commands")


def test_bot_status_agent_commands_run_runtime_failure_restores_status():
    from talk2agent.bots.telegram_bot import (
        BUTTON_BOT_STATUS,
        TelegramUiState,
        handle_callback_query,
        handle_text,
    )

    session = FakeSession(
        session_id="session-abc",
        error=RuntimeError("boom"),
        raise_before_stream=True,
        available_commands=[FakeCommand("status", "Show status")],
    )
    session.prompts.append("already-started")
    ui_state = TelegramUiState()
    update = FakeUpdate(user_id=123, text=BUTTON_BOT_STATUS)
    callback_message = FakeIncomingMessage("status")
    services, store = make_services(provider="codex", session=session)

    run(handle_text(update, None, services, ui_state))

    commands_button = find_inline_button(update.message.reply_markups[0], "Agent Commands")
    commands_update = FakeCallbackUpdate(123, commands_button.callback_data, message=callback_message)
    run(handle_callback_query(commands_update, None, services, ui_state))

    run_button = find_inline_button(callback_message.edit_calls[-1][1], "Run 1")
    run_update = FakeCallbackUpdate(123, run_button.callback_data, message=callback_message)
    run(handle_callback_query(run_update, None, services, ui_state))

    assert store.invalidate_calls == [(123, session)]
    assert callback_message.reply_calls[0].startswith(
        "Request failed. The current live session for Codex in Default Workspace was closed."
    )
    final_text, final_markup = callback_message.edit_calls[-1]
    assert final_text.startswith("Request failed.\nBot status for Codex in Default Workspace")
    assert find_inline_button(final_markup, "Agent Commands")


def test_bot_status_agent_commands_run_with_args_returns_to_status():
    from talk2agent.bots.telegram_bot import (
        BUTTON_BOT_STATUS,
        TelegramUiState,
        handle_callback_query,
        handle_text,
    )

    session = FakeSession(
        session_id="session-abc",
        available_commands=[FakeCommand("model", "Switch model", hint="model id")],
    )
    ui_state = TelegramUiState()
    update = FakeUpdate(user_id=123, text=BUTTON_BOT_STATUS)
    callback_message = FakeIncomingMessage("status")
    services, store = make_services(provider="codex", session=session)

    run(handle_text(update, None, services, ui_state))

    commands_button = find_inline_button(update.message.reply_markups[0], "Agent Commands")
    commands_update = FakeCallbackUpdate(123, commands_button.callback_data, message=callback_message)
    run(handle_callback_query(commands_update, None, services, ui_state))

    args_button = callback_message.edit_calls[-1][1].inline_keyboard[0][0]
    args_update = FakeCallbackUpdate(123, args_button.callback_data, message=callback_message)
    run(handle_callback_query(args_update, None, services, ui_state))

    request_update = FakeUpdate(user_id=123, text="gpt-5.4-mini")
    run(handle_text(request_update, None, services, ui_state))

    assert session.prompts == ["/model gpt-5.4-mini"]
    assert request_update.message.reply_calls == ["hello world"]
    assert store.record_session_usage_calls == [(123, "session-abc", "/model gpt-5.4-mini")]
    final_text, final_markup = callback_message.edit_calls[-1]
    assert final_text.startswith("Ran /model.\nBot status for Codex in Default Workspace")
    assert find_inline_button(final_markup, "Agent Commands")


def test_bot_status_agent_commands_run_with_args_failure_restores_status():
    from talk2agent.bots.telegram_bot import (
        BUTTON_BOT_STATUS,
        TelegramUiState,
        handle_callback_query,
        handle_text,
    )

    session = FakeSession(
        session_id="session-abc",
        available_commands=[FakeCommand("model", "Switch model", hint="model id")],
    )
    ui_state = TelegramUiState()
    update = FakeUpdate(user_id=123, text=BUTTON_BOT_STATUS)
    callback_message = FakeIncomingMessage("status")
    services, store = make_services(
        provider="codex",
        session=session,
        get_or_create_error=RuntimeError("boom"),
    )

    run(handle_text(update, None, services, ui_state))

    commands_button = find_inline_button(update.message.reply_markups[0], "Agent Commands")
    commands_update = FakeCallbackUpdate(123, commands_button.callback_data, message=callback_message)
    run(handle_callback_query(commands_update, None, services, ui_state))

    args_button = callback_message.edit_calls[-1][1].inline_keyboard[0][0]
    args_update = FakeCallbackUpdate(123, args_button.callback_data, message=callback_message)
    run(handle_callback_query(args_update, None, services, ui_state))

    request_update = FakeUpdate(user_id=123, text="gpt-5.4-mini")
    run(handle_text(request_update, None, services, ui_state))

    assert store.get_or_create_calls == [123]
    assert request_update.message.reply_calls == []
    final_text, final_markup = callback_message.edit_calls[-1]
    assert final_text.startswith("Request failed.\nBot status for Codex in Default Workspace")
    assert find_inline_button(final_markup, "Agent Commands")


def test_agent_commands_run_button_executes_command_turn():
    from talk2agent.bots.telegram_bot import BUTTON_AGENT_COMMANDS, TelegramUiState, handle_callback_query, handle_text

    session = FakeSession(
        session_id="session-abc",
        available_commands=[FakeCommand("status", "Show status")],
    )
    update = FakeUpdate(user_id=123, text=BUTTON_AGENT_COMMANDS)
    services, store = make_services(session=session)
    ui_state = TelegramUiState()

    run(handle_text(update, None, services, ui_state))

    run_button = update.message.reply_markups[0].inline_keyboard[0][0]
    callback_message = FakeIncomingMessage("commands")
    callback_update = FakeCallbackUpdate(123, run_button.callback_data, message=callback_message)

    run(handle_callback_query(callback_update, make_context(application=FakeApplication()), services, ui_state))

    assert [text for text, _ in callback_message.edit_calls] == ["Running /status..."]
    assert session.prompts == ["/status"]
    assert [text for _, text in callback_message.draft_calls] == ["Thinking...", "hello ", "hello world"]
    assert callback_message.reply_calls == ["hello world"]
    assert store.record_session_usage_calls == [(123, "session-abc", "/status")]


def test_agent_commands_with_hint_use_next_text_message():
    from talk2agent.bots.telegram_bot import BUTTON_AGENT_COMMANDS, TelegramUiState, handle_callback_query, handle_text

    session = FakeSession(
        session_id="session-123",
        available_commands=[FakeCommand("model", "Switch model", hint="model id")],
    )
    update = FakeUpdate(user_id=123, text=BUTTON_AGENT_COMMANDS)
    services, store = make_services(session=session)
    ui_state = TelegramUiState()

    run(handle_text(update, None, services, ui_state))

    args_button = update.message.reply_markups[0].inline_keyboard[0][0]
    callback_update = FakeCallbackUpdate(123, args_button.callback_data, message=FakeIncomingMessage("commands"))
    run(handle_callback_query(callback_update, None, services, ui_state))

    assert callback_update.callback_query.message.edit_calls[-1][0].startswith(
        "Send arguments for /model as your next plain text message."
    )

    command_update = FakeUpdate(user_id=123, text="gpt-5.4-mini")
    run(handle_text(command_update, None, services, ui_state))

    assert session.prompts == ["/model gpt-5.4-mini"]
    assert command_update.message.reply_calls == ["hello world"]
    assert store.record_session_usage_calls == [(123, "session-123", "/model gpt-5.4-mini")]


def test_agent_commands_cancel_returns_to_command_list():
    from talk2agent.bots.telegram_bot import BUTTON_AGENT_COMMANDS, TelegramUiState, handle_callback_query, handle_text

    session = FakeSession(
        available_commands=[FakeCommand("model", "Switch model", hint="model id")]
    )
    update = FakeUpdate(user_id=123, text=BUTTON_AGENT_COMMANDS)
    services, _ = make_services(session=session)
    ui_state = TelegramUiState()

    run(handle_text(update, None, services, ui_state))

    args_button = update.message.reply_markups[0].inline_keyboard[0][0]
    prompt_update = FakeCallbackUpdate(123, args_button.callback_data, message=FakeIncomingMessage("commands"))
    run(handle_callback_query(prompt_update, None, services, ui_state))

    cancel_button = prompt_update.callback_query.message.edit_calls[-1][1].inline_keyboard[0][0]
    cancel_update = FakeCallbackUpdate(123, cancel_button.callback_data, message=prompt_update.callback_query.message)
    run(handle_callback_query(cancel_update, None, services, ui_state))

    assert cancel_update.callback_query.message.edit_calls[-1][0].startswith(
        "Command input cancelled.\nAgent commands for Claude Code in Default Workspace"
    )


def test_bot_status_agent_commands_cancel_returns_with_back_to_status():
    from talk2agent.bots.telegram_bot import (
        BUTTON_BOT_STATUS,
        TelegramUiState,
        handle_callback_query,
        handle_text,
    )

    session = FakeSession(
        available_commands=[FakeCommand("model", "Switch model", hint="model id")]
    )
    ui_state = TelegramUiState()
    update = FakeUpdate(user_id=123, text=BUTTON_BOT_STATUS)
    services, _ = make_services(provider="codex", session=session, peek_session=None)

    run(handle_text(update, None, services, ui_state))

    callback_message = FakeIncomingMessage("status")
    commands_button = find_inline_button(update.message.reply_markups[0], "Agent Commands")
    commands_update = FakeCallbackUpdate(123, commands_button.callback_data, message=callback_message)
    run(handle_callback_query(commands_update, None, services, ui_state))

    args_button = find_inline_button(callback_message.edit_calls[-1][1], "Args 1")
    prompt_update = FakeCallbackUpdate(123, args_button.callback_data, message=callback_message)
    run(handle_callback_query(prompt_update, None, services, ui_state))

    cancel_button = find_inline_button(callback_message.edit_calls[-1][1], "Cancel Command")
    cancel_update = FakeCallbackUpdate(123, cancel_button.callback_data, message=callback_message)
    run(handle_callback_query(cancel_update, None, services, ui_state))

    restored_text, restored_markup = callback_message.edit_calls[-1]
    assert restored_text.startswith(
        "Command input cancelled.\nAgent commands for Codex in Default Workspace"
    )
    assert find_inline_button(restored_markup, "Back to Bot Status")


def test_workspace_files_button_shows_current_directory_listing(tmp_path):
    from talk2agent.bots.telegram_bot import BUTTON_WORKSPACE_FILES, TelegramUiState, handle_text

    (tmp_path / "src").mkdir()
    (tmp_path / "README.md").write_text("hello", encoding="utf-8")

    update = FakeUpdate(user_id=123, text=BUTTON_WORKSPACE_FILES)
    services, _ = make_services(workspace_path=str(tmp_path))

    run(handle_text(update, None, services, TelegramUiState()))

    assert update.message.reply_calls[0].startswith(
        "Workspace files for Claude Code in Default Workspace\nPath: ."
    )
    labels = [button.text for row in update.message.reply_markups[0].inline_keyboard for button in row]
    assert labels == [
        "src/",
        "README.md",
        "Ask Agent With Visible Files",
        "Start Bundle Chat With Visible Files",
        "Add Visible Files to Context",
        "Open Context Bundle",
    ]


def test_workspace_files_can_navigate_directory_and_preview_file(tmp_path):
    from talk2agent.bots.telegram_bot import BUTTON_WORKSPACE_FILES, TelegramUiState, handle_callback_query, handle_text

    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("print('hello')\n", encoding="utf-8")

    ui_state = TelegramUiState()
    update = FakeUpdate(user_id=123, text=BUTTON_WORKSPACE_FILES)
    services, _ = make_services(workspace_path=str(tmp_path))

    run(handle_text(update, None, services, ui_state))

    src_button = update.message.reply_markups[0].inline_keyboard[0][0]
    dir_update = FakeCallbackUpdate(123, src_button.callback_data, message=FakeIncomingMessage("workspace"))
    run(handle_callback_query(dir_update, None, services, ui_state))

    assert dir_update.callback_query.message.edit_calls[-1][0].startswith(
        "Workspace files for Claude Code in Default Workspace\nPath: src"
    )

    file_button = dir_update.callback_query.message.edit_calls[-1][1].inline_keyboard[0][0]
    file_update = FakeCallbackUpdate(123, file_button.callback_data, message=dir_update.callback_query.message)
    run(handle_callback_query(file_update, None, services, ui_state))

    assert file_update.callback_query.message.edit_calls[-1][0].startswith(
        "Workspace file for Claude Code in Default Workspace\nPath: src/app.py\nprint('hello')"
    )


def test_workspace_file_preview_back_returns_to_parent_listing(tmp_path):
    from talk2agent.bots.telegram_bot import BUTTON_WORKSPACE_FILES, TelegramUiState, handle_callback_query, handle_text

    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("print('hello')\n", encoding="utf-8")

    ui_state = TelegramUiState()
    update = FakeUpdate(user_id=123, text=BUTTON_WORKSPACE_FILES)
    services, _ = make_services(workspace_path=str(tmp_path))

    run(handle_text(update, None, services, ui_state))

    src_button = update.message.reply_markups[0].inline_keyboard[0][0]
    dir_update = FakeCallbackUpdate(123, src_button.callback_data, message=FakeIncomingMessage("workspace"))
    run(handle_callback_query(dir_update, None, services, ui_state))

    file_button = dir_update.callback_query.message.edit_calls[-1][1].inline_keyboard[0][0]
    file_update = FakeCallbackUpdate(123, file_button.callback_data, message=dir_update.callback_query.message)
    run(handle_callback_query(file_update, None, services, ui_state))

    back_button = find_inline_button(
        file_update.callback_query.message.edit_calls[-1][1],
        "Back to Folder",
    )
    back_update = FakeCallbackUpdate(123, back_button.callback_data, message=file_update.callback_query.message)
    run(handle_callback_query(back_update, None, services, ui_state))

    assert back_update.callback_query.message.edit_calls[-1][0].startswith(
        "Workspace files for Claude Code in Default Workspace\nPath: src"
    )


def test_workspace_listing_can_add_visible_files_to_context_bundle(tmp_path):
    from talk2agent.bots.telegram_bot import (
        BUTTON_WORKSPACE_FILES,
        TelegramUiState,
        handle_callback_query,
        handle_text,
    )

    (tmp_path / "src").mkdir()
    (tmp_path / "README.md").write_text("hello", encoding="utf-8")

    ui_state = TelegramUiState()
    update = FakeUpdate(user_id=123, text=BUTTON_WORKSPACE_FILES)
    services, _ = make_services(workspace_path=str(tmp_path))

    run(handle_text(update, None, services, ui_state))

    add_button = find_inline_button(update.message.reply_markups[0], "Add Visible Files to Context")
    add_update = FakeCallbackUpdate(123, add_button.callback_data, message=FakeIncomingMessage("workspace"))
    run(handle_callback_query(add_update, None, services, ui_state))

    edited_text, _ = add_update.callback_query.message.edit_calls[-1]
    assert edited_text.startswith(
        "Added 1 file from workspace view to context bundle.\nContext bundle for Claude Code in Default Workspace\nItems: 1"
    )
    assert "1. [file] README.md" in edited_text


def test_workspace_listing_add_visible_files_reports_existing_bundle_items(tmp_path):
    from talk2agent.bots.telegram_bot import (
        BUTTON_WORKSPACE_FILES,
        TelegramUiState,
        _ContextBundleItem,
        handle_callback_query,
        handle_text,
    )

    (tmp_path / "src").mkdir()
    (tmp_path / "README.md").write_text("hello", encoding="utf-8")

    ui_state = TelegramUiState()
    ui_state.add_context_item(
        123,
        "claude",
        "default",
        _ContextBundleItem(kind="file", relative_path="README.md"),
    )
    update = FakeUpdate(user_id=123, text=BUTTON_WORKSPACE_FILES)
    services, _ = make_services(workspace_path=str(tmp_path))

    run(handle_text(update, None, services, ui_state))

    add_button = find_inline_button(update.message.reply_markups[0], "Add Visible Files to Context")
    add_update = FakeCallbackUpdate(123, add_button.callback_data, message=FakeIncomingMessage("workspace"))
    run(handle_callback_query(add_update, None, services, ui_state))

    edited_text, _ = add_update.callback_query.message.edit_calls[-1]
    assert edited_text == (
        "All 1 visible file is already in the context bundle.\n"
        "Context bundle for Claude Code in Default Workspace\n"
        "Items: 1\n"
        "Bundle chat: off\n"
        "1. [file] README.md"
    )


def test_workspace_listing_can_ask_agent_with_visible_files(tmp_path):
    from talk2agent.bots.telegram_bot import (
        BUTTON_WORKSPACE_FILES,
        TelegramUiState,
        _ContextBundleItem,
        _context_items_agent_prompt,
        handle_callback_query,
        handle_text,
    )

    (tmp_path / "README.md").write_text("hello", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("context", encoding="utf-8")

    ui_state = TelegramUiState()
    session = FakeSession(session_id="session-abc")
    services, store = make_services(session=session, workspace_path=str(tmp_path))
    update = FakeUpdate(user_id=123, text=BUTTON_WORKSPACE_FILES)

    run(handle_text(update, None, services, ui_state))

    ask_button = find_inline_button(update.message.reply_markups[0], "Ask Agent With Visible Files")
    ask_update = FakeCallbackUpdate(123, ask_button.callback_data, message=FakeIncomingMessage("workspace"))
    run(handle_callback_query(ask_update, None, services, ui_state))

    assert ask_update.callback_query.message.edit_calls[-1][0].startswith(
        "Send your request about the visible files as the next plain text message."
    )

    request_update = FakeUpdate(user_id=123, text="Summarize these files.")
    run(handle_text(request_update, None, services, ui_state))

    expected_prompt = _context_items_agent_prompt(
        (
            _ContextBundleItem(kind="file", relative_path="notes.txt"),
            _ContextBundleItem(kind="file", relative_path="README.md"),
        ),
        "Summarize these files.",
        context_label="visible workspace files",
    )
    assert session.prompts == [expected_prompt]
    assert request_update.message.reply_calls == ["hello world"]
    assert store.record_session_usage_calls == [(123, "session-abc", expected_prompt)]


def test_workspace_listing_can_ask_with_last_request(tmp_path):
    from talk2agent.bots.telegram_bot import (
        BUTTON_WORKSPACE_FILES,
        TelegramUiState,
        _ContextBundleItem,
        _context_items_agent_prompt,
        handle_callback_query,
        handle_text,
    )

    (tmp_path / "README.md").write_text("hello", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("context", encoding="utf-8")

    ui_state = TelegramUiState()
    session = FakeSession(session_id="session-abc")
    services, store = make_services(session=session, workspace_path=str(tmp_path))

    run(handle_text(FakeUpdate(user_id=123, text="Summarize these files."), None, services, ui_state))

    update = FakeUpdate(user_id=123, text=BUTTON_WORKSPACE_FILES)
    run(handle_text(update, None, services, ui_state))

    ask_button = find_inline_button(update.message.reply_markups[0], "Ask With Last Request")
    ask_update = FakeCallbackUpdate(123, ask_button.callback_data, message=FakeIncomingMessage("workspace"))
    run(handle_callback_query(ask_update, None, services, ui_state))

    expected_prompt = _context_items_agent_prompt(
        (
            _ContextBundleItem(kind="file", relative_path="notes.txt"),
            _ContextBundleItem(kind="file", relative_path="README.md"),
        ),
        "Summarize these files.",
        context_label="visible workspace files",
    )
    assert session.prompts == ["Summarize these files.", expected_prompt]
    assert ask_update.callback_query.message.reply_calls == ["hello world"]
    assert store.record_session_usage_calls == [
        (123, "session-abc", "Summarize these files."),
        (123, "session-abc", expected_prompt),
    ]


def test_workspace_listing_can_start_bundle_chat_with_visible_files(tmp_path):
    from talk2agent.bots.telegram_bot import (
        BUTTON_WORKSPACE_FILES,
        TelegramUiState,
        _ContextBundleItem,
        _context_bundle_agent_prompt,
        handle_callback_query,
        handle_text,
    )

    (tmp_path / "README.md").write_text("hello", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("context", encoding="utf-8")

    ui_state = TelegramUiState()
    session = FakeSession(session_id="session-abc")
    services, store = make_services(session=session, workspace_path=str(tmp_path))
    update = FakeUpdate(user_id=123, text=BUTTON_WORKSPACE_FILES)

    run(handle_text(update, None, services, ui_state))

    start_button = find_inline_button(update.message.reply_markups[0], "Start Bundle Chat With Visible Files")
    start_update = FakeCallbackUpdate(123, start_button.callback_data, message=FakeIncomingMessage("workspace"))
    run(handle_callback_query(start_update, None, services, ui_state))

    enabled_text, _ = start_update.callback_query.message.edit_calls[-1]
    assert enabled_text.startswith(
        "Added 2 files from workspace view to context bundle. Bundle chat enabled.\n"
        "Context bundle for Claude Code in Default Workspace\nItems: 2\nBundle chat: on"
    )

    request_update = FakeUpdate(user_id=123, text="Keep working with these files.")
    run(handle_text(request_update, None, services, ui_state))

    expected_prompt = _context_bundle_agent_prompt(
        (
            _ContextBundleItem(kind="file", relative_path="notes.txt"),
            _ContextBundleItem(kind="file", relative_path="README.md"),
        ),
        "Keep working with these files.",
    )
    assert session.prompts == [expected_prompt]
    assert request_update.message.reply_calls == ["hello world"]
    assert store.record_session_usage_calls == [(123, "session-abc", "Keep working with these files.")]


def test_workspace_listing_start_bundle_chat_can_go_back_to_folder(tmp_path):
    from talk2agent.bots.telegram_bot import (
        BUTTON_WORKSPACE_FILES,
        TelegramUiState,
        handle_callback_query,
        handle_text,
    )

    (tmp_path / "README.md").write_text("hello", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("context", encoding="utf-8")

    ui_state = TelegramUiState()
    services, _ = make_services(workspace_path=str(tmp_path))
    update = FakeUpdate(user_id=123, text=BUTTON_WORKSPACE_FILES)

    run(handle_text(update, None, services, ui_state))

    start_button = find_inline_button(update.message.reply_markups[0], "Start Bundle Chat With Visible Files")
    start_update = FakeCallbackUpdate(123, start_button.callback_data, message=FakeIncomingMessage("workspace"))
    run(handle_callback_query(start_update, None, services, ui_state))

    bundle_text, bundle_markup = start_update.callback_query.message.edit_calls[-1]
    assert bundle_text.startswith(
        "Added 2 files from workspace view to context bundle. Bundle chat enabled.\n"
        "Context bundle for Claude Code in Default Workspace\nItems: 2\nBundle chat: on"
    )
    back_button = find_inline_button(bundle_markup, "Back to Folder")

    back_update = FakeCallbackUpdate(123, back_button.callback_data, message=start_update.callback_query.message)
    run(handle_callback_query(back_update, None, services, ui_state))

    restored_text, restored_markup = back_update.callback_query.message.edit_calls[-1]
    assert restored_text.startswith("Workspace files for Claude Code in Default Workspace\nPath: .")
    assert find_inline_button(restored_markup, "Start Bundle Chat With Visible Files")


def test_workspace_search_uses_next_text_message_and_shows_matches(tmp_path):
    from talk2agent.bots.telegram_bot import BUTTON_WORKSPACE_SEARCH, TelegramUiState, handle_text

    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("hello agent\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("agent guide\n", encoding="utf-8")

    ui_state = TelegramUiState()
    start_update = FakeUpdate(user_id=123, text=BUTTON_WORKSPACE_SEARCH)
    services, _ = make_services(workspace_path=str(tmp_path))

    run(handle_text(start_update, None, services, ui_state))

    assert start_update.message.reply_calls == [
        "Send your workspace search query as the next plain text message."
    ]

    query_update = FakeUpdate(user_id=123, text="agent")
    run(handle_text(query_update, None, services, ui_state))

    assert query_update.message.reply_calls[0].startswith(
        "Workspace search for Claude Code in Default Workspace\nQuery: agent"
    )
    assert "README.md:1" in query_update.message.reply_calls[0]
    assert "src/app.py:1" in query_update.message.reply_calls[0]
    assert find_inline_button(query_update.message.reply_markups[0], "Ask Agent With Matching Files")
    assert find_inline_button(query_update.message.reply_markups[0], "Start Bundle Chat With Matching Files")
    assert find_inline_button(query_update.message.reply_markups[0], "Add Matching Files to Context")
    assert find_inline_button(query_update.message.reply_markups[0], "Open Context Bundle")


def test_workspace_search_open_file_and_back(tmp_path):
    from talk2agent.bots.telegram_bot import BUTTON_WORKSPACE_SEARCH, TelegramUiState, handle_callback_query, handle_text

    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("hello agent\n", encoding="utf-8")

    ui_state = TelegramUiState()
    services, _ = make_services(workspace_path=str(tmp_path))
    start_update = FakeUpdate(user_id=123, text=BUTTON_WORKSPACE_SEARCH)
    run(handle_text(start_update, None, services, ui_state))

    query_update = FakeUpdate(user_id=123, text="agent")
    run(handle_text(query_update, None, services, ui_state))

    open_button = query_update.message.reply_markups[0].inline_keyboard[0][0]
    preview_update = FakeCallbackUpdate(123, open_button.callback_data, message=FakeIncomingMessage("search"))
    run(handle_callback_query(preview_update, None, services, ui_state))

    assert preview_update.callback_query.message.edit_calls[-1][0].startswith(
        "Workspace file for Claude Code in Default Workspace\nPath: src/app.py\nhello agent"
    )

    back_button = find_inline_button(
        preview_update.callback_query.message.edit_calls[-1][1],
        "Back to Search",
    )
    back_update = FakeCallbackUpdate(123, back_button.callback_data, message=preview_update.callback_query.message)
    run(handle_callback_query(back_update, None, services, ui_state))

    assert back_update.callback_query.message.edit_calls[-1][0].startswith(
        "Workspace search for Claude Code in Default Workspace\nQuery: agent"
    )


def test_workspace_search_can_add_matching_files_to_context_bundle(tmp_path):
    from talk2agent.bots.telegram_bot import (
        BUTTON_WORKSPACE_SEARCH,
        TelegramUiState,
        handle_callback_query,
        handle_text,
    )

    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("hello agent\nagent helper\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("agent guide\n", encoding="utf-8")

    ui_state = TelegramUiState()
    services, _ = make_services(workspace_path=str(tmp_path))
    start_update = FakeUpdate(user_id=123, text=BUTTON_WORKSPACE_SEARCH)
    run(handle_text(start_update, None, services, ui_state))

    query_update = FakeUpdate(user_id=123, text="agent")
    run(handle_text(query_update, None, services, ui_state))

    add_button = find_inline_button(query_update.message.reply_markups[0], "Add Matching Files to Context")
    add_update = FakeCallbackUpdate(123, add_button.callback_data, message=FakeIncomingMessage("search"))
    run(handle_callback_query(add_update, None, services, ui_state))

    edited_text, _ = add_update.callback_query.message.edit_calls[-1]
    assert edited_text.startswith(
        "Added 2 files from search results to context bundle.\nContext bundle for Claude Code in Default Workspace\nItems: 2"
    )
    assert "1. [file] README.md" in edited_text
    assert "2. [file] src/app.py" in edited_text


def test_workspace_search_open_context_bundle_can_go_back_to_search(tmp_path):
    from talk2agent.bots.telegram_bot import (
        BUTTON_WORKSPACE_SEARCH,
        TelegramUiState,
        handle_callback_query,
        handle_text,
    )

    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("hello agent\nagent helper\n", encoding="utf-8")

    ui_state = TelegramUiState()
    services, _ = make_services(workspace_path=str(tmp_path))
    start_update = FakeUpdate(user_id=123, text=BUTTON_WORKSPACE_SEARCH)
    run(handle_text(start_update, None, services, ui_state))

    query_update = FakeUpdate(user_id=123, text="agent")
    run(handle_text(query_update, None, services, ui_state))

    bundle_button = find_inline_button(query_update.message.reply_markups[0], "Open Context Bundle")
    bundle_update = FakeCallbackUpdate(123, bundle_button.callback_data, message=FakeIncomingMessage("search"))
    run(handle_callback_query(bundle_update, None, services, ui_state))

    bundle_text, bundle_markup = bundle_update.callback_query.message.edit_calls[-1]
    assert bundle_text == (
        "Context bundle for Claude Code in Default Workspace\n"
        "Context bundle is empty."
    )
    back_button = find_inline_button(bundle_markup, "Back to Search")

    back_update = FakeCallbackUpdate(123, back_button.callback_data, message=bundle_update.callback_query.message)
    run(handle_callback_query(back_update, None, services, ui_state))

    restored_text, restored_markup = back_update.callback_query.message.edit_calls[-1]
    assert restored_text.startswith(
        "Workspace search for Claude Code in Default Workspace\nQuery: agent"
    )
    assert find_inline_button(restored_markup, "Open Context Bundle")


def test_workspace_search_add_matching_files_reports_existing_bundle_items(tmp_path):
    from talk2agent.bots.telegram_bot import (
        BUTTON_WORKSPACE_SEARCH,
        TelegramUiState,
        _ContextBundleItem,
        handle_callback_query,
        handle_text,
    )

    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("hello agent\nagent helper\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("agent guide\n", encoding="utf-8")

    ui_state = TelegramUiState()
    ui_state.add_context_item(
        123,
        "claude",
        "default",
        _ContextBundleItem(kind="file", relative_path="src/app.py"),
    )
    services, _ = make_services(workspace_path=str(tmp_path))
    start_update = FakeUpdate(user_id=123, text=BUTTON_WORKSPACE_SEARCH)
    run(handle_text(start_update, None, services, ui_state))

    query_update = FakeUpdate(user_id=123, text="agent")
    run(handle_text(query_update, None, services, ui_state))

    add_button = find_inline_button(query_update.message.reply_markups[0], "Add Matching Files to Context")
    add_update = FakeCallbackUpdate(123, add_button.callback_data, message=FakeIncomingMessage("search"))
    run(handle_callback_query(add_update, None, services, ui_state))

    edited_text, _ = add_update.callback_query.message.edit_calls[-1]
    assert edited_text.startswith(
        "Added 1 file from search results to context bundle. 1 file was already present.\nContext bundle for Claude Code in Default Workspace\nItems: 2"
    )
    assert "1. [file] src/app.py" in edited_text
    assert "2. [file] README.md" in edited_text


def test_workspace_search_can_ask_agent_with_matching_files(tmp_path):
    from talk2agent.bots.telegram_bot import (
        BUTTON_WORKSPACE_SEARCH,
        TelegramUiState,
        _ContextBundleItem,
        _context_items_agent_prompt,
        handle_callback_query,
        handle_text,
    )

    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("hello agent\nagent helper\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("agent guide\n", encoding="utf-8")

    ui_state = TelegramUiState()
    session = FakeSession(session_id="session-abc")
    services, store = make_services(session=session, workspace_path=str(tmp_path))
    start_update = FakeUpdate(user_id=123, text=BUTTON_WORKSPACE_SEARCH)
    run(handle_text(start_update, None, services, ui_state))

    query_update = FakeUpdate(user_id=123, text="agent")
    run(handle_text(query_update, None, services, ui_state))

    ask_button = find_inline_button(query_update.message.reply_markups[0], "Ask Agent With Matching Files")
    ask_update = FakeCallbackUpdate(123, ask_button.callback_data, message=FakeIncomingMessage("search"))
    run(handle_callback_query(ask_update, None, services, ui_state))

    assert ask_update.callback_query.message.edit_calls[-1][0].startswith(
        "Send your request about the matching files as the next plain text message."
    )

    request_update = FakeUpdate(user_id=123, text="Compare these files.")
    run(handle_text(request_update, None, services, ui_state))

    expected_prompt = _context_items_agent_prompt(
        (
            _ContextBundleItem(kind="file", relative_path="README.md"),
            _ContextBundleItem(kind="file", relative_path="src/app.py"),
        ),
        "Compare these files.",
        context_label="matching workspace files",
    )
    assert session.prompts == [expected_prompt]
    assert request_update.message.reply_calls == ["hello world"]
    assert store.record_session_usage_calls == [(123, "session-abc", expected_prompt)]


def test_workspace_search_can_ask_with_last_request(tmp_path):
    from talk2agent.bots.telegram_bot import (
        BUTTON_WORKSPACE_SEARCH,
        TelegramUiState,
        _ContextBundleItem,
        _context_items_agent_prompt,
        handle_callback_query,
        handle_text,
    )

    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("hello agent\nagent helper\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("agent guide\n", encoding="utf-8")

    ui_state = TelegramUiState()
    session = FakeSession(session_id="session-abc")
    services, store = make_services(session=session, workspace_path=str(tmp_path))

    run(handle_text(FakeUpdate(user_id=123, text="Compare these files."), None, services, ui_state))

    start_update = FakeUpdate(user_id=123, text=BUTTON_WORKSPACE_SEARCH)
    run(handle_text(start_update, None, services, ui_state))

    query_update = FakeUpdate(user_id=123, text="agent")
    run(handle_text(query_update, None, services, ui_state))

    ask_button = find_inline_button(query_update.message.reply_markups[0], "Ask With Last Request")
    ask_update = FakeCallbackUpdate(123, ask_button.callback_data, message=FakeIncomingMessage("search"))
    run(handle_callback_query(ask_update, None, services, ui_state))

    expected_prompt = _context_items_agent_prompt(
        (
            _ContextBundleItem(kind="file", relative_path="README.md"),
            _ContextBundleItem(kind="file", relative_path="src/app.py"),
        ),
        "Compare these files.",
        context_label="matching workspace files",
    )
    assert session.prompts == ["Compare these files.", expected_prompt]
    assert ask_update.callback_query.message.reply_calls == ["hello world"]
    assert store.record_session_usage_calls == [
        (123, "session-abc", "Compare these files."),
        (123, "session-abc", expected_prompt),
    ]


def test_workspace_search_can_start_bundle_chat_with_matching_files(tmp_path):
    from talk2agent.bots.telegram_bot import (
        BUTTON_WORKSPACE_SEARCH,
        TelegramUiState,
        _ContextBundleItem,
        _context_bundle_agent_prompt,
        handle_callback_query,
        handle_text,
    )

    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("hello agent\nagent helper\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("agent guide\n", encoding="utf-8")

    ui_state = TelegramUiState()
    session = FakeSession(session_id="session-abc")
    services, store = make_services(session=session, workspace_path=str(tmp_path))
    start_update = FakeUpdate(user_id=123, text=BUTTON_WORKSPACE_SEARCH)
    run(handle_text(start_update, None, services, ui_state))

    query_update = FakeUpdate(user_id=123, text="agent")
    run(handle_text(query_update, None, services, ui_state))

    start_button = find_inline_button(query_update.message.reply_markups[0], "Start Bundle Chat With Matching Files")
    start_bundle_update = FakeCallbackUpdate(123, start_button.callback_data, message=FakeIncomingMessage("search"))
    run(handle_callback_query(start_bundle_update, None, services, ui_state))

    enabled_text, _ = start_bundle_update.callback_query.message.edit_calls[-1]
    assert enabled_text.startswith(
        "Added 2 files from search results to context bundle. Bundle chat enabled.\n"
        "Context bundle for Claude Code in Default Workspace\nItems: 2\nBundle chat: on"
    )

    request_update = FakeUpdate(user_id=123, text="Compare these matching files.")
    run(handle_text(request_update, None, services, ui_state))

    expected_prompt = _context_bundle_agent_prompt(
        (
            _ContextBundleItem(kind="file", relative_path="README.md"),
            _ContextBundleItem(kind="file", relative_path="src/app.py"),
        ),
        "Compare these matching files.",
    )
    assert session.prompts == [expected_prompt]
    assert request_update.message.reply_calls == ["hello world"]
    assert store.record_session_usage_calls == [(123, "session-abc", "Compare these matching files.")]


def test_workspace_file_preview_can_start_agent_turn_for_file(tmp_path):
    from talk2agent.bots.telegram_bot import (
        BUTTON_WORKSPACE_FILES,
        TelegramUiState,
        _workspace_file_agent_prompt,
        handle_callback_query,
        handle_text,
    )

    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("print('hello')\n", encoding="utf-8")

    ui_state = TelegramUiState()
    session = FakeSession(session_id="session-abc")
    services, store = make_services(session=session, workspace_path=str(tmp_path))
    update = FakeUpdate(user_id=123, text=BUTTON_WORKSPACE_FILES)

    run(handle_text(update, None, services, ui_state))

    src_button = update.message.reply_markups[0].inline_keyboard[0][0]
    dir_update = FakeCallbackUpdate(123, src_button.callback_data, message=FakeIncomingMessage("workspace"))
    run(handle_callback_query(dir_update, None, services, ui_state))

    file_button = dir_update.callback_query.message.edit_calls[-1][1].inline_keyboard[0][0]
    file_update = FakeCallbackUpdate(123, file_button.callback_data, message=dir_update.callback_query.message)
    run(handle_callback_query(file_update, None, services, ui_state))

    ask_button = file_update.callback_query.message.edit_calls[-1][1].inline_keyboard[0][0]
    ask_update = FakeCallbackUpdate(123, ask_button.callback_data, message=file_update.callback_query.message)
    run(handle_callback_query(ask_update, None, services, ui_state))

    assert ask_update.callback_query.message.edit_calls[-1][0].startswith(
        "Send your request about src/app.py as the next plain text message."
    )

    request_update = FakeUpdate(user_id=123, text="Explain this file.")
    run(handle_text(request_update, None, services, ui_state))

    assert session.prompts == [
        _workspace_file_agent_prompt("src/app.py", "Explain this file.")
    ]
    assert request_update.message.reply_calls == ["hello world"]
    assert store.record_session_usage_calls == [
        (123, "session-abc", _workspace_file_agent_prompt("src/app.py", "Explain this file."))
    ]


def test_workspace_file_preview_can_start_bundle_chat_with_file(tmp_path):
    from talk2agent.bots.telegram_bot import (
        BUTTON_WORKSPACE_FILES,
        TelegramUiState,
        _ContextBundleItem,
        handle_callback_query,
        handle_text,
    )

    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("print('hello')\n", encoding="utf-8")

    ui_state = TelegramUiState()
    services, _ = make_services(workspace_path=str(tmp_path))
    update = FakeUpdate(user_id=123, text=BUTTON_WORKSPACE_FILES)

    run(handle_text(update, None, services, ui_state))

    src_button = update.message.reply_markups[0].inline_keyboard[0][0]
    dir_update = FakeCallbackUpdate(123, src_button.callback_data, message=FakeIncomingMessage("workspace"))
    run(handle_callback_query(dir_update, None, services, ui_state))

    file_button = dir_update.callback_query.message.edit_calls[-1][1].inline_keyboard[0][0]
    file_update = FakeCallbackUpdate(123, file_button.callback_data, message=dir_update.callback_query.message)
    run(handle_callback_query(file_update, None, services, ui_state))

    start_button = find_inline_button(
        file_update.callback_query.message.edit_calls[-1][1],
        "Start Bundle Chat With File",
    )
    start_update = FakeCallbackUpdate(123, start_button.callback_data, message=file_update.callback_query.message)
    run(handle_callback_query(start_update, None, services, ui_state))

    assert start_update.callback_query.message.edit_calls[-1][0].startswith(
        "Added file to context bundle. Bundle chat enabled.\n"
        "Context bundle for Claude Code in Default Workspace\n"
        "Items: 1\n"
        "Bundle chat: on\n"
        "1. [file] src/app.py"
    )
    assert ui_state.context_bundle_chat_active(123, "claude", "default") is True
    bundle = ui_state.get_context_bundle(123, "claude", "default")
    assert bundle is not None
    assert bundle.items == [_ContextBundleItem(kind="file", relative_path="src/app.py")]


def test_workspace_file_preview_open_context_bundle_can_go_back_to_file(tmp_path):
    from talk2agent.bots.telegram_bot import (
        BUTTON_WORKSPACE_FILES,
        TelegramUiState,
        handle_callback_query,
        handle_text,
    )

    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("print('hello')\n", encoding="utf-8")

    ui_state = TelegramUiState()
    services, _ = make_services(workspace_path=str(tmp_path))
    update = FakeUpdate(user_id=123, text=BUTTON_WORKSPACE_FILES)

    run(handle_text(update, None, services, ui_state))

    src_button = update.message.reply_markups[0].inline_keyboard[0][0]
    dir_update = FakeCallbackUpdate(123, src_button.callback_data, message=FakeIncomingMessage("workspace"))
    run(handle_callback_query(dir_update, None, services, ui_state))

    file_button = dir_update.callback_query.message.edit_calls[-1][1].inline_keyboard[0][0]
    file_update = FakeCallbackUpdate(123, file_button.callback_data, message=dir_update.callback_query.message)
    run(handle_callback_query(file_update, None, services, ui_state))

    bundle_button = find_inline_button(
        file_update.callback_query.message.edit_calls[-1][1],
        "Open Context Bundle",
    )
    bundle_update = FakeCallbackUpdate(123, bundle_button.callback_data, message=file_update.callback_query.message)
    run(handle_callback_query(bundle_update, None, services, ui_state))

    bundle_text, bundle_markup = bundle_update.callback_query.message.edit_calls[-1]
    assert bundle_text == (
        "Context bundle for Claude Code in Default Workspace\n"
        "Context bundle is empty."
    )
    back_button = find_inline_button(bundle_markup, "Back to File")

    back_update = FakeCallbackUpdate(123, back_button.callback_data, message=bundle_update.callback_query.message)
    run(handle_callback_query(back_update, None, services, ui_state))

    restored_text, restored_markup = back_update.callback_query.message.edit_calls[-1]
    assert restored_text.startswith(
        "Workspace file for Claude Code in Default Workspace\nPath: src/app.py\nprint('hello')"
    )
    assert find_inline_button(restored_markup, "Open Context Bundle")


def test_workspace_file_preview_can_ask_with_last_request(tmp_path):
    from talk2agent.bots.telegram_bot import (
        BUTTON_WORKSPACE_FILES,
        TelegramUiState,
        _workspace_file_agent_prompt,
        handle_callback_query,
        handle_text,
    )

    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("print('hello')\n", encoding="utf-8")

    ui_state = TelegramUiState()
    session = FakeSession(session_id="session-abc")
    services, store = make_services(session=session, workspace_path=str(tmp_path))

    run(handle_text(FakeUpdate(user_id=123, text="Review this carefully."), None, services, ui_state))

    update = FakeUpdate(user_id=123, text=BUTTON_WORKSPACE_FILES)
    run(handle_text(update, None, services, ui_state))

    src_button = update.message.reply_markups[0].inline_keyboard[0][0]
    dir_update = FakeCallbackUpdate(123, src_button.callback_data, message=FakeIncomingMessage("workspace"))
    run(handle_callback_query(dir_update, None, services, ui_state))

    file_button = dir_update.callback_query.message.edit_calls[-1][1].inline_keyboard[0][0]
    file_update = FakeCallbackUpdate(123, file_button.callback_data, message=dir_update.callback_query.message)
    run(handle_callback_query(file_update, None, services, ui_state))

    ask_button = find_inline_button(
        file_update.callback_query.message.edit_calls[-1][1],
        "Ask With Last Request",
    )
    ask_update = FakeCallbackUpdate(123, ask_button.callback_data, message=file_update.callback_query.message)
    run(handle_callback_query(ask_update, None, services, ui_state))

    assert session.prompts == [
        "Review this carefully.",
        _workspace_file_agent_prompt("src/app.py", "Review this carefully."),
    ]
    assert ask_update.callback_query.message.reply_calls == ["hello world"]
    assert store.record_session_usage_calls == [
        (123, "session-abc", "Review this carefully."),
        (123, "session-abc", _workspace_file_agent_prompt("src/app.py", "Review this carefully.")),
    ]


def test_context_bundle_button_shows_empty_bundle():
    from talk2agent.bots.telegram_bot import BUTTON_CONTEXT_BUNDLE, TelegramUiState, handle_text

    update = FakeUpdate(user_id=123, text=BUTTON_CONTEXT_BUNDLE)
    services, _ = make_services()

    run(handle_text(update, None, services, TelegramUiState()))

    assert update.message.reply_calls == [
        "Context bundle for Claude Code in Default Workspace\nContext bundle is empty."
    ]


def test_workspace_previews_can_add_context_and_bundle_can_run_agent_turn(monkeypatch, tmp_path):
    from talk2agent.bots import telegram_bot
    from talk2agent.bots.telegram_bot import (
        BUTTON_CONTEXT_BUNDLE,
        BUTTON_WORKSPACE_CHANGES,
        BUTTON_WORKSPACE_FILES,
        TelegramUiState,
        _ContextBundleItem,
        _context_bundle_agent_prompt,
        handle_callback_query,
        handle_text,
    )
    from talk2agent.workspace_git import WorkspaceGitDiffPreview, WorkspaceGitStatus, WorkspaceGitStatusEntry

    (tmp_path / "notes.txt").write_text("bot context\n", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("print('hello')\n", encoding="utf-8")

    monkeypatch.setattr(
        telegram_bot,
        "read_workspace_git_status",
        lambda _path: WorkspaceGitStatus(
            is_git_repo=True,
            branch_line="main",
            entries=(WorkspaceGitStatusEntry(" M", "src/app.py", "src/app.py"),),
        ),
    )
    monkeypatch.setattr(
        telegram_bot,
        "read_workspace_git_diff_preview",
        lambda _root, relative_path, status_code: WorkspaceGitDiffPreview(
            relative_path=relative_path,
            status_code=status_code,
            text="diff --git a/src/app.py b/src/app.py",
            truncated=False,
        ),
    )

    ui_state = TelegramUiState()
    session = FakeSession(session_id="session-abc")
    services, store = make_services(session=session, workspace_path=str(tmp_path))

    files_update = FakeUpdate(user_id=123, text=BUTTON_WORKSPACE_FILES)
    run(handle_text(files_update, None, services, ui_state))

    file_button = find_inline_button(files_update.message.reply_markups[0], "notes.txt")
    file_preview_update = FakeCallbackUpdate(123, file_button.callback_data, message=FakeIncomingMessage("files"))
    run(handle_callback_query(file_preview_update, None, services, ui_state))

    add_file_button = find_inline_button(
        file_preview_update.callback_query.message.edit_calls[-1][1],
        "Add File to Context",
    )
    add_file_update = FakeCallbackUpdate(
        123,
        add_file_button.callback_data,
        message=file_preview_update.callback_query.message,
    )
    run(handle_callback_query(add_file_update, None, services, ui_state))

    assert add_file_update.callback_query.answers == [("Added file to context bundle.", False)]

    changes_update = FakeUpdate(user_id=123, text=BUTTON_WORKSPACE_CHANGES)
    run(handle_text(changes_update, None, services, ui_state))

    open_change_button = changes_update.message.reply_markups[0].inline_keyboard[0][0]
    change_preview_update = FakeCallbackUpdate(
        123,
        open_change_button.callback_data,
        message=FakeIncomingMessage("changes"),
    )
    run(handle_callback_query(change_preview_update, None, services, ui_state))

    add_change_button = find_inline_button(
        change_preview_update.callback_query.message.edit_calls[-1][1],
        "Add Change to Context",
    )
    add_change_update = FakeCallbackUpdate(
        123,
        add_change_button.callback_data,
        message=change_preview_update.callback_query.message,
    )
    run(handle_callback_query(add_change_update, None, services, ui_state))

    assert add_change_update.callback_query.answers == [("Added change to context bundle.", False)]

    bundle_update = FakeUpdate(user_id=123, text=BUTTON_CONTEXT_BUNDLE)
    run(handle_text(bundle_update, None, services, ui_state))

    assert bundle_update.message.reply_calls[0].startswith(
        "Context bundle for Claude Code in Default Workspace\nItems: 2"
    )
    assert "1. [file] notes.txt" in bundle_update.message.reply_calls[0]
    assert "2. [change  M] src/app.py" in bundle_update.message.reply_calls[0]

    ask_bundle_button = find_inline_button(
        bundle_update.message.reply_markups[0],
        "Ask Agent With Context",
    )
    ask_bundle_update = FakeCallbackUpdate(
        123,
        ask_bundle_button.callback_data,
        message=FakeIncomingMessage("bundle"),
    )
    run(handle_callback_query(ask_bundle_update, None, services, ui_state))

    assert ask_bundle_update.callback_query.message.edit_calls[-1][0].startswith(
        "Send your request for the current context bundle as the next plain text message."
    )

    request_update = FakeUpdate(user_id=123, text="Summarize this bundle.")
    run(handle_text(request_update, None, services, ui_state))

    expected_prompt = _context_bundle_agent_prompt(
        (
            _ContextBundleItem(kind="file", relative_path="notes.txt"),
            _ContextBundleItem(kind="change", relative_path="src/app.py", status_code=" M"),
        ),
        "Summarize this bundle.",
    )
    assert session.prompts == [expected_prompt]
    assert request_update.message.reply_calls == ["hello world"]
    assert store.record_session_usage_calls == [(123, "session-abc", expected_prompt)]


def test_context_bundle_can_ask_with_last_request():
    from talk2agent.bots.telegram_bot import (
        BUTTON_CONTEXT_BUNDLE,
        TelegramUiState,
        _ContextBundleItem,
        _context_bundle_agent_prompt,
        handle_callback_query,
        handle_text,
    )

    ui_state = TelegramUiState()
    ui_state.add_context_item(
        123,
        "claude",
        "default",
        _ContextBundleItem(kind="file", relative_path="notes.txt"),
    )
    ui_state.add_context_item(
        123,
        "claude",
        "default",
        _ContextBundleItem(kind="change", relative_path="src/app.py", status_code=" M"),
    )
    session = FakeSession(session_id="session-abc")
    services, store = make_services(session=session)

    run(handle_text(FakeUpdate(user_id=123, text="Summarize this bundle."), None, services, ui_state))

    update = FakeUpdate(user_id=123, text=BUTTON_CONTEXT_BUNDLE)
    run(handle_text(update, None, services, ui_state))

    ask_button = find_inline_button(update.message.reply_markups[0], "Ask With Last Request")
    ask_update = FakeCallbackUpdate(123, ask_button.callback_data, message=FakeIncomingMessage("bundle"))
    run(handle_callback_query(ask_update, None, services, ui_state))

    expected_prompt = _context_bundle_agent_prompt(
        (
            _ContextBundleItem(kind="file", relative_path="notes.txt"),
            _ContextBundleItem(kind="change", relative_path="src/app.py", status_code=" M"),
        ),
        "Summarize this bundle.",
    )
    assert session.prompts == ["Summarize this bundle.", expected_prompt]
    assert ask_update.callback_query.message.reply_calls == ["hello world"]
    assert store.record_session_usage_calls == [
        (123, "session-abc", "Summarize this bundle."),
        (123, "session-abc", expected_prompt),
    ]


def test_context_bundle_can_enable_bundle_chat_for_plain_text_turns():
    from talk2agent.bots.telegram_bot import (
        BUTTON_CONTEXT_BUNDLE,
        TelegramUiState,
        _ContextBundleItem,
        _context_bundle_agent_prompt,
        handle_callback_query,
        handle_text,
    )

    ui_state = TelegramUiState()
    ui_state.add_context_item(
        123,
        "claude",
        "default",
        _ContextBundleItem(kind="file", relative_path="notes.txt"),
    )
    ui_state.add_context_item(
        123,
        "claude",
        "default",
        _ContextBundleItem(kind="change", relative_path="src/app.py", status_code=" M"),
    )
    session = FakeSession(session_id="session-abc")
    services, store = make_services(session=session)

    bundle_update = FakeUpdate(user_id=123, text=BUTTON_CONTEXT_BUNDLE)
    run(handle_text(bundle_update, None, services, ui_state))

    assert "Bundle chat: off" in bundle_update.message.reply_calls[0]
    start_button = find_inline_button(bundle_update.message.reply_markups[0], "Start Bundle Chat")
    start_update = FakeCallbackUpdate(123, start_button.callback_data, message=FakeIncomingMessage("bundle"))
    run(handle_callback_query(start_update, None, services, ui_state))

    enabled_text, enabled_markup = start_update.callback_query.message.edit_calls[-1]
    assert enabled_text.startswith(
        "Bundle chat enabled. New plain text messages will use the current context bundle.\n"
        "Context bundle for Claude Code in Default Workspace\nItems: 2\nBundle chat: on"
    )
    assert find_inline_button(enabled_markup, "Stop Bundle Chat")

    request_update = FakeUpdate(user_id=123, text="Keep going with this bundle.")
    run(handle_text(request_update, None, services, ui_state))

    expected_prompt = _context_bundle_agent_prompt(
        (
            _ContextBundleItem(kind="file", relative_path="notes.txt"),
            _ContextBundleItem(kind="change", relative_path="src/app.py", status_code=" M"),
        ),
        "Keep going with this bundle.",
    )
    assert session.prompts == [expected_prompt]
    assert request_update.message.reply_calls == ["hello world"]
    assert store.record_session_usage_calls == [(123, "session-abc", "Keep going with this bundle.")]


def test_context_bundle_can_disable_bundle_chat():
    from talk2agent.bots.telegram_bot import (
        BUTTON_CONTEXT_BUNDLE,
        TelegramUiState,
        _ContextBundleItem,
        handle_callback_query,
        handle_text,
    )

    ui_state = TelegramUiState()
    ui_state.add_context_item(
        123,
        "claude",
        "default",
        _ContextBundleItem(kind="file", relative_path="notes.txt"),
    )
    session = FakeSession(session_id="session-abc")
    services, store = make_services(session=session)

    bundle_update = FakeUpdate(user_id=123, text=BUTTON_CONTEXT_BUNDLE)
    run(handle_text(bundle_update, None, services, ui_state))

    start_button = find_inline_button(bundle_update.message.reply_markups[0], "Start Bundle Chat")
    start_update = FakeCallbackUpdate(123, start_button.callback_data, message=FakeIncomingMessage("bundle"))
    run(handle_callback_query(start_update, None, services, ui_state))

    stop_button = find_inline_button(start_update.callback_query.message.edit_calls[-1][1], "Stop Bundle Chat")
    stop_update = FakeCallbackUpdate(123, stop_button.callback_data, message=start_update.callback_query.message)
    run(handle_callback_query(stop_update, None, services, ui_state))

    disabled_text, disabled_markup = stop_update.callback_query.message.edit_calls[-1]
    assert disabled_text.startswith(
        "Bundle chat disabled.\nContext bundle for Claude Code in Default Workspace\nItems: 1\nBundle chat: off"
    )
    assert find_inline_button(disabled_markup, "Start Bundle Chat")

    request_update = FakeUpdate(user_id=123, text="Plain request only.")
    run(handle_text(request_update, None, services, ui_state))

    assert session.prompts == ["Plain request only."]
    assert request_update.message.reply_calls == ["hello world"]
    assert store.record_session_usage_calls == [(123, "session-abc", "Plain request only.")]


def test_workspace_search_file_ask_cancel_restores_preview(tmp_path):
    from talk2agent.bots.telegram_bot import BUTTON_WORKSPACE_SEARCH, TelegramUiState, handle_callback_query, handle_text

    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("hello agent\n", encoding="utf-8")

    ui_state = TelegramUiState()
    services, _ = make_services(workspace_path=str(tmp_path))
    start_update = FakeUpdate(user_id=123, text=BUTTON_WORKSPACE_SEARCH)
    run(handle_text(start_update, None, services, ui_state))

    query_update = FakeUpdate(user_id=123, text="agent")
    run(handle_text(query_update, None, services, ui_state))

    open_button = query_update.message.reply_markups[0].inline_keyboard[0][0]
    preview_update = FakeCallbackUpdate(123, open_button.callback_data, message=FakeIncomingMessage("search"))
    run(handle_callback_query(preview_update, None, services, ui_state))

    ask_button = preview_update.callback_query.message.edit_calls[-1][1].inline_keyboard[0][0]
    ask_update = FakeCallbackUpdate(123, ask_button.callback_data, message=preview_update.callback_query.message)
    run(handle_callback_query(ask_update, None, services, ui_state))

    cancel_button = ask_update.callback_query.message.edit_calls[-1][1].inline_keyboard[0][0]
    cancel_update = FakeCallbackUpdate(123, cancel_button.callback_data, message=ask_update.callback_query.message)
    run(handle_callback_query(cancel_update, None, services, ui_state))

    assert cancel_update.callback_query.message.edit_calls[-1][0].startswith(
        "Workspace file for Claude Code in Default Workspace\nPath: src/app.py\nhello agent"
    )


def test_workspace_search_cancel_clears_pending_input():
    from talk2agent.bots.telegram_bot import BUTTON_WORKSPACE_SEARCH, TelegramUiState, handle_callback_query, handle_text

    ui_state = TelegramUiState()
    services, _ = make_services()
    start_update = FakeUpdate(user_id=123, text=BUTTON_WORKSPACE_SEARCH)
    run(handle_text(start_update, None, services, ui_state))

    cancel_button = start_update.message.reply_markups[0].inline_keyboard[0][0]
    cancel_update = FakeCallbackUpdate(123, cancel_button.callback_data, message=FakeIncomingMessage("search"))
    run(handle_callback_query(cancel_update, None, services, ui_state))

    assert cancel_update.callback_query.message.edit_calls == [("Search cancelled.", None)]


def test_workspace_changes_button_shows_git_status(monkeypatch):
    from talk2agent.bots import telegram_bot
    from talk2agent.bots.telegram_bot import BUTTON_WORKSPACE_CHANGES, TelegramUiState, handle_text
    from talk2agent.workspace_git import WorkspaceGitStatus, WorkspaceGitStatusEntry

    monkeypatch.setattr(
        telegram_bot,
        "read_workspace_git_status",
        lambda _path: WorkspaceGitStatus(
            is_git_repo=True,
            branch_line="main",
            entries=(
                WorkspaceGitStatusEntry(" M", "src/app.py", "src/app.py"),
                WorkspaceGitStatusEntry("??", "notes.txt", "notes.txt"),
            ),
        ),
    )

    update = FakeUpdate(user_id=123, text=BUTTON_WORKSPACE_CHANGES)
    services, _ = make_services()

    run(handle_text(update, None, services, TelegramUiState()))

    assert update.message.reply_calls[0].startswith(
        "Workspace changes for Claude Code in Default Workspace\nBranch: main"
    )
    assert "[ M] src/app.py" in update.message.reply_calls[0]
    assert "[??] notes.txt" in update.message.reply_calls[0]
    assert find_inline_button(update.message.reply_markups[0], "Ask Agent With Current Changes")
    assert find_inline_button(update.message.reply_markups[0], "Start Bundle Chat With Changes")
    assert find_inline_button(update.message.reply_markups[0], "Add All Changes to Context")
    assert find_inline_button(update.message.reply_markups[0], "Open Context Bundle")


def test_workspace_changes_open_diff_and_back(monkeypatch):
    from talk2agent.bots import telegram_bot
    from talk2agent.bots.telegram_bot import (
        BUTTON_WORKSPACE_CHANGES,
        TelegramUiState,
        handle_callback_query,
        handle_text,
    )
    from talk2agent.workspace_git import WorkspaceGitDiffPreview, WorkspaceGitStatus, WorkspaceGitStatusEntry

    monkeypatch.setattr(
        telegram_bot,
        "read_workspace_git_status",
        lambda _path: WorkspaceGitStatus(
            is_git_repo=True,
            branch_line="main",
            entries=(WorkspaceGitStatusEntry(" M", "src/app.py", "src/app.py"),),
        ),
    )
    monkeypatch.setattr(
        telegram_bot,
        "read_workspace_git_diff_preview",
        lambda _root, relative_path, status_code: WorkspaceGitDiffPreview(
            relative_path=relative_path,
            status_code=status_code,
            text="diff --git a/src/app.py b/src/app.py",
            truncated=False,
        ),
    )

    ui_state = TelegramUiState()
    update = FakeUpdate(user_id=123, text=BUTTON_WORKSPACE_CHANGES)
    services, _ = make_services()

    run(handle_text(update, None, services, ui_state))

    open_button = update.message.reply_markups[0].inline_keyboard[0][0]
    diff_update = FakeCallbackUpdate(123, open_button.callback_data, message=FakeIncomingMessage("changes"))
    run(handle_callback_query(diff_update, None, services, ui_state))

    assert diff_update.callback_query.message.edit_calls[-1][0].startswith(
        "Workspace change for Claude Code in Default Workspace\nPath: src/app.py\nStatus:  M\ndiff --git"
    )

    back_button = find_inline_button(
        diff_update.callback_query.message.edit_calls[-1][1],
        "Back to Changes",
    )
    back_update = FakeCallbackUpdate(123, back_button.callback_data, message=diff_update.callback_query.message)
    run(handle_callback_query(back_update, None, services, ui_state))

    assert back_update.callback_query.message.edit_calls[-1][0].startswith(
        "Workspace changes for Claude Code in Default Workspace\nBranch: main"
    )


def test_workspace_change_can_start_agent_turn(monkeypatch):
    from talk2agent.bots import telegram_bot
    from talk2agent.bots.telegram_bot import (
        BUTTON_WORKSPACE_CHANGES,
        TelegramUiState,
        _workspace_change_agent_prompt,
        handle_callback_query,
        handle_text,
    )
    from talk2agent.workspace_git import WorkspaceGitDiffPreview, WorkspaceGitStatus, WorkspaceGitStatusEntry

    monkeypatch.setattr(
        telegram_bot,
        "read_workspace_git_status",
        lambda _path: WorkspaceGitStatus(
            is_git_repo=True,
            branch_line="main",
            entries=(WorkspaceGitStatusEntry(" M", "src/app.py", "src/app.py"),),
        ),
    )
    monkeypatch.setattr(
        telegram_bot,
        "read_workspace_git_diff_preview",
        lambda _root, relative_path, status_code: WorkspaceGitDiffPreview(
            relative_path=relative_path,
            status_code=status_code,
            text="diff --git a/src/app.py b/src/app.py",
            truncated=False,
        ),
    )

    ui_state = TelegramUiState()
    session = FakeSession(session_id="session-abc")
    services, store = make_services(session=session)
    update = FakeUpdate(user_id=123, text=BUTTON_WORKSPACE_CHANGES)

    run(handle_text(update, None, services, ui_state))

    open_button = update.message.reply_markups[0].inline_keyboard[0][0]
    diff_update = FakeCallbackUpdate(123, open_button.callback_data, message=FakeIncomingMessage("changes"))
    run(handle_callback_query(diff_update, None, services, ui_state))

    ask_button = diff_update.callback_query.message.edit_calls[-1][1].inline_keyboard[0][0]
    ask_update = FakeCallbackUpdate(123, ask_button.callback_data, message=diff_update.callback_query.message)
    run(handle_callback_query(ask_update, None, services, ui_state))

    assert ask_update.callback_query.message.edit_calls[-1][0].startswith(
        "Send your request about the change in src/app.py as the next plain text message."
    )

    request_update = FakeUpdate(user_id=123, text="Summarize this change.")
    run(handle_text(request_update, None, services, ui_state))

    assert session.prompts == [
        _workspace_change_agent_prompt("src/app.py", " M", "Summarize this change.")
    ]
    assert request_update.message.reply_calls == ["hello world"]
    assert store.record_session_usage_calls == [
        (123, "session-abc", _workspace_change_agent_prompt("src/app.py", " M", "Summarize this change."))
    ]


def test_workspace_change_preview_can_start_bundle_chat(monkeypatch):
    from talk2agent.bots import telegram_bot
    from talk2agent.bots.telegram_bot import (
        BUTTON_WORKSPACE_CHANGES,
        TelegramUiState,
        _ContextBundleItem,
        handle_callback_query,
        handle_text,
    )
    from talk2agent.workspace_git import WorkspaceGitDiffPreview, WorkspaceGitStatus, WorkspaceGitStatusEntry

    monkeypatch.setattr(
        telegram_bot,
        "read_workspace_git_status",
        lambda _path: WorkspaceGitStatus(
            is_git_repo=True,
            branch_line="main",
            entries=(WorkspaceGitStatusEntry(" M", "src/app.py", "src/app.py"),),
        ),
    )
    monkeypatch.setattr(
        telegram_bot,
        "read_workspace_git_diff_preview",
        lambda _root, relative_path, status_code: WorkspaceGitDiffPreview(
            relative_path=relative_path,
            status_code=status_code,
            text="diff --git a/src/app.py b/src/app.py",
            truncated=False,
        ),
    )

    ui_state = TelegramUiState()
    services, _ = make_services()
    update = FakeUpdate(user_id=123, text=BUTTON_WORKSPACE_CHANGES)

    run(handle_text(update, None, services, ui_state))

    open_button = update.message.reply_markups[0].inline_keyboard[0][0]
    diff_update = FakeCallbackUpdate(123, open_button.callback_data, message=FakeIncomingMessage("changes"))
    run(handle_callback_query(diff_update, None, services, ui_state))

    start_button = find_inline_button(
        diff_update.callback_query.message.edit_calls[-1][1],
        "Start Bundle Chat With Change",
    )
    start_update = FakeCallbackUpdate(123, start_button.callback_data, message=diff_update.callback_query.message)
    run(handle_callback_query(start_update, None, services, ui_state))

    assert start_update.callback_query.message.edit_calls[-1][0].startswith(
        "Added change to context bundle. Bundle chat enabled.\n"
        "Context bundle for Claude Code in Default Workspace\n"
        "Items: 1\n"
        "Bundle chat: on\n"
        "1. [change  M] src/app.py"
    )
    assert ui_state.context_bundle_chat_active(123, "claude", "default") is True
    bundle = ui_state.get_context_bundle(123, "claude", "default")
    assert bundle is not None
    assert bundle.items == [
        _ContextBundleItem(kind="change", relative_path="src/app.py", status_code=" M")
    ]


def test_workspace_change_preview_start_bundle_chat_can_go_back_to_change(monkeypatch):
    from talk2agent.bots import telegram_bot
    from talk2agent.bots.telegram_bot import (
        BUTTON_WORKSPACE_CHANGES,
        TelegramUiState,
        handle_callback_query,
        handle_text,
    )
    from talk2agent.workspace_git import WorkspaceGitDiffPreview, WorkspaceGitStatus, WorkspaceGitStatusEntry

    monkeypatch.setattr(
        telegram_bot,
        "read_workspace_git_status",
        lambda _path: WorkspaceGitStatus(
            is_git_repo=True,
            branch_line="main",
            entries=(WorkspaceGitStatusEntry(" M", "src/app.py", "src/app.py"),),
        ),
    )
    monkeypatch.setattr(
        telegram_bot,
        "read_workspace_git_diff_preview",
        lambda _root, relative_path, status_code: WorkspaceGitDiffPreview(
            relative_path=relative_path,
            status_code=status_code,
            text="diff --git a/src/app.py b/src/app.py",
            truncated=False,
        ),
    )

    ui_state = TelegramUiState()
    services, _ = make_services()
    update = FakeUpdate(user_id=123, text=BUTTON_WORKSPACE_CHANGES)

    run(handle_text(update, None, services, ui_state))

    open_button = update.message.reply_markups[0].inline_keyboard[0][0]
    diff_update = FakeCallbackUpdate(123, open_button.callback_data, message=FakeIncomingMessage("changes"))
    run(handle_callback_query(diff_update, None, services, ui_state))

    start_button = find_inline_button(
        diff_update.callback_query.message.edit_calls[-1][1],
        "Start Bundle Chat With Change",
    )
    start_update = FakeCallbackUpdate(123, start_button.callback_data, message=diff_update.callback_query.message)
    run(handle_callback_query(start_update, None, services, ui_state))

    bundle_text, bundle_markup = start_update.callback_query.message.edit_calls[-1]
    assert bundle_text.startswith(
        "Added change to context bundle. Bundle chat enabled.\n"
        "Context bundle for Claude Code in Default Workspace\n"
        "Items: 1\n"
        "Bundle chat: on\n"
        "1. [change  M] src/app.py"
    )
    assert find_inline_button(bundle_markup, "Back to Change")

    open_bundle_item_button = find_inline_button(bundle_markup, "Open 1")
    open_bundle_item_update = FakeCallbackUpdate(
        123,
        open_bundle_item_button.callback_data,
        message=start_update.callback_query.message,
    )
    run(handle_callback_query(open_bundle_item_update, None, services, ui_state))

    preview_text, preview_markup = open_bundle_item_update.callback_query.message.edit_calls[-1]
    assert preview_text.startswith(
        "Workspace change for Claude Code in Default Workspace\nPath: src/app.py\nStatus:  M\ndiff --git"
    )
    assert find_inline_button(preview_markup, "Back to Context Bundle")
    back_button = find_inline_button(preview_markup, "Back to Change")

    back_update = FakeCallbackUpdate(
        123,
        back_button.callback_data,
        message=open_bundle_item_update.callback_query.message,
    )
    run(handle_callback_query(back_update, None, services, ui_state))

    restored_change_text, restored_change_markup = back_update.callback_query.message.edit_calls[-1]
    assert restored_change_text.startswith(
        "Workspace change for Claude Code in Default Workspace\nPath: src/app.py\nStatus:  M\ndiff --git"
    )
    assert find_inline_button(restored_change_markup, "Open Context Bundle")


def test_workspace_changes_can_add_all_changes_to_context_bundle(monkeypatch):
    from talk2agent.bots import telegram_bot
    from talk2agent.bots.telegram_bot import (
        BUTTON_WORKSPACE_CHANGES,
        TelegramUiState,
        handle_callback_query,
        handle_text,
    )
    from talk2agent.workspace_git import WorkspaceGitStatus, WorkspaceGitStatusEntry

    monkeypatch.setattr(
        telegram_bot,
        "read_workspace_git_status",
        lambda _path: WorkspaceGitStatus(
            is_git_repo=True,
            branch_line="main",
            entries=(
                WorkspaceGitStatusEntry(" M", "src/app.py", "src/app.py"),
                WorkspaceGitStatusEntry("??", "notes.txt", "notes.txt"),
            ),
        ),
    )

    ui_state = TelegramUiState()
    update = FakeUpdate(user_id=123, text=BUTTON_WORKSPACE_CHANGES)
    services, _ = make_services()

    run(handle_text(update, None, services, ui_state))

    add_all_button = find_inline_button(update.message.reply_markups[0], "Add All Changes to Context")
    add_all_update = FakeCallbackUpdate(123, add_all_button.callback_data, message=FakeIncomingMessage("changes"))
    run(handle_callback_query(add_all_update, None, services, ui_state))

    edited_text, _ = add_all_update.callback_query.message.edit_calls[-1]
    assert edited_text.startswith(
        "Added 2 changes to context bundle.\nContext bundle for Claude Code in Default Workspace\nItems: 2"
    )
    assert "1. [change  M] src/app.py" in edited_text
    assert "2. [change ??] notes.txt" in edited_text


def test_workspace_changes_add_all_can_go_back_to_changes(monkeypatch):
    from talk2agent.bots import telegram_bot
    from talk2agent.bots.telegram_bot import (
        BUTTON_WORKSPACE_CHANGES,
        TelegramUiState,
        handle_callback_query,
        handle_text,
    )
    from talk2agent.workspace_git import WorkspaceGitStatus, WorkspaceGitStatusEntry

    monkeypatch.setattr(
        telegram_bot,
        "read_workspace_git_status",
        lambda _path: WorkspaceGitStatus(
            is_git_repo=True,
            branch_line="main",
            entries=(
                WorkspaceGitStatusEntry(" M", "src/app.py", "src/app.py"),
                WorkspaceGitStatusEntry("??", "notes.txt", "notes.txt"),
            ),
        ),
    )

    ui_state = TelegramUiState()
    services, _ = make_services()
    update = FakeUpdate(user_id=123, text=BUTTON_WORKSPACE_CHANGES)

    run(handle_text(update, None, services, ui_state))

    add_all_button = find_inline_button(update.message.reply_markups[0], "Add All Changes to Context")
    add_all_update = FakeCallbackUpdate(123, add_all_button.callback_data, message=FakeIncomingMessage("changes"))
    run(handle_callback_query(add_all_update, None, services, ui_state))

    bundle_text, bundle_markup = add_all_update.callback_query.message.edit_calls[-1]
    assert bundle_text.startswith(
        "Added 2 changes to context bundle.\n"
        "Context bundle for Claude Code in Default Workspace\nItems: 2"
    )
    back_button = find_inline_button(bundle_markup, "Back to Changes")

    back_update = FakeCallbackUpdate(123, back_button.callback_data, message=add_all_update.callback_query.message)
    run(handle_callback_query(back_update, None, services, ui_state))

    restored_text, restored_markup = back_update.callback_query.message.edit_calls[-1]
    assert restored_text.startswith("Workspace changes for Claude Code in Default Workspace\nBranch: main")
    assert find_inline_button(restored_markup, "Add All Changes to Context")


def test_workspace_change_preview_can_ask_with_last_request(monkeypatch):
    from talk2agent.bots import telegram_bot
    from talk2agent.bots.telegram_bot import (
        BUTTON_WORKSPACE_CHANGES,
        TelegramUiState,
        _workspace_change_agent_prompt,
        handle_callback_query,
        handle_text,
    )
    from talk2agent.workspace_git import WorkspaceGitDiffPreview, WorkspaceGitStatus, WorkspaceGitStatusEntry

    monkeypatch.setattr(
        telegram_bot,
        "read_workspace_git_status",
        lambda _path: WorkspaceGitStatus(
            is_git_repo=True,
            branch_line="main",
            entries=(WorkspaceGitStatusEntry(" M", "src/app.py", "src/app.py"),),
        ),
    )
    monkeypatch.setattr(
        telegram_bot,
        "read_workspace_git_diff_preview",
        lambda _root, relative_path, status_code: WorkspaceGitDiffPreview(
            relative_path=relative_path,
            status_code=status_code,
            text="diff --git a/src/app.py b/src/app.py",
            truncated=False,
        ),
    )

    ui_state = TelegramUiState()
    session = FakeSession(session_id="session-abc")
    services, store = make_services(session=session)

    run(handle_text(FakeUpdate(user_id=123, text="Check the current diff."), None, services, ui_state))

    update = FakeUpdate(user_id=123, text=BUTTON_WORKSPACE_CHANGES)
    run(handle_text(update, None, services, ui_state))

    open_button = update.message.reply_markups[0].inline_keyboard[0][0]
    diff_update = FakeCallbackUpdate(123, open_button.callback_data, message=FakeIncomingMessage("changes"))
    run(handle_callback_query(diff_update, None, services, ui_state))

    ask_button = find_inline_button(
        diff_update.callback_query.message.edit_calls[-1][1],
        "Ask With Last Request",
    )
    ask_update = FakeCallbackUpdate(123, ask_button.callback_data, message=diff_update.callback_query.message)
    run(handle_callback_query(ask_update, None, services, ui_state))

    assert session.prompts == [
        "Check the current diff.",
        _workspace_change_agent_prompt("src/app.py", " M", "Check the current diff."),
    ]
    assert ask_update.callback_query.message.reply_calls == ["hello world"]
    assert store.record_session_usage_calls == [
        (123, "session-abc", "Check the current diff."),
        (123, "session-abc", _workspace_change_agent_prompt("src/app.py", " M", "Check the current diff.")),
    ]


def test_workspace_changes_add_all_reports_existing_bundle_items(monkeypatch):
    from talk2agent.bots import telegram_bot
    from talk2agent.bots.telegram_bot import (
        BUTTON_WORKSPACE_CHANGES,
        TelegramUiState,
        _ContextBundleItem,
        handle_callback_query,
        handle_text,
    )
    from talk2agent.workspace_git import WorkspaceGitStatus, WorkspaceGitStatusEntry

    monkeypatch.setattr(
        telegram_bot,
        "read_workspace_git_status",
        lambda _path: WorkspaceGitStatus(
            is_git_repo=True,
            branch_line="main",
            entries=(
                WorkspaceGitStatusEntry(" M", "src/app.py", "src/app.py"),
                WorkspaceGitStatusEntry("??", "notes.txt", "notes.txt"),
            ),
        ),
    )

    ui_state = TelegramUiState()
    ui_state.add_context_item(
        123,
        "claude",
        "default",
        _ContextBundleItem(kind="change", relative_path="src/app.py", status_code=" M"),
    )
    update = FakeUpdate(user_id=123, text=BUTTON_WORKSPACE_CHANGES)
    services, _ = make_services()

    run(handle_text(update, None, services, ui_state))

    add_all_button = find_inline_button(update.message.reply_markups[0], "Add All Changes to Context")
    add_all_update = FakeCallbackUpdate(123, add_all_button.callback_data, message=FakeIncomingMessage("changes"))
    run(handle_callback_query(add_all_update, None, services, ui_state))

    edited_text, _ = add_all_update.callback_query.message.edit_calls[-1]
    assert edited_text.startswith(
        "Added 1 change to context bundle. 1 change was already present.\nContext bundle for Claude Code in Default Workspace\nItems: 2"
    )
    assert "1. [change  M] src/app.py" in edited_text
    assert "2. [change ??] notes.txt" in edited_text


def test_workspace_changes_can_ask_agent_with_current_changes(monkeypatch):
    from talk2agent.bots import telegram_bot
    from talk2agent.bots.telegram_bot import (
        BUTTON_WORKSPACE_CHANGES,
        TelegramUiState,
        _ContextBundleItem,
        _context_items_agent_prompt,
        handle_callback_query,
        handle_text,
    )
    from talk2agent.workspace_git import WorkspaceGitStatus, WorkspaceGitStatusEntry

    monkeypatch.setattr(
        telegram_bot,
        "read_workspace_git_status",
        lambda _path: WorkspaceGitStatus(
            is_git_repo=True,
            branch_line="main",
            entries=(
                WorkspaceGitStatusEntry(" M", "src/app.py", "src/app.py"),
                WorkspaceGitStatusEntry("??", "notes.txt", "notes.txt"),
            ),
        ),
    )

    ui_state = TelegramUiState()
    session = FakeSession(session_id="session-abc")
    services, store = make_services(session=session)
    update = FakeUpdate(user_id=123, text=BUTTON_WORKSPACE_CHANGES)

    run(handle_text(update, None, services, ui_state))

    ask_button = find_inline_button(update.message.reply_markups[0], "Ask Agent With Current Changes")
    ask_update = FakeCallbackUpdate(123, ask_button.callback_data, message=FakeIncomingMessage("changes"))
    run(handle_callback_query(ask_update, None, services, ui_state))

    assert ask_update.callback_query.message.edit_calls[-1][0].startswith(
        "Send your request about the current workspace changes as the next plain text message."
    )

    request_update = FakeUpdate(user_id=123, text="Review these changes.")
    run(handle_text(request_update, None, services, ui_state))

    expected_prompt = _context_items_agent_prompt(
        (
            _ContextBundleItem(kind="change", relative_path="src/app.py", status_code=" M"),
            _ContextBundleItem(kind="change", relative_path="notes.txt", status_code="??"),
        ),
        "Review these changes.",
        context_label="current workspace changes",
    )
    assert session.prompts == [expected_prompt]
    assert request_update.message.reply_calls == ["hello world"]
    assert store.record_session_usage_calls == [(123, "session-abc", expected_prompt)]


def test_workspace_changes_can_ask_with_last_request(monkeypatch):
    from talk2agent.bots import telegram_bot
    from talk2agent.bots.telegram_bot import (
        BUTTON_WORKSPACE_CHANGES,
        TelegramUiState,
        _ContextBundleItem,
        _context_items_agent_prompt,
        handle_callback_query,
        handle_text,
    )
    from talk2agent.workspace_git import WorkspaceGitStatus, WorkspaceGitStatusEntry

    monkeypatch.setattr(
        telegram_bot,
        "read_workspace_git_status",
        lambda _path: WorkspaceGitStatus(
            is_git_repo=True,
            branch_line="main",
            entries=(
                WorkspaceGitStatusEntry(" M", "src/app.py", "src/app.py"),
                WorkspaceGitStatusEntry("??", "notes.txt", "notes.txt"),
            ),
        ),
    )

    ui_state = TelegramUiState()
    session = FakeSession(session_id="session-abc")
    services, store = make_services(session=session)

    run(handle_text(FakeUpdate(user_id=123, text="Review these changes."), None, services, ui_state))

    update = FakeUpdate(user_id=123, text=BUTTON_WORKSPACE_CHANGES)
    run(handle_text(update, None, services, ui_state))

    ask_button = find_inline_button(update.message.reply_markups[0], "Ask With Last Request")
    ask_update = FakeCallbackUpdate(123, ask_button.callback_data, message=FakeIncomingMessage("changes"))
    run(handle_callback_query(ask_update, None, services, ui_state))

    expected_prompt = _context_items_agent_prompt(
        (
            _ContextBundleItem(kind="change", relative_path="src/app.py", status_code=" M"),
            _ContextBundleItem(kind="change", relative_path="notes.txt", status_code="??"),
        ),
        "Review these changes.",
        context_label="current workspace changes",
    )
    assert session.prompts == ["Review these changes.", expected_prompt]
    assert ask_update.callback_query.message.reply_calls == ["hello world"]
    assert store.record_session_usage_calls == [
        (123, "session-abc", "Review these changes."),
        (123, "session-abc", expected_prompt),
    ]


def test_context_bundle_can_remove_and_clear_items():
    from talk2agent.bots.telegram_bot import (
        BUTTON_CONTEXT_BUNDLE,
        TelegramUiState,
        _ContextBundleItem,
        handle_callback_query,
        handle_text,
    )

    ui_state = TelegramUiState()
    ui_state.add_context_item(
        123,
        "claude",
        "default",
        _ContextBundleItem(kind="file", relative_path="notes.txt"),
    )
    ui_state.add_context_item(
        123,
        "claude",
        "default",
        _ContextBundleItem(kind="change", relative_path="src/app.py", status_code=" M"),
    )

    update = FakeUpdate(user_id=123, text=BUTTON_CONTEXT_BUNDLE)
    services, _ = make_services()

    run(handle_text(update, None, services, ui_state))

    remove_button = find_inline_button(update.message.reply_markups[0], "Remove 1")
    remove_update = FakeCallbackUpdate(123, remove_button.callback_data, message=FakeIncomingMessage("bundle"))
    run(handle_callback_query(remove_update, None, services, ui_state))

    removed_text, removed_markup = remove_update.callback_query.message.edit_calls[-1]
    assert removed_text.startswith(
        "Removed item from context bundle.\nContext bundle for Claude Code in Default Workspace\nItems: 1"
    )
    assert "[file] notes.txt" not in removed_text
    assert "1. [change  M] src/app.py" in removed_text

    clear_button = find_inline_button(removed_markup, "Clear Bundle")
    clear_update = FakeCallbackUpdate(123, clear_button.callback_data, message=remove_update.callback_query.message)
    run(handle_callback_query(clear_update, None, services, ui_state))

    assert clear_update.callback_query.message.edit_calls[-1] == (
        "Cleared context bundle.\nContext bundle for Claude Code in Default Workspace\nContext bundle is empty.",
        None,
    )


def test_context_bundle_can_open_file_preview_and_back(tmp_path):
    from talk2agent.bots.telegram_bot import (
        BUTTON_CONTEXT_BUNDLE,
        TelegramUiState,
        _ContextBundleItem,
        handle_callback_query,
        handle_text,
    )

    (tmp_path / "notes.txt").write_text("bundle note\n", encoding="utf-8")

    ui_state = TelegramUiState()
    ui_state.add_context_item(
        123,
        "claude",
        "default",
        _ContextBundleItem(kind="file", relative_path="notes.txt"),
    )

    update = FakeUpdate(user_id=123, text=BUTTON_CONTEXT_BUNDLE)
    services, _ = make_services(workspace_path=str(tmp_path))

    run(handle_text(update, None, services, ui_state))

    open_button = find_inline_button(update.message.reply_markups[0], "Open 1")
    open_update = FakeCallbackUpdate(123, open_button.callback_data, message=FakeIncomingMessage("bundle"))
    run(handle_callback_query(open_update, None, services, ui_state))

    preview_text, preview_markup = open_update.callback_query.message.edit_calls[-1]
    assert preview_text.startswith(
        "Workspace file for Claude Code in Default Workspace\nPath: notes.txt\nbundle note"
    )

    back_button = preview_markup.inline_keyboard[1][0]
    back_update = FakeCallbackUpdate(123, back_button.callback_data, message=open_update.callback_query.message)
    run(handle_callback_query(back_update, None, services, ui_state))

    assert back_update.callback_query.message.edit_calls[-1][0].startswith(
        "Context bundle for Claude Code in Default Workspace\nItems: 1\nBundle chat: off\n1. [file] notes.txt"
    )


def test_context_bundle_file_preview_can_remove_current_item(tmp_path):
    from talk2agent.bots.telegram_bot import (
        BUTTON_CONTEXT_BUNDLE,
        TelegramUiState,
        _ContextBundleItem,
        handle_callback_query,
        handle_text,
    )

    (tmp_path / "notes.txt").write_text("bundle note\n", encoding="utf-8")

    ui_state = TelegramUiState()
    ui_state.add_context_item(
        123,
        "claude",
        "default",
        _ContextBundleItem(kind="file", relative_path="notes.txt"),
    )
    ui_state.add_context_item(
        123,
        "claude",
        "default",
        _ContextBundleItem(kind="file", relative_path="keep.txt"),
    )

    update = FakeUpdate(user_id=123, text=BUTTON_CONTEXT_BUNDLE)
    services, _ = make_services(workspace_path=str(tmp_path))

    run(handle_text(update, None, services, ui_state))

    open_button = find_inline_button(update.message.reply_markups[0], "Open 1")
    open_update = FakeCallbackUpdate(123, open_button.callback_data, message=FakeIncomingMessage("bundle"))
    run(handle_callback_query(open_update, None, services, ui_state))

    _, preview_markup = open_update.callback_query.message.edit_calls[-1]
    remove_button = find_inline_button(preview_markup, "Remove From Context")
    remove_update = FakeCallbackUpdate(123, remove_button.callback_data, message=open_update.callback_query.message)
    run(handle_callback_query(remove_update, None, services, ui_state))

    assert remove_update.callback_query.message.edit_calls[-1][0].startswith(
        "Removed item from context bundle.\n"
        "Context bundle for Claude Code in Default Workspace\n"
        "Items: 1\n"
        "Bundle chat: off\n"
        "1. [file] keep.txt"
    )
    bundle = ui_state.get_context_bundle(123, "claude", "default")
    assert bundle is not None
    assert bundle.items == [_ContextBundleItem(kind="file", relative_path="keep.txt")]


def test_context_bundle_can_open_change_preview_and_back(monkeypatch):
    from talk2agent.bots import telegram_bot
    from talk2agent.bots.telegram_bot import (
        BUTTON_CONTEXT_BUNDLE,
        TelegramUiState,
        _ContextBundleItem,
        handle_callback_query,
        handle_text,
    )
    from talk2agent.workspace_git import WorkspaceGitDiffPreview

    monkeypatch.setattr(
        telegram_bot,
        "read_workspace_git_diff_preview",
        lambda _root, relative_path, status_code: WorkspaceGitDiffPreview(
            relative_path=relative_path,
            status_code=status_code,
            text="diff --git a/src/app.py b/src/app.py",
            truncated=False,
        ),
    )

    ui_state = TelegramUiState()
    ui_state.add_context_item(
        123,
        "claude",
        "default",
        _ContextBundleItem(kind="change", relative_path="src/app.py", status_code=" M"),
    )

    update = FakeUpdate(user_id=123, text=BUTTON_CONTEXT_BUNDLE)
    services, _ = make_services()

    run(handle_text(update, None, services, ui_state))

    open_button = find_inline_button(update.message.reply_markups[0], "Open 1")
    open_update = FakeCallbackUpdate(123, open_button.callback_data, message=FakeIncomingMessage("bundle"))
    run(handle_callback_query(open_update, None, services, ui_state))

    preview_text, preview_markup = open_update.callback_query.message.edit_calls[-1]
    assert preview_text.startswith(
        "Workspace change for Claude Code in Default Workspace\nPath: src/app.py\nStatus:  M\ndiff --git"
    )

    back_button = preview_markup.inline_keyboard[1][0]
    back_update = FakeCallbackUpdate(123, back_button.callback_data, message=open_update.callback_query.message)
    run(handle_callback_query(back_update, None, services, ui_state))

    assert back_update.callback_query.message.edit_calls[-1][0].startswith(
        "Context bundle for Claude Code in Default Workspace\nItems: 1\nBundle chat: off\n1. [change  M] src/app.py"
    )


def test_context_bundle_change_preview_can_remove_current_item(monkeypatch):
    from talk2agent.bots import telegram_bot
    from talk2agent.bots.telegram_bot import (
        BUTTON_CONTEXT_BUNDLE,
        TelegramUiState,
        _ContextBundleItem,
        handle_callback_query,
        handle_text,
    )
    from talk2agent.workspace_git import WorkspaceGitDiffPreview

    monkeypatch.setattr(
        telegram_bot,
        "read_workspace_git_diff_preview",
        lambda _root, relative_path, status_code: WorkspaceGitDiffPreview(
            relative_path=relative_path,
            status_code=status_code,
            text="diff --git a/src/app.py b/src/app.py",
            truncated=False,
        ),
    )

    ui_state = TelegramUiState()
    ui_state.add_context_item(
        123,
        "claude",
        "default",
        _ContextBundleItem(kind="change", relative_path="src/app.py", status_code=" M"),
    )
    ui_state.add_context_item(
        123,
        "claude",
        "default",
        _ContextBundleItem(kind="change", relative_path="src/keep.py", status_code="??"),
    )

    update = FakeUpdate(user_id=123, text=BUTTON_CONTEXT_BUNDLE)
    services, _ = make_services()

    run(handle_text(update, None, services, ui_state))

    open_button = find_inline_button(update.message.reply_markups[0], "Open 1")
    open_update = FakeCallbackUpdate(123, open_button.callback_data, message=FakeIncomingMessage("bundle"))
    run(handle_callback_query(open_update, None, services, ui_state))

    _, preview_markup = open_update.callback_query.message.edit_calls[-1]
    remove_button = find_inline_button(preview_markup, "Remove From Context")
    remove_update = FakeCallbackUpdate(123, remove_button.callback_data, message=open_update.callback_query.message)
    run(handle_callback_query(remove_update, None, services, ui_state))

    assert remove_update.callback_query.message.edit_calls[-1][0].startswith(
        "Removed item from context bundle.\n"
        "Context bundle for Claude Code in Default Workspace\n"
        "Items: 1\n"
        "Bundle chat: off\n"
        "1. [change ??] src/keep.py"
    )
    bundle = ui_state.get_context_bundle(123, "claude", "default")
    assert bundle is not None
    assert bundle.items == [_ContextBundleItem(kind="change", relative_path="src/keep.py", status_code="??")]


def test_context_bundle_clear_turns_off_bundle_chat():
    from talk2agent.bots.telegram_bot import (
        BUTTON_CONTEXT_BUNDLE,
        TelegramUiState,
        _ContextBundleItem,
        handle_callback_query,
        handle_text,
    )

    ui_state = TelegramUiState()
    ui_state.add_context_item(
        123,
        "claude",
        "default",
        _ContextBundleItem(kind="file", relative_path="notes.txt"),
    )

    update = FakeUpdate(user_id=123, text=BUTTON_CONTEXT_BUNDLE)
    services, _ = make_services()

    run(handle_text(update, None, services, ui_state))

    start_button = find_inline_button(update.message.reply_markups[0], "Start Bundle Chat")
    start_update = FakeCallbackUpdate(123, start_button.callback_data, message=FakeIncomingMessage("bundle"))
    run(handle_callback_query(start_update, None, services, ui_state))

    clear_button = find_inline_button(start_update.callback_query.message.edit_calls[-1][1], "Clear Bundle")
    clear_update = FakeCallbackUpdate(123, clear_button.callback_data, message=start_update.callback_query.message)
    run(handle_callback_query(clear_update, None, services, ui_state))

    assert clear_update.callback_query.message.edit_calls[-1] == (
        "Cleared context bundle. Bundle chat was turned off.\nContext bundle for Claude Code in Default Workspace\nContext bundle is empty.",
        None,
    )


def test_model_mode_button_shows_direct_choices_and_current_is_noop():
    from talk2agent.bots.telegram_bot import BUTTON_MODEL_MODE, TelegramUiState, handle_callback_query, handle_text

    ui_state = TelegramUiState()
    update = FakeUpdate(user_id=123, text=BUTTON_MODEL_MODE)
    services, _ = make_services()

    run(handle_text(update, None, services, ui_state))

    markup = update.message.reply_markups[0]
    assert find_inline_button(markup, "Current Model: GPT-5.4")
    assert find_inline_button(markup, "Model: GPT-5.4 Mini")
    assert find_inline_button(markup, "Current Mode: xhigh")
    assert find_inline_button(markup, "Mode: low")
    assert find_inline_button(markup, "Open Model 1")
    assert find_inline_button(markup, "Open Model 2")
    assert find_inline_button(markup, "Open Mode 1")
    assert find_inline_button(markup, "Open Mode 2")

    current_button = markup.inline_keyboard[0][0]
    callback_update = FakeCallbackUpdate(123, current_button.callback_data, message=FakeIncomingMessage("model"))
    run(handle_callback_query(callback_update, None, services, ui_state))

    assert callback_update.callback_query.answers == [("Already using GPT-5.4.", False)]
    assert callback_update.callback_query.message.edit_calls == []


def test_model_mode_button_starts_session_when_none_exists():
    from talk2agent.bots.telegram_bot import BUTTON_MODEL_MODE, TelegramUiState, handle_text

    ui_state = TelegramUiState()
    update = FakeUpdate(user_id=123, text=BUTTON_MODEL_MODE)
    application = FakeApplication()
    session = FakeSession(available_commands=[FakeCommand("status", "Show status")])
    services, store = make_services(session=session, peek_session=None)

    run(handle_text(update, make_context(application=application), services, ui_state))

    assert store.peek_calls == [123]
    assert store.close_idle_calls
    assert store.get_or_create_calls == [123]
    assert store.record_session_usage_calls == [(123, "session-123", None)]
    assert update.message.reply_calls[0].startswith(
        "Started session for model / mode controls.\nSession: session-123"
    )
    assert [command.command for command in application.bot.set_my_commands_calls[0][0]] == ["status"]


def test_model_mode_selection_updates_choice_and_edits_menu():
    from talk2agent.bots.telegram_bot import BUTTON_MODEL_MODE, TelegramUiState, handle_callback_query, handle_text

    ui_state = TelegramUiState()
    update = FakeUpdate(user_id=123, text=BUTTON_MODEL_MODE)
    services, store = make_services()

    run(handle_text(update, None, services, ui_state))

    markup = update.message.reply_markups[0]
    mini_button = find_inline_button(markup, "Model: GPT-5.4 Mini")
    callback_update = FakeCallbackUpdate(123, mini_button.callback_data, message=FakeIncomingMessage("model"))
    run(handle_callback_query(callback_update, None, services, ui_state))

    assert services.final_session.set_selection_calls == [("model", "gpt-5.4-mini")]
    assert store.record_session_usage_calls == [(123, "session-123", None)]
    assert callback_update.callback_query.message.edit_calls[-1][0].startswith(
        "Updated model to GPT-5.4 Mini.\nSession: session-123"
    )


def test_model_mode_view_shows_retry_shortcuts_when_last_turn_exists():
    from talk2agent.acp.agent_session import PromptText
    from talk2agent.bots.telegram_bot import BUTTON_MODEL_MODE, TelegramUiState, _ReplayTurn, handle_text

    ui_state = TelegramUiState()
    ui_state.set_last_turn(
        123,
        _ReplayTurn(
            provider="claude",
            workspace_id="default",
            prompt_items=(PromptText("hello"),),
            title_hint="hello",
        ),
    )
    update = FakeUpdate(user_id=123, text=BUTTON_MODEL_MODE)
    services, _ = make_services()

    run(handle_text(update, None, services, ui_state))

    markup = update.message.reply_markups[0]
    assert find_inline_button(markup, "Model+Retry: GPT-5.4 Mini").text == "Model+Retry: GPT-5.4 Mini"
    assert find_inline_button(markup, "Mode+Retry: low").text == "Mode+Retry: low"


def test_model_mode_selection_retry_replays_last_turn():
    from talk2agent.acp.agent_session import PromptText
    from talk2agent.bots.telegram_bot import (
        BUTTON_MODEL_MODE,
        TelegramUiState,
        _ReplayTurn,
        handle_callback_query,
        handle_text,
    )

    ui_state = TelegramUiState()
    ui_state.set_last_turn(
        123,
        _ReplayTurn(
            provider="claude",
            workspace_id="default",
            prompt_items=(PromptText("hello"),),
            title_hint="hello",
        ),
    )
    update = FakeUpdate(user_id=123, text=BUTTON_MODEL_MODE)
    callback_message = FakeIncomingMessage("model")
    services, store = make_services()

    run(handle_text(update, None, services, ui_state))

    retry_button = find_inline_button(update.message.reply_markups[0], "Model+Retry: GPT-5.4 Mini")
    callback_update = FakeCallbackUpdate(123, retry_button.callback_data, message=callback_message)
    run(handle_callback_query(callback_update, make_context(application=FakeApplication()), services, ui_state))

    assert services.final_session.set_selection_calls == [("model", "gpt-5.4-mini")]
    assert [text for text, _ in callback_message.edit_calls] == [
        "Updated model to GPT-5.4 Mini.\nRetrying last turn with the updated setting..."
    ]
    assert services.final_session.prompt_items == [(PromptText("hello"),)]
    assert callback_message.reply_calls == ["hello world"]
    assert store.record_session_usage_calls == [
        (123, "session-123", None),
        (123, "session-123", "hello"),
    ]


def test_model_mode_selection_refreshes_command_menu_for_current_session():
    from talk2agent.bots.telegram_bot import BUTTON_MODEL_MODE, TelegramUiState, handle_callback_query, handle_text

    ui_state = TelegramUiState()
    update = FakeUpdate(user_id=123, text=BUTTON_MODEL_MODE)
    application = FakeApplication()
    session = FakeSession(available_commands=[FakeCommand("status", "Show status")])

    async def set_selection(kind, value):
        session.set_selection_calls.append((kind, value))
        selection = session.selections[kind]
        selection.current_value = value
        session.available_commands = (
            FakeCommand("status", "Show status"),
            FakeCommand("plan", "Plan work"),
        )
        return selection

    session.set_selection = set_selection
    services, _ = make_services(session=session)

    run(handle_text(update, None, services, ui_state))

    markup = update.message.reply_markups[0]
    mini_button = find_inline_button(markup, "Model: GPT-5.4 Mini")
    callback_update = FakeCallbackUpdate(123, mini_button.callback_data, message=FakeIncomingMessage("model"))
    run(handle_callback_query(callback_update, make_context(application=application), services, ui_state))

    assert [command.command for command in application.bot.set_my_commands_calls[0][0]] == [
        "status",
        "plan",
    ]


def test_model_mode_choice_detail_can_open_and_back():
    from talk2agent.bots.telegram_bot import BUTTON_MODEL_MODE, TelegramUiState, handle_callback_query, handle_text

    ui_state = TelegramUiState()
    update = FakeUpdate(user_id=123, text=BUTTON_MODEL_MODE)
    services, _ = make_services()
    services.final_session.selections["model"].choices[1].description = "Smaller GPT-5.4 profile for lighter work."

    run(handle_text(update, None, services, ui_state))

    callback_message = FakeIncomingMessage("model")
    open_button = find_inline_button(update.message.reply_markups[0], "Open Model 2")
    open_update = FakeCallbackUpdate(123, open_button.callback_data, message=callback_message)
    run(handle_callback_query(open_update, None, services, ui_state))

    detail_text, detail_markup = callback_message.edit_calls[-1]
    assert detail_text.startswith("Model choice for Claude Code in Default Workspace")
    assert "Session: session-123" in detail_text
    assert "Choice: 2/2" in detail_text
    assert "Current selection: GPT-5.4" in detail_text
    assert "This choice is current: no" in detail_text
    assert "Label: GPT-5.4 Mini" in detail_text
    assert "Value: gpt-5.4-mini" in detail_text
    assert "Description:" in detail_text
    assert "Smaller GPT-5.4 profile for lighter work." in detail_text
    assert find_inline_button(detail_markup, "Use Model")

    back_button = find_inline_button(detail_markup, "Back to Model / Mode")
    back_update = FakeCallbackUpdate(123, back_button.callback_data, message=callback_message)
    run(handle_callback_query(back_update, None, services, ui_state))

    restored_text, restored_markup = callback_message.edit_calls[-1]
    assert restored_text.startswith("Session: session-123")
    assert find_inline_button(restored_markup, "Open Model 2")


def test_failed_text_turn_clears_session_bound_interactions_and_syncs_commands():
    from talk2agent.bots.telegram_bot import (
        CALLBACK_PREFIX,
        TelegramUiState,
        _ContextBundleItem,
        _run_agent_text_turn_on_message,
        handle_callback_query,
    )

    class FakeCancelableTask:
        def __init__(self):
            self.cancel_calls = 0

        def cancel(self):
            self.cancel_calls += 1

    ui_state = TelegramUiState()
    ui_state.set_pending_text_action(123, "workspace_search")
    ui_state.set_agent_command_aliases(123, {"old_status": "old_status"})
    ui_state.add_context_item(
        123,
        "codex",
        "default",
        _ContextBundleItem(kind="file", relative_path="notes.txt"),
    )
    assert ui_state.enable_context_bundle_chat(123, "codex", "default") is True
    ui_state.add_media_group_message(123, "group-1", FakeIncomingMessage(caption="photo"))
    task = FakeCancelableTask()
    ui_state.replace_media_group_task(123, "group-1", task)
    stale_token = ui_state.create(123, "workspace_page", relative_path="", page=0)

    application = FakeApplication()
    session = FakeSession(
        error=RuntimeError("boom"),
        raise_before_stream=True,
        available_commands=[FakeCommand("status", "Show status")],
    )
    message = FakeIncomingMessage("hello")
    services, store = make_services(
        provider="codex",
        session=session,
        history_entries=[build_history_entry("session-old", "Earlier")],
    )

    run(
        _run_agent_text_turn_on_message(
            message,
            123,
            services,
            ui_state,
            "hello",
            application=application,
        )
    )

    assert store.invalidate_calls == [(123, session)]
    assert store.peek_session is None
    assert store.record_session_usage_calls == []
    assert [text for _, text in message.draft_calls] == ["Thinking..."]
    assert message.reply_calls[0].startswith(
        "Request failed. The current live session for Codex in Default Workspace was closed."
    )
    recovery_markup = message.reply_markups[0]
    find_inline_button(recovery_markup, "Retry Last Turn")
    find_inline_button(recovery_markup, "Fork Last Turn")
    find_inline_button(recovery_markup, "New Session")
    history_button = find_inline_button(recovery_markup, "Session History")
    find_inline_button(recovery_markup, "Model / Mode")
    find_inline_button(recovery_markup, "Switch Agent")
    find_inline_button(recovery_markup, "Switch Workspace")
    assert ui_state.get_pending_text_action(123) is None
    assert ui_state.resolve_agent_command(123, "old_status") is None
    assert ui_state.resolve_agent_command(123, "status") == "status"
    assert ui_state.get(stale_token) is None
    assert ui_state.context_bundle_chat_active(123, "codex", "default") is True
    assert ui_state.pop_media_group_messages(123, "group-1") == ()
    assert task.cancel_calls == 1
    assert [command.command for command in application.bot.set_my_commands_calls[0][0]] == [
        "status",
    ]

    history_update = FakeCallbackUpdate(123, history_button.callback_data, message=message)
    run(handle_callback_query(history_update, None, services, ui_state))
    assert message.reply_calls[-1].startswith("Session history for Codex in Default Workspace")

    stale_update = FakeCallbackUpdate(
        123,
        f"{CALLBACK_PREFIX}{stale_token}",
        message=FakeIncomingMessage("stale"),
    )
    run(handle_callback_query(stale_update, None, services, ui_state))
    assert stale_update.callback_query.answers == [("This button has expired.", True)]


def test_failed_text_turn_recovery_retry_replays_last_turn():
    from talk2agent.bots.telegram_bot import (
        TelegramUiState,
        _run_agent_text_turn_on_message,
        handle_callback_query,
    )

    session = FakeSession(available_commands=[FakeCommand("status", "Show status")])

    async def failing_run_turn(prompt_text, stream):
        session.prompts.append(prompt_text)
        raise RuntimeError("boom")

    session.run_turn = failing_run_turn
    message = FakeIncomingMessage("hello")
    ui_state = TelegramUiState()
    services, store = make_services(provider="codex", session=session)

    run(
        _run_agent_text_turn_on_message(
            message,
            123,
            services,
            ui_state,
            "hello",
            application=FakeApplication(),
        )
    )

    retry_button = find_inline_button(message.reply_markups[0], "Retry Last Turn")
    session.run_turn = FakeSession.run_turn.__get__(session, FakeSession)
    retry_update = FakeCallbackUpdate(123, retry_button.callback_data, message=message)

    run(handle_callback_query(retry_update, None, services, ui_state))

    assert store.invalidate_calls == [(123, session)]
    assert session.prompts == ["hello"]
    assert len(session.prompt_items) == 1
    assert session.prompt_items[0][0].text == "hello"
    assert message.reply_calls[-1] == "hello world"
    assert store.record_session_usage_calls == [(123, "session-123", "hello")]


def test_failed_text_turn_recovery_fork_replays_last_turn_in_new_session():
    from talk2agent.bots.telegram_bot import (
        TelegramUiState,
        _run_agent_text_turn_on_message,
        handle_callback_query,
    )

    session = FakeSession(available_commands=[FakeCommand("status", "Show status")])

    async def failing_run_turn(prompt_text, stream):
        session.prompts.append(prompt_text)
        raise RuntimeError("boom")

    session.run_turn = failing_run_turn
    message = FakeIncomingMessage("hello")
    ui_state = TelegramUiState()
    services, store = make_services(provider="codex", session=session)

    run(
        _run_agent_text_turn_on_message(
            message,
            123,
            services,
            ui_state,
            "hello",
            application=FakeApplication(),
        )
    )

    fork_button = find_inline_button(message.reply_markups[0], "Fork Last Turn")
    session.run_turn = FakeSession.run_turn.__get__(session, FakeSession)
    session.error = None
    session.raise_before_stream = False
    fork_update = FakeCallbackUpdate(123, fork_button.callback_data, message=message)

    run(handle_callback_query(fork_update, None, services, ui_state))

    assert store.invalidate_calls == [(123, session)]
    assert store.reset_calls == [123]
    assert session.prompts == ["hello"]
    assert len(session.prompt_items) == 1
    assert session.prompt_items[0][0].text == "hello"
    assert message.reply_calls[-1] == "hello world"
    assert store.record_session_usage_calls == [(123, "session-123", "hello")]


def test_sync_agent_commands_for_user_sets_aliases_and_bot_commands():
    from telegram import BotCommandScopeChat
    from talk2agent.bots.telegram_bot import TelegramUiState, _restore_agent_command_text, _sync_agent_commands_for_user

    ui_state = TelegramUiState()
    application = FakeApplication()
    commands = [FakeCommand("status", "Show status"), FakeCommand("debug_status", "Conflicting")]

    run(_sync_agent_commands_for_user(application, ui_state, 123, commands))

    assert [command.command for command in application.bot.set_my_commands_calls[0][0]] == [
        "status",
        "agent_debug_status",
    ]
    assert isinstance(application.bot.set_my_commands_calls[0][1], BotCommandScopeChat)
    assert application.bot.set_my_commands_calls[0][1].chat_id == 123
    assert _restore_agent_command_text("/agent_debug_status now", 123, ui_state) == "/debug_status now"


def test_handle_attachment_photo_forwards_image_prompt_items():
    from talk2agent.acp.agent_session import PromptImage, PromptText
    from talk2agent.bots.telegram_bot import TelegramUiState, handle_attachment

    ui_state = TelegramUiState()
    application = FakeApplication()
    message = FakeIncomingMessage(
        caption="Describe this screenshot",
        photo=[FakePhotoSize(payload=b"photo-bytes", file_unique_id="photo-123")],
    )
    update = FakeUpdate(user_id=123, message=message)
    services, store = make_services()

    run(handle_attachment(update, make_context(application=application), services, ui_state))

    assert len(services.final_session.prompt_items) == 1
    prompt_items = services.final_session.prompt_items[0]
    assert isinstance(prompt_items[0], PromptText)
    assert prompt_items[0].text == "Describe this screenshot"
    assert isinstance(prompt_items[1], PromptImage)
    assert prompt_items[1].mime_type == "image/jpeg"
    assert prompt_items[1].uri == "telegram://photo/photo-123"
    assert prompt_items[1].data == "cGhvdG8tYnl0ZXM="
    assert store.record_session_usage_calls == [
        (123, "session-123", "Describe this screenshot")
    ]


def test_retry_last_turn_button_replays_previous_attachment_turn():
    from talk2agent.acp.agent_session import PromptImage, PromptText
    from talk2agent.bots.telegram_bot import BUTTON_RETRY_LAST_TURN, TelegramUiState, handle_attachment, handle_text

    ui_state = TelegramUiState()
    message = FakeIncomingMessage(
        caption="Describe this screenshot",
        photo=[FakePhotoSize(payload=b"photo-bytes", file_unique_id="photo-retry")],
    )
    update = FakeUpdate(user_id=123, message=message)
    retry_update = FakeUpdate(user_id=123, text=BUTTON_RETRY_LAST_TURN)
    services, store = make_services()

    run(handle_attachment(update, None, services, ui_state))
    run(handle_text(retry_update, None, services, ui_state))

    assert len(services.final_session.prompt_items) == 2
    first_prompt_items = services.final_session.prompt_items[0]
    second_prompt_items = services.final_session.prompt_items[1]
    assert first_prompt_items == second_prompt_items
    assert isinstance(second_prompt_items[0], PromptText)
    assert second_prompt_items[0].text == "Describe this screenshot"
    assert isinstance(second_prompt_items[1], PromptImage)
    assert second_prompt_items[1].uri == "telegram://photo/photo-retry"
    assert retry_update.message.reply_calls == ["hello world"]
    assert store.record_session_usage_calls == [
        (123, "session-123", "Describe this screenshot"),
        (123, "session-123", "Describe this screenshot"),
    ]


def test_fork_last_turn_button_replays_previous_attachment_turn_in_new_session():
    from talk2agent.acp.agent_session import PromptImage, PromptText
    from talk2agent.bots.telegram_bot import BUTTON_FORK_LAST_TURN, TelegramUiState, handle_attachment, handle_text

    ui_state = TelegramUiState()
    message = FakeIncomingMessage(
        caption="Describe this screenshot",
        photo=[FakePhotoSize(payload=b"photo-bytes", file_unique_id="photo-fork")],
    )
    update = FakeUpdate(user_id=123, message=message)
    fork_update = FakeUpdate(user_id=123, text=BUTTON_FORK_LAST_TURN)
    services, store = make_services()

    run(handle_attachment(update, None, services, ui_state))
    run(handle_text(fork_update, None, services, ui_state))

    assert store.reset_calls == [123]
    assert len(services.final_session.prompt_items) == 2
    first_prompt_items = services.final_session.prompt_items[0]
    second_prompt_items = services.final_session.prompt_items[1]
    assert first_prompt_items == second_prompt_items
    assert isinstance(second_prompt_items[0], PromptText)
    assert second_prompt_items[0].text == "Describe this screenshot"
    assert isinstance(second_prompt_items[1], PromptImage)
    assert second_prompt_items[1].uri == "telegram://photo/photo-fork"
    assert fork_update.message.reply_calls == ["hello world"]
    assert store.record_session_usage_calls == [
        (123, "session-123", "Describe this screenshot"),
        (123, "session-123", "Describe this screenshot"),
    ]


def test_retry_last_turn_recoerces_attachment_for_new_provider_capabilities(tmp_path):
    from talk2agent.acp.agent_session import PromptImage, PromptText
    from talk2agent.bots.telegram_bot import BUTTON_RETRY_LAST_TURN, TelegramUiState, handle_attachment, handle_text

    ui_state = TelegramUiState()
    message = FakeIncomingMessage(
        caption="Describe this screenshot",
        photo=[FakePhotoSize(payload=b"photo-bytes", file_unique_id="photo-cross-provider")],
    )
    update = FakeUpdate(user_id=123, message=message)
    retry_update = FakeUpdate(user_id=123, text=BUTTON_RETRY_LAST_TURN)
    session = FakeSession()
    services, store = make_services(session=session, provider="codex", workspace_path=str(tmp_path))

    run(handle_attachment(update, None, services, ui_state))

    session.capabilities.supports_image_prompt = False

    async def switched_snapshot_runtime_state():
        return SimpleNamespace(
            provider="claude",
            workspace_id="default",
            workspace_path=str(tmp_path),
            session_store=store,
        )

    services.snapshot_runtime_state = switched_snapshot_runtime_state

    run(handle_text(retry_update, None, services, ui_state))

    assert len(session.prompt_items) == 2
    first_prompt_items = session.prompt_items[0]
    second_prompt_items = session.prompt_items[1]
    assert isinstance(first_prompt_items[1], PromptImage)
    assert isinstance(second_prompt_items[0], PromptText)
    assert second_prompt_items[0].text == "Describe this screenshot"
    assert isinstance(second_prompt_items[1], PromptText)
    assert "saved to `.talk2agent/telegram-inbox/" in second_prompt_items[1].text
    bundle = ui_state.get_context_bundle(123, "claude", "default")
    assert bundle is not None
    assert bundle.items
    assert retry_update.message.reply_calls == ["hello world"]
    assert store.record_session_usage_calls == [
        (123, "session-123", "Describe this screenshot"),
        (123, "session-123", "Describe this screenshot"),
    ]


def test_callback_switch_provider_retry_last_turn_recoerces_attachment_for_new_provider_capabilities(tmp_path):
    from talk2agent.acp.agent_session import PromptImage, PromptText
    from talk2agent.bots.telegram_bot import (
        BUTTON_SWITCH_AGENT,
        TelegramUiState,
        handle_attachment,
        handle_callback_query,
        handle_text,
    )

    ui_state = TelegramUiState()
    attachment_message = FakeIncomingMessage(
        caption="Describe this screenshot",
        photo=[FakePhotoSize(payload=b"photo-bytes", file_unique_id="photo-switch-retry")],
    )
    attachment_update = FakeUpdate(user_id=123, message=attachment_message)
    menu_update = FakeUpdate(user_id=123, text=BUTTON_SWITCH_AGENT)
    session = FakeSession()
    switch_message = FakeIncomingMessage("switch")
    services, store = make_services(session=session, provider="codex", admin_user_id=123, workspace_path=str(tmp_path))

    run(handle_attachment(attachment_update, None, services, ui_state))

    session.capabilities.supports_image_prompt = False

    run(handle_text(menu_update, None, services, ui_state))

    retry_button = find_inline_button(menu_update.message.reply_markups[0], "Retry on Claude Code")

    async def switched_snapshot_runtime_state():
        return SimpleNamespace(
            provider="claude",
            workspace_id="default",
            workspace_path=str(tmp_path),
            session_store=store,
        )

    services.snapshot_runtime_state = switched_snapshot_runtime_state

    callback_update = FakeCallbackUpdate(123, retry_button.callback_data, message=switch_message)
    run(handle_callback_query(callback_update, make_context(application=FakeApplication()), services, ui_state))

    assert services.switch_provider_calls == ["claude"]
    assert [text for text, _ in switch_message.edit_calls] == [
        "Switching to Claude Code...",
        (
            "Switched agent to Claude Code in Default Workspace. "
            "Old bot buttons and pending inputs were cleared.\n"
            "Retrying last turn on the new agent..."
        ),
    ]
    assert len(session.prompt_items) == 2
    first_prompt_items = session.prompt_items[0]
    second_prompt_items = session.prompt_items[1]
    assert isinstance(first_prompt_items[1], PromptImage)
    assert isinstance(second_prompt_items[0], PromptText)
    assert second_prompt_items[0].text == "Describe this screenshot"
    assert isinstance(second_prompt_items[1], PromptText)
    assert "saved to `.talk2agent/telegram-inbox/" in second_prompt_items[1].text
    bundle = ui_state.get_context_bundle(123, "claude", "default")
    assert bundle is not None
    assert bundle.items
    assert switch_message.reply_calls == ["hello world"]
    assert store.record_session_usage_calls == [
        (123, "session-123", "Describe this screenshot"),
        (123, "session-123", "Describe this screenshot"),
    ]


def test_handle_attachment_photo_uses_context_bundle_when_bundle_chat_active():
    from talk2agent.acp.agent_session import PromptImage, PromptText
    from talk2agent.bots.telegram_bot import TelegramUiState, _ContextBundleItem, handle_attachment

    ui_state = TelegramUiState()
    ui_state.add_context_item(
        123,
        "claude",
        "default",
        _ContextBundleItem(kind="file", relative_path="notes.txt"),
    )
    ui_state.add_context_item(
        123,
        "claude",
        "default",
        _ContextBundleItem(kind="change", relative_path="src/app.py", status_code=" M"),
    )
    assert ui_state.enable_context_bundle_chat(123, "claude", "default") is True

    message = FakeIncomingMessage(
        caption="Describe this screenshot",
        photo=[FakePhotoSize(payload=b"photo-bytes", file_unique_id="photo-ctx")],
    )
    update = FakeUpdate(user_id=123, message=message)
    services, store = make_services()

    run(handle_attachment(update, None, services, ui_state))

    prompt_items = services.final_session.prompt_items[0]
    assert isinstance(prompt_items[0], PromptText)
    assert "Please work with the following context bundle in the current workspace." in prompt_items[0].text
    assert "- notes.txt" in prompt_items[0].text
    assert "- src/app.py [ M]" in prompt_items[0].text
    assert "Also process the attached Telegram content in this same turn" in prompt_items[0].text
    assert isinstance(prompt_items[1], PromptText)
    assert prompt_items[1].text == "Describe this screenshot"
    assert isinstance(prompt_items[2], PromptImage)
    assert prompt_items[2].uri == "telegram://photo/photo-ctx"
    assert store.record_session_usage_calls == [
        (123, "session-123", "Describe this screenshot")
    ]


def test_handle_attachment_document_builds_text_resource_prompt_without_caption():
    from talk2agent.acp.agent_session import PromptText, PromptTextResource
    from talk2agent.bots.telegram_bot import TelegramUiState, handle_attachment

    ui_state = TelegramUiState()
    message = FakeIncomingMessage(
        document=FakeDocument(
            file_name="notes.md",
            mime_type="text/markdown",
            file_unique_id="doc-123",
            payload=b"# Notes\n- item\n",
        )
    )
    update = FakeUpdate(user_id=123, message=message)
    services, store = make_services()

    run(handle_attachment(update, None, services, ui_state))

    prompt_items = services.final_session.prompt_items[0]
    assert isinstance(prompt_items[0], PromptText)
    assert prompt_items[0].text == "Please inspect the attached Telegram document notes.md."
    assert isinstance(prompt_items[1], PromptTextResource)
    assert prompt_items[1].uri == "telegram://document/doc-123/notes.md"
    assert prompt_items[1].mime_type == "text/markdown"
    assert prompt_items[1].text == "# Notes\n- item\n"
    assert store.record_session_usage_calls == [
        (123, "session-123", "Telegram document: notes.md")
    ]


def test_handle_attachment_inlines_text_document_when_provider_lacks_embedded_context():
    from talk2agent.acp.agent_session import PromptText
    from talk2agent.bots.telegram_bot import TelegramUiState, handle_attachment

    session = FakeSession()
    session.capabilities.supports_embedded_context_prompt = False
    ui_state = TelegramUiState()
    message = FakeIncomingMessage(
        document=FakeDocument(
            file_name="notes.md",
            mime_type="text/markdown",
            file_unique_id="doc-inline",
            payload=b"# Notes\n- item\n",
        )
    )
    update = FakeUpdate(user_id=123, message=message)
    services, store = make_services(session=session)

    run(handle_attachment(update, None, services, ui_state))

    prompt_items = services.final_session.prompt_items[0]
    assert len(prompt_items) == 2
    assert isinstance(prompt_items[0], PromptText)
    assert prompt_items[0].text == "Please inspect the attached Telegram document notes.md."
    assert isinstance(prompt_items[1], PromptText)
    assert "inlined because the current provider does not support ACP embedded context" in prompt_items[1].text
    assert "URI: telegram://document/doc-inline/notes.md" in prompt_items[1].text
    assert "# Notes\n- item\n" in prompt_items[1].text
    assert store.record_session_usage_calls == [
        (123, "session-123", "Telegram document: notes.md")
    ]


def test_handle_attachment_voice_builds_audio_prompt_without_caption():
    from talk2agent.acp.agent_session import PromptAudio, PromptText
    from talk2agent.bots.telegram_bot import TelegramUiState, handle_attachment

    ui_state = TelegramUiState()
    message = FakeIncomingMessage(voice=FakeVoice(file_unique_id="voice-123", payload=b"voice-bytes"))
    update = FakeUpdate(user_id=123, message=message)
    services, store = make_services()

    run(handle_attachment(update, None, services, ui_state))

    prompt_items = services.final_session.prompt_items[0]
    assert isinstance(prompt_items[0], PromptText)
    assert prompt_items[0].text == "Please inspect or transcribe the attached Telegram voice note."
    assert isinstance(prompt_items[1], PromptAudio)
    assert prompt_items[1].mime_type == "audio/ogg"
    assert prompt_items[1].data == "dm9pY2UtYnl0ZXM="
    assert store.record_session_usage_calls == [
        (123, "session-123", "Telegram voice note")
    ]


def test_handle_attachment_audio_uses_caption_and_audio_block():
    from talk2agent.acp.agent_session import PromptAudio, PromptText
    from talk2agent.bots.telegram_bot import TelegramUiState, handle_attachment

    ui_state = TelegramUiState()
    message = FakeIncomingMessage(
        caption="Summarize this meeting audio",
        audio=FakeAudio(title="meeting", file_name="meeting.mp3", payload=b"audio-bytes"),
    )
    update = FakeUpdate(user_id=123, message=message)
    services, store = make_services()

    run(handle_attachment(update, None, services, ui_state))

    prompt_items = services.final_session.prompt_items[0]
    assert isinstance(prompt_items[0], PromptText)
    assert prompt_items[0].text == "Summarize this meeting audio"
    assert isinstance(prompt_items[1], PromptAudio)
    assert prompt_items[1].mime_type == "audio/mpeg"
    assert prompt_items[1].data == "YXVkaW8tYnl0ZXM="
    assert store.record_session_usage_calls == [
        (123, "session-123", "Summarize this meeting audio")
    ]


def test_handle_attachment_audio_document_uses_audio_block():
    from talk2agent.acp.agent_session import PromptAudio, PromptText
    from talk2agent.bots.telegram_bot import TelegramUiState, handle_attachment

    ui_state = TelegramUiState()
    message = FakeIncomingMessage(
        document=FakeDocument(
            file_name="note.ogg",
            mime_type="audio/ogg",
            payload=b"audio-doc-bytes",
        )
    )
    update = FakeUpdate(user_id=123, message=message)
    services, store = make_services()

    run(handle_attachment(update, None, services, ui_state))

    prompt_items = services.final_session.prompt_items[0]
    assert isinstance(prompt_items[0], PromptText)
    assert prompt_items[0].text == "Please inspect or transcribe the attached Telegram audio note.ogg."
    assert isinstance(prompt_items[1], PromptAudio)
    assert prompt_items[1].mime_type == "audio/ogg"
    assert prompt_items[1].data == "YXVkaW8tZG9jLWJ5dGVz"
    assert store.record_session_usage_calls == [
        (123, "session-123", "Telegram audio: note.ogg")
    ]


def test_handle_attachment_video_builds_blob_prompt_without_caption():
    from talk2agent.acp.agent_session import PromptBlobResource, PromptText
    from talk2agent.bots.telegram_bot import TelegramUiState, handle_attachment

    ui_state = TelegramUiState()
    message = FakeIncomingMessage(
        video=FakeVideo(
            file_name="walkthrough.mp4",
            file_unique_id="video-123",
            payload=b"video-bytes",
        )
    )
    update = FakeUpdate(user_id=123, message=message)
    services, store = make_services()

    run(handle_attachment(update, None, services, ui_state))

    prompt_items = services.final_session.prompt_items[0]
    assert isinstance(prompt_items[0], PromptText)
    assert prompt_items[0].text == "Please inspect the attached Telegram video walkthrough.mp4."
    assert isinstance(prompt_items[1], PromptBlobResource)
    assert prompt_items[1].mime_type == "video/mp4"
    assert prompt_items[1].blob == "dmlkZW8tYnl0ZXM="
    assert prompt_items[1].uri == "telegram://video/video-123/walkthrough.mp4"
    assert store.record_session_usage_calls == [
        (123, "session-123", "Telegram video: walkthrough.mp4")
    ]


def test_handle_attachment_media_group_supports_video_items():
    from talk2agent.acp.agent_session import PromptBlobResource, PromptImage, PromptText
    from talk2agent.bots.telegram_bot import TelegramUiState, handle_attachment

    async def scenario():
        ui_state = TelegramUiState(media_group_settle_seconds=0.01)
        services, store = make_services()
        first_message = FakeIncomingMessage(
            caption="Review this recording and screenshot",
            video=FakeVideo(
                file_name="recording.mp4",
                file_unique_id="video-mg-1",
                payload=b"video-one",
            ),
            media_group_id="group-video",
        )
        second_message = FakeIncomingMessage(
            photo=[FakePhotoSize(file_unique_id="photo-mg-video", payload=b"photo-two")],
            media_group_id="group-video",
        )

        await handle_attachment(FakeUpdate(user_id=123, message=first_message), None, services, ui_state)
        await handle_attachment(FakeUpdate(user_id=123, message=second_message), None, services, ui_state)
        await asyncio.sleep(0.03)
        return services, store

    services, store = asyncio.run(scenario())

    assert len(services.final_session.prompt_items) == 1
    prompt_items = services.final_session.prompt_items[0]
    assert isinstance(prompt_items[0], PromptText)
    assert prompt_items[0].text == "Review this recording and screenshot"
    assert isinstance(prompt_items[1], PromptBlobResource)
    assert prompt_items[1].mime_type == "video/mp4"
    assert prompt_items[1].uri == "telegram://video/video-mg-1/recording.mp4"
    assert isinstance(prompt_items[2], PromptImage)
    assert prompt_items[2].uri == "telegram://photo/photo-mg-video"
    assert store.record_session_usage_calls == [
        (123, "session-123", "Review this recording and screenshot")
    ]


def test_handle_attachment_media_group_batches_items_into_single_turn():
    from talk2agent.acp.agent_session import PromptImage, PromptText
    from talk2agent.bots.telegram_bot import TelegramUiState, handle_attachment

    async def scenario():
        ui_state = TelegramUiState(media_group_settle_seconds=0.01)
        services, store = make_services()
        first_message = FakeIncomingMessage(
            caption="Compare these screenshots",
            photo=[FakePhotoSize(file_unique_id="photo-1", payload=b"one")],
            media_group_id="group-1",
        )
        second_message = FakeIncomingMessage(
            photo=[FakePhotoSize(file_unique_id="photo-2", payload=b"two")],
            media_group_id="group-1",
        )

        await handle_attachment(
            FakeUpdate(user_id=123, message=first_message),
            make_context(application=FakeApplication()),
            services,
            ui_state,
        )
        await handle_attachment(
            FakeUpdate(user_id=123, message=second_message),
            make_context(application=FakeApplication()),
            services,
            ui_state,
        )
        await asyncio.sleep(0.03)
        return services, store

    services, store = asyncio.run(scenario())

    assert len(services.final_session.prompt_items) == 1
    prompt_items = services.final_session.prompt_items[0]
    assert isinstance(prompt_items[0], PromptText)
    assert prompt_items[0].text == "Compare these screenshots"
    assert isinstance(prompt_items[1], PromptImage)
    assert prompt_items[1].data == "b25l"
    assert isinstance(prompt_items[2], PromptImage)
    assert prompt_items[2].data == "dHdv"
    assert store.record_session_usage_calls == [
        (123, "session-123", "Compare these screenshots")
    ]


def test_handle_attachment_media_group_uses_context_bundle_when_bundle_chat_active():
    from talk2agent.acp.agent_session import PromptImage, PromptText
    from talk2agent.bots.telegram_bot import TelegramUiState, _ContextBundleItem, handle_attachment

    async def scenario():
        ui_state = TelegramUiState(media_group_settle_seconds=0.01)
        ui_state.add_context_item(
            123,
            "claude",
            "default",
            _ContextBundleItem(kind="file", relative_path="notes.txt"),
        )
        ui_state.enable_context_bundle_chat(123, "claude", "default")
        services, store = make_services()
        first_message = FakeIncomingMessage(
            caption="Compare these screenshots",
            photo=[FakePhotoSize(file_unique_id="photo-mg-1", payload=b"one")],
            media_group_id="group-bundle",
        )
        second_message = FakeIncomingMessage(
            photo=[FakePhotoSize(file_unique_id="photo-mg-2", payload=b"two")],
            media_group_id="group-bundle",
        )

        await handle_attachment(FakeUpdate(user_id=123, message=first_message), None, services, ui_state)
        await handle_attachment(FakeUpdate(user_id=123, message=second_message), None, services, ui_state)
        await asyncio.sleep(0.03)
        return services, store

    services, store = asyncio.run(scenario())

    prompt_items = services.final_session.prompt_items[0]
    assert isinstance(prompt_items[0], PromptText)
    assert "Please work with the following context bundle in the current workspace." in prompt_items[0].text
    assert "- notes.txt" in prompt_items[0].text
    assert isinstance(prompt_items[1], PromptText)
    assert prompt_items[1].text == "Compare these screenshots"
    assert isinstance(prompt_items[2], PromptImage)
    assert prompt_items[2].data == "b25l"
    assert isinstance(prompt_items[3], PromptImage)
    assert prompt_items[3].data == "dHdv"
    assert store.record_session_usage_calls == [
        (123, "session-123", "Compare these screenshots")
    ]


def test_handle_attachment_media_group_respects_pending_plain_text_action():
    from talk2agent.bots.telegram_bot import TelegramUiState, handle_attachment

    async def scenario():
        ui_state = TelegramUiState(media_group_settle_seconds=0.01)
        ui_state.set_pending_text_action(123, "rename_history", session_id="session-1", page=0)
        services, _ = make_services()
        first_message = FakeIncomingMessage(
            photo=[FakePhotoSize(payload=b"one")],
            media_group_id="group-2",
        )
        second_message = FakeIncomingMessage(
            photo=[FakePhotoSize(payload=b"two")],
            media_group_id="group-2",
        )

        await handle_attachment(FakeUpdate(user_id=123, message=first_message), None, services, ui_state)
        await handle_attachment(FakeUpdate(user_id=123, message=second_message), None, services, ui_state)
        await asyncio.sleep(0.03)
        return services, first_message, second_message

    services, first_message, second_message = asyncio.run(scenario())

    assert services.final_session.prompt_items == []
    assert first_message.reply_calls == [
        "The current action is waiting for plain text. Send text or cancel the pending action first."
    ]
    assert second_message.reply_calls == []


def test_handle_attachment_saves_unsupported_image_into_workspace_inbox(tmp_path):
    from talk2agent.acp.agent_session import PromptText
    from talk2agent.bots.telegram_bot import (
        TelegramUiState,
        _ContextBundleItem,
        handle_attachment,
    )

    session = FakeSession()
    session.capabilities.supports_image_prompt = False
    ui_state = TelegramUiState()
    message = FakeIncomingMessage(photo=[FakePhotoSize(file_unique_id="photo-unsupported", payload=b"img")])
    update = FakeUpdate(user_id=123, message=message)
    services, store = make_services(session=session, provider="codex", workspace_path=str(tmp_path))

    run(handle_attachment(update, None, services, ui_state))

    prompt_items = services.final_session.prompt_items[0]
    assert isinstance(prompt_items[0], PromptText)
    assert prompt_items[0].text == "Please inspect the attached Telegram image."
    assert isinstance(prompt_items[1], PromptText)
    assert "saved to `.talk2agent/telegram-inbox/" in prompt_items[1].text
    assert "does not support image attachments via ACP prompts" in prompt_items[1].text
    saved_files = list((tmp_path / ".talk2agent" / "telegram-inbox").iterdir())
    assert len(saved_files) == 1
    assert saved_files[0].read_bytes() == b"img"
    bundle = ui_state.get_context_bundle(123, "codex", "default")
    assert bundle is not None
    assert bundle.items == [
        _ContextBundleItem(
            kind="file",
            relative_path=saved_files[0].relative_to(tmp_path).as_posix(),
        )
    ]
    assert store.invalidate_calls == []
    assert session.close_calls == 0


def test_handle_attachment_saves_unsupported_binary_document_into_workspace_inbox(tmp_path):
    from talk2agent.acp.agent_session import PromptText
    from talk2agent.bots.telegram_bot import (
        TelegramUiState,
        _ContextBundleItem,
        handle_attachment,
    )

    session = FakeSession()
    session.capabilities.supports_embedded_context_prompt = False
    ui_state = TelegramUiState()
    message = FakeIncomingMessage(
        document=FakeDocument(
            file_name="report.pdf",
            mime_type="application/pdf",
            payload=b"%PDF-binary",
        )
    )
    update = FakeUpdate(user_id=123, message=message)
    services, store = make_services(session=session, workspace_path=str(tmp_path))

    run(handle_attachment(update, None, services, ui_state))

    prompt_items = services.final_session.prompt_items[0]
    assert isinstance(prompt_items[0], PromptText)
    assert prompt_items[0].text == "Please inspect the attached Telegram document report.pdf."
    assert isinstance(prompt_items[1], PromptText)
    assert "saved to `.talk2agent/telegram-inbox/" in prompt_items[1].text
    assert "does not support document attachments via ACP prompts" in prompt_items[1].text
    saved_files = list((tmp_path / ".talk2agent" / "telegram-inbox").iterdir())
    assert len(saved_files) == 1
    assert saved_files[0].suffix == ".pdf"
    assert saved_files[0].read_bytes() == b"%PDF-binary"
    bundle = ui_state.get_context_bundle(123, "claude", "default")
    assert bundle is not None
    assert bundle.items == [
        _ContextBundleItem(
            kind="file",
            relative_path=saved_files[0].relative_to(tmp_path).as_posix(),
        )
    ]
    assert store.record_session_usage_calls == [
        (123, "session-123", "Telegram document: report.pdf")
    ]


def test_handle_attachment_saves_unsupported_video_into_workspace_inbox(tmp_path):
    from talk2agent.acp.agent_session import PromptText
    from talk2agent.bots.telegram_bot import (
        TelegramUiState,
        _ContextBundleItem,
        handle_attachment,
    )

    session = FakeSession()
    session.capabilities.supports_embedded_context_prompt = False
    ui_state = TelegramUiState()
    message = FakeIncomingMessage(
        video=FakeVideo(
            file_name="walkthrough.mp4",
            file_unique_id="video-unsupported",
            payload=b"video-bytes",
        )
    )
    update = FakeUpdate(user_id=123, message=message)
    services, store = make_services(session=session, provider="codex", workspace_path=str(tmp_path))

    run(handle_attachment(update, None, services, ui_state))

    prompt_items = services.final_session.prompt_items[0]
    assert isinstance(prompt_items[0], PromptText)
    assert prompt_items[0].text == "Please inspect the attached Telegram video walkthrough.mp4."
    assert isinstance(prompt_items[1], PromptText)
    assert "saved to `.talk2agent/telegram-inbox/" in prompt_items[1].text
    assert "does not support video attachments via ACP prompts" in prompt_items[1].text
    saved_files = list((tmp_path / ".talk2agent" / "telegram-inbox").iterdir())
    assert len(saved_files) == 1
    assert saved_files[0].suffix == ".mp4"
    assert saved_files[0].read_bytes() == b"video-bytes"
    bundle = ui_state.get_context_bundle(123, "codex", "default")
    assert bundle is not None
    assert bundle.items == [
        _ContextBundleItem(
            kind="file",
            relative_path=saved_files[0].relative_to(tmp_path).as_posix(),
        )
    ]
    assert store.record_session_usage_calls == [
        (123, "session-123", "Telegram video: walkthrough.mp4")
    ]


def test_handle_attachment_media_group_adds_saved_fallback_files_to_context_bundle(tmp_path):
    from talk2agent.bots.telegram_bot import TelegramUiState, handle_attachment

    async def scenario():
        session = FakeSession()
        session.capabilities.supports_image_prompt = False
        ui_state = TelegramUiState(media_group_settle_seconds=0.01)
        services, _ = make_services(session=session, provider="codex", workspace_path=str(tmp_path))
        first_message = FakeIncomingMessage(
            caption="Compare these screenshots",
            photo=[FakePhotoSize(file_unique_id="photo-fallback-1", payload=b"one")],
            media_group_id="group-fallback",
        )
        second_message = FakeIncomingMessage(
            photo=[FakePhotoSize(file_unique_id="photo-fallback-2", payload=b"two")],
            media_group_id="group-fallback",
        )

        await handle_attachment(FakeUpdate(user_id=123, message=first_message), None, services, ui_state)
        await handle_attachment(FakeUpdate(user_id=123, message=second_message), None, services, ui_state)
        await asyncio.sleep(0.03)
        return ui_state

    ui_state = asyncio.run(scenario())

    saved_files = list((tmp_path / ".talk2agent" / "telegram-inbox").iterdir())
    assert len(saved_files) == 2
    bundle = ui_state.get_context_bundle(123, "codex", "default")
    assert bundle is not None
    assert [item.kind for item in bundle.items] == ["file", "file"]
    assert {item.relative_path for item in bundle.items} == {
        saved_file.relative_to(tmp_path).as_posix() for saved_file in saved_files
    }


def test_handle_attachment_failed_fallback_turn_does_not_add_context_bundle_item(tmp_path):
    from talk2agent.bots.telegram_bot import TelegramUiState, handle_attachment

    session = FakeSession(error=RuntimeError("boom"), raise_before_stream=True)
    session.capabilities.supports_image_prompt = False
    ui_state = TelegramUiState()
    message = FakeIncomingMessage(photo=[FakePhotoSize(file_unique_id="photo-error", payload=b"img")])
    update = FakeUpdate(user_id=123, message=message)
    services, store = make_services(session=session, provider="codex", workspace_path=str(tmp_path))

    run(handle_attachment(update, None, services, ui_state))

    saved_files = list((tmp_path / ".talk2agent" / "telegram-inbox").iterdir())
    assert len(saved_files) == 1
    assert ui_state.get_context_bundle(123, "codex", "default") is None
    assert store.invalidate_calls == [(123, session)]


def test_handle_attachment_respects_pending_plain_text_action():
    from talk2agent.bots.telegram_bot import TelegramUiState, handle_attachment

    ui_state = TelegramUiState()
    ui_state.set_pending_text_action(123, "rename_history", session_id="session-1", page=0)
    message = FakeIncomingMessage(photo=[FakePhotoSize()])
    update = FakeUpdate(user_id=123, message=message)
    services, _ = make_services()

    run(handle_attachment(update, None, services, ui_state))

    assert services.final_session.prompt_items == []
    assert message.reply_calls == [
        "The current action is waiting for plain text. Send text or cancel the pending action first."
    ]
