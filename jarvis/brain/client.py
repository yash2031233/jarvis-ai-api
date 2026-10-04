"""OpenAI-compatible brain client (NVIDIA build.nvidia.com, Ollama, or any compatible API).

Streams text and tool calls. Falls back to text-based tool calling (ReAct) for models
without native function calling, and repairs malformed tool calls either way.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from dataclasses import dataclass, field
from typing import Any

from openai import APIConnectionError, APIStatusError, AsyncOpenAI, RateLimitError

from .. import config
from .repair import ToolCall, extract_text_tool_calls, match_tool_name, normalize_args, react_tool_listing

log = logging.getLogger(__name__)


DEGENERATE = re.compile(r"([^\w\s])\1{15,}")   # 16+ of the same symbol in a row


class BrainError(Exception):
    def __init__(self, message: str, kind: str = "error") -> None:
        super().__init__(message)
        self.kind = kind  # auth | rate_limit | connection | model | error


@dataclass
class TurnResult:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str | None = None
    usage: dict[str, int] = field(default_factory=dict)


class Brain:
    def __init__(self) -> None:
        self._client: AsyncOpenAI | None = None
        self._client_sig: tuple[str, str] | None = None
        # model id -> True (native tools) / False (use ReAct fallback)
        self.native_tools: dict[str, bool] = {}

    # ------------------------------------------------------------- client
    def client(self) -> AsyncOpenAI:
        s = config.store.load()
        key = config.get_api_key() or ("local" if s.provider in ("ollama", "lmstudio") else "")
        if not key:
            raise BrainError("No API key set. Open Settings and paste your key.", "auth")
        if not s.base_url:
            raise BrainError("No base URL set. Open Settings and pick a provider.", "auth")
        sig = (s.base_url, key)
        if self._client is None or self._client_sig != sig:
            self._client = AsyncOpenAI(base_url=s.base_url, api_key=key, max_retries=0, timeout=90)
            self._client_sig = sig
        return self._client

    def model(self) -> str:
        m = config.store.load().model
        if not m:
            raise BrainError("No model selected. Open Settings and pick a model.", "model")
        return m

    # ------------------------------------------------------------- utilities
    async def list_models(self) -> list[str]:
        try:
            page = await self.client().models.list()
        except Exception as e:
            raise self._wrap(e) from e
        ids = sorted({m.id for m in page.data})
        return ids

    async def test_connection(self) -> dict[str, Any]:
        t0 = time.perf_counter()
        models = await self.list_models()
        return {"ok": True, "models": len(models), "latency_ms": int((time.perf_counter() - t0) * 1000)}

    async def test_tool_calling(self, model: str) -> dict[str, Any]:
        """Send a dummy tool and see whether the model calls it natively."""
        tool = {
            "type": "function",
            "function": {
                "name": "get_time",
                "description": "Get the current time in a city.",
                "parameters": {
                    "type": "object",
                    "properties": {"city": {"type": "string"}},
                    "required": ["city"],
                },
            },
        }
        t0 = time.perf_counter()
        try:
            r = await self.client().chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": "What time is it in Tokyo? Use the tool."}],
                tools=[tool],
                tool_choice="auto",
                max_tokens=200,
                temperature=0,
            )
        except Exception as e:
            err = self._wrap(e)
            if err.kind in ("auth", "rate_limit", "connection"):
                raise err from e
            self.native_tools[model] = False
            return {"native": False, "works": False, "detail": str(err)}
        msg = r.choices[0].message
        native = bool(msg.tool_calls)
        works = native
        if not native:
            calls, _ = extract_text_tool_calls(msg.content or "", ["get_time"])
            works = bool(calls)
        self.native_tools[model] = native
        return {
            "native": native,
            "works": works,
            "latency_ms": int((time.perf_counter() - t0) * 1000),
        }

    # ------------------------------------------------------------- main call
    async def stream_turn(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        on_text: Any = None,
        on_tool_call: Any = None,
        cancel: asyncio.Event | None = None,
        no_think: bool = False,
    ) -> TurnResult:
        """One model turn. Streams text through on_text(delta).

        on_tool_call(ToolCall) fires as soon as each tool call's arguments are complete,
        so the engine can start executing safe tools before the model finishes its turn.
        """
        self.client()  # fail fast on a missing key before complaining about the model
        model = self.model()
        s = config.store.load()
        tools = tools or []
        native = self.native_tools.get(model, True)
        known = [t["function"]["name"] for t in tools]

        req_messages = messages
        kwargs: dict[str, Any] = {}
        if no_think:
            # a retry after a broken turn: answer without the thinking phase (where the breakage happens)
            kwargs["extra_body"] = {"chat_template_kwargs": {"thinking": False, "enable_thinking": False},
                                    "reasoning_effort": "none"} if s.provider in ("lmstudio", "ollama") else                 {"chat_template_kwargs": {"thinking": False, "enable_thinking": False}}
        if tools and native:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        elif tools and not native:
            req_messages = _inject_react(messages, tools)

        for attempt in range(5):
            try:
                return await self._stream_once(
                    model, req_messages, kwargs, known, s, on_text, on_tool_call, cancel
                )
            except BrainError as e:
                if "extra_body" in kwargs and e.kind in ("error", "tools_unsupported") and "400" in str(e):
                    kwargs.pop("extra_body")          # this server doesn't take the no-thinking switch
                    continue
                if e.kind == "tools_unsupported" and native and tools:
                    log.info("model %s rejected native tools; switching to text tool calling", model)
                    self.native_tools[model] = False
                    native = False
                    kwargs.pop("tools", None)
                    kwargs.pop("tool_choice", None)
                    req_messages = _inject_react(messages, tools)
                    continue
                if e.kind in ("rate_limit", "connection", "server") and attempt < 4:
                    delay = min(20.0, (2**attempt) + random.random())
                    from ..events import bus

                    bus.emit("notice", level="warn",
                             text=f"{'Rate limited' if e.kind == 'rate_limit' else 'Connection issue'}"
                                  f" — retrying in {delay:.0f}s")
                    await asyncio.sleep(delay)
                    continue
                raise
        raise BrainError("Model did not respond after several retries.", "connection")

    async def _stream_once(self, model, messages, kwargs, known, s, on_text, on_tool_call, cancel):
        try:
            stream = await self.client().chat.completions.create(
                model=model,
                messages=messages,
                temperature=s.temperature,
                max_tokens=s.max_tokens,
                stream=True,
                **kwargs,
            )
        except Exception as e:
            raise self._wrap(e) from e

        result = TurnResult()
        partial: dict[int, dict[str, Any]] = {}
        emitted: set[int] = set()
        text_parts: list[str] = []
        holding = False  # suppress streaming text that is actually a text tool call
        reasoning = ""   # watched for degeneration (seen on NVIDIA: the thoughts turn into "!!!!!!!…" and the turn dies)

        def finish_call(idx: int) -> None:
            if idx in emitted or idx not in partial:
                return
            p = partial[idx]
            name = match_tool_name(p["name"], known) or p["name"]
            try:
                args = normalize_args(p["args"])
            except ValueError:
                args = {"__invalid_json__": p["args"]}
            tc = ToolCall(name=name, arguments=args, id=p.get("id") or ToolCall(name="x").id,
                          raw_arguments=p["args"])
            result.tool_calls.append(tc)
            emitted.add(idx)
            if on_tool_call:
                on_tool_call(tc)

        try:
            async for chunk in stream:
                if cancel is not None and cancel.is_set():
                    await stream.close()
                    result.finish_reason = "cancelled"
                    break
                if getattr(chunk, "usage", None):
                    u = chunk.usage
                    result.usage = {"prompt": u.prompt_tokens or 0, "completion": u.completion_tokens or 0}
                if not chunk.choices:
                    continue
                ch = chunk.choices[0]
                d = ch.delta
                rc = (getattr(d, "model_extra", None) or {}).get("reasoning_content") if d is not None else None
                if rc:
                    reasoning = (reasoning + rc)[-200:]
                    if DEGENERATE.search(reasoning):
                        log.info("model output degenerated (%r); abandoning this turn", reasoning[-24:])
                        await stream.close()
                        result.finish_reason = "degenerate"
                        break
                if d is not None and d.content:
                    text_parts.append(d.content)
                    joined = "".join(text_parts)
                    if not holding and ("<tool_call>" in joined or joined.lstrip().startswith(("{", "```"))):
                        holding = True
                    if not holding and on_text:
                        on_text(d.content)
                if d is not None and d.tool_calls:
                    for tcd in d.tool_calls:
                        idx = tcd.index if tcd.index is not None else len(partial)
                        # a new index means previous calls are complete
                        for prev in list(partial):
                            if prev < idx:
                                finish_call(prev)
                        p = partial.setdefault(idx, {"id": None, "name": "", "args": ""})
                        if tcd.id:
                            p["id"] = tcd.id
                        if tcd.function is not None:
                            if tcd.function.name:
                                p["name"] += tcd.function.name
                            if tcd.function.arguments:
                                p["args"] += tcd.function.arguments
                if ch.finish_reason:
                    result.finish_reason = ch.finish_reason
        except Exception as e:
            raise self._wrap(e) from e

        for idx in sorted(partial):
            finish_call(idx)

        text = "".join(text_parts)
        if not result.tool_calls and known:
            calls, remaining = extract_text_tool_calls(text, known)
            if calls:
                result.tool_calls = calls
                for c in calls:
                    if on_tool_call:
                        on_tool_call(c)
                text = remaining
            elif holding and on_text:
                on_text(text)  # it was plain text after all
        result.text = text
        return result

    # ------------------------------------------------------------- one-shot completion
    async def complete(self, messages: list[dict[str, Any]], max_tokens: int = 4000, temperature: float = 0.2,
                       think: bool = False) -> tuple[str, str | None]:
        """Non-streaming call for internal jobs (e.g. writing OpenSCAD). Returns (text, finish_reason).

        On local servers thinking is switched off unless asked for: v1 measured a 36-line part at
        203 s with thinking on (and it still didn't compile) vs seconds with it off.
        """
        s = config.store.load()
        extra: dict[str, Any] = {}
        if not think and s.provider in ("lmstudio", "ollama"):
            extra["reasoning_effort"] = "none"
        for attempt in range(4):
            try:
                r = await self.client().chat.completions.create(
                    model=self.model(), messages=messages, max_tokens=max_tokens, temperature=temperature,
                    extra_body=extra or None,
                )
                ch = r.choices[0]
                text = re.sub(r"<think>.*?</think>", "", ch.message.content or "", flags=re.S).strip()
                return text, ch.finish_reason
            except Exception as e:
                err = self._wrap(e)
                if err.kind in ("rate_limit", "connection", "server") and attempt < 3:
                    await asyncio.sleep(min(15.0, 2 ** attempt + random.random()))
                    continue
                raise err from e
        raise BrainError("Model did not respond after several retries.", "connection")

    # ------------------------------------------------------------- errors
    @staticmethod
    def _wrap(e: Exception) -> BrainError:
        if isinstance(e, BrainError):
            return e
        if isinstance(e, RateLimitError):
            return BrainError("Rate limited by the provider.", "rate_limit")
        if isinstance(e, APIConnectionError):
            return BrainError("Can't reach the model provider. Check your internet / base URL.", "connection")
        if isinstance(e, APIStatusError):
            code = e.status_code
            body = str(getattr(e, "message", "") or e)
            low = body.lower()
            if code in (401, 403):
                return BrainError("API key was rejected. Check it in Settings.", "auth")
            if code == 404:
                return BrainError(f"Model or endpoint not found: {body[:200]}", "model")
            if code in (400, 422) and ("tool" in low or "function" in low):
                return BrainError(body[:300], "tools_unsupported")
            if code == 429:
                return BrainError("Rate limited by the provider.", "rate_limit")
            if code >= 500:
                return BrainError(f"Provider error {code}.", "server")
            return BrainError(f"Provider error {code}: {body[:300]}", "error")
        return BrainError(str(e) or e.__class__.__name__, "error")


def _inject_react(messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rewrite a native-tools conversation into plain text for models without function calling."""
    listing = react_tool_listing(tools)
    out: list[dict[str, Any]] = []
    for m in messages:
        role = m["role"]
        if role == "system":
            out.append({"role": "system", "content": f"{m['content']}\n\n{listing}"})
        elif role == "assistant" and m.get("tool_calls"):
            calls = "\n".join(
                f'<tool_call>{{"name": "{c["function"]["name"]}", "arguments": {c["function"]["arguments"]}}}'
                f"</tool_call>"
                for c in m["tool_calls"]
            )
            out.append({"role": "assistant", "content": ((m.get("content") or "") + "\n" + calls).strip()})
        elif role == "tool":
            out.append({"role": "user", "content": f"[tool result: {m.get('name', '')}]\n{m['content']}"})
        else:
            out.append(m)
    if not any(m["role"] == "system" for m in out):
        out.insert(0, {"role": "system", "content": listing})
    return out


brain = Brain()
