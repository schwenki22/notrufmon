"""Minimal SDP offer/answer for G.711 audio."""
from __future__ import annotations

import random

PT_BY_CODEC = {"PCMU": 0, "PCMA": 8}
CODEC_BY_PT = {0: "PCMU", 8: "PCMA"}


def build_sdp(ip: str, port: int, codecs: list[str], dtmf_pt: int | None = 101,
              session_id: int | None = None) -> bytes:
    sid = session_id or random.randint(10**6, 10**9)
    pts = [PT_BY_CODEC[c] for c in codecs]
    fmt = " ".join(str(p) for p in pts + ([dtmf_pt] if dtmf_pt is not None else []))
    lines = [
        "v=0",
        f"o=notrufmon {sid} {sid} IN IP4 {ip}",
        "s=notrufmon",
        f"c=IN IP4 {ip}",
        "t=0 0",
        f"m=audio {port} RTP/AVP {fmt}",
    ]
    for c in codecs:
        lines.append(f"a=rtpmap:{PT_BY_CODEC[c]} {c}/8000")
    if dtmf_pt is not None:
        lines.append(f"a=rtpmap:{dtmf_pt} telephone-event/8000")
        lines.append(f"a=fmtp:{dtmf_pt} 0-16")
    lines += ["a=ptime:20", "a=sendrecv"]
    return ("\r\n".join(lines) + "\r\n").encode()


def parse_sdp(body: bytes | str | None) -> dict | None:
    if not body:
        return None
    text = body.decode("utf-8", "replace") if isinstance(body, bytes) else body
    session_ip = media_ip = None
    port = None
    pts: list[int] = []
    rtpmap: dict[int, str] = {}
    direction = "sendrecv"
    in_audio = False
    seen_audio = False
    for raw in text.splitlines():
        line = raw.strip()
        if len(line) < 2 or line[1] != "=":
            continue
        k, v = line[0], line[2:]
        if k == "m":
            parts = v.split()
            in_audio = parts[0] == "audio" and not seen_audio
            if in_audio:
                seen_audio = True
                port = int(parts[1])
                pts = [int(p) for p in parts[3:] if p.isdigit()]
        elif k == "c":
            ip = v.split()[-1].split("/")[0]
            if in_audio:
                media_ip = ip
            elif not seen_audio:
                session_ip = ip
        elif k == "a" and (in_audio or not seen_audio):
            if v.startswith("rtpmap:"):
                pt_s, _, enc = v[7:].partition(" ")
                if pt_s.isdigit():
                    rtpmap[int(pt_s)] = enc.split("/")[0]
            elif v in ("sendrecv", "sendonly", "recvonly", "inactive"):
                direction = v
    if port is None:
        return None
    dtmf = next((pt for pt, name in rtpmap.items() if name.lower() == "telephone-event"), None)
    return {"ip": media_ip or session_ip, "port": port, "pts": pts, "rtpmap": rtpmap,
            "dtmf_pt": dtmf, "direction": direction}


def codec_of(pt: int, rtpmap: dict[int, str]) -> str | None:
    name = rtpmap.get(pt, CODEC_BY_PT.get(pt))
    return name.upper() if name and name.upper() in PT_BY_CODEC else None


def choose(sdp: dict, preferred: list[str]) -> tuple[str, int] | None:
    """Answerer side: pick our preferred codec if offered, else any offered G.711."""
    offered = {codec_of(pt, sdp["rtpmap"]): pt for pt in sdp["pts"] if codec_of(pt, sdp["rtpmap"])}
    for c in preferred:
        if c in offered:
            return c, offered[c]
    return None


def negotiated(sdp: dict) -> tuple[str, int] | None:
    """Offerer side: first G.711 payload type in the answer."""
    for pt in sdp["pts"]:
        c = codec_of(pt, sdp["rtpmap"])
        if c:
            return c, pt
    return None
