from pathlib import Path

import pytest

from talk2agent.workspace_files import (
    list_workspace_entries,
    read_workspace_file_preview,
    resolve_workspace_path,
    search_workspace_text,
)


def test_resolve_workspace_path_rejects_escape(tmp_path: Path):
    with pytest.raises(ValueError, match="workspace root"):
        resolve_workspace_path(tmp_path, "../outside.txt")


def test_list_workspace_entries_sorts_directories_before_files(tmp_path: Path):
    (tmp_path / "b.txt").write_text("b", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "a.txt").write_text("a", encoding="utf-8")
    (tmp_path / "docs").mkdir()

    listing = list_workspace_entries(tmp_path)

    assert [entry.relative_path for entry in listing.entries] == [
        "docs",
        "src",
        "a.txt",
        "b.txt",
    ]


def test_read_workspace_file_preview_truncates_large_text(tmp_path: Path):
    path = tmp_path / "notes.txt"
    path.write_text("\n".join(f"line {index}" for index in range(20)), encoding="utf-8")

    preview = read_workspace_file_preview(tmp_path, "notes.txt", max_chars=40, max_lines=3)

    assert preview.relative_path == "notes.txt"
    assert preview.is_binary is False
    assert preview.truncated is True
    assert preview.text == "line 0\nline 1\nline 2"


def test_read_workspace_file_preview_marks_binary_files(tmp_path: Path):
    path = tmp_path / "data.bin"
    path.write_bytes(b"\x00\x01\x02")

    preview = read_workspace_file_preview(tmp_path, "data.bin")

    assert preview.is_binary is True
    assert preview.text == "[binary file not shown]"


def test_search_workspace_text_finds_matches_with_line_numbers(tmp_path: Path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("alpha\nBeta value\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("beta section\n", encoding="utf-8")

    results = search_workspace_text(tmp_path, "beta", max_results=10)

    assert [(match.relative_path, match.line_number) for match in results.matches] == [
        ("README.md", 1),
        ("src/app.py", 2),
    ]
    assert results.matches[0].line_text == "[beta] section"


def test_search_workspace_text_skips_binary_files_and_truncates(tmp_path: Path):
    (tmp_path / "a.txt").write_text("needle one\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("needle two\n", encoding="utf-8")
    (tmp_path / "data.bin").write_bytes(b"\x00needle")

    results = search_workspace_text(tmp_path, "needle", max_results=1)

    assert len(results.matches) == 1
    assert results.matches[0].relative_path == "a.txt"
    assert results.truncated is True
