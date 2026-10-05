"""Skills: saved, replayable multi-step procedures ("Jarvis, do my work setup")."""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from rapidfuzz import fuzz, process

from .. import config
from ..hands.registry import ToolError, tool

SKILLS_DIR = config.DATA_DIR / "skills"
# Steps of the most recent successful agent run (the engine fills this in)
last_run: dict[str, Any] = {"request": "", "steps": []}


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "skill"


def all_skills() -> list[dict[str, Any]]:
    out = []
    for f in sorted(SKILLS_DIR.glob("*.json")) if SKILLS_DIR.exists() else []:
        try:
            out.append(json.loads(f.read_text("utf-8")))
        except Exception:
            continue
    # bundled example skills from ./plugins/skills
    bundled = Path(__file__).resolve().parents[2] / "plugins" / "skills"
    for f in sorted(bundled.glob("*.json")) if bundled.exists() else []:
        try:
            s = json.loads(f.read_text("utf-8"))
            if not any(x["name"] == s["name"] for x in out):
                out.append(s)
        except Exception:
            continue
    return out


def find_skill(text: str) -> dict[str, Any] | None:
    """Match 'do my work setup' / 'run work setup' / 'work mode' to a saved skill."""
    skills = all_skills()
    if not skills:
        return None
    t = re.sub(r"^(?:jarvis[, ]+)?(?:please )?(?:run|do|start|activate|execute|launch)?\s*(?:my |the )?(?:skill )?",
               "", text.lower().strip()).strip(" .!?")
    names: dict[str, dict[str, Any]] = {}
    for s in skills:
        names[s["name"].lower()] = s
        for trig in s.get("triggers", []):
            names[trig.lower()] = s
    hit = process.extractOne(t, list(names), scorer=fuzz.ratio)
    if hit and hit[1] >= 88:
        return names[hit[0]]
    return None


def save(name: str, description: str, steps: list[dict[str, Any]], triggers: list[str] | None = None) -> Path:
    SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    data = {"name": name, "description": description, "triggers": triggers or [], "steps": steps,
            "version": 1, "created": time.strftime("%Y-%m-%d %H:%M")}
    p = SKILLS_DIR / f"{_slug(name)}.json"
    if p.exists():
        old = json.loads(p.read_text("utf-8"))
        data["version"] = int(old.get("version", 1)) + 1
    p.write_text(json.dumps(data, indent=2), "utf-8")
    return p


@tool(risk="low", tags=["save", "skill", "routine", "macro", "remember how", "save that"],
      examples=["save_last_as_skill(name='work setup', triggers=['work mode'])"])
def save_last_as_skill(name: str, description: str = "", triggers: list[str] | None = None) -> str:
    """Save the steps of the last completed multi-step task as a reusable skill the user can run by name."""
    steps = last_run.get("steps") or []
    if not steps:
        raise ToolError("There's no recent multi-step task to save.",
                        hint="Use create_skill with explicit steps instead.")
    save(name, description or last_run.get("request", ""), steps, triggers)
    return f"Saved skill '{name}' with {len(steps)} steps."


@tool(risk="low", tags=["create", "skill", "routine", "macro"],
      examples=["create_skill(name='focus mode', steps=[{'tool':'close_app','args':{'name':'discord'}}])"])
def create_skill(name: str, steps: list[dict], description: str = "", triggers: list[str] | None = None) -> str:
    """Create a skill from explicit steps: a list of {"tool": <tool name>, "args": {...}}."""
    from ..hands.registry import registry

    for s in steps:
        if not isinstance(s, dict) or s.get("tool") not in registry.tools:
            raise ToolError(f"Invalid step {s!r}.", hint="Each step needs a valid 'tool' and 'args'.")
    save(name, description, steps, triggers)
    return f"Created skill '{name}' ({len(steps)} steps)."


@tool(risk="low", tags=["skills", "routines", "macros", "list", "what can you do"])
def list_skills() -> list[dict]:
    """List saved skills."""
    return [{"name": s["name"], "description": s.get("description", ""), "steps": len(s.get("steps", [])),
             "triggers": s.get("triggers", [])} for s in all_skills()] or [{"info": "no skills saved yet"}]


@tool(risk="low", tags=["background", "job", "research", "long task", "later", "while", "in the background"],
      examples=["job(action='start', task='Research the best budget 3D printers of 2026 and summarize pros/cons')",
                "job(action='list')"])
async def job(action: str, task: str = "", id: str = "") -> dict:
    """Background jobs for BIG tasks that take many steps or minutes (research across several pages, making lots
    of files or flashcards, long comparisons, 'do X and send me the result'). start = hand off `task` - write it out
    fully with everything needed, since it runs in its own conversation - then tell the user it's started and
    they'll get a notification; list = jobs and their status; status = one job's result (`id`); cancel = stop one.
    Don't use it for quick things you can just do now."""
    from . import jobs

    a = action.lower().strip()
    try:
        if a == "start":
            if len(task.strip()) < 10:
                raise ToolError("Describe the task fully in `task`.")
            try:
                j = jobs.start(task)
            except RuntimeError as e:
                raise ToolError(str(e))
            if j.get("already"):
                return {"already_running": j["id"], "status": j["status"],
                        "note": "That exact job is already running - tell the user it's on its way. Don't start it again."}
            return {"started": j["id"], "status": j["status"],
                    "note": "It runs in the background and the user gets a notification with the result. Do NOT do "
                            "the task yourself now - just tell the user in one sentence that it's started."}
        if a == "list":
            return {"jobs": jobs.all_jobs()[:15] or "none"}
        if a in ("status", "result"):
            return jobs.get(id)
        if a == "cancel":
            return jobs.cancel(id)
        raise ToolError(f"Unknown action '{action}'.", hint="start, list, status, cancel")
    except LookupError as e:
        raise ToolError(str(e), hint="Use action=list to see job ids.")
