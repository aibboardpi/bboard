"""Security regressions: each test fails on the code as it was reviewed.
Numbers match the review's findings (1-16 high/medium, L1-L17 low)."""

import asyncio
import gc
import json
import os
import sqlite3
import threading
import time
from datetime import datetime, timezone

import bb
import pytest
from conftest import board_of, make_site, new_key, ok, send
from fastapi.responses import PlainTextResponse
from fastapi.testclient import TestClient

from bboard.app import Guard, RequestLimiter, create_app, peer_key
from bboard.config import Settings
from bboard.crypto import decode_pubkey, signing_payload, verify
from bboard.model import AGENT_ID_RE, DURATION_RE, GROUP_RE, PROFILE_RE, PostError, Record
from bboard.store import Store, month_of
from bboard.ulid import ULIDGen, is_ulid
from bboard.writer import write_post


def ulids(text: str) -> list[str]:
    return [bb.parse_line(x)["ulid"] for x in text.split("\n") if x]


def direct(st, s, k, group, text, data=None, ip="100.64.0.1", now=None, profile="p"):
    now = time.time() if now is None else now
    sig = k.sign(signing_payload(int(now), group, profile, text, data, board=st.board_id))
    return write_post(st, s, pubkey=k.pubkey, sig=sig, ts=int(now), group=group, profile=profile, text=text,
                      data=data, ip=ip, now=now)


@pytest.fixture
def strict(tmp_path):
    """Production rate limits (the shared fixtures are permissive)."""
    s = Settings(root=make_site(tmp_path / "strict"), fsync=False)
    st = Store(s)
    yield s, st
    st.close()


# ---- 1: one bad post must never wedge anything ------------------------------------------
@pytest.mark.parametrize("bad", [1, [1], {"a": 1}, True])
def test_non_string_reply_to_is_a_400(client, key, bad):
    r = send(client, key, "general", "x", {"reply_to": bad})
    assert r.status_code == 400 and "reply_to" in r.text


def test_deep_or_nested_json_is_a_400(client, key):
    body = b'{"group":"general","profile":"p","text":"x","data":' + b"[" * 8000 + b"}"
    r = client.post("/post", content=body[:8192], headers={"Authorization": key.auth_header("general", "p", "x", board=board_of(client))})
    assert r.status_code == 400 and "bad_json" in r.text
    nested = cur = {}
    for _ in range(9):
        cur["a"] = cur = {}
    r = send(client, key, "general", "deep", nested)
    assert r.status_code == 400 and "nests" in r.text


# ---- 2: nothing can put a second line into the feed ---------------------------------------
def test_trailing_newline_never_passes_a_field_regex(client, key):
    assert not any((GROUP_RE.match("general\n"), PROFILE_RE.match("admin\n"), AGENT_ID_RE.match("abcd1234\n"),
                    DURATION_RE.match("10m\n"), DURATION_RE.match("١٠m"),
                    is_ulid("01J0000000000000000000000A\n"), is_ulid(1)))
    assert send(client, key, "general", "hi", profile="admin\n").status_code == 400
    assert client.get("/feed?group=general%0A").status_code == 400


@pytest.mark.parametrize("ch", [" ", " ", "\x85", "‮", "⁦"])
def test_line_separators_and_bidi_controls_are_rejected_in_text(client, key, ch):
    r = send(client, key, "general", f"fine{ch}01K6ZZZZZZZZZZZZZZZZZZZZZZ | forged")
    assert r.status_code == 400 and "bad_text" in r.text


def test_unsafe_characters_in_data_are_escaped_on_the_wire(client, key):
    data = {"note": "a b\x85c‮d|e"}
    ok(client, key, "general", "t", data)
    body = client.get("/feed").text
    assert len(body.splitlines()) == 1 and "\\u2028" in body and "\\u0085" in body and "\\u202e" in body
    assert bb.parse_line(body.strip())["data"] == data


# ---- 3: swarm coordination is authorized ------------------------------------------------
def test_only_the_lease_holder_or_author_closes_a_task(client, key):
    owner, worker, griefer = key, new_key(), new_key()
    t = ok(client, owner, "tasks", "real task", {"status": "open"})
    ok(client, worker, "tasks", "mine", {"reply_to": t, "status": "claimed", "lease_owner": worker.agent_id, "ttl": "1h"})
    r = send(client, griefer, "tasks", "lol", {"reply_to": t, "status": "failed"})
    assert r.status_code == 403 and "not_lease_holder" in r.text
    r = send(client, griefer, "tasks", "mine now", {"reply_to": t, "status": "claimed", "lease_owner": griefer.agent_id})
    assert r.status_code == 409 and "task_claimed" in r.text
    assert send(client, griefer, "tasks", "done", {"reply_to": t, "status": "done"}).status_code == 403
    r = send(client, worker, "general", "done", {"reply_to": t, "status": "done"})
    assert r.status_code == 400 and "wrong_group" in r.text
    ok(client, worker, "tasks", "done", {"reply_to": t, "status": "done"})
    r = send(client, owner, "tasks", "done again", {"reply_to": t, "status": "done"})
    assert r.status_code == 409 and "task_done" in r.text


def test_claims_are_bounded_leases_on_real_tasks(client, key):
    t = ok(client, key, "tasks", "task", {"status": "open"})
    w = new_key()
    r = send(client, w, "tasks", "squat", {"reply_to": t, "status": "claimed", "lease_owner": w.agent_id, "ttl": "30d"})
    assert r.status_code == 400 and "lease" in r.text
    chatter = ok(client, key, "tasks", "not a task")
    r = send(client, w, "tasks", "claim", {"reply_to": chatter, "status": "claimed", "lease_owner": w.agent_id})
    assert r.status_code == 400 and "not_a_task" in r.text


# ---- 4: reading with a cursor never skips -------------------------------------------------
def test_cursor_reads_page_forward_without_gaps(client, key):
    first = ok(client, key, "general", "cursor")
    posted = [ok(client, key, "general", f"n{i}") for i in range(30)]
    got, cursor, pages = [], first, 0
    while True:
        r = client.get(f"/feed?since={cursor}&limit=20")
        got += ulids(r.text)
        pages += 1
        if r.headers.get("X-More") != "1":
            break
        cursor = r.headers["X-Last-ULID"]
    assert got == posted and pages == 2


def test_clients_page_forward_without_gaps(live):
    b = bb.Board(live.url, key=new_key())
    first = bb.parse_line(b.post("general", "cursor"))["ulid"]
    posted = [bb.parse_line(b.post("general", f"n{i}"))["ulid"] for i in range(5)]
    _, body, last = b.feed(group="general", since=first, limit=3)
    assert ulids(body) == posted[:3] and b.more and last == posted[2]
    assert ulids(b.feed(group="general", since=last, limit=3)[1]) == posted[3:] and not b.more


# ---- 7: an agent_id can't be taken over by a colliding key ---------------------------------
def test_an_agent_id_belongs_to_its_first_key(client, key, store):
    ok(client, key, "general", "first")
    assert key.pubkey in client.get(f"/agent/{key.agent_id}").text
    impostor = new_key()  # stand-in for a ground 40-bit collision: its id is already bound to another key
    store.w.execute("INSERT INTO agent_keys(agent_id, pubkey) VALUES(?,?)", (impostor.agent_id, key.pubkey))
    store.w.commit()
    r = send(client, impostor, "general", "it's me")
    assert r.status_code == 409 and "agent_id_taken" in r.text


# ---- 8, L1: hostile `since` -----------------------------------------------------------------
def test_since_is_range_checked_and_reaches_all_history(tmp_path):
    s = Settings(root=make_site(tmp_path / "pub"), fsync=False)
    g = ULIDGen()
    u = g.new(int(datetime(2024, 3, 1, tzinfo=timezone.utc).timestamp() * 1000))
    s.data_dir.mkdir(parents=True)
    path = s.data_dir / f"log-{month_of(u)}.ndjson"
    path.write_text(Record(ulid=u, group="general", agent_id="abcd1234", profile="p", text="ancient", root=u,
                           exp=int(time.time()) + 86400).to_json() + "\n", encoding="utf-8")
    st = Store(s)
    try:
        c = TestClient(create_app(st, s))
        assert c.get("/feed?since=7ZZZZZZZZZZZZZZZZZZZZZZZZZ").status_code == 400
        for since in ("2024-01-01T00:00:00Z", "9999999w", "0001-01-01T00:00:00Z", "00000000000000000000000000"):
            assert "ancient" in c.get(f"/feed?since={since}").text, since  # a live post, however old
    finally:
        st.close()


# ---- 9: ids never go backwards across a restart ---------------------------------------------
def test_ids_keep_increasing_when_the_clock_comes_back_behind(settings, key):
    st = Store(settings)
    T = time.time()
    a = direct(st, settings, key, "general", "before", now=T).record.ulid
    st.close()
    st = Store(settings)
    try:
        b = direct(st, settings, key, "general", "after", now=T - 120).record.ulid
        assert b > a and [r[0] for r in st.feed(since=a)] == [b]
    finally:
        st.close()


# ---- 10: refused writes aren't free ---------------------------------------------------------
def test_refused_signed_writes_cost_the_peer_ip(strict):
    s, st = strict
    k, g, codes = new_key(), ULIDGen(), []
    for i in range(s.posts_per_hour + 2):
        try:
            direct(st, s, k, "general", f"r{i}", {"reply_to": g.new()})
            codes.append(201)
        except PostError as e:
            codes.append(e.status)
    assert codes == [410] * s.posts_per_hour + [429, 429]
    assert st.resolve_root("7ZZZZZZZZZZZZZZZZZZZZZZZZZ") is None


# ---- 11: read connections are released with their threads ----------------------------------
def test_read_connections_close_with_their_threads(store):
    def work():
        store.r.execute("SELECT 1").fetchone()

    for _ in range(3):
        threads = [threading.Thread(target=work) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    gc.collect()
    assert store.open_readers() <= 1


# ---- 12: data is always strict JSON ---------------------------------------------------------
@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity", "1e400"])
def test_non_finite_numbers_are_rejected(client, key, literal):
    body = '{"group":"general","profile":"p","text":"t","data":{"x":%s}}' % literal
    r = client.post("/post", content=body, headers={"Authorization": key.auth_header("general", "p", "t", {"x": 1}, board=board_of(client)),
                                                    "Content-Type": "application/json"})
    assert r.status_code == 400 and "finite" in r.text
    with pytest.raises(ValueError):
        bb.canonical_json({"x": float("nan")})


# ---- 16: group names ---------------------------------------------------------------------
def test_agents_cannot_create_groups(client, key, store):
    ok(client, key, "general", "one")
    ok(client, key, "general", "two")
    for g in ("nul", "grp-one", "conventions"):
        r = send(client, key, g, "x")
        assert r.status_code == 403 and "group_unknown" in r.text, g
        assert not store.group_exists(g)
    assert client.get("/groups/_conventions").status_code == 404  # the conventions file isn't a group


# ---- L2, L3, L4, L6: HTTP hygiene ---------------------------------------------------------
def test_every_response_is_nosniff_and_errors_are_one_line(client):
    for r in (client.get("/feed"), client.get("/nope"), client.get("/feed?limit=abc"), client.post("/post")):
        assert r.headers["x-content-type-options"] == "nosniff"
    assert client.get("/nope").text == "error not_found: Not Found\n"
    r = client.get("/feed?limit=abc")
    assert r.status_code == 400 and r.text.startswith("error bad_param: limit")
    assert client.get("/search?q=" + "a" * 201).status_code == 400


def test_request_limits_group_ipv6_and_stay_bounded():
    assert peer_key("2001:db8::1") == peer_key("2001:db8::ffff") != peer_key("2001:db8:0:1::1")
    assert peer_key("::ffff:100.64.0.1") == "100.64.0.1"
    lim = RequestLimiter(5, max_keys=2)
    for ip in ("1.1.1.1", "2.2.2.2", "3.3.3.3", "4.4.4.4", "5.5.5.5", "6.6.6.6"):
        lim.hit(ip)
    assert set(lim.counts) == {"1.1.1.1", "2.2.2.2", "3.3.0.0/16", "4.4.0.0/16", "overflow"}
    assert lim.counts["overflow"] == 2


# ---- public board: it is open to the internet, so limits must hold there ---------
def test_a_flood_of_fresh_addresses_cannot_lock_out_newcomers():
    lim = RequestLimiter(5, max_keys=50)
    for i in range(500):  # one attacker, a /48 of fresh /64s, each used to the limit
        for _ in range(5):
            lim.hit(f"2001:db8:aaaa:{i:x}::1")
    assert lim.hit("2001:db8:aaaa:ffff::1") < 0  # past the table, the flood shares one bucket per /48
    assert lim.hit("198.51.100.7") == 4 and lim.hit("2001:db8:bbbb::1") == 4  # newcomers aren't in it
    assert len(lim.counts) <= 2 * 50 + 1


def test_ipv6_writes_are_rate_limited_per_64(strict):
    s, st = strict
    for i in range(s.posts_per_hour):  # fresh keys, fresh addresses, all in one /64
        direct(st, s, new_key(), "general", f"post {i}", ip=f"2001:db8:0:1::{i + 1:x}")
    with pytest.raises(PostError) as e:
        direct(st, s, new_key(), "general", "one too many", ip="2001:db8:0:1:ffff::1")
    assert e.value.status == 429 and "peer IP" in e.value.msg


def test_refused_writes_spend_the_request_quota(store, settings, key):
    with TestClient(create_app(store, settings.with_(requests_per_hour=3))) as c:
        assert [c.post("/post", json={}).status_code for _ in range(4)] == [401, 401, 401, 429]
        assert c.get("/feed").status_code == 429  # one quota for reads and writes


# ---- L7: the private key is never readable by others -----------------------------------------
@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
def test_key_file_is_created_owner_only(tmp_path):
    p = tmp_path / "k" / "key.json"
    bb.Key.generate(p)
    assert os.stat(p).st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        bb.Key.generate(p)


# ---- L8: a signature is only good on the board it was signed for ---------------------------------
def test_signatures_bind_the_board(client, key, store, settings, tmp_path):
    other = Store(Settings(root=make_site(tmp_path / "other"), fsync=False))
    try:
        assert other.board_id != store.board_id and client.get("/board").text == store.board_id + "\n"
        auth = key.auth_header("general", "p", "hi", None, board=other.board_id)  # signed for the other board
        body = {"group": "general", "profile": "p", "text": "hi", "data": None}
        r = client.post("/post", json=body, headers={"Authorization": auth})
        assert r.status_code == 401 and "bad_signature" in r.text
        r = client.post("/post", json={**body, "board": other.board_id}, headers={"Authorization": auth})
        assert r.status_code == 401 and "wrong_board" in r.text and store.board_id in r.text
    finally:
        other.close()
    ok(client, key, "general", "verifiable")  # each stored line names its board, so the log re-verifies alone
    rec = json.loads(next(settings.data_dir.glob("log-*.ndjson")).read_text(encoding="utf-8").splitlines()[-1])
    assert rec["bid"] == store.board_id == store._board_id()  # minted once, then read back
    assert verify(decode_pubkey(rec["pk"]), rec["sig"], signing_payload(rec["sts"], rec["group"], rec["profile"],
                                                                        rec["text"], rec["data"], board=rec["bid"]))


# ---- L17: an apply that failed is mirrored on the next boot -----------------------------------
def test_boot_mirrors_records_sqlite_missed_even_after_later_writes(settings, key):
    st = Store(settings)
    a = direct(st, settings, key, "general", "lost in sqlite").record.ulid
    st.w.execute("DELETE FROM posts WHERE ulid=?", (a,))  # as if its apply had failed and rolled back
    st.w.execute("DELETE FROM kv_root WHERE ulid=?", (a,))
    st.w.commit()
    direct(st, settings, key, "general", "a later write that succeeded")
    st.close()
    st = Store(settings)
    try:
        assert a in [r[0] for r in st.feed()]
    finally:
        st.close()


# ---- 2026-10-07 review: retries after a failed mirror, overload, HEAD, oversized bodies -----------
def test_a_logged_post_whose_mirror_failed_is_not_accepted_twice(store, settings, key, monkeypatch):
    """The log line is written before SQLite. If the mirror write fails, a retry of the same signed post
    is a replay, not a second line, and the post reaches the mirror without waiting for a restart."""
    real, failed = store._apply, []

    def flaky(rec, *a, **kw):
        if kw.get("live") and not failed:
            failed.append(rec.ulid)
            raise sqlite3.OperationalError("disk I/O error")
        return real(rec, *a, **kw)

    monkeypatch.setattr(store, "_apply", flaky)
    auth = key.auth_header("general", "p", "only once", None, board=store.board_id)
    body = {"group": "general", "profile": "p", "text": "only once", "data": None}
    with TestClient(create_app(store, settings), raise_server_exceptions=False) as c:
        assert c.post("/post", json=body, headers={"Authorization": auth}).status_code == 500
        r = c.post("/post", json=body, headers={"Authorization": auth})
        assert r.status_code == 409 and "replay" in r.text
        store.sweep()
        assert "only once" in c.get("/feed").text
    lines = [x for p in settings.data_dir.glob("log-*.ndjson") for x in p.read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 1


@pytest.mark.parametrize("body", [
    '{"group":"general","profile":"p","text":"hi \\ud800","data":null}',
    '{"group":"general","profile":"p","text":"hi","data":{"a":["\\udfff"]}}',
    '{"group":"general","profile":"p","text":"hi","data":{"\\ud800":1}}',
])
def test_lone_surrogates_are_a_400_not_a_500(store, settings, key, body):
    """A lone surrogate is valid JSON but not Unicode: signing it used to raise UnicodeEncodeError."""
    auth = key.auth_header("general", "p", "hi", None, board=store.board_id)
    with TestClient(create_app(store, settings), raise_server_exceptions=False) as c:
        r = c.post("/post", content=body, headers={"Authorization": auth, "Content-Type": "application/json"})
    assert r.status_code == 400 and ("bad_text" in r.text or "bad_data" in r.text), r.text


def test_a_failed_append_does_not_cost_the_next_post_its_line(store, settings, key, monkeypatch):
    """A write that dies halfway (disk full) leaves a torn line; the next append must not glue onto it."""
    import bboard.store as store_mod

    class Torn:
        def __init__(self, f):
            self.f = f

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.f.close()

        def write(self, x):
            self.f.write(x[: len(x) // 2])
            self.f.flush()
            raise OSError(28, "No space left on device")

    ok(TestClient(create_app(store, settings)), key, "general", "before")
    real_open = open
    monkeypatch.setattr(store_mod, "open", lambda p, mode="r", *a, **kw:
                        Torn(real_open(p, mode, *a, **kw)) if "a" in mode else real_open(p, mode, *a, **kw),
                        raising=False)
    with pytest.raises(OSError):
        direct(store, settings, key, "general", "torn")
    monkeypatch.undo()
    after = direct(store, settings, key, "general", "after").record.ulid
    logged = [r.ulid for p in settings.data_dir.glob("log-*.ndjson") for r in store_mod.read_log(p)]
    assert after in logged


def test_overload_is_refused_with_a_one_line_503():
    gate = asyncio.Event()

    async def slow(scope, receive, send):
        await gate.wait()
        await PlainTextResponse("ok")(scope, receive, send)

    guard = Guard(slow, RequestLimiter(1000), 1000, max_inflight=1)

    async def call():
        sent = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(msg):
            sent.append(msg)

        await guard({"type": "http", "method": "GET", "path": "/", "headers": [], "client": ("192.0.2.1", 1)},
                    receive, send)
        return sent

    async def main():
        first = asyncio.create_task(call())
        await asyncio.sleep(0.05)
        busy = await call()
        gate.set()
        return await first, busy, await call()

    first, busy, after = asyncio.run(main())
    assert first[0]["status"] == 200 and after[0]["status"] == 200  # the slot frees when a request ends
    assert busy[0]["status"] == 503 and busy[1]["body"].startswith(b"error busy: ")
    assert (b"retry-after", b"1") in busy[0]["headers"]


def test_head_works_wherever_get_does(client):
    for path in ("/", "/health", "/board", "/feed", "/groups"):
        g, h = client.get(path), client.head(path)
        assert h.status_code == g.status_code == 200 and h.content == b"", path
        assert h.headers["content-length"] == g.headers["content-length"], path
    assert client.head("/feed").headers["etag"] == client.get("/feed").headers["etag"]
