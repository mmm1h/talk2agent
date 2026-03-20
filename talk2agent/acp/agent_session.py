from __future__ import annotations

import asyncio
import time
from typing import Sequence

from acp import PROTOCOL_VERSION, spawn_agent_process, text_block

from talk2agent.acp.bot_client import BotClient
from talk2agent.acp.permission import AutoApprovePermissionPolicy


class AgentSession:
    def __init__(
        self,
        command: str,
        args: Sequence[str],
        cwd: str,
        mcp_servers=None,
        permission_policy=None,
        spawn_agent_process=spawn_agent_process,
    ):
        self.command = command
        self.args = list(args)
        self.cwd = cwd
        self.mcp_servers = [] if mcp_servers is None else list(mcp_servers)
        self.last_used_at = time.monotonic()
        self.session_id = None

        self._spawn_agent_process = spawn_agent_process
        self._permission_policy = (
            AutoApprovePermissionPolicy() if permission_policy is None else permission_policy
        )
        self._client = BotClient(
            on_update=self._handle_update,
            permission_policy=self._permission_policy,
        )
        self._lifecycle_lock = asyncio.Lock()
        self._startup_lock = asyncio.Lock()
        self._context_manager = None
        self._conn = None
        self._process = None
        self._active_sink = None

    async def ensure_started(self):
        await self._ensure_started_locked()

    async def _ensure_started_locked(self):
        async with self._startup_lock:
            if self._conn is not None and self.session_id is not None:
                return

            context_manager = self._spawn_agent_process(
                lambda _agent: self._client,
                self.command,
                *self.args,
                cwd=self.cwd,
            )

            try:
                conn, process = await context_manager.__aenter__()
                await conn.initialize(protocol_version=PROTOCOL_VERSION)
                response = await conn.new_session(cwd=self.cwd, mcp_servers=self.mcp_servers)
            except Exception:
                await context_manager.__aexit__(None, None, None)
                raise

            self._context_manager = context_manager
            self._conn = conn
            self._process = process
            self.session_id = response.session_id

    async def run_turn(self, prompt_text, sink):
        async with self._lifecycle_lock:
            await self._ensure_started_locked()
            self._active_sink = sink
            try:
                return await self._conn.prompt([text_block(prompt_text)], session_id=self.session_id)
            finally:
                self._active_sink = None
                self.last_used_at = time.monotonic()

    async def _handle_update(self, session_id, update):
        if session_id != self.session_id or self._active_sink is None:
            return
        await self._active_sink.on_update(update)

    async def close(self):
        async with self._lifecycle_lock:
            await self._close_locked()

    async def _close_locked(self):
        async with self._startup_lock:
            if self._context_manager is None:
                return

            context_manager = self._context_manager
            self._context_manager = None
            self._conn = None
            self._process = None
            self.session_id = None
            self._active_sink = None

            await context_manager.__aexit__(None, None, None)

    async def reset(self):
        async with self._lifecycle_lock:
            await self._close_locked()
            await self._ensure_started_locked()
