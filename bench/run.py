"""Agent benchmark: success rate, latency, tool calls and tokens per task.

Side-effecting tools are swapped for dry-run stubs that record the call and return a
plausible result, so the benchmark measures the agent's decisions and speed without
touching your machine.

    python -m bench.run --model meta/llama-3.3-70b-instruct
    python -m bench.run --model moonshotai/kimi-k2-instruct --tier multi
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
from pathlib import Path

from jarvis import config
from jarvis.events import bus
from jarvis.hands import load_builtin_tools, registry

SIDE_EFFECTS = {
    "open_app": "Opened {name}.", "close_app": "Closed {name}.", "focus_app": "Focused {name}.",
    "set_volume": "Volume updated.", "media_control": "Media: {action}", "open_url": "Opened {url}",
    "open_file": "Opened {path}", "write_file": "Wrote {path}", "move_file": "Moved.", "create_folder": "Created {path}",
    "delete_file": "Deleted {path}", "run_command": "{{\"exit_code\": 0, \"output\": \"ok\"}}",
    "power_action": "{action} initiated.", "set_dark_mode": "Done.", "write_clipboard": "Copied.",
    "browser_click": "Clicked.", "browser_fill": "Filled.", "remember_note": "Saved note #1.",
}

HERE = Path(__file__).parent


def stub_side_effects(calls: list[tuple[str, dict]]) -> None:
    for name, template in SIDE_EFFECTS.items():
        t = registry.get(name)
        if not t:
            continue

        def make(name=name, template=template):
            def fn(**kw):
                calls.append((name, kw))
                try:
                    return template.format(**kw)
                except Exception:
                    return template
            return fn

        t.fn = make()
        t.verify = None
    # wrap the rest to record calls too
    for t in registry.tools.values():
        if t.name in SIDE_EFFECTS:
            continue
        orig = t.fn
        if asyncio.iscoroutinefunction(orig):
            async def afn(__o=orig, __n=t.name, **kw):
                calls.append((__n, kw))
                return await __o(**kw)
            t.fn = afn
        else:
            def sfn(__o=orig, __n=t.name, **kw):
                calls.append((__n, kw))
                return __o(**kw)
            t.fn = sfn


async def run_task(agent, task, calls):
    calls.clear()
    agent.reset()
    t0 = time.perf_counter()
    reply = await agent.handle(task["prompt"])
    ms = int((time.perf_counter() - t0) * 1000)
    used = [c[0] for c in calls]
    ok = all(t in used for t in task.get("expect_tools", []))
    if task.get("any_of"):
        ok = ok and any(t in used for t in task["any_of"])
    if any(t in used for t in task.get("forbid_tools", [])):
        ok = False
    if not reply:
        ok = False
    return {"id": task["id"], "tier": task["tier"], "ok": ok, "ms": ms, "tools": used, "reply": reply[:200]}


async def main_async(args):
    config.load_dotenv()
    bus.loop = asyncio.get_running_loop()
    if args.model:
        config.store.load().model = args.model
    config.store.load().voice_enabled = False
    load_builtin_tools()
    calls: list[tuple[str, dict]] = []
    stub_side_effects(calls)
    # every tool auto-approved during the benchmark (all side effects are stubbed)
    config.store.load().tool_permissions = {n: "auto" for n in registry.names()}
    from jarvis.agent.engine import Agent

    agent = Agent()
    tasks = json.loads((HERE / "tasks.json").read_text("utf-8"))
    if args.tier:
        tasks = [t for t in tasks if t["tier"] == args.tier]
    results = []
    for t in tasks:
        r = await run_task(agent, t, calls)
        results.append(r)
        print(f"{'✓' if r['ok'] else '✗'} {r['id']:4} {r['ms']:6d} ms  {','.join(r['tools']) or '-':40.40}  {t['prompt'][:50]}")
    summary = {}
    for tier in ("fast", "single", "multi"):
        rs = [r for r in results if r["tier"] == tier]
        if rs:
            summary[tier] = {"n": len(rs), "success": round(sum(r["ok"] for r in rs) / len(rs), 3),
                             "p50_ms": int(statistics.median(r["ms"] for r in rs)),
                             "avg_tools": round(sum(len(r["tools"]) for r in rs) / len(rs), 2)}
    model = config.store.load().model
    print("\n" + json.dumps({"model": model, **summary}, indent=2))
    out = HERE / "results"
    out.mkdir(exist_ok=True)
    (out / f"{model.replace('/', '_')}-{int(time.time())}.json").write_text(
        json.dumps({"model": model, "summary": summary, "results": results}, indent=2), "utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="")
    ap.add_argument("--tier", choices=["fast", "single", "multi"])
    asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    main()
