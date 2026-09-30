import pytest

from src.config import PROJECT_ROOT, load_channels, load_settings

ENV_KEYS = [
    "TELEGRAM_API_ID", "TELEGRAM_API_HASH", "NOTIFY_BOT_TOKEN", "OWNER_TELEGRAM_ID",
    "OPENROUTER_API_KEY", "OPENROUTER_MODEL", "JEV_FALLBACK_TO_OPENROUTER", "NOTIFY_SCORE", "HIGH_FIT_SCORE", "SHOW_PAID_CONTACT",
    "DATABASE_URL",
]


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for k in ENV_KEYS:
        monkeypatch.delenv(k, raising=False)


def _load(tmp_path, env_text=""):
    env = tmp_path / ".env"
    env.write_text(env_text, encoding="utf-8")
    return load_settings(env_file=env, ini_file=PROJECT_ROOT / "config.ini")


def test_defaults_and_missing(tmp_path, monkeypatch):
    s = _load(tmp_path, "DATABASE_URL=sqlite:///" + (tmp_path / "x.db").as_posix() + "\n")
    assert s.jev_model == "~typesafe/jev-latest"
    assert s.jev_url == "https://openrouter.ai/api/v1/systemone"
    assert s.notify_score == 65 and s.high_fit_score == 80
    assert s.show_paid_contact is False
    assert s.jev_fallback_to_openrouter is True
    assert "TELEGRAM_API_ID" in s.missing_for_run()
    assert "OPENROUTER_API_KEY" in s.missing_for_run()


def test_jev_url_and_model_from_ini(tmp_path):
    ini = tmp_path / "c.ini"
    ini.write_text("[openrouter]\nbase_url = https://example.test/api/v1/\n[jev]\nmodel = my/model\n", encoding="utf-8")
    env = tmp_path / ".env"
    env.write_text("", encoding="utf-8")
    s = load_settings(env_file=env, ini_file=ini)
    assert s.jev_url == "https://example.test/api/v1/systemone"
    assert s.jev_model == "my/model"


def test_openrouter_always_required(tmp_path):
    db = "DATABASE_URL=sqlite:///" + (tmp_path / "x.db").as_posix() + "\n"
    s = _load(tmp_path, db)
    assert "OPENROUTER_API_KEY" in s.missing_for_run() and "OPENROUTER_MODEL" in s.missing_for_run()
    s = _load(tmp_path, db + "OPENROUTER_API_KEY=k\nOPENROUTER_MODEL=m\n")
    assert "OPENROUTER_API_KEY" not in s.missing_for_run() and "OPENROUTER_MODEL" not in s.missing_for_run()


@pytest.mark.parametrize("val,expected", [("true", True), ("1", True), ("yes", True), ("on", True), ("false", False), ("no", False)])
def test_bool_parsing(tmp_path, val, expected):
    s = _load(tmp_path, f"SHOW_PAID_CONTACT={val}\nDATABASE_URL=sqlite:///{(tmp_path / 'x.db').as_posix()}\n")
    assert s.show_paid_contact is expected


def test_invalid_int_names_variable(tmp_path):
    with pytest.raises(ValueError, match="NOTIFY_SCORE"):
        _load(tmp_path, "NOTIFY_SCORE=abc\n")


def test_database_url_conversion(tmp_path):
    s = _load(tmp_path, "DATABASE_URL=sqlite:///" + (tmp_path / "sub" / "a.db").as_posix() + "\n")
    assert s.database_url.startswith("sqlite+aiosqlite:///")
    assert (tmp_path / "sub").is_dir()


def test_relative_database_url_is_absolute(tmp_path):
    s = _load(tmp_path, "DATABASE_URL=sqlite:///data/app.db\n")
    path = s.database_url.removeprefix("sqlite+aiosqlite:///")
    assert path.endswith("data/app.db")
    assert path.startswith(PROJECT_ROOT.as_posix())


def test_paths_absolute(tmp_path):
    s = _load(tmp_path, "DATABASE_URL=sqlite:///" + (tmp_path / "x.db").as_posix() + "\n")
    assert s.channels_file.is_absolute()
    assert s.channels_file.name == "channels.yaml"


def test_load_channels(tmp_path):
    f = tmp_path / "c.yaml"
    f.write_text(
        "channels:\n"
        "  - username: '@One'\n"
        "  - username: https://t.me/Two\n    enabled: true\n"
        "  - username: Off\n    enabled: false\n",
        encoding="utf-8",
    )
    result = load_channels(f)
    assert [c.username for c in result] == ["One", "Two"]


def test_click_callbacks_flag(tmp_path):
    f = tmp_path / "c.yaml"
    f.write_text(
        "channels:\n"
        "  - username: One\n"
        "  - username: Two\n    click_callbacks: false\n",
        encoding="utf-8",
    )
    result = load_channels(f)
    assert [(c.username, c.click_callbacks) for c in result] == [("One", True), ("Two", False)]




