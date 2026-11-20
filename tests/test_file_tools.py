import pytest

from jarvis.tools import file_tools
from jarvis.tools.file_tools import make_guard, resolve_root
from jarvis.tools.registry import ToolRegistry


def test_guard_allows_inside_and_blocks_escape(tmp_path):
    guard = make_guard([tmp_path])
    assert guard(str(tmp_path / "a.txt"))
    with pytest.raises(PermissionError):
        guard(str(tmp_path / ".." / "secret.txt"))
    with pytest.raises(PermissionError):
        guard("C:/Windows/System32/drivers/etc/hosts")


def test_known_folder_resolves_to_existing_dir():
    assert resolve_root("documents").exists()


def test_file_tool_flow(tmp_path):
    (tmp_path / "Resume_2026.pdf").write_text("x")
    reg = ToolRegistry()
    file_tools.register(reg, [str(tmp_path)])
    ok = lambda t, a: True

    found = reg.execute("search_files", {"query": "resume", "folder": str(tmp_path)}, ok)
    assert "Resume_2026.pdf" in found.text

    moved = reg.execute(
        "move_file",
        {"source": str(tmp_path / "Resume_2026.pdf"), "destination": str(tmp_path / "cv.pdf")},
        ok,
    )
    assert not moved.error and (tmp_path / "cv.pdf").exists()

    outside = reg.execute("list_folder", {"folder": "C:/Windows"}, ok)
    assert outside.error and "outside" in outside.text

    refused = reg.execute("delete_file", {"path": str(tmp_path / "cv.pdf")}, lambda t, a: False)
    assert refused.error and (tmp_path / "cv.pdf").exists()
