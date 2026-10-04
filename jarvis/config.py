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
    mic_device: int | None = None
    hotkey: str = "ctrl+space"

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

def get_api_key() -> str:
    env = os.environ.get("JARVIS_API_KEY", "").strip()
    if env:
        return env
    try:
        import keyring

        return keyring.get_password(KEYRING_SERVICE, KEYRING_USER) or ""
    except Exception as e:
        log.warning("keychain unavailable: %s", e)
        return ""


def set_api_key(key: str) -> None:
    import keyring

    key = key.strip()
    if key:
        keyring.set_password(KEYRING_SERVICE, KEYRING_USER, key)
    else:
        try:
            keyring.delete_password(KEYRING_SERVICE, KEYRING_USER)
        except Exception:
            pass


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
