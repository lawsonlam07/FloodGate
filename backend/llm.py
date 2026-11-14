"""Structured-output LLM calls with a switchable provider: Google Gemini or Anthropic Claude.

FloodGate uses an LLM for two narrow jobs, each returning JSON that must match a schema:
  - "grouping": which claims from different sources are the same claim (backend/grouping.py)
  - "extract":  claims from the changed lines of a page we already captured (backend/extraction.py)
Rules decide everything else (verdicts, severity, safety), so the model is replaceable.

Settings (env / .env):
  LLM_PROVIDER     gemini | anthropic   default: gemini when GEMINI_API_KEY is set, else anthropic
  GEMINI_MODEL     default gemini-3.5-flash-lite (12/12 grouping checks, 3-6 s per batch; Haiku 6-14 s)
  GROUPING_MODEL   Claude model for grouping (default claude-haiku-5-5)
  LLM_FALLBACK     1 (default) = if the chosen provider fails, retry the call once with the other one (when its key is set)

Every call is blocking; callers run it in a worker thread.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass

CLAUDE_MODEL = os.environ.get("GROUPING_MODEL", "claude-haiku-5-5")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite")
LLM_TIMEOUT_S = float(os.environ.get("LLM_TIMEOUT_S", "45"))     # a hung call must not block a worker thread for long


@dataclass
class LLMResult:
    data: dict | None
    latency_ms: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = ""
    error: str | None = None


def _ms(start: float) -> int:
    return round((time.monotonic() - start) * 1000)


class ClaudeLLM:
    provider = "anthropic"

    def __init__(self, model: str = CLAUDE_MODEL, client=None):
        import anthropic
        self.model, self.client = model, client or anthropic.Anthropic(timeout=LLM_TIMEOUT_S, max_retries=1)

    def json(self, *, system: str, prompt: str, schema: dict, effort: str = "low", max_tokens: int = 16000) -> LLMResult:
        # Server-side refusal fallbacks exist for Opus/Sonnet but not Haiku.
        fallback = {} if "haiku" in self.model else {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}
        start = time.monotonic()
        try:
            r = self.client.beta.messages.create(
                model=self.model, max_tokens=max_tokens, **fallback,
                output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
                system=system, messages=[{"role": "user", "content": prompt}])
        except Exception as exc:
            return LLMResult(None, _ms(start), model=self.model, error=f"{type(exc).__name__}: {exc}"[:300])
        out = LLMResult(None, _ms(start), r.usage.input_tokens, r.usage.output_tokens, r.model)
        if r.stop_reason == "refusal":
            out.error = f"refused: {r.stop_details}"
            return out
        text = next((b.text for b in r.content if b.type == "text"), "")
        try:
            out.data = json.loads(text)
        except ValueError:
            out.error = f"unparseable output (stop_reason {r.stop_reason})"
        return out


class GeminiLLM:
    provider = "gemini"
    _LEVELS = {"low": "LOW", "medium": "MEDIUM", "high": "HIGH"}

    def __init__(self, model: str = GEMINI_MODEL, api_key: str | None = None):
        from google import genai
        from google.genai import types
        self.model = model
        self.client = genai.Client(api_key=api_key or os.environ["GEMINI_API_KEY"],
                                   http_options=types.HttpOptions(timeout=int(LLM_TIMEOUT_S * 1000)))

    def json(self, *, system: str, prompt: str, schema: dict, effort: str = "low", max_tokens: int = 16000) -> LLMResult:
        from google.genai import types
        config = types.GenerateContentConfig(
            system_instruction=system, response_mime_type="application/json", response_json_schema=schema,
            max_output_tokens=max_tokens, temperature=0,
            thinking_config=types.ThinkingConfig(thinking_level=self._LEVELS.get(effort, "LOW")))
        start = time.monotonic()
        try:
            r = self.client.models.generate_content(model=self.model, contents=prompt, config=config)
        except Exception as exc:
            return LLMResult(None, _ms(start), model=self.model, error=f"{type(exc).__name__}: {exc}"[:300])
        usage = r.usage_metadata
        out = LLMResult(None, _ms(start), getattr(usage, "prompt_token_count", 0) or 0,
                        (getattr(usage, "candidates_token_count", 0) or 0) + (getattr(usage, "thoughts_token_count", 0) or 0),
                        r.model_version or self.model)
        finish = str(r.candidates[0].finish_reason) if r.candidates else "no candidates"
        if not r.text:
            out.error = f"empty output ({finish})"
            return out
        try:
            out.data = json.loads(r.text)
        except ValueError:
            out.error = f"unparseable output ({finish})"
        return out


class WithFallback:
    """Try the primary model; if it fails (error, refusal, bad JSON), make the same call once with the backup."""

    def __init__(self, primary, backup=None):
        self.primary, self.backup = primary, backup
        self.provider, self.model = primary.provider, primary.model

    def json(self, **kw) -> LLMResult:
        first = self.primary.json(**kw)
        if first.data is not None or self.backup is None:
            return first
        second = self.backup.json(**kw)
        second.latency_ms += first.latency_ms
        if second.data is not None:
            second.error = None
            second.model += f" (fallback: {first.model} failed: {first.error})"[:200]
        else:
            second.error = f"{first.model}: {first.error}; {second.model}: {second.error}"
        return second


def provider() -> str:
    chosen = os.environ.get("LLM_PROVIDER", "").strip().lower()
    if chosen in ("gemini", "anthropic"):
        return chosen
    return "gemini" if os.environ.get("GEMINI_API_KEY") else "anthropic"


def _build(name: str):
    if name == "gemini" and os.environ.get("GEMINI_API_KEY"):
        return GeminiLLM()
    if name == "anthropic" and os.environ.get("ANTHROPIC_API_KEY"):
        return ClaudeLLM()
    return None


def make(name: str | None = None):
    """The configured LLM (with the other provider as fallback), or None when no key is set."""
    name = name or provider()
    other = "anthropic" if name == "gemini" else "gemini"
    primary, backup = _build(name), None
    if os.environ.get("LLM_FALLBACK", "1") != "0":
        backup = _build(other)
    if primary is None:
        primary, backup = backup, None
    return WithFallback(primary, backup) if primary else None
