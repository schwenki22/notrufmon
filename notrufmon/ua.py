"""Minimal SIP user agent over UDP: REGISTER, OPTIONS, outgoing test call, auto-answer.

Deliberately small and self-contained (standard library only) so it runs on a
Raspberry Pi without compiling anything and gives full control over RTP.
Everything is sent to the configured proxy (outbound-proxy style), responses
to requests go back to the source address (rport behaviour).
"""
from __future__ import annotations

import asyncio
import dataclasses
import ipaddress
import logging
import random
import socket
import time

from . import __version__
from . import sdp as sdpmod
from .rtcpstats import summarize_rtcp
from .rtp import RtpSession, make_udp_socket
from .sipmsg import (SipMessage, digest_authorization, digest_supported, gen_branch, gen_callid,
                     gen_tag, get_tag, parse_challenge, parse_nameaddr, parse_uri)
from . import tones

log = logging.getLogger("notrufmon.ua")
T1, T2, TX_TIMEOUT = 0.5, 4.0, 32.0
ALLOW = "INVITE, ACK, CANCEL, BYE, OPTIONS, NOTIFY, UPDATE, INFO"
USER_AGENT = f"notrufmon/{__version__}"


@dataclasses.dataclass
class Account:
    username: str
    password: str
    domain: str
    proxy: str | None = None          # host to send to (default: domain)
    port: int = 5060
    auth_username: str | None = None
    display: str | None = None
    local_ip: str | None = None       # None/"auto": derived from route to proxy
    public_ip: str | None = None      # None: use local ip; "auto": learn from rport; or fixed IP
    local_port: int = 0
    expires: int = 300
    codec: str = "PCMA"
    rtp_port_min: int = 30000
    rtp_port_max: int = 30999
    dscp_sip: int = 24
    dscp_rtp: int = 46
    keepalive_s: float = 20.0
    # incoming calls are only accepted from these IPs/networks (CIDR allowed).
    # None: only from the proxy/PBX address this account sends to.
    allowed_sources: list | None = None

    @classmethod
    def from_dict(cls, d: dict) -> "Account":
        known = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known and v is not None})


def route_ip(dest_ip: str) -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((dest_ip, 9))
        return s.getsockname()[0]
    finally:
        s.close()


class _Proto(asyncio.DatagramProtocol):
    def __init__(self, ua: "UserAgent"):
        self.ua = ua

    def datagram_received(self, data, addr):
        self.ua._datagram(data, addr)

    def error_received(self, exc):
        log.debug("sip socket error: %s", exc)


class _ClientTx:
    def __init__(self, msg: SipMessage, on_prov, loop):
        self.msg = msg
        self.branch = msg.top_via()["params"].get("branch", "")
        self.future = loop.create_future()
        self.on_prov = on_prov
        self.provisional = False
        self.expires = None


class Dialog:
    def __init__(self, ua: "UserAgent", call_id, local_tag, remote_tag, local_uri, remote_uri,
                 remote_target, route_set, peer_addr, local_cseq=1):
        self.ua = ua
        self.call_id = call_id
        self.local_tag = local_tag
        self.remote_tag = remote_tag
        self.local_uri = local_uri
        self.remote_uri = remote_uri
        self.remote_target = remote_target
        self.route_set = route_set
        self.peer_addr = peer_addr
        self.local_cseq = local_cseq
        self.ended = asyncio.Event()
        self.acked = asyncio.Event()
        self.bye_by: str | None = None
        self.ack: SipMessage | None = None
        self.local_sdp = b""
        self.on_sdp = None

    def new_request(self, method: str, cseq: int | None = None, body: bytes = b"",
                    ctype: str | None = None) -> SipMessage:
        if cseq is None:
            self.local_cseq += 1
            cseq = self.local_cseq
        m = SipMessage(method=method, uri=self.remote_target)
        m.add("Via", self.ua.via())
        for r in self.route_set:
            m.add("Route", r)
        m.add("Max-Forwards", "70")
        m.add("From", f"<{self.local_uri}>;tag={self.local_tag}")
        m.add("To", f"<{self.remote_uri}>" + (f";tag={self.remote_tag}" if self.remote_tag else ""))
        m.add("Call-ID", self.call_id)
        m.add("CSeq", f"{cseq} {method}")
        if method != "ACK":
            m.add("Contact", self.ua.contact())
            m.add("User-Agent", USER_AGENT)
        if body:
            m.add("Content-Type", ctype or "application/sdp")
            m.body = body
        return m

    def handle_request(self, msg: SipMessage, addr) -> None:
        m = msg.method
        if m == "BYE":
            self.ua.respond(msg, addr, 200, "OK")
            if not self.ended.is_set():
                self.bye_by = "remote"
                self.ended.set()
        elif m in ("INVITE", "UPDATE"):
            offer = sdpmod.parse_sdp(msg.body) if msg.body else None
            if offer and self.on_sdp:
                self.on_sdp(offer)
            send_sdp = m == "INVITE" or offer is not None
            self.ua.respond(msg, addr, 200, "OK", contact=True,
                            body=self.local_sdp if send_sdp else b"",
                            ctype="application/sdp" if send_sdp else None)
        elif m == "ACK":
            self.acked.set()
        else:
            self.ua.respond(msg, addr, 200, "OK")


class UserAgent:
    def __init__(self, account: Account, on_invite=None, name: str | None = None):
        self.acc = account
        self.on_invite = on_invite
        self.name = name or account.username
        self.transport = None
        self.proxy_addr: tuple[str, int] | None = None
        self.local_ip = "0.0.0.0"
        self.local_port = 0
        self.learned_addr: tuple[str, int] | None = None
        self.registered = False
        self.last_register: dict | None = None
        self.dialogs: dict[str, Dialog] = {}
        self._ctx: dict[tuple, _ClientTx] = {}
        self._acks: dict[str, tuple] = {}
        self._stx: dict[tuple, dict] = {}
        self._tasks: set[asyncio.Task] = set()
        self._reg_call_id = gen_callid()
        self._reg_from_tag = gen_tag()
        self._reg_cseq = 0
        self._closed = False
        self.allowed_nets: list = []

    # lifecycle ---------------------------------------------------------
    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        host = self.acc.proxy or self.acc.domain
        infos = await loop.getaddrinfo(host, self.acc.port, family=socket.AF_INET, type=socket.SOCK_DGRAM)
        self.proxy_addr = infos[0][4][:2]
        if self.acc.local_ip in (None, "", "auto"):
            self.local_ip = route_ip(self.proxy_addr[0])
        else:
            self.local_ip = self.acc.local_ip
        sock = make_udp_socket(self.acc.local_port, self.acc.dscp_sip)
        self.transport, _ = await loop.create_datagram_endpoint(lambda: _Proto(self), sock=sock)
        self.local_port = sock.getsockname()[1]
        self._spawn(self._housekeeping())
        srcs = self.acc.allowed_sources if self.acc.allowed_sources else [self.proxy_addr[0]]
        self.allowed_nets = [ipaddress.ip_network(str(x).strip(), strict=False) for x in srcs]
        log.info("[%s] SIP UA on %s:%s -> %s:%s", self.name, self.local_ip, self.local_port, *self.proxy_addr)
        if self.on_invite is not None:
            log.info("[%s] accepting calls from %s", self.name, ", ".join(str(n) for n in self.allowed_nets))

    def source_allowed(self, ip: str) -> bool:
        try:
            a = ipaddress.ip_address(ip)
        except ValueError:
            return False
        return any(a in n for n in self.allowed_nets)

    async def close(self, unregister: bool = True) -> None:
        if self._closed:
            return
        if unregister and self.registered:
            try:
                await asyncio.wait_for(self.register(expires=0), 3)
            except Exception:
                pass
        self._closed = True
        for t in list(self._tasks):
            t.cancel()
        if self.transport:
            self.transport.close()

    def _spawn(self, coro) -> asyncio.Task:
        t = asyncio.ensure_future(coro)
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)
        return t

    async def _housekeeping(self) -> None:
        last_ka = time.monotonic()
        while True:
            await asyncio.sleep(1.0)
            now = time.monotonic()
            for k in [k for k, v in self._stx.items() if v["expires"] < now]:
                del self._stx[k]
            for k in [k for k, v in self._acks.items() if v[1] < now]:
                del self._acks[k]
            for k in [k for k, v in self._ctx.items() if v.expires and v.expires < now]:
                del self._ctx[k]
            if self.acc.keepalive_s and now - last_ka >= self.acc.keepalive_s:
                last_ka = now
                if self.transport:
                    self.transport.sendto(b"\r\n\r\n", self.proxy_addr)

    # addressing --------------------------------------------------------
    @property
    def aor(self) -> str:
        return f"sip:{self.acc.username}@{self.acc.domain}"

    def _contact_hostport(self) -> tuple[str, int]:
        pub = self.acc.public_ip
        if pub == "auto" and self.learned_addr:
            return self.learned_addr
        if pub and pub != "auto":
            return pub, self.local_port
        return self.local_ip, self.local_port

    def sdp_ip(self) -> str:
        return self._contact_hostport()[0]

    def contact(self) -> str:
        h, p = self._contact_hostport()
        return f"<sip:{self.acc.username}@{h}:{p};transport=udp>"

    def via(self) -> str:
        return f"SIP/2.0/UDP {self.local_ip}:{self.local_port};rport;branch={gen_branch()}"

    def _from(self, tag: str) -> str:
        disp = f'"{self.acc.display}" ' if self.acc.display else ""
        return f"{disp}<{self.aor}>;tag={tag}"

    def send(self, msg: SipMessage, addr=None) -> None:
        if self.transport is not None:
            self.transport.sendto(msg.to_bytes(), addr or self.proxy_addr)

    # client transactions -----------------------------------------------
    async def request(self, msg: SipMessage, on_provisional=None, timeout: float = TX_TIMEOUT,
                      addr=None) -> SipMessage | None:
        loop = asyncio.get_running_loop()
        tx = _ClientTx(msg, on_provisional, loop)
        key = (tx.branch, msg.method)
        self._ctx[key] = tx
        is_invite = msg.method == "INVITE"
        start = loop.time()
        interval = T1
        self.send(msg, addr)
        try:
            while True:
                remaining = start + timeout - loop.time()
                if remaining <= 0:
                    return None
                wait = remaining if (is_invite and tx.provisional) else min(interval, remaining)
                try:
                    return await asyncio.wait_for(asyncio.shield(tx.future), wait)
                except asyncio.TimeoutError:
                    if tx.future.done():
                        return tx.future.result()
                    if is_invite:
                        if not tx.provisional:
                            self.send(msg, addr)
                            interval *= 2
                    else:
                        self.send(msg, addr)
                        interval = min(interval * 2, T2)
        finally:
            if is_invite and not tx.future.done():
                tx.expires = time.monotonic() + 64   # keep to ACK a late final response
            else:
                self._ctx.pop(key, None)

    def _auth_header(self, resp: SipMessage, method: str, uri: str) -> tuple[str, str] | None:
        hname = "www-authenticate" if resp.status == 401 else "proxy-authenticate"
        for raw in resp.get_all(hname):
            chal = parse_challenge(raw)
            if digest_supported(chal):
                value = digest_authorization(chal, method, uri, self.acc.auth_username or self.acc.username,
                                             self.acc.password)
                return ("Authorization" if resp.status == 401 else "Proxy-Authorization"), value
        return None

    # incoming ----------------------------------------------------------
    def _datagram(self, data: bytes, addr) -> None:
        if not data.strip():
            return
        try:
            msg = SipMessage.parse(data)
        except Exception:
            log.warning("[%s] unparseable SIP from %s", self.name, addr)
            return
        try:
            if msg.is_request:
                self._on_request(msg, addr)
            else:
                self._on_response(msg, addr)
        except Exception:
            log.exception("[%s] error handling SIP message", self.name)

    def _on_response(self, msg: SipMessage, addr) -> None:
        via = msg.top_via()
        params = via["params"]
        if params.get("received") or params.get("rport"):
            rport = params.get("rport")
            self.learned_addr = (params.get("received") or via["host"],
                                 int(rport) if rport and rport.isdigit() else via["port"])
        branch = params.get("branch", "")
        method = msg.cseq[1]
        tx = self._ctx.get((branch, method))
        if tx is None:
            if method == "INVITE" and 200 <= msg.status < 300:
                d = self.dialogs.get(msg.call_id)
                if d is not None and d.ack is not None:
                    self.send(d.ack, d.peer_addr)
            elif method == "INVITE" and msg.status >= 300 and branch in self._acks:
                self.send(self._acks[branch][0])
            return
        if msg.status < 200:
            tx.provisional = True
            if tx.on_prov:
                tx.on_prov(msg)
            return
        if method == "INVITE" and msg.status >= 300:
            ack = SipMessage(method="ACK", uri=tx.msg.uri)
            ack.add("Via", tx.msg.get_all("via")[0])
            for r in tx.msg.get_all("route"):
                ack.add("Route", r)
            ack.add("Max-Forwards", "70")
            ack.add("From", tx.msg.get("from"))
            ack.add("To", msg.get("to"))
            ack.add("Call-ID", tx.msg.call_id)
            ack.add("CSeq", f"{tx.msg.cseq[0]} ACK")
            self.send(ack)
            self._acks[branch] = (ack, time.monotonic() + 32)
        if tx.expires:
            self._ctx.pop((branch, method), None)
        if not tx.future.done():
            tx.future.set_result(msg)

    def respond(self, req: SipMessage, addr, code: int, reason: str, to_tag: str | None = None,
                extra: tuple = (), body: bytes = b"", ctype: str | None = None,
                contact: bool = False) -> SipMessage:
        r = SipMessage(status=code, reason=reason)
        for v in req.get_all("via"):
            r.add("Via", v)
        r.add("From", req.get("from", ""))
        to = req.get("to", "")
        if to_tag and code > 100 and not get_tag(to):
            to = f"{to};tag={to_tag}"
        r.add("To", to)
        r.add("Call-ID", req.call_id)
        r.add("CSeq", req.get("cseq", ""))
        if req.method in ("INVITE", "SUBSCRIBE", "UPDATE") and 100 < code < 300:
            # RFC 3261 12.1.1: dialog-creating responses echo the Record-Route set, otherwise
            # the caller sends ACK/BYE directly and record-routing proxies never see the dialog end
            for rr in req.get_all("record-route"):
                r.add("Record-Route", rr)
        if contact:
            r.add("Contact", self.contact())
        r.add("Server", USER_AGENT)
        for k, v in extra:
            r.add(k, v)
        if body:
            r.add("Content-Type", ctype or "application/sdp")
            r.body = body
        self.send(r, addr)
        key = (req.top_via()["params"].get("branch", ""), req.method)
        st = self._stx.setdefault(key, {"req": req, "addr": addr, "final": False, "cancelled": False,
                                        "expires": time.monotonic() + 300})
        st["resp"] = r
        if code >= 200:
            st["final"] = True
            st["expires"] = time.monotonic() + 32
        return r

    def _on_request(self, msg: SipMessage, addr) -> None:
        branch = msg.top_via()["params"].get("branch", "")
        m = msg.method
        st = self._stx.get((branch, m))
        if st is not None and m != "ACK":
            if st.get("resp") is not None:
                self.send(st["resp"], addr)
            return
        if m == "ACK":
            d = self.dialogs.get(msg.call_id)
            if d is not None:
                d.acked.set()
            return
        if m == "CANCEL":
            inv = self._stx.get((branch, "INVITE"))
            if inv is None:
                self.respond(msg, addr, 481, "Call/Transaction Does Not Exist")
                return
            self.respond(msg, addr, 200, "OK")
            if not inv["final"]:
                inv["cancelled"] = True
                self.respond(inv["req"], addr, 487, "Request Terminated", to_tag=inv.get("to_tag") or gen_tag())
            return
        d = self.dialogs.get(msg.call_id)
        if d is not None and get_tag(msg.get("to")) == d.local_tag:
            d.handle_request(msg, addr)
            return
        if m == "INVITE":
            if get_tag(msg.get("to")):
                self.respond(msg, addr, 481, "Call/Transaction Does Not Exist")
                return
            if self.on_invite is not None and not self.source_allowed(addr[0]):
                log.warning("[%s] rejected call from %s:%s (not in allowed_sources): %s",
                            self.name, addr[0], addr[1], msg.get("from"))
                self.respond(msg, addr, 403, "Forbidden", to_tag=gen_tag())
                return
            self.respond(msg, addr, 100, "Trying")
            if self.on_invite is None:
                self.respond(msg, addr, 486, "Busy Here", to_tag=gen_tag())
                return
            self._spawn(self.on_invite(self, msg, addr))
        elif m == "OPTIONS":
            self.respond(msg, addr, 200, "OK", to_tag=gen_tag(),
                         extra=(("Allow", ALLOW), ("Accept", "application/sdp")))
        elif m in ("NOTIFY", "MESSAGE", "INFO", "UPDATE"):
            self.respond(msg, addr, 200, "OK", to_tag=gen_tag())
        elif m == "BYE":
            self.respond(msg, addr, 481, "Call/Transaction Does Not Exist")
        else:
            self.respond(msg, addr, 405, "Method Not Allowed", to_tag=gen_tag(), extra=(("Allow", ALLOW),))

    # REGISTER / OPTIONS ------------------------------------------------
    async def register(self, expires: int | None = None) -> dict:
        exp = self.acc.expires if expires is None else expires
        t0 = time.monotonic()
        auth = None
        resp = None
        ruri = f"sip:{self.acc.domain}"
        for attempt in range(4):
            self._reg_cseq += 1
            m = SipMessage(method="REGISTER", uri=ruri)
            m.add("Via", self.via())
            m.add("Max-Forwards", "70")
            m.add("From", self._from(self._reg_from_tag))
            m.add("To", f"<{self.aor}>")
            m.add("Call-ID", self._reg_call_id)
            m.add("CSeq", f"{self._reg_cseq} REGISTER")
            m.add("Contact", self.contact())
            m.add("Expires", str(exp))
            m.add("Allow", ALLOW)
            m.add("User-Agent", USER_AGENT)
            if auth:
                m.add(*auth)
            resp = await self.request(m)
            if resp is None:
                break
            if resp.status in (401, 407) and attempt < 3:
                stale = "stale=true" in (resp.get("www-authenticate") or resp.get("proxy-authenticate") or "").lower()
                if auth and not stale:
                    break
                auth = self._auth_header(resp, "REGISTER", ruri)
                if auth is None:
                    break
                continue
            if resp.status == 423 and attempt < 3:
                exp = int((resp.get("min-expires") or "60").strip())
                continue
            break
        rtt = round((time.monotonic() - t0) * 1000, 2)
        if resp is None:
            self.registered = False if exp else self.registered
            res = {"ok": False, "code": None, "reason": "timeout", "rtt_ms": None, "expires": None}
        else:
            ok = 200 <= resp.status < 300
            granted = exp
            if ok and exp:
                granted = self._granted_expires(resp, exp)
            self.registered = ok and exp > 0
            res = {"ok": ok, "code": resp.status, "reason": resp.reason, "rtt_ms": rtt, "expires": granted}
            # NAT: re-register once with the learned public contact
            if ok and exp and self.acc.public_ip == "auto" and self.learned_addr and \
                    self.learned_addr[0] not in m.get("contact", ""):
                return await self.register(expires)
        if exp:
            self.last_register = res
        return res

    @staticmethod
    def _granted_expires(resp: SipMessage, requested: int) -> int:
        for c in resp.get_all("contact"):
            v = parse_nameaddr(c)[2].get("expires")
            if v and v.isdigit():
                return int(v)
        e = resp.get("expires")
        return int(e) if e and e.strip().isdigit() else requested

    async def run_registration(self, on_result=None) -> None:
        while True:
            try:
                res = await self.register()
            except Exception as e:  # network errors etc.
                log.exception("[%s] register failed", self.name)
                res = {"ok": False, "code": None, "reason": str(e), "rtt_ms": None, "expires": None}
            if on_result:
                try:
                    on_result(res)
                except Exception:
                    log.exception("on_result")
            delay = max(5.0, (res.get("expires") or 0) * 0.8) if res["ok"] else 30.0
            await asyncio.sleep(delay)

    async def options(self) -> dict:
        m = SipMessage(method="OPTIONS", uri=f"sip:{self.acc.domain}")
        m.add("Via", self.via())
        m.add("Max-Forwards", "70")
        m.add("From", self._from(gen_tag()))
        m.add("To", f"<sip:{self.acc.domain}>")
        m.add("Call-ID", gen_callid())
        m.add("CSeq", "1 OPTIONS")
        m.add("Contact", self.contact())
        m.add("Accept", "application/sdp")
        m.add("User-Agent", USER_AGENT)
        t0 = time.monotonic()
        resp = await self.request(m, timeout=8)
        if resp is None:
            return {"ok": False, "code": None, "rtt_ms": None}
        return {"ok": True, "code": resp.status, "rtt_ms": round((time.monotonic() - t0) * 1000, 2)}

    # outgoing test call --------------------------------------------------
    def _codecs(self) -> list[str]:
        return [self.acc.codec] + [c for c in ("PCMA", "PCMU") if c != self.acc.codec]

    async def call(self, target: str, duration_s: float, ring_timeout: float = 60.0,
                   abort: asyncio.Event | None = None, extra_headers: list | None = None) -> dict:
        """Place a test call. If `abort` is set while the call is up, the call is ended early with
        a regular BYE (bye_by "abort") and the measurement up to then is returned."""
        loop = asyncio.get_running_loop()
        res: dict = {"target": target, "planned_duration_s": duration_s, "success": False,
                     "sip_code": None, "reason": None, "pdd_ms": None, "setup_ms": None,
                     "auth_rounds": 0, "duration_s": 0.0, "bye_by": None, "dropped": False,
                     "codec": None, "started_at": time.time()}
        rtp = await RtpSession.open(self.acc.rtp_port_min, self.acc.rtp_port_max, self.acc.dscp_rtp)
        call_id = gen_callid()
        res["call_id"] = call_id
        try:
            ruri = f"sip:{target}@{self.acc.domain}"
            from_tag = gen_tag()
            cseq = 1
            offer = sdpmod.build_sdp(self.sdp_ip(), rtp.port, self._codecs())
            t0 = loop.time()
            prov = {"first": None, "ring": None, "codes": []}

            def on_prov(r: SipMessage) -> None:
                now = loop.time()
                prov["codes"].append(r.status)
                if prov["first"] is None:
                    prov["first"] = now
                if r.status in (180, 183) and prov["ring"] is None:
                    prov["ring"] = now

            auth = None
            resp = None
            invite = None
            for attempt in range(3):
                invite = SipMessage(method="INVITE", uri=ruri)
                invite.add("Via", self.via())
                invite.add("Max-Forwards", "70")
                invite.add("From", self._from(from_tag))
                invite.add("To", f"<{ruri}>")
                invite.add("Call-ID", call_id)
                invite.add("CSeq", f"{cseq} INVITE")
                invite.add("Contact", self.contact())
                invite.add("Allow", ALLOW)
                invite.add("User-Agent", USER_AGENT)
                for name, value in extra_headers or []:
                    invite.add(name, value)
                if auth:
                    invite.add(*auth)
                invite.add("Content-Type", "application/sdp")
                invite.body = offer
                resp = await self.request(invite, on_provisional=on_prov, timeout=ring_timeout)
                if resp is not None and resp.status in (401, 407) and attempt < 2:
                    auth = self._auth_header(resp, "INVITE", ruri)
                    if auth is None:
                        break
                    res["auth_rounds"] += 1
                    cseq += 1
                    continue
                break
            res["provisional"] = prov["codes"]
            if resp is None:
                if prov["first"] is not None:
                    await self._cancel(invite)
                    res["reason"] = "ring timeout"
                else:
                    res["reason"] = "no response"
                return res
            res["sip_code"] = resp.status
            if prov["ring"] is not None:
                res["pdd_ms"] = round((prov["ring"] - t0) * 1000, 2)
            if not 200 <= resp.status < 300:
                res["reason"] = f"{resp.status} {resp.reason}"
                return res
            t_answer = loop.time()
            res["setup_ms"] = round((t_answer - t0) * 1000, 2)
            if res["pdd_ms"] is None:
                res["pdd_ms"] = res["setup_ms"]

            remote_target = parse_nameaddr(resp.get("contact") or f"<{ruri}>")[1]
            route_set = list(reversed(resp.get_all("record-route")))
            d = Dialog(self, call_id, from_tag, get_tag(resp.get("to")), self.aor, ruri,
                       remote_target, route_set, self.proxy_addr, local_cseq=cseq)
            d.local_sdp = offer
            self.dialogs[call_id] = d
            d.ack = d.new_request("ACK", cseq=cseq)
            self.send(d.ack, d.peer_addr)

            answer = sdpmod.parse_sdp(resp.body)
            neg = sdpmod.negotiated(answer) if answer else None
            if not answer or not answer["port"] or neg is None:
                res["reason"] = "no usable SDP answer"
                await self._bye(d)
                return res
            codec, pt = neg
            res["codec"] = codec
            remote = (answer["ip"], answer["port"])
            rtp.set_remote(remote[0], remote[1], pt, tones.signal_for("up", codec))

            def on_sdp(s: dict) -> None:
                nonlocal remote
                if s.get("port") and s.get("ip"):
                    remote = (s["ip"], s["port"])
                    rtp.remote = remote
            d.on_sdp = on_sdp
            res["success"] = True

            async def _abort_watch() -> None:
                await abort.wait()
                if not d.ended.is_set():
                    d.bye_by = "abort"
                    d.ended.set()
            watch = asyncio.ensure_future(_abort_watch()) if abort is not None else None
            try:
                await rtp.send_stream(duration_s, d.ended)
            finally:
                if watch is not None:
                    watch.cancel()
            if not d.ended.is_set() or d.bye_by == "abort":
                res["bye_code"] = await self._bye(d)
            res["bye_by"] = d.bye_by
            res["duration_s"] = round(loop.time() - t_answer, 3)
            res["dropped"] = d.bye_by == "remote" and res["duration_s"] < duration_s - 2
            if res["dropped"]:
                res["reason"] = "remote hangup before planned end"
            elif d.bye_by == "abort":
                res["reason"] = "ended early: probe restart"
            res["rtcp"] = summarize_rtcp(rtp.peer_reports, rtp.rtts_ms, rtp.sr_received)
            res["rx"] = tones.analyze(rtp.rx_packets, tones.DOWN_FREQS, codec, pt, answer_time=t_answer)
            res["tx_packets"] = rtp.tx_packets
            return res
        finally:
            rtp.close()
            self.dialogs.pop(call_id, None)

    async def _bye(self, d: Dialog) -> int | None:
        d.bye_by = d.bye_by or "local"
        r = await self.request(d.new_request("BYE"), timeout=8, addr=d.peer_addr)
        d.ended.set()
        return r.status if r else None

    async def _cancel(self, invite: SipMessage) -> None:
        c = SipMessage(method="CANCEL", uri=invite.uri)
        c.add("Via", invite.get_all("via")[0])
        for r in invite.get_all("route"):
            c.add("Route", r)
        c.add("Max-Forwards", "70")
        c.add("From", invite.get("from"))
        c.add("To", invite.get("to"))
        c.add("Call-ID", invite.call_id)
        c.add("CSeq", f"{invite.cseq[0]} CANCEL")
        await self.request(c, timeout=8)

    # auto answer ---------------------------------------------------------
    async def answer(self, req: SipMessage, addr, answer_delay: float = 0.5, max_duration: float = 120.0,
                     rtp_timeout: float = 15.0) -> dict:
        """Auto-answer a test call. Ends on BYE, after max_duration, or when the caller's
        audio stops for rtp_timeout seconds (e.g. the BYE got lost behind NAT)."""
        loop = asyncio.get_running_loop()
        from_disp, from_uri, from_params = parse_nameaddr(req.get("from", ""))
        res: dict = {"call_id": req.call_id, "from_user": parse_uri(from_uri)["user"],
                     "started_at": time.time(), "answered": False, "reason": None, "codec": None}
        to_tag = gen_tag()
        branch = req.top_via()["params"].get("branch", "")
        offer = sdpmod.parse_sdp(req.body)
        choice = sdpmod.choose(offer, self._codecs()) if offer else None
        if choice is None:
            self.respond(req, addr, 488, "Not Acceptable Here", to_tag=to_tag)
            res["reason"] = "no acceptable SDP offer"
            return res
        codec, pt = choice
        self.respond(req, addr, 180, "Ringing", to_tag=to_tag, contact=True)
        st = self._stx.get((branch, "INVITE"))
        if st is not None:
            st["to_tag"] = to_tag
        await asyncio.sleep(answer_delay)
        if st is not None and st.get("cancelled"):
            res["reason"] = "cancelled"
            return res
        rtp = await RtpSession.open(self.acc.rtp_port_min, self.acc.rtp_port_max, self.acc.dscp_rtp)
        try:
            ans = sdpmod.build_sdp(self.sdp_ip(), rtp.port, [codec], dtmf_pt=offer.get("dtmf_pt"))
            remote_target = parse_nameaddr(req.get("contact") or req.get("from", ""))[1]
            to_uri = parse_nameaddr(req.get("to", ""))[1]
            d = Dialog(self, req.call_id, to_tag, from_params.get("tag"), to_uri, from_uri, remote_target,
                       req.get_all("record-route"), addr, local_cseq=random.randint(1, 1000))
            d.local_sdp = ans
            self.dialogs[req.call_id] = d
            ok = self.respond(req, addr, 200, "OK", to_tag=to_tag, body=ans, contact=True)
            remote = (offer["ip"], offer["port"])
            rtp.set_remote(remote[0], remote[1], pt, tones.signal_for("down", codec))

            def on_sdp(s: dict) -> None:
                nonlocal remote
                if s.get("port") and s.get("ip"):
                    remote = (s["ip"], s["port"])
                    rtp.remote = remote
            d.on_sdp = on_sdp
            t_answer = loop.time()
            res["answered"] = True
            res["codec"] = codec
            self._spawn(self._retransmit_2xx(d, ok, addr))
            watchdog = self._spawn(self._rtp_watchdog(d, rtp, rtp_timeout)) if rtp_timeout else None
            await rtp.send_stream(max_duration, d.ended)
            if watchdog is not None:
                watchdog.cancel()
            if not d.ended.is_set():
                await self._bye(d)
            res["bye_by"] = d.bye_by
            res["acked"] = d.acked.is_set()
            res["duration_s"] = round(loop.time() - t_answer, 3)
            res["rtcp"] = summarize_rtcp(rtp.peer_reports, rtp.rtts_ms, rtp.sr_received)
            res["rx"] = tones.analyze(rtp.rx_packets, tones.UP_FREQS, codec, pt, answer_time=t_answer)
            res["tx_packets"] = rtp.tx_packets
            return res
        finally:
            rtp.close()
            self.dialogs.pop(req.call_id, None)

    async def _rtp_watchdog(self, d: Dialog, rtp: RtpSession, timeout: float) -> None:
        """Hang up if audio from the caller stopped (only after it had been flowing)."""
        while not d.ended.is_set():
            await asyncio.sleep(1.0)
            if rtp.rx_packets and time.monotonic() - rtp.rx_packets[-1][0] > timeout:
                log.warning("[%s] no audio from caller for %.0f s - ending call (BYE lost?)", self.name, timeout)
                d.bye_by = "rtp-timeout"
                await self._bye(d)
                return

    async def _retransmit_2xx(self, d: Dialog, ok: SipMessage, addr) -> None:
        interval = T1
        deadline = time.monotonic() + 32
        while not d.acked.is_set() and not d.ended.is_set() and time.monotonic() < deadline:
            try:
                await asyncio.wait_for(d.acked.wait(), interval)
                return
            except asyncio.TimeoutError:
                self.send(ok, addr)
                interval = min(interval * 2, T2)

