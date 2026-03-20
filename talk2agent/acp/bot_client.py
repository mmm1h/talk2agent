class BotClient:
    def __init__(self, on_update, permission_policy):
        self._on_update = on_update
        self._permission_policy = permission_policy

    async def session_update(self, session_id, update, **kwargs):
        await self._on_update(session_id, update)

    async def request_permission(self, options, session_id, tool_call, **kwargs):
        return await self._permission_policy.decide(
            session_id=session_id,
            options=options,
            tool_call=tool_call,
        )
