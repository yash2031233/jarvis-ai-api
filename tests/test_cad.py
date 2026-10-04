"""CAD engine tests (no model needed). Skipped when OpenSCAD isn't installed."""

import asyncio
import json

import pytest

from jarvis.cad import designer, openscad

try:
    openscad.binary()
    HAVE_OPENSCAD = True
except openscad.OpenSCADMissing:
    HAVE_OPENSCAD = False

needs_scad = pytest.mark.skipif(not HAVE_OPENSCAD, reason="OpenSCAD not installed")


@needs_scad
def test_build_and_mesh_report(tmp_path):
    f = tmp_path / "p.scad"
    f.write_text("w=40; difference(){ cube([w,20,10]); translate([20,10,-1]) cylinder(h=12, d=6, $fn=48); }")
    b = openscad.run(f, tmp_path / "p.stl")
    assert b.ok, b.errors
    r = openscad.mesh_report(tmp_path / "p.stl")
    assert r["size_mm"] == [40.0, 20.0, 10.0]
    assert r["watertight"] and r["fits_bed"] and not r["issues"]
    assert r["bounds_mm"]["x"] == [0.0, 40.0]
    assert 7.0 < r["volume_cm3"] < 8.0


@needs_scad
@pytest.mark.parametrize("code,needle", [
    ("cube([10,10,10]\nsphere(3);", "Parser error"),
    ("cube([w, 20, 10]);", "unknown variable"),
    ("x = 5;", "no geometry"),
    ("linear_extrude(extent=5) square(5);", "not specified as parameter"),
    ("include <BOSL2/std.scad>\ncuboid([10, -5, 3]);", "line 2"),   # BOSL2 assertion with the trace into our file
])
def test_errors_are_caught_with_detail(tmp_path, code, needle):
    f = tmp_path / "part.scad"
    f.write_text(code)
    b = openscad.run(f, tmp_path / "part.stl")
    assert not b.ok
    assert needle.lower() in "\n".join(b.errors).lower()


@needs_scad
def test_bigger_than_bed(tmp_path):
    f = tmp_path / "big.scad"
    f.write_text("cube([300, 20, 10]);")
    assert openscad.run(f, tmp_path / "big.stl").ok
    r = openscad.mesh_report(tmp_path / "big.stl", (220, 220, 220))
    assert not r["fits_bed"] and any("Bigger than" in i for i in r["issues"])


def test_clean_code_strips_prose_and_fences():
    text = "Sure! Here you go:\n```openscad\n// PLAN\nw = 10; // width\nh = 4;\ncube([w, w, h]);\n```\nEnjoy."
    assert designer._clean_code(text).startswith("// PLAN")
    assert designer._looks_like_scad(designer._clean_code(text))
    assert not designer._looks_like_scad("I can't design that, sorry.")


def test_quote_lines_points_at_code():
    code = "a = 1;\nb = 2;\ncube([a, b, c]);"
    q = designer._quote_lines(code, 'WARNING: Ignoring unknown variable "c" in file part.scad, line 3')
    assert "3: cube([a, b, c]);" in q


@needs_scad
def test_versions_revert_and_show(tmp_path, monkeypatch):
    monkeypatch.setattr(designer, "CAD_DIR", tmp_path)
    monkeypatch.setattr(designer, "STATE", tmp_path / "state.json")
    for v, size in ((1, 10), (2, 20)):
        d = designer.vdir("box", v)
        d.mkdir(parents=True)
        (d / "part.scad").write_text(f"cube({size});")
        b, rep = designer._build(d)
        assert b.ok
        (d / "meta.json").write_text(json.dumps({"prompt": f"v{v}", **rep}))
    assert designer.versions_of("box") == [1, 2]
    assert designer.resolve("Box") == "box"
    r = designer.revert("box", 1)
    assert r["version"] == 3 and designer.read_meta("box", 3)["size_mm"] == [10.0, 10.0, 10.0]
    assert designer.load_state()["part"] == "box"
    hist = designer.history("box")
    assert [h["version"] for h in hist] == [1, 2, 3] and hist[2]["kind"] == "revert to v1"
    with pytest.raises(LookupError):
        designer.resolve("spaceship")


def test_print3d_tool_registered_with_v1_name():
    from jarvis.hands import load_builtin_tools, registry

    load_builtin_tools()
    t = registry.get("print3d")
    assert t is not None and t.timeout >= 600 and not t.readonly
    props = t.schema()["function"]["parameters"]["properties"]
    assert {"action", "name", "what", "change"} <= set(props)


def test_print3d_printing_actions_explain_themselves():
    from jarvis.hands import load_builtin_tools, registry

    load_builtin_tools()
    r = asyncio.run(registry.run("print3d", {"action": "slice"}))
    assert not r.ok and "export" in r.hint
