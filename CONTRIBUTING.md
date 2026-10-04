# Contributing

Thanks for helping make Jarvis better!

## Setup

```bash
python -m venv .venv && . .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[all,dev]"
pytest -q
jarvis --browser -v
```

## Easy first contributions

- **New tools**: add a file in `plugins/` (see `plugins/example_dice.py`). If it's broadly useful, move it into
  `jarvis/hands/` and register it in `load_builtin_tools()`.
- **Fast-path commands**: add a rule to `jarvis/router/fastpath.py` plus a test in `tests/test_router.py`.
- **Benchmark tasks**: add realistic tasks to `bench/tasks.json`.
- **Platform support**: macOS/Linux adapters in `jarvis/hands/*` get less testing than Windows.

## Guidelines

- Tools must have a clear docstring, typed parameters and an honest `risk` level (`low`/`medium`/`high`).
- Anything that changes files must go through the undo journal (`jarvis/safety/undo.py`).
- Raise `ToolError(message, hint=...)` with a hint the model can act on.
- Never log or persist API keys. Never send clipboard/screen content to the model unless the user refers to it.
- Keep `pytest -q` green; add tests for new behavior.
