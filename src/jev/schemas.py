"""Typed results of the JEV (TypeSafe System One) classifier."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

ACCEPT, REJECT, REVIEW = "accept", "reject", "review"
DECISIONS = (ACCEPT, REJECT, REVIEW)


class JevError(Exception):
    """JEV call failed. ``kind``: config | timeout | http | invalid."""

    def __init__(self, kind: str, message: str, duration_ms: int = 0):
        super().__init__(f"{kind}: {message}")
        self.kind = kind
        self.duration_ms = duration_ms


@dataclass
class JevUsage:
    model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    duration_ms: int = 0


@dataclass
class JevDecision:
    """Business view of one JEV answer.

    ``decision`` is the effective decision after the confidence/consistency guard;
    ``raw_decision`` is what the model picked. ``confidence`` and ``probabilities``
    are the model's own numbers (JEV returns calibrated probabilities), nothing is
    invented. ``reason`` is assembled from the typed answers, JEV returns no prose.
    """

    decision: str
    raw_decision: str
    confidence: float
    fit_score: int                  # 0..100 derived from the 0..3 ``fit`` score
    fit_raw: float                  # JEV score on the 0..3 scale
    category: str | None
    probabilities: dict[str, float] = field(default_factory=dict)
    reason: str = ""
    usage: JevUsage = field(default_factory=JevUsage)

    def as_dict(self) -> dict:
        return asdict(self)

    def short(self) -> str:
        """Compact line passed to OpenRouter on review."""
        return (f"decision={self.raw_decision} (p={self.confidence:.2f}), "
                f"fit={self.fit_raw:.2f}/3, category={self.category}")
