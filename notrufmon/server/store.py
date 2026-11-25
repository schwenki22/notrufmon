"""Sites (probes), lines and results in the database."""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import ipaddress
import json
import re
import secrets

from .db import Database
from .results import normalize_number

ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
SITE_FIELDS = ("name", "region", "enabled")
LINE_FIELDS = ("label", "kind", "network", "cli", "target", "enabled", "sip_domain", "sip_port", "sip_proxy",
               "username", "password", "auth_username", "register", "position")
KEEP_IF_EMPTY = ("password",)
KINDS = ("voip", "mobile")


class ValidationError(ValueError):
    pass


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _bool(v) -> bool:
    return v in (True, 1, "1", "true", "on", "ja", "yes")


def clean_site(data: dict, partial: bool = False) -> dict:
    out = {}
    if "name" in data or not partial:
        name = str(data.get("name") or "").strip()
        if not name:
            raise ValidationError("Name fehlt")
        out["name"] = name[:120]
    if "region" in data:
        out["region"] = str(data.get("region") or "").strip()[:80]
    if "enabled" in data:
        out["enabled"] = _bool(data["enabled"])
    return out


def clean_line(data: dict, country_code: str = "352", partial: bool = False) -> dict:
    out: dict = {}
    for k in LINE_FIELDS:
        if k not in data:
            continue
        v = data[k]
        v = v.strip() if isinstance(v, str) else v
        if k in ("enabled", "register"):
            out[k] = _bool(v)
        elif k in ("sip_port", "position"):
            if v in (None, ""):
                continue
            try:
                out[k] = int(v)
            except ValueError:
                raise ValidationError(f"{k}: keine Zahl") from None
        elif k in KEEP_IF_EMPTY and v in (None, ""):
            continue
        else:
            out[k] = None if v == "" and k in ("sip_proxy", "auth_username") else v
    if not partial:
        for k, label in (("label", "Bezeichnung"), ("cli", "Rufnummer"), ("sip_domain", "SIP-Server"),
                         ("username", "SIP-Benutzer")):
            if not out.get(k):
                raise ValidationError(f"{label} fehlt")
    if "kind" in out and out["kind"] not in KINDS:
        raise ValidationError("Art: voip oder mobile")
    if out.get("cli"):
        n = normalize_number(out["cli"], country_code)
        if not n or len(n) < 6:
            raise ValidationError("Rufnummer ungültig – z. B. +352 20 62 63 75")
        out["cli"] = "+" + n
    if out.get("target") is not None and not re.fullmatch(r"[0-9*#+]{2,20}", out["target"] or ""):
        raise ValidationError("Zielnummer ungültig (z. B. 112)")
    if out.get("sip_port") is not None and not 1 <= out["sip_port"] <= 65535:
        raise ValidationError("SIP-Port 1–65535")
    for k in ("sip_domain", "sip_proxy"):
        if out.get(k) and not re.fullmatch(r"[A-Za-z0-9.\-]{1,253}", out[k]):
            raise ValidationError(f"{k}: nur Hostname oder IP-Adresse")
    return out


class Store:
    def __init__(self, db: Database, country_code: str = "352"):
        self.db, self.cc = db, country_code

    # sites --------------------------------------------------------------------
    def sites(self) -> list[dict]:
        return self.db.query("SELECT * FROM sites ORDER BY region, name, id")

    def site(self, sid: str) -> dict | None:
        r = self.db.query("SELECT * FROM sites WHERE id = %s", (sid,))
        return r[0] if r else None

    def create_site(self, sid: str, data: dict) -> str:
        sid = (sid or "").strip().lower()
        if not ID_RE.match(sid):
            raise ValidationError("Standort-ID: nur Kleinbuchstaben, Ziffern, - und _")
        if self.site(sid):
            raise ValidationError(f"Standort '{sid}' existiert bereits")
        d = clean_site(data)
        token = secrets.token_hex(24)
        self.db.execute("INSERT INTO sites (id, name, region, enabled, token_hash) VALUES (%s,%s,%s,%s,%s)",
                        (sid, d["name"], d.get("region", ""), d.get("enabled", True), hash_token(token)))
        return token

    def update_site(self, sid: str, data: dict) -> None:
        d = clean_site(data, partial=True)
        if d:
            sets = ", ".join(f"{k} = %s" for k in d)
            self.db.execute(f"UPDATE sites SET {sets}, updated_at = now() WHERE id = %s", (*d.values(), sid))

    def new_token(self, sid: str) -> str:
        token = secrets.token_hex(24)
        self.db.execute("UPDATE sites SET token_hash = %s, updated_at = now() WHERE id = %s", (hash_token(token), sid))
        return token

    def delete_site(self, sid: str) -> None:
        self.db.execute("DELETE FROM sites WHERE id = %s", (sid,))

    def request_testcall(self, sid: str) -> None:
        self.db.execute("UPDATE sites SET testcall_at = now() WHERE id = %s", (sid,))

    def by_token(self, token: str) -> dict | None:
        h = hash_token(token)
        rows = self.db.query("SELECT * FROM sites WHERE token_hash = %s", (h,))
        if rows and hmac.compare_digest(rows[0]["token_hash"], h):
            return rows[0]
        return None

    def _touch(self, sid: str) -> None:
        self.db.execute("UPDATE sites SET updated_at = now() WHERE id = %s", (sid,))

    # lines --------------------------------------------------------------------
    def lines(self, sid: str, enabled_only: bool = False) -> list[dict]:
        return self.db.query("SELECT * FROM lines WHERE site_id = %s" + (" AND enabled" if enabled_only else "")
                             + " ORDER BY position, id", (sid,))

    def line(self, lid: int) -> dict | None:
        r = self.db.query("SELECT * FROM lines WHERE id = %s", (lid,))
        return r[0] if r else None

    def add_line(self, sid: str, data: dict) -> int:
        d = clean_line(data, self.cc)
        d.setdefault("position", len(self.lines(sid)))
        cols = ", ".join(["site_id", *d])
        ph = ", ".join(["%s"] * (len(d) + 1))
        lid = self.db.query(f"INSERT INTO lines ({cols}) VALUES ({ph}) RETURNING id", (sid, *d.values()))[0]["id"]
        self._touch(sid)
        return lid

    def update_line(self, lid: int, data: dict) -> None:
        row = self.line(lid)
        if row is None:
            raise ValidationError("Leitung nicht gefunden")
        d = clean_line(data, self.cc, partial=True)
        if d:
            sets = ", ".join(f"{k} = %s" for k in d)
            self.db.execute(f"UPDATE lines SET {sets} WHERE id = %s", (*d.values(), lid))
        self._touch(row["site_id"])

    def delete_line(self, lid: int) -> None:
        row = self.line(lid)
        if row:
            self.db.execute("DELETE FROM lines WHERE id = %s", (lid,))
            self._touch(row["site_id"])

    # probe configuration ----------------------------------------------------
    def probe_config(self, site: dict, schedule: dict) -> dict:
        lines = self.lines(site["id"], enabled_only=True) if site["enabled"] else []
        # spread the sites over the interval so that tests of different sites rarely overlap
        offset = int(hashlib.sha256(site["id"].encode()).hexdigest(), 16) % int(schedule["interval_s"])
        return {
            "site_id": site["id"], "name": site["name"], "enabled": site["enabled"],
            "config_version": site["updated_at"].isoformat(),
            "schedule": {"interval_s": int(schedule["interval_s"]), "call_s": float(schedule["call_s"]),
                         "gap_s": float(schedule["gap_s"]), "offset_s": offset},
            "lines": [{"id": ln["id"], "label": ln["label"], "kind": ln["kind"], "network": ln["network"],
                       "cli": ln["cli"], "target": ln["target"],
                       "account": {"username": ln["username"], "password": ln["password"],
                                   "auth_username": ln["auth_username"], "domain": ln["sip_domain"],
                                   "port": ln["sip_port"], "proxy": ln["sip_proxy"]},
                       "register": ln["register"]} for ln in lines],
        }

    # results ------------------------------------------------------------------
    def save_test(self, t: dict, site_id: str, line: dict | None) -> None:
        r, probe, call = t["result"], t.get("probe") or {}, t.get("call_in") or {}
        ts = dt.datetime.fromtimestamp(t["announced_at"], dt.timezone.utc)
        fin = dt.datetime.fromtimestamp(t.get("finished_at") or t["announced_at"], dt.timezone.utc)
        self.db.execute(
            """INSERT INTO tests (id, time, site_id, line_id, line_label, kind, network, expected_cli, result,
                 reason, available, counted, sip_code, setup_ms, delivered_cli, audio_up, audio_down,
                 finished_at, probe, call_in)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (id) DO NOTHING""",
            (t["id"], ts, site_id, t["line_id"], (line or {}).get("label"), (line or {}).get("kind"),
             (line or {}).get("network"), t["expected_cli"], r["result"], r["reason"], r["available"],
             r["counted"], probe.get("sip_code") or r.get("sip_code"), probe.get("setup_ms"),
             ", ".join(call.get("cli") or []) or None,
             (call.get("rx") or {}).get("audio_ok") if call else None,
             (probe.get("rx") or {}).get("audio_ok") if probe.get("rx") else None,
             fin, json.dumps(probe, default=str) if probe else None,
             json.dumps(call, default=str) if call else None))

    def save_call_in(self, rec: dict) -> None:
        self.db.execute(
            "INSERT INTO calls_in (time, call_id, src, cli, dialed, test_id, answered, duration_s, audio_ok, raw) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (dt.datetime.fromtimestamp(rec["started_at"], dt.timezone.utc), rec.get("call_id"), rec.get("src"),
             rec.get("cli"), rec.get("dialed"), rec.get("test_id"), rec.get("answered"), rec.get("duration_s"),
             (rec.get("rx") or {}).get("audio_ok"), json.dumps(rec, default=str)))

    def save_health(self, sid: str, ip: str, h: dict) -> None:
        self.db.execute("INSERT INTO probe_health (time, site_id, version, remote_ip, local_ip, uptime_s, outbox, lines) "
                        "VALUES (now(),%s,%s,%s,%s,%s,%s,%s)",
                        (sid, str(h.get("version") or "")[:20], ip, str(h.get("local_ip") or "")[:45],
                         h.get("uptime_s"), h.get("outbox"), json.dumps(h.get("lines") or [])))

    # views for the web UI ------------------------------------------------------
    def last_health(self, sid: str) -> dict | None:
        r = self.db.query("SELECT * FROM probe_health WHERE site_id = %s ORDER BY time DESC LIMIT 1", (sid,))
        return r[0] if r else None

    def last_test(self, line_id: int) -> dict | None:
        r = self.db.query("SELECT * FROM tests WHERE line_id = %s ORDER BY time DESC LIMIT 1", (line_id,))
        return r[0] if r else None

    def availability(self, line_id: int, hours: int = 24) -> dict:
        r = self.db.query("SELECT count(*) FILTER (WHERE counted) AS n, count(*) FILTER (WHERE available) AS ok "
                          "FROM tests WHERE line_id = %s AND time > now() - make_interval(hours => %s)",
                          (line_id, hours))[0]
        return {"n": r["n"], "ok": r["ok"], "pct": round(100.0 * r["ok"] / r["n"], 2) if r["n"] else None}

    def recent_tests(self, sid: str | None = None, limit: int = 50) -> list[dict]:
        if sid:
            return self.db.query("SELECT * FROM tests WHERE site_id = %s ORDER BY time DESC LIMIT %s", (sid, limit))
        return self.db.query("SELECT * FROM tests ORDER BY time DESC LIMIT %s", (limit,))

    def unmatched_calls(self, limit: int = 20) -> list[dict]:
        return self.db.query("SELECT * FROM calls_in WHERE test_id IS NULL ORDER BY time DESC LIMIT %s", (limit,))


def valid_ip_list(v: list | None) -> list:
    out = []
    for x in v or []:
        ipaddress.ip_network(str(x).strip(), strict=False)
        out.append(str(x).strip())
    return out
