import asyncio
import time

from acp import text_block
from acp.schema import AgentMessageChunk


class FakeConnection:
    def __init__(self, client, session_ids, prompt_hooks=None):
        self._client = client
        self._session_ids = list(session_ids)
        self._prompt_hooks = [] if prompt_hooks is None else list(prompt_hooks)
        self.initialize_calls = []
        self.new_session_calls = []
        self.prompt_calls = []
        self.prompt_response = object()

    async def initialize(self, protocol_version):
        self.initialize_calls.append(protocol_version)
        return object()

    async def new_session(self, cwd, mcp_servers):
        self.new_session_calls.append((cwd, mcp_servers))

        class Response:
            def __init__(self, session_id):
                self.session_id = session_id

        return Response(self._session_ids.pop(0))

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

    def fake_spawn_agent_process(to_client, command, *args, cwd):
        spawn_calls.append((command, args, cwd))
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
    assert contexts[0].connection.new_session_calls == [("F:/workspace", [])]
    assert contexts[0].connection.prompt_calls == []


def test_run_turn_routes_updates_to_active_sink_only():
    from talk2agent.acp.agent_session import AgentSession

    contexts = []

    def fake_spawn_agent_process(to_client, command, *args, cwd):
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
    assert isinstance(session.last_used_at, float)
    assert before <= session.last_used_at <= after


def test_reset_closes_old_connection_and_starts_new_session():
    from talk2agent.acp.agent_session import AgentSession

    contexts = []
    pending_session_ids = [["session-1"], ["session-2"]]

    def fake_spawn_agent_process(to_client, command, *args, cwd):
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

    def fake_spawn_agent_process(to_client, command, *args, cwd):
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
