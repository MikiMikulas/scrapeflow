"""FastAPI app: HTML dashboard + JSON API."""

import json
import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated
from urllib.parse import quote, urlparse

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field, HttpUrl
from sqlmodel import Session, col, func, or_, select

from . import __version__, ai, auth, flow, llm, runlog, scheduler, storage
from .checker import is_running, mark_interrupted, run_check
from .config import get_settings
from .crawler import FileLink
from .db import get_engine, get_session, init_db
from .files import download
from .models import SCHEDULES, CheckRun, FileOutput, Schedule, Setting, Site, SiteFile, User, utcnow
from .presets import MODEL_PRESETS, STORAGE_PRESETS, ModelPreset, StoragePreset

log = logging.getLogger(__name__)
BASE = Path(__file__).parent
PAGE_SIZE = 100


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    mark_interrupted()
    if get_settings().scheduler_enabled:
        scheduler.start()
    yield
    scheduler.shutdown()


PUBLIC_PATHS = {"/login", "/setup", "/healthz"}


class Redirect(Exception):
    def __init__(self, url: str):
        self.url = url


def require_auth(request: Request) -> None:
    """Every page and API call needs a signed-in user (or an API key), except the sign-in pages."""
    request.state.user = None
    if get_settings().auth_disabled or request.url.path in PUBLIC_PATHS:
        return
    with Session(get_engine()) as session:
        user = auth.current_user(request, session)
        if user is not None:
            session.expunge(user)
            request.state.user = user
            return
        has_users = auth.has_users(session)
    if request.url.path.startswith("/api/") or "authorization" in request.headers:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED,
                            "Sign in, or send an API key as 'Authorization: Bearer …'",
                            headers={"WWW-Authenticate": "Bearer"})
    if not has_users:
        raise Redirect("/setup")
    target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    raise Redirect("/login" + (f"?next={quote(target)}" if target != "/" else ""))


app = FastAPI(title="ScrapeFlow", version=__version__, lifespan=lifespan,
              dependencies=[Depends(require_auth)])
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
templates = Jinja2Templates(directory=BASE / "templates")
templates.env.globals["GITHUB_URL"] = "https://github.com/MikiMikulas/scrapeflow"


@app.exception_handler(Redirect)
async def _redirect(request: Request, exc: Redirect):
    return RedirectResponse(exc.url, status_code=303)
templates.env.globals["version"] = __version__
templates.env.globals["SCHEDULES"] = SCHEDULES

SessionDep = Annotated[Session, Depends(get_session)]


def _aware(dt: datetime) -> datetime:
    # SQLite may hand back naive datetimes; everything is stored in UTC.
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _ago(dt) -> str:
    if dt is None:
        return "never"
    seconds = int((utcnow() - _aware(dt)).total_seconds())
    future = seconds < 0
    seconds = abs(seconds)
    for unit, size in (("d", 86400), ("h", 3600), ("min", 60)):
        if seconds >= size:
            text = f"{seconds // size} {unit}"
            break
    else:
        return "now" if future else "just now"
    return f"in {text}" if future else f"{text} ago"


def _date(dt) -> str:
    return "–" if dt is None else _aware(dt).strftime("%d %b %Y, %H:%M UTC")


def _filesize(n) -> str:
    if n is None:
        return "–"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def _domain(url: str) -> str:
    return urlparse(url).netloc.removeprefix("www.")


def _path(url: str | None) -> str:
    if not url:
        return ""
    p = urlparse(url)
    return (p.path or "/") + (f"?{p.query}" if p.query else "")


def _duration(run: CheckRun) -> str:
    if run.finished_at is None:
        return "running"
    seconds = int((_aware(run.finished_at) - _aware(run.started_at)).total_seconds())
    return f"{seconds // 60} min {seconds % 60} s" if seconds >= 60 else f"{seconds} s"


_FTYPES = {
    "pdf": ("pdf",), "doc": ("doc", "docx", "odt", "rtf", "txt", "md", "pages", "isdoc"),
    "sheet": ("xls", "xlsx", "ods", "csv", "numbers"), "slides": ("ppt", "pptx", "odp", "key"),
    "archive": ("zip", "rar", "7z", "tar", "gz", "tgz", "bz2", "xz", "iso", "dmg"),
    "image": ("jpg", "jpeg", "png", "gif", "svg", "webp", "heic", "tif", "tiff", "bmp", "dwg", "dxf"),
    "media": ("mp3", "mp4", "wav", "mov", "avi", "mkv", "webm", "ogg", "flac"),
}


def _ftype(filename: str) -> tuple[str, str]:
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    group = next((g for g, exts in _FTYPES.items() if ext in exts), "")
    return group, (ext[:4] or "file")


_COLORS = ("#1570ef", "#7a5af8", "#0e9384", "#e04f16", "#dd2590", "#079455", "#444ce7", "#c4320a", "#0086c9")


def _color(text: str) -> str:
    return _COLORS[sum(map(ord, text or "")) % len(_COLORS)]


templates.env.filters.update(color=_color, ago=_ago, date=_date, filesize=_filesize, domain=_domain, path=_path,
                             ftype=_ftype, duration=_duration)


def _get_site(session: Session, site_id: int) -> Site:
    site = session.get(Site, site_id)
    if site is None:
        raise HTTPException(404, "Website not found")
    return site


def _setup_state(session: Session) -> dict:
    backend, _ = storage.load_options(session)
    return {"model": ai.ai_available(), "storage": backend,
            "storage_saved": session.get(Setting, "storage_backend") is not None}


def _site_stats(session: Session) -> dict[int, dict]:
    rows = session.exec(
        select(SiteFile.site_id, func.count(SiteFile.id), func.sum(SiteFile.size))
        .where(SiteFile.status == "stored").group_by(SiteFile.site_id)
    ).all()
    return {site_id: {"files": n, "size": size or 0} for site_id, n, size in rows}


# --------------------------------------------------------------------------- HTML: websites


@app.get("/", response_class=HTMLResponse)
def websites(request: Request, session: SessionDep):
    sites = session.exec(select(Site).order_by(Site.created_at.desc())).all()
    return templates.TemplateResponse(request, "websites.html", {
        "sites": sites, "stats": _site_stats(session), "setup": _setup_state(session),
        "running": {s.id for s in sites if is_running(s.id)}, "new_site": Site(name="", url=""),
    })


def _apply_form(site: Site, form) -> list[str]:
    errors = []

    def text(key):
        return (form.get(key) or "").strip() or None

    def number(key, default, low, high):
        raw = text(key)
        if raw is None:
            return default
        try:
            return min(max(int(raw), low), high)
        except ValueError:
            errors.append(f"{key.replace('_', ' ').capitalize()} must be a whole number")
            return default

    url = text("url") or ""
    if url and "://" not in url:
        url = "https://" + url  # allow typing just a domain
    site.url = url
    if not url.startswith(("http://", "https://")) or not urlparse(url).hostname:
        errors.append("Enter a website address, e.g. example.com")
    site.name = text("name") or _domain(url) or ""
    try:
        site.schedule = Schedule(form.get("schedule") or Schedule.WEEKLY)
    except ValueError:
        errors.append("Unknown check frequency")
    site.max_depth = number("max_depth", site.max_depth if site.id else 3, 0, 20)
    site.max_pages = number("max_pages", site.max_pages if site.id else 300, 1, 100_000)
    site.file_types = text("file_types")
    site.storage_folder = text("storage_folder")
    if site.id is None:
        site.download_existing = form.get("download_existing") == "on"
    site.active = form.get("active") == "on" if site.id else True
    return errors


@app.post("/sites")
async def create_site_form(request: Request, session: SessionDep):
    site = Site(name="", url="")
    form = await request.form()
    errors = _apply_form(site, form)
    if errors:
        sites = session.exec(select(Site).order_by(Site.created_at.desc())).all()
        return templates.TemplateResponse(request, "websites.html", {
            "sites": sites, "stats": _site_stats(session), "setup": _setup_state(session), "running": set(),
            "new_site": site, "errors": errors,
        }, status_code=422)
    session.add(site)
    session.commit()
    session.refresh(site)
    if scheduler.scheduler.running:
        scheduler.start_check(site.id)  # the first crawl starts right away
    return RedirectResponse(f"/sites/{site.id}", status_code=303)


@app.get("/sites/{site_id}", response_class=HTMLResponse)
def site_detail(request: Request, site_id: int, session: SessionDep, tab: str = "files", q: str = "",
                filter: str = "", page: int = 1, run: int | None = None):
    site = _get_site(session, site_id)
    counts = dict(session.exec(
        select(SiteFile.status, func.count(SiteFile.id)).where(SiteFile.site_id == site_id)
        .group_by(SiteFile.status)
    ).all())
    query = select(SiteFile).where(SiteFile.site_id == site_id)
    if q.strip():
        like = f"%{q.strip()}%"
        query = query.where(or_(col(SiteFile.filename).ilike(like), col(SiteFile.link_text).ilike(like),
                                col(SiteFile.url).ilike(like)))
    if filter == "removed":
        query = query.where(SiteFile.on_site == False)  # noqa: E712
    elif filter:
        query = query.where(SiteFile.status == filter)
    removed = session.exec(select(func.count(SiteFile.id)).where(
        SiteFile.site_id == site_id, SiteFile.on_site == False)).one()  # noqa: E712
    total = session.exec(select(func.count()).select_from(query.subquery())).one()
    page = max(page, 1)
    files = session.exec(
        query.order_by(col(SiteFile.updated_at).desc().nulls_last(), col(SiteFile.first_seen).desc(),
                       col(SiteFile.id).desc())
        .offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE)
    ).all()
    runs = session.exec(
        select(CheckRun).where(CheckRun.site_id == site_id).order_by(CheckRun.started_at.desc()).limit(50)
    ).all()
    outputs: dict[int, list[FileOutput]] = {}
    for o in session.exec(select(FileOutput).where(col(FileOutput.site_file_id).in_([f.id for f in files]))
                          .order_by(FileOutput.id)):
        outputs.setdefault(o.site_file_id, []).append(o)
    stats = _site_stats(session).get(site_id, {"files": 0, "size": 0})
    return templates.TemplateResponse(request, "site.html", {
        "site": site, "tab": tab, "files": files, "runs": runs, "counts": counts, "stats": stats,
        "removed": removed, "outputs": outputs,
        "q": q, "filter": filter, "page": page, "pages": max(1, -(-total // PAGE_SIZE)), "total": total,
        "running": is_running(site_id), "next_run": scheduler.next_run(site),
        "selected_run": run or (runs[0].id if runs else None),
        "setup": _setup_state(session),
    })


@app.post("/sites/{site_id}")
async def update_site_form(request: Request, site_id: int, session: SessionDep):
    site = _get_site(session, site_id)
    errors = _apply_form(site, await request.form())
    if errors:
        session.rollback()
        return RedirectResponse(f"/sites/{site_id}?tab=settings&toast={quote(errors[0])}", status_code=303)
    session.add(site)
    session.commit()
    return RedirectResponse(f"/sites/{site_id}?tab=settings&toast=Saved", status_code=303)


@app.post("/sites/{site_id}/check")
async def check_now_form(site_id: int, session: SessionDep):
    _get_site(session, site_id)
    if not is_running(site_id):
        scheduler.start_check(site_id)
    return RedirectResponse(f"/sites/{site_id}?toast=Check+started", status_code=303)


@app.post("/sites/{site_id}/reprocess")
async def reprocess_form(site_id: int, session: SessionDep):
    _get_site(session, site_id)
    if not is_running(site_id):
        scheduler.start_check(site_id, reprocess=True)
    return RedirectResponse(f"/sites/{site_id}?tab=activity&toast=Reprocessing+started", status_code=303)


def _script_json(raw: str) -> str:
    """JSON that is safe inside <script type="application/json"> (user code may contain "</script>")."""
    return raw.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


@app.get("/sites/{site_id}/flow", response_class=HTMLResponse)
def flow_page(request: Request, site_id: int, session: SessionDep):
    site = _get_site(session, site_id)
    flow_def = flow.load(site.flow, site.ai_filter)
    files = session.exec(select(SiteFile).where(SiteFile.site_id == site_id, SiteFile.status != "skipped")
                         .order_by(col(SiteFile.updated_at).desc().nulls_last(), col(SiteFile.id).desc())
                         .limit(300)).all()
    return templates.TemplateResponse(request, "flow.html", {
        "site": site, "flow_json": _script_json(json.dumps(flow_def, ensure_ascii=False)),
        "node_types_json": _script_json(flow.node_types_json()),
        "files": files, "running": is_running(site_id), "model": ai.ai_available(),
    })


@app.post("/sites/{site_id}/toggle")
def toggle_site(site_id: int, session: SessionDep):
    site = _get_site(session, site_id)
    site.active = not site.active
    session.add(site)
    session.commit()
    return RedirectResponse(f"/sites/{site_id}?toast=" + ("Resumed" if site.active else "Paused"),
                            status_code=303)


@app.post("/sites/{site_id}/delete")
async def delete_site_form(site_id: int, session: SessionDep):
    await _delete_site(session, _get_site(session, site_id))
    return RedirectResponse("/?toast=Website+removed", status_code=303)


async def _delete_site(session: Session, site: Site) -> None:
    """Delete a site and its history. Files already saved to storage are kept."""
    await scheduler.cancel_check(site.id)
    file_ids = select(SiteFile.id).where(SiteFile.site_id == site.id)
    for row in session.exec(select(FileOutput).where(col(FileOutput.site_file_id).in_(file_ids))):
        session.delete(row)
    for model in (SiteFile, CheckRun):
        for row in session.exec(select(model).where(model.site_id == site.id)):
            session.delete(row)
    session.flush()
    session.delete(site)
    session.commit()


@app.get("/files/{file_id}/download")
async def download_file(file_id: int, session: SessionDep):
    record = session.get(SiteFile, file_id)
    if record is None or record.status != "stored" or not record.storage_path:
        raise HTTPException(404, "File is not in storage")
    return await _stream_stored(record.storage_backend, record.storage_path, record.filename, record.content_type)


@app.get("/outputs/{output_id}/download")
async def download_output(output_id: int, session: SessionDep):
    output = session.get(FileOutput, output_id)
    if output is None:
        raise HTTPException(404, "Output not found")
    return await _stream_stored(output.storage_backend, output.storage_path, output.filename, output.content_type)


async def _stream_stored(backend_name: str | None, storage_path: str, filename: str, content_type: str | None):
    try:
        backend = storage.get_storage(backend_name)
        if isinstance(backend, storage.LocalStorage):
            path = backend.local_path(storage_path)
            if not path.is_file():
                raise HTTPException(404, "File no longer exists in storage")
            return FileResponse(path, filename=filename, media_type=content_type)
        # Stream through ScrapeFlow: presigned URLs of internal endpoints (e.g. http://minio:9000)
        # would not resolve in the viewer's browser.
        stream = backend.open(storage_path)
        first = await anext(stream)  # surface storage errors before headers are sent
    except storage.StorageError as e:
        raise HTTPException(502, str(e)) from e

    async def body():
        yield first
        async for chunk in stream:
            yield chunk

    disposition = "attachment; filename*=UTF-8''" + quote(filename)
    return StreamingResponse(body(), media_type=content_type or "application/octet-stream",
                             headers={"Content-Disposition": disposition})


# --------------------------------------------------------------------------- HTML: sign-in


def _safe_next(target: str | None) -> str:
    return target if target and target.startswith("/") and not target.startswith("//") else "/"


def _signed_in(request: Request, session: Session, user: User, target: str = "/") -> RedirectResponse:
    response = RedirectResponse(_safe_next(target), status_code=303)
    secure = get_settings().secure_cookies
    response.set_cookie(auth.COOKIE, auth.start_session(session, user), max_age=auth.SESSION_DAYS * 86400,
                        httponly=True, samesite="lax",
                        secure=request.url.scheme == "https" if secure is None else secure)
    return response


@app.get("/setup", response_class=HTMLResponse)
def setup_page(request: Request, session: SessionDep):
    if auth.has_users(session):
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse(request, "auth.html", {"mode": "setup"})


@app.post("/setup")
async def setup_form(request: Request, session: SessionDep):
    if auth.has_users(session):
        return RedirectResponse("/login", status_code=303)
    form = await request.form()
    email, name = (form.get("email") or "").strip(), (form.get("name") or "").strip()
    password = form.get("password") or ""
    error = None
    if "@" not in email or "." not in email.rsplit("@", 1)[-1]:
        error = "Enter a valid e-mail address"
    else:
        error = auth.password_problem(password, form.get("confirm") or "")
    if error:
        return templates.TemplateResponse(request, "auth.html", {"mode": "setup", "error": error, "email": email,
                                                                 "name": name}, status_code=422)
    user = auth.create_user(session, email=email, name=name, password=password)
    return _signed_in(request, session, user)


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, session: SessionDep, next: str = "/"):
    if get_settings().auth_disabled:
        return RedirectResponse("/", status_code=303)
    if not auth.has_users(session):
        return RedirectResponse("/setup", status_code=303)
    if auth.current_user(request, session):
        return RedirectResponse(_safe_next(next), status_code=303)
    return templates.TemplateResponse(request, "auth.html", {"mode": "login", "next": next})


@app.post("/login")
async def login_form(request: Request, session: SessionDep):
    form = await request.form()
    email, target = (form.get("email") or "").strip(), form.get("next") or "/"
    ip = request.client.host if request.client else "?"
    if wait := auth.locked_for(ip):
        error = f"Too many attempts – try again in {wait} s"
    elif user := auth.authenticate(session, email, form.get("password") or ""):
        auth.clear_failures(ip)
        return _signed_in(request, session, user, target)
    else:
        auth.record_failure(ip)
        error = "Wrong e-mail or password"
    return templates.TemplateResponse(request, "auth.html", {"mode": "login", "error": error, "email": email,
                                                             "next": target}, status_code=401)


@app.post("/logout")
def logout(request: Request, session: SessionDep):
    auth.end_session(session, request.cookies.get(auth.COOKIE))
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(auth.COOKIE)
    return response


@app.get("/settings/account", response_class=HTMLResponse)
def account_page(request: Request, session: SessionDep):
    return templates.TemplateResponse(request, "account.html", {"user": request.state.user})


def _account_user(request: Request, session: Session) -> User:
    if request.state.user is None:
        raise HTTPException(400, "Accounts are disabled (SCRAPEFLOW_AUTH_DISABLED)")
    return session.get(User, request.state.user.id)


@app.post("/settings/account")
async def account_form(request: Request, session: SessionDep):
    user = _account_user(request, session)
    form = await request.form()
    action, error, toast, api_key = form.get("action"), None, None, None
    if action == "profile":
        email = (form.get("email") or "").strip().lower()
        taken = session.exec(select(User).where(User.email == email, User.id != user.id)).first()
        if "@" not in email:
            error = "Enter a valid e-mail address"
        elif taken:
            error = "Another account uses this e-mail"
        else:
            user.email, user.name = email, (form.get("name") or "").strip() or user.name
            toast = "Profile saved"
    elif action == "password":
        if not auth.verify_password(form.get("current") or "", user.password_hash):
            error = "The current password is wrong"
        elif not (error := auth.password_problem(form.get("password") or "", form.get("confirm") or "")):
            user.password_hash = auth.hash_password(form.get("password"))
            auth.end_all_sessions(session, user, keep=request.cookies.get(auth.COOKIE))
            toast = "Password changed – other browsers were signed out"
    elif action == "api_key":
        api_key = auth.new_api_key(session, user)
        toast = "New API key created"
    elif action == "revoke_api_key":
        user.api_key_hash = user.api_key_hint = None
        toast = "API key revoked"
    session.add(user)
    session.commit()
    session.refresh(user)
    if error is None and api_key is None:
        return RedirectResponse(f"/settings/account?toast={quote(toast or 'Saved')}", status_code=303)
    return templates.TemplateResponse(request, "account.html", {"user": user, "error": error, "api_key": api_key,
                                                                "active_form": action},
                                      status_code=422 if error else 200)


# --------------------------------------------------------------------------- HTML: settings


def _model_cards() -> list[ModelPreset]:
    cards = list(MODEL_PRESETS)
    builtin = {p.provider for p in MODEL_PRESETS}
    for name, cls in llm.providers().items():  # plugins get a card of their own
        if name not in builtin:
            cards.append(ModelPreset(name, cls.label or name, name, cls.description,
                                     "" if cls.needs_base_url else None, tags=("LLM", "Plugin")))
    return cards


@app.get("/settings")
def settings_redirect():
    return RedirectResponse("/settings/models", status_code=307)


@app.get("/settings/models", response_class=HTMLResponse)
def models_page(request: Request, session: SessionDep):
    cfg = llm.load_config(session)
    cards = _model_cards()
    configured = llm.get_provider(cfg) is not None
    active = None
    if configured:
        active = next((c for c in cards if c.key == cfg.preset), None) or next(
            (c for c in cards if c.provider == cfg.provider and (c.base_url or "") == (cfg.base_url or "")),
            next(c for c in cards if c.key == "custom"),
        )
    return templates.TemplateResponse(request, "models.html", {
        "cfg": cfg, "key_info": cfg.masked(), "cards": cards, "active": active, "configured": configured,
    })


@app.post("/settings/models")
async def save_model_form(request: Request, session: SessionDep):
    form = await request.form()
    cards = {c.key: c for c in _model_cards()}
    card = cards.get(form.get("preset", ""))
    if card is None:
        raise HTTPException(422, "Unknown model provider")
    values = {"preset": card.key, "provider": card.provider,
              "base_url": form.get("base_url", "") if card.base_url is not None else "",
              "model": form.get("model", ""), "effort": form.get("effort", "low")}
    current = llm.load_config(session)
    if form.get("api_key"):
        values["api_key"] = form.get("api_key")
    elif current.provider != card.provider or (current.base_url or "") != (values["base_url"] or ""):
        values["api_key"] = ""  # a key for another service must not leak to this one
    llm.save_config(session, values)
    return RedirectResponse("/settings/models?toast=Model+saved", status_code=303)


@app.post("/settings/models/remove")
def remove_model_form(session: SessionDep):
    llm.save_config(session, {"preset": "", "base_url": "", "model": "", "api_key": ""})
    return RedirectResponse("/settings/models?toast=Model+removed", status_code=303)


def _storage_cards() -> list[StoragePreset]:
    cards = list(STORAGE_PRESETS)
    builtin = {p.backend for p in STORAGE_PRESETS}
    for name, cls in storage.backends().items():
        if name not in builtin:
            cards.append(StoragePreset(name, cls.label or name, name, cls.description, tags=("Plugin",)))
    return cards


@app.get("/settings/storage", response_class=HTMLResponse)
def storage_page(request: Request, session: SessionDep):
    active_backend, per_backend = storage.all_options(session)
    preset_row = session.get(Setting, "storage_preset")
    cards = _storage_cards()
    active = next((c for c in cards if preset_row and c.key == preset_row.value and c.backend == active_backend),
                  None) or next(c for c in cards if c.backend == active_backend)
    backends = storage.backends()
    # Form values per card: what was saved for its backend, else the card's defaults. Secrets stay empty.
    values = {}
    for card in cards:
        cls = backends.get(card.backend)
        saved = per_backend.get(card.backend, {})
        use_saved = card.key == active.key or (saved and card.backend != active_backend)
        values[card.key] = {f.name: "" if f.secret else (saved.get(f.name) if use_saved and saved.get(f.name)
                                                        else card.defaults.get(f.name, ""))
                            for f in (cls.fields if cls else [])}
        values[card.key]["_secrets_set"] = [f.name for f in (cls.fields if cls else [])
                                            if f.secret and saved.get(f.name)]
    target = None
    try:
        target = storage.get_storage(active_backend).describe()
    except storage.StorageError:
        pass
    return templates.TemplateResponse(request, "storage.html", {
        "cards": cards, "active": active, "backends": backends, "values": values, "target": target,
    })


def _storage_values(backend: str, raw: dict, current: dict[str, str]) -> dict[str, str]:
    """Form/API values for a backend; empty secret fields keep the stored secret."""
    cls = storage.backends()[backend]
    values = {}
    for f in cls.fields:
        value = raw.get(f.name)
        if f.kind == "checkbox":
            values[f.name] = "1" if value in ("on", "1", True, "true") else ""
        elif f.secret and not value:
            values[f.name] = current.get(f.name, "")
        else:
            values[f.name] = (value or "").strip()
    return values


@app.post("/settings/storage")
async def save_storage_form(request: Request, session: SessionDep):
    form = await request.form()
    cards = {c.key: c for c in _storage_cards()}
    card = cards.get(form.get("preset", ""))
    if card is None or card.backend not in storage.backends():
        raise HTTPException(422, "Unknown storage")
    current = storage.all_options(session)[1].get(card.backend, {})
    storage.save_options(session, card.backend, _storage_values(card.backend, dict(form), current))
    row = session.get(Setting, "storage_preset") or Setting(key="storage_preset")
    row.value = card.key
    session.add(row)
    session.commit()
    return RedirectResponse("/settings/storage?toast=Storage+saved", status_code=303)


# --------------------------------------------------------------------------- JSON API


class SiteIn(BaseModel):
    url: HttpUrl
    name: str | None = None
    schedule: Schedule = Schedule.WEEKLY
    active: bool = True
    max_depth: int = Field(3, ge=0, le=20)
    max_pages: int = Field(300, ge=1, le=100_000)
    file_types: str | None = None
    storage_folder: str | None = None
    download_existing: bool = True
    ai_filter: str | None = None


def _site_from(body: SiteIn, site: Site | None = None) -> Site:
    data = body.model_dump()
    data["url"] = str(body.url)
    data["name"] = body.name or _domain(data["url"])
    site = site or Site(name="", url="")
    for key, value in data.items():
        setattr(site, key, value)
    return site


@app.get("/api/sites", tags=["sites"])
def api_list_sites(session: SessionDep) -> list[Site]:
    return list(session.exec(select(Site).order_by(Site.id)))


@app.post("/api/sites", tags=["sites"], status_code=201)
async def api_create_site(body: SiteIn, session: SessionDep) -> Site:
    site = _site_from(body)
    session.add(site)
    session.commit()
    session.refresh(site)
    if scheduler.scheduler.running:
        scheduler.start_check(site.id)
    return site


@app.get("/api/sites/{site_id}", tags=["sites"])
def api_get_site(site_id: int, session: SessionDep) -> Site:
    return _get_site(session, site_id)


@app.put("/api/sites/{site_id}", tags=["sites"])
def api_update_site(site_id: int, body: SiteIn, session: SessionDep) -> Site:
    site = _site_from(body, _get_site(session, site_id))
    session.add(site)
    session.commit()
    session.refresh(site)
    return site


@app.delete("/api/sites/{site_id}", tags=["sites"], status_code=204)
async def api_delete_site(site_id: int, session: SessionDep) -> None:
    await _delete_site(session, _get_site(session, site_id))


@app.post("/api/sites/{site_id}/check", tags=["sites"])
async def api_check(site_id: int, session: SessionDep) -> CheckRun:
    """Crawl the site now and wait for the result."""
    _get_site(session, site_id)
    run = await run_check(site_id)
    if run is None:
        raise HTTPException(409, "This website is already being checked")
    return run


def _run_summary(run: CheckRun | None) -> dict | None:
    return run.model_dump(exclude={"log"}) if run else None


@app.get("/api/sites/{site_id}/status", tags=["sites"])
def api_site_status(site_id: int, session: SessionDep):
    site = _get_site(session, site_id)
    last = session.exec(
        select(CheckRun).where(CheckRun.site_id == site_id).order_by(CheckRun.id.desc())
    ).first()
    return {"running": is_running(site_id), "last_run": _run_summary(last), "last_error": site.last_error}


@app.get("/api/sites/{site_id}/runs", tags=["sites"])
def api_runs(site_id: int, session: SessionDep, limit: int = 50) -> list[dict]:
    runs = session.exec(select(CheckRun).where(CheckRun.site_id == site_id)
                        .order_by(CheckRun.id.desc()).limit(min(limit, 500)))
    return [_run_summary(r) for r in runs]


class FlowIn(BaseModel):
    flow: dict


class FlowTestIn(BaseModel):
    file_id: int
    flow: dict | None = None  # test unsaved changes; None = the saved flow


@app.get("/api/sites/{site_id}/flow", tags=["flows"])
def api_get_flow(site_id: int, session: SessionDep):
    site = _get_site(session, site_id)
    flow_def = flow.load(site.flow, site.ai_filter)
    return {"flow": flow_def, "warnings": flow.warnings(flow_def), "custom": site.flow is not None}


@app.put("/api/sites/{site_id}/flow", tags=["flows"])
def api_put_flow(site_id: int, body: FlowIn, session: SessionDep):
    """Save the site's flow. New and changed files use it from the next check on."""
    site = _get_site(session, site_id)
    if errors := flow.validate(body.flow):
        raise HTTPException(422, errors[0])
    site.flow = json.dumps(body.flow, ensure_ascii=False)
    session.add(site)
    session.commit()
    return {"ok": True, "warnings": flow.warnings(body.flow)}


@app.post("/api/sites/{site_id}/flow/test", tags=["flows"])
async def api_test_flow(site_id: int, body: FlowTestIn, session: SessionDep):
    """Send one file through the flow without saving anything. Returns the log and the outputs."""
    site = _get_site(session, site_id)
    flow_def = body.flow or flow.load(site.flow, site.ai_filter)
    if errors := flow.validate(flow_def):
        raise HTTPException(422, errors[0])
    record = session.get(SiteFile, body.file_id)
    if record is None or record.site_id != site_id:
        raise HTTPException(404, "File not found")

    say = runlog.RunLog(0)
    s = get_settings()
    work = flow.workdir(s.data_dir)
    outputs = []
    try:
        item = flow.Item(record.filename, {"url": record.url, "link_text": record.link_text or "",
                                           "page_url": record.page_url or "", "content_type": record.content_type,
                                           "site": site.name})

        async def ai_decide(instructions: str, items: list[flow.Item]) -> set[int] | None:
            if not ai.ai_available():
                say("No model connected – the AI filter lets the file through", "warn")
                return None
            links = [FileLink(i.meta["url"], i.meta.get("link_text") or "", i.meta.get("page_url") or "",
                              i.filename) for i in items]
            keep = await ai.filter_files(instructions, site.name, links)
            return None if keep is None else {n for n, link in enumerate(links) if link.url in keep}

        runner = flow.FlowRunner(flow_def, work, say, ai_decide)
        frontier, stopped_by = await runner.plan(item)
        if not frontier:
            say(f"{record.filename} is filtered out by {stopped_by} – it would not be downloaded", "warn")
            return {"log": say.lines, "outputs": [], "errors": []}

        original = work / "original"
        original.mkdir()
        item.path = original / (record.filename or "file")
        if record.status == "stored" and record.storage_path:
            say(f"Using the stored copy of {record.filename}", "muted")
            try:
                backend = storage.get_storage(record.storage_backend)
                with item.path.open("wb") as f:
                    async for chunk in backend.open(record.storage_path):
                        f.write(chunk)
            except storage.StorageError as e:
                raise HTTPException(502, f"Could not read the stored file: {e}") from e
        else:
            say(f"Downloading {record.url}", "muted")
            async with httpx.AsyncClient(timeout=s.request_timeout, follow_redirects=True,
                                         headers={"User-Agent": s.user_agent}) as client:
                try:
                    dl = await download(client, record.url, referer=record.page_url)
                except Exception as e:
                    raise HTTPException(502, f"Download failed: {e}") from e
            dl.path.replace(item.path)
            item.meta["content_type"] = dl.content_type

        async def store(node_id: str, out: flow.Item) -> None:
            data = out.path.read_bytes()
            try:
                preview = data[:6000].decode("utf-8")
            except UnicodeDecodeError:
                preview = None
            folder = (runner.cfg(node_id).get("folder") or "{site}").strip()
            outputs.append({"node": node_id, "filename": out.filename, "size": len(data), "folder": folder,
                            "preview": preview, "truncated": len(data) > 6000})
            say(f"  ✓ Would save {out.filename} ({runlog.filesize(len(data))}) in “{folder}”", "ok")

        result = await runner.process(item, frontier, store)
        if not outputs and not result.errors:
            say("Nothing would be saved – no Save to storage node was reached", "warn")
        return {"log": say.lines, "outputs": outputs, "errors": result.errors}
    finally:
        flow.cleanup(work)


@app.get("/api/sites/{site_id}/log", tags=["sites"])
def api_log(site_id: int, session: SessionDep, run_id: int | None = None, after: int = 0):
    """Activity log of a check (the latest one by default). Poll with ``after=next`` while it runs."""
    _get_site(session, site_id)
    live = runlog.live(site_id)
    if live is not None and run_id in (None, live.run_id):
        lines, next_pos = live.since(after)
        return {"run_id": live.run_id, "running": True, "lines": lines, "next": next_pos}
    q = select(CheckRun).where(CheckRun.site_id == site_id)
    run = session.exec(q.where(CheckRun.id == run_id) if run_id else q.order_by(CheckRun.id.desc())).first()
    if run is None:
        return {"run_id": None, "running": False, "lines": [], "next": 0}
    lines = runlog.load(run.log)
    return {"run_id": run.id, "running": False, "lines": lines[after:], "next": len(lines)}


@app.get("/api/files", tags=["sites"])
def api_files(session: SessionDep, site_id: int | None = None, status: str | None = None,
              limit: int = 100, offset: int = 0) -> list[SiteFile]:
    q = select(SiteFile)
    if site_id:
        q = q.where(SiteFile.site_id == site_id)
    if status:
        q = q.where(SiteFile.status == status)
    return list(session.exec(q.order_by(SiteFile.id.desc()).offset(offset).limit(min(limit, 1000))))


class AISettingsIn(BaseModel):
    preset: str | None = None
    provider: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    model: str | None = None
    effort: str | None = None


@app.get("/api/settings/ai", tags=["settings"])
def api_get_ai_settings(session: SessionDep):
    cfg = llm.load_config(session)
    return {**cfg.masked(), "configured": llm.get_provider(cfg) is not None,
            "providers": {k: v.label for k, v in llm.providers().items()}}


@app.put("/api/settings/ai", tags=["settings"])
def api_put_ai_settings(body: AISettingsIn, session: SessionDep):
    if body.provider and body.provider not in llm.providers():
        raise HTTPException(422, f"Unknown provider {body.provider!r}")
    llm.save_config(session, body.model_dump(exclude_none=True))
    return api_get_ai_settings(session)


@app.post("/api/ai/test", tags=["settings"])
async def api_test_ai(body: AISettingsIn, session: SessionDep):
    """Send a tiny request with the given (or saved) settings to check the connection."""
    cfg = llm.load_config(session)
    if body.preset:
        card = next((c for c in _model_cards() if c.key == body.preset), None)
        if card is not None:
            if card.provider != cfg.provider or (body.base_url or "") != (cfg.base_url or ""):
                cfg.api_key = None  # don't send the saved key to a different service
            cfg.provider = card.provider
    for key, value in body.model_dump(exclude_none=True).items():
        if key == "api_key" and not value:
            continue  # keep the stored key
        setattr(cfg, key, value)
    provider = llm.get_provider(cfg)
    if provider is None:
        raise HTTPException(422, "Incomplete settings – fill in the model name (and base URL)")
    try:
        data = await provider.generate_json(
            system="You are a connection test.",
            text='Reply with {"ok": true, "model": "<your model name>"}.',
            schema={"type": "object", "properties": {"ok": {"type": "boolean"},
                    "model": {"type": "string"}}, "required": ["ok", "model"],
                    "additionalProperties": False},
        )
    except llm.LLMError as e:
        raise HTTPException(502, str(e)) from e
    return {"ok": True, "provider": provider.name, "model": provider.model, "reply": data}


class StorageIn(BaseModel):
    backend: str
    options: dict[str, str] = {}


@app.get("/api/settings/storage", tags=["settings"])
def api_get_storage(session: SessionDep):
    active, per_backend = storage.all_options(session)
    cls = storage.backends().get(active)
    options = per_backend.get(active, {})
    secrets_ = {f.name for f in cls.fields if f.secret} if cls else set()
    return {
        "backend": active,
        "options": {k: ("••••" if k in secrets_ and v else v) for k, v in options.items()},
        "backends": {k: v.label for k, v in storage.backends().items()},
    }


@app.put("/api/settings/storage", tags=["settings"])
def api_put_storage(body: StorageIn, session: SessionDep):
    if body.backend not in storage.backends():
        raise HTTPException(422, f"Unknown storage backend {body.backend!r}")
    current = storage.all_options(session)[1].get(body.backend, {})
    storage.save_options(session, body.backend, _storage_values(body.backend, body.options, current))
    return api_get_storage(session)


@app.post("/api/storage/test", tags=["settings"])
async def api_test_storage(body: StorageIn, session: SessionDep):
    """Write, read back and delete a small file with the given (or saved) settings."""
    if body.backend not in storage.backends():
        raise HTTPException(422, f"Unknown storage backend {body.backend!r}")
    current = storage.all_options(session)[1].get(body.backend, {})
    backend = storage.get_storage(body.backend, _storage_values(body.backend, body.options, current))
    try:
        backend.validate()
        where = await backend.test()
    except storage.StorageError as e:
        raise HTTPException(502, str(e)) from e
    except Exception as e:  # unexpected library errors should still show up in the UI
        raise HTTPException(502, f"{type(e).__name__}: {e}") from e
    return {"ok": True, "target": where}


@app.get("/healthz", include_in_schema=False)
def healthz():
    return {"ok": True, "version": __version__, "scheduler": scheduler.scheduler.running}
