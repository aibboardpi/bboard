import json
import time

import bb
import pytest
from conftest import board_of, new_key, ok, send

from bboard.config import Settings
from bboard.crypto import signing_payload
from bboard.model import PostError
from bboard.store import Store
from bboard.writer import write_post


def test_post_happy_path(client, key, store, settings):
    r = send(client, key, "general", "first light on the ridge", {"severity": "info", "lat": 50.3, "lon": -122.8})
    assert r.status_code == 201, r.text
    line = r.text.strip()
    rec = bb.parse_line(line)
    assert rec["ulid"] == r.headers["X-ULID"] == r.headers["X-Root-ULID"]
    assert rec["agent_id"] == key.agent_id and rec["profile"] == "tester"
    assert rec["data"] == {"severity": "info", "lat": 50.3, "lon": -122.8}
    assert int(r.headers["X-RateLimit-Remaining"]) >= 0
    # NDJSON is the source of truth and keeps the signature, so the log is self-verifying
    files = list(settings.data_dir.glob("log-*.ndjson"))
    assert len(files) == 1
    stored = json.loads(files[0].read_text(encoding="utf-8").strip())
    assert stored["ulid"] == rec["ulid"] and stored["pk"] == key.pubkey and stored["via"] == "rest"
    from bboard.crypto import decode_pubkey, verify

    assert verify(decode_pubkey(stored["pk"]), stored["sig"],
                  signing_payload(stored["sts"], stored["group"], stored["profile"], stored["text"], stored["data"],
                                  board=stored["bid"]))
    assert client.get("/feed?group=general").text.strip() == line


@pytest.mark.parametrize("group,text,profile,code", [
    ("general", "x" * 501, "tester", "too_long"),
    ("General", "hi", "tester", "bad_group"),
    ("ab", "hi", "tester", "bad_group"),
    ("general", "   ", "tester", "bad_text"),
    ("general", "bell\x07", "tester", "bad_text"),
    ("general", "hi", "has space", "bad_profile"),
    ("general", "hi", "a:b", "bad_profile"),
])
def test_shape_validation(client, key, group, text, profile, code):
    r = send(client, key, group, text, profile=profile)
    assert r.status_code == 400 and code in r.text


def test_cross_group_replay_is_rejected(client, key):
    auth = key.auth_header("general", "tester", "deploy now", None, board=board_of(client))
    r = client.post("/post", json={"group": "tasks", "profile": "tester", "text": "deploy now"},
                    headers={"Authorization": auth})
    assert r.status_code == 401 and "bad_signature" in r.text


def test_tampered_data_profile_rejected(client, key):
    r = send(client, key, "general", "hi", {"status": "open"}, body_overrides={"data": {"status": "done"}})
    assert r.status_code == 401
    r = send(client, key, "general", "hi", body_overrides={"profile": "someone-else"})
    assert r.status_code == 401


def test_timestamp_window_and_exact_replay(client, key):
    r = send(client, key, "general", "old news", ts=int(time.time()) - 301)
    assert r.status_code == 401 and "stale_timestamp" in r.text
    r = send(client, key, "general", "from the future", ts=int(time.time()) + 301)
    assert r.status_code == 401
    ts = int(time.time())
    auth = key.auth_header("general", "tester", "once", None, ts, board=board_of(client))
    body = {"group": "general", "profile": "tester", "text": "once"}
    assert client.post("/post", json=body, headers={"Authorization": auth}).status_code == 201
    r = client.post("/post", json=body, headers={"Authorization": auth})
    assert r.status_code == 409 and "replay" in r.text


def test_auth_header_errors(client):
    r = client.post("/post", json={"group": "general", "profile": "p", "text": "x"})
    assert r.status_code == 401 and "bad_auth" in r.text
    body = {"group": "general", "profile": "p", "text": "x"}
    r = client.post("/post", json=body, headers={"Authorization": "Bearer a:b"})
    assert r.status_code == 401
    r = client.post("/post", json=body, headers={"Authorization": f"Bearer 0OIl:b:{int(time.time())}"})
    assert r.status_code == 401 and "bad_auth" in r.text


def test_client_cannot_set_root_ulid(client, key):
    r = send(client, key, "general", "hi", {"root_ulid": "01J0000000000000000000000A"})
    assert r.status_code == 400 and "root_ulid" in r.text


def test_only_admin_defined_groups_take_posts(client, key, store, settings):
    for i in range(3):  # however many posts an agent has, it can't create a group
        ok(client, key, "general", f"busy agent {i}")
    r = send(client, key, "trail-reports", "new group please")
    assert r.status_code == 403 and "group_unknown" in r.text
    assert not store.group_exists("trail-reports")
    (settings.groups_dir / "trail-reports.md").write_text("---\ndurable: false\n---\n# trail-reports\n\nTrail news.\n",
                                                         encoding="utf-8")
    ok(client, key, "trail-reports", "the admin added it")
    assert "trail-reports | 1 live | Trail news." in client.get("/groups").text


def test_ttl_becomes_absolute_expiry(client, key):
    before = time.time()
    r = send(client, key, "general", "parking spot free", {"ttl": "10m"})
    data = bb.parse_line(r.text.strip())["data"]
    assert "ttl" not in data
    from bboard.model import parse_iso

    assert before + 595 <= parse_iso(data["expires_at"]) <= time.time() + 605
    for bad in ({"ttl": "10 minutes"}, {"expires_at": "+1h"}, {"ttl": "1h", "expires_at": "2099-01-01T00:00:00Z"},
                {"expires_at": "2001-01-01T00:00:00Z"},
                {"ttl": "400d"}, {"status": "maybe"}, {"severity": "meh"}, {"lat": 91}, {"lon": "x"},
                {"reply_to": "not-a-ulid"}):
        r = send(client, key, "general", "bad " + json.dumps(bad), bad)
        assert r.status_code == 400, bad


def test_swarm_rules(client, key):
    task = ok(client, key, "tasks", "index the trail photos", {"status": "open"})
    r = send(client, key, "tasks", "claim", {"status": "claimed"})
    assert r.status_code == 400 and "reply_to" in r.text
    r = send(client, key, "tasks", "claim", {"reply_to": task, "status": "claimed", "lease_owner": "zzzzzzzz"})
    assert r.status_code == 400 and "lease_owner" in r.text
    ok(client, key, "tasks", "claim", {"reply_to": task, "status": "claimed", "lease_owner": key.agent_id, "ttl": "10m"})


def test_threads_resolve_root_in_o1(client, key):
    root = ok(client, key, "general", "root")
    a = ok(client, key, "general", "reply a", {"reply_to": root})
    b = ok(client, key, "general", "reply to a", {"reply_to": a})
    other = ok(client, key, "general", "unrelated")
    for probe in (root, a, b):
        lines = client.get(f"/feed?thread={probe}").text.strip().split("\n")
        assert [bb.parse_line(x)["ulid"] for x in lines] == [root, a, b]
    assert other not in client.get(f"/feed?thread={root}").text
    r = send(client, key, "general", "orphan", {"reply_to": "01J0000000000000000000000A"})
    assert r.status_code == 410 and "parent_expired" in r.text


def test_rate_limit_and_key_rotation(tmp_path):
    from conftest import make_site

    s = Settings(root=make_site(tmp_path / "rl"), fsync=False)
    st = Store(s)
    try:
        def post(k, text, ip="100.64.0.1", data=None, group="general"):
            ts = int(time.time())
            sig = k.sign(signing_payload(ts, group, "p", text, data, board=st.board_id))
            return write_post(st, s, pubkey=k.pubkey, sig=sig, ts=ts, group=group, profile="p", text=text,
                              data=data, ip=ip)

        k1 = new_key()
        assert post(k1, "n0").remaining == s.posts_per_hour - 1
        for i in range(1, s.posts_per_hour):
            post(k1, f"n{i}")
        with pytest.raises(PostError) as e:
            post(k1, "one too many")
        assert e.value.status == 429
        with pytest.raises(PostError) as e:  # fresh key, same peer: no bypass
            post(new_key(), "rotated", ip="100.64.0.1")
        assert e.value.status == 429
        post(new_key(), "different peer is fine", ip="100.64.0.2")
    finally:
        st.close()


def test_body_limits(client, key):
    r = client.post("/post", content=b"{" + b" " * 9000 + b"}",
                    headers={"Authorization": key.auth_header("general", "p", "x", board=board_of(client)), "Content-Type": "application/json"})
    assert r.status_code == 413
    r = client.post("/post", content=b"not json", headers={"Authorization": key.auth_header("general", "p", "x", board=board_of(client))})
    assert r.status_code == 400 and "bad_json" in r.text
    big = {"blob": "x" * 2000}
    r = send(client, key, "general", "big data", big)
    assert r.status_code == 400 and "bad_data" in r.text
