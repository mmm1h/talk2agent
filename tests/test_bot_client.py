from acp.schema import PermissionOption, ToolCallUpdate


def test_session_update_forwards_to_update_handler():
    from talk2agent.acp.bot_client import BotClient

    calls = []

    async def on_update(session_id, update):
        calls.append((session_id, update))

    async def permission_policy(*args, **kwargs):
        raise AssertionError("permission policy should not be called")

    client = BotClient(
        on_update=on_update,
        permission_policy=permission_policy,
        workspace_dir="F:/workspace",
    )
    update = ToolCallUpdate(toolCallId="tool-1")

    import asyncio

    asyncio.run(client.session_update("session-1", update))

    assert calls == [("session-1", update)]


def test_request_permission_delegates_to_permission_policy():
    from talk2agent.acp.bot_client import BotClient

    calls = []
    expected_response = object()

    async def on_update(session_id, update):
        raise AssertionError("update handler should not be called")

    class PermissionPolicy:
        async def decide(self, session_id, options, tool_call):
            calls.append((session_id, options, tool_call))
            return expected_response

    client = BotClient(
        on_update=on_update,
        permission_policy=PermissionPolicy(),
        workspace_dir="F:/workspace",
    )
    options = [PermissionOption(kind="allow_once", name="Once", optionId="once")]
    tool_call = ToolCallUpdate(toolCallId="tool-2")

    import asyncio

    response = asyncio.run(
        client.request_permission(
            session_id="session-2",
            options=options,
            tool_call=tool_call,
        )
    )

    assert response is expected_response
    assert calls == [("session-2", options, tool_call)]


def test_read_text_file_reads_from_workspace_root(tmp_path):
    from talk2agent.acp.bot_client import BotClient

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "notes.txt"
    target.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")

    async def on_update(session_id, update):
        raise AssertionError("update handler should not be called")

    client = BotClient(
        on_update=on_update,
        permission_policy=object(),
        workspace_dir=str(workspace),
    )

    import asyncio

    response = asyncio.run(
        client.read_text_file(
            path=str(target),
            session_id="session-1",
            line=2,
            limit=1,
        )
    )

    assert response.content == "beta"


def test_write_text_file_creates_parent_directories_inside_workspace(tmp_path):
    from talk2agent.acp.bot_client import BotClient

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "nested" / "notes.txt"

    async def on_update(session_id, update):
        raise AssertionError("update handler should not be called")

    client = BotClient(
        on_update=on_update,
        permission_policy=object(),
        workspace_dir=str(workspace),
    )

    import asyncio

    asyncio.run(
        client.write_text_file(
            content="hello from acp client",
            path=str(target),
            session_id="session-1",
        )
    )

    assert target.read_text(encoding="utf-8") == "hello from acp client"


def test_write_text_file_rejects_workspace_escape(tmp_path):
    from talk2agent.acp.bot_client import BotClient

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"

    async def on_update(session_id, update):
        raise AssertionError("update handler should not be called")

    client = BotClient(
        on_update=on_update,
        permission_policy=object(),
        workspace_dir=str(workspace),
    )

    import asyncio
    import pytest

    with pytest.raises(ValueError, match="path escapes workspace root"):
        asyncio.run(
            client.write_text_file(
                content="nope",
                path=str(outside),
                session_id="session-1",
            )
        )


def test_create_terminal_waits_for_exit_and_returns_output(tmp_path):
    import asyncio
    import sys

    from talk2agent.acp.bot_client import BotClient

    workspace = tmp_path / "workspace"
    workspace.mkdir()

    async def on_update(session_id, update):
        raise AssertionError("update handler should not be called")

    client = BotClient(
        on_update=on_update,
        permission_policy=object(),
        workspace_dir=str(workspace),
    )

    async def scenario():
        created = await client.create_terminal(
            command=sys.executable,
            args=["-u", "-c", "import sys; sys.stdout.write('hello'); sys.stdout.flush()"],
            session_id="session-1",
            cwd=str(workspace),
            output_byte_limit=64,
        )
        waited = await client.wait_for_terminal_exit(
            session_id="session-1",
            terminal_id=created.terminal_id,
        )
        output = await client.terminal_output(
            session_id="session-1",
            terminal_id=created.terminal_id,
        )
        await client.release_terminal(
            session_id="session-1",
            terminal_id=created.terminal_id,
        )
        return created, waited, output

    created, waited, output = asyncio.run(scenario())

    assert created.terminal_id
    assert waited.exit_code == 0
    assert waited.signal is None
    assert output.output == "hello"
    assert output.truncated is False
    assert output.exit_status.exit_code == 0
    assert output.exit_status.signal is None


def test_terminal_output_respects_output_byte_limit(tmp_path):
    import asyncio
    import sys

    from talk2agent.acp.bot_client import BotClient

    workspace = tmp_path / "workspace"
    workspace.mkdir()

    async def on_update(session_id, update):
        raise AssertionError("update handler should not be called")

    client = BotClient(
        on_update=on_update,
        permission_policy=object(),
        workspace_dir=str(workspace),
    )

    async def scenario():
        created = await client.create_terminal(
            command=sys.executable,
            args=["-u", "-c", "import sys; sys.stdout.write('abcdef'); sys.stdout.flush()"],
            session_id="session-1",
            output_byte_limit=4,
        )
        await client.wait_for_terminal_exit(
            session_id="session-1",
            terminal_id=created.terminal_id,
        )
        output = await client.terminal_output(
            session_id="session-1",
            terminal_id=created.terminal_id,
        )
        await client.release_terminal(
            session_id="session-1",
            terminal_id=created.terminal_id,
        )
        return output

    output = asyncio.run(scenario())

    assert output.output == "cdef"
    assert output.truncated is True


def test_kill_terminal_stops_running_process(tmp_path):
    import asyncio
    import sys

    from talk2agent.acp.bot_client import BotClient

    workspace = tmp_path / "workspace"
    workspace.mkdir()

    async def on_update(session_id, update):
        raise AssertionError("update handler should not be called")

    client = BotClient(
        on_update=on_update,
        permission_policy=object(),
        workspace_dir=str(workspace),
    )

    async def scenario():
        created = await client.create_terminal(
            command=sys.executable,
            args=["-u", "-c", "import time; print('start', flush=True); time.sleep(30)"],
            session_id="session-1",
        )
        await asyncio.sleep(0.2)
        await client.kill_terminal(
            session_id="session-1",
            terminal_id=created.terminal_id,
        )
        waited = await client.wait_for_terminal_exit(
            session_id="session-1",
            terminal_id=created.terminal_id,
        )
        output = await client.terminal_output(
            session_id="session-1",
            terminal_id=created.terminal_id,
        )
        await client.release_terminal(
            session_id="session-1",
            terminal_id=created.terminal_id,
        )
        return waited, output

    waited, output = asyncio.run(scenario())

    assert "start" in output.output
    assert waited.exit_code is not None or waited.signal is not None


def test_create_terminal_rejects_cwd_escape(tmp_path):
    import asyncio
    import sys

    import pytest

    from talk2agent.acp.bot_client import BotClient

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()

    async def on_update(session_id, update):
        raise AssertionError("update handler should not be called")

    client = BotClient(
        on_update=on_update,
        permission_policy=object(),
        workspace_dir=str(workspace),
    )

    with pytest.raises(ValueError, match="path escapes workspace root"):
        asyncio.run(
            client.create_terminal(
                command=sys.executable,
                args=["-u", "-c", "print('hello')"],
                session_id="session-1",
                cwd=str(outside),
            )
        )
