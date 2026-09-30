"""Pick the 1-3 portfolio projects most relevant to a job (keyword overlap, no LLM)."""
from __future__ import annotations

import re

_WORD = re.compile(r"[a-zа-яё0-9+#]{2,}", re.I)


def _tokens(text: str) -> set[str]:
    return {w.lower()[:6] for w in _WORD.findall(text or "")}


def relevant_projects(profile: dict, job_text: str, limit: int = 3) -> list[dict]:
    job = _tokens(job_text)
    scored = []
    for p in profile.get("portfolio_projects") or []:
        stack = p.get("stack") or []
        stack_hits = sum(1 for s in stack if _tokens(s) & job)
        words = _tokens(f"{p.get('name', '')} {p.get('summary', '')} {p.get('type', '')}")
        score = stack_hits * 3 + len(words & job)
        if score > 0:
            scored.append((score, p))
    scored.sort(key=lambda x: -x[0])
    return [p for _, p in scored[:limit]]
