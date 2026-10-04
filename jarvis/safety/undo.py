"""Undo journal: every file change Jarvis makes is reversible.

- write  → previous content is backed up (or the file is marked as newly created)
- move   → source/destination recorded
- delete → file is moved into Jarvis's trash, never hard-deleted
"""

from __future__ import annotations

import json
import shutil
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

from .. import config


@dataclass
class Entry:
    id: str
    op: str  # write | move | delete | mkdir
    path: str
    dest: str = ""
    backup: str = ""  # path of backup copy / trashed file ("" = file was newly created)
    ts: float = 0.0
    undone: bool = False

    def describe(self) -> str:
        return {
            "write": f"wrote {self.path}",
            "move": f"moved {self.path} → {self.dest}",
            "delete": f"deleted {self.path}",
            "mkdir": f"created folder {self.path}",
        }.get(self.op, self.op)


class Journal:
    def __init__(self, root: Path | None = None) -> None:
        self.root = root or (config.DATA_DIR / "undo")
        self.trash = self.root / "trash"
        self.file = self.root / "journal.jsonl"
        self._lock = threading.Lock()

    def _ensure(self) -> None:
        self.trash.mkdir(parents=True, exist_ok=True)

    def _append(self, e: Entry) -> Entry:
        self._ensure()
        with self._lock, self.file.open("a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(e)) + "\n")
        return e

    def entries(self) -> list[Entry]:
        if not self.file.exists():
            return []
        out = []
        for line in self.file.read_text("utf-8").splitlines():
            try:
                out.append(Entry(**json.loads(line)))
            except Exception:
                continue
        return out

    def _rewrite(self, entries: list[Entry]) -> None:
        with self._lock:
            self.file.write_text("".join(json.dumps(asdict(e)) + "\n" for e in entries), "utf-8")

    # ------------------------------------------------------------ record
    def before_write(self, path: Path) -> Entry:
        self._ensure()
        eid = uuid.uuid4().hex[:10]
        backup = ""
        if path.exists() and path.is_file():
            backup = str(self.trash / f"{eid}__{path.name}")
            shutil.copy2(path, backup)
        return self._append(Entry(eid, "write", str(path), backup=backup, ts=time.time()))

    def record_move(self, src: Path, dst: Path) -> Entry:
        return self._append(Entry(uuid.uuid4().hex[:10], "move", str(src), dest=str(dst), ts=time.time()))

    def delete(self, path: Path) -> Entry:
        """Move a file/folder into the trash (soft delete)."""
        self._ensure()
        eid = uuid.uuid4().hex[:10]
        target = self.trash / f"{eid}__{path.name}"
        shutil.move(str(path), str(target))
        return self._append(Entry(eid, "delete", str(path), backup=str(target), ts=time.time()))

    def record_mkdir(self, path: Path) -> Entry:
        return self._append(Entry(uuid.uuid4().hex[:10], "mkdir", str(path), ts=time.time()))

    # ------------------------------------------------------------ undo
    def undo_last(self, n: int = 1) -> list[str]:
        entries = self.entries()
        done: list[str] = []
        for e in reversed(entries):
            if len(done) >= n:
                break
            if e.undone:
                continue
            self._undo(e)
            e.undone = True
            done.append(e.describe())
        self._rewrite(entries)
        return done

    def _undo(self, e: Entry) -> None:
        p = Path(e.path)
        if e.op == "write":
            if e.backup:
                shutil.copy2(e.backup, p)
            elif p.exists():
                p.unlink()
        elif e.op == "move":
            d = Path(e.dest)
            if d.exists():
                p.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(d), str(p))
        elif e.op == "delete":
            p.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(e.backup, str(p))
        elif e.op == "mkdir":
            if p.exists() and p.is_dir() and not any(p.iterdir()):
                p.rmdir()

    def recent(self, n: int = 10) -> list[dict]:
        return [
            {"id": e.id, "what": e.describe(), "ts": e.ts, "undone": e.undone}
            for e in self.entries()[-n:]
        ][::-1]


journal = Journal()
