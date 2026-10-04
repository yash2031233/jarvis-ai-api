<div align="center">

# J.A.R.V.I.S. — AI API Edition

**An open-source, voice-first desktop AI assistant with fast agentic tools and a living orb UI.**
**Bring your own brain:** NVIDIA (build.nvidia.com), LM Studio, Ollama, or any OpenAI-compatible API.

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

- 🧠 **Bring your own model**: NVIDIA build.nvidia.com by default (free key), LM Studio or Ollama for fully local, or any OpenAI-compatible endpoint
- 🖐️ **71 built-in tools**: apps, files, web, browser automation, screen reading and clicking, cameras, maps, media, timers, clipboard, shell, Python, weather, notes, skills - and every screen and setting of the app, so you can just ask
- ⚡ **Instant commands**: "open Spotify", "volume 40", "timer 5 minutes" run in milliseconds with **no LLM call**
- 🎙️ **Local voice**: "Hey Jarvis" wake word, Whisper speech-to-text, Kokoro or Pocket TTS, interrupt any time
- 🔮 **The orb**: a real-time 3D (WebGL2) amber "molecular circuit" sphere that listens, thinks and speaks with you. Drag it to spin it
- 🧭 **Maps & navigation**: "take me to the airport" → the fastest real route with a live ETA, then Google-Maps-style turn-by-turn that Jarvis **speaks before every turn**, reroutes when you miss one, and finds faster routes on the way. Nearby places, saved places, location reminders. Your phone is the GPS (through your own Telegram bot)
- 🧊 **3D CAD**: "design a phone stand" → OpenSCAD model built, mesh-checked and visually self-reviewed, shown in a 3D viewer you can spin; tweak it by voice, STL export
- 👁️ **Eyes**: reads and clicks anything on screen (local OCR), looks through your webcam or any IP camera, records the screen
- 🧠 **Memory that grows**: an Obsidian-compatible notes vault Jarvis files facts into by itself, a nightly diary, search your files by meaning, and lessons learned from its own mistakes
- 🕰️ **Proactive**: morning briefing, rain/test/battery heads-ups, quiet hours, long tasks run in the background
- 🔁 **Habits**: repeated tool sequences get compiled into real, sandboxed, tested tools
- 📱 **Phone app**: add it to your home screen over Tailscale - talk to Jarvis, hear him, and navigate with your phone's GPS, anywhere
- 🧾 **See what it did**: every reply shows the tools it called (live), with timings - in the conversation and the History panel
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
- **Updating:** `git pull`, then start Jarvis again. If the update needs new packages, `Jarvis.exe` installs them
  first; a Jarvis that's already open is replaced by the new one. Your settings, keys and memory are kept.
- NVIDIA GPUs are detected automatically and get GPU speech recognition.

> `Jarvis.exe` is a tiny open-source launcher ([source](installer/Launcher.cs), rebuild with
> `installer\build-launcher.ps1`). It runs [`installer/install.ps1`](installer/install.ps1) on first launch.
> If you downloaded the repo as a ZIP instead of cloning, Windows SmartScreen may warn about an unrecognised app:
> click **More info → Run anyway**.

### Linux / macOS

`Jarvis.exe` is Windows only. On Linux and macOS use the launcher next to it:

- **Linux:** double-click **`Jarvis.desktop`** (the first time, your file manager may ask: right-click →
  **Allow Launching** / "Trust and launch"). A terminal shows the setup; after that Jarvis is in your app menu.
- **macOS:** double-click **`Jarvis.command`** (first time: right-click → Open, if macOS asks).
- **Or from a terminal** (works everywhere):

```bash
git clone https://github.com/yash2031233/jarvis-ai-api
cd jarvis-ai-api
./install.sh
```

Needs Python 3.10–3.14 (the installer tells you the exact command if it's missing - on Debian/Ubuntu that's
usually `sudo apt install python3-venv`). For voice on Linux also `sudo apt install libportaudio2`. If your system
has no desktop-window support, Jarvis opens in your browser instead.

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
| Screen | read screen (OCR), find/click text, click, type, keys, scroll, screenshot, look at screen (vision), record video | read: auto · input: ask |
| Cameras | look, watch for something, read text, presence, snapshot — webcam or IP camera (RTSP / MJPEG / snapshot URL) | auto |
| Location | where am I, routes + ETA + spoken navigation, nearby, saved places, location reminders, trip | auto |
| The app itself | settings (voice, speed, model, quiet hours…), camera setup, open any screen, search past conversations, message your phone | auto (phone message: ask) |
| 3D CAD | design, tweak, view, versions/revert, export STL (`print3d`) | auto |
| Memory | remember/list/forget notes (vault), find files by content, briefing, diary | auto |
| Study & diagrams | flashcard/quiz study sessions; graphs, geometry, charts, flowcharts as images | auto |
| Background | `job` (long tasks in the background), habits (make/list/remove) | auto |
| Skills | save last task as skill, create skill, list skills | auto |

Every tool's permission (auto / ask / off) is configurable in **Settings → Hands**.

## 3D CAD

Say *"design a wall hook for a 20 mm rail"*. Jarvis plans the part, writes OpenSCAD (BOSL2 bundled), compiles
it, fixes its own errors, checks the mesh (watertight, fits your bed), renders it from four angles and has the model
check the renders against what you asked for. The result opens in a 3D viewer in the app. *"Make it 5 mm thicker"*
tweaks it; every change is a version you can go back to. Needs [OpenSCAD](https://openscad.org/downloads.html)
(a nightly build is fastest); the visual check needs a vision-capable model.

## Cameras

**Settings → Cameras**: your webcam works with no setup. Add an **IP camera** by URL: `rtsp://…`, an MJPEG stream
(`http://…/video`) or a snapshot URL (`http://…/snapshot.jpg`), with a username/password if it needs one. Camera
URLs are kept in your OS keychain. Then: *"is anyone at the front door?"*, *"tell me when the package arrives"*.

## Memory

Jarvis keeps an Obsidian-compatible Markdown vault (or point it at yours in **Settings → Memory**). After a quiet
spell it files lasting facts from your conversations into notes by itself, writes a short diary each night, and
reflects on what went wrong to keep a list of principles it follows. **Settings → Memory** can turn each part off.

## Phone app

Jarvis runs on your computer; your phone can use it as an app - its own mic, speaker and GPS - from anywhere,
privately, with [Tailscale](https://tailscale.com) (free):

1. Install Tailscale on the computer and the phone and sign in to the same account on both.
2. On the computer, once:
   ```bash
   tailscale serve --bg --https=8443 http://127.0.0.1:47821
   ```
   It prints your address, like `https://my-pc.tailXXXX.ts.net:8443` - reachable only by your own devices.
3. Open that address on the phone. iPhone: Share → **Add to Home Screen**. Android: ⋮ → **Install app**.

In the app, tap the mic and talk (it stops when you stop) - Jarvis's Whisper on the computer listens and his
voice answers from the phone; typed questions get typed answers. While the app is open, the phone's GPS keeps
"where am I", routes and ETAs current, and navigation speaks every turn from the phone. Keep the app open with
the screen on while navigating - phones pause web apps in the background. For location while the app is closed,
use the Telegram bot below too.

## Maps & navigation

Ask *"how long to drive to work"*, *"walk me to the nearest coffee shop"*, *"take me to the airport"*. Jarvis finds
the place, picks the fastest real route (OSRM and Valhalla routes re-timed on one yardstick - or Google's live-traffic
routes if you add a Google Maps key in Settings → Location), opens the map with a live ETA, and navigates: a tilted,
heading-up map that follows you, a big turn banner, and his voice before every turn - *"In half a mile, turn right
onto Route 9"* … *"Turn right onto Route 9"*. Leave the route and he reroutes; with traffic data he checks every few
minutes for a faster way; he tells you when you've arrived.

**Where you are** comes from your phone: make a Telegram bot with [@BotFather](https://t.me/BotFather), paste its
token in **Settings → Location**, press **Pair** and send the bot the code. Then share your live location with it
(📎 → Location → Share My Live Location). It works away from home - nothing needs to be reachable from the internet.
A laptop can also use its own location while the map is open. The bot also lets you message Jarvis from your phone,
and location reminders (*"remind me to buy milk when I get to the store"*) arrive there. Your location history stays
on your computer.

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
- Location: the history stays on your computer; place searches and routes go to OpenStreetMap services (or Google, if you add a key). The Telegram bot only listens to the one chat you pair
- OCR runs locally; screenshots and camera frames only go to your model when you ask Jarvis to *look* at something
- Habits are checked by an allow-list (no imports, no attributes outside a safe list) and every tool they call still goes through your permissions

## Development

```bash
pip install -e ".[all,dev]"   # optional: [pocket] for the Pocket TTS voices (needs PyTorch)
pytest -q                      # 153 tests, no network needed
jarvis --browser -v
```

The orb fidelity tool (dev only, not part of the app UI) is at `http://127.0.0.1:<port>/compare`:
side-by-side vs. the reference image, overlay slider, difference view, similarity scores and a blind A/B test.

Project layout: `jarvis/brain` (model client + tool-call repair) · `jarvis/router` (fast path) ·
`jarvis/agent` (engine, skills, jobs, habits, learning) · `jarvis/cad` · `jarvis/vision` · `jarvis/hands` (tools) · `jarvis/voice` · `jarvis/safety` ·
`jarvis/memory` · `jarvis/server` · `jarvis/ui`. Full plan: [docs/PLAN.md](docs/PLAN.md) · orb spec: [docs/UI_ORB.md](docs/UI_ORB.md).

## License

[MIT](LICENSE)
