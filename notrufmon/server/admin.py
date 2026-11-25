"""Web UI: sites (probes), their lines, test results. Behind Caddy Basic-Auth.

All state-changing requests are POSTs whose Origin/Referer must match the host."""
from __future__ import annotations

import datetime as dt
import logging
import pathlib
from urllib.parse import quote, urlparse

from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, RedirectResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape

from .. import __version__
from .results import CLASSES
from .store import LINE_FIELDS, SITE_FIELDS, ValidationError

log = logging.getLogger("notrufmon.admin")
router = APIRouter(prefix="/admin")
_env = Environment(loader=FileSystemLoader(pathlib.Path(__file__).with_name("templates")),
                   autoescape=select_autoescape(["html"]))
RES_CSS = {"ok": "res-ok", "probe_error": "res-warn"}


def _app():
    from . import app  # late import (app imports this module)
    return app


def local_tz():
    from zoneinfo import ZoneInfo
    try:
        return ZoneInfo(_app().cfg().web.get("timezone") or "Europe/Luxembourg")
    except Exception:
        return dt.timezone.utc


def _lt(ts, fmt: str = "%d.%m. %H:%M") -> str:
    return ts.astimezone(local_tz()).strftime(fmt) if ts else ""


_env.filters["lt"] = _lt
_env.globals["CLASSES"] = CLASSES
_env.globals["res_css"] = lambda r: RES_CSS.get(r, "res-bad")


def _render(name: str, **ctx) -> HTMLResponse:
    ctx.setdefault("version", __version__)
    ctx.setdefault("tzname", _app().cfg().web.get("timezone") or "Europe/Luxembourg")
    return HTMLResponse(_env.get_template(name).render(**ctx))


def _check_origin(request: Request) -> None:
    host = request.headers.get("host", "")
    src = request.headers.get("origin") or request.headers.get("referer")
    if not src or urlparse(src).netloc != host:
        raise HTTPException(403, "Ungültige Herkunft der Anfrage (Origin/Referer)")


def _redirect(path: str, msg: str = "", err: str = "") -> RedirectResponse:
    q = f"?msg={quote(msg)}" if msg else (f"?err={quote(err)}" if err else "")
    return RedirectResponse(f"/admin{path}{q}", status_code=303)


def _server_url(request: Request) -> str:
    url = _app().cfg().web.get("public_url")
    return url.rstrip("/") if url else f"https://{request.headers.get('host', 'notruf.example.net')}"


def _age(ts) -> tuple[str, bool]:
    if not ts:
        return "nie", False
    s = int((dt.datetime.now(dt.timezone.utc) - ts).total_seconds())
    txt = f"vor {s} s" if s < 120 else f"vor {s // 60} min" if s < 7200 else f"vor {s // 3600} h"
    return txt, s < 180


def _overview(sid: str) -> dict:
    s = _app().store()
    h = s.last_health(sid)
    seen, online = _age(h["time"] if h else None)
    lines = [{"l": ln, "last": s.last_test(ln["id"]), "av": s.availability(ln["id"])} for ln in s.lines(sid)]
    return {"health": h, "seen": seen, "online": online, "lines": lines}


# ----------------------------------------------------------------------------- pages
@router.get("", include_in_schema=False)
def admin_root() -> RedirectResponse:
    return RedirectResponse("/admin/", status_code=307)


@router.get("/", response_class=HTMLResponse)
async def index(msg: str = "", err: str = "") -> HTMLResponse:
    def load():
        return [{"site": x, **_overview(x["id"])} for x in _app().store().sites()]
    return _render("index.html", items=await run_in_threadpool(load), msg=msg, err=err)


@router.get("/sites/new", response_class=HTMLResponse)
def site_new(err: str = "") -> HTMLResponse:
    return _render("site_new.html", row={}, err=err)


@router.post("/sites/new", response_class=HTMLResponse)
async def site_create(request: Request) -> HTMLResponse:
    _check_origin(request)
    form = dict(await request.form())
    sid = form.pop("id", "").strip().lower()
    try:
        token = await run_in_threadpool(_app().store().create_site, sid, {k: form.get(k) for k in SITE_FIELDS
                                                                         if k in form} | {"enabled": "1"})
    except ValidationError as e:
        return _render("site_new.html", row={**form, "id": sid}, err=str(e))
    log.info("admin: site %s created", sid)
    return _render("token.html", sid=sid, token=token, server=_server_url(request), created=True)


@router.get("/sites/{sid}", response_class=HTMLResponse)
async def site_detail(sid: str, msg: str = "", err: str = "") -> HTMLResponse:
    s = _app().store()
    site = await run_in_threadpool(s.site, sid)
    if site is None:
        raise HTTPException(404, "Standort nicht gefunden")
    ov = await run_in_threadpool(_overview, sid)
    tests = await run_in_threadpool(s.recent_tests, sid, 30)
    return _render("site.html", site=site, ov=ov, tests=tests, line={"target": "112", "sip_port": 5060,
                   "kind": "mobile", "register": True, "enabled": True}, msg=msg, err=err)


@router.post("/sites/{sid}")
async def site_update(request: Request, sid: str):
    _check_origin(request)
    form = dict(await request.form())
    try:
        await run_in_threadpool(_app().store().update_site, sid, {k: form[k] for k in SITE_FIELDS if k in form})
    except ValidationError as e:
        return _redirect(f"/sites/{sid}", err=str(e))
    return _redirect(f"/sites/{sid}", msg="Gespeichert.")


@router.post("/sites/{sid}/token", response_class=HTMLResponse)
async def site_token(request: Request, sid: str) -> HTMLResponse:
    _check_origin(request)
    token = await run_in_threadpool(_app().store().new_token, sid)
    log.info("admin: new token for %s", sid)
    return _render("token.html", sid=sid, token=token, server=_server_url(request), created=False)


@router.post("/sites/{sid}/testcall")
async def site_testcall(request: Request, sid: str):
    _check_origin(request)
    await run_in_threadpool(_app().store().request_testcall, sid)
    return _redirect(f"/sites/{sid}", msg="Testdurchlauf angefordert – die Probe beginnt innerhalb von etwa 20 s.")


@router.post("/sites/{sid}/delete")
async def site_delete(request: Request, sid: str):
    _check_origin(request)
    if str((await request.form()).get("confirm", "")).strip() != sid:
        return _redirect(f"/sites/{sid}", err="Zum Löschen die Standort-ID exakt eintippen.")
    await run_in_threadpool(_app().store().delete_site, sid)
    log.info("admin: site %s deleted", sid)
    return _redirect("/", msg=f"Standort {sid} gelöscht. Testergebnisse bleiben erhalten.")


@router.post("/sites/{sid}/lines/new")
async def line_create(request: Request, sid: str):
    _check_origin(request)
    form = dict(await request.form())
    data = {k: form.get(k, "") for k in LINE_FIELDS if k != "position"}
    data["enabled"] = form.get("enabled", "0")
    data["register"] = form.get("register", "0")
    try:
        await run_in_threadpool(_app().store().add_line, sid, data)
    except ValidationError as e:
        return _redirect(f"/sites/{sid}", err=str(e))
    return _redirect(f"/sites/{sid}", msg="Leitung angelegt.")


@router.get("/lines/{lid}", response_class=HTMLResponse)
async def line_edit(lid: int, err: str = "") -> HTMLResponse:
    line = await run_in_threadpool(_app().store().line, lid)
    if line is None:
        raise HTTPException(404, "Leitung nicht gefunden")
    return _render("line.html", line=line, err=err)


@router.post("/lines/{lid}")
async def line_update(request: Request, lid: int):
    _check_origin(request)
    form = dict(await request.form())
    s = _app().store()
    line = await run_in_threadpool(s.line, lid)
    if line is None:
        raise HTTPException(404, "Leitung nicht gefunden")
    data = {k: form.get(k, "") for k in LINE_FIELDS if k != "position"}
    data["enabled"] = form.get("enabled", "0")
    data["register"] = form.get("register", "0")
    try:
        await run_in_threadpool(s.update_line, lid, data)
    except ValidationError as e:
        return _redirect(f"/lines/{lid}", err=str(e))
    return _redirect(f"/sites/{line['site_id']}", msg="Leitung gespeichert.")


@router.post("/lines/{lid}/delete")
async def line_delete(request: Request, lid: int):
    _check_origin(request)
    s = _app().store()
    line = await run_in_threadpool(s.line, lid)
    if line is None:
        raise HTTPException(404, "Leitung nicht gefunden")
    await run_in_threadpool(s.delete_line, lid)
    return _redirect(f"/sites/{line['site_id']}", msg="Leitung gelöscht.")


@router.get("/tests", response_class=HTMLResponse)
async def tests_page() -> HTMLResponse:
    s = _app().store()
    tests = await run_in_threadpool(s.recent_tests, None, 200)
    unmatched = await run_in_threadpool(s.unmatched_calls, 30)
    return _render("tests.html", tests=tests, unmatched=unmatched)
