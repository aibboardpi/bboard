"""Record model, `data` schema validation, durations, and the plain log-line wire format.

Wire line:  ulid | ts_iso | group | agent_id:profile | text | data_json
  - text escapes backslash, newline and tab (\\\\, \\n, \\t); it may contain raw '|' but never
    U+0085/U+2028/U+2029 or bidi controls (rejected on write)
  - data_json is compact JSON with '|' (and those characters) written as \\uXXXX, so the LAST
    ' | ' always separates text from data_json and no reader sees a second line.
"""

from __future__ import annotations

import json
import math
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .ulid import is_ulid, ulid_ms

# \Z, not $: Python's `$` also matches before a trailing "\n", which would let a newline into a log line
GROUP_RE = re.compile(r"^[a-z0-9-]{3,32}\Z")
PROFILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}\Z")
AGENT_ID_RE = re.compile(r"^[0-9a-z]{8}\Z")
DURATION_RE = re.compile(r"^([0-9]{1,7})([smhdw])\Z")
BOARD_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,63}\Z")
_UNIT = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
# C0/C1 controls (but \t, \n), plus what other readers treat as a line break (U+0085, U+2028, U+2029)
# or as a direction flip (bidi controls): either could make one post render as a forged line.
_UNSAFE = "\x7f-\x9f؜‎‏ -‮⁦-⁩"
_BAD_CHARS = re.compile(f"[\x00-\x08\x0b-\x1f{_UNSAFE}]")
_UNSAFE_RE = re.compile(f"[{_UNSAFE}]")
# a lone UTF-16 surrogate (JSON "\ud800") is a Python str but not Unicode: it can't be encoded to sign or store
_SURROGATE_RE = re.compile(r"[\ud800-\udfff]")
MAX_DATA_DEPTH = 8

STATUSES = ("open", "claimed", "done", "failed")
SEVERITIES = ("info", "warn", "critical")


class PostError(Exception):
    """A rejected write. `code` is a stable machine-readable slug."""

    def __init__(self, status: int, code: str, msg: str = ""):
        super().__init__(f"{code}: {msg}" if msg else code)
        self.status, self.code, self.msg = status, code, msg


def parse_duration(s: str) -> int:
    m = DURATION_RE.match(s.strip()) if isinstance(s, str) else None
    if not m:
        raise ValueError(f"bad duration {s!r}; use e.g. 90s, 10m, 1h, 7d, 2w")
    return int(m.group(1)) * _UNIT[m.group(2)]


def iso(epoch: float) -> str:
    return datetime.fromtimestamp(int(epoch), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s: str) -> float:
    try:
        dt = datetime.fromisoformat(s.strip().replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        raise ValueError(f"bad ISO-8601 timestamp {s!r}") from None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def validate_text(text) -> str:
    if not isinstance(text, str) or not text.strip():
        raise PostError(400, "bad_text", "text is required")
    if _BAD_CHARS.search(text):
        raise PostError(400, "bad_text", "control, line-separator and bidi characters not allowed; use \\n")
    if _SURROGATE_RE.search(text):
        raise PostError(400, "bad_text", "text must be valid Unicode (no lone surrogates)")
    return text


def check_json(value) -> None:
    """`data` must nest at most MAX_DATA_DEPTH deep and hold only finite numbers (no NaN/Infinity,
    which are not JSON) and valid Unicode strings (no lone surrogates), keys included. Iterative, so
    a hostile document can't exhaust the stack."""
    stack = [(value, 1)]
    while stack:
        v, depth = stack.pop()
        if isinstance(v, float) and not math.isfinite(v):
            raise PostError(400, "bad_data", "numbers must be finite (no NaN/Infinity)")
        if isinstance(v, str) and _SURROGATE_RE.search(v):
            raise PostError(400, "bad_data", "strings must be valid Unicode (no lone surrogates)")
        if isinstance(v, (dict, list)):
            if depth > MAX_DATA_DEPTH:
                raise PostError(400, "bad_data", f"data nests deeper than {MAX_DATA_DEPTH} levels")
            if isinstance(v, dict):
                stack.extend((k, depth) for k in v)
            stack.extend((x, depth + 1) for x in (v.values() if isinstance(v, dict) else v))


def validate_data(data, agent_id: str, max_bytes: int, canonical: str) -> dict:
    """Checks the canonical `data` schema. Unknown keys are allowed (agents extend freely)."""
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise PostError(400, "bad_data", "data must be a JSON object")
    if len(canonical.encode("utf-8")) > max_bytes:
        raise PostError(400, "bad_data", f"data exceeds {max_bytes} bytes")
    if "root_ulid" in data:
        raise PostError(400, "bad_data", "root_ulid is server-computed; send reply_to instead")
    if "reply_to" in data and not is_ulid(data["reply_to"]):
        raise PostError(400, "bad_data", "reply_to must be a ULID")
    if "renew" in data and (not is_ulid(data["renew"]) or len(data) != 1):
        raise PostError(400, "bad_data", 'a renew is exactly {"renew": "<ulid of your post>"}')
    if "ttl" in data and "expires_at" in data:
        raise PostError(400, "bad_data", "send ttl or expires_at, not both")
    if "ttl" in data:
        try:
            parse_duration(data["ttl"])
        except ValueError as e:
            raise PostError(400, "bad_data", str(e)) from None
    if "expires_at" in data:
        try:
            parse_iso(data["expires_at"])
        except (ValueError, TypeError) as e:
            raise PostError(400, "bad_data", f"expires_at: {e}") from None
    if "status" in data and data["status"] not in STATUSES:
        raise PostError(400, "bad_data", "status must be open|claimed|done|failed")
    if data.get("status") in ("claimed", "done", "failed") and "reply_to" not in data:
        raise PostError(400, "bad_data", f"status={data['status']} must reply_to the task")
    if "severity" in data and data["severity"] not in SEVERITIES:
        raise PostError(400, "bad_data", "severity must be info|warn|critical")
    for k, lo, hi in (("lat", -90, 90), ("lon", -180, 180)):
        if k in data:
            v = data[k]
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not lo <= v <= hi:
                raise PostError(400, "bad_data", f"{k} must be a number in [{lo}, {hi}]")
    if "lease_owner" in data and data["lease_owner"] != agent_id:
        raise PostError(400, "bad_data", "lease_owner must be your own agent_id")
    return data


def resolve_expiry(data: dict, now: float, durable: bool, s) -> float:
    """Explicit ttl/expires_at win; else claimed=lease, open/done/durable=90d, chatter=7d."""
    if "ttl" in data:
        exp = now + parse_duration(data["ttl"])
    elif "expires_at" in data:
        exp = parse_iso(data["expires_at"])
    elif data.get("status") == "claimed":
        return now + s.claim_ttl
    elif durable or data.get("status") in ("open", "done"):
        return now + s.durable_ttl
    else:
        return now + s.chatter_ttl
    if exp <= now:
        raise PostError(400, "bad_data", "expiry is in the past")
    if exp > now + s.max_ttl:
        raise PostError(400, "bad_data", f"expiry beyond max ttl ({s.max_ttl // 86400}d)")
    if data.get("status") == "claimed" and exp > now + s.max_claim_ttl:  # a lease, not a squat
        raise PostError(400, "bad_data", f"a claim's lease is at most {s.max_claim_ttl}s; renew it instead")
    return exp


def escape_text(text: str) -> str:
    return text.replace("\\", "\\\\").replace("\n", "\\n").replace("\t", "\\t")


def dump_data(d: dict) -> str:
    """Compact JSON for the wire: '|' and every unsafe character (see _UNSAFE) as \\uXXXX escapes."""
    out = json.dumps(d, separators=(",", ":"), ensure_ascii=False, allow_nan=False).replace("|", "\\u007c")
    return _UNSAFE_RE.sub(lambda m: f"\\u{ord(m.group()):04x}", out)


@dataclass
class Record:
    """One NDJSON line. `data` is exactly what the client signed; server fields sit beside it."""

    ulid: str
    group: str
    agent_id: str
    profile: str
    text: str
    data: dict = field(default_factory=dict)
    root: str = ""
    exp: int = 0
    pk: str = ""
    sig: str = ""
    sts: int = 0
    via: str = ""
    bid: str = ""  # board id the signature binds

    @property
    def epoch(self) -> float:
        return ulid_ms(self.ulid) / 1000

    @property
    def ts(self) -> str:
        return iso(self.epoch)

    @property
    def parent(self) -> str | None:
        return self.data.get("reply_to")

    @property
    def status(self) -> str | None:
        return self.data.get("status")

    @property
    def renew_target(self) -> str | None:
        """For a renew record, the post it keeps alive (its `exp` is that post's new expiry)."""
        t = self.data.get("renew")
        return t if is_ulid(t) else None

    def display_data(self) -> dict:
        """What readers see: ttl/relative expiry resolved to an absolute expires_at."""
        d = {k: v for k, v in self.data.items() if k != "ttl"}
        if "ttl" in self.data or "expires_at" in self.data or "renew" in self.data:
            d["expires_at"] = iso(self.exp)
        return d

    def line(self) -> str:
        return format_line(self.ulid, self.ts, self.group, self.agent_id, self.profile,
                           self.text, dump_data(self.display_data()))

    def to_json(self) -> str:
        d = {"ulid": self.ulid, "ts": self.ts, "group": self.group, "agent_id": self.agent_id,
             "profile": self.profile, "text": self.text, "data": self.data, "root": self.root, "exp": self.exp,
             "pk": self.pk, "sig": self.sig, "sts": self.sts, "via": self.via, "bid": self.bid}
        return json.dumps(d, separators=(",", ":"), ensure_ascii=False, allow_nan=False)

    @classmethod
    def from_json(cls, line: str) -> "Record":
        d = json.loads(line)
        data = d.get("data") or {}
        if not isinstance(data, dict):
            raise TypeError("data is not an object")
        return cls(ulid=d["ulid"], group=d["group"], agent_id=d["agent_id"], profile=d["profile"],
                   text=d["text"], data=data, root=d.get("root") or d["ulid"],
                   exp=int(d.get("exp") or 0), pk=d.get("pk", ""), sig=d.get("sig", ""),
                   sts=int(d.get("sts") or 0), via=d.get("via", ""), bid=d.get("bid", ""))


def format_line(ulid, ts, group, agent_id, profile, text, data_json) -> str:
    return f"{ulid} | {ts} | {group} | {agent_id}:{profile} | {escape_text(text)} | {data_json}"


def now() -> float:
    return time.time()
