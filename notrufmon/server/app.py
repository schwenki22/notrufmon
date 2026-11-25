"""Central server: probe API, termination of the redirected calls, web UI - one process.

The termination (SIP trunk) and the registry of open tests live in the same asyncio
process as the HTTP API, so announcing a test and matching its call need no shared
state outside memory. Results are written to PostgreSQL. Run with ONE worker:

  uvicorn notrufmon.server.app:app --host 127.0.0.1 --port 8080 --proxy-headers

Probe API (Authorization: Bearer <token>):
  GET  /api/v1/probe/config    lines, accounts, schedule
  GET  /api/v1/probe/poll      {config_version, testcall}
  POST /api/v1/probe/announce  {line_id}            -> {test_id, window_s}
  POST /api/v1/probe/report    {test_id, result}    -> {result}
  POST /api/v1/probe/health    {version, uptime_s, lines: [...]}
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.concurrency import run_in_threadpool

from .. import __version__
from .config import Config
from .db import Database
from .registry import Registry
from .store import Store
from .terminator import Terminator

log = logging.getLogger("notrufmon.app")
SWEEP_S = 5.0
_state: dict = {}


def cfg() -> Config:
    if "cfg" not in _state:
        _state["cfg"] = Config.load()
    return _state["cfg"]


def db() -> Database:
    if "db" not in _state:
        _state["db"] = Database(cfg().dsn)
        _state["db"].ensure_schema()
    return _state["db"]


def store() -> Store:
    if "store" not in _state:
        _state["store"] = Store(db(), cfg().country_code)
    return _state["store"]


def registry() -> Registry:
    return _state["registry"]


def _save_result(t: dict) -> None:
    try:
        store().save_test(t, t.get("site_id") or "", t.get("line"))
        r = t["result"]
        lvl = logging.INFO if r["result"] == "ok" else logging.WARNING
        log.log(lvl, "test %s line %s: %s %s", t["id"], t["line_id"], r["label"], r["reason"])
    except Exception:
        log.exception("could not store test %s", t.get("id"))


def _save_call(rec: dict) -> None:
    try:
        store().save_call_in(rec)
    except Exception:
        log.exception("could not store incoming call")


def _in_thread(fn):
    """Registry callbacks run inside the event loop: hand the DB write to a thread."""
    def run(arg):
        asyncio.get_running_loop().run_in_executor(None, fn, arg)
    return run


async def _sweeper() -> None:
    while True:
        await asyncio.sleep(SWEEP_S)
        try:
            registry().sweep()
        except Exception:
            log.exception("sweep failed")


@contextlib.asynccontextmanager
async def lifespan(_app: FastAPI):
    logging.basicConfig(level=os.environ.get("NOTRUFMON_LOGLEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    c = cfg()
    await run_in_threadpool(store)
    sch, term = c.schedule, c.termination
    _state["registry"] = Registry(c.country_code, float(sch["window_s"]), float(sch["grace_s"]),
                                  on_result=_in_thread(_save_result), on_call=_in_thread(_save_call))
    t = Terminator(registry(), int(term["listen_port"]), term["allowed_sources"], term["peer"],
                   term["local_ip"], term["public_ip"], int(term["rtp_port_min"]), int(term["rtp_port_max"]),
                   float(term["answer_delay_s"]), float(term["max_call_s"]))
    await t.start()
    _state["terminator"] = t
    sweeper = asyncio.ensure_future(_sweeper())
    log.info("notrufmon %s ready: termination on UDP %s", __version__, t.port)
    try:
        yield
    finally:
        sweeper.cancel()
        await t.close()


app = FastAPI(title="notrufmon", version=__version__, docs_url=None, redoc_url=None, openapi_url=None,
              lifespan=lifespan)


def _site(authorization: str | None) -> dict:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "missing bearer token")
    s = store().by_token(authorization[7:].strip())
    if s is None:
        raise HTTPException(403, "unknown token")
    return s


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    return fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "")


async def _json(request: Request) -> dict:
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(400, "invalid JSON") from None
    if not isinstance(body, dict):
        raise HTTPException(400, "expected an object")
    return body


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True, "version": __version__}


@app.get("/api/v1/probe/config")
def probe_config(authorization: str | None = Header(default=None)) -> dict:
    return store().probe_config(_site(authorization), cfg().schedule)


@app.get("/api/v1/probe/poll")
def probe_poll(authorization: str | None = Header(default=None)) -> dict:
    s = _site(authorization)
    tc = s.get("testcall_at")
    return {"config_version": s["updated_at"].isoformat(), "testcall": tc.isoformat() if tc else None}


@app.post("/api/v1/probe/announce")
async def probe_announce(request: Request, authorization: str | None = Header(default=None)) -> dict:
    site = await run_in_threadpool(_site, authorization)
    body = await _json(request)
    try:
        lid = int(body.get("line_id"))
    except (TypeError, ValueError):
        raise HTTPException(400, "line_id missing") from None
    line = await run_in_threadpool(store().line, lid)
    if line is None or line["site_id"] != site["id"] or not line["enabled"] or not site["enabled"]:
        raise HTTPException(404, "line not found or disabled")
    t = registry().announce({"id": line["id"], "cli": line["cli"], "site_id": site["id"], "label": line["label"],
                             "kind": line["kind"], "network": line["network"]})
    return {"test_id": t["id"], "window_s": registry().window_s}


@app.post("/api/v1/probe/report")
async def probe_report(request: Request, authorization: str | None = Header(default=None)) -> dict:
    site = await run_in_threadpool(_site, authorization)
    body = await _json(request)
    tid, result = body.get("test_id"), body.get("result")
    t = registry().open.get(str(tid))
    if t is None or t.get("site_id") != site["id"] or not isinstance(result, dict):
        raise HTTPException(404, "unknown or finished test")
    done = registry().probe_report(t["id"], result)
    return {"result": done["result"] if done else None}


@app.post("/api/v1/probe/health")
async def probe_health(request: Request, authorization: str | None = Header(default=None)) -> dict:
    site = await run_in_threadpool(_site, authorization)
    body = await _json(request)
    await run_in_threadpool(store().save_health, site["id"], _client_ip(request), body)
    return {"ok": True}


from .admin import router as admin_router  # noqa: E402

app.include_router(admin_router)
