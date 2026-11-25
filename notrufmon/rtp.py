"""RTP/RTCP session: paced sender of the test signal, raw receiver, basic RTCP SR/RR."""
from __future__ import annotations

import asyncio
import logging
import random
import socket
import struct
import time

FRAME_SAMPLES = 160       # 20 ms G.711 at 8 kHz
FRAME_SEC = 0.02

log = logging.getLogger("notrufmon.rtp")
RTP_HDR = struct.Struct("!BBHII")
NTP_OFFSET = 2208988800
RTCP_INTERVAL = 5.0


def ntp_now() -> tuple[int, int]:
    t = time.time() + NTP_OFFSET
    sec = int(t)
    return sec & 0xFFFFFFFF, int((t - sec) * 2**32) & 0xFFFFFFFF


def ntp_mid32(sec: int, frac: int) -> int:
    return ((sec & 0xFFFF) << 16) | (frac >> 16)


def make_udp_socket(port: int, dscp: int, bind_ip: str = "0.0.0.0") -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_TOS, (dscp & 0x3F) << 2)
    except OSError:
        log.warning("could not set DSCP %s", dscp)
    sock.bind((bind_ip, port))
    sock.setblocking(False)
    return sock


class _Proto(asyncio.DatagramProtocol):
    def __init__(self, cb):
        self.cb = cb

    def datagram_received(self, data, addr):
        self.cb(data, addr)

    def error_received(self, exc):  # ICMP port unreachable etc.
        log.debug("rtp socket error: %s", exc)


class RtpSession:
    def __init__(self):
        self.port = 0
        self.ssrc = random.getrandbits(32)
        self.seq0 = random.getrandbits(16)
        self.ts0 = random.getrandbits(32)
        self.remote: tuple[str, int] | None = None
        self.pt = 8
        self.signal = None          # object with frame(i) -> 160-byte payload
        self.rx_packets: list[tuple] = []
        self.tx_packets = 0
        self.tx_octets = 0
        self._rtp_tr = None
        self._rtcp_tr = None
        self._tx_mono0: float | None = None
        # receive state for RTCP report blocks
        self._rx_ssrc = None
        self._base_seq = None
        self._max_seq = 0
        self._cycles = 0
        self._received = 0
        self._expected_prior = 0
        self._received_prior = 0
        self._transit = None
        self._jitter = 0.0
        self._peer_lsr = 0
        self._peer_sr_mono = 0.0
        # what the peer reported about us
        self.peer_reports: list[dict] = []
        self.rtts_ms: list[float] = []
        self.sr_received = 0

    @classmethod
    async def open(cls, port_min: int, port_max: int, dscp: int = 46) -> "RtpSession":
        self = cls()
        loop = asyncio.get_running_loop()
        evens = [p for p in range(port_min + (port_min & 1), port_max, 2)]
        random.shuffle(evens)
        for p in evens[:64]:
            try:
                rtp_sock = make_udp_socket(p, dscp)
            except OSError:
                continue
            try:
                rtcp_sock = make_udp_socket(p + 1, dscp)
            except OSError:
                rtp_sock.close()
                continue
            self.port = p
            self._rtp_tr, _ = await loop.create_datagram_endpoint(lambda: _Proto(self._on_rtp), sock=rtp_sock)
            self._rtcp_tr, _ = await loop.create_datagram_endpoint(lambda: _Proto(self._on_rtcp), sock=rtcp_sock)
            return self
        raise OSError(f"no free RTP port in {port_min}-{port_max}")

    def set_remote(self, ip: str, port: int, pt: int, signal) -> None:
        self.remote = (ip, port)
        self.pt = pt
        self.signal = signal

    def close(self) -> None:
        for tr in (self._rtp_tr, self._rtcp_tr):
            if tr is not None:
                tr.close()
        self._rtp_tr = self._rtcp_tr = None

    # sending -----------------------------------------------------------
    async def send_stream(self, duration_s: float, stop: asyncio.Event) -> int:
        """Send the test signal paced at 20 ms until duration or stop event."""
        loop = asyncio.get_running_loop()
        n = int(duration_s / FRAME_SEC)
        t0 = loop.time()
        self._tx_mono0 = time.monotonic()
        next_rtcp = t0 + random.uniform(0.5, 1.5)   # early first report, then every 5 s
        sent = 0
        for i in range(n):
            if stop.is_set():
                break
            delay = t0 + i * FRAME_SEC - loop.time()
            if delay > 0:
                await asyncio.sleep(delay)
            if stop.is_set():
                break
            self._send_frame(i)
            sent += 1
            if loop.time() >= next_rtcp:
                next_rtcp += RTCP_INTERVAL
                self._send_rtcp()
        self._send_rtcp()
        return sent

    def _send_frame(self, i: int) -> None:
        if self._rtp_tr is None or self.remote is None or self.signal is None:
            return
        payload = self.signal.frame(i)
        b1 = (0x80 if i == 0 else 0) | (self.pt & 0x7F)
        hdr = RTP_HDR.pack(0x80, b1, (self.seq0 + i) & 0xFFFF,
                           (self.ts0 + i * FRAME_SAMPLES) & 0xFFFFFFFF, self.ssrc)
        self._rtp_tr.sendto(hdr + payload, self.remote)
        self.tx_packets += 1
        self.tx_octets += len(payload)

    # receiving ---------------------------------------------------------
    def _on_rtp(self, data: bytes, addr) -> None:
        arrival = time.monotonic()
        if len(data) < 12 or (data[0] >> 6) != 2:
            return
        b0, b1, seq, ts, ssrc = RTP_HDR.unpack_from(data)
        cc = b0 & 0x0F
        off = 12 + 4 * cc
        if b0 & 0x10 and len(data) >= off + 4:  # header extension
            ext_len = struct.unpack_from("!H", data, off + 2)[0]
            off += 4 + 4 * ext_len
        end = len(data)
        if b0 & 0x20 and end > off:  # padding
            end -= data[-1]
        payload = data[off:end]
        pt = b1 & 0x7F
        self.rx_packets.append((arrival, seq, ts, ssrc, pt, payload, (addr[0], addr[1])))
        self._update_rx_stats(seq, ts, ssrc, arrival)

    def _update_rx_stats(self, seq, ts, ssrc, arrival) -> None:
        if self._rx_ssrc != ssrc:
            self._rx_ssrc = ssrc
            self._base_seq = seq
            self._max_seq = seq
            self._cycles = 0
            self._received = 0
            self._expected_prior = self._received_prior = 0
            self._transit = None
        delta = (seq - self._max_seq) & 0xFFFF
        if 0 < delta < 3000:
            if seq < self._max_seq:
                self._cycles += 1 << 16
            self._max_seq = seq
        self._received += 1
        transit = arrival * 8000.0 - ts
        if self._transit is not None:
            d = abs(transit - self._transit)
            if d < 2**31:
                self._jitter += (d - self._jitter) / 16.0
        self._transit = transit

    def _report_block(self) -> bytes:
        if self._rx_ssrc is None:
            return b""
        ext_max = self._cycles + self._max_seq
        expected = ext_max - self._base_seq + 1
        lost = max(0, expected - self._received)
        exp_int = expected - self._expected_prior
        rec_int = self._received - self._received_prior
        self._expected_prior, self._received_prior = expected, self._received
        lost_int = exp_int - rec_int
        fraction = (lost_int << 8) // exp_int if exp_int > 0 and lost_int > 0 else 0
        dlsr = int((time.monotonic() - self._peer_sr_mono) * 65536) if self._peer_lsr else 0
        return struct.pack("!IBBHIIII", self._rx_ssrc, min(fraction, 255),
                           (min(lost, 0x7FFFFF) >> 16) & 0xFF, min(lost, 0x7FFFFF) & 0xFFFF,
                           ext_max & 0xFFFFFFFF, int(self._jitter) & 0xFFFFFFFF,
                           self._peer_lsr, dlsr & 0xFFFFFFFF)

    def _send_rtcp(self) -> None:
        if self._rtcp_tr is None or self.remote is None:
            return
        sec, frac = ntp_now()
        if self._tx_mono0 is not None:
            rtp_ts = (self.ts0 + int((time.monotonic() - self._tx_mono0) * 8000)) & 0xFFFFFFFF
        else:
            rtp_ts = self.ts0
        rb = self._report_block()
        rc = 1 if rb else 0
        sr_len = (28 + len(rb)) // 4 - 1
        sr = struct.pack("!BBHI", 0x80 | rc, 200, sr_len, self.ssrc)
        sr += struct.pack("!IIIII", sec, frac, rtp_ts, self.tx_packets & 0xFFFFFFFF,
                          self.tx_octets & 0xFFFFFFFF) + rb
        cname = b"notrufmon"
        item = bytes([1, len(cname)]) + cname + b"\x00"
        chunk = struct.pack("!I", self.ssrc) + item
        chunk += b"\x00" * ((4 - len(chunk) % 4) % 4)
        sdes = struct.pack("!BBH", 0x81, 202, len(chunk) // 4) + chunk
        self._rtcp_tr.sendto(sr + sdes, (self.remote[0], self.remote[1] + 1))

    def _on_rtcp(self, data: bytes, addr) -> None:
        now = time.monotonic()
        off = 0
        while off + 8 <= len(data):
            b0, pt, length = struct.unpack_from("!BBH", data, off)
            if (b0 >> 6) != 2:
                return
            rc = b0 & 0x1F
            plen = (length + 1) * 4
            pkt = data[off: off + plen]
            blocks_at = None
            if pt == 200 and len(pkt) >= 28:
                sec, frac = struct.unpack_from("!II", pkt, 8)
                self._peer_lsr = ntp_mid32(sec, frac)
                self._peer_sr_mono = now
                self.sr_received += 1
                blocks_at = 28
            elif pt == 201:
                blocks_at = 8
            if blocks_at is not None:
                for i in range(rc):
                    bo = blocks_at + 24 * i
                    if bo + 24 > len(pkt):
                        break
                    ssrc, fl, cl_hi, cl_lo, ext, jit, lsr, dlsr = struct.unpack_from("!IBBHIIII", pkt, bo)
                    cum = (cl_hi << 16) | cl_lo
                    if cum & 0x800000:
                        cum -= 1 << 24
                    self.peer_reports.append({"fraction_lost": fl / 256.0, "cum_lost": cum,
                                              "jitter_ms": jit / 8.0, "t": now})
                    if lsr:
                        s, f = ntp_now()
                        rtt = ((ntp_mid32(s, f) - lsr - dlsr) & 0xFFFFFFFF) / 65536.0
                        if 0 <= rtt < 10:
                            self.rtts_ms.append(rtt * 1000.0)
            off += plen
