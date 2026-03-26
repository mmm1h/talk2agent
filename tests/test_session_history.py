import asyncio
from pathlib import Path

from talk2agent.session_history import SessionHistoryStore


def test_touch_entry_updates_existing_record_in_place(tmp_path: Path):
    store = SessionHistoryStore(tmp_path / "session-history.json")

    async def scenario():
        first = await store.touch_entry(
            "codex",
            123,
            "session-1",
            title="first",
            cwd="F:/workspace",
            updated_at="2026-03-20T00:00:00+00:00",
        )
        second = await store.touch_entry(
            "codex",
            123,
            "session-1",
            title="second",
            cwd="F:/workspace",
            updated_at="2026-03-21T00:00:00+00:00",
        )
        entries = await store.list_entries("codex", 123, "F:/workspace")
        return first, second, entries

    first, second, entries = asyncio.run(scenario())

    assert first.created_at == "2026-03-20T00:00:00+00:00"
    assert second.created_at == "2026-03-20T00:00:00+00:00"
    assert second.updated_at == "2026-03-21T00:00:00+00:00"
    assert entries[0].title == "second"


def test_list_entries_filters_by_provider_user_and_workspace(tmp_path: Path):
    store = SessionHistoryStore(tmp_path / "session-history.json")

    async def scenario():
        await store.touch_entry("codex", 123, "session-1", title="one", cwd="F:/workspace")
        await store.touch_entry("gemini", 123, "session-2", title="two", cwd="F:/workspace")
        await store.touch_entry("codex", 999, "session-3", title="three", cwd="F:/workspace")
        await store.touch_entry("codex", 123, "session-4", title="four", cwd="F:/other")
        return await store.list_entries("codex", 123, "F:/workspace")

    entries = asyncio.run(scenario())

    assert [entry.session_id for entry in entries] == ["session-1"]


def test_delete_entry_filters_by_workspace(tmp_path: Path):
    store = SessionHistoryStore(tmp_path / "session-history.json")

    async def scenario():
        await store.touch_entry("codex", 123, "session-1", title="one", cwd="F:/workspace")
        await store.touch_entry("codex", 123, "session-1", title="other", cwd="F:/other")
        deleted = await store.delete_entry("codex", 123, "session-1", "F:/workspace")
        remaining = await store.list_entries("codex", 123, "F:/other")
        return deleted, remaining

    deleted, remaining = asyncio.run(scenario())

    assert deleted is True
    assert [entry.session_id for entry in remaining] == ["session-1"]


def test_rename_entry_updates_title_in_place(tmp_path: Path):
    store = SessionHistoryStore(tmp_path / "session-history.json")

    async def scenario():
        await store.touch_entry(
            "codex",
            123,
            "session-1",
            title="first",
            cwd="F:/workspace",
            updated_at="2026-03-20T00:00:00+00:00",
        )
        renamed = await store.rename_entry(
            "codex",
            123,
            "session-1",
            title="renamed",
            cwd="F:/workspace",
            updated_at="2026-03-21T00:00:00+00:00",
        )
        entries = await store.list_entries("codex", 123, "F:/workspace")
        return renamed, entries

    renamed, entries = asyncio.run(scenario())

    assert renamed.title == "renamed"
    assert renamed.created_at == "2026-03-20T00:00:00+00:00"
    assert renamed.updated_at == "2026-03-21T00:00:00+00:00"
    assert entries[0].title == "renamed"
