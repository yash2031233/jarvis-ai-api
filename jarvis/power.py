"""Sleep schedule (Windows): the PC hibernates at night and wakes itself - with Jarvis starting - in the morning.

Two tasks in Windows Task Scheduler (your user account, no admin needed):
  Jarvis Wake    every day at the wake time: wakes the PC from hibernation / sleep and starts Jarvis
  Jarvis Night   from the night time until 5 AM, every 10 minutes: if nobody has touched the PC for 10 minutes and
                 Jarvis isn't busy, it warns (phone notification + the app) and hibernates 2 minutes later - unless
                 the PC got used in between, or "keep it on tonight" was said.
The tasks are the setting: change the times here (or ask Jarvis) and they're rewritten. Waking needs "Allow wake
timers" on in the Windows power plan (it is by default).

    python -m jarvis.power night     what the Jarvis Night task runs
"""

from __future__ import annotations

import ctypes
import datetime as dt
import json
import re
import subprocess
import sys
import time
from pathlib import Path

from . import config

WAKE_TASK, NIGHT_TASK = "Jarvis Wake", "Jarvis Night"
ROOT = Path(__file__).resolve().parent.parent
KEEP = config.DATA_DIR / "keep_on_tonight.json"
IDLE_NEEDED = 600


class PowerError(Exception):
    pass


def _ps(script: str) -> str:
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True,
                       text=True, timeout=60, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if r.returncode:
        raise PowerError((r.stderr or r.stdout).strip()[-400:])
    return r.stdout.strip()


def _hhmm(t: str) -> str:
    if not re.match(r"^([01]?\d|2[0-3]):[0-5]\d$", t or ""):
        raise PowerError(f"'{t}' isn't a time like 06:30 or 22:30.")
    h, m = t.split(":")
    return f"{int(h):02d}:{m}"


def _launcher() -> tuple[str, str]:
    exe = ROOT / "Jarvis.exe"
    if exe.exists():
        return str(exe), ""
    pyw = Path(sys.executable).with_name("pythonw.exe")
    return str(pyw if pyw.exists() else Path(sys.executable)), "-m jarvis"


def _q(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def install(wake: str = "06:30", night: str = "22:30") -> dict:
    """Create (or update) both tasks. Empty time = remove that task."""
    if sys.platform != "win32":
        raise PowerError("The sleep schedule is Windows-only for now.")
    out = {}
    if wake:
        w = _hhmm(wake)
        exe, args = _launcher()
        _ps(f"""
$a = New-ScheduledTaskAction -Execute {_q(exe)} {'-Argument ' + _q(args) if args else ''} -WorkingDirectory {_q(str(ROOT))}
$t = New-ScheduledTaskTrigger -Daily -At {_q(w)}
$s = New-ScheduledTaskSettingsSet -WakeToRun -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 10)
Register-ScheduledTask -TaskName {_q(WAKE_TASK)} -Action $a -Trigger $t -Settings $s -Force -Description 'Wakes the PC and starts Jarvis (Jarvis > Settings > Sleep schedule).' | Out-Null""")
        out["wake"] = w
    else:
        _ps(f"Unregister-ScheduledTask -TaskName {_q(WAKE_TASK)} -Confirm:$false -ErrorAction SilentlyContinue")
    if night:
        n = _hhmm(night)
        pyw = Path(sys.executable).with_name("pythonw.exe")
        py = str(pyw if pyw.exists() else Path(sys.executable))
        nh, nm = map(int, n.split(":"))
        hours = ((5 * 60 - (nh * 60 + nm)) % (24 * 60)) / 60 or 1
        _ps(f"""
$a = New-ScheduledTaskAction -Execute {_q(py)} -Argument '-m jarvis.power night' -WorkingDirectory {_q(str(ROOT))}
$t = New-ScheduledTaskTrigger -Daily -At {_q(n)}
$t.Repetition = (New-ScheduledTaskTrigger -Once -At {_q(n)} -RepetitionInterval (New-TimeSpan -Minutes 10) -RepetitionDuration (New-TimeSpan -Minutes {int(hours * 60)})).Repetition
$s = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 8)
Register-ScheduledTask -TaskName {_q(NIGHT_TASK)} -Action $a -Trigger $t -Settings $s -Force -Description 'Hibernates the PC at night when nobody is using it (Jarvis > Settings > Sleep schedule).' | Out-Null""")
        out["night"] = n
    else:
        _ps(f"Unregister-ScheduledTask -TaskName {_q(NIGHT_TASK)} -Confirm:$false -ErrorAction SilentlyContinue")
    return {**status(), "set": out}


def status() -> dict:
    if sys.platform != "win32":
        return {"supported": False}
    raw = _ps(f"""
$o = @{{}}
foreach ($n in @({_q(WAKE_TASK)}, {_q(NIGHT_TASK)})) {{
  $t = Get-ScheduledTask -TaskName $n -ErrorAction SilentlyContinue
  if ($t) {{ $o[$n] = @{{ at = ([datetime]$t.Triggers[0].StartBoundary).ToString('HH:mm'); state = "$($t.State)"; wake = $t.Settings.WakeToRun }} }}
}}
$o | ConvertTo-Json -Compress""")
    d = json.loads(raw or "{}") if raw.startswith("{") else {}
    w, n = d.get(WAKE_TASK), d.get(NIGHT_TASK)
    return {"supported": True, "wake": w["at"] if w else None, "night": n["at"] if n else None,
            "keep_on_tonight": keep_on_tonight_active()}


# ------------------------------------------------------------------ tonight
def _night_key(now: dt.datetime) -> str:
    return (now.date() - dt.timedelta(days=1 if now.hour < 12 else 0)).isoformat()


def keep_on_tonight(on: bool = True) -> None:
    KEEP.parent.mkdir(parents=True, exist_ok=True)
    KEEP.write_text(json.dumps({"night": _night_key(dt.datetime.now()) if on else ""}), "utf-8")


def keep_on_tonight_active() -> bool:
    try:
        return json.loads(KEEP.read_text("utf-8")).get("night") == _night_key(dt.datetime.now())
    except Exception:
        return False


def idle_seconds() -> float:
    class LII(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]
    li = LII(ctypes.sizeof(LII), 0)
    ctypes.windll.user32.GetLastInputInfo(ctypes.byref(li))
    return (ctypes.windll.kernel32.GetTickCount() - li.dwTime) / 1000.0


def _jarvis_busy() -> bool:
    """A request or a background job in the running Jarvis (it writes busy.json while working)."""
    try:
        d = json.loads((config.DATA_DIR / "busy.json").read_text("utf-8"))
        return bool(d.get("busy")) and time.time() - d.get("t", 0) < 900
    except Exception:
        return False


def night_check() -> str:
    """What the Jarvis Night task runs every 10 minutes at night."""
    if keep_on_tonight_active():
        return "kept on tonight"
    if idle_seconds() < IDLE_NEEDED or _jarvis_busy():
        return "in use"
    try:
        from . import push

        push.send("PC hibernating in 2 min", "Nobody's used it for 10 minutes. Tell Jarvis \"keep it on tonight\" to stop it.",
                  "/", "night")
    except Exception:
        pass
    for _ in range(24):                    # 2 minutes, watching for anyone coming back
        time.sleep(5)
        if idle_seconds() < 30 or keep_on_tonight_active() or _jarvis_busy():
            return "someone came back"
    subprocess.run(["shutdown", "/h"], check=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return "hibernated"


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "night":
        r = night_check()
        try:
            log = config.DATA_DIR / "logs" / "night.log"
            log.parent.mkdir(parents=True, exist_ok=True)
            with log.open("a", encoding="utf-8") as f:
                f.write(f"{dt.datetime.now():%Y-%m-%d %H:%M} {r}\n")
        except Exception:
            pass
