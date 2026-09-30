"""Final fit decision from rules + JEV + (optional) OpenRouter review.

No single model score decides alone:
* JEV accept      -> JEV fit (0-100) + small rules bonus, must reach NOTIFY_SCORE;
* OpenRouter used -> 70% LLM fit + 30% JEV fit (if JEV answered) + rules bonus,
                     and the LLM must also say ``should_notify``.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

from ..jev.schemas import ACCEPT, JevDecision
from ..llm.schemas import ReviewResult
from .rules import RuleResult


@dataclass
class FinalDecision:
    accept: bool
    score: int
    reason: str
    category: str | None = None
    relevant_skills: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


def rules_bonus(rules: RuleResult | None) -> int:
    return min(10, (rules.score if rules else 0) // 10)


def decide(rules: RuleResult | None, jev: JevDecision | None, llm: ReviewResult | None,
           notify_score: int) -> FinalDecision:
    bonus = rules_bonus(rules)
    rule_skills = rules.technologies if rules else []
    if llm is not None:
        base = llm.fit_score if jev is None else round(0.7 * llm.fit_score + 0.3 * jev.fit_score)
        score = min(100, base + bonus)
        accept = llm.should_notify and score >= notify_score
        reason = llm.reason or ("OpenRouter: fit" if accept else "OpenRouter: not a fit")
        skills = llm.relevant_skills or rule_skills
        category = llm.category or (jev.category if jev else None)
        if llm.should_notify and not accept:
            reason += f" (score {score} < {notify_score})"
        return FinalDecision(accept, score, reason, category, skills)
    if jev is not None and jev.decision == ACCEPT:
        score = min(100, jev.fit_score + bonus)
        accept = score >= notify_score
        reason = jev.reason if accept else f"{jev.reason} (score {score} < {notify_score})"
        return FinalDecision(accept, score, reason, jev.category, rule_skills)
    return FinalDecision(False, jev.fit_score if jev else 0,
                         jev.reason if jev else "no decision", jev.category if jev else None, rule_skills)
