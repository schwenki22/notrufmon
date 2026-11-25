"""Probe agent: places the 112 test calls of one site.

Per cycle (every schedule.interval_s, start to start) every enabled line is tested
one after another, schedule.gap_s apart:

  1. announce the test at the server (POST /announce) - no announcement, no call:
     a 112 call the test system cannot evaluate would only load the network
  2. dial the target (112) for schedule.call_s seconds, play the upstream tones,
     check the downstream tones
  3. report the result (POST /report); kept in a local outbox while the server
     is unreachable

Plus: registration of the SIP lines that need it, a sign of life every minute,
configuration changes (poll every 20 s, applied between tests, never mid-call)
and the "test now" button of the web UI.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import ssl
import time
import tomllib
import urllib.error
import urllib.request

from .. import __version__
from ..ua import Account, UserAgent
from .outbox import Outbox

log = logging.getLogger("notrufmon.probe")
POLL_S = 20
HEALTH_S = 60
UPLOAD_S = 15


class Probe:
    def __init__(self, local: dict):
        self.server_url = local["server_url"].rstrip("/")
        self.token = local["token"]
        self.state_dir = os.environ.get("STATE_DIRECTORY") or local.get("state_dir", "/var/lib/notrufmon")
        self.ssl_ctx = ssl.create_default_context(cafile=local.get("ca_file") or None)
        self.outbox = Outbox(os.path.join(self.state_dir, "outbox.sqlite"))
        self.cfg: dict | None = None
        self.uas: dict[int, UserAgent] = {}
        self.reg_state: dict[int, dict] = {}
        self.started = time.time()
        self.testcall = asyncio.Event()
        self._last_testcall: str | None = None
        self._poll_seen = False
        self._restart = False
        self.in_test = False

    # HTTP ------------------------------------------------------------------
    def _http_sync(self, method: str, path: str, body: dict | None = None, timeout: float = 15) -> dict:
        data = json.dumps(body, default=str).encode() if body is not None else None
        req = urllib.request.Request(self.server_url + path, data=data, method=method, headers={
            "Authorization": f"Bearer {self.token}", "Content-Type": "application/json",
            "User-Agent": f"notrufmon-probe/{__version__}"})
        with urllib.request.urlopen(req, timeout=timeout, context=self.ssl_ctx) as r:
            return json.loads(r.read() or b"{}")

    async def http(self, method: str, path: str, body: dict | None = None, timeout: float = 15) -> dict:
        return await asyncio.to_thread(self._http_sync, method, path, body, timeout)

    # one test ----------------------------------------------------------------
    async def run_test(self, line: dict, ua: UserAgent, call_s: float) -> dict | None:
        try:
            ann = await self.http("POST", "/api/v1/probe/announce", {"line_id": line["id"]})
        except Exception as e:
            log.warning("[%s] test skipped - server did not accept the announcement: %s", line["label"], e)
            return None
        tid = ann["test_id"]
        log.info("[%s] test %s: calling %s for %.0f s", line["label"], tid, line["target"], call_s)
        self.in_test = True
        try:
            res = await ua.call(line["target"], call_s)
        except Exception as e:
            log.exception("[%s] test call crashed", line["label"])
            res = {"success": False, "reason": f"internal error: {e}", "error": f"Probe-Fehler: {e}"}
        finally:
            self.in_test = False
        report = {k: res.get(k) for k in ("success", "sip_code", "reason", "pdd_ms", "setup_ms", "duration_s",
                                           "bye_by", "dropped", "codec", "started_at", "rx", "error")}
        if line.get("register") and not self.reg_state.get(line["id"], {}).get("ok") and not res.get("success"):
            report["error"] = report.get("error") or "SIP-Leitung nicht registriert"
        rx = res.get("rx") or {}
        log.info("[%s] result: success=%s code=%s audio_down=%s", line["label"], res.get("success"),
                 res.get("sip_code"), rx.get("audio_ok"))
        await self.send_report(tid, report)
        return report

    async def send_report(self, tid: str, report: dict) -> None:
        try:
            await self.http("POST", "/api/v1/probe/report", {"test_id": tid, "result": report})
        except urllib.error.HTTPError as e:
            if e.code != 404:                      # 404: test already evaluated (deadline) - nothing to do
                self.outbox.put({"test_id": tid, "result": report})
        except Exception:
            self.outbox.put({"test_id": tid, "result": report})

    async def uploader(self) -> None:
        while True:
            await asyncio.sleep(UPLOAD_S)
            for rid, rec in await asyncio.to_thread(self.outbox.batch, 50):
                try:
                    await self.http("POST", "/api/v1/probe/report", rec)
                except urllib.error.HTTPError as e:
                    if e.code not in (404, 400):
                        break
                except Exception:
                    break
                await asyncio.to_thread(self.outbox.delete, [rid])

    # schedule ------------------------------------------------------------------
    async def _wait(self, seconds: float) -> bool:
        """Sleep; True if a manual test run was requested meanwhile."""
        try:
            await asyncio.wait_for(self.testcall.wait(), max(0.0, seconds))
            return True
        except asyncio.TimeoutError:
            return False
        finally:
            self.testcall.clear()

    async def cycles(self) -> None:
        s = self.cfg["schedule"]
        interval, gap, call_s = float(s["interval_s"]), float(s["gap_s"]), float(s["call_s"])
        # first cycle at the site's offset within the interval (spreads sites over the country)
        wait = (float(s["offset_s"]) - time.time()) % interval
        while True:
            await self._wait(wait)
            start = time.monotonic()
            for i, line in enumerate(self.cfg["lines"]):
                if self._restart:
                    return
                if i:
                    await asyncio.sleep(gap)
                await self.run_test(line, self.uas[line["id"]], call_s)
            if self._restart:
                return
            wait = interval - (time.monotonic() - start)

    async def registration(self, line: dict, ua: UserAgent) -> None:
        def on_result(res: dict) -> None:
            self.reg_state[line["id"]] = {"ok": res.get("ok"), "code": res.get("code"), "reason": res.get("reason")}
            if not res.get("ok"):
                log.warning("[%s] registration failed: %s %s", line["label"], res.get("code"), res.get("reason"))
        await ua.run_registration(on_result)

    async def health(self) -> None:
        while True:
            lines = [{"id": lid, **self.reg_state.get(lid, {"ok": None})} for lid in self.uas]
            ip = next((ua.local_ip for ua in self.uas.values()), None)
            try:
                await self.http("POST", "/api/v1/probe/health", {
                    "version": __version__, "uptime_s": round(time.time() - self.started, 1),
                    "local_ip": ip, "outbox": self.outbox.count(), "lines": lines})
            except Exception as e:
                log.debug("health not sent: %s", e)
            await asyncio.sleep(HEALTH_S)

    async def poll(self) -> bool:
        """True if the configuration changed on the server."""
        try:
            p = await self.http("GET", "/api/v1/probe/poll")
        except Exception as e:
            log.debug("poll failed: %s", e)
            return False
        tc = p.get("testcall")
        if tc and tc != self._last_testcall and self._poll_seen:
            log.info("test run requested from the web UI")
            self.testcall.set()
        self._last_testcall, self._poll_seen = tc, True
        return p.get("config_version") != (self.cfg or {}).get("config_version")

    async def run_with_config(self) -> None:
        self._restart = False
        tasks = []
        try:
            for line in self.cfg["lines"]:
                a = dict(line["account"])
                acc = Account.from_dict({**a, "local_port": 0, "rtp_port_min": 30000, "rtp_port_max": 30999})
                ua = UserAgent(acc, name=f"{self.cfg['site_id']}/{line['label']}")
                await ua.start()
                self.uas[line["id"]] = ua
                if line.get("register"):
                    tasks.append(asyncio.ensure_future(self.registration(line, ua)))
            tasks.append(asyncio.ensure_future(self.cycles()))
            await asyncio.gather(*tasks)
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            for ua in self.uas.values():
                await ua.close()
            self.uas, self.reg_state = {}, {}

    async def run(self, stop: asyncio.Event) -> None:
        bg = [asyncio.ensure_future(self.uploader()), asyncio.ensure_future(self.health())]
        try:
            while not stop.is_set():
                try:
                    self.cfg = await self.http("GET", "/api/v1/probe/config")
                except Exception as e:
                    log.warning("config fetch failed: %s", e)
                    try:
                        await asyncio.wait_for(stop.wait(), 30)
                    except asyncio.TimeoutError:
                        pass
                    continue
                labels = ", ".join(f"{ln['label']}->{ln['target']}" for ln in self.cfg["lines"]) or "-"
                log.info("config for %s: lines %s, every %ss", self.cfg["site_id"], labels,
                         self.cfg["schedule"]["interval_s"])
                worker = asyncio.ensure_future(self.run_with_config())
                while not stop.is_set() and not worker.done():
                    waiter = asyncio.ensure_future(stop.wait())
                    await asyncio.wait({worker, waiter}, timeout=POLL_S, return_when=asyncio.FIRST_COMPLETED)
                    waiter.cancel()
                    if stop.is_set() or worker.done():
                        break
                    if await self.poll():
                        log.info("configuration changed - applied after the running test")
                        self._restart = True
                        while self.in_test and not stop.is_set():
                            await asyncio.sleep(1)
                        break
                if self.in_test:                   # shutdown: let the running test call end with a BYE
                    self._restart = True
                    for _ in range(int(self.cfg["schedule"]["call_s"]) + 15):
                        if not self.in_test:
                            break
                        await asyncio.sleep(1)
                if worker.done() and not worker.cancelled() and worker.exception():
                    log.error("probe worker failed: %r", worker.exception())
                    await asyncio.sleep(10)
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)
        finally:
            for b in bg:
                b.cancel()


def load_local_config(path: str) -> dict:
    with open(path, "rb") as f:
        return tomllib.load(f)

