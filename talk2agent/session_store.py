from __future__ import annotations

import asyncio
from typing import Any, Callable


class RetiredSessionStoreError(RuntimeError):
    pass


class SessionStore:
    def __init__(self, session_factory: Callable[[int], Any], idle_timeout_minutes: float):
        self._session_factory = session_factory
        self._idle_timeout_seconds = idle_timeout_minutes * 60
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

    async def invalidate(self, user_id: int, session):
        user_lock = await self._get_user_lock(user_id)
        async with user_lock:
            async with self._lock:
                current = self._sessions.get(user_id)
                if current is not session:
                    return
                self._sessions.pop(user_id, None)

            await session.close()

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
