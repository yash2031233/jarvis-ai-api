<div align="center">

# J.A.R.V.I.S. — AI API Edition

**An open-source, voice-first desktop AI assistant with fast agentic tools and a sci-fi UI.**
**Bring your own brain:** NVIDIA (build.nvidia.com), Ollama, or any OpenAI-compatible API.

![Status](https://img.shields.io/badge/status-in%20development-orange)
![License](https://img.shields.io/badge/license-MIT-blue)
![Platforms](https://img.shields.io/badge/platforms-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey)
![Python](https://img.shields.io/badge/python-3.11%2B-3776AB)

</div>

---

> 🚧 **Early development.** The design is done — see the full [plan & roadmap](docs/PLAN.md). Code is coming next.

## What is it?

Jarvis is the **hands, ears, voice and face** of an AI assistant. You plug in the **brain** — paste an API key
in the settings screen and pick a model. No subscription, no lock-in.

- 🧠 **Bring your own model** — NVIDIA build.nvidia.com by default (free API key), plus Ollama (fully local) or any OpenAI-compatible endpoint
- 🖐️ **Fast agentic tools** — controls apps, files, browser, media and system; plans, acts and verifies its work
- 🎙️ **Local voice** — "Hey Jarvis" wake word, Whisper speech-to-text, Kokoro/Piper text-to-speech, interrupt any time
- ⚡ **Built for speed** — common commands run instantly without an LLM call; tools run in parallel
- 🖥️ **Sci-fi HUD** — animated orb, waveform, live tool activity panel
- 🔒 **Safe by default** — risky actions need confirmation, file changes are undoable, API keys live in your OS keychain
- 🧩 **Extensible** — drop a Python file in `plugins/` to add a new tool or skill

## Why another assistant?

The **agentic engine is the flagship.** Goals:

| | Target |
|---|---|
| Simple commands ("open Spotify", "volume 40") | **< 150 ms**, no LLM call |
| Voice: end of speech → first spoken word | **< 1.5 s** |
| Multi-step tasks | Plan → act → **verify** → self-correct |
| Weak or cheap models | Tool-call repair + ReAct fallback so they still work as agents |

Every claim will be backed by a public benchmark suite (`bench/`).

## How it will work

```
 "Hey Jarvis"  ─►  Whisper (local)  ─►  Fast-path router ──► instant action
                                            │
                                            ▼
                             Your model (NVIDIA / Ollama / any API)
                                            │
                              Plan → parallel tools → verify
                                            │
                     Kokoro / Piper (local)  ◄──  streamed reply
```

## Planned quick start

1. Download the installer for your OS from **Releases**
2. Get a free API key at [build.nvidia.com](https://build.nvidia.com) (or point it at Ollama)
3. Paste the key in the setup wizard → pick a model → *"Good evening. Jarvis online."*

## Hardware

- **CPU only:** works — uses lighter voice models
- **NVIDIA GPU:** auto-detected for fastest voice (Blackwell / RTX 50-series need CUDA 12.8+)
- **Apple Silicon / AMD:** on the roadmap

## Roadmap

See **[docs/PLAN.md](docs/PLAN.md)** for the full feature list, v1 scope and roadmap.

## Contributing

Ideas, issues and PRs are welcome once the first code lands. Plugins and skills are the easiest place to start.

## License

[MIT](LICENSE)
