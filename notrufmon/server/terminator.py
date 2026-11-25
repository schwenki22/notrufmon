"""Termination of the redirected test calls (the "PBX" side of the test system).

The emergency call centre forwards calls from the known test numbers to this SIP
trunk endpoint. It accepts calls only from the configured carrier / centre
addresses, records the calling number as delivered (From and P-Asserted-Identity),
answers, plays the downstream tone signal, checks the upstream tones, and hands
the record to the registry, which matches it to the probe's announced test.
"""
from __future__ import annotations

import logging
import time
import uuid

from ..sipmsg import parse_nameaddr
from ..ua import Account, UserAgent
from .registry import Registry

log = logging.getLogger("notrufmon.terminator")


def delivered_numbers(req) -> list[str]:
    """Calling-number candidates as delivered: P-Asserted-Identity first, then From."""
    out = []
    for h in req.get_all("p-asserted-identity") + [req.get("from", "")]:
        for part in h.split(","):          # PAI may carry sip: and tel: identities
            uri = parse_nameaddr(part.strip())[1] if part.strip() else ""
            if uri and uri not in out:
                out.append(uri)
    return out


class Terminator:
    def __init__(self, registry: Registry, listen_port: int = 5060, allowed_sources: list | None = None,
                 peer: str = "127.0.0.1", local_ip: str | None = None, public_ip: str | None = None,
                 rtp_port_min: int = 21000, rtp_port_max: int = 21999, answer_delay: float = 0.3,
                 max_call_s: float = 90.0):
        self.registry = registry
        self.answer_delay, self.max_call_s = answer_delay, max_call_s
        self.ua = UserAgent(Account(username="notruf", password="", domain=peer, local_port=listen_port,
                                    local_ip=local_ip, public_ip=public_ip, rtp_port_min=rtp_port_min,
                                    rtp_port_max=rtp_port_max, keepalive_s=0,
                                    allowed_sources=allowed_sources or [peer]),
                            on_invite=self._on_invite, name="termination")

    async def start(self) -> None:
        await self.ua.start()

    async def close(self) -> None:
        await self.ua.close(unregister=False)

    @property
    def port(self) -> int:
        return self.ua.local_port

    async def _on_invite(self, ua: UserAgent, req, addr) -> None:
        rec = {"id": str(uuid.uuid4()), "started_at": time.time(), "call_id": req.call_id,
               "src": f"{addr[0]}:{addr[1]}", "cli": delivered_numbers(req),
               "dialed": parse_nameaddr(req.get("to", ""))[1], "ruri": req.uri}
        test = self.registry.call_started(rec)
        log.info("incoming call %s from %s (%s) -> test %s", req.call_id, ", ".join(rec["cli"]) or "-",
                 rec["src"], test["id"] if test else "none")
        try:
            res = await ua.answer(req, addr, answer_delay=self.answer_delay, max_duration=self.max_call_s)
        except Exception as e:                     # never leave a matched test hanging
            log.exception("answering failed")
            res = {"answered": False, "reason": f"internal error: {e}"}
        rec.update(answered=res.get("answered"), duration_s=res.get("duration_s"), bye_by=res.get("bye_by"),
                   codec=res.get("codec"), rx=res.get("rx"), reason=res.get("reason"), ended_at=time.time())
        self.registry.call_ended(rec)
