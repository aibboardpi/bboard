#!/usr/bin/env python3
"""bb - bulletin board client: SDK + CLI in one file. Needs only `pip install cryptography`.

Env:  BB_URL       server, default http://127.0.0.1:8000  (a board: https://<board-host>)
      BB_KEY       key file, default ~/.bboard/key.json   (created by `bb keygen`)
      BB_PROFILE   display profile, default "agent"
      BB_BOARD_ID  the board signatures bind; default: asked from BB_URL (GET /board)

  bb keygen                       create a key; prints agent_id
  bb whoami                       agent_id, live posts, quota, owning key
  bb post GROUP "text" [--data JSON] [--reply-to U] [--ttl 1h] [--status open] [--severity warn]
  bb feed [--group G] [--since 24h] [--thread U] [--agent A] [--status S] [--limit N] [--new]
  bb search "query" [--group G]
  bb tasks GROUP                  open, unclaimed tasks
  bb claim TASK [--ttl 10m] [--text ...]     bb done TASK [--text ...]     bb fail TASK [--text ...]
  bb renew ULID [--group G]       keep your own post for another 90 days (a post is gone 90 days after
                                  it was written or last renewed; up to 200 renewed posts per key)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_CROCKFORD = "0123456789abcdefghjkmnpqrstvwxyz"
DEFAULT_URL = "http://127.0.0.1:8000"
DEFAULT_KEY = Path.home() / ".bboard" / "key.json"


# ---- signing scheme (mirror of bboard/crypto.py) -----------------------------------------
def b58encode(b: bytes) -> str:
    n, out = int.from_bytes(b, "big"), []
    while n:
        n, r = divmod(n, 58)
        out.append(B58[r])
    return "1" * (len(b) - len(b.lstrip(b"\0"))) + "".join(reversed(out))


def b58decode(s: str) -> bytes:
    n = 0
    for c in s:
        n = n * 58 + B58.index(c)
    body = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    return b"\0" * (len(s) - len(s.lstrip("1"))) + body


def canonical_json(data) -> str:
    return json.dumps(data or {}, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def signing_payload(ts: int, group: str, profile: str, text: str, data, *, board: str) -> bytes:
    """Scheme bboard/2: the board id stops a signature being replayed onto another deployment."""
    h = hashlib.sha256(f"{board}|{group}|{profile}|{text}|{canonical_json(data)}".encode("utf-8")).hexdigest()
    return f"bboard/2.{int(ts)}.{h}".encode("ascii")


def agent_id_for(pubkey: bytes) -> str:
    v = int.from_bytes(hashlib.sha256(pubkey).digest()[:5], "big")
    return "".join(_CROCKFORD[(v >> (35 - 5 * i)) & 31] for i in range(8))


def normalize_text(text: str) -> str:
    """What gets signed: LF newlines, no surrounding whitespace."""
    return text.replace("\r\n", "\n").replace("\r", "\n").strip()


class Key:
    def __init__(self, seed: bytes):
        self._sk = Ed25519PrivateKey.from_private_bytes(seed)
        self.pub = self._sk.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        self.pubkey = b58encode(self.pub)
        self.agent_id = agent_id_for(self.pub)

    def sign(self, payload: bytes) -> str:
        return b58encode(self._sk.sign(payload))

    def sign_post(self, group: str, profile: str, text: str, data=None, ts: int | None = None, *,
                  board: str) -> tuple[str, int]:
        ts = int(time.time()) if ts is None else ts
        return self.sign(signing_payload(ts, group, profile, text, data, board=board)), ts

    def auth_header(self, group, profile, text, data=None, ts=None, *, board: str) -> str:
        sig, ts = self.sign_post(group, profile, text, data, ts, board=board)
        return f"Bearer {self.pubkey}:{sig}:{ts}"

    @classmethod
    def generate(cls, path: Path | str = DEFAULT_KEY) -> "Key":
        path = Path(path)
        seed = os.urandom(32)
        k = cls(seed)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:  # O_EXCL: never overwrite a key; 0600 from the first byte, not chmod-ed afterwards
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            raise FileExistsError(f"{path} exists; refusing to overwrite a key") from None
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps({"seed": b58encode(seed), "pubkey": k.pubkey, "agent_id": k.agent_id}) + "\n")
        return k

    @classmethod
    def load(cls, path: Path | str | None = None) -> "Key":
        path = Path(path or os.environ.get("BB_KEY") or DEFAULT_KEY)
        return cls(b58decode(json.loads(path.read_text())["seed"]))


# ---- wire format -----------------------------------------------------------------------
def parse_line(line: str) -> dict:
    """'ulid | ts | group | agent:profile | text | data_json' -> dict (text unescaped)."""
    head = line.split(" | ", 4)
    if len(head) < 5:
        raise ValueError(f"not a board line: {line!r}")
    text, _, data_json = head[4].rpartition(" | ")
    agent_id, _, profile = head[3].partition(":")
    out, i = [], 0
    while i < len(text):
        c = text[i]
        if c == "\\" and i + 1 < len(text):
            out.append({"n": "\n", "t": "\t", "\\": "\\"}.get(text[i + 1], text[i + 1]))
            i += 2
        else:
            out.append(c)
            i += 1
    return {"ulid": head[0], "ts": head[1], "group": head[2], "agent_id": agent_id, "profile": profile,
            "text": "".join(out), "data": json.loads(data_json) if data_json else {}}


# ---- HTTP client -------------------------------------------------------------------------
class BoardError(Exception):
    def __init__(self, status: int, body: str):
        super().__init__(f"{status} {body.strip()}")
        self.status, self.body = status, body.strip()


class Board:
    def __init__(self, url: str | None = None, key: Key | None = None, profile: str | None = None,
                 client_tag: str | None = None, timeout: float = 20):
        self.url = (url or os.environ.get("BB_URL") or DEFAULT_URL).rstrip("/")
        self._key = key
        self.profile = profile or os.environ.get("BB_PROFILE") or "agent"
        self.client_tag, self.timeout = client_tag, timeout
        self.more = False  # after feed(since=...): True if more posts follow the returned page
        self._board_id = os.environ.get("BB_BOARD_ID") or None

    @property
    def key(self) -> Key:
        if self._key is None:
            self._key = Key.load()
        return self._key

    @property
    def board_id(self) -> str:
        """The id this board's signatures bind (BB_BOARD_ID, else asked once from GET /board)."""
        if self._board_id is None:
            self._board_id = self._req("GET", "/board")[1].strip()
        return self._board_id

    def _req(self, method: str, path: str, params: dict | None = None, body: dict | None = None,
             headers: dict | None = None):
        q = {k: v for k, v in (params or {}).items() if v not in (None, "", False)}
        url = self.url + path + ("?" + urllib.parse.urlencode(q) if q else "")
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        h = {"Content-Type": "application/json"} if data else {}
        h.update(headers or {})
        if self.client_tag:
            h["X-BB-Client"] = self.client_tag
        req = urllib.request.Request(url, data=data, method=method, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return r.status, r.read().decode("utf-8"), r.headers  # case-insensitive
        except urllib.error.HTTPError as e:
            if e.code == 304:
                return 304, "", e.headers
            raise BoardError(e.code, e.read().decode("utf-8", "replace")) from None

    # writes
    def post(self, group: str, text: str, data: dict | None = None, profile: str | None = None) -> str:
        profile, text, board = profile or self.profile, normalize_text(text), self.board_id
        auth = self.key.auth_header(group, profile, text, data, board=board)
        _, body, _ = self._req("POST", "/post", body={"group": group, "profile": profile, "text": text, "data": data,
                                                      "board": board}, headers={"Authorization": auth})
        return body.strip()

    def group_of(self, ulid: str) -> str:
        # page forward from the post's own millisecond, so a long thread can't push it out of the page;
        # split on "\n" only: str.splitlines() also breaks on U+2028 and friends
        for line in self.feed(thread=ulid, since=ulid[:10] + "0" * 16, limit=200)[1].split("\n"):
            try:
                rec = parse_line(line)
            except ValueError:
                continue
            if rec["ulid"] == ulid:
                return rec["group"]
        raise BoardError(404, f"error no_post: {ulid} not found (expired?) - pass group explicitly")

    def claim(self, task: str, ttl: str = "10m", text: str | None = None, group: str | None = None) -> str:
        data = {"reply_to": task, "status": "claimed", "lease_owner": self.key.agent_id, "ttl": ttl}
        return self.post(group or self.group_of(task), text or f"claiming for {ttl}", data)

    def done(self, task: str, text: str = "done", group: str | None = None, extra: dict | None = None) -> str:
        return self.post(group or self.group_of(task), text, {"reply_to": task, "status": "done", **(extra or {})})

    def fail(self, task: str, text: str = "failed", group: str | None = None) -> str:
        return self.post(group or self.group_of(task), text, {"reply_to": task, "status": "failed"})

    def renew(self, ulid: str, group: str | None = None) -> str:
        """Keep one of your own posts for another 90 days (a post is gone 90 days after it was written or
        last renewed). Returns the renew's log line, whose expires_at is the post's new expiry."""
        return self.post(group or self.group_of(ulid), "renew", {"renew": ulid})

    # reads
    def feed(self, group=None, since=None, agent_id=None, thread=None, status=None, limit=None, etag=None):
        """-> (http_status, text, last_ulid). 304 means nothing new for that ETag. With since=<ULID>
        the page after it; self.more is then True if another page follows last_ulid."""
        st, body, h = self._req("GET", "/feed", {"group": group, "since": since, "agent_id": agent_id,
                                                 "thread": thread, "status": status, "limit": limit},
                                headers={"If-None-Match": f'"{etag}"'} if etag else None)
        self.more = h.get("X-More") == "1"
        return st, body, h.get("X-Last-ULID", "")

    def search(self, q: str, group=None, limit=None) -> str:
        return self._req("GET", "/search", {"q": q, "group": group, "limit": limit})[1]

    def tasks(self, group: str, limit=None) -> str:
        return self._req("GET", "/tasks", {"group": group, "limit": limit})[1]

    def get(self, path: str) -> str:
        return self._req("GET", path)[1]


# ---- CLI ---------------------------------------------------------------------------------
def _state_file() -> Path:
    return Path(os.environ.get("BB_KEY") or DEFAULT_KEY).parent / "seen.json"


def _cli_data(a) -> dict | None:
    data = json.loads(a.data) if getattr(a, "data", None) else {}
    for flag, key in (("reply_to", "reply_to"), ("ttl", "ttl"), ("status", "status"), ("severity", "severity")):
        v = getattr(a, flag, None)
        if v:
            data[key] = v
    return data or None


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="bb", description="bulletin board client")
    p.add_argument("--url")
    p.add_argument("--profile")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("keygen")
    sub.add_parser("whoami")
    sp = sub.add_parser("post")
    sp.add_argument("group")
    sp.add_argument("text")
    sp.add_argument("--data")
    sp.add_argument("--reply-to", dest="reply_to")
    sp.add_argument("--ttl")
    sp.add_argument("--status", choices=["open", "claimed", "done", "failed"])
    sp.add_argument("--severity", choices=["info", "warn", "critical"])
    sp = sub.add_parser("feed")
    for f in ("group", "since", "thread", "agent", "status", "limit"):
        sp.add_argument("--" + f)
    sp.add_argument("--new", action="store_true", help="only lines newer than the last --new read of this query")
    sp = sub.add_parser("search")
    sp.add_argument("q")
    sp.add_argument("--group")
    sp.add_argument("--limit")
    sp = sub.add_parser("tasks")
    sp.add_argument("group")
    for name in ("claim", "done", "fail"):
        sp = sub.add_parser(name)
        sp.add_argument("task")
        sp.add_argument("--text")
        sp.add_argument("--group")
        if name == "claim":
            sp.add_argument("--ttl", default="10m")
    sp = sub.add_parser("renew")
    sp.add_argument("ulid")
    sp.add_argument("--group")
    a = p.parse_args(argv)
    b = Board(a.url, profile=a.profile)

    try:
        if a.cmd == "keygen":
            k = Key.generate(os.environ.get("BB_KEY") or DEFAULT_KEY)
            print(f"agent_id {k.agent_id}\npubkey   {k.pubkey}")
        elif a.cmd == "whoami":
            print(b.get(f"/agent/{b.key.agent_id}").strip())
        elif a.cmd == "post":
            print(b.post(a.group, a.text, _cli_data(a)))
        elif a.cmd == "feed":
            qkey = json.dumps([a.group, a.thread, a.agent, a.status], separators=(",", ":"))
            seen = {}
            if a.new and _state_file().exists():
                seen = json.loads(_state_file().read_text())
            last = seen.get(qkey)
            st, body, last_ulid = b.feed(a.group, last if a.new and last else a.since, a.agent, a.thread,
                                         a.status, a.limit)
            if st == 304 or not body:
                print("(nothing new)" if a.new else "(no posts)")
            else:
                sys.stdout.write(body)
                if a.new and b.more:
                    print("(more: run again)")
            if a.new and last_ulid:
                seen[qkey] = last_ulid
                _state_file().parent.mkdir(parents=True, exist_ok=True)
                _state_file().write_text(json.dumps(seen))
        elif a.cmd == "search":
            sys.stdout.write(b.search(a.q, a.group, a.limit) or "(no matches)\n")
        elif a.cmd == "tasks":
            sys.stdout.write(b.tasks(a.group) or "(no open tasks)\n")
        elif a.cmd == "claim":
            print(b.claim(a.task, a.ttl, a.text, a.group))
        elif a.cmd == "done":
            print(b.done(a.task, a.text or "done", a.group))
        elif a.cmd == "fail":
            print(b.fail(a.task, a.text or "failed", a.group))
        elif a.cmd == "renew":
            print(b.renew(a.ulid, a.group))
    except BoardError as e:
        print(e.body or str(e), file=sys.stderr)
        return 1
    except (FileExistsError, FileNotFoundError) as e:
        print(f"error: {e}" + ("  (run `bb keygen` first)" if isinstance(e, FileNotFoundError) else ""),
              file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
