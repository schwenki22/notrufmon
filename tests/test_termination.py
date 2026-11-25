"""End to end on localhost: probe UA 'dials 112', the call lands at the termination
(as if redirected by the emergency call centre), the registry matches and evaluates."""
import asyncio

from notrufmon.server.registry import Registry
from notrufmon.server.terminator import Terminator, delivered_numbers
from notrufmon.sipmsg import SipMessage
from notrufmon.ua import Account, UserAgent

LINE = {"id": "lux-nord-post", "cli": "+35220626375"}


def _probe(number, port, rtp):
    return UserAgent(Account(username=number, password="", domain="127.0.0.1", port=port,
                             rtp_port_min=rtp, rtp_port_max=rtp + 40, keepalive_s=0), name="probe")


async def _run(number, allowed=None, announce=True, duration=4.0, rtp=33000):
    results = []
    reg = Registry(on_result=results.append)
    term = Terminator(reg, listen_port=0, allowed_sources=allowed or ["127.0.0.1"], answer_delay=0.05,
                      rtp_port_min=rtp + 100, rtp_port_max=rtp + 140)
    await term.start()
    probe = _probe(number, term.port, rtp)
    await probe.start()
    t = reg.announce(LINE) if announce else None
    res = await probe.call("112", duration)
    for _ in range(50):                                  # termination ends right after the BYE
        if not any(x["call_running"] for x in reg.open.values()):
            break
        await asyncio.sleep(0.1)
    if t:
        reg.probe_report(t["id"], res)
    await probe.close(unregister=False)
    await term.close()
    return res, reg, results


def test_redirected_call_is_ok():
    res, reg, results = asyncio.run(_run("+35220626375"))
    assert res["success"] and len(results) == 1, (res, reg.open)
    r = results[0]
    assert r["result"]["result"] == "ok", r["result"]
    assert r["call_in"]["rx"]["audio_ok"] and r["probe"]["rx"]["audio_ok"]
    assert r["call_in"]["dialed"].startswith("sip:112@")


def test_wrong_calling_number_detected():
    res, reg, results = asyncio.run(_run("+35299999999", rtp=33200))
    assert results and results[0]["result"]["result"] == "wrong_cli"
    assert "35299999999" in results[0]["result"]["reason"]


def test_unannounced_call_is_recorded_as_unmatched():
    res, reg, results = asyncio.run(_run("+35220626375", announce=False, duration=2.0, rtp=33400))
    assert res["success"] and results == [] and len(reg.unmatched) == 1
    assert reg.unmatched[0]["cli"] == ["sip:+35220626375@127.0.0.1"]


def test_call_from_unknown_source_is_refused_and_not_delivered():
    res, reg, results = asyncio.run(_run("+35220626375", allowed=["10.9.9.9"], duration=2.0, rtp=33600))
    assert res["sip_code"] == 403 and results[0]["result"]["result"] == "not_delivered"


def test_delivered_numbers_prefers_pai():
    raw = (b"INVITE sip:112@x SIP/2.0\r\nVia: SIP/2.0/UDP 1.2.3.4;branch=z9hG4bKa\r\n"
           b"From: <sip:anonymous@anonymous.invalid>;tag=1\r\nTo: <sip:112@x>\r\nCall-ID: c\r\nCSeq: 1 INVITE\r\n"
           b"P-Asserted-Identity: <sip:+35220626375@carrier.lu;user=phone>, <tel:+35220626375>\r\n"
           b"Content-Length: 0\r\n\r\n")
    nums = delivered_numbers(SipMessage.parse(raw))
    assert nums == ["sip:+35220626375@carrier.lu;user=phone", "tel:+35220626375", "sip:anonymous@anonymous.invalid"]
