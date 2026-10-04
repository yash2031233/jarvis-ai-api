import asyncio

import pytest

from jarvis.hands import load_builtin_tools, registry
from jarvis.hands.registry import Registry, ToolError, tool

load_builtin_tools()


def run(name, **args):
    return asyncio.run(registry.run(name, args))


def test_all_tools_have_valid_schemas():
    assert len(registry.tools) >= 40
    for t in registry.tools.values():
        s = t.schema()
        assert s["function"]["name"] == t.name
        assert s["function"]["description"], t.name
        assert s["function"]["parameters"]["type"] == "object"
        assert t.risk in ("low", "medium", "high")


def test_risky_tools_are_not_auto():
    for name in ("run_command", "delete_file", "power_action", "close_app", "write_file", "run_python"):
        assert registry.get(name).risk in ("medium", "high"), name


def test_validation_coerces_and_reports():
    r = run("calculate", expression="2 + 2 * 10")
    assert r.ok and r.output.endswith("= 22")
    bad = run("set_timer", seconds="soon")
    assert not bad.ok and "Invalid arguments" in bad.error and bad.hint


def test_unknown_tool_gives_hint():
    r = run("teleport")
    assert not r.ok and "Available tools" in r.hint


def test_calculate_is_safe():
    assert run("calculate", expression="15% of 2400").output.endswith("= 360.0")
    assert not run("calculate", expression="__import__('os').system('echo hi')").ok


def test_system_info():
    r = run("system_info", what="all")
    assert r.ok and "time" in r.output and "cpu_percent" in r.output


def test_select_relevant_tools():
    picked = registry.select("turn the volume down and skip this song", k=8)
    assert "set_volume" in picked and "media_control" in picked
    picked = registry.select("find my resume pdf", k=8)
    assert "search_files" in picked


def test_tool_error_hint_round_trip():
    reg = Registry()

    @tool(reg=reg)
    def flaky(x: int) -> str:
        """Fails on purpose."""
        raise ToolError("nope", hint="try x=2")

    r = asyncio.run(reg.run("flaky", {"x": 1}))
    assert not r.ok and r.hint == "try x=2"
    assert "HINT: try x=2" in r.for_model()


def test_output_truncation():
    reg = Registry()

    @tool(reg=reg)
    def big() -> str:
        """Returns a lot."""
        return "x" * 20000

    r = asyncio.run(reg.run("big", {}))
    assert len(r.for_model(4000)) < 4200 and "omitted" in r.for_model(4000)


@pytest.mark.asyncio
async def test_timer_lifecycle():
    from jarvis.hands import timers

    out = await timers.set_timer(seconds=60, label="pasta")
    assert "1m" in out
    assert any(t["label"] == "pasta" for t in timers.list_timers_sync())
    assert "Cancelled" in timers.cancel_timer("pasta")
