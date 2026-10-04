"""Notes / long-term facts Jarvis can remember about the user."""

from __future__ import annotations

from ..memory.store import memory
from .registry import ToolError, tool


@tool(risk="low", tags=["remember", "note", "save", "don't forget", "memorize", "todo"],
      examples=["remember_note(text='My wifi password is on the fridge', kind='fact')"])
def remember_note(text: str, kind: str = "note") -> str:
    """Save a note, to-do or fact about the user for later. kind = note|todo|fact."""
    nid = memory.add_note(text, kind)
    return f"Saved {kind} #{nid}."


@tool(risk="low", tags=["notes", "todos", "remember", "what do you know", "list", "recall"])
def list_notes(kind: str = "", query: str = "", limit: int = 30) -> list[dict]:
    """List saved notes/todos/facts, optionally filtered by kind or search text."""
    rows = memory.notes(kind or None, query or None, limit)
    return rows or [{"info": "no notes saved"}]


@tool(risk="low", tags=["forget", "delete note", "done", "complete", "remove note"])
def forget_note(note_id: int) -> str:
    """Delete a saved note/todo by its id."""
    if not memory.delete_note(note_id):
        raise ToolError(f"No note #{note_id}.", hint="Use list_notes to see ids.")
    return f"Removed note #{note_id}."
