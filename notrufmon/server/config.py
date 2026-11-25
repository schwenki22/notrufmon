"""Server configuration (config.yaml): database, termination, test schedule, web.

Sites and lines are managed in the web UI (database)."""
from __future__ import annotations

import copy
import os

import yaml


def _merge(base: dict, over: dict | None) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


DEFAULTS = {
    "database": {"dsn": None},
    "termination": {
        "listen_port": 5060,          # SIP trunk port the redirected calls arrive on
        "local_ip": None,             # None = from the routing table
        "public_ip": None,            # set if the server sits behind 1:1 NAT
        "peer": "127.0.0.1",          # carrier / centre SBC (also the default allowed source)
        "allowed_sources": None,      # list of IPs / CIDR the calls may come from
        "rtp_port_min": 21000,
        "rtp_port_max": 21999,
        "answer_delay_s": 0.3,
        "max_call_s": 90,
    },
    "schedule": {
        "interval_s": 900,            # one test per line every 15 min
        "call_s": 15,                 # length of a test call
        "gap_s": 30,                  # pause between two lines of the same site
        "window_s": 90,               # a call must arrive within this time after the announcement
        "grace_s": 120,
    },
    "country_code": "352",
    "web": {"public_url": None, "timezone": "Europe/Luxembourg"},
}


class Config:
    def __init__(self, data: dict):
        self.raw = data
        c = _merge(DEFAULTS, data)
        self.dsn = os.environ.get("NOTRUFMON_DSN") or c["database"]["dsn"]
        self.termination = c["termination"]
        self.schedule = c["schedule"]
        self.country_code = str(c["country_code"])
        self.web = c["web"]

    @classmethod
    def load(cls, path: str | None = None) -> "Config":
        path = path or os.environ.get("NOTRUFMON_CONFIG", "/etc/notrufmon/config.yaml")
        with open(path, encoding="utf-8") as f:
            return cls(yaml.safe_load(f) or {})
