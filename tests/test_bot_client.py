from acp.schema import PermissionOption, ToolCallUpdate


def test_session_update_forwards_to_update_handler():
    from talk2agent.acp.bot_client import BotClient

    calls = []

    async def on_update(session_id, update):
        calls.append((session_id, update))

    async def permission_policy(*args, **kwargs):
        raise AssertionError("permission policy should not be called")

    client = BotClient(on_update=on_update, permission_policy=permission_policy)
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

    client = BotClient(on_update=on_update, permission_policy=PermissionPolicy())
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
