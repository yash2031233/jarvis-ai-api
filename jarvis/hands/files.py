"""Files: instant search (background index), read, list, write/move/delete (all undoable)."""

from __future__ import annotations

import fnmatch
import logging
import os
import shutil
import threading
import time
from datetime import datetime
from pathlib import Path

from rapidfuzz import fuzz

from ..safety.undo import journal
from .osutil import open_path, resolve_user_path, user_folders
from .registry import ToolError, tool

log = logging.getLogger(__name__)

SKIP_DIRS = {
    "node_modules", ".git", "__pycache__", ".venv", "venv", "AppData", "Library", ".cache",
    "$RECYCLE.BIN", ".Trash", "site-packages", ".npm", ".gradle", ".m2", ".cargo", ".rustup",
}
MAX_INDEX = 400_000


class FileIndex:
    """Everything-style in-memory index of the user's folders, refreshed in the background."""

    def __init__(self) -> None:
        self.paths: list[tuple[str, str]] = []  # (lowercase name, full path)
        self.built_at = 0.0
        self.building = False

    def build(self) -> None:
        if self.building:
            return
        self.building = True
        t0 = time.time()
        out: list[tuple[str, str]] = []
        try:
            roots = {str(p) for k, p in user_folders().items() if k != "home"}
            roots.add(str(Path.home()))
            seen: set[str] = set()
            for root in sorted(roots, key=len):
                for dirpath, dirnames, filenames in os.walk(root):
                    if dirpath in seen:
                        dirnames[:] = []
                        continue
                    seen.add(dirpath)
                    dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
                    for n in filenames:
                        out.append((n.lower(), os.path.join(dirpath, n)))
                    for d in dirnames:
                        out.append((d.lower() + "/", os.path.join(dirpath, d)))
                    if len(out) > MAX_INDEX:
                        break
        except Exception as e:
            log.warning("file index error: %s", e)
        self.paths = out
        self.built_at = time.time()
        self.building = False
        log.info("file index: %d entries in %.1fs", len(out), time.time() - t0)

    def search(self, query: str, kind: str = "any", limit: int = 20) -> list[str]:
        if not self.paths:
            self.build()
        q = query.lower().strip()
        glob = any(c in q for c in "*?[")
        words = q.split()
        scored: list[tuple[float, str]] = []
        for name, full in self.paths:
            is_dir = name.endswith("/")
            if kind == "file" and is_dir or kind == "folder" and not is_dir:
                continue
            n = name.rstrip("/")
            if glob:
                if fnmatch.fnmatch(n, q):
                    scored.append((100, full))
                continue
            if all(w in n for w in words):
                score = 100 - len(n) * 0.1 + (20 if n.startswith(words[0]) else 0)
                scored.append((score, full))
        if not scored and not glob and len(q) >= 4:
            for name, full in self.paths[:150_000]:
                s = fuzz.partial_ratio(q, name)
                if s >= 88:
                    scored.append((s - 20, full))
        scored.sort(key=lambda x: -x[0])
        # prefer recently modified among equals
        return [p for _, p in scored[:limit]]


index = FileIndex()


def warm() -> None:
    def loop():
        while True:
            index.build()
            time.sleep(900)

    threading.Thread(target=loop, daemon=True).start()


def _info(p: Path) -> dict:
    st = p.stat()
    return {
        "path": str(p),
        "type": "folder" if p.is_dir() else "file",
        "size_kb": round(st.st_size / 1024, 1) if p.is_file() else None,
        "modified": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M"),
    }


@tool(risk="low", tags=["find", "search", "file", "folder", "where", "locate"],
      examples=["search_files(query='resume pdf')", "search_files(query='*.mp4', kind='file')"])
def search_files(query: str, kind: str = "any", limit: int = 15) -> list[dict]:
    """Instantly search the user's files and folders by name (words, fuzzy or *.glob). kind: any|file|folder."""
    hits = index.search(query, kind, limit)
    if not hits:
        raise ToolError(f"No files matching '{query}'.", hint="Try fewer/different words or a glob like '*.pdf'.")
    out = []
    for h in hits:
        try:
            out.append(_info(Path(h)))
        except OSError:
            continue
    return out


@tool(risk="low", tags=["list", "folder", "directory", "contents", "ls", "dir"])
def list_folder(path: str = "~", limit: int = 100) -> dict:
    """List the contents of a folder (accepts names like 'downloads' or 'desktop')."""
    p = resolve_user_path(path)
    if not p.is_dir():
        raise ToolError(f"Not a folder: {p}", hint="Use search_files to find the right folder.")
    items = sorted(p.iterdir(), key=lambda x: (not x.is_dir(), -x.stat().st_mtime))
    rows = []
    for it in items[:limit]:
        try:
            rows.append(_info(it))
        except OSError:
            continue
    return {"folder": str(p), "count": len(items), "items": rows}


TEXT_EXT = {".txt", ".md", ".py", ".js", ".ts", ".json", ".csv", ".log", ".html", ".css", ".xml", ".yaml",
            ".yml", ".ini", ".toml", ".cfg", ".java", ".c", ".cpp", ".h", ".rs", ".go", ".sh", ".ps1", ".bat",
            ".sql", ".tsx", ".jsx", ".env.example", ".rtf"}


@tool(risk="low", tags=["read", "open", "file", "contents", "show", "summarize"])
def read_file(path: str, max_chars: int = 12000) -> str:
    """Read a text file (or extract text from PDF/DOCX when libraries are available)."""
    p = resolve_user_path(path)
    if not p.is_file():
        raise ToolError(f"File not found: {p}", hint="Use search_files to locate it first.")
    ext = p.suffix.lower()
    if ext == ".pdf":
        try:
            from pypdf import PdfReader  # optional

            text = "\n".join((pg.extract_text() or "") for pg in PdfReader(str(p)).pages[:50])
        except ImportError:
            raise ToolError("PDF reading needs the 'pypdf' package.", hint="pip install pypdf")
    elif ext == ".docx":
        try:
            import docx  # optional

            text = "\n".join(par.text for par in docx.Document(str(p)).paragraphs)
        except ImportError:
            raise ToolError("DOCX reading needs 'python-docx'.", hint="pip install python-docx")
    else:
        raw = p.read_bytes()[: max_chars * 4]
        if b"\x00" in raw[:2000] and ext not in TEXT_EXT:
            raise ToolError("That's a binary file.", hint="Use open_file to open it in its default app.")
        text = raw.decode("utf-8", errors="replace")
    if len(text) > max_chars:
        text = text[:max_chars] + f"\n…[truncated, {len(text)} chars total]"
    return text


@tool(risk="low", tags=["open", "file", "folder", "show", "launch"])
def open_file(path: str) -> str:
    """Open a file or folder with its default application."""
    p = resolve_user_path(path)
    if not p.exists():
        hits = index.search(path, limit=1)
        if not hits:
            raise ToolError(f"Not found: {path}", hint="Use search_files first.")
        p = Path(hits[0])
    open_path(str(p))
    return f"Opened {p}"


def _verify_exists(args: dict, _out) -> bool:
    return resolve_user_path(args.get("path") or args.get("destination", "")).exists()


@tool(risk="medium", tags=["write", "create", "save", "file", "edit", "append"], undo=True,
      verify=_verify_exists)
def write_file(path: str, content: str, append: bool = False) -> str:
    """Create or overwrite (or append to) a text file. Undoable."""
    p = resolve_user_path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    journal.before_write(p)
    with p.open("a" if append else "w", encoding="utf-8") as f:
        f.write(content)
    return f"{'Appended to' if append else 'Wrote'} {p} ({len(content)} chars)"


@tool(risk="medium", tags=["move", "rename", "file", "folder", "organize"], undo=True)
def move_file(path: str, destination: str) -> str:
    """Move or rename a file/folder. If destination is a folder, the item goes inside it. Undoable."""
    src = resolve_user_path(path)
    if not src.exists():
        raise ToolError(f"Not found: {src}", hint="Use search_files to find the exact path.")
    dst = resolve_user_path(destination)
    if dst.is_dir():
        dst = dst / src.name
    if dst.exists():
        raise ToolError(f"Destination already exists: {dst}", hint="Choose a different name.")
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))
    journal.record_move(src, dst)
    return f"Moved {src} → {dst}"


@tool(risk="medium", tags=["create", "folder", "directory", "mkdir", "new"], undo=True)
def create_folder(path: str) -> str:
    """Create a folder (and parents). Undoable."""
    p = resolve_user_path(path)
    if p.exists():
        return f"Already exists: {p}"
    p.mkdir(parents=True)
    journal.record_mkdir(p)
    return f"Created {p}"


@tool(risk="high", tags=["delete", "remove", "trash", "file", "folder"], undo=True)
def delete_file(path: str) -> str:
    """Delete a file or folder (moved to Jarvis trash, restorable with undo)."""
    p = resolve_user_path(path)
    if not p.exists():
        raise ToolError(f"Not found: {p}")
    home = Path.home().resolve()
    if p.resolve() in (home, Path(p.anchor)) or len(p.resolve().parts) <= 2:
        raise ToolError("Refusing to delete a top-level folder.")
    journal.delete(p)
    return f"Deleted {p} (restorable with undo)"


@tool(risk="low", tags=["undo", "revert", "restore", "take back"])
def undo_last_action(count: int = 1) -> str:
    """Undo the last file change(s) Jarvis made (write, move, delete, create folder)."""
    done = journal.undo_last(count)
    if not done:
        raise ToolError("Nothing to undo.")
    return "Undid: " + "; ".join(done)
