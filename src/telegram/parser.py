"""Telegram post -> plain data, text normalization and contact/budget extraction.

Everything here is pure (no network). ``post_from_message`` is the only place that
knows Telethon types; it imports them lazily so the rest is testable without a client.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import asdict, dataclass, field
from datetime import datetime
from urllib.parse import parse_qs, urlparse

# --------------------------------------------------------------------------- data


@dataclass
class ButtonInfo:
    text: str
    kind: str  # callback | url | buy | url_auth | user_profile | webview | copy | switch_inline | game | other
    row: int = 0
    col: int = 0
    url: str | None = None            # url / url_auth / webview; tg://user?id=N for user_profile
    copy_text: str | None = None      # "copy" button payload (often a @username or email)
    data_hex: str | None = None       # callback payload (bytes as hex)
    requires_password: bool = False   # callback asks for the account 2FA password

    @property
    def data(self) -> bytes | None:
        return bytes.fromhex(self.data_hex) if self.data_hex is not None else None

    def as_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "ButtonInfo":
        return cls(**{k: d.get(k) for k in cls.__dataclass_fields__ if k in d})


@dataclass
class RawPost:
    channel_tg_id: int
    channel_username: str | None
    message_id: int
    date: datetime | None
    text: str
    buttons: list[ButtonInfo] = field(default_factory=list)

    @property
    def url(self) -> str | None:
        if self.channel_username:
            return f"https://t.me/{self.channel_username}/{self.message_id}"
        # public channels always have a username; keep a c/ link as a fallback
        return f"https://t.me/c/{self.channel_tg_id}/{self.message_id}"


@dataclass
class ContactInfo:
    kind: str    # username | tg_link | bot | bot_deeplink | email | url | phone
    value: str
    source: str = "text"

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class NormalizedPost:
    text: str                 # readable normalized text (for rules, JEV, LLM, notification)
    dedup_key: str            # aggressive form used for hashing and fuzzy matching
    text_hash: str
    title: str | None
    budget: str | None
    contacts: list[ContactInfo]


# ------------------------------------------------------------------ normalization

_ZERO_WIDTH = re.compile(r"[​-‏⁠﻿­]")
# lines made only of decorative symbols: ━━━, -----, ═══, ••• etc.
_DECOR_LINE = re.compile(r"^[\s\-_=~*•·▪▫■□◆◇►▶◀➖➗—–━─═┄┈|+#.]{3,}$")
_DECOR_CHARS = re.compile(r"[━─═┄┈▪▫■□◆◇►▶◀➖•·]+")
_SPACES = re.compile(r"[ \t  -   　]+")
_MANY_NL = re.compile(r"\n{3,}")

URL_RE = re.compile(r"(?:https?://|www\.)[^\s<>()\"'«»]+|\b(?:t\.me|telegram\.me)/[^\s<>()\"'«»]+", re.I)
EMAIL_RE = re.compile(r"(?<![\w.+-])[\w.+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
USERNAME_RE = re.compile(r"(?<![\w@./])@([A-Za-z][A-Za-z0-9_]{3,31})\b")
PHONE_RE = re.compile(r"(?<![\w+])\+\d[\d\s\-()]{9,17}\d")


def _strip_emoji(s: str) -> str:
    return "".join(ch for ch in s if unicodedata.category(ch) not in ("So", "Sk", "Cs", "Co"))


def normalize_text(text: str) -> str:
    """Unicode NFKC, remove zero-width chars and decorative separators, tidy whitespace."""
    if not text:
        return ""
    t = unicodedata.normalize("NFKC", text)
    t = _ZERO_WIDTH.sub("", t).replace("\r\n", "\n").replace("\r", "\n")
    lines = []
    for line in t.split("\n"):
        if _DECOR_LINE.match(line):
            continue
        line = _DECOR_CHARS.sub(" ", line)
        line = _SPACES.sub(" ", line).strip()
        lines.append(line)
    t = "\n".join(lines)
    t = _MANY_NL.sub("\n\n", t)
    return t.strip()


def dedup_key(normalized: str) -> str:
    """Form for duplicate detection: lowercase, no links/mentions/emails/emoji/punctuation."""
    t = normalized.lower()
    t = URL_RE.sub(" ", t)
    t = EMAIL_RE.sub(" ", t)
    t = re.sub(r"@\w+", " ", t)
    t = _strip_emoji(t)
    t = re.sub(r"[^\w\s]", " ", t)
    t = re.sub(r"\s+", " ", t)
    return t.strip()


def text_hash(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def extract_title(normalized: str, limit: int = 120) -> str | None:
    for line in normalized.split("\n"):
        clean = _strip_emoji(line).strip(" :-—#*")
        if len(clean) >= 3:
            return clean[:limit]
    return None


# ------------------------------------------------------------------------- budget

_CUR = r"(?:₽|руб(?:лей|\.)?|р\.|rub|\$|usd|долл\w*|€|eur|евро|грн|uah|₸|тенге|usdt)"
_NUM = r"\d[\d\s.,]*(?:\s?(?:k|к|тыс\.?|000))?"
_BUDGET_RES = [
    re.compile(rf"(?:от|до|from|up to)?\s*{_CUR}\s?{_NUM}(?:\s?[-–]\s?{_NUM})?", re.I),
    re.compile(rf"(?:от|до|from|up to)?\s*{_NUM}(?:\s?[-–]\s?{_NUM})?\s?{_CUR}", re.I),
]
_BUDGET_LINE = re.compile(r"(?:бюджет|оплата|цена|стоимость|budget|price|rate)\s*[:\-—]?\s*([^\n]{1,60})", re.I)


def extract_budget(normalized: str) -> str | None:
    line = _BUDGET_LINE.search(normalized)
    scope = line.group(1) if line else normalized
    for rx in _BUDGET_RES:
        m = rx.search(scope)
        if m and re.search(r"\d", m.group(0)):
            return re.sub(r"\s+", " ", m.group(0)).strip(" ,.")
    if line:
        value = line.group(1).strip(" .")
        if re.search(r"договор|обсужд|negotiable|по результат", value, re.I):
            return value[:60]
    return None


# ----------------------------------------------------------------------- contacts

_TG_HOSTS = {"t.me", "telegram.me", "www.t.me", "telegram.dog"}
_TG_RESERVED = {"joinchat", "addstickers", "share", "proxy", "socks", "setlanguage", "addtheme",
                "iv", "c", "s", "addlist", "boost", "m", "invoice", "giftcode", "nft"}


def is_payment_url(url: str) -> bool:
    """t.me/$slug and t.me/invoice/... are Telegram payment (invoice) links."""
    p = urlparse(url if "://" in url else "https://" + url)
    if (p.hostname or "").lower() not in _TG_HOSTS:
        return False
    first = p.path.lstrip("/").split("/", 1)[0].lower()
    return first.startswith("$") or first in ("invoice", "giftcode", "premium", "stars")


def classify_url(url: str) -> ContactInfo | None:
    """Turn a URL into a contact. Returns None for links that are not contacts
    (channel posts, chat invites, payment links)."""
    raw = url.rstrip(".,;:!?)»")
    if raw.lower().startswith("www."):
        raw = "https://" + raw
    if "://" not in raw:
        raw = "https://" + raw
    p = urlparse(raw)
    host = (p.hostname or "").lower()
    if host in _TG_HOSTS:
        if is_payment_url(raw):
            return None
        parts = [x for x in p.path.split("/") if x]
        if not parts or parts[0].startswith("+") or parts[0].lower() in _TG_RESERVED:
            return None
        name = parts[0]
        if len(parts) > 1 and parts[1].isdigit():
            return None  # link to a channel post, not a contact
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{3,31}", name):
            return None
        qs = parse_qs(p.query)
        if name.lower().endswith("bot"):
            kind = "bot_deeplink" if ("start" in qs or "startapp" in qs) else "bot"
            return ContactInfo(kind, raw)
        return ContactInfo("tg_link", f"https://t.me/{name}")
    if not host or "." not in host:
        return None
    return ContactInfo("url", raw)


def extract_contacts(text: str, ignore: set[str] | None = None) -> list[ContactInfo]:
    """Direct contacts found in the text. ``ignore`` = lowercase usernames to skip
    (the channel itself, its admins / ad footers)."""
    ignore = {i.lower().lstrip("@") for i in (ignore or set())}
    found: list[ContactInfo] = []
    seen: set[tuple[str, str]] = set()

    def add(c: ContactInfo | None) -> None:
        if c is None:
            return
        key = (c.kind, c.value.lower())
        if key not in seen:
            seen.add(key)
            found.append(c)

    for m in URL_RE.finditer(text):
        c = classify_url(m.group(0))
        if c and c.kind in ("tg_link", "bot", "bot_deeplink"):
            name = urlparse(c.value).path.strip("/").split("/")[0].lower()
            if name in ignore:
                continue
        add(c)
    for m in re.finditer(r"tg://user\?id=(\d+)", text):  # mention of a user without @username
        add(ContactInfo("tg_link", f"tg://user?id={m.group(1)}"))
    for m in EMAIL_RE.finditer(text):
        add(ContactInfo("email", m.group(0)))
    for m in USERNAME_RE.finditer(text):
        name = m.group(1)
        if name.lower() in ignore:
            continue
        add(ContactInfo("bot" if name.lower().endswith("bot") else "username", "@" + name))
    for m in PHONE_RE.finditer(text):
        digits = re.sub(r"\D", "", m.group(0))
        if 10 <= len(digits) <= 15:
            add(ContactInfo("phone", "+" + digits))
    return found


# Contacts a person can be reached through right away (no bot conversation needed).
DIRECT_KINDS = ("username", "tg_link", "email", "phone", "url")


SHORT_POST_CHARS = 60  # below this the contact is part of the dedup hash


def normalize_post(post: RawPost, ignore_contacts: set[str] | None = None) -> NormalizedPost:
    normalized = normalize_text(post.text)
    key = dedup_key(normalized)
    ignore = set(ignore_contacts or set())
    if post.channel_username:
        ignore.add(post.channel_username.lower())
    contacts = extract_contacts(normalized, ignore)
    hash_src = key
    if len(key) < SHORT_POST_CHARS:
        # identical short ads ("Нужен бот, писать @x") from different clients are different jobs
        who = sorted({c.value.lower() for c in contacts if c.kind in ("username", "tg_link", "email", "phone")})
        hash_src = key + "|" + ",".join(who)
    return NormalizedPost(
        text=normalized,
        dedup_key=key,
        text_hash=text_hash(hash_src),
        title=extract_title(normalized),
        budget=extract_budget(normalized),
        contacts=contacts,
    )


# ------------------------------------------------------------------------ Telethon


# Button kind by TL class name. Telethon <= 1.4x uses one class per button
# (KeyboardButtonCallback ...); newer layers use KeyboardInlineButton(text, type=
# InlineButtonType...). Matching by name works with both and never raises on a
# class that a given Telethon version does not have.
_KIND_BY_NAME = {
    "Buy": "buy",
    "Callback": "callback",
    "Url": "url",
    "UrlAuth": "url_auth",
    "SwitchInline": "switch_inline",
    "Game": "game",
    "UserProfile": "user_profile",
    "WebView": "webview",
    "SimpleWebView": "webview",
    "Copy": "copy",
    "Disabled": "other",
}
_NAME_PREFIXES = ("InputInlineButtonType", "InlineButtonType", "InputKeyboardButton", "KeyboardButton")


def _button_kind(obj) -> str:
    name = type(obj).__name__
    for prefix in _NAME_PREFIXES:
        if name.startswith(prefix):
            return _KIND_BY_NAME.get(name[len(prefix):], "other")
    return "other"


def buttons_from_message(message) -> list[ButtonInfo]:
    """Inline keyboard of a Telethon message -> ButtonInfo list (any Telethon version)."""
    markup = getattr(message, "reply_markup", None)
    if type(markup).__name__ != "ReplyInlineMarkup":
        return []
    result: list[ButtonInfo] = []
    for r, row in enumerate(getattr(markup, "rows", None) or []):
        for c, b in enumerate(getattr(row, "buttons", None) or []):
            text = getattr(b, "text", "") or ""
            # new layer: details live in b.type; old layer: on the button itself
            spec = getattr(b, "type", None)
            spec = spec if spec is not None and not isinstance(spec, (str, int)) else b
            kind = _button_kind(spec)
            info = ButtonInfo(text, kind, r, c)
            if kind == "callback":
                info.data_hex = (getattr(spec, "data", None) or b"").hex()
                info.requires_password = bool(getattr(spec, "requires_password", False))
            elif kind in ("url", "url_auth", "webview"):
                info.url = getattr(spec, "url", None)
            elif kind == "user_profile":
                uid = getattr(spec, "user_id", None)
                info.url = f"tg://user?id={uid}" if uid else None
            elif kind == "copy":
                info.copy_text = getattr(spec, "copy_text", None)
            result.append(info)
    return result


def message_text_with_links(message) -> str:
    """Message text plus URLs hidden behind text links (MessageEntityTextUrl)."""
    from telethon.tl import types as tl

    text = message.message or ""
    extra = []
    for ent in message.entities or []:
        if isinstance(ent, tl.MessageEntityTextUrl) and ent.url and ent.url not in text:
            extra.append(ent.url)
        elif isinstance(ent, tl.MessageEntityMentionName):
            extra.append(f"tg://user?id={ent.user_id}")
    if extra:
        text = text + "\n" + "\n".join(extra)
    return text
