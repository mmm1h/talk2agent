import asyncio

import pytest

from talk2agent.session_store import RetiredSessionStoreError, SessionStore


class FakeSession:
    def __init__(self, user_id, last_used_at, close_error=None):
        self.user_id = user_id
        self.last_used_at = last_used_at
        self.close_error = close_error
        self.closed = False
        self.close_calls = 0

    async def close(self):
        self.closed = True
        self.close_calls += 1
        if self.close_error is not None:
            raise self.close_error


class FakeSessionFactory:
    def __init__(self, last_used_at_by_user_id=None, close_error_by_user_id=None):
        self.last_used_at_by_user_id = {} if last_used_at_by_user_id is None else dict(last_used_at_by_user_id)
        self.close_error_by_user_id = {} if close_error_by_user_id is None else dict(close_error_by_user_id)
        self.created_user_ids = []
        self.created_sessions = []
        self.closed_ids = []

    def __call__(self, user_id):
        self.created_user_ids.append(user_id)
        session = FakeSession(
            user_id,
            self.last_used_at_by_user_id.get(user_id, 0.0),
            close_error=self.close_error_by_user_id.get(user_id),
        )
        original_close = session.close

        async def close():
            try:
                await original_close()
            finally:
                self.closed_ids.append(user_id)

        session.close = close
        self.created_sessions.append(session)
        return session


class RaisingSessionFactory(FakeSessionFactory):
    def __init__(self, last_used_at_by_user_id=None, fail_on_call=None):
        super().__init__(last_used_at_by_user_id=last_used_at_by_user_id)
        self.fail_on_call = fail_on_call
        self.call_count = 0

    def __call__(self, user_id):
        self.call_count += 1
        if self.fail_on_call is not None and self.call_count == self.fail_on_call:
            raise RuntimeError("factory failed")
        return super().__call__(user_id)


def test_get_or_create_reuses_existing_session():
    factory = FakeSessionFactory()
    store = SessionStore(session_factory=factory, idle_timeout_minutes=30)

    async def scenario():
        first = await store.get_or_create(123)
        second = await store.get_or_create(123)
        return first, second

    first, second = asyncio.run(scenario())

    assert first is second
    assert factory.created_user_ids == [123]


def test_peek_returns_existing_session_without_creating_new_one():
    factory = FakeSessionFactory()
    store = SessionStore(session_factory=factory, idle_timeout_minutes=30)

    async def scenario():
        await store.get_or_create(123)
        return await store.peek(123), factory.created_user_ids

    session, created_user_ids = asyncio.run(scenario())

    assert session.user_id == 123
    assert created_user_ids == [123]


def test_retired_store_rejects_peek_lookup():
    factory = FakeSessionFactory()
    store = SessionStore(session_factory=factory, idle_timeout_minutes=30)

    async def scenario():
        await store.retire()
        with pytest.raises(RetiredSessionStoreError):
            await store.peek(123)

    asyncio.run(scenario())


def test_retired_store_rejects_new_session_creation():
    factory = FakeSessionFactory()
    store = SessionStore(session_factory=factory, idle_timeout_minutes=30)

    async def scenario():
        await store.retire()
        with pytest.raises(RetiredSessionStoreError):
            await store.get_or_create(123)

    asyncio.run(scenario())


def test_retired_store_rejects_reset():
    factory = FakeSessionFactory()
    store = SessionStore(session_factory=factory, idle_timeout_minutes=30)

    async def scenario():
        await store.retire()
        with pytest.raises(RetiredSessionStoreError):
            await store.reset(123)

    asyncio.run(scenario())


def test_unretire_restores_access_after_rollback():
    factory = FakeSessionFactory()
    store = SessionStore(session_factory=factory, idle_timeout_minutes=30)

    async def scenario():
        await store.retire()
        await store.activate()
        return await store.get_or_create(123)

    session = asyncio.run(scenario())

    assert session.user_id == 123


def test_reset_closes_old_session_and_returns_new_one():
    factory = FakeSessionFactory()
    store = SessionStore(session_factory=factory, idle_timeout_minutes=30)

    async def scenario():
        old = await store.get_or_create(123)
        new = await store.reset(123)
        return old, new

    old, new = asyncio.run(scenario())

    assert old.closed is True
    assert old.close_calls == 1
    assert new is not old
    assert factory.created_user_ids == [123, 123]
    assert factory.closed_ids == [123]


def test_reset_keeps_old_session_if_factory_raises():
    factory = RaisingSessionFactory(fail_on_call=2)
    store = SessionStore(session_factory=factory, idle_timeout_minutes=30)

    async def scenario():
        original = await store.get_or_create(123)
        try:
            await store.reset(123)
        except RuntimeError as exc:
            assert str(exc) == "factory failed"
        else:
            raise AssertionError("reset should have raised")
        reused = await store.get_or_create(123)
        return original, reused

    original, reused = asyncio.run(scenario())

    assert reused is original
    assert original.closed is False
    assert factory.created_user_ids == [123]
    assert factory.closed_ids == []


def test_reset_does_not_publish_replacement_if_old_close_raises():
    factory = FakeSessionFactory(close_error_by_user_id={123: RuntimeError("close failed")})
    store = SessionStore(session_factory=factory, idle_timeout_minutes=30)

    async def scenario():
        original = await store.get_or_create(123)
        try:
            await store.reset(123)
        except RuntimeError as exc:
            assert str(exc) == "close failed"
        else:
            raise AssertionError("reset should have raised")
        prepared_replacement = factory.created_sessions[1]
        reused = await store.get_or_create(123)
        return original, prepared_replacement, reused

    original, prepared_replacement, reused = asyncio.run(scenario())

    assert prepared_replacement is not original
    assert reused is original
    assert reused is not prepared_replacement
    assert factory.created_user_ids == [123, 123]
    assert factory.closed_ids == [123, 123]


def test_reset_rechecks_retired_state_before_publishing_replacement():
    close_started = asyncio.Event()
    allow_close = asyncio.Event()

    class BlockingCloseSession(FakeSession):
        async def close(self):
            self.closed = True
            self.close_calls += 1
            close_started.set()
            await allow_close.wait()

    class QueuedFactory:
        def __init__(self):
            self.created_user_ids = []
            self.sessions = [
                BlockingCloseSession(123, 0.0),
                FakeSession(123, 0.0),
                FakeSession(123, 0.0),
            ]

        def __call__(self, user_id):
            self.created_user_ids.append(user_id)
            return self.sessions.pop(0)

    store = SessionStore(session_factory=QueuedFactory(), idle_timeout_minutes=30)

    async def scenario():
        await store.get_or_create(123)
        reset_task = asyncio.create_task(store.reset(123))
        await close_started.wait()
        await store.retire()
        allow_close.set()
        with pytest.raises(RetiredSessionStoreError):
            await reset_task
        await store.activate()
        recreated = await store.get_or_create(123)
        return recreated

    recreated = asyncio.run(scenario())

    assert recreated.user_id == 123
    assert recreated.closed is False


def test_invalidate_closes_and_removes_current_session():
    factory = FakeSessionFactory()
    store = SessionStore(session_factory=factory, idle_timeout_minutes=30)

    async def scenario():
        original = await store.get_or_create(123)
        await store.invalidate(123, original)
        replacement = await store.get_or_create(123)
        return original, replacement

    original, replacement = asyncio.run(scenario())

    assert original.closed is True
    assert original.close_calls == 1
    assert replacement is not original
    assert factory.created_user_ids == [123, 123]
    assert factory.closed_ids == [123]


def test_close_all_closes_every_session():
    factory = FakeSessionFactory()
    store = SessionStore(session_factory=factory, idle_timeout_minutes=30)

    async def scenario():
        first = await store.get_or_create(123)
        second = await store.get_or_create(456)
        await store.close_all()
        return first, second

    first, second = asyncio.run(scenario())

    assert first.closed is True
    assert second.closed is True
    assert factory.closed_ids == [123, 456]


def test_close_all_continues_when_one_close_fails():
    factory = FakeSessionFactory(
        close_error_by_user_id={
            123: RuntimeError("close failed"),
        }
    )
    store = SessionStore(session_factory=factory, idle_timeout_minutes=30)

    async def scenario():
        first = await store.get_or_create(123)
        second = await store.get_or_create(456)
        await store.close_all()
        replacement = await store.get_or_create(123)
        return first, second, replacement

    first, second, replacement = asyncio.run(scenario())

    assert first.close_calls == 1
    assert second.close_calls == 1
    assert second.closed is True
    assert factory.closed_ids == [123, 456]
    assert replacement is not first


def test_close_idle_sessions_only_closes_expired_sessions():
    factory = FakeSessionFactory(
        {
            123: 60.0,
            456: 75.0,
            789: 81.0,
        }
    )
    store = SessionStore(session_factory=factory, idle_timeout_minutes=0.5)

    async def scenario():
        expired = await store.get_or_create(123)
        active = await store.get_or_create(456)
        await store.get_or_create(789)
        await store.close_idle_sessions(now=100.0)
        reused_active = await store.get_or_create(456)
        return expired, active, reused_active

    expired, active, reused_active = asyncio.run(scenario())

    assert factory.closed_ids == [123]
    assert expired.closed is True
    assert active.closed is False
    assert reused_active is active


def test_close_idle_sessions_continues_when_one_close_fails():
    factory = FakeSessionFactory(
        {
            123: 60.0,
            456: 61.0,
        },
        close_error_by_user_id={
            123: RuntimeError("close failed"),
        },
    )
    store = SessionStore(session_factory=factory, idle_timeout_minutes=0.5)

    async def scenario():
        failed = await store.get_or_create(123)
        succeeded = await store.get_or_create(456)
        await store.close_idle_sessions(now=100.0)
        replacement = await store.get_or_create(123)
        return failed, succeeded, replacement

    failed, succeeded, replacement = asyncio.run(scenario())

    assert failed.close_calls == 1
    assert succeeded.close_calls == 1
    assert factory.closed_ids == [123, 456]
    assert replacement is not failed


def test_slow_reset_for_one_user_does_not_block_other_user():
    close_started = asyncio.Event()
    allow_close = asyncio.Event()

    class BlockingCloseSession(FakeSession):
        async def close(self):
            self.closed = True
            self.close_calls += 1
            close_started.set()
            await allow_close.wait()

    class QueuedSessionFactory:
        def __init__(self):
            self.created_user_ids = []
            self.sessions_by_user = {
                1: [
                    BlockingCloseSession(1, 0.0),
                    FakeSession(1, 0.0),
                ],
                2: [FakeSession(2, 0.0)],
            }

        def __call__(self, user_id):
            self.created_user_ids.append(user_id)
            return self.sessions_by_user[user_id].pop(0)

    factory = QueuedSessionFactory()
    store = SessionStore(session_factory=factory, idle_timeout_minutes=30)

    async def scenario():
        await store.get_or_create(1)
        reset_task = asyncio.create_task(store.reset(1))
        await close_started.wait()
        other_user_session = await asyncio.wait_for(store.get_or_create(2), timeout=0.05)
        allow_close.set()
        reset_session = await reset_task
        return other_user_session, reset_session

    other_user_session, reset_session = asyncio.run(scenario())

    assert other_user_session.user_id == 2
    assert reset_session.user_id == 1
    assert factory.created_user_ids == [1, 1, 2]
