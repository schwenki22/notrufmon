"""Whole system on localhost against a real PostgreSQL: site + line created in the web UI,
the probe announces, calls, reports; the termination answers; the result lands in the DB
and in the web UI. Skipped without a test database (NOTRUFMON_TEST_DSN)."""
import asyncio
import json
import os
import re
import socket
import threading
import time
import urllib.parse
import urllib.request

import pytest
import yaml

psycopg = pytest.importorskip("psycopg")
uvicorn = pytest.importorskip("uvicorn")

DSN = os.environ.get("NOTRUFMON_TEST_DSN", "postgresql://voipmon:voipmon@127.0.0.1:5432/notrufmon_test")
TABLES = ["lines", "sites", "tests", "calls_in", "probe_health", "settings"]


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    try:
        conn = psycopg.connect(DSN, autocommit=True, connect_timeout=3)
    except Exception as e:
        pytest.skip(f"no test database: {e}")
    for t in TABLES:
        conn.execute(f"DROP TABLE IF EXISTS {t} CASCADE")
    conn.close()
    cfgp = tmp_path_factory.mktemp("srv") / "config.yaml"
    cfgp.write_text(yaml.safe_dump({
        "database": {"dsn": DSN},
        "termination": {"listen_port": 0, "peer": "127.0.0.1", "rtp_port_min": 34000, "rtp_port_max": 34099,
                        "answer_delay_s": 0.05},
        "schedule": {"interval_s": 600, "call_s": 3, "gap_s": 0, "window_s": 15, "grace_s": 5},
    }))
    os.environ["NOTRUFMON_CONFIG"] = str(cfgp)
    os.environ.pop("NOTRUFMON_DSN", None)
    from notrufmon.server import app as appmod
    appmod._state.clear()
    port = _free_port()
    srv = uvicorn.Server(uvicorn.Config(appmod.app, host="127.0.0.1", port=port, log_level="warning"))
    th = threading.Thread(target=srv.run, daemon=True)
    th.start()
    base = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            urllib.request.urlopen(base + "/healthz", timeout=1)
            break
        except Exception:
            time.sleep(0.1)
    while "terminator" not in appmod._state:
        time.sleep(0.05)
    yield base, appmod
    srv.should_exit = True
    th.join(5)


def _post(base, path, data):
    req = urllib.request.Request(base + path, data=urllib.parse.urlencode(data).encode(), method="POST",
                                 headers={"Origin": base, "Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.read().decode()


def _get(base, path):
    with urllib.request.urlopen(base + path, timeout=10) as r:
        return r.read().decode()


def _result(appmod, n, timeout=15):
    for _ in range(int(timeout * 10)):
        rows = appmod.store().recent_tests(None, 50)
        if len(rows) >= n:
            return rows
        time.sleep(0.1)
    raise AssertionError(f"only {len(rows)} results")


def test_full_cycle(server, tmp_path):
    base, appmod = server
    term_port = appmod._state["terminator"].port
    page = _post(base, "/admin/sites/new", {"id": "ettelbruck", "name": "Ettelbruck", "region": "Nord"})
    token = re.search(r"<pre>([0-9a-f]{48})</pre>", page).group(1)
    line = {"label": "VoIP Test", "kind": "voip", "network": "VoIP A", "cli": "+352 20 62 63 75",
            "target": "112", "sip_domain": "127.0.0.1", "sip_port": term_port, "username": "+35220626375",
            "password": "", "enabled": "1"}
    _post(base, "/admin/sites/ettelbruck/lines/new", line)
    _post(base, "/admin/sites/ettelbruck/lines/new", {**line, "label": "Falsche Nummer", "cli": "+35299999999"})

    from notrufmon.probe.agent import Probe
    from notrufmon.ua import Account, UserAgent

    async def probe_run():
        p = Probe({"server_url": base, "token": token, "state_dir": str(tmp_path)})
        p.cfg = await p.http("GET", "/api/v1/probe/config")
        out = []
        for ln in p.cfg["lines"]:
            ua = UserAgent(Account.from_dict({**ln["account"], "rtp_port_min": 34200, "rtp_port_max": 34260,
                                              "keepalive_s": 0}), name="probe")
            await ua.start()
            out.append(await p.run_test(ln, ua, 3))
            await ua.close(unregister=False)
            await asyncio.sleep(0.5)
        await p.http("POST", "/api/v1/probe/health", {"version": "test", "uptime_s": 1, "lines": []})
        return p.cfg, out

    cfg, reports = asyncio.run(probe_run())
    assert cfg["site_id"] == "ettelbruck" and len(cfg["lines"]) == 2 and cfg["lines"][0]["cli"] == "+35220626375"
    assert reports[0]["success"] and reports[0]["rx"]["audio_ok"]
    rows = _result(appmod, 2)
    by_label = {r["line_label"]: r for r in rows}
    assert by_label["VoIP Test"]["result"] == "ok", by_label["VoIP Test"]
    assert by_label["VoIP Test"]["audio_up"] and by_label["VoIP Test"]["audio_down"]
    assert by_label["Falsche Nummer"]["result"] == "wrong_cli", by_label["Falsche Nummer"]

    index = _get(base, "/admin/")
    assert "Ettelbruck" in index and "online" in index and "100.0 %" in index
    site = _get(base, "/admin/sites/ettelbruck")
    assert "Rufnummer falsch" in site and "OK" in site
    assert "VoIP Test" in _get(base, "/admin/tests")


def test_probe_auth_and_foreign_line(server):
    base, appmod = server
    req = urllib.request.Request(base + "/api/v1/probe/config", headers={"Authorization": "Bearer nope"})
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req)
    assert e.value.code == 403
    page = _post(base, "/admin/sites/new", {"id": "wiltz", "name": "Wiltz"})
    token = re.search(r"<pre>([0-9a-f]{48})</pre>", page).group(1)
    other_line = appmod.store().lines("ettelbruck")[0]["id"]
    req = urllib.request.Request(base + "/api/v1/probe/announce", method="POST",
                                 data=json.dumps({"line_id": other_line}).encode(),
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req)
    assert e.value.code == 404                     # a probe cannot announce another site's line


def test_admin_requires_same_origin(server):
    base, _ = server
    req = urllib.request.Request(base + "/admin/sites/new", data=b"id=x&name=x", method="POST")
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req)
    assert e.value.code == 403
