import time

import bb
from conftest import new_key, ok
from fastapi.testclient import TestClient

from bboard.app import create_app


def ulids(text: str) -> list[str]:
    return [bb.parse_line(x)["ulid"] for x in text.strip().split("\n") if x]


def test_feed_filters_and_order(client, key):
    k2 = new_key()
    a = ok(client, key, "general", "alpha")
    b = ok(client, k2, "general", "bravo", {"status": "open"})
    c = ok(client, key, "tasks", "charlie", {"status": "open"})
    assert ulids(client.get("/feed").text) == [a, b, c]  # chronological, newest last
    assert ulids(client.get("/feed?group=general").text) == [a, b]
    assert ulids(client.get(f"/feed?agent_id={key.agent_id}").text) == [a, c]
    assert ulids(client.get("/feed?status=open").text) == [b, c]
    assert ulids(client.get("/feed?limit=2").text) == [b, c]
    assert ulids(client.get(f"/feed?since={a}").text) == [b, c]
    assert ulids(client.get("/feed?since=1h").text) == [a, b, c]
    assert client.get("/feed?since=2099-01-01T00:00:00Z").text == ""
    r = client.get("/feed")
    assert r.headers["content-type"].startswith("text/plain")
    for bad in ("group=UPPER", "agent_id=x", "thread=nope", "status=meh", "since=yesterday"):
        assert client.get("/feed?" + bad).status_code == 400, bad


def test_etag_304_when_idle(client, key):
    r = client.get("/feed?group=general")
    assert r.headers["ETag"] == '"empty"'
    assert client.get("/feed?group=general", headers={"If-None-Match": r.headers["ETag"]}).status_code == 304
    u = ok(client, key, "general", "news")
    r = client.get("/feed?group=general", headers={"If-None-Match": '"empty"'})
    assert r.status_code == 200 and r.headers["X-Last-ULID"] == u and r.headers["ETag"] == f'"{u}"'
    r = client.get("/feed?group=general", headers={"If-None-Match": u})  # bare ulid accepted too
    assert r.status_code == 304 and r.content == b""
    assert "X-RateLimit-Remaining" in r.headers


def test_robots_txt(client):
    r = client.get("/robots.txt")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain")
    lines = r.text.splitlines()
    assert "User-agent: *" in lines and "Allow: /" in lines
    assert "Disallow: /search" in lines and "Disallow: /post" in lines


def test_discovery_files(client, store):
    r = client.get("/llms.txt")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain")
    assert r.text.startswith("# bboard\n") and "untrusted" in r.text
    r = client.get("/.well-known/bboard.json")
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/json")
    body = r.json()
    assert body["board"] == store.board_id and body["llms_txt"] == "/llms.txt"


def test_search(client, key):
    a = ok(client, key, "general", "bear sighting near the creek")
    b = ok(client, key, "tasks", "fix the creek bridge", {"status": "open"})
    ok(client, key, "general", "sunny all day")
    assert set(ulids(client.get("/search?q=creek").text)) == {a, b}
    assert ulids(client.get("/search?q=creek&group=tasks").text) == [b]
    assert ulids(client.get('/search?q="bear sighting"').text) == [a]
    assert ulids(client.get("/search?q=cre*").text)
    # FTS syntax errors fall back to quoted terms instead of 500s
    assert client.get("/search?q=trail-scout AND (").status_code == 200
    assert client.get("/search?q=").status_code == 400


def test_open_tasks_follow_claims_leases_and_done(client, key, store):
    worker = new_key()
    t1 = ok(client, key, "tasks", "task one", {"status": "open"})
    t2 = ok(client, key, "tasks", "task two", {"status": "open"})
    ok(client, key, "tasks", "just chatter")
    assert ulids(client.get("/tasks?group=tasks").text) == [t1, t2]
    ok(client, worker, "tasks", "mine", {"reply_to": t1, "status": "claimed", "lease_owner": worker.agent_id, "ttl": "10m"})
    assert ulids(client.get("/tasks?group=tasks").text) == [t2]
    # the lease runs out -> visible again even before the sweeper runs
    later = time.time() + 601
    assert [r[0] for r in store.tasks("tasks", now=later)] == [t1, t2]
    ok(client, worker, "tasks", "gave up", {"reply_to": t1, "status": "failed"})
    assert ulids(client.get("/tasks?group=tasks").text) == [t1, t2]
    ok(client, worker, "tasks", "retry", {"reply_to": t1, "status": "claimed", "lease_owner": worker.agent_id})
    ok(client, worker, "tasks", "finished", {"reply_to": t1, "status": "done"})
    assert ulids(client.get("/tasks?group=tasks").text) == [t2]
    assert client.get("/tasks").status_code == 400


def test_public_clients_write_like_everyone_else(client, key):
    """The board is public: a request arriving through a reverse proxy posts like any other."""
    ulid = ok(client, key, "general", "hello from the internet", headers={"X-Forwarded-Proto": "https"})
    assert "hello from the internet" in client.get(f"/feed?thread={ulid}").text


def test_discovery_endpoints(client, key):
    assert "POST /post" in client.get("/").text
    groups = client.get("/groups").text
    assert "general | 0 live |" in groups and "_conventions" not in groups
    assert "durable: true" in client.get("/groups/tasks").text
    assert client.get("/groups/nope-nope").status_code == 404
    assert "lease_owner" in client.get("/conventions").text
    assert "live posts=0 | limit=" in client.get(f"/agent/{key.agent_id}").text
    assert client.get("/health").text == "ok\n"


def test_read_quota(store, settings):
    with TestClient(create_app(store, settings.with_(requests_per_hour=3))) as c:
        assert [c.get("/health").status_code for _ in range(4)] == [200, 200, 200, 429]
