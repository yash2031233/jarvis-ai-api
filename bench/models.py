"""Compare models for Jarvis: which one is best at what, on your own provider and key.

    python -m bench.models                         # the default lineup on your provider
    python -m bench.models --models z-ai/glm-5.3,moonshotai/kimi-k3 --cad

Per model:
  agent    the agent benchmark (bench/tasks.json, single + multi-step tiers): success rate and median time
  quiz     12 questions with checkable answers - reasoning, maths, knowledge, exact instruction-following, JSON
  vision   3 made-up pictures: steer a robot car, read a code off a sign, count objects (n/a if no image input)
  speed    time to the first token, median of 3
  broken   replies that came back empty or degenerate ("!!!!…") during the quiz
  cad      (with --cad) 3 OpenSCAD parts from bench/cad_prompts.json: built + passed the visual check
Results: bench/results/models-<date>.json and a table on screen. Nothing on your PC is touched (side-effecting
tools are stubbed, as in bench.run).
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import re
import statistics
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent

# frontier models (100B+ parameters, or the provider's flagship) - smart first, fast second
LINEUP = ["z-ai/glm-5.3", "moonshotai/kimi-k3", "moonshotai/kimi-k2.6", "deepseek-ai/deepseek-v4.1-flash",
          "nvidia/nemotron-3-ultra-550b-a55b", "nvidia/llama-3.1-nemotron-ultra-253b-v1", "mistralai/mistral-large-2-instruct",
          "nvidia/nemotron-3-super-120b-a12b"]

QUIZ = [
    ("math", "Pens cost 3 for $2. How much do 18 pens cost? Reply with just the number of dollars.", lambda a: re.search(r"\b12(\.0+)?\b", a)),
    ("math", "What is 17 * 23? Reply with just the number.", lambda a: "391" in a),
    ("time", "A train leaves at 14:45 and the trip takes 2 h 50 min. When does it arrive? Reply HH:MM, 24-hour.", lambda a: "17:35" in a),
    ("logic", "All bloops are razzies and all razzies are lazzies. Are all bloops definitely lazzies? Reply yes or no.", lambda a: a.strip().lower().startswith("yes")),
    ("logic", "I have 3 apples, eat one, buy 4 more, then give half of what I have to a friend. How many do I have? Just the number.", lambda a: re.search(r"\b3\b", a)),
    ("letters", "How many times does the letter r appear in the word strawberry? Just the number.", lambda a: re.search(r"\b3\b", a)),
    ("knowledge", "What is the capital of Australia? One word.", lambda a: "canberra" in a.lower()),
    ("knowledge", "Which planet has the most moons as of 2024? One word.", lambda a: "saturn" in a.lower()),
    ("format", "Reply with exactly three words, all lowercase, no punctuation, about the sea.", lambda a: re.fullmatch(r"[a-z]+ [a-z]+ [a-z]+", a.strip()) is not None),
    ("format", "Sort ascending, comma-separated, nothing else: 42, 7, 19, 3, 25", lambda a: re.sub(r"\s", "", a) == "3,7,19,25,42"),
    ("json", 'Return only JSON like {"name": "...", "age": 0} for: Maria turned 31 last week.',
     lambda a: (lambda j: j and j.get("name") == "Maria" and j.get("age") == 31)(_json(a))),
    ("language", "Translate to Spanish, only the translation: The car is red.", lambda a: re.search(r"(coche|carro|auto) es rojo", a.lower())),
    # harder: the kind fast answers get wrong
    ("hard", "A bat and a ball cost $1.10 in total. The bat costs $1.00 more than the ball. How many cents does the ball cost? Just the number.", lambda a: re.fullmatch(r"\D*5\D*", a.strip()) is not None),
    ("hard", "If 5 machines take 5 minutes to make 5 widgets, how many minutes do 100 machines take to make 100 widgets? Just the number.", lambda a: re.fullmatch(r"\D*5\D*", a.strip()) is not None),
    ("hard", "What is the sum of all integers from 1 to 200 that are divisible by 7? Just the number.", lambda a: "2842" in a.replace(",", "")),
    ("hard", "What is the 10th prime number? Just the number.", lambda a: re.search(r"\b29\b", a)),
    ("hard", "Alice is older than Bob. Carol is younger than Bob. Dave is older than Alice. Who is the youngest? One name.", lambda a: "carol" in a.lower()),
    ("hard", "How many days are there from March 3, 2025 to April 17, 2025, not counting March 3? Just the number.", lambda a: re.search(r"\b45\b", a)),
    ("hard", "Unscramble this animal: PNTHLEAE. One word.", lambda a: "elephant" in a.lower()),
    ("code", "Write one Python expression (no explanation, no code fences) that evaluates to the number of vowels (a, e, i, o, u, any case) in a string s.", lambda a: _vowels(a)),
]


def _vowels(a: str) -> bool:
    expr = re.sub(r"^```\w*|```$", "", a.strip()).strip().splitlines()[0] if a.strip() else ""
    try:
        safe = {"sum": sum, "len": len, "set": set, "list": list, "str": str, "map": map, "filter": filter, "any": any}
        return all(eval(expr, {"__builtins__": safe}, {"s": t}) == n for t, n in (("Programming in Python", 5), ("AEIOU xyz", 5), ("", 0)))
    except Exception:
        return False


def _json(a: str):
    m = re.search(r"\{.*\}", a, re.S)
    try:
        return json.loads(m.group(0)) if m else None
    except Exception:
        return None


def _images():
    import cv2
    import numpy as np

    car = np.full((320, 480, 3), 235, np.uint8)
    cv2.rectangle(car, (40, 140), (140, 300), (30, 30, 220), -1)
    cv2.circle(car, (380, 230), 50, (200, 120, 20), -1)
    cv2.putText(car, "DOOR", (200, 80), cv2.FONT_HERSHEY_SIMPLEX, 1.4, (0, 0, 0), 3)
    sign = np.full((240, 520, 3), 250, np.uint8)
    cv2.rectangle(sign, (20, 40), (500, 200), (30, 90, 30), -1)
    cv2.putText(sign, "GATE X7-42B", (45, 140), cv2.FONT_HERSHEY_SIMPLEX, 1.6, (255, 255, 255), 4)
    dots = np.full((300, 500, 3), 255, np.uint8)
    for x, y in [(60, 70), (180, 200), (300, 90), (420, 210), (250, 250)]:
        cv2.circle(dots, (x, y), 28, (40, 40, 200), -1)
    enc = lambda im: base64.b64encode(cv2.imencode(".jpg", im)[1].tobytes()).decode()  # noqa: E731
    return [
        ("steer", enc(car), 'A robot car camera. Goal: drive to the blue ball. Reply JSON only: {"move": "forward|left|right|veer_left|veer_right"}',
         lambda a: (_json(a) or {}).get("move") in ("right", "veer_right")),
        ("read", enc(sign), "What exactly is written on the green sign? Reply with only the text.", lambda a: "X7-42B" in a.upper().replace(" ", "")),
        ("count", enc(dots), "How many red circles are in the picture? Reply with just the number.", lambda a: re.search(r"\b5\b", a)),
    ]


async def _chat(client, model, content, max_tokens=900, timeout=120):
    t0 = time.time()
    r = await asyncio.wait_for(client.chat.completions.create(model=model, messages=[{"role": "user", "content": content}],
                                                              max_tokens=max_tokens, temperature=0.2), timeout)
    text = re.sub(r"<think>.*?</think>", "", r.choices[0].message.content or "", flags=re.S).strip()
    return text, time.time() - t0


async def quiz(client, model) -> dict:
    ok, broken, times, cats = 0, 0, [], {}
    for cat, q, check in QUIZ:
        try:
            a, dt = await _chat(client, model, q)
            times.append(dt)
            good = bool(a) and bool(check(a))
            broken += (not a) or bool(re.search(r"([^\w\s])\1{15,}", a))
        except Exception:
            good = False
            broken += 1
        ok += good
        cats.setdefault(cat, []).append(good)
        await asyncio.sleep(0.5)
    return {"score": f"{ok}/{len(QUIZ)}", "pct": round(100 * ok / len(QUIZ)), "broken": broken,
            "median_s": round(statistics.median(times), 1) if times else None,
            "missed": [c for c, v in cats.items() if not all(v)]}


async def vision(client, model) -> dict:
    res, times = {}, []
    for name, b64, q, check in _images():
        try:
            a, dt = await _chat(client, model, [{"type": "text", "text": q},
                                                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}], 600)
            res[name] = bool(check(a))
            times.append(dt)
        except Exception as e:
            if any(w in str(e).lower() for w in ("image", "invalid request", "400", "vision", "multimodal")):
                return {"score": "n/a (no image input)"}
            res[name] = False
    return {"score": f"{sum(res.values())}/3", "detail": res, "median_s": round(statistics.median(times), 1) if times else None}


async def speed(client, model) -> dict:
    ttfts = []

    async def first(t0: float):
        stream = await client.chat.completions.create(model=model, messages=[{"role": "user", "content": "Say hello in five words."}],
                                                      max_tokens=200, stream=True)
        try:
            async for ch in stream:
                d = ch.choices[0].delta if ch.choices else None
                if d and (d.content or (d.model_extra or {}).get("reasoning_content")):
                    return time.time() - t0
        finally:
            await stream.close()
    for _ in range(3):
        try:
            v = await asyncio.wait_for(first(time.time()), 60)
            if v:
                ttfts.append(v)
        except Exception:
            pass
    return {"ttft_s": round(statistics.median(ttfts), 2) if ttfts else None}


def agent(model: str) -> dict:
    out = {}
    for tier in ("single", "multi"):
        r = subprocess.run([sys.executable, "-m", "bench.run", "--model", model, "--tier", tier], capture_output=True,
                           text=True, timeout=3600, cwd=HERE.parent)
        m = re.search(r"\{\s*\"model\".*\}", r.stdout, re.S)
        try:
            out[tier] = json.loads(m.group(0)) if m else {"error": (r.stderr or r.stdout)[-300:]}
        except Exception:
            out[tier] = {"error": r.stdout[-300:]}
    return out


def cad(model: str) -> dict:
    from jarvis import config

    config.store.update(model=model)                     # cad_run designs with the configured model
    r = subprocess.run([sys.executable, "-m", "bench.cad_run", "0", "3"], capture_output=True, text=True, timeout=3600,
                       cwd=HERE.parent)
    files = sorted((HERE / "results").glob("cad-*.json"), key=lambda f: f.stat().st_mtime)
    try:
        rows = json.loads(files[-1].read_text("utf-8")) if files else []
        rows = rows.get("rows", rows) if isinstance(rows, dict) else rows
        built = sum(1 for x in rows if x.get("ok") or x.get("built"))
        passed = sum(1 for x in rows if x.get("built") and x.get("visual_ok"))
        return {"built": f"{built}/3", "passed_check": f"{passed}/3", "rows": rows}
    except Exception:
        return {"tail": r.stdout[-600:]}


async def main_async(args) -> None:
    from openai import AsyncOpenAI

    from jarvis import config

    s = config.store.load()
    client = AsyncOpenAI(base_url=s.base_url, api_key=config.get_api_key() or "local", max_retries=1, timeout=150)
    models = args.models.split(",") if args.models else LINEUP
    out_f = HERE / "results" / f"models-{time.strftime('%Y%m%d-%H%M')}.json"
    out_f.parent.mkdir(exist_ok=True)
    allres = {}
    for m in models:
        print(f"\n=== {m}", flush=True)
        r: dict = {}
        for name, fn in (("speed", speed), ("quiz", quiz), ("vision", vision)):
            try:
                r[name] = await fn(client, m)
            except Exception as e:
                r[name] = {"error": str(e)[:200]}
            print(f"  {name}: {r[name]}", flush=True)
        if not args.no_agent:
            r["agent"] = await asyncio.to_thread(agent, m)
            print(f"  agent: {json.dumps(r['agent'])[:400]}", flush=True)
        if args.cad:
            r["cad"] = await asyncio.to_thread(cad, m)
        allres[m] = r
        out_f.write_text(json.dumps(allres, indent=2), "utf-8")
    print(f"\nSaved {out_f}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", default="")
    ap.add_argument("--cad", action="store_true")
    ap.add_argument("--no-agent", action="store_true")
    asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    main()
