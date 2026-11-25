"""Open tests and their matching to incoming calls.

A probe announces each test (line, expected calling number) before it dials.
An incoming call is matched to the oldest open test whose expected number was
delivered and whose time window covers the call. A test is evaluated once both
sides have reported, or when its deadline has passed.

Kept in memory here; persistence goes through the `on_result` / `on_call`
callbacks (database layer).
"""
from __future__ import annotations

import time
import uuid

from .results import classify, normalize_number

WINDOW_S = 90.0          # an announced test accepts a call this long after announcement
GRACE_S = 120.0          # after the window: time for the probe's report / a long call to end


class Registry:
    def __init__(self, country_code: str = "352", window_s: float = WINDOW_S, grace_s: float = GRACE_S,
                 on_result=None, on_call=None, clock=time.time):
        self.cc, self.window_s, self.grace_s = country_code, window_s, grace_s
        self.on_result, self.on_call, self.clock = on_result, on_call, clock
        self.open: dict[str, dict] = {}
        self.unmatched: list[dict] = []

    # probe side ------------------------------------------------------------
    def announce(self, line: dict, test_id: str | None = None) -> dict:
        now = self.clock()
        t = {"id": test_id or str(uuid.uuid4()), "line_id": line["id"], "expected_cli": line["cli"],
             "site_id": line.get("site_id"), "line": line,
             "want": normalize_number(line["cli"], self.cc), "announced_at": now,
             "window_end": now + self.window_s, "deadline": now + self.window_s + self.grace_s,
             "probe": None, "call_in": None, "call_running": False, "result": None}
        self.open[t["id"]] = t
        return t

    def probe_report(self, test_id: str, report: dict) -> dict | None:
        t = self.open.get(test_id)
        if t is None:
            return None
        t["probe"] = report
        return self._try_finish(t)

    # termination side ------------------------------------------------------
    def call_started(self, rec: dict) -> dict | None:
        nums = {normalize_number(c, self.cc) for c in rec.get("cli") or []} - {None}
        at = rec.get("started_at", self.clock())
        cands = [t for t in self.open.values()
                 if t["call_in"] is None and not t["call_running"] and t["want"] in nums
                 and t["announced_at"] - 5 <= at <= t["window_end"]]
        if not cands:
            # a call with the wrong number still belongs to a test if exactly one test of that
            # time window is waiting - otherwise "wrong number" could never be detected
            cands = [t for t in self.open.values()
                     if t["call_in"] is None and not t["call_running"] and t["announced_at"] - 5 <= at <= t["window_end"]]
            if len(cands) != 1:
                rec["test_id"] = None
                self.unmatched.append(rec)
                return None
        t = min(cands, key=lambda x: x["announced_at"])
        t["call_running"] = True
        rec["test_id"] = t["id"]
        return t

    def call_ended(self, rec: dict) -> dict | None:
        if self.on_call:
            self.on_call(rec)
        t = self.open.get(rec.get("test_id") or "")
        if t is None:
            return None
        t["call_in"], t["call_running"] = rec, False
        return self._try_finish(t)

    # evaluation ------------------------------------------------------------
    def _try_finish(self, t: dict, now: float | None = None) -> dict | None:
        now = self.clock() if now is None else now
        if t["call_running"] and now < t["deadline"]:
            return None
        r = classify(t["expected_cli"], t["probe"], t["call_in"], now >= t["deadline"], self.cc)
        if r is None:
            return None
        t["result"] = r
        t["finished_at"] = now
        self.open.pop(t["id"], None)
        if self.on_result:
            self.on_result(t)
        return t

    def sweep(self) -> list[dict]:
        """Finish tests whose deadline passed (called periodically)."""
        now = self.clock()
        done = []
        for t in list(self.open.values()):
            if now >= t["deadline"]:
                f = self._try_finish(t, now)
                if f:
                    done.append(f)
        return done
