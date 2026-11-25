from notrufmon.server.results import classify, normalize_number

CLI = "+35220626375"
GOOD_RX = {"audio_ok": True}


def test_normalize_number():
    for raw in ("+352 20 62 63 75", "0035220626375", "20626375", "sip:+35220626375@1.2.3.4;user=phone",
                "tel:+352-20626375", "35220626375"):
        assert normalize_number(raw) == "35220626375", raw
    assert normalize_number("+49 30 1234567") == "49301234567"
    assert normalize_number("anonymous") is None and normalize_number("") is None
    assert normalize_number("sip:anonymous@anonymous.invalid") is None


def test_ok():
    r = classify(CLI, {"success": True, "rx": GOOD_RX}, {"cli": ["sip:+35220626375@x"], "rx": GOOD_RX}, False)
    assert r["result"] == "ok" and r["available"] and r["counted"]


def test_cli_from_pai_is_enough():
    r = classify(CLI, {"success": True, "rx": GOOD_RX},
                 {"cli": ["sip:anonymous@anonymous.invalid", "tel:20626375"], "rx": GOOD_RX}, False)
    assert r["result"] == "ok"


def test_wrong_or_missing_cli():
    r = classify(CLI, {"success": True, "rx": GOOD_RX}, {"cli": ["+35220626399"], "rx": GOOD_RX}, False)
    assert r["result"] == "wrong_cli" and "35220626399" in r["reason"] and not r["available"]
    r = classify(CLI, {"success": True, "rx": GOOD_RX}, {"cli": ["anonymous"], "rx": GOOD_RX}, False)
    assert r["result"] == "wrong_cli" and "keine" in r["reason"]


def test_no_audio_names_direction():
    r = classify(CLI, {"success": True, "rx": {"audio_ok": False}}, {"cli": [CLI], "rx": GOOD_RX}, False)
    assert r["result"] == "no_audio" and "zurück zum Anrufer" in r["reason"]
    r = classify(CLI, {"success": True, "rx": GOOD_RX}, {"cli": [CLI], "rx": {"audio_ok": False}}, False)
    assert r["result"] == "no_audio" and "zur Leitstelle" in r["reason"]


def test_not_delivered_and_waiting():
    probe = {"success": False, "sip_code": 503, "reason": "Service Unavailable"}
    r = classify(CLI, probe, None, False)
    assert r["result"] == "not_delivered" and r["sip_code"] == 503 and "503" in r["reason"]


def test_answered_elsewhere_is_misrouted():
    """Probe got 200 OK but no call reached the test system: a real dispatcher may have answered."""
    probe = {"success": True, "rx": GOOD_RX}
    assert classify(CLI, probe, None, False) is None            # termination may still report
    r = classify(CLI, probe, None, True)
    assert r["result"] == "misrouted" and not r["available"]


def test_probe_problems_are_not_counted():
    r = classify(CLI, {"error": "SIM nicht im Netz eingebucht"}, None, True)
    assert r["result"] == "probe_error" and not r["counted"]
    assert classify(CLI, None, None, False) is None
    assert classify(CLI, None, None, True)["result"] == "probe_error"


def test_call_arrived_but_probe_report_missing():
    call = {"cli": [CLI], "rx": GOOD_RX}
    assert classify(CLI, None, call, False) is None
    r = classify(CLI, None, call, True)
    assert r["result"] == "ok" and "fehlt" in r["reason"]
