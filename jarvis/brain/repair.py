"""Tool-call repair: makes weak / quirky models usable as agents.

- Lenient JSON parsing (code fences, trailing commas, single quotes, Python literals)
- Fuzzy tool-name matching
- Extraction of tool calls a model wrote as *text* instead of native tool_calls
  (<tool_call>…</tool_call>, ```json {...}```, bare {"name":…, "arguments":…})
"""

from __future__ import annotations

import ast
import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Any

from rapidfuzz import process, fuzz


@dataclass
class ToolCall:
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: "call_" + uuid.uuid4().hex[:12])
    raw_arguments: str = ""

    def to_message(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": "function",
            "function": {"name": self.name, "arguments": json.dumps(self.arguments)},
        }


_FENCE = re.compile(r"^\s*```(?:json|JSON)?\s*|\s*```\s*$")
_TRAILING_COMMA = re.compile(r",\s*([}\]])")


def loads_lenient(text: str) -> Any:
    """Parse JSON-ish text produced by an LLM. Raises ValueError if hopeless."""
    if text is None:
        raise ValueError("empty")
    s = text.strip()
    if not s:
        return {}
    s = _FENCE.sub("", s)
    try:
        return json.loads(s)
    except Exception:
        pass
    # Cut to the outermost object/array if there is surrounding prose
    start = min([i for i in (s.find("{"), s.find("[")) if i != -1], default=-1)
    if start > 0:
        s = s[start:]
    s2 = _TRAILING_COMMA.sub(r"\1", s)
    try:
        return json.loads(s2)
    except Exception:
        pass
    # Balance unclosed braces (truncated output)
    balanced = _balance(s2)
    try:
        return json.loads(balanced)
    except Exception:
        pass
    # Python literal syntax: single quotes, True/False/None
    try:
        return ast.literal_eval(balanced)
    except Exception:
        pass
    py = re.sub(r"\btrue\b", "True", re.sub(r"\bfalse\b", "False", re.sub(r"\bnull\b", "None", balanced)))
    try:
        return ast.literal_eval(py)
    except Exception as e:
        raise ValueError(f"could not parse arguments: {text[:200]!r}") from e


def _balance(s: str) -> str:
    stack: list[str] = []
    in_str = False
    esc = False
    quote = ""
    for ch in s:
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == quote:
                in_str = False
            continue
        if ch in "\"'":
            in_str, quote = True, ch
        elif ch in "{[":
            stack.append("}" if ch == "{" else "]")
        elif ch in "}]" and stack:
            stack.pop()
    if in_str:
        s += quote
    return s + "".join(reversed(stack))


def match_tool_name(name: str, known: list[str], cutoff: int = 75) -> str | None:
    if not name:
        return None
    if name in known:
        return name
    norm = re.sub(r"[^a-z0-9]", "", name.lower())
    for k in known:
        if re.sub(r"[^a-z0-9]", "", k.lower()) == norm:
            return k
    # functions.open_app / tools.open_app / open_app()
    tail = re.split(r"[.:/]", name)[-1].rstrip("()")
    if tail in known:
        return tail
    hit = process.extractOne(tail, known, scorer=fuzz.ratio)
    if hit and hit[1] >= cutoff:
        return hit[0]
    return None


def normalize_args(raw: Any) -> dict[str, Any]:
    if raw is None or raw == "":
        return {}
    if isinstance(raw, str):
        parsed = loads_lenient(raw)
        # Some models double-encode: "\"{...}\""
        if isinstance(parsed, str):
            parsed = loads_lenient(parsed)
        raw = parsed
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, list) and len(raw) == 1 and isinstance(raw[0], dict):
        return raw[0]
    return {"input": raw}


_TAGGED = re.compile(r"<tool_call>\s*(.*?)\s*(?:</tool_call>|$)", re.S)
_FENCED = re.compile(r"```(?:json|tool_call|tool)?\s*(\{.*?\})\s*```", re.S)


def extract_text_tool_calls(text: str, known: list[str]) -> tuple[list[ToolCall], str]:
    """Find tool calls written as text. Returns (calls, remaining_text)."""
    if not text:
        return [], text
    calls: list[ToolCall] = []
    remaining = text
    candidates: list[tuple[str, str]] = []  # (span_text, json_text)
    for m in _TAGGED.finditer(text):
        candidates.append((m.group(0), m.group(1)))
    if not candidates:
        for m in _FENCED.finditer(text):
            candidates.append((m.group(0), m.group(1)))
    if not candidates:
        stripped = text.strip()
        if stripped.startswith("{") and '"name"' in stripped:
            candidates.append((text, stripped))
    for span, js in candidates:
        try:
            obj = loads_lenient(js)
        except ValueError:
            continue
        items = obj if isinstance(obj, list) else [obj]
        found = False
        for it in items:
            if not isinstance(it, dict):
                continue
            fn = it.get("function") if isinstance(it.get("function"), dict) else it
            name = fn.get("name") or fn.get("tool") or fn.get("action")
            args = fn.get("arguments", fn.get("parameters", fn.get("args", fn.get("action_input", {}))))
            real = match_tool_name(str(name or ""), known)
            if not real:
                continue
            try:
                calls.append(ToolCall(name=real, arguments=normalize_args(args)))
                found = True
            except ValueError:
                continue
        if found:
            remaining = remaining.replace(span, "")
    return calls, remaining.strip()


REACT_INSTRUCTIONS = """\
You can use tools. To call a tool, output ONLY this block (one per tool, several allowed):
<tool_call>{"name": "<tool name>", "arguments": {<json arguments>}}</tool_call>
After the tool results arrive you will continue. When no tool is needed, answer normally.
Available tools:
"""


def react_tool_listing(tools: list[dict[str, Any]]) -> str:
    lines = []
    for t in tools:
        f = t["function"]
        props = f.get("parameters", {}).get("properties", {})
        args = ", ".join(f"{k}: {v.get('type', 'any')}" for k, v in props.items())
        lines.append(f"- {f['name']}({args}): {f.get('description', '')}")
    return REACT_INSTRUCTIONS + "\n".join(lines)
