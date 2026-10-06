"""The brain vault: long-term memory as a folder of small linked Markdown notes (Obsidian-compatible).

One note per thing that matters (a person, a class, a project, a device, a place, preferences...), facts as
dated bullet lines, notes linked with [[Note Title]]. Plain files, so the vault opens in Obsidian and can be
edited by hand. Point `vault_path` in settings at an existing Obsidian vault to share it.
"""

from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from typing import Any

from rapidfuzz import fuzz, process

from .. import config

_lock = threading.RLock()
ABOUT_ME = "About me"
LOG_NOTE = "Memory log"
_FACT = re.compile(r"^\s*[-*]\s+(.*\S)\s*$")
_LINK = re.compile(r"\[\[([^\]|#]+)(?:[|#][^\]]*)?\]\]")
_BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def root() -> Path:
    p = config.store.load().vault_path
    d = Path(p).expanduser() if p else config.DATA_DIR / "vault"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _title(name: str) -> str:
    t = _BAD.sub("", name).strip().strip(".")
    return t[:80] or "Inbox"


def _path(title: str) -> Path:
    return root() / f"{_title(title)}.md"


SYSTEM_NOTES = {LOG_NOTE, "Jarvis principles"}
# where the memory keeper got to in THIS Jarvis's conversations. Its own name: other apps sharing the vault (Jarvis
# v1 used ".jarvis-keeper.json" with its own message numbers) made this one think it had already read everything.
KEEPER_FILE = ".jarvis-v2-keeper.json"


def notes() -> list[str]:
    return sorted(p.stem for p in root().glob("*.md"))


def user_notes() -> list[str]:
    """Notes about the user's world - not Jarvis's own bookkeeping (Memory log, principles)."""
    return [n for n in notes() if n not in SYSTEM_NOTES]


def find_note(name: str, cutoff: int = 82) -> str | None:
    """Existing note by (fuzzy) title."""
    if not name:
        return None
    t = _title(name)
    all_ = notes()
    for n in all_:
        if n.lower() == t.lower():
            return n
    hit = process.extractOne(t, all_, scorer=fuzz.WRatio) if all_ else None
    return hit[0] if hit and hit[1] >= cutoff else None


def read(name: str) -> str:
    n = find_note(name)
    if not n:
        raise LookupError(f"No note called '{name}'.")
    return _path(n).read_text("utf-8")


def facts(name: str) -> list[str]:
    try:
        return [m.group(1) for ln in read(name).splitlines() if (m := _FACT.match(ln))]
    except LookupError:
        return []


def _today() -> str:
    return time.strftime("%Y-%m-%d")


def add(note: str, fact: str, source: str = "") -> dict[str, Any]:
    """Add a fact to a note (created if new). Near-duplicates are skipped."""
    fact = " ".join(fact.split()).strip().rstrip(".")
    if not fact:
        raise ValueError("empty fact")
    with _lock:
        n = find_note(note) or _title(note)
        p = _path(n)
        existing = facts(n)
        for e in existing:
            if fuzz.token_set_ratio(_strip_date(e).lower(), fact.lower()) >= 90:
                return {"note": n, "added": False, "duplicate_of": e}
        if not p.exists():
            p.write_text(f"# {n}\n\n", "utf-8")
        text = p.read_text("utf-8")
        if not text.endswith("\n"):
            text += "\n"
        text += f"- {fact} ({_today()})\n"
        p.write_text(text, "utf-8")
        _log(f"+ [[{n}]]: {fact}" + (f"  _({source})_" if source else ""))
        return {"note": n, "added": True}


def replace(note: str, old: str, new: str, source: str = "") -> dict[str, Any]:
    with _lock:
        n = find_note(note)
        if not n:
            return add(note, new, source)
        p = _path(n)
        lines = p.read_text("utf-8").splitlines()
        best, best_i = 0, -1
        for i, ln in enumerate(lines):
            m = _FACT.match(ln)
            if m:
                s = fuzz.partial_ratio(old.lower(), m.group(1).lower())
                if s > best:
                    best, best_i = s, i
        if best < 75:
            return add(n, new, source)
        was = lines[best_i]
        lines[best_i] = f"- {' '.join(new.split()).rstrip('.')} ({_today()})"
        p.write_text("\n".join(lines) + "\n", "utf-8")
        _log(f"~ [[{n}]]: {_FACT.match(was).group(1)} -> {new}" + (f"  _({source})_" if source else ""))
        return {"note": n, "replaced": _FACT.match(was).group(1)}


def remove(note: str, old: str) -> dict[str, Any]:
    with _lock:
        n = find_note(note)
        if not n:
            raise LookupError(f"No note called '{note}'.")
        p = _path(n)
        lines = p.read_text("utf-8").splitlines()
        for i, ln in enumerate(lines):
            m = _FACT.match(ln)
            if m and fuzz.partial_ratio(old.lower(), m.group(1).lower()) >= 80:
                del lines[i]
                p.write_text("\n".join(lines) + "\n", "utf-8")
                _log(f"- [[{n}]]: {m.group(1)}")
                return {"note": n, "removed": m.group(1)}
        raise LookupError(f"Nothing like '{old}' in {n}.")


def link(note: str, other: str) -> dict[str, Any]:
    with _lock:
        n = find_note(note) or _title(note)
        o = find_note(other) or _title(other)
        p = _path(n)
        if not p.exists():
            p.write_text(f"# {n}\n\n", "utf-8")
        text = p.read_text("utf-8")
        if f"[[{o}]]" in text:
            return {"note": n, "linked": o, "already": True}
        if "\nRelated:" in text:
            text = re.sub(r"(\nRelated:[^\n]*)", lambda m: m.group(1) + f" [[{o}]]", text, count=1)
        else:
            text = text.rstrip("\n") + f"\n\nRelated: [[{o}]]\n"
        p.write_text(text, "utf-8")
        return {"note": n, "linked": o}


def recall(query: str, limit: int = 12) -> list[dict[str, Any]]:
    """Facts across the vault that match the query (words + fuzzy), best first."""
    q = query.lower()
    words = [w for w in re.findall(r"[a-z0-9]+", q) if len(w) > 2]
    rows = []
    for n in user_notes():
        title_score = fuzz.WRatio(q, n.lower())
        try:
            text = _path(n).read_text("utf-8")
        except OSError:
            continue
        for ln in text.splitlines():
            m = _FACT.match(ln)
            if not m:
                continue
            f = m.group(1)
            fl = f.lower()
            overlap = sum(1 for w in words if w in fl)
            s = overlap * 25 + fuzz.partial_ratio(q, fl) * 0.5 + (title_score * 0.4 if title_score > 70 else 0)
            if s >= 45:
                rows.append({"note": n, "fact": f, "score": round(s, 1)})
    rows.sort(key=lambda r: -r["score"])
    return rows[:limit]


def summary() -> list[dict[str, Any]]:
    out = []
    for n in notes():
        p = _path(n)
        text = p.read_text("utf-8")
        out.append({"note": n, "facts": sum(1 for ln in text.splitlines() if _FACT.match(ln)),
                    "links": sorted(set(_LINK.findall(text))),
                    "updated": time.strftime("%Y-%m-%d", time.localtime(p.stat().st_mtime))})
    return out


_cache: dict[str, tuple[float, str]] = {}


def _text(title: str) -> str:
    """A note's text (re-read only when the file changed)."""
    p = _path(title)
    try:
        m = p.stat().st_mtime
    except OSError:
        return ""
    hit = _cache.get(str(p))
    if hit and hit[0] == m:
        return hit[1]
    try:
        t = p.read_text("utf-8")
    except OSError:
        return ""
    _cache[str(p)] = (m, t)
    return t


_FRONT = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.S)


def _aliases(text: str) -> list[str]:
    """Obsidian `aliases:` from a note's frontmatter (list or inline)."""
    m = _FRONT.match(text)
    if not m:
        return []
    fm = m.group(1)
    inline = re.search(r"(?m)^aliases:\s*\[(.*?)\]", fm)
    if inline:
        return [a.strip(" '\"") for a in inline.group(1).split(",") if a.strip()]
    block = re.search(r"(?ms)^aliases:\s*\n((?:\s+-\s*.+\n?)+)", fm)
    return [a.strip(" -'\"") for a in block.group(1).splitlines() if a.strip()] if block else []


def _body(text: str, limit: int) -> str:
    """A note without its frontmatter, blank lines squeezed, cut to `limit` characters."""
    t = re.sub(r"\n{3,}", "\n\n", _FRONT.sub("", text, count=1)).strip()
    return t if len(t) <= limit else t[:limit].rsplit("\n", 1)[0] + "\n…"


def profile_notes() -> list[str]:
    """The notes about the user themself, for every request: 'About me', the note named after them (or with the alias
    'me' / 'the user'), and their preferences / how they talk / memory rules."""
    name = (config.store.load().user_name or "").strip()
    want = [ABOUT_ME, name, "Preferences", f"How {name} talks" if name else "", "Memory rules"]
    have = {n.lower(): n for n in user_notes()}
    out = [have[w.lower()] for w in want if w and w.lower() in have]
    for n in user_notes():
        if n not in out and "the user" in {a.lower() for a in _aliases(_text(n))}:
            out.append(n)
    return out[:6]


def for_prompt(max_lines: int = 25, max_chars: int = 3500) -> str:
    """What goes into every system prompt: the user's profile notes (who they are, how they like things done)."""
    parts, used = [], 0
    for n in profile_notes():
        body = _body(_text(n), 1400)
        if not body or used + len(body) > max_chars:
            continue
        parts.append(f"[{n}]\n{body}")
        used += len(body)
    if not parts:
        fs = facts(ABOUT_ME)
        return "\n".join(f"- {_strip_date(f)}" for f in fs[-max_lines:])
    return "\n\n".join(parts)


def relevant(text: str, limit: int = 3, max_chars: int = 2400) -> str:
    """The notes this request is about (by title, alias or matching facts) - so 'how's the robot car wired?' brings in
    the Robot car note without having to search for it."""
    q = (text or "").lower()
    if len(q) < 4:
        return ""
    skip = set(profile_notes()) | SYSTEM_NOTES
    words = {w for w in re.findall(r"[a-z0-9]+", q) if len(w) > 2}
    scored = []
    for n in user_notes():
        if n in skip:
            continue
        t = _text(n)
        names = [n] + _aliases(t)
        s = 0.0
        for nm in names:
            nl = nm.lower().strip()
            if len(nl) < 3:
                continue
            if re.search(rf"\b{re.escape(nl)}\b", q):
                s = max(s, 100 + len(nl))                       # named in the request ("robot car", "3D printer")
            else:
                nw = {w for w in re.findall(r"[a-z0-9]+", nl) if len(w) > 2}
                if nw and len(nw & words) == len(nw):
                    s = max(s, 80)
                elif fuzz.partial_ratio(nl, q) >= 92 and len(nl) > 4:
                    s = max(s, 70)
        if s:
            scored.append((s, n, t))
    scored.sort(key=lambda r: -r[0])
    parts, used = [], 0
    for _s, n, t in scored[:limit]:
        body = _body(t, 1200)
        if used + len(body) > max_chars:
            break
        parts.append(f"[{n}]\n{body}")
        used += len(body)
    return "\n\n".join(parts)


def _strip_date(f: str) -> str:
    return re.sub(r"\s*\(\d{4}-\d{2}-\d{2}\)$", "", f)


def _log(line: str) -> None:
    p = _path(LOG_NOTE)
    if not p.exists():
        p.write_text(f"# {LOG_NOTE}\n\nEvery change Jarvis makes to his memory.\n\n", "utf-8")
    with p.open("a", encoding="utf-8") as f:
        f.write(f"- {time.strftime('%Y-%m-%d %H:%M')} {line}\n")


# ------------------------------------------------------------------ memory keeper state
def keeper_state() -> dict[str, Any]:
    try:
        return json.loads((root() / KEEPER_FILE).read_text("utf-8"))
    except Exception:
        return {}


def save_keeper_state(**kw: Any) -> None:
    s = keeper_state()
    s.update(kw)
    (root() / KEEPER_FILE).write_text(json.dumps(s), "utf-8")
