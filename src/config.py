"""Application settings: secrets from .env, non-secret constants from config.ini."""
from __future__ import annotations

import configparser
import logging
import logging.handlers
import os
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent

_JEV_DEFAULT_MODEL = "~typesafe/jev-latest"


def _to_bool(value: str | None, default: bool = False) -> bool:
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in ("true", "1", "yes", "on")


def _env_int(name: str, default: int | None) -> int | None:
    raw = os.environ.get(name, "").strip()
    if raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"Environment variable {name} must be an integer, got {raw!r}") from exc


def _ini_get(cfg: configparser.ConfigParser, section: str, key: str, cast, default):
    raw = cfg.get(section, key, fallback=None)
    if raw is None or raw.strip() == "":
        return default
    try:
        return cast(raw.strip())
    except ValueError as exc:
        raise ValueError(f"config.ini [{section}] {key} has invalid value {raw!r}") from exc


def _resolve(path_str: str) -> Path:
    p = Path(path_str)
    return p if p.is_absolute() else PROJECT_ROOT / p


def _normalize_db_url(url: str) -> str:
    prefix = "sqlite:///"
    if url.startswith(prefix):
        rest = url[len(prefix):]
        url = "sqlite+aiosqlite:///" + rest
    aio = "sqlite+aiosqlite:///"
    if url.startswith(aio):
        rest = url[len(aio):]
        if rest and rest != ":memory:":
            p = Path(rest)
            if not p.is_absolute():
                p = PROJECT_ROOT / p
            p.parent.mkdir(parents=True, exist_ok=True)
            url = aio + p.as_posix()
    return url


@dataclass(frozen=True)
class Settings:
    telegram_api_id: int | None
    telegram_api_hash: str
    notify_bot_token: str
    owner_telegram_id: int | None
    openrouter_api_key: str
    openrouter_model: str
    openrouter_base_url: str
    openrouter_timeout: float
    openrouter_max_retries: int
    jev_model: str
    jev_url: str
    jev_timeout: float
    jev_min_confidence: float
    jev_max_text_chars: int
    jev_fallback_to_openrouter: bool
    notify_score: int
    high_fit_score: int
    show_paid_contact: bool
    database_url: str
    channels_file: Path
    filter_file: Path
    profile_file: Path
    profile_md_file: Path
    materials_dir: Path
    session_file: Path
    dedup_fuzzy_threshold: int
    dedup_window_days: int
    dedup_max_candidates: int
    catchup_limit: int
    poll_interval_sec: int
    contact_click_delay_sec: float
    flood_sleep_threshold: int
    min_text_length: int
    retry_limit: int
    retry_interval_sec: int
    log_level: str
    log_file: Path

    def missing_for_run(self) -> list[str]:
        """Names of variables required to run the monitor that are still empty."""
        missing: list[str] = []
        if self.telegram_api_id is None:
            missing.append("TELEGRAM_API_ID")
        if not self.telegram_api_hash:
            missing.append("TELEGRAM_API_HASH")
        if not self.notify_bot_token:
            missing.append("NOTIFY_BOT_TOKEN")
        if self.owner_telegram_id is None:
            missing.append("OWNER_TELEGRAM_ID")
        # OpenRouter is always needed: REVIEW decisions, JEV fallback and manual replies
        if not self.openrouter_api_key:
            missing.append("OPENROUTER_API_KEY")
        if not self.openrouter_model:
            missing.append("OPENROUTER_MODEL")
        return missing


def load_settings(env_file: Path | None = None, ini_file: Path | None = None) -> Settings:
    """Build Settings from .env (override=False) and config.ini."""
    load_dotenv(env_file or PROJECT_ROOT / ".env", override=False)
    cfg = configparser.ConfigParser()
    cfg.read(ini_file or PROJECT_ROOT / "config.ini", encoding="utf-8")

    env = os.environ.get
    openrouter_base_url = _ini_get(cfg, "openrouter", "base_url", str, "https://openrouter.ai/api/v1")

    db_url = (env("DATABASE_URL", "") or "").strip() or "sqlite:///data/app.db"

    def path(section: str, key: str, default: str) -> Path:
        return _resolve(_ini_get(cfg, section, key, str, default))

    return Settings(
        telegram_api_id=_env_int("TELEGRAM_API_ID", None),
        telegram_api_hash=(env("TELEGRAM_API_HASH", "") or "").strip(),
        notify_bot_token=(env("NOTIFY_BOT_TOKEN", "") or "").strip(),
        owner_telegram_id=_env_int("OWNER_TELEGRAM_ID", None),
        openrouter_api_key=(env("OPENROUTER_API_KEY", "") or "").strip(),
        openrouter_model=(env("OPENROUTER_MODEL", "") or "").strip(),
        openrouter_base_url=openrouter_base_url,
        openrouter_timeout=_ini_get(cfg, "openrouter", "timeout_sec", float, 60.0),
        openrouter_max_retries=_ini_get(cfg, "openrouter", "max_retries", int, 2),
        jev_model=_ini_get(cfg, "jev", "model", str, _JEV_DEFAULT_MODEL),
        jev_url=openrouter_base_url.rstrip("/") + "/systemone",
        jev_timeout=_ini_get(cfg, "jev", "timeout_sec", float, 20.0),
        jev_min_confidence=_ini_get(cfg, "jev", "min_confidence", float, 0.70),
        jev_max_text_chars=_ini_get(cfg, "jev", "max_text_chars", int, 3000),
        jev_fallback_to_openrouter=_to_bool(env("JEV_FALLBACK_TO_OPENROUTER"), True),
        notify_score=_env_int("NOTIFY_SCORE", 65),
        high_fit_score=_env_int("HIGH_FIT_SCORE", 80),
        show_paid_contact=_to_bool(env("SHOW_PAID_CONTACT"), False),
        database_url=_normalize_db_url(db_url),
        channels_file=path("paths", "channels_file", "config/channels.yaml"),
        filter_file=path("paths", "filter_file", "config/filter.yaml"),
        profile_file=path("paths", "profile_file", "data/profile.json"),
        profile_md_file=path("paths", "profile_md_file", "data/profile.md"),
        materials_dir=path("paths", "materials_dir", "materials"),
        session_file=path("paths", "session_file", "data/telegram"),
        dedup_fuzzy_threshold=_ini_get(cfg, "dedup", "fuzzy_threshold", int, 90),
        dedup_window_days=_ini_get(cfg, "dedup", "window_days", int, 14),
        dedup_max_candidates=_ini_get(cfg, "dedup", "max_candidates", int, 1000),
        catchup_limit=_ini_get(cfg, "telegram", "catchup_limit", int, 50),
        poll_interval_sec=_ini_get(cfg, "telegram", "poll_interval_sec", int, 300),
        contact_click_delay_sec=_ini_get(cfg, "telegram", "contact_click_delay_sec", float, 2.0),
        flood_sleep_threshold=_ini_get(cfg, "telegram", "flood_sleep_threshold", int, 60),
        min_text_length=_ini_get(cfg, "pipeline", "min_text_length", int, 40),
        retry_limit=_ini_get(cfg, "pipeline", "retry_limit", int, 5),
        retry_interval_sec=_ini_get(cfg, "pipeline", "retry_interval_sec", int, 600),
        log_level=_ini_get(cfg, "logging", "level", str, "INFO").upper(),
        log_file=path("logging", "file", "data/app.log"),
    )


@dataclass
class ChannelConfig:
    username: str
    enabled: bool = True
    click_callbacks: bool = True   # may the resolver press this channel's "get contact" callback button


def _clean_username(raw: str) -> str:
    name = str(raw).strip()
    for prefix in ("https://t.me/", "http://t.me/", "t.me/"):
        if name.lower().startswith(prefix):
            name = name[len(prefix):]
            break
    return name.lstrip("@").strip("/")


def load_channels(path: Path) -> list[ChannelConfig]:
    """Read channels.yaml and return enabled channels with normalized usernames."""
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    result: list[ChannelConfig] = []
    for item in data.get("channels") or []:
        if isinstance(item, str):
            item = {"username": item}
        username = _clean_username(item.get("username", ""))
        enabled = bool(item.get("enabled", True))
        if username and enabled:
            result.append(ChannelConfig(username=username, enabled=True,
                                        click_callbacks=bool(item.get("click_callbacks", True))))
    return result


def setup_logging(settings: Settings) -> None:
    """Log to stderr and a rotating file; quiet noisy libraries."""
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    root = logging.getLogger()
    root.setLevel(getattr(logging, settings.log_level, logging.INFO))
    for h in list(root.handlers):
        root.removeHandler(h)
    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(fmt)
    root.addHandler(stream)
    settings.log_file.parent.mkdir(parents=True, exist_ok=True)
    fh = logging.handlers.RotatingFileHandler(
        settings.log_file, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    fh.setFormatter(fmt)
    root.addHandler(fh)
    for noisy in ("telethon", "httpx", "aiogram"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
