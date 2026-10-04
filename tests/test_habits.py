import asyncio
import json

import pytest

from jarvis import config
from jarvis.agent import habits
from jarvis.events import bus
from jarvis.hands import load_builtin_tools, registry

load_builtin_tools()

GOOD = '''PARAMS = {"expr": {"type": "string", "description": "maths to work out"}}

def run(call, **p):
    r = call("calculate", expression=p["expr"])
    parts = r.split("=")
    return f"The answer is {parts[-1].strip()}"
'''


@pytest.mark.parametrize("code,why", [
    ("import os\ndef run(call, **p):\n    return 1", "Import"),
    ("def run(call, **p):\n    return p.copy()", "attribute"),
    ("def run(call, **p):\n    return ''.join.__self__", "attribute"),
    ("def run(call, **p):\n    return open('x')", "open"),
    ("def run(call, **p):\n    return eval('1')", "eval"),
    ("def run(call, **p):\n    return __import__('os')", "_"),
    ("def run(call, **p):\n    return [].__class__", "Attribute"),
    ("def run(call, **p):\n    t = 'calc'\n    return call(t)", "string literal"),
    ("def run(call, **p):\n    return 1\ndef other():\n    return 2", "exactly one function"),
    ("x = print\ndef run(call, **p):\n    return 1", "PARAMS"),
    ("def run(call, **p):\n    return (lambda: 1)()", "Lambda"),
    ("def run(call, **p):\n    return getattr(p, 'x')", "getattr"),
])
def test_validator_blocks_escapes(code, why):
    with pytest.raises(habits.HabitError) as e:
        habits.validate(code)
    assert why.lower() in str(e.value).lower()


def test_good_habit_validates_and_runs(tmp_path, monkeypatch):
    monkeypatch.setattr(habits, "DIR", tmp_path)
    habits.validate(GOOD)
    assert habits.tools_called(GOOD) == ["calculate"]
    h = {"name": "work_it_out", "description": "calc", "params": habits.load_params(GOOD), "code": GOOD,
         "tools": ["calculate"], "status": "trial", "uses": 0, "failures": 0}
    habits._save(h)
    habits.register(h)
    assert "habit_work_it_out" in registry.tools

    async def go():
        bus.loop = asyncio.get_running_loop()
        return await registry.run("habit_work_it_out", {"expr": "6*7"})

    r = asyncio.run(go())
    assert r.ok and r.output == "The answer is 42"
    saved = json.loads((tmp_path / "work_it_out.json").read_text())
    assert saved["status"] == "active" and saved["uses"] == 1


def test_permissions_still_apply_and_probation(tmp_path, monkeypatch):
    monkeypatch.setattr(habits, "DIR", tmp_path)
    code = 'PARAMS = {}\n\ndef run(call, **p):\n    return call("run_command", command="echo hi")\n'
    h = {"name": "sneaky", "description": "x", "params": {}, "code": code, "tools": ["run_command"],
         "status": "trial", "uses": 0, "failures": 0}
    habits._save(h)
    habits.register(h)
    assert registry.get("habit_sneaky").risk == "high"           # inherits the riskiest tool it calls
    config.store.update(shell_enabled=False)                      # shell turned off -> the habit can't use it
    try:
        async def go():
            bus.loop = asyncio.get_running_loop()
            return [await registry.run("habit_sneaky", {}) for _ in range(3)]

        rs = asyncio.run(go())
        assert all(not r.ok for r in rs) and "turned off" in rs[0].error
        assert json.loads((tmp_path / "sneaky.json").read_text())["status"] == "probation"
        assert "habit_sneaky" not in registry.tools               # benched after 3 failures
    finally:
        config.store.update(shell_enabled=True)
