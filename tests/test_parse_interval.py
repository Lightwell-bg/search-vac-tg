import pytest

from src.settings_store import format_interval, parse_interval


@pytest.mark.parametrize("text,sec", [
    ("500", 30000), ("500 мин", 30000), ("30м", 1800), ("30 m", 1800), ("90с", 90), ("90 сек", 90),
    ("90s", 90), ("2ч", 7200), ("2 h", 7200), ("1.5ч", 5400), ("1,5 ч", 5400), (" 1 ", 60),
])
def test_parse_interval_ok(text, sec):
    assert parse_interval(text) == sec


@pytest.mark.parametrize("text", ["0", "abc", "25ч", "", "59с", "-5", "5 лет", "1441"])
def test_parse_interval_errors(text):
    with pytest.raises(ValueError) as e:
        parse_interval(text)
    assert any(ch in str(e.value) for ch in "Интервал")


@pytest.mark.parametrize("sec,text", [
    (60, "1 мин"), (300, "5 мин"), (90, "90 с"), (30000, "8 ч 20 мин"), (3600, "1 ч"),
    (3690, "1 ч 1 мин 30 с"), (86400, "24 ч"),
])
def test_format_interval(sec, text):
    assert format_interval(sec) == text
