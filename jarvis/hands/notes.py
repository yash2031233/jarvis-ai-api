"""Long-term memory tools, backed by the brain vault (linked Markdown notes, Obsidian-compatible)."""

from __future__ import annotations

from ..memory import vault
from .registry import ToolError, tool

_KIND_NOTE = {"fact": vault.ABOUT_ME, "todo": "To-do", "note": "Inbox", "preference": vault.ABOUT_ME}


@tool(risk="low", tags=["remember", "memory", "vault", "note", "notes", "recall", "know about", "obsidian"],
      examples=["brain(action='add', note='Physics', text='Lab report due Oct 12')",
                "brain(action='recall', query='when is my physics lab due')"])
def brain(action: str, note: str = "", text: str = "", query: str = "", old: str = "") -> dict:
    """Long-term memory: a vault of small linked notes - one per person, class, project, device, place...
    ('About me' = the user's own facts and preferences). add = remember `text` in `note` (created if new);
    recall = find what's known about `query`; read = a whole note; list = all notes; replace = change the line
    matching `old` to `text`; remove = forget the line matching `old`; link = connect `note` to `text` (another
    note). Keep each fact short and specific; one note per thing."""
    a = action.lower().strip()
    try:
        if a == "add":
            if not text.strip():
                raise ToolError("Give the fact in `text`.")
            return vault.add(note or "Inbox", text)
        if a in ("recall", "search", "find"):
            hits = vault.recall(query or text or note)
            return {"matches": hits} if hits else {"matches": [], "info": "nothing remembered about that"}
        if a == "read":
            return {"note": vault.find_note(note), "text": vault.read(note)[:6000]}
        if a == "list":
            return {"notes": vault.summary()}
        if a == "replace":
            return vault.replace(note, old, text)
        if a in ("remove", "forget"):
            return vault.remove(note, old or text)
        if a == "link":
            return vault.link(note, text)
        raise ToolError(f"Unknown action '{action}'.", hint="add, recall, read, list, replace, remove, link")
    except LookupError as e:
        raise ToolError(str(e), hint="Use action=list to see the notes.")


@tool(risk="low", tags=["remember", "note", "save", "don't forget", "memorize", "todo"],
      examples=["remember_note(text='My wifi password is on the fridge', kind='fact')"])
def remember_note(text: str, kind: str = "note") -> str:
    """Quickly save something to remember. kind = fact (about the user) | todo | note.
    (For anything about a specific person/class/project, use brain(action='add', note=...).)"""
    r = vault.add(_KIND_NOTE.get(kind, "Inbox"), text)
    return f"Saved to [[{r['note']}]]." if r["added"] else f"Already remembered in [[{r['note']}]]."


@tool(risk="low", tags=["notes", "todos", "what do you know", "list", "remember", "recall"])
def list_notes(kind: str = "", query: str = "", limit: int = 30) -> dict:
    """What's remembered: kind = fact|todo|note lists that note's lines; query = search everything."""
    if query:
        return {"matches": vault.recall(query, limit)}
    if kind:
        return {"note": _KIND_NOTE.get(kind, "Inbox"), "items": vault.facts(_KIND_NOTE.get(kind, "Inbox"))[-limit:]}
    return {"notes": vault.summary()[:limit]}


@tool(risk="low", tags=["forget", "delete note", "done", "complete", "remove note"])
def forget_note(text: str, note: str = "") -> str:
    """Forget a remembered line (matched by its text), from `note` or the To-do / Inbox / About me notes."""
    for n in ([note] if note else ["To-do", "Inbox", vault.ABOUT_ME]):
        try:
            r = vault.remove(n, text)
            return f"Removed from [[{r['note']}]]: {r['removed']}"
        except LookupError:
            continue
    raise ToolError(f"Nothing like '{text}' is remembered.", hint="brain(action='recall') to find it.")
