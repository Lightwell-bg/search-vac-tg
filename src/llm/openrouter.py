"""OpenRouter chat-completions client (model from OPENROUTER_MODEL, never hardcoded).

Called only for JEV REVIEW, JEV failure with fallback enabled, manual response
generation and optional profile building.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import time

import httpx
from pydantic import ValidationError

from . import prompts
from .schemas import LlmError, LlmUsage, ReviewResult

log = logging.getLogger(__name__)

_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.I)


def parse_json_object(content: str) -> dict:
    """Strict-ish JSON extraction: strips code fences and surrounding prose."""
    text = _FENCE.sub("", content or "").strip()
    try:
        data = json.loads(text)
    except ValueError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise LlmError("invalid_json", f"no JSON object in: {text[:200]}") from None
        try:
            data = json.loads(text[start:end + 1])
        except ValueError as e:
            raise LlmError("invalid_json", f"{e}: {text[:200]}") from None
    if not isinstance(data, dict):
        raise LlmError("invalid_json", "JSON is not an object")
    return data


def _num(v) -> float:
    """Usage numbers come from an external API: anything not a finite number counts as 0."""
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        return 0.0
    return float(v)


class OpenRouterClient:
    def __init__(self, api_key: str, model: str, base_url: str = "https://openrouter.ai/api/v1",
                 timeout: float = 60.0, max_retries: int = 2, http: httpx.AsyncClient | None = None):
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self._http = http
        self._own_http = http is None

    @classmethod
    def from_settings(cls, s) -> "OpenRouterClient":
        return cls(s.openrouter_api_key, s.openrouter_model, s.openrouter_base_url,
                   s.openrouter_timeout, s.openrouter_max_retries)

    async def close(self) -> None:
        if self._own_http and self._http is not None:
            await self._http.aclose()
            self._http = None

    async def chat(self, system: str, user: str, json_mode: bool, max_tokens: int = 600,
                   temperature: float = 0.2) -> tuple[str, LlmUsage]:
        if not self.api_key or not self.model:
            raise LlmError("config", "OPENROUTER_API_KEY / OPENROUTER_MODEL not set",
                           LlmUsage(self.model or ""))
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=self.timeout)
        body = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "max_tokens": max_tokens,
            "temperature": temperature,
            "usage": {"include": True},
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json",
                   "X-Title": "search-vac-tg"}
        started = time.monotonic()
        last: LlmError | None = None
        for attempt in range(self.max_retries + 1):
            try:
                resp = await self._http.post(f"{self.base_url}/chat/completions", json=body,
                                             headers=headers, timeout=self.timeout)
            except httpx.TimeoutException:
                last = LlmError("timeout", "OpenRouter timeout")
            except httpx.HTTPError as e:
                last = LlmError("http", f"{type(e).__name__}: {e}")
            else:
                if resp.status_code == 200:
                    return self._parse(resp, started)
                kind = "rate_limit" if resp.status_code == 429 else "http"
                last = LlmError(kind, f"HTTP {resp.status_code}: {resp.text[:200]}")
                if resp.status_code not in (408, 429) and resp.status_code < 500:
                    break  # 4xx other than rate limit: retrying will not help
            if attempt < self.max_retries:
                await asyncio.sleep(min(2 ** attempt * 2, 20))
        last.usage = LlmUsage(self.model, duration_ms=int((time.monotonic() - started) * 1000))
        raise last

    def _parse(self, resp: httpx.Response, started: float) -> tuple[str, LlmUsage]:
        duration = int((time.monotonic() - started) * 1000)
        try:
            data = resp.json()
            content = data["choices"][0]["message"]["content"] or ""
        except (ValueError, KeyError, IndexError, TypeError) as e:
            raise LlmError("invalid_json", f"bad completion payload: {e}",
                           LlmUsage(self.model, duration_ms=duration)) from None
        if not isinstance(content, str):
            raise LlmError("invalid_json", "completion content is not text",
                           LlmUsage(self.model, duration_ms=duration))
        u = data.get("usage")
        if not isinstance(u, dict):
            u = {}
        model = data.get("model")
        usage = LlmUsage(
            model=model if isinstance(model, str) and model else self.model,
            input_tokens=int(_num(u.get("prompt_tokens"))),
            output_tokens=int(_num(u.get("completion_tokens"))),
            cost_usd=_num(u.get("cost")),
            duration_ms=duration,
        )
        return content, usage

    # ------------------------------------------------------------------ use cases

    async def review_job(self, profile: str, job: str, jev_short: str) -> tuple[ReviewResult, LlmUsage]:
        content, usage = await self.chat(prompts.REVIEW_SYSTEM,
                                         prompts.review_user(profile, job, jev_short),
                                         json_mode=True, max_tokens=400, temperature=0.0)
        try:
            data = parse_json_object(content)
            return ReviewResult.model_validate(data), usage
        except LlmError as e:
            e.usage = usage
            raise
        except ValidationError as e:
            raise LlmError("invalid_json", f"schema: {e.errors()[:2]}", usage) from None

    async def write_application(self, profile: str, job: str, projects: list[dict]) -> tuple[str, LlmUsage]:
        content, usage = await self.chat(prompts.APPLICATION_SYSTEM,
                                         prompts.application_user(profile, job, projects),
                                         json_mode=False, max_tokens=700, temperature=0.5)
        return content.strip(), usage
