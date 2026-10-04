import asyncio

from jarvis import config
from jarvis.events import bus
from jarvis.hands import load_builtin_tools, registry

load_builtin_tools()


def run(tool_name, **args):
    async def go():
        bus.loop = asyncio.get_running_loop()
        return await registry.run(tool_name, args)
    return asyncio.run(go())


def test_settings_tool_changes_safe_settings_only():
    r = run("settings", action="set", name="tts_speed", value="0.85")
    assert r.ok and config.store.load().tts_speed == 0.85
    assert run("settings", action="set", name="proactive", value="off").ok and config.store.load().proactive is False
    assert run("settings", action="set", name="provider", value="lmstudio").ok
    assert config.store.load().base_url == config.PROVIDER_PRESETS["lmstudio"]["base_url"]
    for private in ("shell_enabled", "tool_permissions", "base_url"):
        r = run("settings", action="set", name=private, value="x")
        assert not r.ok and "can't be changed" in r.error
    got = run("settings", action="get").output
    assert "shell_enabled" not in got and got["tts_speed"] == 0.85
    config.store.update(proactive=True, tts_speed=1.0, provider="nvidia",
                        base_url=config.PROVIDER_PRESETS["nvidia"]["base_url"])


def test_every_screen_can_be_opened_by_asking():
    seen = []
    orig = bus.emit
    bus.emit = lambda t, **d: seen.append((t, d))
    try:
        assert run("show_panel", panel="history").ok
        assert not run("show_panel", panel="nonsense").ok
    finally:
        bus.emit = orig
    assert ("ui_open", {"panel": "history"}) in seen


def test_message_phone_needs_a_linked_phone():
    r = run("message_phone", text="hi")
    assert not r.ok and "No phone linked" in r.error
