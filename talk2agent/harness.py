from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import subprocess
import sys
import tempfile


@dataclass(frozen=True, slots=True)
class DocBudget:
    path: str
    max_lines: int
    purpose: str


class HarnessError(RuntimeError):
    """Raised when a repository contract check fails."""


DOC_BUDGETS = (
    DocBudget("AGENTS.md", 100, "AGENTS.md must stay a short table of contents."),
    DocBudget("ARCHITECTURE.md", 140, "ARCHITECTURE.md must stay at the system-map level."),
    DocBudget("README.md", 120, "README.md is for human quickstart, not a full manual."),
    DocBudget("docs/index.md", 60, "docs/index.md must stay a narrow entry point."),
    DocBudget(
        "docs/design-docs/index.md",
        60,
        "docs/design-docs/index.md must stay a concise index, not a design dump.",
    ),
)

REQUIRED_LINKS = {
    "AGENTS.md": ("ARCHITECTURE.md", "docs/index.md"),
    "docs/index.md": (
        "operator-guide.md",
        "manual-checklist.md",
        "harness.md",
        "design-docs/index.md",
        "exec-plans/index.md",
    ),
    "README.md": ("docs/operator-guide.md", "docs/manual-checklist.md", "docs/harness.md"),
}

FORBIDDEN_SNIPPETS = {
    "AGENTS.md": ("1. 先读 [README.md]",),
    "ARCHITECTURE.md": (
        "`Bot Status`",
        "`Workspace Files`",
        "`Workspace Search`",
        "`Context Bundle`",
        "`Provider Sessions`",
    ),
}


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise HarnessError(f"missing required file: {path.as_posix()}") from exc


def _run_command(command: list[str], cwd: Path) -> None:
    result = subprocess.run(command, cwd=str(cwd), check=False)
    if result.returncode != 0:
        rendered = " ".join(command)
        raise HarnessError(f"command failed with exit code {result.returncode}: {rendered}")


def check_doc_contract(repo_root: Path | None = None) -> None:
    root = repo_root or Path.cwd()

    for budget in DOC_BUDGETS:
        path = root / budget.path
        line_count = len(_read_text(path).splitlines())
        if line_count > budget.max_lines:
            raise HarnessError(
                f"{budget.path} is {line_count} lines; limit is {budget.max_lines}. {budget.purpose}"
            )

    for relative_path, required_links in REQUIRED_LINKS.items():
        text = _read_text(root / relative_path)
        for link in required_links:
            if link not in text:
                raise HarnessError(f"{relative_path} must link to {link}")

    for relative_path, forbidden_snippets in FORBIDDEN_SNIPPETS.items():
        text = _read_text(root / relative_path)
        for snippet in forbidden_snippets:
            if snippet in text:
                raise HarnessError(
                    f"{relative_path} contains out-of-bound detail: {snippet}"
                )


def run_cli_smoke_checks(repo_root: Path | None = None) -> None:
    root = repo_root or Path.cwd()
    _run_command([sys.executable, "-m", "talk2agent", "--help"], root)

    with tempfile.TemporaryDirectory(dir=root) as temp_dir:
        config_path = Path(temp_dir) / "config.yaml"
        _run_command(
            [sys.executable, "-m", "talk2agent", "init", "--config", str(config_path)],
            root,
        )
        if not config_path.exists():
            raise HarnessError("talk2agent init did not create the expected config file")


def run_harness(repo_root: Path | None = None) -> int:
    root = repo_root or Path.cwd()
    try:
        check_doc_contract(root)
        _run_command([sys.executable, "-m", "pytest", "-q"], root)
        run_cli_smoke_checks(root)
    except HarnessError as exc:
        print(f"Harness failed: {exc}", file=sys.stderr)
        return 1

    print("Harness passed.")
    return 0
