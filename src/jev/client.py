"""HTTP adapter for JEV — TypeSafe "System One" typed-decision API.

This is the same runtime the dev tooling uses (~/.claude/scripts/ojc/jev-route.sh):

    POST {openrouter base_url}/systemone   (e.g. https://openrouter.ai/api/v1/systemone, OPENROUTER_API_KEY)
    {"model": "~typesafe/jev-latest", "state": {...}, "questions": {id: {type, instructions, criteria}}}

    -> {"model": ..., "answers": {id: {"type": "choice", "choice": .., "probabilities": {..},
        "confidence": ..} | {"type": "score", "score": .., "probabilities": .., "confidence": ..}
        | {"type": "noul", "noul": ..}}, "usage": {"input_tokens", "output_tokens", "cost"}}

The rest of the project talks to ``JevClient.ask`` only.
"""
from __future__ import annotations

import asyncio
import logging
import math
import time

import httpx

from .schemas import JevError, JevUsage

log = logging.getLogger(__name__)


class JevClient:
    def __init__(self, url: str, api_key: str, model: str, timeout: float = 20.0,
                 http: httpx.AsyncClient | None = None):
        self.url = url
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self._http = http
        self._own_http = http is None

    @classmethod
    def from_settings(cls, s) -> "JevClient":
        return cls(s.jev_url, s.openrouter_api_key, s.jev_model, s.jev_timeout)

    async def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=self.timeout)
        return self._http

    async def close(self) -> None:
        if self._own_http and self._http is not None:
            await self._http.aclose()
            self._http = None

    async def ask(self, state: dict, questions: dict) -> tuple[dict, JevUsage]:
        """One typed call. Returns (answers, usage). Raises JevError, never other exceptions."""
        if not self.api_key:
            raise JevError("config", "JEV API key is not set")
        body = {"model": self.model, "state": state, "questions": questions}
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        started = time.monotonic()
        client = await self._client()
        resp = None
        for attempt in range(2):  # one retry on 429/5xx
            try:
                resp = await client.post(self.url, json=body, headers=headers, timeout=self.timeout)
            except httpx.TimeoutException as e:
                raise JevError("timeout", str(e) or "timeout", _ms(started)) from e
            except httpx.HTTPError as e:
                raise JevError("http", f"{type(e).__name__}: {e}", _ms(started)) from e
            if resp.status_code in (429, 500, 502, 503, 504) and attempt == 0:
                await asyncio.sleep(_retry_after(resp))
                continue
            break
        duration = _ms(started)
        if resp.status_code != 200:
            raise JevError("http", f"HTTP {resp.status_code}: {resp.text[:200]}", duration)
        try:
            data = resp.json()
        except ValueError as e:
            raise JevError("invalid", "response is not JSON", duration) from e
        if not isinstance(data, dict):
            raise JevError("invalid", f"response is not an object: {str(data)[:200]}", duration)
        answers = data.get("answers")
        if not isinstance(answers, dict):
            raise JevError("invalid", f"no 'answers' in response: {str(data)[:200]}", duration)
        u = data.get("usage")
        if not isinstance(u, dict):
            u = {}
        model = data.get("model")
        usage = JevUsage(
            model=model if isinstance(model, str) and model else self.model,
            input_tokens=_int_or_none(u.get("input_tokens")),
            output_tokens=_int_or_none(u.get("output_tokens")),
            cost_usd=_float_or_none(u.get("cost")),
            duration_ms=duration,
        )
        return answers, usage


def _float_or_none(v) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        return None
    return float(v)


def _int_or_none(v) -> int | None:
    f = _float_or_none(v)
    return int(f) if f is not None else None


def _retry_after(resp) -> float:
    """Seconds to wait before the retry: numeric Retry-After only (HTTP-date etc. -> 1s), max 5s."""
    try:
        val = float(resp.headers.get("retry-after", 1))
    except (TypeError, ValueError):
        return 1.0
    if not math.isfinite(val) or val < 0:
        return 1.0
    return min(val, 5.0)


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
