# Benchmark tasks

The task list lives in [`tasks.json`](tasks.json). Each task has:

- `prompt` — what the user says
- `expect_tools` — tools that must be called (any order) for success
- `forbid_tools` — tools that must NOT be called
- `tier` — `fast` (should hit the no-LLM fast path), `single` (one tool), `multi` (several steps)

Run with `python -m bench.run --model <model>`; results go to `bench/results/`.
Destructive tools are replaced with dry-run stubs during benchmarking, so it is safe to run.
