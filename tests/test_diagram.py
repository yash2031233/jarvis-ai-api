import asyncio
import json

import pytest

from jarvis.hands import load_builtin_tools, registry

load_builtin_tools()
pytest.importorskip("matplotlib")
pytest.importorskip("sympy")


@pytest.mark.parametrize("args", [
    {"kind": "graph", "equations": "y=2x+1; y<=x^2-3"},
    {"kind": "number_line", "inequalities": "-2<=x<5"},
    {"kind": "geometry", "data": json.dumps({"points": {"A": [0, 0], "B": [4, 0], "C": [0, 3]}, "polygons": [["A", "B", "C"]]})},
    {"kind": "chart", "data": json.dumps({"type": "bar", "labels": ["a", "b"], "series": {"n": [1, 2]}})},
    {"kind": "flow", "data": json.dumps({"nodes": {"a": "Start", "b": "End"}, "edges": [["a", "b"]]})},
])
def test_draws(args):
    r = asyncio.run(registry.run("diagram", args))
    assert r.ok, r.error
    from pathlib import Path

    assert Path(r.output["image"]).stat().st_size > 2000


def test_bad_equation_is_explained():
    r = asyncio.run(registry.run("diagram", {"kind": "graph", "equations": "y = = 3"}))
    assert not r.ok and "y=2x+1" in r.error


def test_no_code_execution_in_equations():
    r = asyncio.run(registry.run("diagram", {"kind": "graph", "equations": "y=__import__('os').system('echo hi')"}))
    assert not r.ok


@pytest.mark.parametrize("bad", ["y=eval('1')", "y=x.__class__", "y=[x]", "y=open(1)", "y=lambda", "y=os"])
def test_rejects_non_maths(bad):
    r = asyncio.run(registry.run("diagram", {"kind": "graph", "equations": bad}))
    assert not r.ok


@pytest.mark.parametrize("ok", ["y=2x+1", "y=sin(x)", "y=sqrt(x)+ln(x)", "x^2+y^2=25", "y=xy+3", "y=e^x"])
def test_accepts_normal_maths(ok):
    r = asyncio.run(registry.run("diagram", {"kind": "graph", "equations": ok}))
    assert r.ok, r.error
