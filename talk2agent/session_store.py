from __future__ import annotations

import asyncio
from typing import Any, Callable

from talk2agent.session_history import SessionHistoryEntry, SessionHistoryStore


class RetiredSessionStoreError(RuntimeError):
    pass


def _truncate_title(text: str, limit: int = 60) -> str:
    stripped = text.strip()
    if not stripped:
        return ""
    if len(stripped) <= limit:
        return stripped
    return f"{stripped[: limit - 3]}..."


class SessionStore:
    def __init__(
        self,
        session_factory: Callable[[int], Any],
        idle_timeout_minutes: float,
        *,
        provider: str = "unknown",
        workspace_dir: str = ".",
        history_store: SessionHistoryStore | None = None,
    ):
        self._session_factory = session_factory
        self._idle_timeout_seconds = idle_timeout_minutes * 60
        self._provider = provider
        self._workspace_dir = workspace_dir
        self._history_store = history_store
        self._sessions: dict[int, Any] = {}
        self._lock = asyncio.Lock()
        self._user_locks: dict[int, asyncio.Lock] = {}
        self._retired = False

    async def peek(self, user_id: int):
        async with self._lock:
            if self._retired:
                raise RetiredSessionStoreError("session store retired")
            return self._sessions.get(user_id)

    async def retire(self) -> None:
        async with self._lock:
            self._retired = True

    async def activate(self) -> None:
        async with self._lock:
            self._retired = False

    async def get_or_create(self, user_id: int):
        user_lock = await self._get_user_lock(user_id)
        async with user_lock:
            async with self._lock:
                if self._retired:
                    raise RetiredSessionStoreError("session store retired")
                session = self._sessions.get(user_id)
                if session is None:
                    session = self._session_factory(user_id)
                    self._sessions[user_id] = session
                return session

    async def reset(self, user_id: int):
        user_lock = await self._get_user_lock(user_id)
        async with user_lock:
            async with self._lock:
                if self._retired:
                    raise RetiredSessionStoreError("session store retired")
                old_session = self._sessions.get(user_id)

            session = self._session_factory(user_id)

            if old_session is None:
                async with self._lock:
                    if self._retired:
                        try:
                            await session.close()
                        except Exception:
                            pass
                        raise RetiredSessionStoreError("session store retired")
                    self._sessions[user_id] = session
                    return session

            try:
                await old_session.close()
            except Exception:
                try:
                    await session.close()
                except Exception:
                    pass
                raise

            async with self._lock:
                if self._retired:
                    current = self._sessions.get(user_id)
                    if current is old_session:
                        self._sessions.pop(user_id, None)
                    try:
                        await session.close()
                    except Exception:
                        pass
                    raise RetiredSessionStoreError("session store retired")
                self._sessions[user_id] = session
                return session

    async def restart(self, user_id: int):
        return await self.reset(user_id)

    async def activate_history_session(self, user_id: int, session_id: str):
        entry = None
        if self._history_store is not None:
            entry = await self._history_store.get_entry(
                self._provider,
                user_id,
                session_id,
                self._workspace_dir,
            )
            if entry is None:
                raise KeyError(session_id)

        return await self._activate_session(
            user_id,
            session_id,
            title_hint=None if entry is None else entry.title,
        )

    async def activate_provider_session(
        self,
        user_id: int,
        session_id: str,
        *,
        title_hint: str | None = None,
    ):
        return await self._activate_session(
            user_id,
            session_id,
            title_hint=title_hint,
        )

    async def list_history(self, user_id: int) -> list[SessionHistoryEntry]:
        if self._history_store is None:
            return []
        return await self._history_store.list_entries(
            self._provider,
            user_id,
            self._workspace_dir,
        )

    async def rename_history(self, user_id: int, session_id: str, title: str) -> SessionHistoryEntry:
        if self._history_store is None:
            raise RuntimeError("session history not configured")

        normalized_title = _truncate_title(title)
        if not normalized_title:
            raise ValueError("session title cannot be empty")

        return await self._history_store.rename_entry(
            self._provider,
            user_id,
            session_id,
            title=normalized_title,
            cwd=self._workspace_dir,
        )

    async def delete_history(self, user_id: int, session_id: str) -> bool:
        deleted_entry = False
        if self._history_store is not None:
            deleted_entry = await self._history_store.delete_entry(
                self._provider,
                user_id,
                session_id,
                self._workspace_dir,
            )

        active_session = None
        user_lock = await self._get_user_lock(user_id)
        async with user_lock:
            async with self._lock:
                current = self._sessions.get(user_id)
                if current is not None and current.session_id == session_id:
                    active_session = current
                    self._sessions.pop(user_id, None)

            if active_session is not None:
                await active_session.close()

        return deleted_entry

    async def record_session_usage(self, user_id: int, session, *, title_hint: str | None = None) -> None:
        if self._history_store is None or session.session_id is None:
            return
        await self._history_store.touch_entry(
            self._provider,
            user_id,
            session.session_id,
            title=_truncate_title(title_hint or ""),
            cwd=self._workspace_dir,
        )

    async def invalidate(self, user_id: int, session):
        user_lock = await self._get_user_lock(user_id)
        async with user_lock:
            async with self._lock:
                current = self._sessions.get(user_id)
                if current is not session:
                    return
                self._sessions.pop(user_id, None)

            await session.close()

    async def _activate_session(
        self,
        user_id: int,
        session_id: str,
        *,
        title_hint: str | None,
    ):
        user_lock = await self._get_user_lock(user_id)
        async with user_lock:
            async with self._lock:
                if self._retired:
                    raise RetiredSessionStoreError("session store retired")
                old_session = self._sessions.get(user_id)

            if old_session is not None and old_session.session_id == session_id:
                await self.record_session_usage(user_id, old_session, title_hint=title_hint)
                return old_session

            session = self._session_factory(user_id)
            try:
                await session.load_session(session_id, prefer_resume=True)
            except Exception:
                try:
                    await session.close()
                except Exception:
                    pass
                raise

            if old_session is not None:
                try:
                    await old_session.close()
                except Exception:
                    try:
                        await session.close()
                    except Exception:
                        pass
                    raise

            async with self._lock:
                if self._retired:
                    current = self._sessions.get(user_id)
                    if current is old_session:
                        self._sessions.pop(user_id, None)
                    try:
                        await session.close()
                    except Exception:
                        pass
                    raise RetiredSessionStoreError("session store retired")
                self._sessions[user_id] = session

        await self.record_session_usage(user_id, session, title_hint=title_hint)
        return session

    async def close_idle_sessions(self, now: float):
        async with self._lock:
            candidates = list(self._sessions.items())

        for user_id, session in candidates:
            user_lock = await self._get_user_lock(user_id)
            async with user_lock:
                async with self._lock:
                    current = self._sessions.get(user_id)
                    if current is not session:
                        continue
                    if now - current.last_used_at < self._idle_timeout_seconds:
                        continue
                    self._sessions.pop(user_id, None)

            try:
                await session.close()
            except Exception:
                continue

    async def close_all(self):
        async with self._lock:
            candidates = list(self._sessions.items())

        for user_id, session in candidates:
            user_lock = await self._get_user_lock(user_id)
            async with user_lock:
                async with self._lock:
                    current = self._sessions.get(user_id)
                    if current is not session:
                        continue
                    self._sessions.pop(user_id, None)

            try:
                await session.close()
            except Exception:
                continue

    async def _get_user_lock(self, user_id: int) -> asyncio.Lock:
        async with self._lock:
            user_lock = self._user_locks.get(user_id)
            if user_lock is None:
                user_lock = asyncio.Lock()
                self._user_locks[user_id] = user_lock
            return user_lock
