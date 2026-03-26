from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import mimetypes
import re
import secrets
from datetime import datetime, timezone

from talk2agent.workspace_files import resolve_workspace_path


INBOX_RELATIVE_DIR = ".talk2agent/telegram-inbox"


@dataclass(frozen=True, slots=True)
class WorkspaceInboxFile:
    relative_path: str
    mime_type: str | None


def save_workspace_inbox_file(
    root_dir: str | Path,
    payload: bytes,
    *,
    suggested_name: str | None,
    mime_type: str | None,
    default_stem: str,
) -> WorkspaceInboxFile:
    root = resolve_workspace_path(root_dir)
    inbox_dir = resolve_workspace_path(root, INBOX_RELATIVE_DIR)
    inbox_dir.mkdir(parents=True, exist_ok=True)

    normalized_name = _normalize_file_name(
        suggested_name=suggested_name,
        mime_type=mime_type,
        default_stem=default_stem,
    )
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    file_name = f"{timestamp}-{secrets.token_hex(4)}-{normalized_name}"
    target = inbox_dir / file_name
    target.write_bytes(payload)
    return WorkspaceInboxFile(
        relative_path=target.relative_to(root).as_posix(),
        mime_type=mime_type,
    )


def _normalize_file_name(*, suggested_name: str | None, mime_type: str | None, default_stem: str) -> str:
    candidate = "" if suggested_name is None else Path(suggested_name).name.strip()
    if candidate in {"", ".", ".."}:
        candidate = default_stem

    base = Path(candidate)
    stem = _sanitize_stem(base.stem or default_stem)
    suffix = _sanitize_suffix(base.suffix)
    if not suffix:
        suffix = _sanitize_suffix(_default_extension_for_mime(mime_type))
    return f"{stem}{suffix}"


def _sanitize_stem(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._-")
    return normalized or "attachment"


def _sanitize_suffix(value: str | None) -> str:
    if not value:
        return ""
    normalized = re.sub(r"[^A-Za-z0-9.]+", "", value)
    if not normalized.startswith("."):
        normalized = f".{normalized}"
    return normalized[:16]


def _default_extension_for_mime(mime_type: str | None) -> str:
    if not mime_type:
        return ""
    overrides = {
        "audio/ogg": ".ogg",
        "image/jpeg": ".jpg",
    }
    if mime_type in overrides:
        return overrides[mime_type]
    guessed = mimetypes.guess_extension(mime_type, strict=False)
    return "" if guessed is None else guessed
