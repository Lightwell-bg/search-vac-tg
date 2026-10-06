import ast
from pathlib import Path


from src.telegram.contact_resolver import (
    DIRECT,
    EXTERNAL,
    FREE,
    MISSING,
    PAID,
    UNKNOWN,
    CallbackAnswer,
    ContactResolver,
    has_payment_marker,
)
from src.telegram.parser import ButtonInfo, extract_contacts

from .helpers import FakeActions, make_post

SRC = Path(__file__).resolve().parents[1] / "src"


def cb(text, **kw):
    return ButtonInfo(text, "callback", data_hex=b"get".hex(), **kw)


async def resolve(text="Нужен бот для магазина", buttons=None, actions=None, ignore=None):
    actions = actions if actions is not None else FakeActions()
    post = make_post(text, buttons=buttons or [])
    resolver = ContactResolver(actions, ignore)
    contacts = extract_contacts(post.text, {post.channel_username} | set(ignore or ()))
    return await resolver.resolve(post, contacts), actions


# ------------------------------------------------------------------ direct


async def test_username_in_text_direct_no_click():
    r, a = await resolve("Нужен бот. Пишите @client_user", [cb("Получить контакт")])
    assert r.status == DIRECT and r.value == "@client_user" and a.clicks == 0


async def test_tme_url_in_text_direct():
    r, a = await resolve("Нужен бот. Пишите t.me/client_user")
    assert r.status == DIRECT and r.value == "https://t.me/client_user" and a.clicks == 0


async def test_email_direct():
    r, a = await resolve("Нужен бот. Почта: boss@example.com")
    assert r.status == DIRECT and r.value == "boss@example.com" and a.clicks == 0


async def test_plain_url_no_buttons_direct():
    r, a = await resolve("Нужен бот. Подробности: https://example.com/job/1")
    assert r.status == EXTERNAL and r.value == "https://example.com/job/1" and a.clicks == 0


async def test_channel_username_is_not_a_contact():
    r, _ = await resolve("Нужен бот. Наш канал @chan_one")  # make_post channel is chan_one
    assert r.status == MISSING


async def test_free_word_not_paid_in_text():
    r, a = await resolve("Контакт бесплатно: @client_user")
    assert r.status == DIRECT and a.clicks == 0
    assert not has_payment_marker("Получить контакт бесплатно")


# ------------------------------------------------------------------ free callback


async def test_free_callback_click_once():
    actions = FakeActions(CallbackAnswer("Контакт: @client_user"))
    r, a = await resolve(buttons=[cb("Получить контакт")], actions=actions)
    assert r.status == FREE and r.value == "@client_user"
    assert a.clicks == 1
    assert r.contacts[0].source == "callback"


async def test_callback_answer_free_word_not_paid():
    actions = FakeActions(CallbackAnswer("Контакт бесплатно: @client_user"))
    r, a = await resolve(buttons=[cb("Получить контакт")], actions=actions)
    assert r.status == FREE and a.clicks == 1


async def test_callback_empty_answer_then_edited_post_free():
    actions = FakeActions(CallbackAnswer(), refetched=("Нужен бот для магазина\nКонтакт: @client", []))
    r, a = await resolve(buttons=[cb("Получить контакт")], actions=actions)
    assert r.status == FREE and r.value == "@client"
    assert r.contacts[0].source == "edited_message"
    assert a.clicks == 1 and a.refetches == 1


async def test_callback_only_one_click_even_with_two_buttons():
    actions = FakeActions(CallbackAnswer())
    r, a = await resolve(buttons=[cb("Получить контакт"), cb("Связаться")], actions=actions)
    assert a.clicks == 1


# ------------------------------------------------------------------ URL buttons


async def test_url_button_to_user_direct_no_click():
    b = ButtonInfo("Написать заказчику", "url", url="https://t.me/client")
    r, a = await resolve(buttons=[b])
    assert r.status == DIRECT and r.value == "https://t.me/client" and a.clicks == 0
    assert r.contacts[0].source == "url_button"


async def test_url_button_to_bot_deeplink_external():
    b = ButtonInfo("Откликнуться", "url", url="https://t.me/somebot?start=abc")
    r, a = await resolve(buttons=[b])
    assert r.status == EXTERNAL and "somebot" in r.value and a.clicks == 0


async def test_bot_username_in_text_is_external():
    r, a = await resolve("Нужен бот. Заявки через @orders_bot")
    assert r.status == EXTERNAL and a.clicks == 0


# ------------------------------------------------------------------ paid


async def test_paid_looking_callback_button_not_clicked():
    r, a = await resolve(buttons=[cb("Получить контакт за 50 ⭐")])
    assert r.status == PAID and a.clicks == 0


async def test_callback_answer_insufficient_stars_paid():
    actions = FakeActions(CallbackAnswer("Недостаточно Stars"))
    r, a = await resolve(buttons=[cb("Получить контакт")], actions=actions)
    assert r.status == PAID and a.clicks == 1


async def test_buy_button_paid_not_clicked():
    r, a = await resolve(buttons=[ButtonInfo("Контакт", "buy")])
    assert r.status == PAID and a.clicks == 0


async def test_buy_button_without_contact_label_paid():
    r, a = await resolve(buttons=[ButtonInfo("Pay", "buy")])
    assert r.status == PAID and a.clicks == 0


async def test_invoice_url_button_labelled_contact_paid():
    r, a = await resolve(buttons=[ButtonInfo("Контакт", "url", url="https://t.me/$abcDEF")])
    assert r.status == PAID and a.clicks == 0


async def test_callback_answer_invoice_url_paid():
    actions = FakeActions(CallbackAnswer(None, "https://t.me/invoice/xyz"))
    r, a = await resolve(buttons=[cb("Получить контакт")], actions=actions)
    assert r.status == PAID and a.clicks == 1


async def test_callback_answer_subscription_paid():
    actions = FakeActions(CallbackAnswer("Оформите подписку для доступа к контактам"))
    r, _ = await resolve(buttons=[cb("Получить контакт")], actions=actions)
    assert r.status == PAID


async def test_post_edited_after_click_with_buy_paid():
    actions = FakeActions(CallbackAnswer(), refetched=("Нужен бот для магазина\nКупить контакт", []))
    r, a = await resolve(buttons=[cb("Получить контакт")], actions=actions)
    assert r.status == PAID and a.clicks == 1


async def test_post_edited_with_buy_button_paid():
    actions = FakeActions(CallbackAnswer(), refetched=("Нужен бот для магазина", [ButtonInfo("Открыть", "buy")]))
    r, _ = await resolve(buttons=[cb("Получить контакт")], actions=actions)
    assert r.status == PAID


async def test_requires_password_unknown_not_clicked():
    r, a = await resolve(buttons=[cb("Получить контакт", requires_password=True)])
    assert r.status == UNKNOWN and a.clicks == 0


# ------------------------------------------------------------------ unknown / missing


async def test_unknown_callback_only_not_clicked():
    r, a = await resolve(buttons=[cb("Подробнее")])
    assert r.status == UNKNOWN and a.clicks == 0


async def test_no_buttons_no_contacts_missing():
    r, a = await resolve()
    assert r.status == MISSING and a.clicks == 0


async def test_callback_raising_unknown():
    actions = FakeActions(RuntimeError("flood"))
    r, a = await resolve(buttons=[cb("Получить контакт")], actions=actions)
    assert r.status == UNKNOWN and a.clicks == 1


async def test_callback_no_result_unknown():
    r, a = await resolve(buttons=[cb("Получить контакт")], actions=FakeActions(CallbackAnswer("Готово")))
    assert r.status == UNKNOWN and a.clicks == 1


async def test_refetch_error_tolerated():
    actions = FakeActions(CallbackAnswer(), refetched=RuntimeError("gone"))
    r, _ = await resolve(buttons=[cb("Получить контакт")], actions=actions)
    assert r.status == UNKNOWN


async def test_no_actions_unknown():
    post = make_post("Нужен бот", buttons=[cb("Получить контакт")])
    r = await ContactResolver(None).resolve(post, [])
    assert r.status == UNKNOWN


# ------------------------------------------------------------------ static safety


def _payment_usages(source: str) -> list[str]:
    """Code-level (AST) references to Telegram payment API; docstrings/comments are ignored."""
    tree = ast.parse(source)
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found += [a.name for a in node.names if "payments" in a.name]
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if "payments" in mod or any(a.name == "payments" for a in node.names):
                found.append(f"from {mod} import ...")
            found += [a.name for a in node.names if a.name in ("SendStarsForm", "SendPaymentForm")]
        elif isinstance(node, ast.Attribute):
            if node.attr == "payments" or node.attr in ("SendStarsForm", "SendPaymentForm"):
                found.append(node.attr)
        elif isinstance(node, ast.Name) and node.id in ("payments", "SendStarsForm", "SendPaymentForm"):
            found.append(node.id)
    return found


def test_payment_usage_detector_works():
    assert _payment_usages("from telethon.tl.functions.payments import SendStarsForm")
    assert _payment_usages("await c(functions.payments.SendPaymentForm(1))")
    assert _payment_usages('"""never calls payments.* methods"""') == []


def test_no_payment_api_anywhere_in_src():
    offenders = []
    for path in SRC.rglob("*.py"):
        for usage in _payment_usages(path.read_text(encoding="utf-8")):
            offenders.append(f"{path.relative_to(SRC)}: {usage}")
    assert offenders == []


# ------------------------------------------------------------------ payment scan first / click policy


async def resolve_with(text="Нужен бот для магазина", buttons=None, actions=None, *, allow_click=True,
                       click_policy=None):
    actions = actions if actions is not None else FakeActions()
    post = make_post(text, buttons=buttons or [])
    resolver = ContactResolver(actions, None, click_policy=click_policy)
    contacts = extract_contacts(post.text, {post.channel_username})
    return await resolver.resolve(post, contacts, allow_click=allow_click), actions


async def test_mixed_keyboard_buy_and_contact_callback_paid_not_clicked():
    r, a = await resolve_with(buttons=[ButtonInfo("Купить", "buy"), cb("Получить контакт")])
    assert r.status == PAID and a.clicks == 0


async def test_text_username_plus_paid_callback_is_paid():
    r, a = await resolve_with("Нужен бот. Пишите @client_user", [cb("Контакт за 50 ⭐")])
    assert r.status == PAID and a.clicks == 0


async def test_url_button_to_checkout_domain_paid():
    b = ButtonInfo("Контакт", "url", url="https://buy.stripe.com/abc")
    r, a = await resolve_with(buttons=[b])
    assert r.status == PAID and a.clicks == 0


async def test_callback_answer_boosty_url_paid():
    actions = FakeActions(CallbackAnswer(None, "https://boosty.to/x"))
    r, a = await resolve_with(buttons=[cb("Получить контакт")], actions=actions)
    assert r.status == PAID and a.clicks == 1


async def test_url_button_web_form_is_external_not_direct():
    b = ButtonInfo("Откликнуться", "url", url="https://example.com/form")
    r, a = await resolve_with(buttons=[b])
    assert r.status == EXTERNAL and r.value == "https://example.com/form" and a.clicks == 0


async def test_callback_answer_with_only_web_url_is_external_not_free():
    actions = FakeActions(CallbackAnswer(None, "https://example.com/apply"))
    r, a = await resolve_with(buttons=[cb("Получить контакт")], actions=actions)
    assert r.status == EXTERNAL and a.clicks == 1


async def test_callback_answer_payment_not_required_then_contact_is_free():
    actions = FakeActions(CallbackAnswer("Оплата не требуется. Контакт: @client"))
    r, a = await resolve_with(buttons=[cb("Получить контакт")], actions=actions)
    assert r.status == FREE and r.value == "@client" and a.clicks == 1


async def test_free_contact_button_is_clicked():
    r, a = await resolve_with(buttons=[cb("Получить бесплатный контакт")])
    assert a.clicks == 1 and r.status == UNKNOWN


async def test_requires_password_never_clicked_even_if_free_label():
    r, a = await resolve_with(buttons=[cb("Получить бесплатный контакт", requires_password=True)])
    assert r.status == UNKNOWN and a.clicks == 0


async def test_allow_click_false_does_not_click():
    actions = FakeActions(CallbackAnswer("Контакт: @client_user"))
    r, a = await resolve_with(buttons=[cb("Получить контакт")], actions=actions, allow_click=False)
    assert r.status == UNKNOWN and a.clicks == 0


async def test_click_policy_false_does_not_click():
    actions = FakeActions(CallbackAnswer("Контакт: @client_user"))
    r, a = await resolve_with(buttons=[cb("Получить контакт")], actions=actions,
                              click_policy=lambda post: False)
    assert r.status == UNKNOWN and a.clicks == 0


async def test_click_policy_true_clicks():
    actions = FakeActions(CallbackAnswer("Контакт: @client_user"))
    r, a = await resolve_with(buttons=[cb("Получить контакт")], actions=actions,
                              click_policy=lambda post: post.channel_username == "chan_one")
    assert r.status == FREE and a.clicks == 1
