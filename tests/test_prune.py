"""Prune: expired posts leave the log for good; live and renewed ones stay byte for byte; an agent that
would vanish keeps a blank stub so the first key seen with its agent_id still owns it."""

import json
import time
from datetime import datetime, timezone

import pytest
from conftest import free_port, new_key

from bboard.__main__ import main
from bboard.crypto import signing_payload
from bboard.model import PostError, Record
from bboard.prune import PruneError, prune_logs
from bboard.store import Store, log_files, row_line
from bboard.ulid import ULIDGen
from bboard.writer import write_post

DAY = 86400


def direct(st, s, k, group, text, data=None, now=None):
    now = time.time() if now is None else now
    sig = k.sign(signing_payload(int(now), group, "p", text, data, board=st.board_id))
    return write_post(st, s, pubkey=k.pubkey, sig=sig, ts=int(now), group=group, profile="p", text=text, data=data,
                      ip="100.64.0.1", now=now).record


def raw(settings) -> dict[str, str]:
    """ulid -> the line as it sits in the log."""
    out = {}
    for p in log_files(settings.data_dir):
        for x in p.read_text(encoding="utf-8").splitlines():
            if x.strip():
                out[json.loads(x)["ulid"]] = x
    return out


def snapshot(settings) -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in sorted(settings.data_dir.iterdir())}


def rebuilt(store, settings):
    store.close()
    for suffix in ("", "-wal", "-shm"):
        p = settings.db_path.with_name(settings.db_path.name + suffix)
        if p.exists():
            p.unlink()
    return Store(settings)


@pytest.fixture
def world(store, settings):
    """Five agents' history, written on a stepped clock, and the day (T) it is pruned."""
    t0 = time.time()
    alice, bob, carol, dave = (new_key() for _ in range(4))
    w = {}
    w["alice_stale"] = direct(store, settings, alice, "general", "alice stale chatter", now=t0).ulid
    w["alice_kept"] = direct(store, settings, alice, "general", "alice renewed chatter", now=t0 + 1).ulid
    w["renew1"] = direct(store, settings, alice, "general", "renew", {"renew": w["alice_kept"]}, now=t0 + 5 * DAY).ulid
    w["renew2"] = direct(store, settings, alice, "general", "renew", {"renew": w["alice_kept"]}, now=t0 + 10 * DAY).ulid
    w["bob_stale"] = direct(store, settings, bob, "general", "bob only said this", {"severity": "warn"}, now=t0 + 2).ulid
    w["carol_stale"] = direct(store, settings, carol, "general", "carol stale", now=t0 + 3).ulid
    w["carol_fresh"] = direct(store, settings, carol, "general", "carol fresh", now=t0 + 90 * DAY).ulid
    w["dave_post"] = direct(store, settings, dave, "general", "dave renewed once", now=t0 + 4).ulid
    w["dave_renew"] = direct(store, settings, dave, "general", "renew", {"renew": w["dave_post"]}, now=t0 + 5).ulid
    return dict(w=w, T=t0 + 94 * DAY, keys=dict(alice=alice, bob=bob, carol=carol, dave=dave))


def test_prune_keeps_what_is_live_byte_for_byte_and_removes_the_rest(store, settings, world):
    w, T, keys = world["w"], world["T"], world["keys"]
    before_lines = raw(settings)
    before_feed = [row_line(r) for r in store.feed(now=T, limit=200)]
    assert len(before_feed) == 2

    rep = prune_logs(settings, T, grace=0)

    after = raw(settings)
    live = {w["alice_kept"], w["renew2"], w["carol_fresh"]}
    stubs = {w["bob_stale"], w["dave_post"]}  # the first dropped line of an agent with nothing left
    assert set(after) == live | stubs
    assert all(after[u] == before_lines[u] for u in live)  # untouched: signatures still verify
    text = "".join(p.read_text(encoding="utf-8") for p in log_files(settings.data_dir))
    for gone in ("alice stale chatter", "bob only said this", "carol stale", "dave renewed once", "severity"):
        assert gone not in text
    assert (rep.kept_posts, rep.dropped_posts, rep.kept_renews, rep.dropped_renews, rep.stubs) == (2, 4, 1, 2, 2)

    for name, ulid in (("bob", w["bob_stale"]), ("dave", w["dave_post"])):
        stub = json.loads(after[ulid])
        assert stub["text"] == "" and stub["data"] == {} and stub["exp"] == 0 and stub["sig"] == ""
        assert stub["pk"] == keys[name].pubkey and stub["agent_id"] == keys[name].agent_id and stub["via"] == "prune"

    fresh = rebuilt(store, settings)
    try:
        assert [row_line(r) for r in fresh.feed(now=T, limit=200)] == before_feed
        for k in keys.values():  # nobody lost their agent_id, stub or not
            assert fresh.agent_key(k.agent_id) == k.pubkey
        assert fresh.renewed_count(keys["alice"].agent_id, T - DAY) == 1
        # a reply to a pruned post can no longer find its thread
        with pytest.raises(PostError) as e:
            direct(fresh, settings, keys["alice"], "general", "late", {"reply_to": w["alice_stale"]}, now=T)
        assert e.value.status == 410
    finally:
        fresh.close()


def test_a_second_prune_changes_nothing(store, settings, world):
    prune_logs(settings, world["T"], grace=0)
    once = snapshot(settings)
    rep = prune_logs(settings, world["T"], grace=0)
    assert snapshot(settings) == once
    assert (rep.dropped_posts, rep.dropped_renews, rep.stubs) == (0, 0, 2)  # the stubs are re-emitted as they were


def test_a_dry_run_changes_nothing_but_reports_the_same(store, settings, world):
    before = snapshot(settings)
    dry = prune_logs(settings, world["T"], grace=0, dry_run=True)
    assert snapshot(settings) == before
    real = prune_logs(settings, world["T"], grace=0)
    assert str(dry) == str(real) and dry.bytes_after < dry.bytes_before


def test_grace_keeps_what_expired_only_recently(store, settings, key):
    t0 = time.time()
    post = direct(store, settings, key, "general", "expired yesterday", now=t0).ulid  # gone at t0+7d
    prune_logs(settings, t0 + 8 * DAY, grace=2 * DAY)
    assert json.loads(raw(settings)[post])["text"] == "expired yesterday"
    prune_logs(settings, t0 + 8 * DAY, grace=0)
    assert json.loads(raw(settings)[post])["text"] == ""  # only the agent's stub is left


def test_a_clock_behind_the_log_refuses_and_changes_nothing(store, settings, key):
    direct(store, settings, key, "general", "written now")
    before = snapshot(settings)
    with pytest.raises(PruneError, match="behind the newest record"):
        prune_logs(settings, time.time() - 30 * DAY, grace=0)
    assert snapshot(settings) == before


def test_a_renew_that_names_nothing_of_its_authors_is_dropped(settings):
    """Only the log's own writer makes renews, but a line naming another's post (or none) must not stay."""
    g = ULIDGen()
    post, forged, orphan = g.new(), g.new(), g.new()
    soon = int(time.time()) + 30 * DAY
    a = dict(group="general", profile="p", pk="A" * 40)
    mine = Record(ulid=post, agent_id="aaaaaaaa", text="alive", root=post, exp=soon, **a)
    # JSON written with spaces and an extra key: a kept line must come back exactly as it was
    spaced = json.dumps({**json.loads(mine.to_json()), "x": 1})
    lines = [spaced,
             Record(ulid=forged, agent_id="bbbbbbbb", text="renew", root=forged, data={"renew": post},
                    exp=soon, **{**a, "pk": "B" * 40}).to_json(),
             Record(ulid=orphan, agent_id="aaaaaaaa", text="renew", root=orphan, data={"renew": g.new()},
                    exp=soon, **a).to_json()]
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    path = settings.data_dir / f"log-{datetime.now(timezone.utc):%Y-%m}.ndjson"
    path.write_text("".join(x + "\n" for x in lines), encoding="utf-8")
    rep = prune_logs(settings, time.time(), grace=0)
    kept = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]
    assert path.read_text(encoding="utf-8").splitlines()[0] == spaced
    assert (rep.kept_posts, rep.kept_renews, rep.dropped_renews) == (1, 0, 2)
    assert [x["ulid"] for x in kept] == [post, forged]  # `forged` is bbbbbbbb's stub: it has no other line
    assert kept[1]["text"] == "" and kept[1]["data"] == {}


def test_unreadable_lines_are_left_alone_and_emptied_files_go(settings):
    g = ULIDGen()
    old = int(datetime(2020, 1, 15, tzinfo=timezone.utc).timestamp() * 1000)
    new = int(datetime(2020, 2, 15, tzinfo=timezone.utc).timestamp() * 1000)
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    a, b = g.new(old), g.new(new)
    me = dict(group="general", agent_id="abcd1234", profile="p", pk="K" * 40)
    (settings.data_dir / "log-2020-01.ndjson").write_text(
        Record(ulid=a, text="long gone", root=a, exp=old // 1000 + DAY, **me).to_json() + "\n", encoding="utf-8")
    (settings.data_dir / "log-2020-02.ndjson").write_text(
        "\n" + Record(ulid=b, text="alive", root=b, exp=int(time.time()) + 30 * DAY, **me).to_json()
        + '\n{"ulid":"01J-torn', encoding="utf-8")
    rep = prune_logs(settings, time.time(), grace=0)
    assert [p.name for p in log_files(settings.data_dir)] == ["log-2020-02.ndjson"]  # the agent still has `b`
    assert rep.files_removed == 1 and rep.unreadable == 1 and rep.stubs == 0
    lines = (settings.data_dir / "log-2020-02.ndjson").read_text(encoding="utf-8").split("\n")
    assert json.loads(lines[0])["text"] == "alive" and lines[1] == '{"ulid":"01J-torn' and lines[2] == ""
    st = Store(settings)  # still boots, and the live post is served
    try:
        assert [r[0] for r in st.feed()] == [b]
    finally:
        st.close()


# ---- the command --------------------------------------------------------------------------
@pytest.fixture
def env(monkeypatch, settings):
    monkeypatch.setenv("BB_ROOT", str(settings.root))
    monkeypatch.setenv("BB_PORT", str(free_port()))
    return settings


def test_the_command_prunes_then_rebuilds(env, capsys):
    t0 = time.time() - 100 * DAY
    old, k = ULIDGen().new(int(t0 * 1000)), new_key()
    env.data_dir.mkdir(parents=True, exist_ok=True)
    month = datetime.fromtimestamp(t0, timezone.utc).strftime("%Y-%m")
    (env.data_dir / f"log-{month}.ndjson").write_text(
        Record(ulid=old, group="general", agent_id=k.agent_id, profile="p", text="ancient", root=old,
               exp=int(t0) + DAY, pk=k.pubkey).to_json() + "\n", encoding="utf-8")
    assert main(["prune", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("would prune: ") and "rebuilt" not in out and "ancient" in raw(env)[old]
    assert main(["prune"]) == 0
    out = capsys.readouterr().out
    assert "pruned: " in out and "rebuilt: 0 live posts" in out and "ancient" not in raw(env)[old]
    st = Store(env)
    try:
        assert st.agent_key(k.agent_id) == k.pubkey
    finally:
        st.close()


def test_the_command_refuses_while_the_board_is_serving(live, monkeypatch, capsys):
    monkeypatch.setenv("BB_ROOT", str(live.s.root))
    monkeypatch.setenv("BB_PORT", str(live.s.port))
    before = snapshot(live.s)
    assert main(["prune"]) == 1
    assert "refusing: bboard is serving" in capsys.readouterr().out
    assert snapshot(live.s) == before
