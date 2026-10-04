import asyncio
from pathlib import Path

from jarvis.hands import load_builtin_tools, registry
from jarvis.safety.undo import Journal

load_builtin_tools()


def run(name, **args):
    return asyncio.run(registry.run(name, args))


def test_write_move_delete_are_undoable(tmp_path, monkeypatch):
    import jarvis.hands.files as files

    j = Journal(tmp_path / "undo")
    monkeypatch.setattr(files, "journal", j)
    f = tmp_path / "note.txt"

    assert run("write_file", path=str(f), content="v1").ok
    assert run("write_file", path=str(f), content="v2").ok
    assert f.read_text() == "v2"
    j.undo_last()
    assert f.read_text() == "v1"

    dest = tmp_path / "sub"
    dest.mkdir()
    assert run("move_file", path=str(f), destination=str(dest)).ok
    assert (dest / "note.txt").exists() and not f.exists()
    j.undo_last()
    assert f.exists() and not (dest / "note.txt").exists()

    assert run("delete_file", path=str(f)).ok
    assert not f.exists()
    j.undo_last()
    assert f.read_text() == "v1"

    j.undo_last()  # undo the very first write → file didn't exist before
    j.undo_last()
    assert not f.exists()


def test_refuses_to_delete_top_level():
    r = run("delete_file", path=str(Path.home()))
    assert not r.ok


def test_read_and_list(tmp_path):
    (tmp_path / "a.txt").write_text("hello world")
    r = run("read_file", path=str(tmp_path / "a.txt"))
    assert r.ok and r.output == "hello world"
    r = run("list_folder", path=str(tmp_path))
    assert r.ok and r.output["count"] == 1
    assert not run("read_file", path=str(tmp_path / "missing.txt")).ok
