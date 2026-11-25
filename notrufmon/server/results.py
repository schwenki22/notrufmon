"""Evaluation of one 112 test: what the probe saw + what arrived at the termination.

Pure functions, no I/O - the rules the tender is judged on live here and are tested
in isolation (tests/test_results.py).
"""
from __future__ import annotations

import re

# result classes (stored as these keys; labels for UI / reports)
CLASSES = {
    "ok": "OK",
    "no_audio": "Ohne Ton",
    "wrong_cli": "Rufnummer falsch",
    "not_delivered": "Nicht zugestellt",
    "misrouted": "Fehlgeleitet",          # answered, but not by the test system (e.g. a dispatcher)
    "probe_error": "Probe gestört",       # measurement failed - not counted as 112 outage
}
AVAILABLE = {"ok"}
NOT_COUNTED = {"probe_error"}            # excluded from 112 availability, alarmed separately


def normalize_number(raw: str | None, country_code: str = "352") -> str | None:
    """'+352 20 62 63 75', '0035220626375', '20626375', 'sip:+35220626375@x' -> '35220626375'.
    Luxembourg has no trunk prefix, so a number without country code is national."""
    if not raw:
        return None
    m = re.match(r"^(?:sips?:|tel:)?([^@;]+)", raw.strip())
    user = m.group(1) if m else raw
    if user.lower() in ("anonymous", "unknown", "restricted", "unavailable"):
        return None
    digits = re.sub(r"\D", "", user)
    if not digits:
        return None
    if user.lstrip().startswith("+"):
        return digits
    if digits.startswith("00"):
        return digits[2:]
    if not digits.startswith(country_code) or len(digits) <= 9:
        return country_code + digits
    return digits


def classify(expected_cli: str, probe: dict | None, call_in: dict | None, deadline_passed: bool,
             country_code: str = "352") -> dict | None:
    """Result of one test, or None while still waiting for one side.

    probe   = the probe's report: {"success", "sip_code", "reason", "error", "rx": {"audio_ok", ...}, ...}
    call_in = the call that arrived at the termination and was matched to this test:
              {"cli": [numbers seen in From / P-Asserted-Identity], "rx": {"audio_ok", ...}, ...}
    """
    if probe is None and call_in is None:
        if not deadline_passed:
            return None
        return _r("probe_error", "keine Rückmeldung der Probe und kein Anruf angekommen")
    if probe is None and not deadline_passed:
        return None                                   # call arrived, wait for the probe's view
    if call_in is None and not deadline_passed and probe is not None and probe.get("success"):
        return None                                   # answered somewhere; the termination may still report
    if probe is not None and probe.get("error"):
        return _r("probe_error", probe["error"])
    if call_in is None:
        if probe and probe.get("success"):
            return _r("misrouted", "Anruf wurde angenommen, kam aber nicht beim Prüfsystem an")
        code = probe.get("sip_code") if probe else None
        why = (probe or {}).get("reason") or "keine Antwort"
        return _r("not_delivered", f"{code} {why}".strip() if code else why, sip_code=code)

    want = normalize_number(expected_cli, country_code)
    seen = [n for n in (normalize_number(c, country_code) for c in call_in.get("cli") or []) if n]
    if want not in seen:
        shown = ", ".join(seen) or "keine"
        return _r("wrong_cli", f"erwartet {want}, übermittelt: {shown}")

    up_ok = bool((call_in.get("rx") or {}).get("audio_ok"))
    down_ok = None if probe is None else bool((probe.get("rx") or {}).get("audio_ok"))
    if not up_ok or down_ok is False:
        missing = [d for d, ok in (("zur Leitstelle", up_ok), ("zurück zum Anrufer", down_ok)) if ok is False]
        return _r("no_audio", "kein Ton " + " und ".join(missing))
    note = "Rückmeldung der Probe fehlt" if probe is None else ""
    return _r("ok", note)


def _r(cls: str, reason: str = "", **extra) -> dict:
    return {"result": cls, "label": CLASSES[cls], "reason": reason, "available": cls in AVAILABLE,
            "counted": cls not in NOT_COUNTED, **extra}
