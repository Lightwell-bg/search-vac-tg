from src.telegram.parser import (
    ButtonInfo,
    classify_url,
    dedup_key,
    extract_budget,
    extract_contacts,
    is_payment_url,
    normalize_post,
    normalize_text,
    RawPost,
    text_hash,
)


# ------------------------------------------------------------------ normalize_text


def test_normalize_nfkc():
    assert normalize_text("ＡＢＣ １２３") == "ABC 123"


def test_normalize_zero_width_removed():
    assert normalize_text("при​вет‍ мир﻿") == "привет мир"


def test_normalize_decorative_lines_dropped():
    text = "━━━━━━━━\nНужен бот\n━━━━━━━━\n-----\nОплата 100"
    assert normalize_text(text) == "Нужен бот\nОплата 100"


def test_normalize_whitespace_and_newlines():
    t = normalize_text("  a   b \t c  \r\n\r\n\r\n\r\n d  ")
    assert t == "a b c\n\nd"


def test_normalize_empty():
    assert normalize_text("") == ""
    assert normalize_text(None) == ""


def test_normalize_inline_decor_chars_replaced():
    assert normalize_text("Задача ▪ бот ▪ n8n") == "Задача бот n8n"


# ------------------------------------------------------------------ dedup_key


def test_dedup_key_ignores_links_mentions_emoji_punct_case():
    a = dedup_key(normalize_text("🔥 Нужен Telegram-бот!!! Пишите @client_user https://t.me/foo_bar"))
    b = dedup_key(normalize_text("Нужен telegram бот  пишите @other_guy https://example.com/x"))
    assert a == b == "нужен telegram бот пишите"


def test_dedup_key_removes_email():
    assert "example" not in dedup_key("контакт: a.b@example.com срочно")


def test_text_hash_stable_and_distinct():
    assert text_hash("abc") == text_hash("abc")
    assert text_hash("abc") != text_hash("abd")
    assert len(text_hash("abc")) == 64


def test_normalize_post_same_hash_for_variants():
    p1 = RawPost(1, "chan_one", 1, None, "🔥 Нужен бот для магазина! Оплата 5000 руб.\n@user_one")
    p2 = RawPost(2, "chan_two", 9, None, "Нужен   бот для магазина  Оплата 5000 руб\n@user_one")
    n1, n2 = normalize_post(p1), normalize_post(p2)
    assert n1.dedup_key == n2.dedup_key
    assert n1.text_hash == n2.text_hash


def test_normalize_post_ignores_channel_username_contact():
    p = RawPost(1, "my_channel", 1, None, "Нужен бот. Подписывайтесь @my_channel, пишите @client_user")
    n = normalize_post(p)
    assert [c.value for c in n.contacts] == ["@client_user"]


# ------------------------------------------------------------------ budget


def test_budget_line_rub():
    assert extract_budget("Нужен бот\nБюджет: 50 000 руб") == "50 000 руб"


def test_budget_dollar():
    assert extract_budget("Готов заплатить $800 за проект") == "$800"


def test_budget_from_k():
    b = extract_budget("Оплата от 30к ₽ в месяц")
    assert b is not None and "30к" in b and "₽" in b


def test_budget_negotiable():
    b = extract_budget("Бюджет: договорная")
    assert b is not None and "договор" in b.lower()


def test_budget_none():
    assert extract_budget("Нужен бот для магазина, пишите в личку") is None


# ------------------------------------------------------------------ contacts


def kinds(contacts):
    return [(c.kind, c.value) for c in contacts]


def test_contacts_username():
    assert kinds(extract_contacts("Пишите @client_user")) == [("username", "@client_user")]


def test_contacts_bot_username():
    assert kinds(extract_contacts("Заявки в @orders_bot")) == [("bot", "@orders_bot")]


def test_contacts_tme_user():
    assert kinds(extract_contacts("Мой контакт t.me/client_user")) == [("tg_link", "https://t.me/client_user")]


def test_contacts_tme_https_user():
    assert kinds(extract_contacts("https://t.me/client_user")) == [("tg_link", "https://t.me/client_user")]


def test_contacts_bot_deeplink():
    res = extract_contacts("Откликнуться: https://t.me/somebot?start=abc")
    assert [c.kind for c in res] == ["bot_deeplink"]
    assert "start=abc" in res[0].value


def test_contacts_plain_bot_link():
    assert [c.kind for c in extract_contacts("https://t.me/somebot")] == ["bot"]


def test_contacts_channel_post_ignored():
    assert extract_contacts("см. https://t.me/somechannel/123") == []


def test_contacts_invite_ignored():
    assert extract_contacts("вступай https://t.me/+AbCdEfGh1234") == []


def test_contacts_invoice_dollar_ignored():
    assert extract_contacts("https://t.me/$AbCdEf") == []
    assert extract_contacts("https://t.me/invoice/xyz") == []


def test_contacts_email():
    assert kinds(extract_contacts("mail: john.doe@example.com")) == [("email", "john.doe@example.com")]


def test_contacts_url():
    res = extract_contacts("Сайт https://example.com/jobs/1")
    assert kinds(res) == [("url", "https://example.com/jobs/1")]


def test_contacts_phone():
    res = extract_contacts("Звоните +7 (999) 123-45-67")
    assert kinds(res) == [("phone", "+79991234567")]


def test_contacts_ignore_list_and_channel():
    text = "Реклама @admin_guy, канал t.me/my_channel, пишите @client_user"
    res = extract_contacts(text, {"@Admin_Guy", "my_channel"})
    assert kinds(res) == [("username", "@client_user")]


def test_contacts_dedup_repeated():
    assert len(extract_contacts("@client_user и снова @client_user")) == 1


def test_contacts_email_not_username():
    res = extract_contacts("a@example.com")
    assert [c.kind for c in res] == ["email"]


# ------------------------------------------------------------------ classify/payment


def test_classify_url_variants():
    assert classify_url("https://t.me/client_user").kind == "tg_link"
    assert classify_url("t.me/somebot?start=x").kind == "bot_deeplink"
    assert classify_url("https://t.me/somebot").kind == "bot"
    assert classify_url("https://t.me/+invite") is None
    assert classify_url("https://t.me/chan_name/55") is None
    assert classify_url("https://t.me/$abc") is None
    assert classify_url("https://t.me/joinchat/xyz") is None
    assert classify_url("https://example.com/a").kind == "url"
    assert classify_url("www.example.com/a").kind == "url"
    assert classify_url("https://localhost/a") is None


def test_classify_url_strips_trailing_punct():
    assert classify_url("https://t.me/client_user.").value == "https://t.me/client_user"


def test_is_payment_url():
    assert is_payment_url("https://t.me/$abc")
    assert is_payment_url("t.me/invoice/xyz")
    assert is_payment_url("https://telegram.me/giftcode/abc")
    assert not is_payment_url("https://t.me/client")
    assert not is_payment_url("https://example.com/$abc")


# ------------------------------------------------------------------ ButtonInfo


def test_button_roundtrip_with_bytes():
    b = ButtonInfo("Получить контакт", "callback", 1, 2, data_hex=b"\x00\xffget:1".hex(), requires_password=True)
    d = b.as_dict()
    b2 = ButtonInfo.from_dict(d)
    assert b2 == b
    assert b2.data == b"\x00\xffget:1"


def test_button_from_dict_ignores_unknown_and_missing():
    b = ButtonInfo.from_dict({"text": "x", "kind": "url", "url": "https://a.b", "junk": 1})
    assert b.text == "x" and b.url == "https://a.b" and b.data is None


def test_button_data_none():
    assert ButtonInfo("x", "url").data is None
