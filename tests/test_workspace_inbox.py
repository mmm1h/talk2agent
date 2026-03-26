from pathlib import Path

from talk2agent.workspace_inbox import INBOX_RELATIVE_DIR, save_workspace_inbox_file


def test_save_workspace_inbox_file_writes_inside_hidden_inbox(tmp_path: Path):
    result = save_workspace_inbox_file(
        tmp_path,
        b"hello",
        suggested_name="notes.md",
        mime_type="text/markdown",
        default_stem="telegram-document",
    )

    saved_path = tmp_path / Path(result.relative_path)
    assert result.relative_path.startswith(f"{INBOX_RELATIVE_DIR}/")
    assert saved_path.read_bytes() == b"hello"


def test_save_workspace_inbox_file_sanitizes_name_and_adds_extension_from_mime(tmp_path: Path):
    result = save_workspace_inbox_file(
        tmp_path,
        b"image",
        suggested_name="../../weird name",
        mime_type="image/jpeg",
        default_stem="telegram-image",
    )

    saved_path = tmp_path / Path(result.relative_path)
    assert saved_path.suffix == ".jpg"
    assert "weird_name" in saved_path.name
    assert saved_path.read_bytes() == b"image"
