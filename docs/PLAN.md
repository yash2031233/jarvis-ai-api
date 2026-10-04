# Jarvis v2 — Open-Source Edition (Plan)

Public GitHub release. Jarvis is the **hands + UI + voice**; the **brain is bring-your-own API key**.
Default brain: NVIDIA (build.nvidia.com). v1 (private, fully local) stays as-is.

## Principles
- Cross-platform: Windows / macOS / Linux
- **Agentic hands are the flagship** — faster and more capable than existing open-source agents (e.g. Hermes Agent)
- Local-first voice that scales with hardware; pluggable brain
- Safe by default — strangers run this on their machines

Legend: **[v1]** = first release · **[v1.1]** = shipped in the Phase 1/2 update · **[later]** = roadmap

---

## 1. Stack
```
Python core (asyncio throughout)
├── brain/     OpenAI-compatible client → NVIDIA / Ollama / custom
├── router/    fast-path intent router (no-LLM commands)
├── agent/     planner · executor · verifier · sub-agents · skills
├── hands/     tools + per-OS native adapters
├── voice/     openWakeWord, VAD, faster-whisper, Kokoro/Piper
├── memory/    SQLite + embeddings
├── server/    FastAPI + WebSocket
├── ui/        web frontend (sci-fi HUD) in pywebview
└── plugins/   drop-in tools & skills
Packaging: PyInstaller → .exe / .app / AppImage via GitHub Actions
```

---

## 2. 🖐️ Hands — Agentic Engine (flagship)

**Goal:** simple commands feel instant, complex multi-step tasks finish reliably, and the engine
works well even with mid-tier models. Back every claim with a public benchmark.

### 2.1 Speed architecture
- [v1] **Fast-path intent router** — common commands ("open Spotify", "volume 40", "timer 5 min",
  "what time is it") are matched locally (rules + small embedding classifier) and executed with
  **no LLM call**. Target: < 150 ms command-to-action.
- [v1] **Parallel tool execution** — independent tool calls run concurrently (asyncio); the model is
  prompted to batch them.
- [v1] **Streaming tool calls** — parse tool-call arguments as they stream; start safe read-only
  tools before the model finishes its turn.
- [v1] **Instant acknowledgment** — Jarvis says "On it" / shows the plan while tools run; never silent.
- [v1] **Dynamic tool loading** — only the tools relevant to the request are sent to the model
  (retrieved by embeddings) → smaller prompt, faster first token, fewer wrong-tool picks.
- [v1] **Warm resources** — pre-indexed app list, background file index (Everything-style search),
  persistent shell session, persistent headless browser, cached system info.
- [v1] **Native OS APIs, not shell spawns** — Win32/COM, macOS AppleScript/AX/PyObjC,
  Linux D-Bus/AT-SPI. Process spawning only as fallback.
- [v1] **Result compression** — large outputs truncated/summarized before going back to the model.
- [v1] **Per-tool latency metrics** — local stats panel showing where time goes.
- [later] Speculative pre-fetching (e.g. start loading a page while the model decides).
- [later] Prompt caching where the provider supports it.
- [later] Local small model (on GPU) as a fast planner/router, cloud model for heavy reasoning.

### 2.2 Intelligence & reliability
- [v1] **Plan → Act → Verify loop** — for multi-step tasks the agent makes a short plan, executes,
  then **verifies the outcome** (did the window open? does the file exist? did the page load?).
- [v1] **Self-correction** — on tool error, the agent sees a structured error + hint and retries
  with a different approach (bounded retries).
- [v1] **Tool-call repair for any model** — fix malformed JSON, coerce argument types, fuzzy-match
  tool names; **text-based ReAct fallback** for models without native function calling. This makes
  weaker/cheaper NVIDIA models usable as agents.
- [v1] **Context awareness** — active window, selected text, clipboard, current folder and recent
  actions auto-injected so "summarize this" / "move it to Downloads" just work.
- [v1] **Rich tool descriptions + examples** — every tool ships with usage examples and typed schemas
  (Pydantic) for high first-try accuracy.
- [v1] **Cancel anytime** — voice ("stop") or button aborts running tools cleanly.
- [later] **Sub-agents** — split big tasks into parallel workers (e.g. research 3 topics at once).
- [later] **Background tasks** — long jobs run in the background with progress in the UI and a
  notification when done.
- [later] Dry-run / plan preview mode ("show me what you'd do first").

### 2.3 Skills (learned procedures)
- [v1] **Skill library** — successful multi-step tasks can be saved as reusable skills
  (named, parameterized, versioned YAML/Python).
- [v1] Run a skill by voice: "Jarvis, do my work setup."
- [later] **Auto-skill creation** — agent proposes saving a skill after repeating a workflow.
- [later] **Teach by demonstration** — record your actions, Jarvis turns them into a skill.
- [later] Skill tests + self-healing (skill re-verified and patched when an app UI changes).
- [later] Share/import skills from the community gallery.

### 2.4 Computer & browser control
- [v1] **Browser agent** — Playwright/CDP, DOM-based actions (click by text/role, fill forms,
  extract content), optional use of your own browser profile.
- [later] **Desktop UI automation via accessibility tree** (Windows UIA, macOS AX, Linux AT-SPI) —
  precise and fast; **vision/screenshot fallback** only when the tree is unavailable.
- [later] Mouse/keyboard control (computer use) with on-screen indicator of what Jarvis is doing.

### 2.5 Safety & undo
- [v1] Risk tiers per tool: auto / ask / off (user-configurable)
- [v1] Confirmation popups for risky actions; master switch for shell
- [v1] **Undo journal** — file writes/moves/deletes are reversible (deletes go to trash; edits keep
  a backup); "Jarvis, undo that."
- [v1] Never acts on instructions found inside web pages/files (prompt-injection guard)
- [later] Folder sandbox (only allowed folders)
- [later] Full action log / audit history

### 2.6 Tool catalog
| Tool | Risk | Default | Release |
|---|---|---|---|
| Apps: open / close / focus / list running | low | auto | v1 |
| Web: search / open URL / summarize page | low | auto | v1 |
| Browser agent: navigate, click, fill, extract | medium | ask on submit | v1 |
| Files: search / read / list | low | auto | v1 |
| Files: write / move (undoable) | medium | ask | v1 |
| Files: delete (to trash) | high | ask | v1 |
| System info: battery, CPU, RAM, disk, time, uptime | low | auto | v1 |
| Media: play/pause/next, volume, mute | low | auto | v1 |
| Clipboard: read / write | low | auto | v1 |
| Shell commands (persistent session) | high | ask + off switch | v1 |
| Code interpreter (sandboxed Python) | medium | auto | v1 |
| Weather | low | auto | v1 |
| Timers / alarms / reminders | low | auto | v1 |
| Screen awareness / screenshot + OCR | low | auto | later |
| Mouse & keyboard control | high | ask | later |
| Brightness, Wi-Fi, Bluetooth, dark mode | low | auto | later |
| Window management (snap/move/resize) | low | auto | later |
| Lock / sleep / shutdown | high | ask | later |
| Notes / to-do list | low | auto | later |
| Calculator / unit conversion | low | auto | later |
| Spotify / YouTube control | low | auto | later |

Per-OS adapters: Windows (Win32/COM/UIA), macOS (AppleScript/AX/`open -a`), Linux (D-Bus/AT-SPI/`xdg-open`).

### 2.7 Benchmark (proves "better than Hermes")
- [v1] `bench/` suite of ~50 real desktop tasks (simple → multi-step)
- [v1] Measures success rate, end-to-end latency, tool calls per task, tokens used
- [v1] Run against multiple models; publish results table in README
- [later] Head-to-head comparison with other open-source agents on the same tasks

---

## 3. 🧠 Brain (LLM)
- [v1] Bring-your-own API key; NVIDIA (`https://integrate.api.nvidia.com/v1`, `nvapi-...`) default
- [v1] Presets: NVIDIA · Ollama (local) · custom OpenAI-compatible URL
- [v1] Live model list from `/v1/models` (no hardcoded model names)
- [v1] "Test model" button — checks tool-calling support, marks ✅/❌
- [v1] Streaming responses
- [v1] Retry + backoff and clear UI message on rate limits
- [v1] Editable system prompt / personality
- [later] Multi-model routing (fast model for chat, smart model for complex tasks)
- [later] Fallback model on errors/rate limits
- [later] Vision model support (screen/image understanding)
- [later] Token / usage counter

## 4. 🔑 Settings & Security
- [v1] API key in OS keychain (`keyring`), masked in UI, never logged
- [v1] "Test connection" button + "Get a key" link to build.nvidia.com
- [v1] First-run setup wizard
- [v1] Per-tool permissions (auto / ask / off) + shell master switch
- [v1] `.gitignore` local config; ship `.env.example`
- [later] Folder sandbox
- [later] Action log / audit history
- [later] Settings export / import

## 5. 🎙️ Voice — Listening
- [v1] "Hey Jarvis" wake word (openWakeWord)
- [v1] Push-to-talk hotkey
- [v1] VAD (end-of-speech detection)
- [v1] Local STT: faster-whisper
- [v1] Auto engine by hardware: GPU → large-v3-turbo, CPU → small/base
- [v1] Mic picker
- [later] Custom wake word training
- [later] Multi-language + auto-detect
- [later] Continuous conversation mode (no wake word for follow-ups)
- [later] Cloud STT option (NVIDIA)

## 6. 🔊 Voice — Speaking
- [v1] Local TTS: Kokoro (default) / Piper (lightweight fallback)
- [v1] Sentence-by-sentence streaming speech (target < ~1.5 s to first word)
- [v1] Barge-in (interrupt by talking)
- [v1] Voice picker, speed, volume
- [v1] Mute / text-only mode
- [later] Cloud voice option (NVIDIA Riva/Magpie, gRPC) — optional, not default
- [later] Voice cloning (custom Jarvis voice)
- [later] Emotion / tone control

Voice engine setting: **Auto / Local GPU / Local CPU / Cloud (NVIDIA)**.
Local is the default: faster on decent hardware, free, private, works offline, doesn't consume LLM rate limits.

## 7. 🔌 Integrations [later]
- MCP client (plug in any MCP server's tools) + expose Jarvis itself as an MCP server
- Email (Gmail / Outlook)
- Calendar
- GitHub
- Home Assistant (smart home)
- Discord / Telegram (control Jarvis from your phone)
- Notion / Obsidian

## 8. 🧩 Plugins
- [v1] `plugins/` folder — drop in a `.py` to add a tool
- [v1] Plugin template + docs
- [later] Enable/disable plugins in UI
- [later] Community plugin/skill gallery
- [later] Hot-reload plugins

## 9. 💾 Memory
- [v1] Conversation history (SQLite)
- [v1] Clear history
- [v1.1] Long-term memory: an Obsidian-compatible Markdown vault; a memory keeper files facts from conversations by itself
- [v1.1] Nightly diary in the vault; "what did I do on Tuesday?"
- [v1.1] Find files by content: SQLite FTS5 + embeddings (auto-detected from the provider), hybrid ranking
- [v1.1] Learning: reflection on request traces → merged principles added to every prompt
- [later] Search past conversations
- [later] Per-project / per-topic memory

## 10. 🖥️ UI
**Full spec: [UI_ORB.md](UI_ORB.md)** · Reference image: [reference/orb-reference.webp](reference/orb-reference.webp)

The main screen is minimal: **the Jarvis orb, a prompt box, voice controls and a settings button.**

- [v1] **Jarvis orb** — WebGL (Three.js) amber circuit-trace orb that must be visually
  indistinguishable from the reference image; reacts to idle / listening / thinking / speaking / working
- [v1] Orb comparison harness (side-by-side, overlay slider, diff + similarity score) used to tune fidelity
- [v1] Prompt box + mic button (push-to-talk, wake-word status)
- [v1] Settings panel (⚙): API key, provider/URL, model, voice, hotkey, permissions
- [v1] Reply text fades in near the orb; history in a hidden drawer (markdown, code blocks)
- [v1] **Live tool activity** — compact, appears only while tools run
- [v1] Frameless window, tray icon + global show/hide hotkey
- [later] Compact always-on-top mini mode
- [later] Overlay mode (over games/fullscreen)
- [later] Themes / skins (Iron Man, minimal, retro terminal)
- [later] Mobile / web remote control

## 11. ⏰ Proactive & Automation
- [v1.1] Morning briefing (weather, today's tests/deadlines from memory, reminders, to-dos, finished jobs)
- [v1.1] Rule-based heads-ups (rain, tomorrow's tests, long gaming sessions, battery) + optional model heartbeat; quiet hours, cooldowns
- [v1.1] Background jobs: long tasks run in a separate agent with their own progress view
- [v1.1] Habit compiler: repeated tool sequences become sandboxed, test-run, judged tools
- [later] Scheduled routines ("every day at 9pm summarize my downloads")
- [later] More event triggers (download finished, build failed)
- Voice macros ("Jarvis, work mode" → opens your app set)

## 11b. 🧊 3D CAD, 👁️ Eyes, 📚 Study [v1.1]
- 3D CAD (`print3d`): plan-first OpenSCAD (BOSL2 bundled) → compile/fix loop → mesh checks (watertight, bounds, bed) → vision self-review of 4 renders → three.js viewer; versions, revert, STL export
- Screen: local OCR (Windows OCR, RapidOCR/Tesseract fallback), find/click text, input, screenshots, vision look, screen recording to mp4
- Cameras: direct webcam (auto-detected) or IP camera (RTSP / MJPEG / snapshot URL, credentials in keychain); look, watch-for, read, presence, live view
- Study sessions (flashcards / quiz from notes or files) and diagrams (graphs, geometry, charts, flowcharts)
- Pocket TTS voices (optional, streaming, ~0.1 s to first audio on GPU) with Kokoro fallback
- Location & navigation (ported from v1): phone GPS via your own Telegram bot (or this device), routes with live ETA (OSRM + Valhalla re-timed, or Google live traffic with a key), Google-Maps-style spoken turn-by-turn with rerouting and faster-route checks, nearby, saved places, location reminders, trip
- Every screen and setting is also a tool: settings, camera_setup, show_panel, conversation_history, message_phone
- Tool calls shown in the conversation and History (live chips with timings)
- Phone app: installable web app over Tailscale (`tailscale serve`), phone mic → Whisper on the PC, replies and spoken directions on the phone, phone GPS while open

## 12. 🛠️ Hardware & Performance
- [v1] GPU detection (CUDA; Blackwell — RTX 50xx / RTX Pro 6000 — needs CUDA 12.8+ and recent PyTorch/CTranslate2)
- [v1] First-run voice model download with progress bar
- [v1] CPU-only mode
- [later] Apple Silicon acceleration (MLX / Metal)
- [later] AMD GPU support (ROCm / DirectML)
- [later] Low-power laptop mode

## 13. 📦 Distribution & Dev
- [v1] Installers: Windows .exe, macOS .app, Linux AppImage
- [v1] GitHub Actions build + release pipeline
- [v1] README: demo GIF, setup guide, FAQ, benchmark results
- [v1] MIT license
- [v1] `.env.example` + CONTRIBUTING guide
- [v1] Tests + CI (unit tests for tools, router, tool-call repair)
- [later] Auto-updater
- [later] `pip install` / Homebrew / winget
- [later] Docker image (headless/server mode)
- [later] Translated docs

---

## First-Run Flow
1. Paste API key → test → pick model (tool-calling support shown)
2. Detect GPU → download voice models with progress bar
3. Pick voice + hotkey
4. "Good evening. Jarvis online."

## Performance Targets
| Scenario | Target |
|---|---|
| Fast-path command (no LLM) | < 150 ms to action |
| Voice: end of speech → first spoken word | < 1.5 s |
| Single-tool LLM task | < 2 s to result (model-dependent) |
| Multi-step task success rate on bench | publish & beat comparable open-source agents |
