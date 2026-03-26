from __future__ import annotations

import asyncio
import codecs
import os
import secrets
import signal
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from talk2agent.acp.client_filesystem import resolve_workspace_target

DEFAULT_OUTPUT_BYTE_LIMIT = 64 * 1024
TERMINAL_READ_CHUNK_SIZE = 4096
TERMINAL_TERMINATE_TIMEOUT_SECONDS = 1.0


@dataclass(frozen=True, slots=True)
class ClientTerminalExitStatus:
    exit_code: int | None
    signal: str | None


@dataclass(frozen=True, slots=True)
class ClientTerminalOutput:
    output: str
    truncated: bool
    exit_status: ClientTerminalExitStatus | None


@dataclass(slots=True)
class _TerminalHandle:
    session_id: str
    process: subprocess.Popen[bytes]
    output_byte_limit: int
    decoder: Any
    reader_task: asyncio.Task[None] | None = None
    output_text: str = ""
    truncated: bool = False
    output_lock: threading.Lock = field(default_factory=threading.Lock)


class WorkspaceTerminalManager:
    def __init__(self, workspace_dir: str | Path):
        self._workspace_dir = Path(workspace_dir).resolve()
        self._terminals: dict[str, _TerminalHandle] = {}
        self._lock = asyncio.Lock()

    async def create_terminal(
        self,
        *,
        command: str,
        session_id: str,
        args: list[str] | None = None,
        cwd: str | None = None,
        env: list[Any] | None = None,
        output_byte_limit: int | None = None,
    ) -> str:
        resolved_cwd = self._resolve_cwd(cwd)
        resolved_limit = DEFAULT_OUTPUT_BYTE_LIMIT if output_byte_limit is None else output_byte_limit
        if resolved_limit < 0:
            raise ValueError("output_byte_limit must be non-negative")

        process = subprocess.Popen(
            [command, *(args or [])],
            cwd=str(resolved_cwd),
            env=self._build_env(env),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            shell=False,
        )
        terminal_id = secrets.token_hex(8)
        handle = _TerminalHandle(
            session_id=session_id,
            process=process,
            output_byte_limit=resolved_limit,
            decoder=codecs.getincrementaldecoder("utf-8")("replace"),
        )
        handle.reader_task = asyncio.create_task(asyncio.to_thread(self._consume_output, handle))
        async with self._lock:
            self._terminals[terminal_id] = handle
        return terminal_id

    async def terminal_output(self, *, session_id: str, terminal_id: str) -> ClientTerminalOutput:
        handle = await self._get_terminal(session_id=session_id, terminal_id=terminal_id)
        with handle.output_lock:
            output = handle.output_text
            truncated = handle.truncated
        return ClientTerminalOutput(
            output=output,
            truncated=truncated,
            exit_status=self._exit_status(handle),
        )

    async def wait_for_terminal_exit(
        self,
        *,
        session_id: str,
        terminal_id: str,
    ) -> ClientTerminalExitStatus:
        handle = await self._get_terminal(session_id=session_id, terminal_id=terminal_id)
        await asyncio.to_thread(handle.process.wait)
        if handle.reader_task is not None:
            await asyncio.shield(handle.reader_task)
        exit_status = self._exit_status(handle)
        if exit_status is None:
            raise RuntimeError("terminal exit status unavailable")
        return exit_status

    async def kill_terminal(self, *, session_id: str, terminal_id: str) -> None:
        handle = await self._get_terminal(session_id=session_id, terminal_id=terminal_id)
        await self._terminate_handle(handle)

    async def release_terminal(self, *, session_id: str, terminal_id: str) -> None:
        handle = await self._pop_terminal(session_id=session_id, terminal_id=terminal_id)
        await self._terminate_handle(handle)

    async def close(self) -> None:
        async with self._lock:
            handles = list(self._terminals.values())
            self._terminals.clear()
        for handle in handles:
            await self._terminate_handle(handle)

    def _consume_output(self, handle: _TerminalHandle) -> None:
        stream = handle.process.stdout
        if stream is None:
            return
        try:
            while True:
                chunk = stream.read(TERMINAL_READ_CHUNK_SIZE)
                if not chunk:
                    break
                text = handle.decoder.decode(chunk)
                if text:
                    self._append_output(handle, text)
        finally:
            tail = handle.decoder.decode(b"", final=True)
            if tail:
                self._append_output(handle, tail)
            stream.close()

    async def _get_terminal(self, *, session_id: str, terminal_id: str) -> _TerminalHandle:
        async with self._lock:
            handle = self._terminals.get(terminal_id)
        if handle is None or handle.session_id != session_id:
            raise KeyError(terminal_id)
        return handle

    async def _pop_terminal(self, *, session_id: str, terminal_id: str) -> _TerminalHandle:
        async with self._lock:
            handle = self._terminals.get(terminal_id)
            if handle is None or handle.session_id != session_id:
                raise KeyError(terminal_id)
            self._terminals.pop(terminal_id, None)
        return handle

    def _resolve_cwd(self, cwd: str | None) -> Path:
        target = str(self._workspace_dir) if cwd is None else cwd
        resolved = resolve_workspace_target(self._workspace_dir, target)
        if not resolved.exists():
            raise FileNotFoundError(resolved)
        if not resolved.is_dir():
            raise NotADirectoryError(resolved)
        return resolved

    def _build_env(self, env: list[Any] | None) -> dict[str, str]:
        resolved = dict(os.environ)
        if env is None:
            return resolved
        for item in env:
            name = str(getattr(item, "name"))
            value = str(getattr(item, "value"))
            resolved[name] = value
        return resolved

    def _append_output(self, handle: _TerminalHandle, text: str) -> None:
        with handle.output_lock:
            handle.output_text, was_truncated = _append_terminal_output(
                handle.output_text,
                text,
                handle.output_byte_limit,
            )
            handle.truncated = handle.truncated or was_truncated

    def _exit_status(self, handle: _TerminalHandle) -> ClientTerminalExitStatus | None:
        returncode = handle.process.poll()
        if returncode is None:
            return None
        if returncode < 0:
            signal_number = -returncode
            try:
                signal_name = signal.Signals(signal_number).name
            except Exception:
                signal_name = str(signal_number)
            return ClientTerminalExitStatus(exit_code=None, signal=signal_name)
        return ClientTerminalExitStatus(exit_code=returncode, signal=None)

    async def _terminate_handle(self, handle: _TerminalHandle) -> None:
        if handle.process.poll() is None:
            handle.process.terminate()
            try:
                await asyncio.to_thread(handle.process.wait, TERMINAL_TERMINATE_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                handle.process.kill()
                await asyncio.to_thread(handle.process.wait)
        if handle.reader_task is not None:
            await asyncio.shield(handle.reader_task)


def _append_terminal_output(existing_text: str, new_text: str, limit: int) -> tuple[str, bool]:
    combined = existing_text + new_text
    if limit <= 0:
        return "", bool(combined)
    if len(combined.encode("utf-8")) <= limit:
        return combined, False

    trimmed = combined
    while trimmed and len(trimmed.encode("utf-8")) > limit:
        trimmed = trimmed[1:]
    return trimmed, True
