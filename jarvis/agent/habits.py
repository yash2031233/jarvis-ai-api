"""Habits: procedural memory. A tool sequence that worked becomes a REAL tool - a small Python function the model
writes once - so next time it's one call instead of re-thinking eight steps. Also how "make yourself a tool that..."
works.

Safety: a habit's code is checked before it's ever run (AST allow-list): no imports, no attribute access, no
dunders, no exec/eval/open - only plain logic and `call(tool_name, **args)`, which runs Jarvis's existing tools
through the normal permission system (tools set to "ask" still ask; "off" stays off).

Lifecycle: new habits start on `trial`, become `active` after a successful run, and go to `probation` (disabled)
after 3 failures. Stored as JSON in <data>/habits/.
"""

from __future__ import annotations

import ast
import asyncio
import json
import logging
import re
import time
from pathlib import Path
from typing import Any

from pydantic import create_model

from .. import config
from ..hands.registry import Tool, ToolError, registry, tool

log = logging.getLogger(__name__)
DIR = config.DATA_DIR / "habits"
PREFIX = "habit_"

SAFE_BUILTINS = {"len": len, "str": str, "int": int, "float": float, "bool": bool, "min": min, "max": max,
                 "range": range, "enumerate": enumerate, "sorted": sorted, "round": round, "dict": dict, "list": list,
                 "isinstance": isinstance, "abs": abs, "sum": sum, "any": any, "all": all, "zip": zip,
                 "reversed": reversed, "Exception": Exception, "ValueError": ValueError}
# plain string / list / dict methods a habit may call - nothing that reaches internals
SAFE_ATTRS = {"split", "rsplit", "strip", "lstrip", "rstrip", "lower", "upper", "title", "capitalize", "replace",
              "join", "startswith", "endswith", "find", "count", "splitlines", "isdigit", "format", "get", "keys",
              "values", "items", "append", "extend", "insert", "index"}
_ALLOWED_NODES = (
    ast.Module, ast.FunctionDef, ast.arguments, ast.arg, ast.Return, ast.Assign, ast.AugAssign, ast.AnnAssign,
    ast.Expr, ast.Call, ast.keyword, ast.Name, ast.Load, ast.Store, ast.Constant, ast.JoinedStr, ast.FormattedValue,
    ast.Dict, ast.List, ast.Tuple, ast.Set, ast.Subscript, ast.Slice, ast.Index if hasattr(ast, "Index") else ast.Slice,
    ast.BinOp, ast.UnaryOp, ast.BoolOp, ast.Compare, ast.IfExp, ast.If, ast.For, ast.While, ast.Break, ast.Continue,
    ast.Pass, ast.ListComp, ast.DictComp, ast.comprehension, ast.Try, ast.ExceptHandler, ast.Raise,
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow, ast.USub, ast.UAdd, ast.Not, ast.And, ast.Or,
    ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.In, ast.NotIn, ast.Is, ast.IsNot,
)

COMPILE_SYSTEM = """You turn a task an AI assistant just completed into a reusable Python function ("habit").
Rules for the code:
- Define PARAMS (a dict: parameter name -> {"type": "string"|"integer"|"number"|"boolean", "description": "...",
  "default": optional}) for the parts that would change next time (an app name, a city, a number...).
- Define `def run(call, **p):` that does the task by calling tools: `result = call("tool_name", arg=value, ...)`.
  `call` returns the tool's result (dict/list/str) or raises Exception with the error.
- Use only plain Python: variables, if/for/while, f-strings, list/dict literals, len/str/int/min/max/range/sorted.
  Simple methods are fine: .split .strip .lower .upper .replace .join .startswith .get .keys .items .append.
  NO imports, NO other attributes, NO open/exec/eval, nothing starting with _.
- Return a short string saying what was done.
Check each tool's description for the shape of its result before you parse it; if unsure, keep the raw result.
Also give "test_params": realistic example values for PARAMS - the habit may be test-run with them.
Reply with JSON only: {"name": "snake_case_name", "description": "one sentence: what it does and when to use it",
"test_params": {...},
"code": "PARAMS = {...}\\n\\ndef run(call, **p):\\n    ..."}"""


class HabitError(ValueError):
    pass


_SAMPLE = {"string": "test", "integer": 2, "number": 10.0, "boolean": True}


# ------------------------------------------------------------------ validation + execution
def validate(code: str) -> None:
    """Reject anything outside the allow-list before it can run."""
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        raise HabitError(f"syntax error: {e}") from e
    funcs = [n for n in tree.body if isinstance(n, ast.FunctionDef)]
    if [f.name for f in funcs] != ["run"]:
        raise HabitError("define exactly one function: run(call, **p)")
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            if node.attr.startswith("_") or node.attr not in SAFE_ATTRS:
                raise HabitError(f"attribute '.{node.attr}' isn't allowed (only simple str/list/dict methods)")
            continue
        if not isinstance(node, _ALLOWED_NODES):
            raise HabitError(f"not allowed in a habit: {type(node).__name__}")
        if isinstance(node, ast.Name) and node.id.startswith("_"):
            raise HabitError(f"names starting with _ aren't allowed ({node.id})")
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id in (
                "exec", "eval", "open", "compile", "globals", "locals", "vars", "getattr", "setattr", "input",
                "breakpoint", "help", "type", "object", "super", "memoryview", "__import__"):
            raise HabitError(f"'{node.id}' isn't allowed in a habit")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "call":
            if not node.args or not isinstance(node.args[0], ast.Constant) or not isinstance(node.args[0].value, str):
                raise HabitError("call() must name the tool as a string literal: call(\"tool_name\", ...)")
    tops = [n for n in tree.body if not isinstance(n, ast.FunctionDef)]
    for n in tops:
        if not (isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name)
                and n.targets[0].id == "PARAMS"):
            raise HabitError("only PARAMS = {...} and def run(...) are allowed at the top level")


def tools_called(code: str) -> list[str]:
    tree = ast.parse(code)
    return sorted({n.args[0].value for n in ast.walk(tree) if isinstance(n, ast.Call)
                   and isinstance(n.func, ast.Name) and n.func.id == "call" and n.args
                   and isinstance(n.args[0], ast.Constant)})


def load_params(code: str) -> dict[str, Any]:
    tree = ast.parse(code)
    for n in tree.body:
        if isinstance(n, ast.Assign) and getattr(n.targets[0], "id", "") == "PARAMS":
            v = ast.literal_eval(n.value)
            if not isinstance(v, dict):
                raise HabitError("PARAMS must be a dict")
            return v
    return {}


def _compile_fn(code: str):
    validate(code)
    ns: dict[str, Any] = {"__builtins__": SAFE_BUILTINS}
    exec(compile(code, "<habit>", "exec"), ns)  # noqa: S102 - AST-validated above, no builtins beyond the allow-list
    return ns["run"]


async def _execute(h: dict[str, Any], params: dict[str, Any], seen: list | None = None) -> Any:
    from ..events import bus
    from ..safety import permissions

    loop = asyncio.get_running_loop()
    fn = _compile_fn(h["code"])

    def call(tool_name: str, **args: Any) -> Any:
        if tool_name.startswith(PREFIX):
            raise Exception("habits can't call other habits")
        t = registry.get(tool_name)
        if t is None:
            raise Exception(f"unknown tool {tool_name}")
        perm = permissions.effective(tool_name, t.risk)
        if perm == "off":
            raise Exception(f"the user turned off {tool_name}")
        if perm == "ask":
            ok = asyncio.run_coroutine_threadsafe(
                permissions.confirmations.ask(tool_name, args, f"{h['name']}: {tool_name}"), loop).result(180)
            if not ok:
                raise Exception(f"the user declined {tool_name}")
        bus.emit("tool_start", id=f"{h['name']}-{tool_name}-{time.time()}", name=tool_name, args=args)
        r = asyncio.run_coroutine_threadsafe(registry.run(tool_name, args), loop).result(t.timeout + 30)
        if seen is not None:
            seen.append({"tool": tool_name, "args": args, "returned": json.dumps(r.output if r.ok else r.error,
                                                                                 default=str)[:1500]})
        if not r.ok:
            raise Exception(f"{tool_name}: {r.error}")
        return r.output

    return await asyncio.to_thread(fn, call, **params)


# ------------------------------------------------------------------ store + registration
def _path(name: str) -> Path:
    return DIR / f"{name}.json"


def all_habits() -> list[dict[str, Any]]:
    out = []
    for p in sorted(DIR.glob("*.json")) if DIR.exists() else []:
        try:
            out.append(json.loads(p.read_text("utf-8")))
        except Exception:
            continue
    return out


def _save(h: dict[str, Any]) -> None:
    DIR.mkdir(parents=True, exist_ok=True)
    _path(h["name"]).write_text(json.dumps(h, indent=1), "utf-8")


_TYPES = {"string": str, "integer": int, "number": float, "boolean": bool}


def register(h: dict[str, Any]) -> None:
    """Make a habit a real tool (name habit_<name>) - unless it's on probation."""
    tname = PREFIX + h["name"]
    if h["status"] == "probation":
        registry.tools.pop(tname, None)
        return
    params = h.get("params", {})
    fields = {k: (_TYPES.get(v.get("type", "string"), str), v.get("default", ...)) for k, v in params.items()}
    model = create_model(f"{tname}_args", **fields)  # type: ignore[call-overload]
    risks = [registry.get(t).risk for t in h.get("tools", []) if registry.get(t)]
    risk = "high" if "high" in risks else "medium" if "medium" in risks else "low"

    async def fn(**kwargs: Any) -> Any:
        return await run_habit(h["name"], kwargs)

    registry.register(Tool(
        name=tname, description=f"[habit] {h['description']} (uses: {', '.join(h.get('tools', []))})",
        fn=fn, args_model=model, risk=risk, tags=["habit"] + h["name"].split("_"),
        readonly=False, timeout=600,
    ))


def load_all() -> int:
    n = 0
    for h in all_habits():
        try:
            validate(h["code"])
            register(h)
            n += 1
        except Exception as e:
            log.warning("habit %s skipped: %s", h.get("name"), e)
    return n


async def run_habit(name: str, params: dict[str, Any]) -> Any:
    h = json.loads(_path(name).read_text("utf-8"))
    try:
        out = await _execute(h, params)
    except Exception as e:
        h["failures"] = h.get("failures", 0) + 1
        h["last_error"] = str(e)[:300]
        if h["failures"] >= 3:
            h["status"] = "probation"
        _save(h)
        register(h)
        raise ToolError(f"Habit '{name}' failed: {e}",
                        hint="Do the task step by step with the normal tools instead." +
                             (" The habit is now on probation." if h["status"] == "probation" else ""))
    h["uses"] = h.get("uses", 0) + 1
    h["failures"] = 0
    if h["status"] == "trial":
        h["status"] = "active"
    _save(h)
    return out


# ------------------------------------------------------------------ making habits
async def compile_habit(request: str, steps: list[dict[str, Any]], name: str = "", hint: str = "") -> dict[str, Any]:
    from ..brain.client import brain
    from ..brain.repair import loads_lenient

    used = sorted({s["tool"] for s in steps})
    schemas = [registry.get(t).schema()["function"] for t in used if registry.get(t)]
    user = (f"Task the user asked for: {request}\n"
            f"What worked (tool calls in order): {json.dumps(steps)[:4000]}\n"
            f"Tool schemas: {json.dumps(schemas)[:6000]}\n" + (f"Name it: {name}\n" if name else "") +
            (f"Extra guidance: {hint}\n" if hint else ""))
    messages = [{"role": "system", "content": COMPILE_SYSTEM}, {"role": "user", "content": user}]
    last_err = ""
    for _ in range(4):
        text, _ = await brain.complete(messages, max_tokens=1500, temperature=0.2)
        try:
            j = loads_lenient(text[text.find("{"):])
            code = str(j["code"])
            validate(code)
            params = load_params(code)
            hname = re.sub(r"[^a-z0-9_]+", "_", (name or j.get("name") or "habit").lower()).strip("_")[:40]
            bad = [t for t in tools_called(code) if registry.get(t) is None]
            if bad:
                raise HabitError(f"calls unknown tools: {bad}")
            h = {"name": hname, "description": str(j.get("description", request))[:300], "params": params,
                 "code": code, "tools": tools_called(code), "status": "trial", "uses": 0, "failures": 0,
                 "created": time.time(), "from_request": request[:300]}
            # habits that only read (no side effects) are test-run before they're kept
            if all(registry.get(t) and registry.get(t).readonly for t in h["tools"]):
                test = j.get("test_params") if isinstance(j.get("test_params"), dict) else {}
                args = {k: test.get(k, v.get("default", _SAMPLE.get(v.get("type", "string"))))
                        for k, v in params.items()}
                seen: list = []
                try:
                    out = await _execute(h, args, seen)
                except Exception as e:
                    raise HabitError(f"test run with {args} failed: {e}. What the tools actually returned: "
                                     f"{json.dumps(seen)[:3000]}") from e
                problem = await _judge(h, args, out, seen)
                if problem:
                    raise HabitError(f"test run gave a wrong answer: {problem}. The habit said: {str(out)[:300]}. "
                                     f"What the tools actually returned: {json.dumps(seen)[:3000]}")
                h["tested"] = {"params": args, "result": str(out)[:300]}
                h["status"] = "active"
            _save(h)
            registry.tools.pop(PREFIX + hname, None)
            register(h)
            return h
        except Exception as e:
            last_err = str(e)
            messages = messages + [{"role": "assistant", "content": text[:3000]},
                                   {"role": "user", "content": f"That habit was rejected: {last_err}. "
                                                               "Fix it and reply with the JSON again."}]
    raise HabitError(f"couldn't make a valid habit: {last_err}")


async def _judge(h: dict[str, Any], args: dict[str, Any], out: Any, seen: list) -> str:
    """Did the habit's answer actually match what the tools returned? '' = yes, else the problem."""
    from ..brain.client import brain
    from ..brain.repair import loads_lenient

    text, _ = await brain.complete([
        {"role": "system", "content": "You check a small program's answer against the raw data it worked from. "
                                      "Reply JSON only: {\"correct\": true} or {\"correct\": false, \"problem\": "
                                      "\"what's wrong, e.g. it read the wrong field\"}."},
        {"role": "user", "content": f"Purpose: {h['description']}\nInputs: {json.dumps(args)}\n"
                                    f"Raw tool results: {json.dumps(seen)[:4000]}\nProgram's answer: {str(out)[:500]}"},
    ], max_tokens=200, temperature=0.0)
    try:
        j = loads_lenient(text[text.find("{"):])
    except ValueError:
        return ""
    return "" if not isinstance(j, dict) or j.get("correct", True) else str(j.get("problem") or "wrong answer")


@tool(risk="low", timeout=180, tags=["habit", "make a tool", "remember how", "automate", "shortcut", "next time"],
      examples=["make_habit(name='focus_mode')", "make_habit(task='open spotify, set volume to 30 and play', "
                "name='music_time')"])
async def make_habit(name: str = "", task: str = "", hint: str = "") -> dict:
    """Turn what just worked into a real reusable tool (a 'habit') - or build a new tool from a described `task`
    made of existing tools. Without `task`, compiles the last multi-step task you completed. The new tool is called
    habit_<name> and starts on trial; it becomes active after it works once."""
    from . import skills

    if task:
        steps = [{"tool": t, "args": {}} for t in registry.select(task, k=6)]
        request = task
    else:
        steps = skills.last_run.get("steps") or []
        request = skills.last_run.get("request", "")
        if not steps:
            raise ToolError("There's no recent multi-step task to turn into a habit.",
                            hint="Give `task` describing what the habit should do.")
    try:
        h = await compile_habit(request, steps, name, hint)
    except HabitError as e:
        raise ToolError(str(e), hint="Try again with a clearer description, or use create_skill for a fixed list.")
    return {"habit": PREFIX + h["name"], "description": h["description"], "params": h["params"],
            "uses_tools": h["tools"], "status": h["status"]}


@tool(risk="low", tags=["habits", "my tools", "shortcuts", "list"])
def list_habits() -> list[dict]:
    """List the habits (tools Jarvis made for himself) with status and use counts."""
    return [{"tool": PREFIX + h["name"], "description": h["description"], "status": h["status"],
             "uses": h.get("uses", 0), "failures": h.get("failures", 0)} for h in all_habits()] or [{"info": "none yet"}]


@tool(risk="medium", tags=["habit", "delete", "remove", "forget"])
def remove_habit(name: str) -> str:
    """Delete a habit by name."""
    n = name.removeprefix(PREFIX)
    p = _path(n)
    if not p.exists():
        raise ToolError(f"No habit '{name}'.", hint="list_habits")
    p.unlink()
    registry.tools.pop(PREFIX + n, None)
    return f"Removed {PREFIX + n}."
