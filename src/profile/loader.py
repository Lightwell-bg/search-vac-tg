"""Load data/profile.json and build the compact profile string sent to JEV/OpenRouter."""
from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)

# Used only until scripts/rebuild_profile.py produced data/profile.json from real materials.
PROVISIONAL_PROFILE: dict = {
    "provisional": True,
    "summary": "Python developer: Telegram bots, business automation, AI/LLM integrations.",
    "skills": ["Python", "Telegram bots", "API integrations", "webhooks", "AI automation",
               "AI assistants", "AI agents", "RAG", "MCP", "parsing/scraping",
               "business process automation", "CRM integrations"],
    "technologies": ["Python", "aiogram", "Telegram Bot API", "Telethon", "n8n", "OpenAI API",
                     "Anthropic API", "Gemini API", "OpenRouter", "FastAPI", "WordPress",
                     "WooCommerce", "HTML/CSS/JS", "Supabase", "PostgreSQL", "SQLite", "Docker",
                     "Ubuntu VPS", "Google Sheets", "Google Drive"],
    "services": ["Telegram bot development", "n8n / workflow automation", "API and webhook integrations",
                 "AI assistants and chatbots on LLM APIs", "RAG / AI agents", "parsers and scrapers",
                 "WordPress/WooCommerce customization", "CRM and Google Sheets integrations",
                 "deployment on Ubuntu VPS with Docker"],
    "project_types": ["telegram_bot", "automation", "ai_assistant", "parser", "wordpress", "web_backend"],
    "portfolio_projects": [],
    "strong_matches": ["Telegram bots", "n8n automation", "AI/LLM integration", "API integration",
                       "Python automation", "RAG", "AI agents", "parsing"],
    "acceptable_matches": ["WordPress/WooCommerce", "FastAPI backend", "CRM integration",
                           "Google Sheets automation", "Docker/VPS deployment", "social media APIs"],
    "weak_matches": ["frontend-only work", "mobile apps", "1C", "Bitrix CMS development"],
    "reject_categories": ["design", "SMM", "copywriting", "video editing", "sales", "recruiting",
                          "accounting", "non-technical tasks"],
}


def load_profile(path: Path) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and data.get("skills") is not None:
            return data
        log.warning("profile %s has no 'skills', using provisional profile", path)
    except FileNotFoundError:
        log.warning("profile %s not found, using provisional profile", path)
    except (OSError, ValueError) as e:
        log.error("cannot read profile %s: %s; using provisional profile", path, e)
    return dict(PROVISIONAL_PROFILE)


def _join(items, limit: int) -> str:
    return ", ".join(str(i) for i in (items or [])[:limit])


def compact_profile(profile: dict, max_chars: int = 900) -> str:
    """Short text profile (a few hundred chars): what JEV and OpenRouter get instead of the CV."""
    parts = []
    if profile.get("summary"):
        parts.append(str(profile["summary"]))
    parts.append("Stack: " + _join(profile.get("technologies"), 25))
    parts.append("Does: " + _join(profile.get("services"), 10))
    parts.append("Best fit: " + _join(profile.get("strong_matches"), 10))
    if profile.get("acceptable_matches"):
        parts.append("Also OK: " + _join(profile.get("acceptable_matches"), 8))
    if profile.get("reject_categories"):
        parts.append("Not interested: " + _join(profile.get("reject_categories"), 10))
    text = "\n".join(p for p in parts if not p.endswith(": "))
    return text[:max_chars]
