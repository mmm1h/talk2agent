from acp.schema import (
    CreateTerminalResponse,
    KillTerminalCommandResponse,
    ReadTextFileResponse,
    ReleaseTerminalResponse,
    TerminalExitStatus,
    TerminalOutputResponse,
    WaitForTerminalExitResponse,
    WriteTextFileResponse,
)

from talk2agent.acp.client_filesystem import (
    read_workspace_text_file,
    write_workspace_text_file,
)
from talk2agent.acp.client_terminal import WorkspaceTerminalManager


class BotClient:
    def __init__(self, on_update, permission_policy, *, workspace_dir):
        self._on_update = on_update
        self._permission_policy = permission_policy
        self._workspace_dir = workspace_dir
        self._terminal_manager = WorkspaceTerminalManager(workspace_dir)

    async def session_update(self, session_id, update, **kwargs):
        await self._on_update(session_id, update)

    async def request_permission(self, options, session_id, tool_call, **kwargs):
        return await self._permission_policy.decide(
            session_id=session_id,
            options=options,
            tool_call=tool_call,
        )

    async def read_text_file(self, path, session_id, limit=None, line=None, **kwargs):
        result = read_workspace_text_file(
            self._workspace_dir,
            path,
            line=line,
            limit=limit,
        )
        return ReadTextFileResponse(content=result.content)

    async def write_text_file(self, content, path, session_id, **kwargs):
        write_workspace_text_file(
            self._workspace_dir,
            path,
            content,
        )
        return WriteTextFileResponse()

    async def create_terminal(
        self,
        command,
        session_id,
        args=None,
        cwd=None,
        env=None,
        output_byte_limit=None,
        **kwargs,
    ):
        terminal_id = await self._terminal_manager.create_terminal(
            command=command,
            session_id=session_id,
            args=None if args is None else list(args),
            cwd=cwd,
            env=None if env is None else list(env),
            output_byte_limit=output_byte_limit,
        )
        return CreateTerminalResponse(terminalId=terminal_id)

    async def terminal_output(self, session_id, terminal_id, **kwargs):
        output = await self._terminal_manager.terminal_output(
            session_id=session_id,
            terminal_id=terminal_id,
        )
        return TerminalOutputResponse(
            output=output.output,
            truncated=output.truncated,
            exitStatus=_coerce_terminal_exit_status(output.exit_status),
        )

    async def release_terminal(self, session_id, terminal_id, **kwargs):
        await self._terminal_manager.release_terminal(
            session_id=session_id,
            terminal_id=terminal_id,
        )
        return ReleaseTerminalResponse()

    async def wait_for_terminal_exit(self, session_id, terminal_id, **kwargs):
        exit_status = await self._terminal_manager.wait_for_terminal_exit(
            session_id=session_id,
            terminal_id=terminal_id,
        )
        return WaitForTerminalExitResponse(
            exitCode=exit_status.exit_code,
            signal=exit_status.signal,
        )

    async def kill_terminal(self, session_id, terminal_id, **kwargs):
        await self._terminal_manager.kill_terminal(
            session_id=session_id,
            terminal_id=terminal_id,
        )
        return KillTerminalCommandResponse()

    async def close(self):
        await self._terminal_manager.close()


def _coerce_terminal_exit_status(exit_status):
    if exit_status is None:
        return None
    return TerminalExitStatus(
        exitCode=exit_status.exit_code,
        signal=exit_status.signal,
    )
