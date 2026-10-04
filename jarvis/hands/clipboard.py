"""Clipboard read/write."""

from __future__ import annotations

from .registry import ToolError, tool


@tool(risk="low", tags=["clipboard", "copied", "paste", "copy"])
def read_clipboard(max_chars: int = 8000) -> str:
    """Read the text currently on the clipboard."""
    import pyperclip

    try:
        text = pyperclip.paste() or ""
    except Exception as e:
        raise ToolError(f"Clipboard unavailable: {e}", hint="On Linux install xclip or wl-clipboard.")
    return text[:max_chars] if text else "(clipboard is empty)"


@tool(risk="low", tags=["clipboard", "copy"])
def write_clipboard(text: str) -> str:
    """Copy text to the clipboard."""
    import pyperclip

    try:
        pyperclip.copy(text)
    except Exception as e:
        raise ToolError(f"Clipboard unavailable: {e}")
    return f"Copied {len(text)} characters to the clipboard."
