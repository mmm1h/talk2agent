from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class SessionHistoryEntry:
    provider: str
    telegram_user_id: int
    session_id: str
    title: str
    cwd: str
    created_at: str
    updated_at: str


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class SessionHistoryStore:
    def __init__(self, path: Path):
        self._path = path
        self._lock = asyncio.Lock()

    async def list_entries(
        self,
        provider: str,
        telegram_user_id: int,
        cwd: str,
    ) -> list[SessionHistoryEntry]:
        async with self._lock:
            entries = self._load_entries_locked()
        filtered = [
            entry
            for entry in entries
            if entry.provider == provider
            and entry.telegram_user_id == telegram_user_id
            and entry.cwd == cwd
        ]
        return sorted(filtered, key=lambda entry: entry.updated_at, reverse=True)

    async def get_entry(
        self,
        provider: str,
        telegram_user_id: int,
        session_id: str,
        cwd: str,
    ) -> SessionHistoryEntry | None:
        async with self._lock:
            entries = self._load_entries_locked()
        for entry in entries:
            if (
                entry.provider == provider
                and entry.telegram_user_id == telegram_user_id
                and entry.session_id == session_id
                and entry.cwd == cwd
            ):
                return entry
        return None

    async def touch_entry(
        self,
        provider: str,
        telegram_user_id: int,
        session_id: str,
        *,
        title: str | None = None,
        cwd: str,
        updated_at: str | None = None,
    ) -> SessionHistoryEntry:
        updated_at = updated_at or _utc_now_iso()

        async with self._lock:
            entries = self._load_entries_locked()
            for index, entry in enumerate(entries):
                if (
                    entry.provider == provider
                    and entry.telegram_user_id == telegram_user_id
                    and entry.session_id == session_id
                    and entry.cwd == cwd
                ):
                    replacement = replace(
                        entry,
                        title=title if title else entry.title,
                        cwd=cwd or entry.cwd,
                        updated_at=updated_at,
                    )
                    entries[index] = replacement
                    self._write_entries_locked(entries)
                    return replacement

            created_at = updated_at
            entry = SessionHistoryEntry(
                provider=provider,
                telegram_user_id=telegram_user_id,
                session_id=session_id,
                title=title or "",
                cwd=cwd,
                created_at=created_at,
                updated_at=updated_at,
            )
            entries.append(entry)
            self._write_entries_locked(entries)
            return entry

    async def rename_entry(
        self,
        provider: str,
        telegram_user_id: int,
        session_id: str,
        *,
        title: str,
        cwd: str,
        updated_at: str | None = None,
    ) -> SessionHistoryEntry:
        updated_at = updated_at or _utc_now_iso()

        async with self._lock:
            entries = self._load_entries_locked()
            for index, entry in enumerate(entries):
                if (
                    entry.provider == provider
                    and entry.telegram_user_id == telegram_user_id
                    and entry.session_id == session_id
                    and entry.cwd == cwd
                ):
                    replacement = replace(
                        entry,
                        title=title,
                        updated_at=updated_at,
                    )
                    entries[index] = replacement
                    self._write_entries_locked(entries)
                    return replacement

        raise KeyError(session_id)

    async def delete_entry(
        self,
        provider: str,
        telegram_user_id: int,
        session_id: str,
        cwd: str,
    ) -> bool:
        async with self._lock:
            entries = self._load_entries_locked()
            filtered = [
                entry
                for entry in entries
                if not (
                    entry.provider == provider
                    and entry.telegram_user_id == telegram_user_id
                    and entry.session_id == session_id
                    and entry.cwd == cwd
                )
            ]
            if len(filtered) == len(entries):
                return False
            self._write_entries_locked(filtered)
            return True

    def _load_entries_locked(self) -> list[SessionHistoryEntry]:
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except Exception:
            return []

        raw_entries = payload.get("entries") if isinstance(payload, dict) else None
        if not isinstance(raw_entries, list):
            return []

        entries: list[SessionHistoryEntry] = []
        for raw_entry in raw_entries:
            entry = self._coerce_entry(raw_entry)
            if entry is not None:
                entries.append(entry)
        return entries

    def _write_entries_locked(self, entries: list[SessionHistoryEntry]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"entries": [asdict(entry) for entry in entries]}
        self._path.write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")

    def _coerce_entry(self, raw_entry: Any) -> SessionHistoryEntry | None:
        if not isinstance(raw_entry, dict):
            return None
        try:
            provider = str(raw_entry["provider"])
            telegram_user_id = int(raw_entry["telegram_user_id"])
            session_id = str(raw_entry["session_id"])
            title = str(raw_entry.get("title", ""))
            cwd = str(raw_entry["cwd"])
            created_at = str(raw_entry["created_at"])
            updated_at = str(raw_entry["updated_at"])
        except (KeyError, TypeError, ValueError):
            return None

        return SessionHistoryEntry(
            provider=provider,
            telegram_user_id=telegram_user_id,
            session_id=session_id,
            title=title,
            cwd=cwd,
            created_at=created_at,
            updated_at=updated_at,
        )
