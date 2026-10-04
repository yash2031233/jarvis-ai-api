"""Settings storage.

Non-secret settings live in a JSON file in the user's config dir.
The API key lives in the OS keychain (Windows Credential Manager, macOS Keychain,
Secret Service on Linux) and is never written to disk in plain text or logged.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Literal

from platformdirs import user_config_dir, user_data_dir
from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

APP_NAME = "jarvis-ai-api"
KEYRING_SERVICE = "jarvis-ai-api"
KEYRING_USER = "api-key"

CONFIG_DIR = Path(os.environ.get("JARVIS_CONFIG_DIR") or user_config_dir(APP_NAME, appauthor=False))
DATA_DIR = Path(os.environ.get("JARVIS_DATA_DIR") or user_data_dir(APP_NAME, appauthor=False))
SETTINGS_FILE = CONFIG_DIR / "settings.json"

PROVIDER_PRESETS: dict[str, dict[str, str]] = {
    "nvidia": {
        "label": "NVIDIA (build.nvidia.com)",
        "base_url": "https://integrate.api.nvidia.com/v1",
        "key_hint": "nvapi-...",
        "key_url": "https://build.nvidia.com",
    },
    "lmstudio": {
        "label": "LM Studio (local)",
        "base_url": "http://localhost:1234/v1",
        "key_hint": "not needed",
        "key_url": "https://lmstudio.ai",
    },
    "ollama": {
        "label": "Ollama (local)",
        "base_url": "http://localhost:11434/v1",
        "key_hint": "not needed",
        "key_url": "https://ollama.com",
    },
    "custom": {
        "label": "Custom (OpenAI-compatible)",
        "base_url": "",
        "key_hint": "sk-...",
        "key_url": "",
    },
}

Permission = Literal["auto", "ask", "off"]


class Settings(BaseModel):
    provider: str = "nvidia"
    base_url: str = PROVIDER_PRESETS["nvidia"]["base_url"]
    model: str = ""
    temperature: float = 0.4
    max_tokens: int = 2048
    personality: str = (
        "You are J.A.R.V.I.S., a calm, precise and slightly dry-witted desktop assistant. "
        "Keep spoken replies short (1-3 sentences) unless asked for detail."
    )
    user_name: str = ""

    # Voice
    voice_enabled: bool = True
    voice_engine: Literal["auto", "gpu", "cpu", "cloud"] = "auto"
    tts_voice: str = "bm_george"
    tts_speed: float = 1.0
    wake_word: bool = True
    wake_phrase: str = "jarvis"      # also wake on this word at the start of what you say ("" = only "hey Jarvis")
    mic_device: int | None = None
    speaker_device: str = ""        # output device name ("" = the system default)
    hotkey: str = "ctrl+space"

    # CAD (OpenSCAD)
    openscad_path: str = ""          # empty = auto-detect
    bed_mm: list[int] = Field(default_factory=lambda: [220, 220, 220])  # printer build volume (x, y, z)
    cad_review: bool = True          # vision self-check of each design (needs a vision-capable model)

    # Cameras: [{"id", "name", "kind": "device"|"ip", "device": int, "url_display": str}]
    # (an IP camera's full URL - often with a password in it - lives in the OS keychain)
    cameras: list[dict] = Field(default_factory=list)
    default_camera: str = ""
    auto_webcam: bool = True         # offer the first webcam without setting it up

    # Memory
    vault_path: str = ""             # empty = <data>/vault; or point at an existing Obsidian vault
    memory_keeper: bool = True       # file lasting facts from conversations into the vault by itself
    learn: bool = True               # learn lessons from experience (reflection -> principles in every prompt)

    # Proactivity
    proactive: bool = True           # rule checks (rain, tomorrow's tests, long gaming, battery) - no model calls
    heartbeat_min: int = 0           # 0 = off; else every N minutes the model decides if anything is worth saying
    proactive_gap_min: int = 20      # at most one unprompted message per this many minutes
    quiet_start: str = "22:30"
    quiet_end: str = "07:00"
    home_city: str = ""              # for rain alerts (empty = your live location, else detect from IP)

    # Location / navigation (the Telegram bot token and a Google Maps key live in the keychain)
    telegram_chat_id: int | None = None   # the paired chat - the only one the bot listens to
    nav_voice: bool = True           # speak turn-by-turn directions while navigating
    nav_units: str = "imperial"      # imperial (miles/feet) | metric (km/m) for spoken directions
    briefing_time: str = ""          # e.g. "07:30" - morning briefing while Jarvis is running (empty = off)
    diary_time: str = "23:30"        # nightly diary into the vault (empty = off)

    # Finding files by content
    index_folders: list[str] = Field(default_factory=list)   # empty = Documents, Desktop, Downloads
    embed_model: str = ""            # empty = auto-detect an embeddings model from the provider (else keywords only)

    # Hands
    tool_permissions: dict[str, Permission] = Field(default_factory=dict)
    shell_enabled: bool = True
    fast_path: bool = True
    max_agent_steps: int = 12

    def public(self) -> dict[str, Any]:
        return self.model_dump()


class _Store:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._settings: Settings | None = None

    def load(self) -> Settings:
        with self._lock:
            if self._settings is None:
                self._settings = self._read()
            return self._settings

    def _read(self) -> Settings:
        if SETTINGS_FILE.exists():
            try:
                return Settings.model_validate_json(SETTINGS_FILE.read_text("utf-8"))
            except Exception as e:  # corrupted file -> defaults, keep a backup
                log.warning("settings file unreadable (%s); using defaults", e)
                SETTINGS_FILE.replace(SETTINGS_FILE.with_suffix(".bak"))
        s = Settings()
        # Dev convenience: .env-style environment overrides (never persisted for the key)
        if os.environ.get("JARVIS_BASE_URL"):
            s.base_url = os.environ["JARVIS_BASE_URL"]
        if os.environ.get("JARVIS_PROVIDER"):
            s.provider = os.environ["JARVIS_PROVIDER"]
        if os.environ.get("JARVIS_MODEL"):
            s.model = os.environ["JARVIS_MODEL"]
        return s

    def save(self, s: Settings) -> None:
        with self._lock:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            tmp = SETTINGS_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(s.model_dump(), indent=2), "utf-8")
            tmp.replace(SETTINGS_FILE)
            self._settings = s

    def update(self, **changes: Any) -> Settings:
        s = self.load().model_copy(update=changes)
        s = Settings.model_validate(s.model_dump())
        self.save(s)
        return s


store = _Store()


# ---------------------------------------------------------------- API key

# Secrets live in the OS keychain (Windows Credential Manager / macOS Keychain / Secret Service). Some Linux setups
# have no keychain running (no GNOME Keyring / KWallet); then they go in a file only this user can read.
SECRETS_FILE = CONFIG_DIR / "secrets.json"


def _file_secrets() -> dict[str, str]:
    try:
        return json.loads(SECRETS_FILE.read_text("utf-8"))
    except Exception:
        return {}


def _write_file_secrets(d: dict[str, str]) -> None:
    SECRETS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = SECRETS_FILE.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(d, f)
    os.replace(tmp, SECRETS_FILE)


def _secret_get(name: str) -> str:
    try:
        import keyring

        v = keyring.get_password(KEYRING_SERVICE, name)
        if v:
            return v
    except Exception as e:
        log.debug("keychain unavailable: %s", e)
    return _file_secrets().get(name, "")


def _secret_set(name: str, value: str) -> None:
    stored = False
    try:
        import keyring

        if value:
            keyring.set_password(KEYRING_SERVICE, name, value)
            stored = True
        else:
            try:
                keyring.delete_password(KEYRING_SERVICE, name)
            except Exception:
                pass
    except Exception as e:
        log.warning("OS keychain unavailable (%s); keeping %s in %s (only your user can read it)", e, name, SECRETS_FILE)
    d = _file_secrets()
    if value and not stored:
        d[name] = value
    else:
        d.pop(name, None)
    if d or SECRETS_FILE.exists():
        _write_file_secrets(d)


def get_api_key() -> str:
    env = os.environ.get("JARVIS_API_KEY", "").strip()
    if env:
        return env
    return _secret_get(KEYRING_USER)


def set_api_key(key: str) -> None:
    _secret_set(KEYRING_USER, key.strip())


def get_secret(name: str) -> str:
    return _secret_get(name)


def set_secret(name: str, value: str) -> None:
    _secret_set(name, value)


def mask_url(url: str) -> str:
    """rtsp://user:pass@host/x -> rtsp://user:••••@host/x"""
    import re

    return re.sub(r"(://[^:/@]+):[^@/]+@", r"\1:••••@", url)


def mask_key(key: str) -> str:
    if not key:
        return ""
    if len(key) <= 10:
        return "••••"
    prefix = key[:6] if key.startswith(("nvapi-", "sk-")) else key[:3]
    return f"{prefix}••••{key[-4:]}"


def load_dotenv(path: Path | None = None) -> None:
    """Minimal .env loader for development (no dependency)."""
    path = path or Path.cwd() / ".env"
    if not path.exists():
        return
    for line in path.read_text("utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
