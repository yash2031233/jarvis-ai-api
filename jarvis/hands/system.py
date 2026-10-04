"""System info + power actions."""

from __future__ import annotations

import platform
import subprocess
import time
from datetime import datetime

import psutil

from .osutil import IS_MAC, IS_WIN, run
from .registry import ToolError, tool

_BOOT = psutil.boot_time()


@tool(risk="low", tags=["battery", "cpu", "ram", "memory", "disk", "time", "date", "uptime", "system", "status"],
      examples=["system_info(what='battery')", "system_info(what='all')"])
def system_info(what: str = "all") -> dict:
    """Get system status: what = all|battery|cpu|memory|disk|time|uptime|os."""
    w = what.lower()
    out: dict = {}
    if w in ("all", "time", "date"):
        now = datetime.now()
        out["time"] = now.strftime("%I:%M %p").lstrip("0")
        out["date"] = now.strftime("%A, %B %d, %Y")
    if w in ("all", "battery", "power"):
        b = psutil.sensors_battery()
        out["battery"] = (
            {"percent": round(b.percent), "plugged_in": b.power_plugged,
             "minutes_left": None if b.power_plugged or b.secsleft < 0 else b.secsleft // 60}
            if b else "no battery (desktop)"
        )
    if w in ("all", "cpu"):
        out["cpu_percent"] = psutil.cpu_percent(interval=0.2)
        out["cpu_cores"] = psutil.cpu_count()
    if w in ("all", "memory", "ram"):
        vm = psutil.virtual_memory()
        out["ram"] = {"used_gb": round(vm.used / 2**30, 1), "total_gb": round(vm.total / 2**30, 1),
                      "percent": vm.percent}
    if w in ("all", "disk", "storage"):
        disks = []
        for part in psutil.disk_partitions(all=False):
            try:
                u = psutil.disk_usage(part.mountpoint)
            except (PermissionError, OSError):
                continue
            disks.append({"drive": part.mountpoint, "free_gb": round(u.free / 2**30),
                          "total_gb": round(u.total / 2**30), "percent": u.percent})
        out["disks"] = disks
    if w in ("all", "uptime"):
        secs = int(time.time() - _BOOT)
        out["uptime"] = f"{secs // 86400}d {secs % 86400 // 3600}h {secs % 3600 // 60}m"
    if w in ("all", "os"):
        out["os"] = f"{platform.system()} {platform.release()}"
    if w in ("all", "gpu"):
        from ..hardware import detect

        out["gpu"] = detect().get("gpu_name") or "none detected"
    return out


@tool(risk="high", tags=["lock", "sleep", "shutdown", "restart", "power", "log off"])
def power_action(action: str) -> str:
    """Lock the screen, sleep, restart or shut down the computer. action = lock|sleep|restart|shutdown."""
    a = action.lower()
    if IS_WIN:
        cmds = {
            "lock": ["rundll32.exe", "user32.dll,LockWorkStation"],
            "sleep": ["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"],
            "restart": ["shutdown", "/r", "/t", "5"],
            "shutdown": ["shutdown", "/s", "/t", "5"],
        }
    elif IS_MAC:
        cmds = {
            "lock": ["pmset", "displaysleepnow"],
            "sleep": ["pmset", "sleepnow"],
            "restart": ["osascript", "-e", 'tell app "System Events" to restart'],
            "shutdown": ["osascript", "-e", 'tell app "System Events" to shut down'],
        }
    else:
        cmds = {
            "lock": ["loginctl", "lock-session"],
            "sleep": ["systemctl", "suspend"],
            "restart": ["systemctl", "reboot"],
            "shutdown": ["systemctl", "poweroff"],
        }
    if a not in cmds:
        raise ToolError(f"Unknown action '{action}'.", hint="Use lock, sleep, restart or shutdown.")
    subprocess.Popen(cmds[a])
    return f"{a.capitalize()} initiated."


@tool(risk="low", tags=["dark mode", "light mode", "theme", "appearance"])
def set_dark_mode(enabled: bool) -> str:
    """Turn the OS dark mode on or off."""
    if IS_WIN:
        val = "0" if enabled else "1"
        key = r"HKCU\Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
        for name in ("AppsUseLightTheme", "SystemUsesLightTheme"):
            r = run(["reg", "add", key, "/v", name, "/t", "REG_DWORD", "/d", val, "/f"])
            if r.returncode != 0:
                raise ToolError("Couldn't change the theme.")
    elif IS_MAC:
        run(["osascript", "-e",
             f'tell app "System Events" to tell appearance preferences to set dark mode to {str(enabled).lower()}'])
    else:
        scheme = "prefer-dark" if enabled else "default"
        run(["gsettings", "set", "org.gnome.desktop.interface", "color-scheme", scheme])
    return f"Dark mode {'on' if enabled else 'off'}."
