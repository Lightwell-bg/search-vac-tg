"""Deterministic pre-filter (config/filter.yaml).

Only obvious garbage is rejected here; anything with a technical signal goes on to JEV.
A negative word alone never rejects a post that also has a positive signal:
"автоматизировать дизайн-агентство через n8n" passes.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

import yaml

WEIGHTS = {"strong_positive": 3, "positive": 1, "negative": -1, "strong_negative": -3}


def _keyword_regex(keyword: str) -> re.Pattern:
    """Keyword -> regex.

    * spaces and hyphens inside a keyword match any run of spaces/hyphens;
    * the left side must be a word boundary;
    * Cyrillic keywords may take a word ending (бот -> бота, парсер -> парсеры);
    * Latin keywords may take a plural ``s`` (bot -> bots) but not other letters,
      so ``api`` does not fire inside ``capital``; ``*`` at the end = explicit prefix.
    """
    kw = keyword.strip().lower()
    prefix = kw.endswith("*")
    kw = kw.rstrip("*")
    parts = [re.escape(p) for p in re.split(r"[\s\-]+", kw) if p]
    body = r"[\s\-]+".join(parts)
    if prefix:
        tail = r"\w*"
    elif re.search(r"[а-яё]", kw):
        tail = r"[а-яё]{0,4}(?![\w])"
    else:
        tail = r"(?:s|es)?(?![\w])"
    return re.compile(rf"(?<![\w]){body}{tail}", re.I)


@dataclass
class RuleResult:
    verdict: str                  # reject | pass
    reason: str
    score: int                    # 0..100, strength of the technical signal
    strong_accept: bool = False   # many strong hits, no negatives (logged as RULE_ACCEPT)
    hits: dict[str, list[str]] = field(default_factory=dict)

    @property
    def technologies(self) -> list[str]:
        return self.hits.get("strong_positive", []) + self.hits.get("positive", [])

    def as_dict(self) -> dict:
        return asdict(self)


class RuleFilter:
    def __init__(self, config: dict, min_text_length: int = 40):
        self.min_text_length = min_text_length
        self.lists: dict[str, list[tuple[str, re.Pattern]]] = {}
        for name in (*WEIGHTS, "job_markers"):
            words = [str(w) for w in (config.get(name) or []) if str(w).strip()]
            self.lists[name] = [(w, _keyword_regex(w)) for w in words]
        self.ignore_contacts = {str(x).lower().lstrip("@") for x in (config.get("ignore_contacts") or [])}

    @classmethod
    def from_file(cls, path: Path, min_text_length: int = 40) -> "RuleFilter":
        with open(path, encoding="utf-8") as f:
            return cls(yaml.safe_load(f) or {}, min_text_length)

    def _hits(self, text: str) -> dict[str, list[str]]:
        low = text.lower()
        return {name: [w for w, rx in items if rx.search(low)] for name, items in self.lists.items()}

    def evaluate(self, text: str) -> RuleResult:
        hits = self._hits(text)
        sp, p = len(hits["strong_positive"]), len(hits["positive"])
        n, sn = len(hits["negative"]), len(hits["strong_negative"])
        score = max(0, min(100, sp * 25 + p * 10 - n * 5 - sn * 15))
        hits = {k: v for k, v in hits.items() if v}

        if len(text.strip()) < self.min_text_length:
            return RuleResult("reject", "too_short", score, hits=hits)
        if (sn or n) and not (sp or p):
            kind = "strong_negative" if sn else "negative"
            words = ", ".join(hits.get("strong_negative", []) + hits.get("negative", []))
            return RuleResult("reject", f"{kind}_only: {words}", score, hits=hits)
        if not (sp or p) and not hits.get("job_markers"):
            return RuleResult("reject", "no_job_signal", score, hits=hits)
        strong = sp >= 2 and not (n or sn)
        return RuleResult("pass", "strong_positive" if strong else "needs_semantic_check",
                          score, strong_accept=strong, hits=hits)
