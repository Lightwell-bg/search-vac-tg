"""Prompts for OpenRouter. Kept short: the model gets a compact profile, not the CV."""
from __future__ import annotations

REVIEW_SYSTEM = (
    "You screen freelance job posts for one developer. Return strict JSON only, no markdown, "
    "no explanations outside JSON. Schema: "
    '{"fit_score": int 0-100, "category": str, "relevant_skills": [str], '
    '"missing_skills": [str], "reason": str (one short sentence in Russian), '
    '"should_notify": bool}. '
    "should_notify=true only for a real technical job the developer can do with the profile stack. "
    "Non-technical jobs (design, SMM, sales, copywriting, video) get fit_score below 30."
)


def review_user(profile: str, job: str, jev_short: str) -> str:
    return f"PROFILE:\n{profile}\n\nJOB:\n{job}\n\nJEV:\n{jev_short}\n\nReturn strict JSON only."


APPLICATION_SYSTEM = (
    "You write a short personal reply from a freelance developer to a job post, in the language "
    "of the post (Russian by default). 500-900 characters, plain text, no markdown headers. "
    "Structure: one line showing you understood the task; 2-3 concrete relevant points from the "
    "developer's experience and projects (only from the given data, never invent facts, numbers or "
    "clients); a short plan or first step; one question to clarify the task. No flattery, no "
    "placeholders like [Name]."
)


def application_user(profile: str, job: str, projects: list[dict]) -> str:
    lines = []
    for p in projects[:3]:
        stack = ", ".join(p.get("stack") or [])
        lines.append(f"- {p.get('name')}: {p.get('summary', '')} [{stack}]")
    proj = "\n".join(lines) or "- (no project descriptions available)"
    return f"PROFILE:\n{profile}\n\nRELEVANT PROJECTS:\n{proj}\n\nJOB:\n{job}\n\nWrite the reply."
