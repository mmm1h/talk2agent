from types import SimpleNamespace

from talk2agent.workspace_git import read_workspace_git_diff_preview, read_workspace_git_status


def test_read_workspace_git_status_parses_branch_and_entries(tmp_path, monkeypatch):
    def fake_run_git(root, *args):
        assert str(root) == str(tmp_path.resolve())
        assert args[:2] == ("status", "--short")
        return SimpleNamespace(
            returncode=0,
            stdout="## main...origin/main [ahead 1]\n M src/app.py\n?? notes.txt\n",
            stderr="",
        )

    monkeypatch.setattr("talk2agent.workspace_git._run_git", fake_run_git)

    status = read_workspace_git_status(tmp_path)

    assert status.is_git_repo is True
    assert status.branch_line == "main...origin/main [ahead 1]"
    assert [(entry.status_code, entry.relative_path) for entry in status.entries] == [
        (" M", "src/app.py"),
        ("??", "notes.txt"),
    ]


def test_read_workspace_git_status_handles_non_repo(tmp_path, monkeypatch):
    def fake_run_git(root, *args):
        return SimpleNamespace(
            returncode=128,
            stdout="",
            stderr="fatal: not a git repository",
        )

    monkeypatch.setattr("talk2agent.workspace_git._run_git", fake_run_git)

    status = read_workspace_git_status(tmp_path)

    assert status.is_git_repo is False
    assert status.entries == ()


def test_read_workspace_git_diff_preview_uses_untracked_file_preview(tmp_path):
    path = tmp_path / "notes.txt"
    path.write_text("hello\nworld\n", encoding="utf-8")

    preview = read_workspace_git_diff_preview(tmp_path, "notes.txt", status_code="??")

    assert preview.relative_path == "notes.txt"
    assert preview.text.startswith("[untracked file]\nhello")
    assert preview.truncated is False


def test_read_workspace_git_diff_preview_truncates_git_diff(tmp_path, monkeypatch):
    def fake_run_git(root, *args):
        return SimpleNamespace(
            returncode=0,
            stdout="line1\nline2\nline3\nline4\n",
            stderr="",
        )

    monkeypatch.setattr("talk2agent.workspace_git._run_git", fake_run_git)

    preview = read_workspace_git_diff_preview(
        tmp_path,
        "src/app.py",
        status_code=" M",
        max_chars=20,
        max_lines=2,
    )

    assert preview.relative_path == "src/app.py"
    assert preview.truncated is True
    assert preview.text == "line1\nline2"
