"""CAD design benchmark: does the model + fix loop + visual check produce a buildable, sensible part?

    python -m bench.cad_run            # all prompts
    python -m bench.cad_run 0 6        # a slice
Results: bench/results/cad-<model>-<time>.json (parts stay in the data dir for viewing).
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

from jarvis import config
from jarvis.cad import designer
from jarvis.events import bus

HERE = Path(__file__).parent


async def main(lo: int, hi: int) -> None:
    bus.loop = asyncio.get_running_loop()
    prompts = json.loads((HERE / "cad_prompts.json").read_text("utf-8"))[lo:hi]
    rows = []
    for p in prompts:
        t0 = time.time()
        try:
            r = await designer.design(p["what"], "bench " + p["name"])
            visual = [i for i in r["issues"] if i.startswith("Visual check")]
            row = {"name": p["name"], "built": True, "visual_ok": not visual, "issues": r["issues"],
                   "size_mm": r["size_mm"], "watertight": r["watertight"], "fixes": r["auto_fixes"],
                   "seconds": round(time.time() - t0, 1), "part": r["part"]}
        except Exception as e:
            row = {"name": p["name"], "built": False, "error": str(e)[:300], "seconds": round(time.time() - t0, 1)}
        rows.append(row)
        mark = "OK " if row["built"] and row.get("visual_ok") else ("~  " if row["built"] else "X  ")
        print(f"{mark}{row['name']:18} {row['seconds']:6.1f}s fixes={row.get('fixes', '-')} "
              f"size={row.get('size_mm')} {'; '.join(row.get('issues', []))[:150] or row.get('error', '')[:150]}",
              flush=True)
    built = sum(r["built"] for r in rows)
    clean = sum(r["built"] and r.get("visual_ok") for r in rows)
    print(f"\nbuilt {built}/{len(rows)} · passed visual check {clean}/{len(rows)} · "
          f"avg {sum(r['seconds'] for r in rows) / max(1, len(rows)):.1f}s")
    out = HERE / "results"
    out.mkdir(exist_ok=True)
    model = config.store.load().model.replace("/", "_")
    (out / f"cad-{model}-{int(time.time())}.json").write_text(json.dumps(rows, indent=2), "utf-8")


if __name__ == "__main__":
    a = [int(x) for x in sys.argv[1:3]]
    asyncio.run(main(a[0] if a else 0, a[1] if len(a) > 1 else 999))
