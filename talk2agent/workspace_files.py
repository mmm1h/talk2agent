from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re


@dataclass(frozen=True, slots=True)
class WorkspaceEntry:
    name: str
    relative_path: str
    is_dir: bool


@dataclass(frozen=True, slots=True)
class WorkspaceListing:
    relative_path: str
    entries: tuple[WorkspaceEntry, ...]


@dataclass(frozen=True, slots=True)
class WorkspaceFilePreview:
    relative_path: str
    text: str
    truncated: bool
    is_binary: bool


@dataclass(frozen=True, slots=True)
class WorkspaceSearchMatch:
    relative_path: str
    line_number: int
    line_text: str


@dataclass(frozen=True, slots=True)
class WorkspaceSearchResults:
    query: str
    matches: tuple[WorkspaceSearchMatch, ...]
    truncated: bool


def resolve_workspace_path(root_dir: str | Path, relative_path: str = "") -> Path:
    root = Path(root_dir).resolve()
    candidate = (root / relative_path).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError("path escapes workspace root") from exc
    return candidate


def list_workspace_entries(root_dir: str | Path, relative_path: str = "") -> WorkspaceListing:
    target = resolve_workspace_path(root_dir, relative_path)
    if not target.exists():
        raise FileNotFoundError(target)
    if not target.is_dir():
        raise NotADirectoryError(target)

    entries = sorted(
        (
            WorkspaceEntry(
                name=child.name,
                relative_path=child.relative_to(Path(root_dir).resolve()).as_posix(),
                is_dir=child.is_dir(),
            )
            for child in target.iterdir()
        ),
        key=lambda entry: (not entry.is_dir, entry.name.casefold()),
    )
    normalized_relative = target.relative_to(Path(root_dir).resolve()).as_posix()
    return WorkspaceListing(
        relative_path="" if normalized_relative == "." else normalized_relative,
        entries=tuple(entries),
    )


def read_workspace_file_preview(
    root_dir: str | Path,
    relative_path: str,
    *,
    max_chars: int = 2800,
    max_lines: int = 120,
) -> WorkspaceFilePreview:
    if max_chars <= 0:
        raise ValueError("max_chars must be positive")
    if max_lines <= 0:
        raise ValueError("max_lines must be positive")

    target = resolve_workspace_path(root_dir, relative_path)
    if not target.exists():
        raise FileNotFoundError(target)
    if target.is_dir():
        raise IsADirectoryError(target)

    raw = target.read_bytes()
    normalized_relative = target.relative_to(Path(root_dir).resolve()).as_posix()
    if b"\x00" in raw[:1024]:
        return WorkspaceFilePreview(
            relative_path=normalized_relative,
            text="[binary file not shown]",
            truncated=False,
            is_binary=True,
        )

    text = raw.decode("utf-8", errors="replace")
    lines = text.splitlines()
    truncated = False
    if len(lines) > max_lines:
        text = "\n".join(lines[:max_lines])
        truncated = True
    if len(text) > max_chars:
        text = text[:max_chars]
        truncated = True
    if not text:
        text = "[empty file]"

    return WorkspaceFilePreview(
        relative_path=normalized_relative,
        text=text,
        truncated=truncated,
        is_binary=False,
    )


def search_workspace_text(
    root_dir: str | Path,
    query: str,
    *,
    max_results: int = 20,
    max_files: int = 500,
    max_line_chars: int = 180,
) -> WorkspaceSearchResults:
    normalized_query = query.strip()
    if not normalized_query:
        raise ValueError("query must not be empty")
    if max_results <= 0:
        raise ValueError("max_results must be positive")
    if max_files <= 0:
        raise ValueError("max_files must be positive")
    if max_line_chars <= 0:
        raise ValueError("max_line_chars must be positive")

    root = resolve_workspace_path(root_dir)
    lowered_query = normalized_query.casefold()
    matches: list[WorkspaceSearchMatch] = []
    truncated = False
    scanned_files = 0

    for path in sorted(root.rglob("*"), key=lambda candidate: candidate.relative_to(root).as_posix()):
        if not path.is_file():
            continue
        scanned_files += 1
        if scanned_files > max_files:
            truncated = True
            break

        raw = path.read_bytes()
        if b"\x00" in raw[:1024]:
            continue

        text = raw.decode("utf-8", errors="replace")
        for line_number, line in enumerate(text.splitlines(), start=1):
            if lowered_query not in line.casefold():
                continue
            match_text = line.strip() or "[blank line]"
            if len(match_text) > max_line_chars:
                match_text = f"{match_text[: max_line_chars - 3]}..."
            matches.append(
                WorkspaceSearchMatch(
                    relative_path=path.relative_to(root).as_posix(),
                    line_number=line_number,
                    line_text=_highlight_match(match_text, normalized_query),
                )
            )
            if len(matches) >= max_results:
                truncated = True
                break
        if len(matches) >= max_results:
            break

    return WorkspaceSearchResults(
        query=normalized_query,
        matches=tuple(matches),
        truncated=truncated,
    )


def _highlight_match(line_text: str, query: str) -> str:
    pattern = re.compile(re.escape(query), re.IGNORECASE)
    return pattern.sub(lambda match: f"[{match.group(0)}]", line_text, count=1)
