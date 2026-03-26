import asyncio
import time
from types import SimpleNamespace

from acp import text_block
from acp.schema import (
    AgentMessageChunk,
    AgentPlanUpdate,
    AvailableCommand,
    AvailableCommandsUpdate,
    Cost,
    PlanEntry,
    SessionInfoUpdate,
    ToolCallLocation,
    ToolCallProgress,
    ToolCallStart,
    UnstructuredCommandInput,
    UsageUpdate,
)
import pytest


def make_config_option(option_id, category, current_value, values):
    return SimpleNamespace(
        root=SimpleNamespace(
            id=option_id,
            category=category,
            current_value=current_value,
            options=[
                SimpleNamespace(value=value, name=label, description=None)
                for value, label in values
            ],
        )
    )


def make_new_session_response(session_id):
    return SimpleNamespace(
        session_id=session_id,
        config_options=[
            make_config_option(
                "model",
                "model",
                "gpt-5.4",
                [("gpt-5.4", "GPT-5.4"), ("gpt-5.4-mini", "GPT-5.4 Mini")],
            ),
            make_config_option(
                "mode",
                "mode",
                "xhigh",
                [("xhigh", "xhigh"), ("low", "low")],
            ),
        ],
        models=None,
        modes=None,
    )


class FakeConnection:
    def __init__(self, client, session_ids, prompt_hooks=None):
        self._client = client
        self._session_ids = list(session_ids)
        self._prompt_hooks = [] if prompt_hooks is None else list(prompt_hooks)
        self.initialize_calls = []
        self.initialize_client_capabilities = []
        self.new_session_calls = []
        self.fork_session_calls = []
        self.resume_session_calls = []
        self.load_session_calls = []
        self.list_sessions_calls = []
        self.cancel_calls = []
        self.set_config_option_calls = []
        self.set_session_model_calls = []
        self.prompt_calls = []
        self.prompt_response = object()

    async def initialize(self, protocol_version, client_capabilities=None, client_info=None):
        self.initialize_calls.append(protocol_version)
        self.initialize_client_capabilities.append(client_capabilities)
        return SimpleNamespace(
            agent_capabilities=SimpleNamespace(
                load_session=True,
                prompt_capabilities=SimpleNamespace(
                    image=True,
                    audio=True,
                    embedded_context=True,
                ),
                session_capabilities=SimpleNamespace(fork={}, list={}, resume={}),
            )
        )

    async def new_session(self, cwd, mcp_servers):
        self.new_session_calls.append((cwd, mcp_servers))
        return make_new_session_response(self._session_ids.pop(0))

    async def resume_session(self, cwd, session_id, mcp_servers):
        self.resume_session_calls.append((cwd, session_id, mcp_servers))
        return make_new_session_response(session_id)

    async def fork_session(self, cwd, session_id, mcp_servers):
        self.fork_session_calls.append((cwd, session_id, mcp_servers))
        return make_new_session_response(f"fork-{session_id}")

    async def load_session(self, cwd, session_id, mcp_servers):
        self.load_session_calls.append((cwd, session_id, mcp_servers))
        return make_new_session_response(session_id)

    async def list_sessions(self, cursor, cwd):
        self.list_sessions_calls.append((cursor, cwd))
        return SimpleNamespace(sessions=[], next_cursor=None)

    async def cancel(self, session_id):
        self.cancel_calls.append(session_id)

    async def set_config_option(self, config_id, session_id, value):
        self.set_config_option_calls.append((config_id, session_id, value))
        response = make_new_session_response(session_id)
        for option in response.config_options:
            if option.root.id == config_id:
                option.root.current_value = value
        return SimpleNamespace(config_options=response.config_options)

    async def prompt(self, prompt, session_id):
        self.prompt_calls.append((prompt, session_id))
        if self._prompt_hooks:
            await self._prompt_hooks.pop(0)()
        await self._client.session_update(
            session_id,
            AgentMessageChunk(
                sessionUpdate="agent_message_chunk",
                content=text_block("hello from agent"),
            ),
        )
        return self.prompt_response


class LegacyConnection(FakeConnection):
    async def new_session(self, cwd, mcp_servers):
        self.new_session_calls.append((cwd, mcp_servers))
        return SimpleNamespace(
            session_id=self._session_ids.pop(0),
            config_options=None,
            models=SimpleNamespace(
                available_models=[
                    SimpleNamespace(model_id="gpt-5.4", name="GPT-5.4", description=None),
                    SimpleNamespace(
                        model_id="gpt-5.4-mini",
                        name="GPT-5.4 Mini",
                        description=None,
                    ),
                ],
                current_model_id="gpt-5.4",
            ),
            modes=None,
        )

    async def set_session_model(self, model_id, session_id):
        self.set_session_model_calls.append((model_id, session_id))
        return SimpleNamespace()


class NonListingConnection(FakeConnection):
    async def initialize(self, protocol_version, client_capabilities=None, client_info=None):
        self.initialize_calls.append(protocol_version)
        self.initialize_client_capabilities.append(client_capabilities)
        return SimpleNamespace(
            agent_capabilities=SimpleNamespace(
                load_session=True,
                prompt_capabilities=SimpleNamespace(
                    image=True,
                    audio=True,
                    embedded_context=True,
                ),
                session_capabilities=SimpleNamespace(fork={}, list=None, resume={}),
            )
        )


class NonForkingConnection(FakeConnection):
    async def initialize(self, protocol_version, client_capabilities=None, client_info=None):
        self.initialize_calls.append(protocol_version)
        self.initialize_client_capabilities.append(client_capabilities)
        return SimpleNamespace(
            agent_capabilities=SimpleNamespace(
                load_session=True,
                prompt_capabilities=SimpleNamespace(
                    image=True,
                    audio=True,
                    embedded_context=True,
                ),
                session_capabilities=SimpleNamespace(fork=None, list={}, resume={}),
            )
        )


class TextOnlyConnection(FakeConnection):
    async def initialize(self, protocol_version, client_capabilities=None, client_info=None):
        self.initialize_calls.append(protocol_version)
        self.initialize_client_capabilities.append(client_capabilities)
        return SimpleNamespace(
            agent_capabilities=SimpleNamespace(
                load_session=True,
                prompt_capabilities=SimpleNamespace(
                    image=False,
                    audio=False,
                    embedded_context=False,
                ),
                session_capabilities=SimpleNamespace(fork={}, list={}, resume={}),
            )
        )


class FakeSpawnContext:
    def __init__(self, connection):
        self.connection = connection
        self.enter_count = 0
        self.exit_count = 0

    async def __aenter__(self):
        self.enter_count += 1
        return self.connection, object()

    async def __aexit__(self, exc_type, exc, tb):
        self.exit_count += 1
        return False


class RecordingSink:
    def __init__(self):
        self.updates = []

    async def on_update(self, update):
        self.updates.append(update.content.text)


def test_last_used_at_starts_as_monotonic_timestamp():
    from talk2agent.acp.agent_session import AgentSession

    before = time.monotonic()
    session = AgentSession(
        command="claude-agent-acp",
        args=[],
        cwd="F:/workspace",
        spawn_agent_process=lambda *args, **kwargs: None,
    )
    after = time.monotonic()

    assert isinstance(session.last_used_at, float)
    assert before <= session.last_used_at <= after


def test_ensure_started_initializes_once():
    from talk2agent.acp.agent_session import AgentSession

    spawn_calls = []
    contexts = []

    def fake_spawn_agent_process(to_client, command, *args, cwd, env):
        spawn_calls.append((command, args, cwd, env))
        client = to_client(object())
        context = FakeSpawnContext(FakeConnection(client=client, session_ids=["session-1"]))
        contexts.append(context)
        return context

    session = AgentSession(
        command="claude-agent-acp",
        args=["--verbose"],
        cwd="F:/workspace",
        spawn_agent_process=fake_spawn_agent_process,
    )

    async def scenario():
        await session.ensure_started()
        await session.ensure_started()

    asyncio.run(scenario())

    assert len(spawn_calls) == 1
    assert session.session_id == "session-1"
    assert session.capabilities.can_resume is True
    assert session.capabilities.can_fork is True
    assert session.capabilities.supports_image_prompt is True
    assert session.capabilities.supports_audio_prompt is True
    assert session.capabilities.supports_embedded_context_prompt is True
    assert session.get_selection("model").current_value == "gpt-5.4"
    assert "PATH" in spawn_calls[0][3]
    assert contexts[0].connection.new_session_calls == [("F:/workspace", [])]


def test_ensure_started_advertises_client_filesystem_capabilities():
    from talk2agent.acp.agent_session import AgentSession

    context = None

    def fake_spawn_agent_process(to_client, command, *args, cwd, env):
        nonlocal context
        client = to_client(object())
        context = FakeSpawnContext(FakeConnection(client=client, session_ids=["session-1"]))
        return context

    session = AgentSession(
        command="claude-agent-acp",
        args=[],
        cwd="F:/workspace",
        spawn_agent_process=fake_spawn_agent_process,
    )

    asyncio.run(session.ensure_started())

    capabilities = context.connection.initialize_client_capabilities[0]
    assert capabilities.fs.read_text_file is True
    assert capabilities.fs.write_text_file is True
    assert capabilities.terminal is True


def test_run_turn_routes_updates_to_active_sink_only():
    from talk2agent.acp.agent_session import AgentSession

    contexts = []

    def fake_spawn_agent_process(to_client, command, *args, cwd, env):
        client = to_client(object())
        context = FakeSpawnContext(FakeConnection(client=client, session_ids=["session-1"]))
        contexts.append(context)
        return context

    session = AgentSession(
        command="claude-agent-acp",
        args=[],
        cwd="F:/workspace",
        spawn_agent_process=fake_spawn_agent_process,
    )
    sink = RecordingSink()
    before = time.monotonic()

    async def scenario():
        response = await session.run_turn("hello", sink)
        await session._handle_update(
            "session-1",
            AgentMessageChunk(
                sessionUpdate="agent_message_chunk",
                content=text_block("ignored after turn"),
            ),
        )
        return response

    response = asyncio.run(scenario())
    after = time.monotonic()

    assert response is contexts[0].connection.prompt_response
    assert sink.updates == ["hello from agent"]
    assert session._active_sink is None
    assert contexts[0].connection.prompt_calls == [([text_block("hello")], "session-1")]
    assert before <= session.last_used_at <= after


def test_run_prompt_routes_structured_content_to_connection():
    from talk2agent.acp.agent_session import (
        AgentSession,
        PromptAudio,
        PromptBlobResource,
        PromptImage,
        PromptText,
        PromptTextResource,
    )

    contexts = []

    def fake_spawn_agent_process(to_client, command, *args, cwd, env):
        client = to_client(object())
        context = FakeSpawnContext(FakeConnection(client=client, session_ids=["session-1"]))
        contexts.append(context)
        return context

    session = AgentSession(
        command="claude-agent-acp",
        args=[],
        cwd="F:/workspace",
        spawn_agent_process=fake_spawn_agent_process,
    )
    sink = RecordingSink()

    async def scenario():
        return await session.run_prompt(
            [
                PromptText("describe these attachments"),
                PromptImage(
                    data="aW1hZ2U=",
                    mime_type="image/png",
                    uri="telegram://photo/photo-1",
                ),
                PromptAudio(
                    data="b2dn",
                    mime_type="audio/ogg",
                ),
                PromptTextResource(
                    uri="telegram://document/notes.md",
                    text="# Notes",
                    mime_type="text/markdown",
                ),
                PromptBlobResource(
                    uri="telegram://document/report.pdf",
                    blob="cGRm",
                    mime_type="application/pdf",
                ),
            ],
            sink,
        )

    response = asyncio.run(scenario())

    assert response is contexts[0].connection.prompt_response
    sent_prompt, session_id = contexts[0].connection.prompt_calls[0]
    assert session_id == "session-1"
    assert [block.type for block in sent_prompt] == ["text", "image", "audio", "resource", "resource"]
    assert sent_prompt[0].text == "describe these attachments"
    assert sent_prompt[1].mime_type == "image/png"
    assert sent_prompt[1].uri == "telegram://photo/photo-1"
    assert sent_prompt[2].mime_type == "audio/ogg"
    assert sent_prompt[2].data == "b2dn"
    assert sent_prompt[3].resource.text == "# Notes"
    assert sent_prompt[3].resource.mime_type == "text/markdown"
    assert sent_prompt[4].resource.blob == "cGRm"
    assert sent_prompt[4].resource.mime_type == "application/pdf"


def test_run_prompt_rejects_empty_prompt_items():
    from talk2agent.acp.agent_session import AgentSession

    session = AgentSession(
        command="claude-agent-acp",
        args=[],
        cwd="F:/workspace",
        spawn_agent_process=lambda *args, **kwargs: None,
    )

    async def scenario():
        sink = RecordingSink()
        with pytest.raises(ValueError, match="prompt_items"):
            await session.run_prompt([], sink)

    asyncio.run(scenario())


def test_run_prompt_rejects_unsupported_multimodal_items_before_prompt_call():
    from talk2agent.acp.agent_session import (
        AgentSession,
        PromptAudio,
        PromptImage,
        PromptText,
        PromptTextResource,
        UnsupportedPromptContentError,
    )

    contexts = []

    def fake_spawn_agent_process(to_client, command, *args, cwd, env):
        client = to_client(object())
        context = FakeSpawnContext(TextOnlyConnection(client=client, session_ids=["session-1"]))
        contexts.append(context)
        return context

    session = AgentSession(
        command="claude-agent-acp",
        args=[],
        cwd="F:/workspace",
        spawn_agent_process=fake_spawn_agent_process,
    )

    async def scenario():
        sink = RecordingSink()
        with pytest.raises(UnsupportedPromptContentError) as exc_info:
            await session.run_prompt(
                [
                    PromptText("hello"),
                    PromptImage(data="aW1hZ2U=", mime_type="image/png"),
                    PromptAudio(data="YXVkaW8=", mime_type="audio/mpeg"),
                    PromptTextResource(
                        uri="telegram://document/notes.md",
                        text="# Notes",
                        mime_type="text/markdown",
                    ),
                ],
                sink,
            )
        return exc_info.value

    error = asyncio.run(scenario())

    assert error.unsupported_content_types == ("image", "audio", "embedded_context")
    assert session.capabilities.supports_image_prompt is False
    assert session.capabilities.supports_audio_prompt is False
    assert session.capabilities.supports_embedded_context_prompt is False
    assert contexts[0].connection.prompt_calls == []


def test_reset_closes_old_connection_and_starts_new_session():
    from talk2agent.acp.agent_session import AgentSession

    contexts = []
    pending_session_ids = [["session-1"], ["session-2"]]

    def fake_spawn_agent_process(to_client, command, *args, cwd, env):
        client = to_client(object())
        context = FakeSpawnContext(
            FakeConnection(client=client, session_ids=pending_session_ids.pop(0))
        )
        contexts.append(context)
        return context

    session = AgentSession(
        command="claude-agent-acp",
        args=[],
        cwd="F:/workspace",
        spawn_agent_process=fake_spawn_agent_process,
    )

    async def scenario():
        await session.ensure_started()
        first_session_id = session.session_id
        await session.reset()
        return first_session_id, session.session_id

    first_session_id, second_session_id = asyncio.run(scenario())

    assert first_session_id == "session-1"
    assert second_session_id == "session-2"
    assert contexts[0].exit_count == 1
    assert contexts[1].enter_count == 1


def test_close_waits_for_in_flight_and_queued_turns():
    from talk2agent.acp.agent_session import AgentSession

    contexts = []
    first_prompt_started = asyncio.Event()
    release_first_prompt = asyncio.Event()

    async def block_first_prompt():
        first_prompt_started.set()
        await release_first_prompt.wait()

    def fake_spawn_agent_process(to_client, command, *args, cwd, env):
        client = to_client(object())
        context = FakeSpawnContext(
            FakeConnection(
                client=client,
                session_ids=["session-1"],
                prompt_hooks=[block_first_prompt],
            )
        )
        contexts.append(context)
        return context

    session = AgentSession(
        command="claude-agent-acp",
        args=[],
        cwd="F:/workspace",
        spawn_agent_process=fake_spawn_agent_process,
    )
    first_sink = RecordingSink()
    second_sink = RecordingSink()

    async def scenario():
        first_turn = asyncio.create_task(session.run_turn("first", first_sink))
        await first_prompt_started.wait()

        second_turn = asyncio.create_task(session.run_turn("second", second_sink))
        await asyncio.sleep(0)

        close_task = asyncio.create_task(session.close())
        await asyncio.sleep(0)

        assert not close_task.done()
        assert session.session_id == "session-1"

        release_first_prompt.set()

        first_response = await first_turn
        second_response = await second_turn
        await close_task

        return first_response, second_response

    first_response, second_response = asyncio.run(scenario())

    assert first_response is contexts[0].connection.prompt_response
    assert second_response is contexts[0].connection.prompt_response
    assert first_sink.updates == ["hello from agent"]
    assert second_sink.updates == ["hello from agent"]
    assert contexts[0].connection.prompt_calls == [
        ([text_block("first")], "session-1"),
        ([text_block("second")], "session-1"),
    ]
    assert contexts[0].exit_count == 1
    assert session.session_id is None


def test_load_session_prefers_resume_when_available():
    from talk2agent.acp.agent_session import AgentSession

    context = None

    def fake_spawn_agent_process(to_client, command, *args, cwd, env):
        nonlocal context
        client = to_client(object())
        context = FakeSpawnContext(FakeConnection(client=client, session_ids=["session-1"]))
        return context

    session = AgentSession(
        command="codex-acp",
        args=[],
        cwd="F:/workspace",
        spawn_agent_process=fake_spawn_agent_process,
    )

    asyncio.run(session.load_session("historic-session"))

    assert context.connection.resume_session_calls == [("F:/workspace", "historic-session", [])]
    assert context.connection.load_session_calls == []
    assert session.session_id == "historic-session"


def test_fork_session_creates_new_forked_session():
    from talk2agent.acp.agent_session import AgentSession

    context = None

    def fake_spawn_agent_process(to_client, command, *args, cwd, env):
        nonlocal context
        client = to_client(object())
        context = FakeSpawnContext(FakeConnection(client=client, session_ids=["session-1"]))
        return context

    session = AgentSession(
        command="codex-acp",
        args=[],
        cwd="F:/workspace",
        spawn_agent_process=fake_spawn_agent_process,
    )

    asyncio.run(session.fork_session("historic-session"))

    assert context.connection.fork_session_calls == [("F:/workspace", "historic-session", [])]
    assert session.session_id == "fork-historic-session"


def test_fork_session_raises_when_provider_does_not_support_forking():
    from talk2agent.acp.agent_session import AgentSession, SessionForkingNotSupportedError

    def fake_spawn_agent_process(to_client, command, *args, cwd, env):
        client = to_client(object())
        return FakeSpawnContext(NonForkingConnection(client=client, session_ids=["session-1"]))

    session = AgentSession(
        command="codex-acp",
        args=[],
        cwd="F:/workspace",
        spawn_agent_process=fake_spawn_agent_process,
    )

    async def scenario():
        with pytest.raises(SessionForkingNotSupportedError):
            await session.fork_session("historic-session")

    asyncio.run(scenario())


def test_cancel_turn_notifies_agent_for_current_session():
    from talk2agent.acp.agent_session import AgentSession

    context = None

    def fake_spawn_agent_process(to_client, command, *args, cwd, env):
        nonlocal context
        client = to_client(object())
        context = FakeSpawnContext(FakeConnection(client=client, session_ids=["session-1"]))
        return context

    session = AgentSession(
        command="codex-acp",
        args=[],
        cwd="F:/workspace",
        spawn_agent_process=fake_spawn_agent_process,
    )

    async def scenario():
        await session.ensure_started()
        return await session.cancel_turn()

    cancelled = asyncio.run(scenario())

    assert cancelled is True
    assert context.connection.cancel_calls == ["session-1"]


def test_cancel_turn_returns_false_without_active_session():
    from talk2agent.acp.agent_session import AgentSession

    session = AgentSession(
        command="codex-acp",
        args=[],
        cwd="F:/workspace",
        spawn_agent_process=lambda *args, **kwargs: None,
    )

    assert asyncio.run(session.cancel_turn()) is False


def test_list_sessions_raises_when_provider_does_not_support_listing():
    from talk2agent.acp.agent_session import AgentSession, SessionListingNotSupportedError

    def fake_spawn_agent_process(to_client, command, *args, cwd, env):
        client = to_client(object())
        return FakeSpawnContext(NonListingConnection(client=client, session_ids=["session-1"]))

    session = AgentSession(
        command="codex-acp",
        args=[],
        cwd="F:/workspace",
        spawn_agent_process=fake_spawn_agent_process,
    )

    async def scenario():
        with pytest.raises(SessionListingNotSupportedError):
            await session.list_sessions()

    asyncio.run(scenario())


def test_set_selection_uses_config_option_when_available():
    from talk2agent.acp.agent_session import AgentSession

    context = None

    def fake_spawn_agent_process(to_client, command, *args, cwd, env):
        nonlocal context
        client = to_client(object())
        context = FakeSpawnContext(FakeConnection(client=client, session_ids=["session-1"]))
        return context

    session = AgentSession(
        command="codex-acp",
        args=[],
        cwd="F:/workspace",
        spawn_agent_process=fake_spawn_agent_process,
    )

    async def scenario():
        await session.ensure_started()
        return await session.set_selection("mode", "low")

    selection = asyncio.run(scenario())

    assert context.connection.set_config_option_calls == [("mode", "session-1", "low")]
    assert selection.current_value == "low"


def test_set_selection_falls_back_to_legacy_model_api():
    from talk2agent.acp.agent_session import AgentSession

    context = None

    def fake_spawn_agent_process(to_client, command, *args, cwd, env):
        nonlocal context
        client = to_client(object())
        context = FakeSpawnContext(LegacyConnection(client=client, session_ids=["session-1"]))
        return context

    session = AgentSession(
        command="codex-acp",
        args=[],
        cwd="F:/workspace",
        spawn_agent_process=fake_spawn_agent_process,
    )

    async def scenario():
        await session.ensure_started()
        return await session.set_selection("model", "gpt-5.4-mini")

    selection = asyncio.run(scenario())

    assert context.connection.set_session_model_calls == [("gpt-5.4-mini", "session-1")]
    assert selection.current_value == "gpt-5.4-mini"


def test_agent_session_passes_selected_environment_to_provider(monkeypatch):
    from talk2agent.acp.agent_session import AgentSession

    captured_env = {}

    def fake_spawn_agent_process(to_client, command, *args, cwd, env):
        captured_env.update(env)
        client = to_client(object())
        return FakeSpawnContext(FakeConnection(client=client, session_ids=["session-1"]))

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("CODEX_HOME", "F:/codex-home")

    session = AgentSession(
        command="codex-acp",
        args=[],
        cwd="F:/workspace",
        spawn_agent_process=fake_spawn_agent_process,
    )

    asyncio.run(session.ensure_started())

    assert captured_env["OPENAI_API_KEY"] == "sk-test"
    assert captured_env["CODEX_HOME"] == "F:/codex-home"


def test_available_commands_update_is_cached():
    from talk2agent.acp.agent_session import AgentSession

    def fake_spawn_agent_process(to_client, command, *args, cwd, env):
        client = to_client(object())
        return FakeSpawnContext(FakeConnection(client=client, session_ids=["session-1"]))

    session = AgentSession(
        command="codex-acp",
        args=[],
        cwd="F:/workspace",
        spawn_agent_process=fake_spawn_agent_process,
    )

    async def scenario():
        await session.ensure_started()
        await session._handle_update(
            "session-1",
            AvailableCommandsUpdate(
                sessionUpdate="available_commands_update",
                availableCommands=[
                    AvailableCommand(
                        name="model",
                        description="Switch model",
                        input=UnstructuredCommandInput(hint="model id"),
                    )
                ],
            ),
        )
        return await session.wait_for_available_commands(0.01)

    commands = asyncio.run(scenario())

    assert len(commands) == 1
    assert commands[0].name == "model"
    assert commands[0].hint == "model id"


def test_session_info_plan_and_usage_updates_are_cached():
    from talk2agent.acp.agent_session import AgentSession

    def fake_spawn_agent_process(to_client, command, *args, cwd, env):
        client = to_client(object())
        return FakeSpawnContext(FakeConnection(client=client, session_ids=["session-1"]))

    session = AgentSession(
        command="codex-acp",
        args=[],
        cwd="F:/workspace",
        spawn_agent_process=fake_spawn_agent_process,
    )

    async def scenario():
        await session.ensure_started()
        await session._handle_update(
            "session-1",
            SessionInfoUpdate(
                sessionUpdate="session_info_update",
                title="Workspace Refactor",
                updatedAt="2026-03-26T12:00:00Z",
            ),
        )
        await session._handle_update(
            "session-1",
            AgentPlanUpdate(
                sessionUpdate="plan",
                entries=[
                    PlanEntry(
                        content="Audit the runtime status view",
                        status="in_progress",
                        priority="high",
                    ),
                    PlanEntry(
                        content="Update the Telegram bot tests",
                        status="pending",
                        priority="medium",
                    ),
                ],
            ),
        )
        await session._handle_update(
            "session-1",
            UsageUpdate(
                sessionUpdate="usage_update",
                used=512,
                size=4096,
                cost=Cost(amount=0.42, currency="USD"),
            ),
        )

    asyncio.run(scenario())

    assert session.session_title == "Workspace Refactor"
    assert session.session_updated_at == "2026-03-26T12:00:00Z"
    assert [entry.content for entry in session.plan_entries] == [
        "Audit the runtime status view",
        "Update the Telegram bot tests",
    ]
    assert [entry.status for entry in session.plan_entries] == ["in_progress", "pending"]
    assert session.usage.used == 512
    assert session.usage.size == 4096
    assert session.usage.cost_amount == pytest.approx(0.42)
    assert session.usage.cost_currency == "USD"


def test_recent_tool_activity_is_cached_and_latest_status_replaces_previous_entry():
    from talk2agent.acp.agent_session import AgentSession

    def fake_spawn_agent_process(to_client, command, *args, cwd, env):
        client = to_client(object())
        return FakeSpawnContext(FakeConnection(client=client, session_ids=["session-1"]))

    session = AgentSession(
        command="codex-acp",
        args=[],
        cwd="F:/workspace",
        spawn_agent_process=fake_spawn_agent_process,
    )

    async def scenario():
        await session.ensure_started()
        await session._handle_update(
            "session-1",
            ToolCallStart(
                sessionUpdate="tool_call",
                toolCallId="tool-1",
                title="Run tests",
                kind="execute",
                status="in_progress",
                rawInput={"command": "python -m pytest -q"},
                locations=[ToolCallLocation(path="tests/test_app.py", line=12)],
            ),
        )
        await session._handle_update(
            "session-1",
            ToolCallProgress(
                sessionUpdate="tool_call_update",
                toolCallId="tool-1",
                title="Run tests",
                kind="execute",
                status="completed",
                rawInput={"command": "python -m pytest -q"},
                locations=[ToolCallLocation(path="tests/test_app.py", line=12)],
            ),
        )

    asyncio.run(scenario())

    assert len(session.recent_tool_activities) == 1
    activity = session.recent_tool_activities[0]
    assert activity.tool_call_id == "tool-1"
    assert activity.title == "Run tests"
    assert activity.status == "completed"
    assert activity.kind == "execute"
    assert activity.details == (
        "cmd: python -m pytest -q",
        "paths: tests/test_app.py:12",
    )


def test_reset_clears_cached_session_metadata():
    from talk2agent.acp.agent_session import AgentSession

    pending_session_ids = [["session-1"], ["session-2"]]

    def fake_spawn_agent_process(to_client, command, *args, cwd, env):
        client = to_client(object())
        return FakeSpawnContext(
            FakeConnection(client=client, session_ids=pending_session_ids.pop(0))
        )

    session = AgentSession(
        command="codex-acp",
        args=[],
        cwd="F:/workspace",
        spawn_agent_process=fake_spawn_agent_process,
    )

    async def scenario():
        await session.ensure_started()
        await session._handle_update(
            "session-1",
            SessionInfoUpdate(
                sessionUpdate="session_info_update",
                title="Workspace Refactor",
            ),
        )
        await session._handle_update(
            "session-1",
            AgentPlanUpdate(
                sessionUpdate="plan",
                entries=[
                    PlanEntry(
                        content="Audit the runtime status view",
                        status="in_progress",
                        priority="high",
                    )
                ],
            ),
        )
        await session._handle_update(
            "session-1",
            UsageUpdate(
                sessionUpdate="usage_update",
                used=512,
                size=4096,
            ),
        )
        await session._handle_update(
            "session-1",
            ToolCallStart(
                sessionUpdate="tool_call",
                toolCallId="tool-1",
                title="Run tests",
                kind="execute",
                status="in_progress",
                rawInput={"command": "python -m pytest -q"},
            ),
        )
        await session.reset()

    asyncio.run(scenario())

    assert session.session_id == "session-2"
    assert session.session_title is None
    assert session.session_updated_at is None
    assert session.plan_entries == ()
    assert session.usage is None
    assert session.recent_tool_activities == ()


def test_read_terminal_output_delegates_to_client_for_current_session():
    from talk2agent.acp.agent_session import AgentSession

    calls = []
    expected = object()

    class FakeClient:
        async def terminal_output(self, *, session_id, terminal_id):
            calls.append((session_id, terminal_id))
            return expected

    session = AgentSession(
        command="codex-acp",
        args=[],
        cwd="F:/workspace",
        spawn_agent_process=lambda *args, **kwargs: None,
    )
    session.session_id = "session-1"
    session._client = FakeClient()

    output = asyncio.run(session.read_terminal_output("terminal-1"))

    assert output is expected
    assert calls == [("session-1", "terminal-1")]


def test_read_terminal_output_returns_none_without_live_session():
    from talk2agent.acp.agent_session import AgentSession

    session = AgentSession(
        command="codex-acp",
        args=[],
        cwd="F:/workspace",
        spawn_agent_process=lambda *args, **kwargs: None,
    )

    assert asyncio.run(session.read_terminal_output("terminal-1")) is None
