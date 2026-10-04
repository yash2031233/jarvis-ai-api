"""Tool registry.

Tools are plain Python functions decorated with @tool. The signature becomes a typed
JSON schema (via Pydantic) so arguments are validated and coerced before execution.
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import inspect
import logging
import re
import sys
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal

from pydantic import BaseModel, ValidationError, create_model
from rapidfuzz import fuzz

log = logging.getLogger(__name__)

Risk = Literal["low", "medium", "high"]


class ToolError(Exception):
    """Raise from a tool with a hint the model can use to self-correct."""

    def __init__(self, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.hint = hint


@dataclass
class ToolResult:
    ok: bool
    output: Any = None
    error: str = ""
    hint: str = ""
    duration_ms: int = 0
    verified: bool | None = None

    def for_model(self, limit: int = 4000) -> str:
        if self.ok:
            body = self.output if isinstance(self.output, str) else _compact(self.output)
            if self.verified is False:
                body = f"WARNING: action ran but verification failed.\n{body}"
            return _truncate(body, limit)
        msg = f"ERROR: {self.error}"
        if self.hint:
            msg += f"\nHINT: {self.hint}"
        return msg


@dataclass
class Tool:
    name: str
    description: str
    fn: Callable[..., Any]
    args_model: type[BaseModel]
    risk: Risk = "low"
    tags: list[str] = field(default_factory=list)
    examples: list[str] = field(default_factory=list)
    readonly: bool = False
    timeout: float = 60.0
    verify: Callable[[dict[str, Any], Any], bool] | None = None
    undo: bool = False

    def schema(self) -> dict[str, Any]:
        params = self.args_model.model_json_schema()
        params.pop("title", None)
        for p in params.get("properties", {}).values():
            p.pop("title", None)
        desc = self.description
        if self.examples:
            desc += " Examples: " + "; ".join(self.examples)
        return {"type": "function", "function": {"name": self.name, "description": desc, "parameters": params}}


def _compact(obj: Any) -> str:
    import json

    try:
        return json.dumps(obj, ensure_ascii=False, default=str, separators=(",", ":"))
    except Exception:
        return str(obj)


def _truncate(s: str, limit: int) -> str:
    if len(s) <= limit:
        return s
    head = s[: int(limit * 0.7)]
    tail = s[-int(limit * 0.2):]
    return f"{head}\n…[{len(s) - len(head) - len(tail)} chars omitted]…\n{tail}"


class Registry:
    def __init__(self) -> None:
        self.tools: dict[str, Tool] = {}

    def register(self, t: Tool) -> None:
        if t.name in self.tools:
            log.warning("tool %s re-registered (plugin override?)", t.name)
        self.tools[t.name] = t

    def names(self) -> list[str]:
        return list(self.tools)

    def get(self, name: str) -> Tool | None:
        return self.tools.get(name)

    def schemas(self, names: list[str] | None = None) -> list[dict[str, Any]]:
        ts = self.tools.values() if names is None else [self.tools[n] for n in names if n in self.tools]
        return [t.schema() for t in ts]

    # ---------------------------------------------------- dynamic tool loading
    ALWAYS = ("system_info", "remember_note")

    def select(self, query: str, k: int = 14, enabled: Callable[[str], bool] | None = None) -> list[str]:
        """Pick the tools relevant to a request → smaller prompt, faster + more accurate picks."""
        q = query.lower()
        words = set(re.findall(r"[a-z0-9]+", q))
        scored: list[tuple[float, str]] = []
        for t in self.tools.values():
            if enabled and not enabled(t.name):
                continue
            hay = " ".join([t.name.replace("_", " "), t.description, " ".join(t.tags)]).lower()
            hay_words = set(re.findall(r"[a-z0-9]+", hay))
            overlap = len(words & hay_words)
            tag_hit = sum(3 for tag in t.tags if tag in q)
            fuzzy = fuzz.partial_ratio(q, t.name.replace("_", " ")) / 100
            score = overlap + tag_hit + fuzzy
            scored.append((score, t.name))
        scored.sort(reverse=True)
        picked = [n for _, n in scored[:k]]
        for a in self.ALWAYS:
            if a in self.tools and a not in picked and (not enabled or enabled(a)):
                picked.append(a)
        return picked

    # ---------------------------------------------------- execution
    async def run(self, name: str, args: dict[str, Any]) -> ToolResult:
        t0 = time.perf_counter()
        t = self.tools.get(name)
        if t is None:
            return ToolResult(False, error=f"Unknown tool '{name}'",
                              hint=f"Available tools: {', '.join(self.tools)}")
        if "__invalid_json__" in args:
            return ToolResult(False, error="Tool arguments were not valid JSON.",
                              hint="Call the tool again with a valid JSON object of arguments.")
        try:
            parsed = t.args_model.model_validate(args)
        except ValidationError as e:
            errs = "; ".join(f"{'.'.join(map(str, er['loc']))}: {er['msg']}" for er in e.errors())
            return ToolResult(False, error=f"Invalid arguments: {errs}",
                              hint=f"Expected schema: {_compact(t.schema()['function']['parameters'])}")
        kwargs = parsed.model_dump()
        try:
            if inspect.iscoroutinefunction(t.fn):
                out = await asyncio.wait_for(t.fn(**kwargs), timeout=t.timeout)
            else:
                out = await asyncio.wait_for(asyncio.to_thread(t.fn, **kwargs), timeout=t.timeout)
            verified = None
            if t.verify is not None:
                try:
                    verified = bool(await asyncio.to_thread(t.verify, kwargs, out))
                except Exception:
                    verified = False
            return ToolResult(True, output=out, duration_ms=_ms(t0), verified=verified)
        except ToolError as e:
            return ToolResult(False, error=str(e), hint=e.hint, duration_ms=_ms(t0))
        except asyncio.TimeoutError:
            return ToolResult(False, error=f"Timed out after {t.timeout:.0f}s",
                              hint="Try a narrower request or a different approach.", duration_ms=_ms(t0))
        except Exception as e:
            log.debug("tool %s crashed:\n%s", name, traceback.format_exc())
            return ToolResult(False, error=f"{e.__class__.__name__}: {e}", duration_ms=_ms(t0),
                              hint="Check the arguments or try an alternative tool.")


def _ms(t0: float) -> int:
    return int((time.perf_counter() - t0) * 1000)


registry = Registry()


def tool(
    name: str | None = None,
    *,
    description: str | None = None,
    risk: Risk = "low",
    tags: list[str] | None = None,
    examples: list[str] | None = None,
    readonly: bool | None = None,
    timeout: float = 60.0,
    verify: Callable[[dict[str, Any], Any], bool] | None = None,
    undo: bool = False,
    reg: Registry | None = None,
):
    """Decorator that registers a function as a Jarvis tool."""

    def deco(fn: Callable[..., Any]):
        sig = inspect.signature(fn)
        fields: dict[str, Any] = {}
        for pname, p in sig.parameters.items():
            ann = p.annotation if p.annotation is not inspect.Parameter.empty else str
            default = p.default if p.default is not inspect.Parameter.empty else ...
            fields[pname] = (ann, default)
        model = create_model(f"{fn.__name__}_args", **fields)  # type: ignore[call-overload]
        doc = inspect.getdoc(fn) or ""
        t = Tool(
            name=name or fn.__name__,
            description=description or doc.split("\n\n")[0].replace("\n", " "),
            fn=fn,
            args_model=model,
            risk=risk,
            tags=tags or [],
            examples=examples or [],
            readonly=(risk == "low") if readonly is None else readonly,
            timeout=timeout,
            verify=verify,
            undo=undo,
        )
        (reg or registry).register(t)
        fn.__jarvis_tool__ = t  # type: ignore[attr-defined]
        return fn

    return deco


def load_builtin_tools() -> None:
    from . import (  # noqa: F401
        apps, briefing, browser, cad, camera, clipboard, codeexec, control, diagram, files, location, media, notes,
        recording, robot, screen, shell, study, system, timers, weather, web,
    )
    from ..agent import skills  # noqa: F401  (skill tools)
    from ..agent import habits  # noqa: F401  (habit tools)

    habits.load_all()


def load_plugins(paths: list[Path]) -> list[str]:
    """Import every .py in the plugin dirs. Plugins use @tool to register."""
    loaded: list[str] = []
    for d in paths:
        if not d.exists():
            continue
        for f in sorted(d.glob("*.py")):
            if f.name.startswith("_"):
                continue
            mod_name = f"jarvis_plugin_{f.stem}"
            try:
                spec = importlib.util.spec_from_file_location(mod_name, f)
                assert spec and spec.loader
                mod = importlib.util.module_from_spec(spec)
                sys.modules[mod_name] = mod
                spec.loader.exec_module(mod)
                loaded.append(f.name)
            except Exception as e:
                log.error("plugin %s failed to load: %s", f.name, e)
    return loaded
