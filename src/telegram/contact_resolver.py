"""Contact resolver: runs only for jobs already accepted as a fit.

Order:
  0. payment scan of the whole keyboard (Buy button, paid-looking contact button,
     checkout/invoice link) -> immediate STOP with ``paid_contact``;
  1. direct human contact in the text (@username, t.me/user, email, phone);
  2. URL buttons: Telegram user link -> direct; bot / website / form -> external flow;
  3. at most one public callback button labelled like "Получить контакт", only when
     clicking is enabled for the channel. The answer (or the edited post) is scanned
     for payment markers again; a human contact in it -> ``free``.

This module never calls any payment API. The only Telegram action it can take is
``GetBotCallbackAnswerRequest`` on a callback button without a password prompt
(see ``TelegramActions``). A callback handler is opaque server code, so clicking is
configurable per channel (``click_callbacks`` in config/channels.yaml).
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import urlparse

from .parser import (
    ButtonInfo,
    ContactInfo,
    RawPost,
    classify_url,
    extract_contacts,
    is_payment_url,
    normalize_text,
)

log = logging.getLogger(__name__)

DIRECT, FREE, EXTERNAL, PAID, UNKNOWN, MISSING = (
    "direct", "free", "external_contact_flow", "paid_contact", "contact_unknown", "missing")

# Phrases that mention payment but say it is NOT needed; removed before the payment scan.
_NEGATION_RE = re.compile(
    r"бесплатн\w*|без\s+(?:оплат\w*|подписк\w*|регистрац\w*|предоплат\w*)|"
    r"(?:оплат\w*|подписк\w*|предоплат\w*)\s+не\s+(?:требуется|нужн\w*|обязательн\w*)|"
    r"не\s+(?:требует\w*|нужн\w*)\s+(?:оплат\w*|подписк\w*)|"
    r"\bfree\b|free of charge|no (?:payment|subscription) (?:required|needed)",
    re.I,
)
_PAYMENT_RE = re.compile(
    r"stars?\b|звёзд|звезд|⭐|🌟|invoice|инвойс|сч[её]т на оплату|оплат|платн|плати|заплат|"
    r"купит|купи\b|покуп|purchase|\bbuy\b|\bpay\b|payment|paid\b|subscri|подписк|"
    r"premium|премиум|тариф|пополн|баланс|balance|недостаточно|insufficient|top.?up|"
    r"кредит\w* спиш|спиш\w* \d|списан|стоимост\w* контакт|контакт за \d|"
    r"за \d+\s*(?:₽|руб|\$|⭐|stars?|звезд)",
    re.I,
)
_CONTACT_BTN_RE = re.compile(
    r"контакт|связ|отклик|откликн|написат|заказчик|получит|показат|открыт\w* контакт|"
    r"contact|apply|respond|reply|reveal|get in touch|message|dm\b|reach",
    re.I,
)
# Web checkout / donation / billing links (Telegram invoices are handled by is_payment_url).
_PAY_HOSTS = ("stripe.com", "paypal.", "boosty.to", "patreon.com", "donationalerts", "yoomoney.ru",
              "money.yandex", "yookassa", "cloudpayments", "robokassa", "tinkoff.ru/rm", "qiwi.",
              "buymeacoffee", "ko-fi.com", "gumroad.com", "lemonsqueezy", "paddle.com",
              "payform", "prodamus", "tribute", "donate.")
_PAY_PATH = re.compile(r"/(?:checkout|pay|payment|payments|donate|billing|subscribe|invoice|buy|order|cart)(?:/|$|\?)",
                       re.I)


def has_payment_marker(text: str | None) -> bool:
    if not text:
        return False
    return bool(_PAYMENT_RE.search(_NEGATION_RE.sub(" ", text)))


def is_payment_link(url: str | None) -> bool:
    """Telegram invoice link or a web checkout/donation/billing link."""
    if not url:
        return False
    if is_payment_url(url):
        return True
    p = urlparse(url if "://" in url else "https://" + url)
    host = (p.hostname or "").lower()
    if any(h in host or h in (host + p.path.lower()) for h in _PAY_HOSTS):
        return True
    if host.startswith(("pay.", "checkout.", "billing.", "donate.")):
        return True
    return bool(_PAY_PATH.search(p.path or ""))


def is_contact_button(b: ButtonInfo) -> bool:
    return bool(_CONTACT_BTN_RE.search(b.text or ""))


def _paid_button(b: ButtonInfo) -> bool:
    return (b.kind == "buy" or has_payment_marker(b.text)
            or (b.kind in ("url", "url_auth") and is_payment_link(b.url)))


@dataclass
class CallbackAnswer:
    message: str | None = None   # toast/popup text
    url: str | None = None
    alert: bool = False


class TelegramActions(Protocol):
    """The only Telegram operations the resolver may use."""

    async def click_callback(self, post: RawPost, button: ButtonInfo) -> CallbackAnswer: ...

    async def refetch(self, post: RawPost) -> tuple[str, list[ButtonInfo]] | None: ...


@dataclass
class ContactResult:
    status: str
    value: str | None = None
    contacts: list[ContactInfo] = field(default_factory=list)
    detail: str = ""

    @property
    def is_paid(self) -> bool:
        return self.status == PAID


# human contacts: reachable without talking to a bot or opening a form
HUMAN_KINDS = ("username", "tg_link", "email", "phone")
_PRIORITY = {"username": 0, "tg_link": 1, "email": 2, "phone": 3}


def _best_human(contacts: list[ContactInfo]) -> str | None:
    human = sorted((c for c in contacts if c.kind in HUMAN_KINDS), key=lambda c: _PRIORITY[c.kind])
    return human[0].value if human else None


def _new_lines(before: str, after: str) -> str:
    old = {line.strip() for line in normalize_text(before).split("\n")}
    return "\n".join(line for line in normalize_text(after).split("\n") if line.strip() not in old)


class ContactResolver:
    def __init__(self, actions: TelegramActions | None, ignore_contacts: set[str] | None = None,
                 max_clicks: int = 1, click_policy=None):
        """``click_policy(post) -> bool`` decides whether callbacks may be pressed for the
        post's channel (default: allowed when ``actions`` is set)."""
        self.actions = actions
        self.ignore = {x.lower().lstrip("@") for x in (ignore_contacts or set())}
        self.max_clicks = max_clicks
        self.click_policy = click_policy or (lambda post: True)

    def _ignore_for(self, post: RawPost) -> set[str]:
        return self.ignore | ({post.channel_username.lower()} if post.channel_username else set())

    async def resolve(self, post: RawPost, text_contacts: list[ContactInfo],
                      allow_click: bool = True) -> ContactResult:
        """``allow_click=False`` is used when an earlier attempt may already have pressed
        a button (durable claim found on retry): the button is never pressed twice."""
        # 0. payment anywhere in the keyboard's contact path -> STOP before anything else
        for b in post.buttons:
            if b.kind == "buy":
                return ContactResult(PAID, None, [], f"Buy button {b.text!r}")
            if is_contact_button(b) and _paid_button(b):
                return ContactResult(PAID, None, [], f"paid contact button {b.text!r}")

        # 1. human contact in the text
        human = [c for c in text_contacts if c.kind in HUMAN_KINDS]
        if human:
            return ContactResult(DIRECT, _best_human(human), human, "contact in text")
        external: list[ContactInfo] = [c for c in text_contacts if c.kind in ("bot", "bot_deeplink")]
        external += [c for c in text_contacts if c.kind == "url" and not is_payment_link(c.value)]

        # 2. URL buttons (contact-labelled first)
        ignore = self._ignore_for(post)
        url_btns = sorted((b for b in post.buttons if b.kind == "url" and b.url),
                          key=lambda b: 0 if is_contact_button(b) else 1)
        for b in url_btns:
            if is_payment_link(b.url):
                continue  # non-contact donation/support link; contact ones were stopped above
            c = classify_url(b.url)
            if c is None:
                continue
            c.source = "url_button"
            if c.kind == "tg_link":
                if c.value.rsplit("/", 1)[-1].lower() in ignore:
                    continue
                return ContactResult(DIRECT, c.value, [c], f"URL button {b.text!r}")
            if is_contact_button(b) or c.kind in ("bot", "bot_deeplink"):
                external.insert(0, c)  # response form / contact bot
            else:
                external.append(c)
        for b in post.buttons:
            if b.kind == "url_auth" and is_contact_button(b) and b.url:
                external.append(ContactInfo("url", b.url, "url_auth_button"))

        # 3. one public callback button
        callbacks = [b for b in post.buttons if b.kind == "callback" and is_contact_button(b)]
        clicked_detail = ""
        if callbacks and allow_click and self.actions is not None and self.click_policy(post):
            for b in callbacks[: self.max_clicks]:
                if b.requires_password:
                    clicked_detail = f"button {b.text!r} asks for the account password: not pressed"
                    continue
                result = await self._try_callback(post, b)
                if result is not None:
                    return result
        elif callbacks:
            clicked_detail = "callback not pressed (disabled or already attempted)"

        if external:
            return ContactResult(EXTERNAL, external[0].value, external, "bot / form / site")
        if callbacks or any(b.kind == "callback" for b in post.buttons):
            return ContactResult(UNKNOWN, None, [], clicked_detail or "callback buttons gave no contact")
        return ContactResult(MISSING, None, [], "no contact and no buttons")

    async def _try_callback(self, post: RawPost, b: ButtonInfo) -> ContactResult | None:
        try:
            answer = await self.actions.click_callback(post, b)
        except Exception as e:  # bot did not answer, data invalid, flood wait ...
            log.warning("CONTACT_UNKNOWN callback failed msg=%s: %s", post.message_id, e)
            return ContactResult(UNKNOWN, None, [], f"callback error: {type(e).__name__}")

        ignore = self._ignore_for(post)
        if has_payment_marker(answer.message) or is_payment_link(answer.url):
            return ContactResult(PAID, None, [], f"callback answer asks for payment: {(answer.message or answer.url or '')[:100]!r}")
        found = extract_contacts(answer.message or "", ignore)
        if answer.url:
            c = classify_url(answer.url)
            if c:
                found.append(c)
        for c in found:
            c.source = "callback"
        if _best_human(found):
            return ContactResult(FREE, _best_human(found), found, "callback answer")
        other = [c for c in found if c.kind not in HUMAN_KINDS and not is_payment_link(c.value)]

        # the bot may have edited the post instead of answering
        refetched = None
        try:
            refetched = await self.actions.refetch(post)
        except Exception as e:
            log.warning("refetch failed msg=%s: %s", post.message_id, e)
        if refetched:
            new_text, new_buttons = refetched
            added = _new_lines(post.text, new_text)
            old_keys = {(ob.kind, ob.text, ob.url) for ob in post.buttons}
            new_btns = [nb for nb in new_buttons if (nb.kind, nb.text, nb.url) not in old_keys]
            if has_payment_marker(added) or any(_paid_button(nb) for nb in new_btns):
                return ContactResult(PAID, None, [], "post edited with a payment request")
            found = extract_contacts(added, ignore)
            for nb in new_btns:
                if nb.kind == "url" and nb.url:
                    c = classify_url(nb.url)
                    if c:
                        found.append(c)
            for c in found:
                c.source = "edited_message"
            if _best_human(found):
                return ContactResult(FREE, _best_human(found), found, "post edited after click")
            other += [c for c in found if c.kind not in HUMAN_KINDS and not is_payment_link(c.value)]
        if other:
            return ContactResult(EXTERNAL, other[0].value, other, "callback led to a bot/site")
        return None
