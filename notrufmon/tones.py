"""Tone signal that survives mobile networks, and its detector.

The 112 test only has to show that audio gets through in both directions. Mobile
networks transcode (AMR/EVS), suppress noise and send comfort noise in pauses, so
a bit-exact comparison (voipmon) is useless there. Instead each direction plays a
cycle of short sine bursts at its own frequencies:

    up   (probe  -> server):  700, 1100, 1500, 1900 Hz
    down (server -> probe ):  500,  900, 1300, 1700 Hz

each burst 300 ms followed by 100 ms silence (cycle 1.6 s). Bursts instead of a
steady tone, because noise suppression tends to fade out stationary signals.
Different frequencies per direction mean an echo of our own signal is never
mistaken for the far end.

The receiver decodes G.711, places the samples by RTP timestamp, and looks at
50 ms windows with the Goertzel algorithm: a window counts when one of the
expected frequencies carries most of its energy. From that: share of tone
windows, number of bursts heard, time to the first burst, longest gap.
"""
from __future__ import annotations

import math

RATE = 8000
FRAME_SAMPLES = 160                 # 20 ms
BURST_MS, PAUSE_MS = 300, 100
UP_FREQS = (700, 1100, 1500, 1900)
DOWN_FREQS = (500, 900, 1300, 1700)
LEVEL = 0.25                        # of full scale, about -15 dBm0
WINDOW = 400                        # 50 ms
MIN_ENERGY = 1e-5                   # mean square (full scale 1.0): about -50 dBFS, below = silence
DOMINANCE = 0.45                    # share of the window energy that must sit on the expected tone


# ----------------------------------------------------------------------------- G.711
def _seg(v: int, ends: tuple) -> int:
    for i, e in enumerate(ends):
        if v <= e:
            return i
    return len(ends)


_A_END = (0x1F, 0x3F, 0x7F, 0xFF, 0x1FF, 0x3FF, 0x7FF, 0xFFF)
_U_END = (0x3F, 0x7F, 0xFF, 0x1FF, 0x3FF, 0x7FF, 0xFFF, 0x1FFF)


def _lin2alaw(s: int) -> int:
    """ITU-T G.711 A-law (same algorithm as the reference code / CPython audioop)."""
    v = s >> 3
    if v >= 0:
        mask = 0xD5
    else:
        mask, v = 0x55, -v - 1
    seg = _seg(v, _A_END)
    if seg >= 8:
        return 0x7F ^ mask
    a = (seg << 4) | ((v >> 1 if seg < 2 else v >> seg) & 0x0F)
    return a ^ mask


def _alaw2lin(b: int) -> int:
    b ^= 0x55
    seg, mant = (b >> 4) & 7, b & 0x0F
    v = (mant << 4) + 8 if seg == 0 else ((mant << 4) + 0x108) << (seg - 1)
    return v if b & 0x80 else -v


def _lin2ulaw(s: int) -> int:
    """ITU-T G.711 mu-law (same algorithm as the reference code / CPython audioop)."""
    v = s >> 2
    if v < 0:
        mask, v = 0x7F, -v
    else:
        mask = 0xFF
    v = min(v, 8159) + 33
    seg = _seg(v, _U_END)
    if seg >= 8:
        return 0x7F ^ mask
    return ((seg << 4) | ((v >> (seg + 1)) & 0x0F)) ^ mask


def _ulaw2lin(b: int) -> int:
    b = ~b & 0xFF
    sign, exp, mant = b & 0x80, (b >> 4) & 7, b & 0x0F
    v = (((mant << 3) + 0x84) << exp) - 0x84
    return -v if sign else v


ENC = {"PCMA": bytes(_lin2alaw(s) for s in range(-32768, 32768)),
       "PCMU": bytes(_lin2ulaw(s) for s in range(-32768, 32768))}
DEC = {"PCMA": [_alaw2lin(b) for b in range(256)], "PCMU": [_ulaw2lin(b) for b in range(256)]}


def encode(samples, codec: str) -> bytes:
    t = ENC[codec]
    return bytes(t[max(-32768, min(32767, int(s))) + 32768] for s in samples)


def decode(payload: bytes, codec: str) -> list[int]:
    t = DEC[codec]
    return [t[b] for b in payload]


# ----------------------------------------------------------------------------- signal
class ToneSignal:
    """Frame source for RtpSession (same interface as voipmon's TestSignal: frame(i))."""

    def __init__(self, freqs: tuple, codec: str = "PCMA"):
        self.freqs, self.codec = tuple(freqs), codec
        burst, pause = BURST_MS * RATE // 1000, PAUSE_MS * RATE // 1000
        ramp = 40                                      # 5 ms fade in/out: no clicks
        cycle: list[float] = []
        for f in self.freqs:
            for n in range(burst):
                g = min(1.0, n / ramp, (burst - 1 - n) / ramp)
                cycle.append(LEVEL * 32767 * g * math.sin(2 * math.pi * f * n / RATE))
            cycle.extend([0.0] * pause)
        pcm = encode(cycle, codec)
        self.loop = len(pcm) // FRAME_SAMPLES          # 1.6 s = 80 frames
        self._frames = [pcm[i * FRAME_SAMPLES:(i + 1) * FRAME_SAMPLES] for i in range(self.loop)]

    def frame(self, i: int) -> bytes:
        return self._frames[i % self.loop]


def signal_for(direction: str, codec: str = "PCMA") -> ToneSignal:
    return ToneSignal(UP_FREQS if direction == "up" else DOWN_FREQS, codec)


# ----------------------------------------------------------------------------- detection
def _goertzel(x: list[float], f: float) -> float:
    w = 2 * math.pi * f / RATE
    c = 2 * math.cos(w)
    s1 = s2 = 0.0
    for v in x:
        s0 = v + c * s1 - s2
        s2, s1 = s1, s0
    return (s1 * s1 + s2 * s2 - c * s1 * s2) * 2 / (len(x) * len(x))   # power of that sine (amplitude^2 / 2)


def _timeline(packets: list, codec: str, pt: int) -> tuple[list[float], float | None]:
    """Decode audio packets in RTP-timestamp order into one sample list (missing = 0.0).
    Returns samples (full scale 1.0) and the arrival time of the first audio packet."""
    audio = [p for p in packets if p[4] == pt and p[5]]
    if not audio:
        return [], None
    base_ts = audio[0][2]
    pos = {}
    for p in audio:
        off = (p[2] - base_ts) % 2**32
        if off < 2**31:                                  # ignore packets "before" the first one
            pos.setdefault(off, p[5])
    end = max(o + len(pl) for o, pl in pos.items())
    end = min(end, RATE * 600)                           # safety: at most 10 min
    out = [0.0] * end
    for off, pl in pos.items():
        if off >= end:
            continue
        for k, v in enumerate(decode(pl, codec)[:end - off]):
            out[off + k] = v / 32768.0
    return out, min(p[0] for p in audio)


def analyze(packets: list, freqs: tuple, codec: str, pt: int, answer_time: float | None = None) -> dict:
    """Check whether the far end's tone signal arrived. packets: voipmon RtpSession.rx_packets
    tuples (arrival, seq, ts, ssrc, pt, payload, addr)."""
    x, first_arrival = _timeline(packets, codec, pt)
    res = {"packets": sum(1 for p in packets if p[4] == pt), "windows": 0, "tone_windows": 0,
           "tone_ratio": 0.0, "bursts": 0, "first_tone_s": None, "longest_gap_s": None,
           "audio_ok": False, "freqs_seen": []}
    n = len(x) // WINDOW
    res["windows"] = n
    if n == 0:
        return res
    hits: list[int | None] = []
    for w in range(n):
        seg = x[w * WINDOW:(w + 1) * WINDOW]
        energy = sum(v * v for v in seg) / WINDOW
        if energy < MIN_ENERGY:
            hits.append(None)
            continue
        powers = [_goertzel(seg, f) for f in freqs]
        k = max(range(len(freqs)), key=lambda i: powers[i])
        hits.append(k if powers[k] >= DOMINANCE * energy else None)
    tone_w = [i for i, h in enumerate(hits) if h is not None]
    res["tone_windows"] = len(tone_w)
    # tone is on 300 of 400 ms: a perfect stream has ~75 % tone windows
    res["tone_ratio"] = round(len(tone_w) / (n * BURST_MS / (BURST_MS + PAUSE_MS)), 3)
    bursts, prev = 0, None
    for h in hits:
        if h is not None and h != prev:
            bursts += 1
        prev = h
    res["bursts"] = bursts
    res["freqs_seen"] = sorted({freqs[h] for h in hits if h is not None})
    if tone_w:
        first_s = tone_w[0] * WINDOW / RATE
        if answer_time is not None and first_arrival is not None:
            first_s += first_arrival - answer_time
        res["first_tone_s"] = round(first_s, 2)
        gaps = [b - a for a, b in zip(tone_w, tone_w[1:])]
        res["longest_gap_s"] = round((max(gaps, default=1) - 1) * WINDOW / RATE, 2)
    # audio is "there" if at least half the expected tone time and 3 distinct bursts arrived
    res["audio_ok"] = res["tone_ratio"] >= 0.5 and bursts >= 3 and len(res["freqs_seen"]) >= 2
    return res
