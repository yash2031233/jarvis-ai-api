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


def test_printing_needs_setup_and_says_so():
    from jarvis import config
    from jarvis.hands import load_builtin_tools, registry

    load_builtin_tools()
    config.store.update(printer_kind="", printer_model="")
    r = asyncio.run(registry.run("printer", {"action": "pause"}))
    assert not r.ok and "No printer set up" in r.error


def test_slicer_profiles_are_picked_from_the_printer_model(tmp_path, monkeypatch):
    from jarvis import config
    from jarvis.cad import printer

    exe = tmp_path / "slicer" / "orca.exe"
    exe.parent.mkdir()
    exe.write_text("")
    v = tmp_path / "slicer" / "resources" / "profiles" / "Flashforge"
    for sub, names in {"machine": ["Flashforge AD5X 0.4 nozzle", "Flashforge AD5X 0.6 nozzle"],
                       "process": ["0.20mm Standard @FF AD5X", "0.24mm Draft @FF AD5X", "0.16mm Standard @FF AD5X",
                                   "0.30mm Standard @FF AD5X 0.6 nozzle"],
                       "filament": ["Flashforge PLA Basic @FF AD5X", "Flashforge HS PLA @FF AD5X",
                                    "Flashforge PLA Basic @FF AD5X 0.6 nozzle"]}.items():
        (v / sub).mkdir(parents=True)
        for n in names:
            (v / sub / f"{n}.json").write_text("{}")
    config.store.update(slicer_path=str(exe), printer_model="Flashforge AD5X 0.4 nozzle", printer_filament="PLA Basic")
    try:
        assert printer.profiles("draft")["process"].stem == "0.24mm Draft @FF AD5X"
        assert printer.profiles("fine")["process"].stem == "0.16mm Standard @FF AD5X"
        assert printer.profiles("standard")["filament"].stem == "Flashforge PLA Basic @FF AD5X"
        config.store.update(printer_model="Flashforge AD5X 0.6 nozzle")
        p = printer.profiles("standard")
        assert p["process"].stem == "0.30mm Standard @FF AD5X 0.6 nozzle" and "0.6 nozzle" in p["filament"].stem
    finally:
        config.store.update(slicer_path="", printer_model="", printer_filament="PLA")


def test_devices_must_identify_themselves():
    import socket
    import threading

    from jarvis import devices

    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]

    def fake_flashforge():
        c, _ = srv.accept()
        with c:
            for _ in range(3):
                data = c.recv(1024)
                if b"M115" in data:
                    c.sendall(b"CMD M115 Received.\r\nMachine Type: Flashforge AD5X\r\nMachine Name: Shop\r\nok\r\n")
                elif data:
                    c.sendall(b"ok\r\n")
    threading.Thread(target=fake_flashforge, daemon=True).start()
    real = socket.create_connection
    devices.socket.create_connection = lambda addr, timeout=None: real(("127.0.0.1", port), timeout=timeout)
    try:
        info = devices.flashforge_info("10.9.9.9")
    finally:
        devices.socket.create_connection = real
        srv.close()
    assert info["kind"] == "flashforge" and info["model"] == "Flashforge AD5X" and info["name"] == "Shop"
    assert devices.octoprint_info("127.0.0.1", 1) is None and devices.car_info("127.0.0.1") is None