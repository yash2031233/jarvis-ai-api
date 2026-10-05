"""The user's own browser: its profiles (personal / school / work accounts) and opening pages in the right one.

Chrome, Edge and Brave keep their profiles in "User Data/Local State": a name, the signed-in account (email) and
whether it's managed (a school / work account). open_url(profile=...) opens a page in that profile; web apps are
known by name ("spotify", "gmail", "a new google doc"), so "open spotify in chrome" opens Spotify's web player in
Chrome instead of looking for an installed app called that.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from .registry import ToolError, tool

BROWSERS = {   # name -> (User Data folder under LOCALAPPDATA, executable candidates)
    "chrome": (r"Google\Chrome\User Data", [r"Google\Chrome\Application\chrome.exe"]),
    "edge": (r"Microsoft\Edge\User Data", [r"Microsoft\Edge\Application\msedge.exe"]),
    "brave": (r"BraveSoftware\Brave-Browser\User Data", [r"BraveSoftware\Brave-Browser\Application\brave.exe"]),
}
WEB_APPS = {
    "spotify": "https://open.spotify.com", "youtube": "https://www.youtube.com", "youtube music": "https://music.youtube.com",
    "gmail": "https://mail.google.com", "google drive": "https://drive.google.com", "drive": "https://drive.google.com",
    "google calendar": "https://calendar.google.com", "calendar": "https://calendar.google.com",
    "google docs": "https://docs.google.com", "google sheets": "https://sheets.google.com",
    "google slides": "https://slides.google.com", "google classroom": "https://classroom.google.com",
    "classroom": "https://classroom.google.com", "netflix": "https://www.netflix.com", "github": "https://github.com",
    "chatgpt": "https://chatgpt.com", "claude": "https://claude.ai", "whatsapp": "https://web.whatsapp.com",
    "discord": "https://discord.com/app", "outlook": "https://outlook.live.com", "twitch": "https://www.twitch.tv",
    "reddit": "https://www.reddit.com", "x": "https://x.com", "twitter": "https://x.com", "instagram": "https://www.instagram.com",
    "amazon": "https://www.amazon.com", "maps": "https://maps.google.com", "google maps": "https://maps.google.com",
    # new documents open straight into a blank one, ready to type in
    "new google doc": "https://docs.new", "new doc": "https://docs.new", "new document": "https://docs.new",
    "new google sheet": "https://sheets.new", "new sheet": "https://sheets.new", "new spreadsheet": "https://sheets.new",
    "new google slides": "https://slides.new", "new presentation": "https://slides.new", "new slides": "https://slides.new",
    "new google form": "https://forms.new", "new form": "https://forms.new", "new email": "https://mail.google.com/mail/?view=cm",
}


def web_app_url(name: str) -> str | None:
    n = re.sub(r"^(a |an |the |my )", "", name.strip().lower())
    n = re.sub(r"\s+(web ?app|website|site|player)$", "", n)
    return WEB_APPS.get(n)


def _local() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", ""))


def browser_exe(browser: str) -> str | None:
    if sys.platform != "win32":
        return shutil.which({"chrome": "google-chrome", "edge": "microsoft-edge", "brave": "brave-browser"}[browser])
    for base in (_local(), Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")),
                 Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"))):
        for rel in BROWSERS[browser][1]:
            if (base / rel).exists():
                return str(base / rel)
    return None


def profiles(browser: str = "chrome") -> list[dict]:
    """The browser's profiles: folder, display name, signed-in account, managed (school / work) or not."""
    data = _local() / BROWSERS[browser][0]
    try:
        cache = json.loads((data / "Local State").read_text("utf-8"))["profile"]["info_cache"]
    except Exception:
        return []
    out = []
    for folder, p in cache.items():
        email = p.get("user_name") or ""
        managed = bool(p.get("hosted_domain") and p.get("hosted_domain") not in ("NO_HOSTED_DOMAIN", "")) or \
            bool(p.get("is_managed")) or (email and not re.search(r"@(gmail|googlemail|outlook|hotmail|live|icloud|yahoo)\.", email))
        out.append({"folder": folder, "name": p.get("name") or p.get("shortcut_name") or folder, "account": email,
                    "person": p.get("gaia_name") or p.get("gaia_given_name") or "",
                    "kind": "school / work (managed)" if managed and email else "personal" if email else "not signed in"})
    return out


def pick_profile(browser: str, want: str) -> dict | None:
    """'personal' / 'school' / 'work' / a name / an email / part of one -> that profile."""
    ps = profiles(browser)
    w = want.strip().lower()
    if not w or not ps:
        return None
    for p in ps:
        if w in (p["folder"].lower(), p["name"].lower(), p["account"].lower()):
            return p
    if w in ("personal", "my personal", "home", "own", "my own"):
        return next((p for p in ps if p["kind"] == "personal"), None)
    if w in ("school", "work", "managed", "edu", "education"):
        return next((p for p in ps if p["kind"].startswith("school")), None)
    return next((p for p in ps if w in p["name"].lower() or w in p["account"].lower() or w in p["person"].lower()), None)


def open_in(browser: str, url: str, profile: str = "") -> str:
    exe = browser_exe(browser)
    if not exe:
        raise ToolError(f"{browser.title()} isn't installed.")
    args = [exe]
    label = ""
    if profile:
        p = pick_profile(browser, profile)
        if p is None:
            have = "; ".join(f"{x['name']} ({x['account'] or 'no account'}, {x['kind']})" for x in profiles(browser))
            raise ToolError(f"No {browser} profile matching '{profile}'.", hint=f"Profiles: {have or 'none found'}")
        args.append(f"--profile-directory={p['folder']}")
        label = f" (profile {p['name']}{', ' + p['account'] if p['account'] else ''})"
    if url:
        args.append(url)
    subprocess.Popen(args, close_fds=True, creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
    return f"Opened {url or browser.title()} in {browser.title()}{label}."


@tool(risk="low", readonly=True, tags=["chrome profile", "profiles", "account", "personal account", "school account",
                                       "which account", "switch account", "browser profile"],
      examples=["browser_profiles()", "browser_profiles(browser='edge')"])
def browser_profiles(browser: str = "chrome") -> dict:
    """The profiles in the user's Chrome / Edge / Brave: each one's name, signed-in account and whether it's personal
    or a managed school / work account. Open a page in one with open_url(url, browser, profile)."""
    b = browser.lower().strip()
    if b not in BROWSERS:
        raise ToolError("browser must be chrome, edge or brave.")
    return {"browser": b, "profiles": profiles(b)}
