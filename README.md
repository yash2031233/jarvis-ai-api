<div align="center">

# J.A.R.V.I.S. — AI API Edition

**An open-source, voice-first desktop AI assistant with fast agentic tools and a living orb UI.**
**Bring your own brain:** NVIDIA (build.nvidia.com), Ollama, or any OpenAI-compatible API.

![Status](https://img.shields.io/badge/status-alpha-orange)
![License](https://img.shields.io/badge/license-MIT-blue)
![Platforms](https://img.shields.io/badge/platforms-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey)
![Python](https://img.shields.io/badge/python-3.10%2B-3776AB)

<img src="docs/reference/orb-reference.webp" width="360" alt="The Jarvis orb">

</div>

---

## What is it?

Jarvis is the **hands, ears, voice and face** of an AI assistant. You plug in the **brain**: paste an API key
in Settings and pick a model. No subscription, no lock-in.

- 🧠 **Bring your own model**: NVIDIA build.nvidia.com by default (free key), Ollama for fully local, or any OpenAI-compatible endpoint
- 🖐️ **42 built-in tools**: apps, files, web, browser automation, media, volume, timers, clipboard, shell, Python, weather, notes, skills
- ⚡ **Instant commands**: "open Spotify", "volume 40", "timer 5 minutes" run in milliseconds with **no LLM call**
- 🎙️ **Local voice**: "Hey Jarvis" wake word, Whisper speech-to-text, Kokoro text-to-speech, interrupt any time
- 🔮 **The orb**: a real-time 3D (WebGL2) amber "molecular circuit" sphere that listens, thinks and speaks with you. Drag it to spin it
- 🔒 **Safe by default**: risky actions ask first, file changes are undoable, API keys live in your OS keychain
- 🧩 **Extensible**: drop a Python file in `plugins/` to add a tool, or save multi-step tasks as skills

## Quick start

### Windows: double-click

```bash
git clone https://github.com/yash2031233/jarvis-ai-api
```

Then open the folder and **double-click `Jarvis.exe`**.

- **First time:** a setup window installs everything (Python too, if you don't have it), adds **Jarvis** to your
  Desktop and Start Menu, and starts it. Takes a few minutes.
- **After that:** `Jarvis.exe` (or the shortcut) just starts Jarvis.
- NVIDIA GPUs are detected automatically and get GPU speech recognition.

> `Jarvis.exe` is a tiny open-source launcher ([source](installer/Launcher.cs), rebuild with
> `installer\build-launcher.ps1`). It runs [`installer/install.ps1`](installer/install.ps1) on first launch.
> If you downloaded the repo as a ZIP instead of cloning, Windows SmartScreen may warn about an unrecognised app:
> click **More info → Run anyway**.

### macOS / Linux

```bash
git clone https://github.com/yash2031233/jarvis-ai-api
cd jarvis-ai-api
./install.sh
```

### Then

1. Get a free API key at **[build.nvidia.com](https://build.nvidia.com)** (or install [Ollama](https://ollama.com) for local models)
2. Jarvis opens on the Settings screen: paste the key → **Save** → a model is picked for you
3. Say **"Hey Jarvis"**, click the orb, or type

On first launch the voice models download once (~400 MB on CPU, ~2 GB with the GPU Whisper model).
Blackwell GPUs (RTX 50-series / RTX PRO 6000) need a driver with CUDA 12.8+.

<details><summary>Manual install (developers)</summary>

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate    macOS/Linux: source .venv/bin/activate
pip install -e ".[all]"          # add ,gpu for NVIDIA GPU speech recognition
jarvis
```
</details>

Other launch options: `jarvis --browser` (UI in your browser), `jarvis --headless` (server only),
`jarvis --no-voice`, `jarvis -v` (debug logs).

## How it works

```
 "Hey Jarvis" ─► Whisper (local) ─► Fast-path router ──────────────► instant action (no LLM)
                                        │ (anything more complex)
                                        ▼
                         Your model (NVIDIA / Ollama / any API)
                                        │  only the relevant tools are sent
                                        ▼
               tool calls stream → safe tools start immediately → independent calls run in parallel
                                        │
                     results + verification ──► model self-corrects on errors
                                        │
                  Kokoro (local) speaks sentence-by-sentence while the reply streams
```

What makes the hands fast and reliable:

| | |
|---|---|
| **Fast path** | Common commands are matched locally and executed in ~1 ms, no API call |
| **Dynamic tool loading** | Only ~15 relevant tools are sent per request → smaller prompt, faster first token |
| **Streaming execution** | Read-only tools start while the model is still writing its turn |
| **Parallel tools** | Independent calls run concurrently |
| **Verify + self-correct** | Tools return structured errors with hints; actions are verified (e.g. the file exists) |
| **Tool-call repair** | Fixes malformed JSON and fuzzy tool names; text-based fallback for models without function calling |
| **Warm indexes** | Installed apps and your files are indexed in the background for instant lookup |
| **Undo journal** | Every file write/move/delete can be undone ("Jarvis, undo that") |
| **Injection guard** | Web pages, files and command output are marked untrusted; instructions inside them are ignored |

Measured on the dev machine (RTX A6000): fast-path commands **~1 ms**, Whisper transcription **~130 ms** (GPU),
first spoken audio **~275 ms** after a sentence is ready.

## Tools

| Category | Tools | Default permission |
|---|---|---|
| Apps | open, close, focus, list installed/running | auto (close: ask) |
| Files | search (instant index), read (txt/pdf/docx), list, open, write, move, create folder, delete, undo | read: auto · change: ask |
| Web | search, open URL, read page | auto |
| Browser agent | open, click, fill, read (Playwright) | auto (click/fill: ask) |
| System | info (battery/CPU/RAM/disk/GPU), dark mode, lock/sleep/restart/shutdown | power: ask |
| Media | play/pause/next/previous, volume/mute | auto |
| Productivity | timers, reminders/alarms, notes/todos/facts, clipboard, calculator, weather | auto |
| Power user | shell commands, Python runner | ask (shell can be disabled) |
| Skills | save last task as skill, create skill, list skills | auto |

Every tool's permission (auto / ask / off) is configurable in **Settings → Hands**.

## Skills

Do something multi-step, then say *"save that as work setup"*. Next time just say *"work setup"*.
Skills are JSON files in your data folder; see [`plugins/skills/focus-mode.json`](plugins/skills/focus-mode.json).

## Plugins

```python
# plugins/my_tool.py
from jarvis.hands import tool, ToolError

@tool(risk="low", tags=["dice", "roll"])
def roll_dice(sides: int = 6, count: int = 1) -> dict:
    """Roll one or more dice."""
    ...
```

The function signature becomes the schema the model sees. See [`plugins/example_dice.py`](plugins/example_dice.py).

## Benchmark

```bash
python -m bench.run --model meta/llama-3.3-70b-instruct
```

37 real desktop tasks in three tiers (instant / single-tool / multi-step) measuring success rate, latency and tool
calls. Side-effecting tools are stubbed so it's safe to run. Results are saved to `bench/results/`.

## Privacy & security

- The API key is stored in your OS keychain (Windows Credential Manager / macOS Keychain / Secret Service), never in plain text
- Voice is processed **locally**: audio never leaves your machine; only the transcribed text goes to your model provider
- The clipboard is only shared with the model when you refer to it ("summarize *this*")
- The local server binds to `127.0.0.1` and requires a per-launch token
- File deletes go to Jarvis's own trash and can be undone

## Development

```bash
pip install -e ".[all,dev]"
pytest -q                      # 64 tests, no network needed
jarvis --browser -v
```

The orb fidelity tool (dev only, not part of the app UI) is at `http://127.0.0.1:<port>/compare`:
side-by-side vs. the reference image, overlay slider, difference view, similarity scores and a blind A/B test.

Project layout: `jarvis/brain` (model client + tool-call repair) · `jarvis/router` (fast path) ·
`jarvis/agent` (engine, skills, context) · `jarvis/hands` (tools) · `jarvis/voice` · `jarvis/safety` ·
`jarvis/memory` · `jarvis/server` · `jarvis/ui`. Full plan: [docs/PLAN.md](docs/PLAN.md) · orb spec: [docs/UI_ORB.md](docs/UI_ORB.md).

## License

[MIT](LICENSE)
