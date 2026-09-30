"""OpenRouter result schemas."""
from __future__ import annotations

from dataclasses import asdict, dataclass

from pydantic import BaseModel, Field, field_validator


class LlmError(Exception):
    """OpenRouter call failed. ``kind``: config | timeout | rate_limit | http | invalid_json."""

    def __init__(self, kind: str, message: str, usage: "LlmUsage | None" = None):
        super().__init__(f"{kind}: {message}")
        self.kind = kind
        self.usage = usage


@dataclass
class LlmUsage:
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    duration_ms: int = 0

    def as_dict(self) -> dict:
        return asdict(self)


class ReviewResult(BaseModel):
    fit_score: int = Field(ge=0, le=100)
    category: str = "other"
    relevant_skills: list[str] = Field(default_factory=list)
    missing_skills: list[str] = Field(default_factory=list)
    reason: str = ""
    should_notify: bool = False

    @field_validator("fit_score", mode="before")
    @classmethod
    def _clamp(cls, v):
        try:
            return max(0, min(100, int(round(float(v)))))
        except (TypeError, ValueError):
            raise ValueError("fit_score must be a number") from None

    @field_validator("relevant_skills", "missing_skills", mode="before")
    @classmethod
    def _list(cls, v):
        if v is None:
            return []
        if isinstance(v, str):
            return [s.strip() for s in v.split(",") if s.strip()]
        return [str(s) for s in v][:15]
