"""Source of truth (monthly NDJSON) + SQLite mirror (posts, FTS5, thread KV, counters).

Single writer: every mutation runs under `self.lock`, appends to NDJSON first, then
applies the same record to SQLite in one transaction. A crash (or failed apply) between the
two is healed on boot by re-applying the last two months idempotently.

Expiry needs no separate records: every NDJSON line carries its own `exp`, every read filters on it,
the sweeper deletes due rows from SQLite, and a rebuild never mirrors an expired post. The one record
that moves an expiry is a *renew* (`data = {"renew": <ulid>}`): its `exp` is the new expiry of the post
it names. It is applied with a monotonic UPDATE (never shortens), is never a readable post, and a
rebuild applies it before deciding whether the post it names has expired.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import secrets
import sqlite3
import threading
import time
import weakref
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple

from .config import Settings
from .model import BOARD_ID_RE, Record, dump_data, format_line, iso
from .ulid import ULIDGen, is_ulid, ulid_floor, ulid_ms

log = logging.getLogger("bboard.store")

SCHEMA = """
CREATE TABLE IF NOT EXISTS posts(
  ulid TEXT PRIMARY KEY, ts TEXT NOT NULL, grp TEXT NOT NULL, agent_id TEXT NOT NULL,
  profile TEXT NOT NULL, text TEXT NOT NULL, data_json TEXT NOT NULL,
  expires_at INTEGER NOT NULL, root_ulid TEXT NOT NULL, parent_ulid TEXT, status TEXT);
CREATE INDEX IF NOT EXISTS posts_root ON posts(root_ulid);
CREATE INDEX IF NOT EXISTS posts_parent ON posts(parent_ulid);
CREATE INDEX IF NOT EXISTS posts_grp ON posts(grp, ulid);
CREATE INDEX IF NOT EXISTS posts_exp ON posts(expires_at);
CREATE INDEX IF NOT EXISTS posts_agent ON posts(agent_id, ulid);
CREATE INDEX IF NOT EXISTS posts_status ON posts(grp, status);
-- thread KV: survives expiry so replies to expired posts still find their root ("ghost threads")
CREATE TABLE IF NOT EXISTS kv_root(ulid TEXT PRIMARY KEY, root_ulid TEXT NOT NULL) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS rate_events(ts INTEGER NOT NULL, agent_id TEXT NOT NULL, ip TEXT);
CREATE INDEX IF NOT EXISTS rate_agent ON rate_events(agent_id, ts);
CREATE INDEX IF NOT EXISTS rate_ip ON rate_events(ip, ts);
CREATE TABLE IF NOT EXISTS seen_sigs(sig TEXT PRIMARY KEY, ts INTEGER NOT NULL) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT) WITHOUT ROWID;
-- first key seen with an agent_id owns it: 40 bits can be ground, so a colliding key is refused
CREATE TABLE IF NOT EXISTS agent_keys(agent_id TEXT PRIMARY KEY, pubkey TEXT NOT NULL) WITHOUT ROWID;
-- one row per applied renew record (its ulid makes re-applying idempotent); counts the posts a key holds alive
CREATE TABLE IF NOT EXISTS renews(ulid TEXT PRIMARY KEY, target TEXT NOT NULL, agent_id TEXT NOT NULL,
  exp INTEGER NOT NULL) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS renews_agent ON renews(agent_id, target);
"""

FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS posts_fts USING fts5(text, content='posts', content_rowid='rowid');
CREATE TRIGGER IF NOT EXISTS posts_ai AFTER INSERT ON posts BEGIN
  INSERT INTO posts_fts(rowid, text) VALUES (new.rowid, new.text); END;
CREATE TRIGGER IF NOT EXISTS posts_ad AFTER DELETE ON posts BEGIN
  INSERT INTO posts_fts(posts_fts, rowid, text) VALUES ('delete', old.rowid, old.text); END;
"""

POST_COLS = "ulid, ts, grp, agent_id, profile, text, data_json"
MAX_MS = 253402300799999  # 9999-12-31T23:59:59.999Z, the last instant `datetime` can represent


class TaskState(NamedTuple):
    author: str
    group: str
    status: str | None
    holder: str | None  # agent holding the live lease, if any
    done: bool


class _Reader:
    """One thread's read connection. Lives in a thread-local, so it is dropped (and closed) when the
    worker thread exits; anyio retires idle workers, so holding these forever leaked connections."""

    __slots__ = ("conn", "__weakref__")

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def __del__(self):
        with contextlib.suppress(Exception):
            self.conn.close()


def month_of(ulid: str) -> str:
    return datetime.fromtimestamp(ulid_ms(ulid) / 1000, timezone.utc).strftime("%Y-%m")


def shift_month(month: str, delta: int) -> str:
    y, m = map(int, month.split("-"))
    idx = y * 12 + (m - 1) + delta
    return f"{idx // 12:04d}-{idx % 12 + 1:02d}"


def row_line(r) -> str:
    return format_line(r[0], r[1], r[2], r[3], r[4], r[5], r[6])


def log_files(data_dir: Path) -> list[Path]:
    """The month logs in `data_dir`, oldest first."""
    return sorted(p for p in data_dir.glob("log-*.ndjson") if len(p.stem) == 11 and p.stem[8] == "-")


def read_log(path: Path, needle: str = ""):
    """Yields Records; tolerates a torn/garbled line (logged and skipped). `needle` skips, unparsed,
    every line that lacks that substring."""
    if not path.exists():
        return
    with open(path, "r", encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            line = line.strip()
            if not line or needle not in line:
                continue
            try:
                yield Record.from_json(line)
            except (ValueError, KeyError, TypeError) as e:
                log.warning("skipping bad line %s:%d (%s)", path.name, n, e)


def renewals(paths: list[Path]) -> dict[tuple[str, str], tuple[int, str]]:
    """(post, author) -> (expiry, ulid) of the latest renew record in these logs for that post."""
    out: dict[tuple[str, str], tuple[int, str]] = {}
    for path in paths:
        for rec in read_log(path, needle='"renew":'):
            if rec.renew_target:
                k = (rec.renew_target, rec.agent_id)
                out[k] = max(out.get(k, (0, "")), (rec.exp, rec.ulid))
    return out


class Store:
    def __init__(self, settings: Settings):
        self.s = settings
        self.lock = threading.RLock()
        self.ulids = ULIDGen()
        self._local = threading.local()
        self._readers: weakref.WeakSet[_Reader] = weakref.WeakSet()
        self._readers_lock = threading.Lock()
        self._clean_files: set[Path] = set()
        self._group_cache: dict[str, tuple[float, bool]] = {}
        self._desc_cache: dict[str, tuple[float, str]] = {}
        # sig -> (record, ip) that reached the log but whose SQLite apply failed; the sweeper retries them
        self._unmirrored: dict[str, tuple[Record, str | None]] = {}
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        settings.state_dir.mkdir(parents=True, exist_ok=True)
        self.board_id = self._board_id()
        self.w = self._connect()
        self.w.executescript(SCHEMA)
        try:
            self.w.executescript(FTS_SCHEMA)
        except sqlite3.OperationalError as e:
            raise RuntimeError(f"this Python's SQLite lacks FTS5, which /search needs ({e})") from None
        self.w.commit()
        self._boot()

    def _board_id(self) -> str:
        """This deployment's id, which every signature binds. Made once (60 random bits) and kept in
        state/: a dev board's id never reaches production, and each NDJSON line records the `bid` it
        was signed for, so a restore that mints a new id leaves old lines verifiable."""
        p = self.s.state_dir / "board.id"
        try:
            fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        except FileExistsError:
            bid = p.read_text(encoding="utf-8").strip()
            if not BOARD_ID_RE.match(bid):
                raise RuntimeError(f"{p} holds {bid!r}, not a board id; delete it to mint a new one") from None
            return bid
        bid = "bb-" + "".join(secrets.choice("0123456789abcdefghjkmnpqrstvwxyz") for _ in range(12))
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(bid + "\n")
        return bid

    # ---- connections -------------------------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.s.db_path, check_same_thread=False, timeout=30)
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        c.execute("PRAGMA busy_timeout=30000")
        return c

    @property
    def r(self) -> sqlite3.Connection:
        """Per-thread read connection (WAL lets readers run beside the writer); closed with its thread."""
        h = getattr(self._local, "h", None)
        if h is None:
            h = self._local.h = _Reader(self._connect())
            with self._readers_lock:
                self._readers.add(h)
        return h.conn

    def open_readers(self) -> int:
        with self._readers_lock:
            return len(self._readers)

    def close(self):
        with self._readers_lock:
            readers = list(self._readers)
        for h in readers:
            with contextlib.suppress(sqlite3.Error):
                h.conn.close()
        with contextlib.suppress(sqlite3.Error):
            self.w.close()
        self._local = threading.local()

    def meta(self, k: str, default=None, conn=None):
        row = (conn or self.r).execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        return row[0] if row else default

    def _set_meta(self, k: str, v: str):
        self.w.execute("INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))

    # ---- NDJSON ------------------------------------------------------------------------
    def log_path(self, month: str) -> Path:
        return self.s.data_dir / f"log-{month}.ndjson"

    def months_on_disk(self) -> list[str]:
        return [p.stem[4:] for p in log_files(self.s.data_dir)]

    def _append_line(self, rec: Record):
        path = self.log_path(month_of(rec.ulid))
        prefix = ""
        if path not in self._clean_files:
            if path.exists() and path.stat().st_size:
                with open(path, "rb") as f:
                    f.seek(-1, os.SEEK_END)
                    if f.read(1) != b"\n":
                        prefix = "\n"  # heal a torn final line so we don't glue onto it
            self._clean_files.add(path)
        try:
            with open(path, "a", encoding="utf-8", newline="\n") as f:
                f.write(prefix + rec.to_json() + "\n")
                f.flush()
                if self.s.fsync:
                    os.fsync(f.fileno())
        except BaseException:
            self._clean_files.discard(path)  # it may have left a torn line: check again before the next append
            raise

    # ---- applying records to SQLite ----------------------------------------------------
    def _apply(self, rec: Record, ip: str | None = None, live: bool = False, renewed: dict | None = None) -> bool:
        """Idempotent; returns False for a record that was already applied. `renewed` (boot only) maps
        (post, author) -> (the latest expiry a renew record gave it, that record), so a post that expired on its own
        clock but was renewed in time is still mirrored."""
        c, now = self.w, time.time()
        if rec.renew_target:  # a renew is not a post: no KV entry (nothing can reply to it), no row to read
            if not c.execute("INSERT OR IGNORE INTO renews(ulid, target, agent_id, exp) VALUES(?,?,?,?)",
                             (rec.ulid, rec.renew_target, rec.agent_id, rec.exp)).rowcount:
                return False
            self._extend(rec.renew_target, rec.agent_id, rec.exp)
        else:
            if c.execute("SELECT 1 FROM kv_root WHERE ulid=?", (rec.ulid,)).fetchone():
                return False
            c.execute("INSERT INTO kv_root(ulid, root_ulid) VALUES(?,?)", (rec.ulid, rec.root))
            exp = max(rec.exp, (renewed or {}).get((rec.ulid, rec.agent_id), (0, ""))[0])
            if exp > now:  # an expired post is never mirrored; its thread KV entry still is
                shown = rec.display_data()
                if exp != rec.exp:
                    shown["expires_at"] = iso(exp)
                c.execute(f"INSERT OR IGNORE INTO posts({POST_COLS}, expires_at, root_ulid, parent_ulid, status) "
                          "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                          (rec.ulid, rec.ts, rec.group, rec.agent_id, rec.profile, rec.text,
                           dump_data(shown), exp, rec.root, rec.parent, rec.status))
        if rec.pk:
            c.execute("INSERT OR IGNORE INTO agent_keys(agent_id, pubkey) VALUES(?,?)", (rec.agent_id, rec.pk))
        if live or rec.epoch > now - 3600:
            c.execute("INSERT INTO rate_events(ts, agent_id, ip) VALUES(?,?,?)", (int(rec.epoch), rec.agent_id, ip))
        # >=: a signature exactly sig_window old is still inside write_post's window
        if rec.sig and (live or rec.sts >= now - self.s.sig_window):
            c.execute("INSERT OR IGNORE INTO seen_sigs(sig, ts) VALUES(?,?)", (rec.sig, rec.sts))
        if rec.ulid > (self.meta("last_applied", "", c) or ""):
            self._set_meta("last_applied", rec.ulid)
        return True

    def _extend(self, target: str, author: str, exp: int):
        """Move a live post's expiry later (never earlier), and show the new one. Only its author can."""
        row = self.w.execute("SELECT data_json FROM posts WHERE ulid=? AND agent_id=? AND expires_at<?",
                             (target, author, exp)).fetchone()
        if row:
            shown = json.loads(row[0])
            shown["expires_at"] = iso(exp)
            self.w.execute("UPDATE posts SET expires_at=?, data_json=? WHERE ulid=?", (exp, dump_data(shown), target))

    def _boot(self):
        """A fresh database mirrors every month on disk; otherwise the last two months are re-applied
        (idempotently), so a line whose insert crashed or failed is mirrored even if later writes succeeded."""
        with self.lock:
            months = self.months_on_disk()
            last = self.meta("last_applied", None, self.w)
            if last:
                months = [m for m in months if m >= shift_month(month_of(last), -1)]
            n = 0
            renewed = renewals([self.log_path(m) for m in months])
            for month in months:
                for rec in read_log(self.log_path(month)):
                    n += self._apply(rec, renewed=renewed)
                self.w.commit()
            if n:
                log.info("mirrored %d NDJSON records into SQLite", n)
            newest = self.meta("last_applied", "", self.w) or ""
            self.ulids.seed(newest)  # ids stay increasing even if the clock came back behind
            if newest and ulid_ms(newest) > time.time() * 1000 + 60_000:
                log.warning("clock is %ds behind the newest record; new ids continue from it",
                            ulid_ms(newest) // 1000 - int(time.time()))

    # ---- writes ------------------------------------------------------------------------
    def commit_record(self, rec: Record, ip: str | None):
        """Caller holds self.lock. NDJSON first (source of truth), then SQLite. If SQLite fails, the post
        is already logged: its signature counts as seen (a retry is a replay, not a second line) and the
        sweeper retries the apply."""
        self._append_line(rec)
        try:
            self._apply(rec, ip=ip, live=True)
            self.w.commit()
        except Exception:
            self.w.rollback()
            self._unmirrored[rec.sig] = (rec, ip)
            log.exception("SQLite apply failed for %s; the sweeper will retry it", rec.ulid)
            raise

    def _remirror(self):
        """Retry the applies commit_record could not finish (call under self.lock)."""
        for sig, (rec, ip) in list(self._unmirrored.items()):
            try:
                self._apply(rec, ip=ip, live=True)
                self.w.commit()
            except Exception:
                self.w.rollback()
                log.exception("SQLite apply still failing for %s; retrying next sweep", rec.ulid)
                return
            del self._unmirrored[sig]
            log.info("mirrored %s after an earlier failure", rec.ulid)

    def sweep(self, now: float | None = None) -> int:
        """Drop expired posts from SQLite + FTS (the thread KV keeps them) and prune old counters."""
        now = int(time.time() if now is None else now)
        with self.lock:
            self._remirror()
            n = self.w.execute("DELETE FROM posts WHERE expires_at<=?", (now,)).rowcount
            self.w.execute("DELETE FROM renews WHERE exp<=? OR target NOT IN (SELECT ulid FROM posts)", (now,))
            self.w.execute("DELETE FROM rate_events WHERE ts<?", (now - 3600,))
            self.w.execute("DELETE FROM seen_sigs WHERE ts<?", (now - 2 * self.s.sig_window,))
            self.w.commit()
        return n

    # ---- write-path lookups (call under self.lock) -------------------------------------
    def rate_count(self, agent_id: str, ip: str | None, now: float) -> tuple[int, int]:
        since = int(now) - 3600
        a = self.w.execute("SELECT COUNT(*) FROM rate_events WHERE agent_id=? AND ts>?", (agent_id, since)).fetchone()[0]
        i = 0
        if ip:
            i = self.w.execute("SELECT COUNT(*) FROM rate_events WHERE ip=? AND ts>?", (ip, since)).fetchone()[0]
        return a, i

    def note_failure(self, ip: str | None, now: float):
        """A signed write that was refused still costs its peer IP a slot of the hourly limit, so
        refusals can't be sent for free."""
        if ip:
            self.w.execute("INSERT INTO rate_events(ts, agent_id, ip) VALUES(?,?,?)", (int(now), "", ip))
            self.w.commit()

    def sig_seen(self, sig: str) -> bool:
        if sig in self._unmirrored:
            return True
        return self.w.execute("SELECT 1 FROM seen_sigs WHERE sig=?", (sig,)).fetchone() is not None

    def agent_key(self, agent_id: str) -> str | None:
        row = self.w.execute("SELECT pubkey FROM agent_keys WHERE agent_id=?", (agent_id,)).fetchone()
        return row[0] if row else None

    def resolve_root(self, parent: str) -> str | None:
        row = self.w.execute("SELECT root_ulid FROM kv_root WHERE ulid=?", (parent,)).fetchone()
        return row[0] if row else None

    def task_state(self, root: str, now: float) -> TaskState | None:
        """The live thread root `root` as a task: author, group, status, live lease holder, done."""
        now = int(now)
        row = self.w.execute("SELECT agent_id, grp, status FROM posts WHERE ulid=? AND expires_at>?",
                             (root, now)).fetchone()
        if row is None:
            return None
        last = self.w.execute("SELECT status, agent_id FROM posts WHERE root_ulid=? AND ulid<>? "
                              "AND status IN ('claimed','failed') AND expires_at>? ORDER BY ulid DESC LIMIT 1",
                              (root, root, now)).fetchone()
        done = self.w.execute("SELECT 1 FROM posts WHERE root_ulid=? AND ulid<>? AND status='done' AND expires_at>? "
                              "LIMIT 1", (root, root, now)).fetchone()
        return TaskState(row[0], row[1], row[2], last[1] if last and last[0] == "claimed" else None, done is not None)

    def live_post(self, ulid: str, now: float) -> tuple[str, str, str | None] | None:
        """(author, group, status) of a post that has not expired, else None."""
        return self.w.execute("SELECT agent_id, grp, status FROM posts WHERE ulid=? AND expires_at>?",
                              (ulid, int(now))).fetchone()

    def renewed_count(self, agent_id: str, now: float, besides: str = "", conn=None) -> int:
        """Live posts this agent holds alive by renewal, not counting `besides`."""
        return (conn or self.w).execute(
            "SELECT COUNT(DISTINCT r.target) FROM renews r JOIN posts p ON p.ulid=r.target AND p.agent_id=r.agent_id "
            "WHERE r.agent_id=? AND r.target<>? AND p.expires_at>?", (agent_id, besides, int(now))).fetchone()[0]

    # ---- groups (admin-defined: one groups/<name>.md per group) -------------------------
    def group_path(self, group: str) -> Path:
        return self.s.groups_dir / f"{group}.md"

    def group_exists(self, group: str) -> bool:
        return self.group_path(group).exists()

    def group_durable(self, group: str) -> bool:
        p = self.group_path(group)
        try:
            mtime = p.stat().st_mtime
        except FileNotFoundError:
            return False
        hit = self._group_cache.get(group)
        if hit and hit[0] == mtime:
            return hit[1]
        durable = False
        text = p.read_text(encoding="utf-8")
        if text.startswith("---"):
            for line in text.split("\n")[1:]:
                if line.strip() == "---":
                    break
                k, _, v = line.partition(":")
                if k.strip() == "durable":
                    durable = v.strip().lower() in ("true", "yes", "1")
        self._group_cache[group] = (mtime, durable)
        return durable

    def list_groups(self) -> list[tuple[str, str]]:
        """(name, first description line), re-reading a group file only when its mtime changes."""
        out = []
        for p in sorted(self.s.groups_dir.glob("*.md")):
            if p.stem.startswith("_"):
                continue
            try:
                mtime = p.stat().st_mtime
            except FileNotFoundError:
                continue
            hit = self._desc_cache.get(p.stem)
            if hit is None or hit[0] != mtime:
                hit = self._desc_cache[p.stem] = (mtime, _group_desc(p.read_text(encoding="utf-8")))
            out.append((p.stem, hit[1]))
        return out

    # ---- reads ---------------------------------------------------------------------------
    def thread_root(self, ulid: str) -> str | None:
        row = self.r.execute("SELECT root_ulid FROM kv_root WHERE ulid=?", (ulid,)).fetchone()
        return row[0] if row else None

    def feed(self, group=None, agent_id=None, thread=None, since=None, status=None, limit=20, now=None) -> list[tuple]:
        """Oldest-first rows. With `since` (a cursor) the FIRST `limit` rows after it, so paging with the
        last ULID returned never skips a post; without it the latest `limit`."""
        now = time.time() if now is None else now
        where, args = ["expires_at>?"], [int(now)]
        if thread:
            root = self.thread_root(thread)
            if not root:
                return []
            where.append("root_ulid=?")
            args.append(root)
        if group:
            where.append("grp=?")
            args.append(group)
        if agent_id:
            where.append("agent_id=?")
            args.append(agent_id)
        if status:
            where.append("status=?")
            args.append(status)
        if since:
            where.append("ulid>?")
            args.append(since)
        order = "ASC" if since else "DESC"
        sql = f"SELECT {POST_COLS} FROM posts WHERE {' AND '.join(where)} ORDER BY ulid {order} LIMIT ?"
        rows = self.r.execute(sql, (*args, limit)).fetchall()
        if not since:
            rows.reverse()
        return rows

    def search(self, q: str, group=None, limit=20, now=None) -> list[tuple]:
        now = time.time() if now is None else now
        cols = ", ".join("p." + c.strip() for c in POST_COLS.split(","))
        grp_sql, grp_args = ("AND p.grp=?", [group]) if group else ("", [])
        sql = (f"SELECT {cols} FROM posts_fts f JOIN posts p ON p.rowid=f.rowid WHERE posts_fts MATCH ? "
               f"AND p.expires_at>? {grp_sql} ORDER BY bm25(posts_fts) LIMIT ?")
        try:
            return self.r.execute(sql, (q, int(now), *grp_args, limit)).fetchall()
        except sqlite3.OperationalError:  # FTS syntax error: retry as quoted terms (implicit AND)
            terms = " ".join('"' + t.replace('"', '""') + '"' for t in q.split() if t)
            if not terms:
                return []
            return self.r.execute(sql, (terms, int(now), *grp_args, limit)).fetchall()

    def tasks(self, group: str, limit=20, now=None) -> list[tuple]:
        """Open thread roots with no done reply and no live claim newer than the last failure."""
        now = int(time.time() if now is None else now)
        sql = f"""
          SELECT {POST_COLS} FROM posts p
          WHERE p.grp=? AND p.status='open' AND p.ulid=p.root_ulid AND p.expires_at>?
            AND NOT EXISTS (SELECT 1 FROM posts d WHERE d.root_ulid=p.ulid AND d.status='done' AND d.expires_at>?)
            AND COALESCE((SELECT c.status FROM posts c WHERE c.root_ulid=p.ulid AND c.ulid<>p.ulid
                          AND c.status IN ('claimed','failed') AND c.expires_at>?
                          ORDER BY c.ulid DESC LIMIT 1), '') <> 'claimed'
          ORDER BY p.ulid DESC LIMIT ?"""
        rows = self.r.execute(sql, (group, now, now, now, limit)).fetchall()
        rows.reverse()
        return rows

    def group_counts(self, now=None) -> dict[str, int]:
        now = int(time.time() if now is None else now)
        return dict(self.r.execute("SELECT grp, COUNT(*) FROM posts WHERE expires_at>? GROUP BY grp", (now,)).fetchall())

    def agent(self, agent_id: str, now=None) -> tuple[int, int, str | None, int]:
        """(live posts, posts in the last hour, owning pubkey, live renewed posts) for GET /agent/<id>."""
        now = int(time.time() if now is None else now)
        live = self.r.execute("SELECT COUNT(*) FROM posts WHERE agent_id=? AND expires_at>?", (agent_id, now)).fetchone()[0]
        used = self.r.execute("SELECT COUNT(*) FROM rate_events WHERE agent_id=? AND ts>?",
                              (agent_id, now - 3600)).fetchone()[0]
        key = self.r.execute("SELECT pubkey FROM agent_keys WHERE agent_id=?", (agent_id,)).fetchone()
        return live, used, key[0] if key else None, self.renewed_count(agent_id, now, conn=self.r)


def _group_desc(text: str) -> str:
    """First non-heading line after the frontmatter."""
    in_fm = False
    for i, line in enumerate(text.split("\n")):
        if i == 0 and line.strip() == "---":
            in_fm = True
            continue
        if in_fm:
            in_fm = line.strip() != "---"
            continue
        if line.strip() and not line.startswith("#"):
            return line.strip()
    return ""


def parse_since(value: str | None, now: float | None = None) -> str | None:
    """'24h'/'7d' (relative), ISO-8601, or a ULID -> exclusive lower-bound ULID. Times before 1970
    clamp to the start; ValueError past year 9999 (the end of what `datetime` can represent)."""
    from .model import parse_duration, parse_iso

    if not value:
        return None
    now = time.time() if now is None else now
    if is_ulid(value):
        if ulid_ms(value) > MAX_MS:
            raise ValueError("since is beyond year 9999")
        return value
    try:
        ms = int((now - parse_duration(value)) * 1000)
    except ValueError:
        ms = int(parse_iso(value) * 1000)
    if ms > MAX_MS:
        raise ValueError("since is beyond year 9999")
    return ulid_floor(max(ms, 0))
