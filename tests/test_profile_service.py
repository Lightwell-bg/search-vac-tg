import io
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.bot.menu import MenuHandlers, MenuStates, profile_text
from src.profile import builder as builder_mod
from src.profile import service as svc_mod
from src.profile.loader import compact_profile
from src.profile.service import (MAX_UPLOAD_BYTES, NO_TEXT_ERROR, ProfileService, merge_profile, parse_items,
                                 sanitize_filename)

BASE = {
    "provisional": True, "summary": "S",
    "skills": ["Python", "Telegram bots"], "technologies": ["Python", "aiogram", "Docker"],
    "services": ["Telegram bot development"], "strong_matches": ["Telegram bots"],
    "acceptable_matches": ["Docker/VPS deployment"], "portfolio_projects": [{"name": "Bot A", "stack": []}],
    "reject_categories": ["design"], "sources": ["base.pdf"],
}


def make_service(tmp_path, base=BASE):
    data = tmp_path / "data"
    data.mkdir()
    (data / "profile.json").write_text(json.dumps(base), encoding="utf-8")
    return ProfileService(SimpleNamespace(profile_file=data / "profile.json")), data


# ------------------------------------------------------------------ merge


def test_merge_uploads_then_overrides():
    uploads = {"technologies": ["n8n", "python"], "skills": ["workflow automation"], "services": [],
               "strong_matches": ["workflow automation"], "acceptable_matches": [],
               "portfolio_projects": [{"name": "bot a"}, {"name": "New"}], "has_cv": True, "sources": ["cv.md"]}
    eff = merge_profile(BASE, uploads, {"add": ["Make"], "remove": ["DOCKER", "workflow Automation"]})
    assert eff["technologies"] == ["Make", "n8n", "python", "aiogram"]
    assert eff["strong_matches"] == ["Make", "Telegram bots"]
    assert "Docker/VPS deployment" not in eff["acceptable_matches"]
    assert "workflow automation" not in eff["skills"]
    assert [p["name"] for p in eff["portfolio_projects"]] == ["Bot A", "New"]
    assert eff["provisional"] is False and eff["uploads"] == ["cv.md"]
    assert BASE["technologies"] == ["Python", "aiogram", "Docker"]  # base is not mutated


def test_merge_keeps_provisional_without_cv():
    up = {"technologies": ["n8n"], "sources": ["notes.txt"], "has_cv": False, "portfolio_projects": []}
    assert merge_profile(BASE, up, None)["provisional"] is True


def test_load_without_files_uses_base(tmp_path):
    s, _ = make_service(tmp_path)
    eff = s.load()
    assert eff["technologies"] == BASE["technologies"] and eff["uploads"] == []


# ------------------------------------------------------------------ filenames and limits


def test_sanitize_filename():
    assert sanitize_filename("../../etc/passwd.txt") == "passwd.txt"
    assert sanitize_filename("C:\\x\\..\\Резюме Иван.pdf") == "Резюме Иван.pdf"
    assert sanitize_filename(".hidden.md") == "hidden.md"
    assert sanitize_filename("a<b>:*?.txt") == "a_b____.txt"
    long = sanitize_filename("x" * 300 + ".pdf")
    assert len(long) == 100 and long.endswith(".pdf")


async def test_extension_and_size_limits(tmp_path):
    s, _ = make_service(tmp_path)
    with pytest.raises(ValueError):
        await s.add_upload("a.exe", b"x")
    with pytest.raises(ValueError):
        await s.add_upload("a.txt", b"")
    with pytest.raises(ValueError):
        await s.add_upload("a.txt", b"x" * (MAX_UPLOAD_BYTES + 1))
    assert s.list_uploads() == []


async def test_upload_flow_suffix_and_traversal(tmp_path):
    s, data = make_service(tmp_path)
    notes = []
    s.subscribe(notes.append)
    r = await s.add_upload("../cv.md", "Резюме: n8n, Make.com, Python".encode())
    assert r["filename"] == "cv.md" and "n8n" in r["added"] and "Make" in r["added"]
    assert "Python" not in r["added"] and r["provisional"] is False
    r2 = await s.add_upload("cv.md", b"Zapier automation")
    assert r2["filename"] == "cv_2.md"
    assert [n for n, _ in s.list_uploads()] == ["cv.md", "cv_2.md"]
    assert len(notes) == 2 and "Zapier" in notes[-1]["technologies"]
    assert (data / "profile_uploads.json").exists() and not (tmp_path / "cv.md").exists()
    assert await s.delete_upload("../profile.json") is False
    assert await s.delete_upload("cv_2.md") is True
    assert "Zapier" not in s.load()["technologies"] and len(notes) == 3
    assert json.loads((data / "profile.json").read_text(encoding="utf-8")) == BASE


async def test_scanned_pdf_rejected_and_deleted(tmp_path, monkeypatch):
    s, _ = make_service(tmp_path)
    monkeypatch.setattr(svc_mod, "read_document_isolated", lambda p: None)
    with pytest.raises(ValueError, match="Не удалось извлечь текст"):
        await s.add_upload("scan.pdf", b"%PDF-1.4")
    assert s.list_uploads() == []
    assert NO_TEXT_ERROR.startswith("Не удалось")


async def test_rebuild_runs_in_thread(tmp_path, monkeypatch):
    s, _ = make_service(tmp_path)
    calls = []
    real = svc_mod.asyncio.to_thread

    async def spy(fn, *a, **k):
        calls.append(fn)
        return await real(fn, *a, **k)

    monkeypatch.setattr(svc_mod.asyncio, "to_thread", spy)
    await s.add_upload("a.txt", b"python aiogram")
    assert s._rebuild_uploads in calls or any(getattr(c, "__name__", "") == "_rebuild_uploads" for c in calls)


# ------------------------------------------------------------------ skills


def test_parse_items():
    assert parse_items("a, b\n c;;\n") == ["a", "b", "c"]


async def test_add_remove_conflicts_and_compact(tmp_path):
    s, data = make_service(tmp_path)
    seen = []
    s.subscribe(seen.append)
    assert await s.add_skills(["Make", "make", "Zapier"]) == ["Make", "Zapier"]
    assert "Make" in s.compact()
    assert await s.remove_skills(["make", "Docker"]) == ["make", "Docker"]
    ov = json.loads((data / "profile_overrides.json").read_text(encoding="utf-8"))
    assert ov["add"] == ["Zapier"] and [x.lower() for x in ov["remove"]] == ["make", "docker"]
    assert "Make" not in s.load()["technologies"] and "Docker" not in s.load()["technologies"]
    await s.add_skills(["docker"])
    ov = json.loads((data / "profile_overrides.json").read_text(encoding="utf-8"))
    assert "docker" not in [x.lower() for x in ov["remove"]]
    assert "docker" in [t.lower() for t in s.load()["technologies"]]
    assert len(seen) == 3
    assert s.status()["added"] == 2


async def test_skill_validation(tmp_path):
    s, _ = make_service(tmp_path)
    with pytest.raises(ValueError):
        await s.add_skills(["x" * 41])
    with pytest.raises(ValueError):
        await s.remove_skills([])


# ------------------------------------------------------------------ bot handlers


class FakeState:
    def __init__(self):
        self.state = None

    async def clear(self):
        self.state = None

    async def set_state(self, s):
        self.state = s


def make_msg(text=None, document=None, photo=None, content=b""):
    m = MagicMock()
    m.text, m.document, m.photo = text, document, photo
    m.answer = AsyncMock()
    m.bot.download = AsyncMock(return_value=io.BytesIO(content))
    return m


def make_cb(data):
    cb = MagicMock()
    cb.data = data
    cb.answer = AsyncMock()
    cb.message.edit_text = AsyncMock()
    return cb


def make_menu(tmp_path):
    s, _ = make_service(tmp_path)
    return MenuHandlers(MagicMock(), None, None, s), s


async def test_document_upload_flow(tmp_path):
    menu, s = make_menu(tmp_path)
    st = FakeState()
    st.state = MenuStates.waiting_upload
    doc = SimpleNamespace(file_name="resume.txt", file_size=100)
    msg = make_msg(document=doc, content=b"python n8n")
    await menu.on_upload_input(msg, st)
    assert st.state is None and [n for n, _ in s.list_uploads()] == ["resume.txt"]
    sent = [c.args[0] for c in msg.answer.await_args_list]
    assert sent[0].startswith("Добавлено в профиль: n8n") and "Профиль исполнителя" in sent[1]


async def test_upload_rejections_keep_state(tmp_path):
    menu, s = make_menu(tmp_path)
    st = FakeState()
    st.state = MenuStates.waiting_upload
    msg = make_msg(photo=[object()])
    await menu.on_upload_input(msg, st)
    assert "не фото" in msg.answer.await_args.args[0] and st.state == MenuStates.waiting_upload
    msg = make_msg(text="hi")
    await menu.on_upload_input(msg, st)
    assert "Жду файл" in msg.answer.await_args.args[0]
    big = make_msg(document=SimpleNamespace(file_name="a.pdf", file_size=MAX_UPLOAD_BYTES + 1))
    await menu.on_upload_input(big, st)
    big.bot.download.assert_not_awaited()
    bad = make_msg(document=SimpleNamespace(file_name="a.exe", file_size=10), content=b"x")
    await menu.on_upload_input(bad, st)
    assert "Допустимые форматы" in bad.answer.await_args.args[0] and st.state == MenuStates.waiting_upload


async def test_add_skills_flow(tmp_path):
    menu, s = make_menu(tmp_path)
    st = FakeState()
    await menu.on_callback(make_cb("pf:add"), st)
    assert st.state == MenuStates.waiting_add_skills
    msg = make_msg(text="Make, Zapier")
    await menu.on_add_skills_input(msg, st)
    assert st.state is None and "Make" in s.load()["technologies"]
    assert msg.answer.await_args_list[0].args[0] == "Добавлено: Make, Zapier"
    st.state = MenuStates.waiting_add_skills
    bad = make_msg(text="x" * 50)
    await menu.on_add_skills_input(bad, st)
    assert st.state == MenuStates.waiting_add_skills and "длиннее" in bad.answer.await_args.args[0]


async def test_delete_requires_confirmation(tmp_path):
    menu, s = make_menu(tmp_path)
    await s.add_upload("a.txt", b"python")
    st = FakeState()
    token = s.list_uploads_ids()[0][0]
    cb = make_cb(f"pf:d:{token}")
    await menu.on_callback(cb, st)
    assert s.list_uploads() and "Удалить файл" in cb.message.edit_text.await_args.args[0]
    stale = make_cb("pf:dy:0.0000")
    await menu.on_callback(stale, st)
    assert s.list_uploads()
    assert stale.answer.await_args.kwargs.get("show_alert") is True
    await menu.on_callback(make_cb(f"pf:dy:{token}"), st)
    assert s.list_uploads() == []


async def test_delete_token_is_random_stable_id_and_index_hidden(tmp_path):
    """Finding 6: random per-upload id in a hidden index file, not idx+crc."""
    menu, s = make_menu(tmp_path)
    await s.add_upload("a.txt", b"python")
    await s.add_upload("b.txt", b"n8n")
    ids = {n: i for i, n, _ in s.list_uploads_ids()}
    assert len(ids["a.txt"]) == 12 and ids["a.txt"] != ids["b.txt"]
    assert (s.uploads_dir / ".ids.json").exists()
    assert [n for n, _ in s.list_uploads()] == ["a.txt", "b.txt"]  # hidden index is not an upload
    await s.delete_upload("a.txt")  # indexes shift, b keeps its id
    assert {n: i for i, n, _ in s.list_uploads_ids()} == {"b.txt": ids["b.txt"]}
    assert s.name_for_id(ids["a.txt"]) is None
    await s.startup()
    assert ".ids.json" not in s.load()["uploads"]


def test_profile_text_escaped_and_trimmed():
    t = profile_text({"base_projects": 2}, "<b>" * 3000)
    assert len(t) <= 4096 and "<b>" not in t.split("<pre>")[1].replace("&lt;b&gt;", "")
    assert t.endswith("</pre>")


# ------------------------------------------------------------------ Codex review fixes


def _zip_docx(path, entries):
    import zipfile
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries:
            zf.writestr(name, data)


def test_docx_zip_bomb_rejected(tmp_path):
    """Finding 1: entries / total size / ratio are checked before python-docx opens the file."""
    many = tmp_path / "many.docx"
    _zip_docx(many, [(f"f{i}.xml", b"x") for i in range(1001)])
    ratio = tmp_path / "ratio.docx"
    _zip_docx(ratio, [("word/document.xml", b"0" * 5_000_000)])
    for p in (many, ratio):
        with pytest.raises(ValueError, match="подозрительно"):
            builder_mod.read_document(p, strict=True)
        assert builder_mod.read_document(p) is None  # non-strict (local builder) just skips it


def test_pdf_page_limit_and_early_stop(tmp_path, monkeypatch):
    import sys
    import types
    pulled = []

    class Page:
        def __init__(self, n):
            self.n = n

        def extract_text(self):
            pulled.append(self.n)
            return "python " * 2000

    class Reader:
        def __init__(self, _):
            self.pages = (Page(i) for i in range(10_000))

    monkeypatch.setitem(sys.modules, "pypdf", types.SimpleNamespace(PdfReader=Reader))
    f = tmp_path / "a.pdf"
    f.write_bytes(b"%PDF")
    text = builder_mod.read_document(f)
    assert len(text) == builder_mod.MAX_DOC_CHARS
    assert len(pulled) < builder_mod.MAX_PDF_PAGES  # stopped at MAX_DOC_CHARS, long before 10000 pages


async def test_extraction_runs_in_separate_process_with_timeout(tmp_path, monkeypatch):
    f = tmp_path / "a.txt"
    f.write_text("python aiogram", encoding="utf-8")
    assert svc_mod.read_document_isolated(f) == "python aiogram"  # spawned child, picklable entry point
    monkeypatch.setattr(svc_mod, "EXTRACT_TIMEOUT_SEC", 0.001)
    with pytest.raises(ValueError, match="за 60 с"):
        svc_mod.read_document_isolated(f)
    (tmp_path / "svc").mkdir()
    s, _ = make_service(tmp_path / "svc")
    with pytest.raises(ValueError, match="за 60 с"):
        await s.add_upload("b.txt", b"python")
    assert s.list_uploads() == [] and not list(s.uploads_dir.glob(".upload-*"))


async def test_extraction_crash_reported(tmp_path, monkeypatch):
    monkeypatch.setattr(svc_mod, "_extract_child", _crashing_child)
    f = tmp_path / "a.txt"
    f.write_text("x", encoding="utf-8")
    with pytest.raises(ValueError, match="за 60 с"):
        svc_mod.read_document_isolated(f)


def _crashing_child(conn, path):
    import os
    os._exit(1)


async def test_stale_uploads_cache_is_rebuilt_on_startup(tmp_path):
    """Finding 2: fingerprint of the uploads dir; load() does not parse, startup() rebuilds."""
    s, data = make_service(tmp_path)
    await s.add_upload("a.txt", b"n8n")
    cache = json.loads((data / "profile_uploads.json").read_text(encoding="utf-8"))
    assert cache["fingerprint"] and cache["fingerprint"][0][0] == "a.txt"
    assert "n8n" in s.load()["technologies"]
    # a file appears behind the cache's back (crash between steps, manual copy)
    (s.uploads_dir / "b.txt").write_text("Zapier", encoding="utf-8")
    eff = s.load()  # stale -> base + overrides only, no synchronous parsing
    assert "n8n" not in eff["technologies"] and eff["technologies"] == BASE["technologies"]
    await s.startup()
    assert {"n8n", "Zapier"} <= set(s.load()["technologies"])
    # corrupt cache
    (data / "profile_uploads.json").write_text("{not json", encoding="utf-8")
    assert "n8n" not in s.load()["technologies"]
    await s.startup()
    assert "n8n" in s.load()["technologies"]
    # file removed behind the cache's back
    (s.uploads_dir / "a.txt").unlink()
    await s.startup()
    assert "n8n" not in s.load()["technologies"] and "Zapier" in s.load()["technologies"]


async def test_upload_saved_under_tmp_name_until_parsed(tmp_path, monkeypatch):
    s, _ = make_service(tmp_path)
    seen = []
    real = svc_mod.read_document_isolated

    def spy(p):
        seen.append((p.name, p.exists()))
        return real(p)

    monkeypatch.setattr(svc_mod, "read_document_isolated", spy)
    await s.add_upload("cv.txt", b"python")
    assert seen[0][0].startswith(".upload-") and seen[0][1]
    assert [n for n, _ in s.list_uploads()] == ["cv.txt"] and not list(s.uploads_dir.glob(".upload-*"))


def test_removed_skill_is_absent_from_compact_profile():
    """Finding 3: removal drops every phrase that contains the term as a whole token."""
    base = {
        "summary": "Developer with Docker and VPS", "skills": ["deployment", "workflow automation"],
        "technologies": ["Python", "Docker", "Docker Compose", "Ubuntu VPS"],
        "services": ["deployment on Ubuntu VPS with Docker", "n8n / workflow automation", "Dockerfile tuning"],
        "strong_matches": ["Docker-based deploys"], "acceptable_matches": ["Docker/VPS deployment"],
        "weak_matches": ["docker swarm"], "project_types": ["automation"], "reject_categories": ["design"],
    }
    eff = merge_profile(base, None, {"add": [], "remove": ["Docker", "workflow automation"]})
    text = compact_profile(eff).lower()
    assert "docker" not in text.replace("dockerfile", "") and "workflow automation" not in text
    assert "Dockerfile tuning" in eff["services"]  # not a whole-word match
    assert eff["weak_matches"] == [] and eff["summary"].startswith("Developer. Main stack: Python")
    assert "Ubuntu VPS" in eff["technologies"]


async def test_download_checks_before_download(tmp_path):
    """Finding 4: extension, size presence and size limit are checked before downloading."""
    menu, s = make_menu(tmp_path)
    st = FakeState()
    st.state = MenuStates.waiting_upload
    for doc in (SimpleNamespace(file_name="a.exe", file_size=10),
                SimpleNamespace(file_name="a.txt", file_size=None),
                SimpleNamespace(file_name="a.txt", file_size=0),
                SimpleNamespace(file_name="a.txt", file_size=MAX_UPLOAD_BYTES + 1)):
        msg = make_msg(document=doc, content=b"x")
        await menu.on_upload_input(msg, st)
        msg.bot.download.assert_not_awaited()
        assert st.state == MenuStates.waiting_upload and msg.answer.await_count == 1
    # the real size exceeds the declared one -> rejected after a capped download
    liar = make_msg(document=SimpleNamespace(file_name="a.txt", file_size=10),
                    content=b"x" * (MAX_UPLOAD_BYTES + 5))
    await menu.on_upload_input(liar, st)
    assert "больше" in liar.answer.await_args.args[0] and s.list_uploads() == []
