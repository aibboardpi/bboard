import json
import time

from conftest import new_key, ok

from bboard.crypto import signing_payload
from bboard.model import Record
from bboard.store import Store, month_of, row_line
from bboard.ulid import ULIDGen
from bboard.writer import write_post


def lines(settings):
    out = []
    for p in sorted(settings.data_dir.glob("log-*.ndjson")):
        out += [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]
    return out


def test_sweep_and_ghost_threads(client, key, store, settings):
    task = ok(client, key, "tasks", "short-lived task", {"status": "open", "ttl": "1m"})
    keep = ok(client, key, "general", "long-lived")
    logged = lines(settings)
    assert store.sweep(now=time.time() + 61) == 1
    assert lines(settings) == logged  # expiry writes nothing: each line carries its own exp
    assert task not in client.get("/feed").text and keep in client.get("/feed").text
    assert client.get("/search?q=short").text == ""
    # replying to an expired post still lands in the right thread via the KV
    r = ok(client, key, "tasks", "late reply", {"reply_to": task})
    assert store.thread_root(r) == task
    assert r in client.get(f"/feed?thread={task}").text


def test_rebuild_from_ndjson_is_identical(client, key, store, settings):
    root = ok(client, key, "general", "root | with pipe\nand newline", {"status": "open"})
    worker = new_key()
    ok(client, worker, "general", "mine", {"reply_to": root, "status": "claimed", "lease_owner": worker.agent_id})
    ok(client, worker, "general", "reply", {"reply_to": root, "status": "done"})
    gone = ok(client, key, "general", "expires", {"ttl": "1m"})
    later = time.time() + 61
    store.sweep(now=later)
    before = "".join(row_line(r) + "\n" for r in store.feed(now=later))
    store.close()
    for suffix in ("", "-wal", "-shm"):
        p = settings.db_path.with_name(settings.db_path.name + suffix)
        if p.exists():
            p.unlink()
    fresh = Store(settings)
    try:
        assert "".join(row_line(r) + "\n" for r in fresh.feed(now=later)) == before
        assert gone not in before
        assert fresh.thread_root(gone) == gone  # KV survives expiry + rebuild
        assert fresh.agent_key(worker.agent_id) == worker.pubkey
    finally:
        fresh.close()


def test_rebuild_never_mirrors_an_expired_post(settings):
    g = ULIDGen()
    dead, alive = g.new(), g.new()
    path = settings.data_dir / f"log-{month_of(dead)}.ndjson"
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(Record(ulid=dead, group="general", agent_id="abcd1234", profile="p", text="long gone", root=dead,
                       exp=int(time.time()) - 1).to_json() + "\n")
        f.write(Record(ulid=alive, group="general", agent_id="abcd1234", profile="p", text="still here",
                       root=dead, exp=int(time.time()) + 3600, data={"reply_to": dead}).to_json() + "\n")
    st = Store(settings)
    try:
        assert [r[0] for r in st.feed()] == [alive]
        assert st.thread_root(dead) == dead and st.thread_root(alive) == dead
    finally:
        st.close()


def test_boot_catches_up_after_crash_and_heals_torn_line(settings, key):
    st = Store(settings)
    ts = int(time.time())
    sig = key.sign(signing_payload(ts, "general", "p", "a", None, board=st.board_id))
    write_post(st, settings, pubkey=key.pubkey, sig=sig, ts=ts, group="general", profile="p", text="a", data=None)
    st.close()
    # simulate a crash: line made it to NDJSON, SQLite never saw it; then a torn partial write
    u = ULIDGen().new()
    rec = Record(ulid=u, group="general", agent_id=key.agent_id, profile="p", text="crash survivor",
                 root=u, exp=int(time.time()) + 3600)
    path = settings.data_dir / f"log-{month_of(u)}.ndjson"
    with open(path, "a", encoding="utf-8", newline="\n") as f:
        f.write(rec.to_json() + "\n" + '{"ulid":"01J-torn')
    st = Store(settings)
    try:
        assert "crash survivor" in "".join(r[5] for r in st.feed())
        ts = int(time.time())
        sig = key.sign(signing_payload(ts, "general", "p", "after", None, board=st.board_id))
        write_post(st, settings, pubkey=key.pubkey, sig=sig, ts=ts, group="general", profile="p", text="after", data=None)
        raw = path.read_text(encoding="utf-8").splitlines()
        assert raw[-2] == '{"ulid":"01J-torn' and json.loads(raw[-1])["text"] == "after"
    finally:
        st.close()
