from __future__ import annotations

from pathlib import Path
import subprocess

import pytest

from talk2agent.harness import HarnessError, check_doc_contract, run_cli_smoke_checks


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content.strip() + "\n", encoding="utf-8")


def _create_minimal_doc_tree(root: Path) -> None:
    _write(
        root / "AGENTS.md",
        """
        # AGENTS.md
        1. 先读 [ARCHITECTURE.md](ARCHITECTURE.md)
        2. 再读 [docs/index.md](docs/index.md)
        """,
    )
    _write(
        root / "ARCHITECTURE.md",
        """
        # ARCHITECTURE.md
        顶层地图。
        """,
    )
    _write(
        root / "README.md",
        """
        # README
        - [docs/operator-guide.md](docs/operator-guide.md)
        - [docs/manual-checklist.md](docs/manual-checklist.md)
        - [docs/harness.md](docs/harness.md)
        """,
    )
    _write(
        root / "docs/index.md",
        """
        # docs
        - [operator-guide.md](operator-guide.md)
        - [manual-checklist.md](manual-checklist.md)
        - [harness.md](harness.md)
        - [design-docs/index.md](design-docs/index.md)
        - [exec-plans/index.md](exec-plans/index.md)
        """,
    )
    _write(root / "docs/design-docs/index.md", "# design docs\n")
    _write(root / "docs/operator-guide.md", "# operator guide\n")
    _write(root / "docs/manual-checklist.md", "# checklist\n")
    _write(root / "docs/harness.md", "# harness\n")
    _write(root / "docs/exec-plans/index.md", "# exec plans\n")


def test_check_doc_contract_accepts_progressive_disclosure_docs(tmp_path: Path):
    _create_minimal_doc_tree(tmp_path)

    check_doc_contract(tmp_path)


def test_check_doc_contract_rejects_agents_budget_regression(tmp_path: Path):
    _create_minimal_doc_tree(tmp_path)
    oversized = "\n".join(f"line {index}" for index in range(101))
    _write(tmp_path / "AGENTS.md", oversized)

    with pytest.raises(HarnessError, match="AGENTS.md is 101 lines"):
        check_doc_contract(tmp_path)


def test_check_doc_contract_rejects_architecture_ui_detail(tmp_path: Path):
    _create_minimal_doc_tree(tmp_path)
    _write(tmp_path / "ARCHITECTURE.md", "# ARCHITECTURE\n`Bot Status`\n")

    with pytest.raises(HarnessError, match="ARCHITECTURE.md contains out-of-bound detail"):
        check_doc_contract(tmp_path)


def test_run_cli_smoke_checks_runs_help_and_init(tmp_path: Path, monkeypatch):
    commands: list[list[str]] = []

    def fake_run(command: list[str], cwd: str, check: bool) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        if command[2:4] == ["talk2agent", "init"]:
            Path(command[-1]).write_text("telegram: {}\n", encoding="utf-8")
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)

    run_cli_smoke_checks(tmp_path)

    assert commands[0][2:] == ["talk2agent", "--help"]
    assert commands[1][2:4] == ["talk2agent", "init"]
