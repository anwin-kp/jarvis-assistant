"""Files in bulk: preview + ask, never overwrite, stay inside allowed folders, checked results."""

import os
import time
import zipfile

import pytest

from jarvis.tools import pc_maintenance
from jarvis.tools.file_tools import make_guard
from jarvis.tools.pc_maintenance import duplicates, free_name, plan_organize, plan_rename
from jarvis.tools.registry import ToolRegistry


@pytest.fixture
def setup(tmp_path):
    root = tmp_path / "Downloads"
    root.mkdir()
    for n in ("photo.JPG", "cv.pdf", "song.mp3", "setup.exe", "half.crdownload", "weird.xyz"):
        (root / n).write_text(n)
    (root / "Documents").mkdir()
    (root / "Documents" / "cv.pdf").write_text("an older cv")  # same name already there
    class Asked(list):
        approve = True

    reg, asked = ToolRegistry(), Asked()
    pc_maintenance.register(reg, make_guard([root]), [root])
    reg.confirm_fn = lambda tool, args: asked.append(args["action"]) or asked.approve
    return reg, root, asked


def test_organize_asks_with_a_summary_and_never_overwrites(setup):
    reg, root, asked = setup
    asked.approve = False
    assert reg.get("organize_folder").func(str(root)).startswith("Not organized") and (root / "cv.pdf").exists()
    assert "5 files" in asked[0]
    asked.approve = True
    out = reg.get("organize_folder").func(str(root))
    assert out.startswith("Organized 5 files")
    assert (root / "Images" / "photo.JPG").exists() and (root / "Installers" / "setup.exe").exists()
    assert (root / "Documents" / "cv (2).pdf").read_text() == "cv.pdf"  # the older one kept as it was
    assert (root / "Documents" / "cv.pdf").read_text() == "an older cv"
    assert (root / "half.crdownload").exists() and (root / "Other" / "weird.xyz").exists()


def test_rename_patterns_and_clashes(tmp_path):
    for i, n in enumerate(["b.jpg", "a.jpg", "notes.txt"]):
        p = tmp_path / n
        p.write_text(n)
        os.utime(p, (time.time() + i, time.time() + i))
    plan = plan_rename(tmp_path, "Goa trip {n:03}", "jpg")
    assert sorted(v.name for v in plan.values()) == ["Goa trip 001.jpg", "Goa trip 002.jpg"]
    with pytest.raises(ValueError):
        plan_rename(tmp_path, "same name")  # every file would get the same name
    with pytest.raises(ValueError):
        plan_rename(tmp_path, "x", "jpg")


def test_rename_runs_after_approval_and_outside_folders_are_refused(setup, tmp_path):
    reg, root, asked = setup
    out = reg.get("bulk_rename").func(str(root), "file {n}", "mp3")
    assert out.startswith("Renamed 1 of 1") and (root / "file 1.mp3").exists()
    assert reg.execute("bulk_rename", {"folder": str(tmp_path), "pattern": "{n}"}, lambda t, a: True).error


def test_zip_and_unzip_and_zip_slip(setup):
    reg, root, _ = setup
    assert "Created photo.zip (1 files" in reg.get("zip_files").func(str(root / "photo.JPG"))
    out = reg.get("unzip_file").func(str(root / "photo.zip"))
    assert "Extracted 1 files" in out and (root / "photo" / "photo.JPG").exists()
    evil = root / "evil.zip"
    with zipfile.ZipFile(evil, "w") as z:
        z.writestr("../../escaped.txt", "x")
    assert reg.get("unzip_file").func(str(evil)).startswith("ERROR") and not (root.parent / "escaped.txt").exists()


def test_duplicates_and_free_names(tmp_path):
    for n in ("a.bin", "b.bin"):
        (tmp_path / n).write_bytes(b"x" * 2000)
    (tmp_path / "c.bin").write_bytes(b"y" * 2000)
    (g,) = duplicates(tmp_path)
    assert sorted(p.name for p in g) == ["a.bin", "b.bin"]
    assert free_name(tmp_path / "a.bin").name == "a (2).bin" and free_name(tmp_path / "new.bin").name == "new.bin"
    assert {p.name for p in plan_organize(tmp_path)} == {"a.bin", "b.bin", "c.bin"}
