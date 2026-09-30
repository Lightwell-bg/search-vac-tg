"""Executor profile managed from the bot: base + uploaded files + manual overrides.

Layers (the effective profile is what JEV/OpenRouter/applications use):

* BASE      - ``data/profile.json`` (committed, built locally; never overwritten here);
* UPLOADS   - files the owner sent to the bot (``data/materials_uploads/``); the builder runs
              over these files only and the result is cached in ``data/profile_uploads.json``;
* OVERRIDES - ``data/profile_overrides.json`` ``{"add": [...], "remove": [...]}`` (skill names,
              compared case-insensitively).
"""
from __future__ import annotations

import asyncio
import json
import logging
import multiprocessing
import os
import re
import secrets
import sys
from pathlib import Path
from typing import Callable

from .builder import (CV_NAME_RE, Document, derive_from_technologies, extract_projects, find_technologies,
                      read_document)
from .loader import compact_profile, load_profile

log = logging.getLogger(__name__)

EXTRACT_TIMEOUT_SEC = 60
TIMEOUT_ERROR = "Не удалось обработать файл за 60 с"
CHILD_MEMORY_LIMIT = 1024 * 1024 * 1024
IDS_FILE = ".ids.json"

ALLOWED_EXTENSIONS = {".pdf", ".docx", ".md", ".txt"}
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_NAME_LEN = 100
MAX_SKILL_LEN = 40
NO_TEXT_ERROR = "Не удалось извлечь текст (скан?). Пришлите PDF с текстом, DOCX или TXT"
_BAD_CHARS_RE = re.compile(r"[^A-Za-z0-9А-Яа-яЁё._ -]")


def sanitize_filename(filename: str) -> str:
    """Basename only, safe characters, no leading dot, at most 100 chars (extension kept)."""
    name = re.split(r"[\\/]", str(filename or ""))[-1]
    name = _BAD_CHARS_RE.sub("_", name).strip().lstrip(".").strip()
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    stem = stem.strip(" .") or "file"
    ext = ("." + ext) if ext else ""
    if len(stem) + len(ext) > MAX_NAME_LEN:
        stem = stem[:MAX_NAME_LEN - len(ext)]
    return stem + ext


def parse_items(text: str) -> list[str]:
    """Split comma/newline/semicolon separated text into stripped non-empty items."""
    return [p.strip() for p in re.split(r"[,\n;]", text or "") if p.strip()]


def _dedup(items) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for i in items:
        s = str(i).strip()
        if s and s.lower() not in seen:
            seen.add(s.lower())
            out.append(s)
    return out


def _atomic_write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _read_json(path: Path, default):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, type(default)) else default
    except FileNotFoundError:
        return default
    except (OSError, ValueError) as e:
        log.warning("cannot read %s: %s", path, e)
        return default


def _extract_child(conn, path_str: str) -> None:
    """Child process entry: memory-limited extraction, result goes back through the pipe."""
    try:
        if sys.platform.startswith("linux"):
            import resource
            resource.setrlimit(resource.RLIMIT_AS, (CHILD_MEMORY_LIMIT, CHILD_MEMORY_LIMIT))
        text = read_document(Path(path_str), strict=True)
        conn.send(("ok", text))
    except ValueError as e:
        conn.send(("err", str(e)))
    except BaseException:  # noqa: BLE001 - MemoryError and friends
        conn.send(("err", TIMEOUT_ERROR))
    finally:
        conn.close()


def read_document_isolated(path: Path) -> str | None:
    """Blocking: extract text in a separate spawned process with a timeout (call via ``to_thread``).

    Raises ``ValueError`` with a user-facing message on timeout, crash or a suspicious file.
    """
    ctx = multiprocessing.get_context("spawn")
    parent, child = ctx.Pipe(duplex=False)
    proc = ctx.Process(target=_extract_child, args=(child, str(path)), daemon=True)
    proc.start()
    child.close()
    try:
        if not parent.poll(EXTRACT_TIMEOUT_SEC):
            raise ValueError(TIMEOUT_ERROR)
        try:
            kind, value = parent.recv()
        except (EOFError, OSError):
            raise ValueError(TIMEOUT_ERROR) from None
    finally:
        if proc.is_alive():
            proc.terminate()
        proc.join(5)
        parent.close()
    if kind == "err":
        raise ValueError(value)
    return value


def build_uploads_profile(files: list[Path], fingerprint: list | None = None) -> dict:
    """Builder run over the uploaded files only. ``sources`` lists files with extractable text."""
    docs: list[Document] = []
    for p in files:
        try:
            text = read_document_isolated(p)
        except ValueError as e:
            log.warning("upload %s skipped: %s", p.name, e)
            continue
        if text is not None and text.strip():
            docs.append(Document(p, text))
    doc_counts: dict[str, int] = {}
    mentions: dict[str, int] = {}
    for d in docs:
        for tech, n in find_technologies(d.text).items():
            doc_counts[tech] = doc_counts.get(tech, 0) + 1
            mentions[tech] = mentions.get(tech, 0) + n
    techs = sorted(doc_counts, key=lambda t: (-doc_counts[t], -mentions[t], t))
    derived = derive_from_technologies(set(techs))
    return {
        "technologies": techs,
        "skills": derived["skills"],
        "services": derived["services"],
        "strong_matches": derived["strong"],
        "acceptable_matches": derived["acceptable"],
        "portfolio_projects": extract_projects(docs),
        "has_cv": any(CV_NAME_RE.search(d.name) for d in docs),
        "sources": [d.name for d in docs],
        "fingerprint": fingerprint if fingerprint is not None else [],
    }


def _term_re(term: str) -> re.Pattern:
    """Whole-token, case-insensitive match: '/', '-' and spaces are boundaries."""
    return re.compile(r"(?<!\w)" + re.escape(term.strip()) + r"(?!\w)", re.I)


_REMOVABLE_KEYS = ("technologies", "skills", "services", "strong_matches", "acceptable_matches",
                   "weak_matches", "project_types")


def merge_profile(base: dict, uploads: dict | None, overrides: dict | None) -> dict:
    """BASE + UPLOADS-derived, then OVERRIDES (see module docstring)."""
    eff = json.loads(json.dumps(base))  # deep copy, base stays untouched
    up = uploads or {}
    if up.get("sources") or up.get("technologies"):
        eff["technologies"] = _dedup(list(up.get("technologies", [])) + list(eff.get("technologies", [])))
        for key in ("skills", "services", "strong_matches"):
            eff[key] = _dedup(list(eff.get(key, [])) + list(up.get(key, [])))
        eff["acceptable_matches"] = _dedup(list(eff.get("acceptable_matches", []))
                                           + list(up.get("acceptable_matches", [])))
        projects = list(eff.get("portfolio_projects", []))
        names = {str(p.get("name", "")).lower() for p in projects}
        for p in up.get("portfolio_projects", []):
            if str(p.get("name", "")).lower() not in names:
                names.add(str(p.get("name", "")).lower())
                projects.append(p)
        eff["portfolio_projects"] = projects
        if up.get("has_cv") or up.get("portfolio_projects"):
            eff["provisional"] = False
    ov = overrides or {}
    add = _dedup(ov.get("add", []))
    patterns = [_term_re(str(x)) for x in ov.get("remove", []) if str(x).strip()]
    if patterns:
        for key in _REMOVABLE_KEYS:
            eff[key] = [x for x in eff.get(key, []) if not any(p.search(str(x)) for p in patterns)]
        if any(p.search(str(eff.get("summary", ""))) for p in patterns):
            top = ", ".join(str(t) for t in eff.get("technologies", [])[:8])
            eff["summary"] = f"Developer. Main stack: {top}." if top else ""
    for key in ("technologies", "strong_matches"):
        eff[key] = _dedup(add + list(eff.get(key, [])))
    eff["sources"] = list(eff.get("sources", [])) + [f"upload:{n}" for n in up.get("sources", [])]
    eff["uploads"] = list(up.get("sources", []))
    return eff


class ProfileService:
    def __init__(self, settings) -> None:
        self.settings = settings
        data_dir = Path(settings.profile_file).parent
        self.base_file = Path(settings.profile_file)
        self.uploads_dir = data_dir / "materials_uploads"
        self.uploads_cache = data_dir / "profile_uploads.json"
        self.overrides_file = data_dir / "profile_overrides.json"
        self._lock = asyncio.Lock()
        self._subscribers: list[Callable[[dict], None]] = []

    # ------------------------------------------------------------------ reading

    def _base(self) -> dict:
        return load_profile(self.base_file)

    def _overrides(self) -> dict:
        ov = _read_json(self.overrides_file, {})
        return {"add": _dedup(ov.get("add", [])), "remove": _dedup(ov.get("remove", []))}

    def _fingerprint(self) -> list:
        """Sorted ``[name, size, mtime_ns]`` of the uploaded files: detects a cache that drifted."""
        out = []
        if self.uploads_dir.is_dir():
            for p in self.uploads_dir.iterdir():
                if self._is_upload(p):
                    st = p.stat()
                    out.append([p.name, st.st_size, st.st_mtime_ns])
        return sorted(out)

    def _cache_fresh(self) -> tuple[bool, dict]:
        cache = _read_json(self.uploads_cache, {})
        fp = self._fingerprint()
        if not cache:
            return (not fp and not self.uploads_cache.exists()), {}
        return cache.get("fingerprint") == fp, cache

    def _uploads_derived(self) -> dict:
        """Cached uploads profile; empty when missing/stale (``startup()`` rebuilds it)."""
        fresh, cache = self._cache_fresh()
        return cache if fresh else {}

    async def startup(self) -> None:
        """Rebuild the uploads cache if it is missing, corrupt or stale (call before the pipeline starts)."""
        async with self._lock:
            if self.uploads_dir.is_dir():
                for tmp in self.uploads_dir.glob(".upload-*"):
                    tmp.unlink(missing_ok=True)
            if not self._cache_fresh()[0]:
                log.info("profile uploads cache is stale, rebuilding")
                await asyncio.to_thread(self._rebuild_uploads)

    def load(self) -> dict:
        """Effective profile (base + uploads + overrides)."""
        return merge_profile(self._base(), self._uploads_derived(), self._overrides())

    def compact(self) -> str:
        return compact_profile(self.load())

    def status(self) -> dict:
        ov = self._overrides()
        return {"base_projects": len(self._base().get("portfolio_projects", [])),
                "uploads": len(self.list_uploads()), "added": len(ov["add"]), "removed": len(ov["remove"])}

    @staticmethod
    def _is_upload(p: Path) -> bool:
        return p.is_file() and not p.name.startswith(".") and p.suffix.lower() in ALLOWED_EXTENSIONS

    def list_uploads(self) -> list[tuple[str, int]]:
        if not self.uploads_dir.is_dir():
            return []
        out = []
        for p in sorted(self.uploads_dir.iterdir(), key=lambda x: x.name.lower()):
            if self._is_upload(p):
                out.append((p.name, p.stat().st_size))
        return out

    def list_uploads_ids(self) -> list[tuple[str, str, int]]:
        """``(id, name, size)``; ids are random and stable per file (index in ``.ids.json``)."""
        uploads = self.list_uploads()
        ids_path = self.uploads_dir / IDS_FILE
        ids = {k: v for k, v in _read_json(ids_path, {}).items() if isinstance(v, str)}
        names = {n for n, _ in uploads}
        clean = {k: v for k, v in ids.items() if v in names}
        by_name = {v: k for k, v in clean.items()}
        for n in names:
            if n not in by_name:
                new = secrets.token_hex(6)
                clean[new] = n
                by_name[n] = new
        if clean != ids:
            _atomic_write(ids_path, clean)
        return [(by_name[n], n, s) for n, s in uploads]

    def name_for_id(self, upload_id: str) -> str | None:
        return next((n for i, n, _ in self.list_uploads_ids() if i == upload_id), None)

    # ------------------------------------------------------------- subscriptions

    def subscribe(self, callback: Callable[[dict], None]) -> None:
        self._subscribers.append(callback)

    def _notify(self) -> dict:
        profile = self.load()
        for cb in list(self._subscribers):
            try:
                cb(profile)
            except Exception:  # noqa: BLE001 - one bad subscriber must not break the others
                log.exception("profile subscriber failed")
        return profile

    # --------------------------------------------------------------- mutations

    def _rebuild_uploads(self) -> dict:
        """Blocking (PDF/DOCX parsing): call through ``asyncio.to_thread`` from async code."""
        fingerprint = self._fingerprint()
        files = [self.uploads_dir / n for n, _ in self.list_uploads()]
        derived = build_uploads_profile(files, fingerprint)
        _atomic_write(self.uploads_cache, derived)
        return derived

    def _unique_name(self, name: str) -> str:
        existing = {n.lower() for n, _ in self.list_uploads()}
        if name.lower() not in existing:
            return name
        stem, dot, ext = name.rpartition(".")
        stem, ext = (stem, "." + ext) if dot else (name, "")
        i = 2
        while f"{stem}_{i}{ext}".lower() in existing:
            i += 1
        return f"{stem}_{i}{ext}"

    async def add_upload(self, filename: str, content: bytes) -> dict:
        name = sanitize_filename(filename)
        if Path(name).suffix.lower() not in ALLOWED_EXTENSIONS:
            raise ValueError("Допустимые форматы: PDF, DOCX, MD, TXT")
        if not content:
            raise ValueError("Файл пустой")
        if len(content) > MAX_UPLOAD_BYTES:
            raise ValueError(f"Файл больше {MAX_UPLOAD_BYTES // (1024 * 1024)} МБ")
        async with self._lock:
            before = {t.lower() for t in self.load().get("technologies", [])}
            self.uploads_dir.mkdir(parents=True, exist_ok=True)
            name = self._unique_name(name)
            path = self.uploads_dir / name
            tmp = self.uploads_dir / f".upload-{secrets.token_hex(6)}{Path(name).suffix.lower()}"
            tmp.write_bytes(content)
            try:
                text = await asyncio.to_thread(read_document_isolated, tmp)
                if not text or not text.strip():
                    raise ValueError(NO_TEXT_ERROR)
                os.replace(tmp, path)
            except BaseException:
                tmp.unlink(missing_ok=True)
                raise
            try:
                derived = await asyncio.to_thread(self._rebuild_uploads)
            except Exception:
                path.unlink(missing_ok=True)
                raise
            if name not in derived.get("sources", []):
                path.unlink(missing_ok=True)
                await asyncio.to_thread(self._rebuild_uploads)
                raise ValueError(NO_TEXT_ERROR)
            self.list_uploads_ids()  # assign the stable id right away
            profile = self._notify()
        techs = profile.get("technologies", [])
        return {"filename": name,
                "added": [t for t in techs if t.lower() not in before],
                "total": len(techs),
                "projects": len(profile.get("portfolio_projects", [])),
                "provisional": bool(profile.get("provisional"))}

    async def delete_upload(self, name: str) -> bool:
        async with self._lock:
            if name not in {n for n, _ in self.list_uploads()}:
                return False
            (self.uploads_dir / name).unlink(missing_ok=True)
            await asyncio.to_thread(self._rebuild_uploads)
            self.list_uploads_ids()  # prune the id of the deleted file
            self._notify()
        return True

    @staticmethod
    def _validate_items(items: list[str]) -> list[str]:
        clean = [str(i).strip() for i in items if str(i).strip()]
        if not clean:
            raise ValueError("Пришлите хотя бы один навык (через запятую или с новой строки)")
        for i in clean:
            if not 1 <= len(i) <= MAX_SKILL_LEN:
                raise ValueError(f"Навык длиннее {MAX_SKILL_LEN} символов: {i[:20]}…")
        return _dedup(clean)

    async def _change_overrides(self, items: list[str], to: str, other: str) -> list[str]:
        clean = self._validate_items(items)
        async with self._lock:
            ov = self._overrides()
            low = {i.lower() for i in clean}
            ov[other] = [x for x in ov[other] if x.lower() not in low]
            present = {x.lower() for x in ov[to]}
            new = [i for i in clean if i.lower() not in present]
            ov[to] = ov[to] + new
            await asyncio.to_thread(_atomic_write, self.overrides_file, ov)
            self._notify()
        return new

    async def add_skills(self, items: list[str]) -> list[str]:
        """Adding a skill that is in "remove" takes it out of "remove" (and puts it into "add")."""
        return await self._change_overrides(items, "add", "remove")

    async def remove_skills(self, items: list[str]) -> list[str]:
        """Removing a skill that is in "add" takes it out of "add" (and puts it into "remove")."""
        return await self._change_overrides(items, "remove", "add")
