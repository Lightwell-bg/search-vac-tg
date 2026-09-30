"""JevClassifier — cheap semantic job filter in front of OpenRouter.

One JEV call per job with three typed questions (decision / fit / category).
Only the compact profile and the normalized job text are sent.
"""
from __future__ import annotations

import math

from .client import JevClient
from .schemas import ACCEPT, DECISIONS, REJECT, REVIEW, JevDecision, JevError

CATEGORIES = {
    "telegram_automation": "Telegram bots, userbots, channel/chat automation",
    "business_automation": "n8n/Make/Zapier workflows, CRM, Google Sheets, API and webhook integrations",
    "ai_llm": "AI assistants, LLM/ChatGPT integrations, AI agents, RAG, MCP",
    "web_backend": "Python backend, FastAPI, databases, deployment, Docker/VPS",
    "wordpress": "WordPress / WooCommerce sites and plugins",
    "parsing": "parsing, scraping, data collection",
    "other_tech": "other software development outside the profile stack",
    "non_tech": "not software development (design, SMM, sales, texts, video, admin work)",
}

QUESTIONS = {
    "decision": {
        "type": "choice",
        "instructions": ("Should this freelance job post be sent to the developer described in `profile`? "
                         "Judge by what has to be built, not by single words: automating a design agency "
                         "with n8n is a fit, a designer job is not."),
        "criteria": {
            ACCEPT: "Clearly a technical task the developer can do with the profile stack.",
            REJECT: ("Clearly not a fit: non-technical work (design, SMM, sales, copywriting, video), "
                     "an ad or not a job at all, or a stack far outside the profile."),
            REVIEW: "Ambiguous, mixed or too vague to decide; needs a closer look.",
        },
    },
    "fit": {
        "type": "score",
        "instructions": "How well does the job match the developer `profile`?",
        "criteria": [
            "No match",
            "Weak match (adjacent technical work, few overlapping skills)",
            "Moderate match (doable with the profile stack, partial overlap)",
            "Strong match (core of the profile: Python, bots, automation, AI/LLM, integrations)",
        ],
    },
    "category": {
        "type": "choice",
        "instructions": "Main category of the job.",
        "criteria": CATEGORIES,
    },
}


def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _is_prob(v) -> bool:
    """A finite number in [0, 1]."""
    return _is_num(v) and math.isfinite(v) and 0.0 <= v <= 1.0


class JevClassifier:
    def __init__(self, client: JevClient, min_confidence: float = 0.7, max_text_chars: int = 3000):
        self.client = client
        self.min_confidence = min_confidence
        self.max_text_chars = max_text_chars

    async def classify(self, job_text: str, compact_profile: str, rule_hits: list[str] | None = None) -> JevDecision:
        state = {
            "profile": compact_profile,
            "job": job_text[: self.max_text_chars],
        }
        if rule_hits:
            state["keywords_found"] = ", ".join(rule_hits[:15])
        answers, usage = await self.client.ask(state, QUESTIONS)
        result = self.parse(answers)
        result.usage = usage
        return result

    def parse(self, answers: dict) -> JevDecision:
        """Validate raw answers and apply the uncertainty guard. Raises JevError('invalid')."""
        d = answers.get("decision")
        f = answers.get("fit")
        c = answers.get("category")
        if not isinstance(c, dict):
            c = {}
        if not isinstance(d, dict) or d.get("choice") not in DECISIONS:
            raise JevError("invalid", f"bad decision answer: {str(d)[:200]}")
        fit_val = f.get("score") if isinstance(f, dict) else None
        if not _is_num(fit_val):
            raise JevError("invalid", f"bad fit answer: {str(f)[:200]}")
        fit_raw = float(fit_val)
        if not (math.isfinite(fit_raw) and 0.0 <= fit_raw <= 3.0 + 1e-6):
            raise JevError("invalid", f"fit score out of range 0..3: {fit_raw}")
        fit_raw = min(fit_raw, 3.0)  # only the 1e-6 float tolerance, not a clamp of bad data
        raw = d["choice"]
        raw_probs = d.get("probabilities")
        probs = {str(k): float(v) for k, v in (raw_probs.items() if isinstance(raw_probs, dict) else ())
                 if _is_prob(v)}
        conf = d.get("confidence")
        if not _is_prob(conf):
            conf = probs.get(raw)
        if not _is_prob(conf):
            raise JevError("invalid", "decision has no valid confidence/probabilities in [0,1]")
        conf = float(conf)
        fit_score = round(fit_raw / 3 * 100)
        category = c.get("choice") if isinstance(c.get("choice"), str) and c.get("choice") in CATEGORIES else None

        decision, why = raw, []
        if raw in (ACCEPT, REJECT) and conf < self.min_confidence:
            decision = REVIEW
            why.append(f"low confidence {conf:.2f} < {self.min_confidence:.2f}")
        # the typed answers disagree with each other -> uncertain, let OpenRouter look
        if raw == ACCEPT and fit_raw < 1.5:
            decision = REVIEW
            why.append(f"accept with low fit {fit_raw:.2f}/3")
        if raw == REJECT and fit_raw >= 2.0:
            decision = REVIEW
            why.append(f"reject with high fit {fit_raw:.2f}/3")

        reason = f"JEV {raw} p={conf:.2f}, fit {fit_raw:.2f}/3, {category or 'no category'}"
        if why:
            reason += " -> review: " + "; ".join(why)
        return JevDecision(decision=decision, raw_decision=raw, confidence=round(conf, 4),
                           fit_score=fit_score, fit_raw=round(fit_raw, 3), category=category,
                           probabilities=probs, reason=reason)
