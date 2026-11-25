"""SIP message parsing/building and digest authentication (RFC 3261 / RFC 7616 subset)."""
from __future__ import annotations

import hashlib
import re
import secrets

COMPACT = {
    "v": "via", "f": "from", "t": "to", "i": "call-id", "m": "contact", "l": "content-length",
    "c": "content-type", "k": "supported", "s": "subject", "e": "content-encoding",
    "o": "event", "u": "allow-events", "r": "refer-to", "b": "referred-by", "x": "session-expires",
}
CANON = {
    "call-id": "Call-ID", "cseq": "CSeq", "www-authenticate": "WWW-Authenticate",
    "min-se": "Min-SE", "mime-version": "MIME-Version",
}
MULTI = {"via", "route", "record-route", "contact", "allow", "supported"}


def canon(name: str) -> str:
    n = name.strip().lower()
    n = COMPACT.get(n, n)
    return CANON.get(n) or "-".join(p.capitalize() for p in n.split("-"))


def split_values(value: str) -> list[str]:
    """Split a comma separated header value, respecting quotes and <...>."""
    out, cur = [], []
    quoted = False
    angle = 0
    for ch in value:
        if ch == '"':
            quoted = not quoted
        elif not quoted and ch == "<":
            angle += 1
        elif not quoted and ch == ">":
            angle = max(0, angle - 1)
        if ch == "," and not quoted and angle == 0:
            part = "".join(cur).strip()
            if part:
                out.append(part)
            cur = []
            continue
        cur.append(ch)
    part = "".join(cur).strip()
    if part:
        out.append(part)
    return out


def parse_params(s: str) -> dict[str, str]:
    d: dict[str, str] = {}
    for part in s.split(";"):
        part = part.strip()
        if not part:
            continue
        k, _, v = part.partition("=")
        d[k.strip().lower()] = v.strip().strip('"')
    return d


def split_hostport(s: str, default: int | None = 5060) -> tuple[str, int | None]:
    s = s.strip()
    if s.startswith("["):
        host, _, rest = s[1:].partition("]")
        port = rest[1:] if rest.startswith(":") else ""
    else:
        host, _, port = s.partition(":")
    return host, int(port) if port.isdigit() else default


def parse_nameaddr(v: str) -> tuple[str, str, dict[str, str]]:
    v = v.strip()
    if "<" in v:
        disp, _, rest = v.partition("<")
        uri, _, params = rest.partition(">")
        return disp.strip().strip('"'), uri.strip(), parse_params(params)
    uri, _, params = v.partition(";")
    return "", uri.strip(), parse_params(params)


def get_tag(v: str | None) -> str | None:
    if not v:
        return None
    return parse_nameaddr(v)[2].get("tag") or None


def parse_uri(uri: str) -> dict:
    scheme, _, rest = uri.strip().partition(":")
    rest, _, params = rest.partition(";")
    rest = rest.split("?")[0]
    userinfo, at, hostport = rest.rpartition("@")
    if not at:
        hostport, userinfo = rest, ""
    host, port = split_hostport(hostport, None)
    return {"scheme": scheme.lower(), "user": userinfo.partition(":")[0], "host": host,
            "port": port, "params": parse_params(params)}


def parse_via(v: str) -> dict:
    head, _, params = v.partition(";")
    proto, _, sentby = head.strip().partition(" ")
    host, port = split_hostport(sentby)
    return {"proto": proto, "host": host, "port": port, "params": parse_params(params)}


def gen_tag() -> str:
    return secrets.token_hex(6)


def gen_branch() -> str:
    return "z9hG4bK" + secrets.token_hex(8)


def gen_callid() -> str:
    return secrets.token_hex(12) + "@notrufmon"


class SipMessage:
    def __init__(self, method: str | None = None, uri: str | None = None,
                 status: int | None = None, reason: str = ""):
        self.method = method
        self.uri = uri
        self.status = status
        self.reason = reason
        self.headers: list[list[str]] = []
        self.body = b""

    @property
    def is_request(self) -> bool:
        return self.method is not None

    # header access ------------------------------------------------------
    def get(self, name: str, default: str | None = None) -> str | None:
        n = canon(name).lower()
        for k, v in self.headers:
            if k.lower() == n:
                return v
        return default

    def get_all(self, name: str) -> list[str]:
        n = canon(name).lower()
        out: list[str] = []
        for k, v in self.headers:
            if k.lower() == n:
                out.extend(split_values(v) if n in MULTI else [v])
        return out

    def add(self, name: str, value) -> None:
        self.headers.append([canon(name), str(value)])

    def remove(self, name: str) -> None:
        n = canon(name).lower()
        self.headers = [h for h in self.headers if h[0].lower() != n]

    def set(self, name: str, value) -> None:
        self.remove(name)
        self.add(name, value)

    @property
    def call_id(self) -> str:
        return self.get("call-id") or ""

    @property
    def cseq(self) -> tuple[int, str]:
        num, _, method = (self.get("cseq") or "0 X").partition(" ")
        return int(num), method.strip().upper()

    def top_via(self) -> dict:
        vias = self.get_all("via")
        return parse_via(vias[0]) if vias else {"proto": "", "host": "", "port": None, "params": {}}

    # wire format --------------------------------------------------------
    def to_bytes(self) -> bytes:
        if self.is_request:
            start = f"{self.method} {self.uri} SIP/2.0"
        else:
            start = f"SIP/2.0 {self.status} {self.reason}"
        lines = [start] + [f"{k}: {v}" for k, v in self.headers if k.lower() != "content-length"]
        lines.append(f"Content-Length: {len(self.body)}")
        return ("\r\n".join(lines) + "\r\n\r\n").encode() + self.body

    @classmethod
    def parse(cls, data: bytes) -> "SipMessage":
        head, sep, body = data.partition(b"\r\n\r\n")
        if not sep:
            head, sep, body = data.partition(b"\n\n")
        lines = head.decode("utf-8", "replace").replace("\r\n", "\n").split("\n")
        while lines and not lines[0].strip():
            lines.pop(0)
        if not lines:
            raise ValueError("empty message")
        start = lines[0].strip()
        if start.startswith("SIP/2.0 "):
            parts = start.split(" ", 2)
            msg = cls(status=int(parts[1]), reason=parts[2] if len(parts) > 2 else "")
        else:
            parts = start.split(" ")
            if len(parts) != 3 or not parts[2].startswith("SIP/"):
                raise ValueError(f"bad start line: {start!r}")
            msg = cls(method=parts[0].upper(), uri=parts[1])
        for line in lines[1:]:
            if not line.strip():
                continue
            if line[0] in " \t" and msg.headers:
                msg.headers[-1][1] += " " + line.strip()
                continue
            name, _, value = line.partition(":")
            msg.headers.append([canon(name), value.strip()])
        cl = msg.get("content-length")
        if cl is not None and cl.strip().isdigit():
            body = body[: int(cl)]
        msg.body = body
        return msg


# digest authentication ---------------------------------------------------
_CHAL_RE = re.compile(r'(\w+)\s*=\s*("([^"]*)"|[^,\s]+)')


def parse_challenge(value: str) -> dict[str, str]:
    scheme, _, rest = value.strip().partition(" ")
    params = {m.group(1).lower(): (m.group(3) if m.group(3) is not None else m.group(2))
              for m in _CHAL_RE.finditer(rest)}
    params["scheme"] = scheme
    return params


def _hash(alg: str):
    base = alg.upper().replace("-SESS", "")
    if base == "MD5":
        return lambda s: hashlib.md5(s.encode()).hexdigest()
    if base in ("SHA-256", "SHA256"):
        return lambda s: hashlib.sha256(s.encode()).hexdigest()
    raise ValueError(f"unsupported digest algorithm {alg}")


def digest_supported(chal: dict) -> bool:
    try:
        _hash(chal.get("algorithm", "MD5"))
        return chal.get("scheme", "").lower() == "digest"
    except ValueError:
        return False


def digest_authorization(chal: dict, method: str, uri: str, username: str, password: str,
                         nc: int = 1, cnonce: str | None = None) -> str:
    alg = chal.get("algorithm", "MD5")
    h = _hash(alg)
    realm = chal.get("realm", "")
    nonce = chal.get("nonce", "")
    qops = [q.strip() for q in chal.get("qop", "").split(",") if q.strip()]
    cnonce = cnonce or secrets.token_hex(8)
    ncs = f"{nc:08x}"
    ha1 = h(f"{username}:{realm}:{password}")
    if alg.upper().endswith("-SESS"):
        ha1 = h(f"{ha1}:{nonce}:{cnonce}")
    ha2 = h(f"{method}:{uri}")
    if "auth" in qops:
        response = h(f"{ha1}:{nonce}:{ncs}:{cnonce}:auth:{ha2}")
    else:
        response = h(f"{ha1}:{nonce}:{ha2}")
    parts = [f'username="{username}"', f'realm="{realm}"', f'nonce="{nonce}"', f'uri="{uri}"',
             f'response="{response}"', f"algorithm={alg}"]
    if "auth" in qops:
        parts += [f'cnonce="{cnonce}"', "qop=auth", f"nc={ncs}"]
    if "opaque" in chal:
        parts.append(f'opaque="{chal["opaque"]}"')
    return "Digest " + ", ".join(parts)
