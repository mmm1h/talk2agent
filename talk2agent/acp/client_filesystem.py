from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class ClientFileReadResult:
    content: str


def resolve_workspace_target(workspace_dir: str | Path, path: str) -> Path:
    workspace_root = Path(workspace_dir).resolve()
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = workspace_root / candidate
    candidate = candidate.resolve()
    try:
        candidate.relative_to(workspace_root)
    except ValueError as exc:
        raise ValueError("path escapes workspace root") from exc
    return candidate


def read_workspace_text_file(
    workspace_dir: str | Path,
    path: str,
    *,
    line: int | None = None,
    limit: int | None = None,
) -> ClientFileReadResult:
    target = resolve_workspace_target(workspace_dir, path)
    if not target.exists():
        raise FileNotFoundError(target)
    if target.is_dir():
        raise IsADirectoryError(target)

    text = target.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    start_index = max(0, 0 if line is None else line - 1)
    if start_index >= len(lines):
        return ClientFileReadResult(content="")

    selected_lines = lines[start_index:]
    if limit is not None:
        if limit < 0:
            raise ValueError("limit must be non-negative")
        selected_lines = selected_lines[:limit]
    return ClientFileReadResult(content="\n".join(selected_lines))


def write_workspace_text_file(
    workspace_dir: str | Path,
    path: str,
    content: str,
) -> None:
    target = resolve_workspace_target(workspace_dir, path)
    if target.exists() and target.is_dir():
        raise IsADirectoryError(target)

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
