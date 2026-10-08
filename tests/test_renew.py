"""Renew: a post lives max_ttl (90d) from when it was written or last renewed. A renew is a signed log
record `{"renew": <ulid>}` that moves its target's expiry; it is never a readable post."""

import json
import time

import bb
import pytest
from conftest import new_key, ok, send
from fastapi.testclient import TestClient

from bboard.app import create_app
from bboard.crypto import signing_payload
from bboard.model import PostError, Record, iso, parse_iso
from bboard.store import Store, month_of, row_line
from bboard.ulid import ULIDGen
from bboard.writer import write_post

DAY = 86400


def direct(st, s, k, group, text, data=None, now=None):
    """write_post at a chosen clock, to step through days without waiting."""
    now = time.time() if now is None else now
    sig = k.sign(signing_payload(int(now), group, "p", text, data, board=st.board_id))
    return write_post(st, s, pubkey=k.pubkey, sig=sig, ts=int(now), group=group, profile="p", text=text, data=data,
                      ip="100.64.0.1", now=now).record


def alive(st, ulid, now):
    return ulid in [r[0] for r in st.feed(now=now, limit=200)]


def logged(settings):
    return [json.loads(x) for p in sorted(settings.data_dir.glob("log-*.ndjson"))
            for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]


def rebuilt(store, settings):
    store.close()
    for suffix in ("", "-wal", "-shm"):
        p = settings.db_path.with_name(settings.db_path.name + suffix)
        if p.exists():
            p.unlink()
    return Store(settings)


def test_a_post_lives_ninety_days_from_its_last_renewal(store, settings, key):
    t0 = time.time()
    post = direct(store, settings, key, "general", "the creek ford is closed", now=t0).ulid  # chatter: 7d
    assert alive(store, post, t0 + 6 * DAY) and not alive(store, post, t0 + 8 * DAY)

    r1 = direct(store, settings, key, "general", "renew", {"renew": post}, now=t0 + 5 * DAY)
    assert r1.exp == int(t0 + 5 * DAY) + 90 * DAY
    assert alive(store, post, t0 + 94 * DAY) and not alive(store, post, t0 + 96 * DAY)

    direct(store, settings, key, "general", "renew", {"renew": post}, now=t0 + 60 * DAY)  # the lease restarts
    assert alive(store, post, t0 + 149 * DAY) and not alive(store, post, t0 + 151 * DAY)
    assert store.sweep(now=t0 + 151 * DAY) == 1 and not alive(store, post, t0 + 151 * DAY)


def test_an_unrenewed_post_never_outlives_ninety_days(client, key):
    assert send(client, key, "general", "ok", {"ttl": "90d"}).status_code == 201
    r = send(client, key, "general", "too long", {"ttl": "91d"})
    assert r.status_code == 400 and "max ttl (90d)" in r.text
    assert send(client, key, "general", "far", {"expires_at": "2099-01-01T00:00:00Z"}).status_code == 400


def test_a_renew_is_not_a_readable_post(client, store, key):
    post = ok(client, key, "general", "sunrise at 06:10 on the ridge")
    renew = ok(client, key, "general", "renew", {"renew": post})
    assert renew not in client.get("/feed").text
    assert client.get("/search?q=renew").text == ""
    assert store.thread_root(renew) is None  # nothing can reply to it
    r = send(client, key, "general", "reply", {"reply_to": renew})
    assert r.status_code == 410 and "parent_expired" in r.text
    assert "general | 1 live" in client.get("/groups").text  # the post, not the renew


def test_the_new_expiry_is_shown_and_costs_a_post_slot(client, store, key):
    post = ok(client, key, "general", "sunrise at 06:10")
    assert "expires_at" not in client.get("/feed").text
    before = store.agent(key.agent_id)[1]
    r = send(client, key, "general", "renew", {"renew": post})
    assert r.status_code == 201 and r.headers["Location"] == f"/feed?thread={post}"
    assert abs(parse_iso(bb.parse_line(r.text.strip())["data"]["expires_at"]) - (time.time() + 90 * DAY)) < 5
    shown = bb.parse_line(client.get("/feed").text.strip())["data"]["expires_at"]  # the post, not the renew
    assert abs(parse_iso(shown) - (time.time() + 90 * DAY)) < 5
    assert store.agent(key.agent_id)[1] == before + 1
    assert f"renewed=1/{store.s.max_renewed}" in client.get(f"/agent/{key.agent_id}").text


def test_a_renewed_task_stays_listed(store, settings, key):
    t0 = time.time()
    task = direct(store, settings, key, "tasks", "map the north trail", {"status": "open", "ttl": "1h"}, now=t0).ulid
    direct(store, settings, key, "tasks", "renew", {"renew": task}, now=t0 + 1800)
    assert [r[0] for r in store.tasks("tasks", now=t0 + 2 * DAY)] == [task]


def test_a_rebuild_matches_the_live_mirror(client, store, settings, key):
    plain = ok(client, key, "general", "no ttl")
    timed = ok(client, key, "general", "with ttl", {"ttl": "1h"})
    dropped = ok(client, key, "general", "not renewed", {"ttl": "1h"})
    for i, u in enumerate((plain, timed, plain)):  # the second renew of `plain` must not change the outcome
        ok(client, key, "general", f"renew {i}", {"renew": u})
    before = [row_line(r) for r in store.feed()]
    fresh = rebuilt(store, settings)
    try:
        assert [row_line(r) for r in fresh.feed()] == before
        assert fresh.renewed_count(key.agent_id, time.time()) == 2
        assert alive(fresh, dropped, time.time())  # still within its hour
        assert not alive(fresh, dropped, time.time() + 2 * 3600) and alive(fresh, timed, time.time() + 2 * 3600)
    finally:
        fresh.close()


def test_a_rebuild_keeps_a_post_that_expired_on_its_own_clock_but_was_renewed(settings):
    """The renew moved the expiry before the original ran out; a fresh mirror must not drop the post."""
    g = ULIDGen()
    renewed, forgotten, hijacked = g.new(), g.new(), g.new()
    me, other = "abcd1234", "wxyz5678"
    path = settings.data_dir / f"log-{month_of(renewed)}.ndjson"
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    gone, later = int(time.time()) - 100, int(time.time()) + 3600
    recs = [Record(ulid=u, group="general", agent_id=me, profile="p", text=t, root=u, exp=gone)
            for u, t in ((renewed, "renewed"), (forgotten, "forgotten"), (hijacked, "hijacked"))]
    recs.append(Record(ulid=g.new(), group="general", agent_id=me, profile="p", text="renew", root="x",
                       data={"renew": renewed}, exp=later))
    recs.append(Record(ulid=g.new(), group="general", agent_id=other, profile="p", text="renew", root="x",
                       data={"renew": hijacked}, exp=later))  # a line only the author's renew may revive
    path.write_text("".join(r.to_json() + "\n" for r in recs), encoding="utf-8", newline="\n")
    st = Store(settings)
    try:
        assert [r[0] for r in st.feed()] == [renewed]
        assert f'"expires_at":"{iso(later)}"' in row_line(st.feed()[0])
        assert st.thread_root(forgotten) == forgotten  # gone, but replies still thread
        assert st.renewed_count(me, time.time()) == 1
    finally:
        st.close()


def test_a_restart_does_not_apply_a_renew_twice(store, settings, key):
    post = direct(store, settings, key, "general", "keep this").ulid
    direct(store, settings, key, "general", "renew", {"renew": post})
    now = time.time()
    used, count = store.rate_count(key.agent_id, None, now), store.renewed_count(key.agent_id, now)
    assert used == (2, 0) and count == 1
    store.close()
    again = Store(settings)  # re-applies the last two months
    try:
        assert again.rate_count(key.agent_id, None, now) == used
        assert again.renewed_count(key.agent_id, now) == 1
        assert again.w.execute("SELECT COUNT(*) FROM renews").fetchone()[0] == 1
    finally:
        again.close()


def test_a_forged_renew_line_cannot_extend_someone_elses_post(store, settings, key):
    """The server never writes one, but replaying the log must not trust a line that names another's post."""
    post = direct(store, settings, key, "general", "mine", now=time.time()).ulid
    original = store.feed()[0]
    forger = Record(ulid=ULIDGen().new(), group="general", agent_id="wxyz5678", profile="p", text="renew",
                    root="x", data={"renew": post}, exp=int(time.time()) + 90 * DAY)
    with open(settings.data_dir / f"log-{month_of(forger.ulid)}.ndjson", "a", encoding="utf-8", newline="\n") as f:
        f.write(forger.to_json() + "\n")
    store.close()
    again = Store(settings)
    try:
        assert again.feed()[0] == original and again.renewed_count("wxyz5678", time.time()) == 0
    finally:
        again.close()


# ---- refusals ---------------------------------------------------------------------------
def test_only_the_author_can_renew(client, key):
    post = ok(client, key, "general", "mine")
    r = send(client, new_key(), "general", "renew", {"renew": post})
    assert r.status_code == 403 and "not_author" in r.text


def test_a_renew_must_name_a_live_post_in_its_own_group(client, store, settings, key):
    post = ok(client, key, "general", "mine")
    r = send(client, key, "tasks", "renew", {"renew": post})
    assert r.status_code == 400 and "wrong_group" in r.text
    unknown = ULIDGen().new()
    r = send(client, key, "general", "renew", {"renew": unknown})
    assert r.status_code == 410 and "renew_gone" in r.text
    # an expired post is gone for good: a renew cannot raise the dead
    t0 = time.time()
    old = direct(store, settings, key, "general", "stale", now=t0).ulid
    with pytest.raises(PostError) as e:
        direct(store, settings, key, "general", "renew", {"renew": old}, now=t0 + 8 * DAY)
    assert e.value.status == 410
    # a renew record is not a post, so it cannot itself be renewed
    renew = ok(client, key, "general", "renew", {"renew": post})
    assert send(client, key, "general", "renew", {"renew": renew}).status_code == 410


def test_a_claim_is_a_lease_and_cannot_be_renewed_into_a_squat(client, key):
    task = ok(client, key, "tasks", "task", {"status": "open"})
    w = new_key()
    claim = ok(client, w, "tasks", "mine", {"reply_to": task, "status": "claimed", "lease_owner": w.agent_id, "ttl": "10m"})
    r = send(client, w, "tasks", "renew", {"renew": claim})
    assert r.status_code == 400 and "not_renewable" in r.text


@pytest.mark.parametrize("extra", [{"ttl": "1h"}, {"expires_at": "2030-01-01T00:00:00Z"}, {"status": "open"},
                                   {"reply_to": "01J0000000000000000000000A"}, {"anything": 1}])
def test_a_renew_carries_nothing_else(client, key, extra):
    post = ok(client, key, "general", "mine")
    r = send(client, key, "general", "renew", {"renew": post, **extra})
    assert r.status_code == 400 and "bad_data" in r.text


@pytest.mark.parametrize("bad", [1, None, "nope", ["x"], {"a": 1}, "01J0000000000000000000000A\n"])
def test_a_renew_target_must_be_a_ulid(client, key, bad):
    r = send(client, key, "general", "renew", {"renew": bad})
    assert r.status_code == 400 and "bad_data" in r.text


@pytest.fixture
def capped(settings):
    s = settings.with_(max_renewed=2)
    st = Store(s)
    with TestClient(create_app(st, s)) as c:
        yield c, st, s
    st.close()


def test_a_key_holds_at_most_max_renewed_posts_alive(capped, key):
    client, store, settings = capped
    a, b, c = (ok(client, key, "general", f"post {i}") for i in range(3))
    ok(client, key, "general", "renew", {"renew": a})
    ok(client, key, "general", "renew", {"renew": b})
    r = send(client, key, "general", "renew", {"renew": c})
    assert r.status_code == 409 and "renew_cap" in r.text
    ok(client, key, "general", "renew again", {"renew": a})  # one already held can be renewed again
    other = new_key()  # the cap is per key
    ok(client, other, "general", "renew", {"renew": ok(client, other, "general", "theirs")})
    # a held post that expires frees its slot
    t0 = time.time()
    store.sweep(now=t0 + 91 * DAY)
    assert store.renewed_count(key.agent_id, t0 + 91 * DAY) == 0
    assert direct(store, settings, key, "general", "late", now=t0 + 91 * DAY).exp


def test_the_unrenewed_posts_a_key_leaves_behind_do_not_count(capped, key):
    client, store, _ = capped
    posts = [ok(client, key, "general", f"p{i}") for i in range(10)]
    assert store.renewed_count(key.agent_id, time.time()) == 0
    for u in posts[:2]:
        ok(client, key, "general", "renew", {"renew": u})
    assert store.renewed_count(key.agent_id, time.time()) == 2
    assert store.renewed_count(key.agent_id, time.time(), besides=posts[0]) == 1


def test_the_log_records_the_renew_with_its_signature(client, settings, key):
    post = ok(client, key, "general", "mine")
    renew = ok(client, key, "general", "renew", {"renew": post})
    line = [x for x in logged(settings) if x["ulid"] == renew][0]
    assert line["data"] == {"renew": post} and line["pk"] == key.pubkey and line["sig"] and line["bid"]
    assert line["exp"] > time.time() + 89 * DAY
