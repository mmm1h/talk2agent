from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from talk2agent.workspace_files import read_workspace_file_preview, resolve_workspace_path


@dataclass(frozen=True, slots=True)
class WorkspaceGitStatusEntry:
    status_code: str
    relative_path: str
    display_path: str


@dataclass(frozen=True, slots=True)
class WorkspaceGitStatus:
    is_git_repo: bool
    branch_line: str | None
    entries: tuple[WorkspaceGitStatusEntry, ...]


@dataclass(frozen=True, slots=True)
class WorkspaceGitDiffPreview:
    relative_path: str
    status_code: str
    text: str
    truncated: bool


def read_workspace_git_status(root_dir: str | Path) -> WorkspaceGitStatus:
    root = resolve_workspace_path(root_dir)
    result = _run_git(root, "status", "--short", "--branch", "--untracked-files=all", "--", ".")
    if result.returncode != 0:
        if _looks_like_not_git_repo(result.stderr):
            return WorkspaceGitStatus(
                is_git_repo=False,
                branch_line=None,
                entries=(),
            )
        raise RuntimeError(result.stderr.strip() or "git status failed")

    lines = [line.rstrip("\n") for line in result.stdout.splitlines()]
    branch_line = None
    if lines and lines[0].startswith("## "):
        branch_line = lines[0][3:]
        lines = lines[1:]

    entries: list[WorkspaceGitStatusEntry] = []
    for line in lines:
        if len(line) < 4:
            continue
        status_code = line[:2]
        path_text = line[3:]
        relative_path = path_text.split(" -> ", 1)[-1].replace("\\", "/")
        entries.append(
            WorkspaceGitStatusEntry(
                status_code=status_code,
                relative_path=relative_path,
                display_path=path_text.replace("\\", "/"),
            )
        )

    return WorkspaceGitStatus(
        is_git_repo=True,
        branch_line=branch_line,
        entries=tuple(entries),
    )


def read_workspace_git_diff_preview(
    root_dir: str | Path,
    relative_path: str,
    *,
    status_code: str,
    max_chars: int = 2800,
    max_lines: int = 160,
) -> WorkspaceGitDiffPreview:
    root = resolve_workspace_path(root_dir)
    normalized_path = relative_path.strip().replace("\\", "/")
    if not normalized_path:
        raise ValueError("relative_path must not be empty")

    code = status_code.strip()
    if code == "??":
        preview = read_workspace_file_preview(
            root,
            normalized_path,
            max_chars=max_chars,
            max_lines=max_lines,
        )
        text = f"[untracked file]\n{preview.text}"
        truncated = preview.truncated
        return WorkspaceGitDiffPreview(
            relative_path=normalized_path,
            status_code=status_code,
            text=text,
            truncated=truncated,
        )

    result = _run_git(root, "diff", "--no-ext-diff", "HEAD", "--", normalized_path)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "git diff failed")

    text = result.stdout.strip()
    truncated = False
    if not text:
        text = "[no diff output]"
    lines = text.splitlines()
    if len(lines) > max_lines:
        text = "\n".join(lines[:max_lines])
        truncated = True
    if len(text) > max_chars:
        text = f"{text[: max_chars - 3]}..."
        truncated = True

    return WorkspaceGitDiffPreview(
        relative_path=normalized_path,
        status_code=status_code,
        text=text,
        truncated=truncated,
    )


def _run_git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def _looks_like_not_git_repo(stderr: str) -> bool:
    lowered = stderr.casefold()
    return "not a git repository" in lowered or "不是 git 仓库" in lowered
