"""Sandboxed-ish Python execution in a separate process (math, data crunching, conversions)."""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

from .registry import ToolError, tool


@tool(risk="medium", tags=["python", "calculate", "compute", "math", "convert", "code", "script", "data"],
      examples=["run_python(code='print(2**64)')"], timeout=70)
def run_python(code: str, timeout: int = 30) -> dict:
    """Run a short Python script in an isolated subprocess and return its printed output.
    Use print() for results. Good for math, unit conversion, CSV/JSON crunching."""
    with tempfile.TemporaryDirectory(prefix="jarvis_py_") as d:
        script = Path(d) / "main.py"
        script.write_text(code, "utf-8")
        try:
            r = subprocess.run([sys.executable, "-I", str(script)], capture_output=True, text=True,
                               timeout=max(1, min(timeout, 60)), cwd=d, encoding="utf-8", errors="replace",
                               creationflags=0x08000000 if sys.platform == "win32" else 0)
        except subprocess.TimeoutExpired:
            raise ToolError("Script timed out.", hint="Make it faster or raise the timeout (max 60s).")
    if r.returncode != 0:
        raise ToolError(f"Script error:\n{r.stderr[-3000:]}", hint="Fix the error and run again.")
    return {"output": r.stdout[-6000:] or "(no output — use print())"}


@tool(risk="low", tags=["calculate", "math", "plus", "minus", "times", "divided", "percent", "sqrt"],
      examples=["calculate(expression='15% of 2400')", "calculate(expression='sqrt(2)*10')"])
def calculate(expression: str) -> str:
    """Evaluate a math expression safely (supports +-*/ ** % sqrt sin cos log pi e, 'x% of y')."""
    import ast
    import math
    import re

    expr = expression.lower().replace("^", "**").replace("×", "*").replace("÷", "/")
    expr = re.sub(r"(\d+(?:\.\d+)?)\s*%\s*of\s*(\d+(?:\.\d+)?)", r"(\1/100*\2)", expr)
    allowed = {k: getattr(math, k) for k in ("sqrt", "sin", "cos", "tan", "log", "log10", "log2", "exp",
                                             "floor", "ceil", "pi", "e", "factorial", "radians", "degrees")}
    allowed.update(abs=abs, round=round, min=min, max=max)
    tree = ast.parse(expr, mode="eval")
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id not in allowed:
            raise ToolError(f"Unknown name '{node.id}'.", hint="Use run_python for complex code.")
        if isinstance(node, (ast.Attribute, ast.Subscript, ast.Lambda, ast.Import)):
            raise ToolError("Unsupported expression.", hint="Use run_python.")
    val = eval(compile(tree, "<expr>", "eval"), {"__builtins__": {}}, allowed)  # noqa: S307 (AST-checked)
    if isinstance(val, float):
        val = round(val, 10)
    return f"{expression} = {val}"
