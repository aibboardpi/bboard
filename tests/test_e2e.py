"""Against a real uvicorn listener: proxy headers, the CLI and the MCP proxy."""

import asyncio
import json
import sys

import bb
import httpx
from conftest import ROOT, new_key


def test_the_board_sees_the_address_the_proxy_forwards(live):
    """A proxy on loopback overwrites X-Forwarded-For with the real client: limits use it."""
    assert httpx.get(live.url + "/peer").text == "127.0.0.1\n"
    r = httpx.get(live.url + "/peer", headers={"X-Forwarded-For": "2001:db8:1:2:3::9"})
    assert r.text == "2001:db8:1:2::/64\n"


def test_cli_flow(live, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("BB_URL", live.url)
    monkeypatch.setenv("BB_KEY", str(tmp_path / "k" / "key.json"))
    monkeypatch.setenv("BB_PROFILE", "cli-bot")

    def run(*argv):
        code = bb.main(list(argv))
        out = capsys.readouterr()
        return code, out.out + out.err

    code, out = run("keygen")
    assert code == 0 and out.startswith("agent_id ")
    code, out = run("keygen")
    assert code == 1 and "refusing to overwrite" in out
    code, out = run("post", "general", "cli says hi", "--severity", "info")
    assert code == 0 and "cli says hi" in out and ":cli-bot |" in out
    assert "cli says hi" in run("feed", "--group", "general", "--new")[1]
    assert run("feed", "--group", "general", "--new")[1].strip() == "(nothing new)"
    # a task posted by another agent
    task = bb.parse_line(bb.Board(live.url, key=new_key()).post("tasks", "count the bridges", {"status": "open"}))["ulid"]
    assert task in run("tasks", "tasks")[1]
    code, out = run("claim", task, "--ttl", "5m")
    assert code == 0 and '"status":"claimed"' in out and '"expires_at"' in out
    assert run("tasks", "tasks")[1].strip() == "(no open tasks)"
    code, out = run("done", task, "--text", "12 bridges")
    assert code == 0 and '"status":"done"' in out
    assert "live posts=3" in run("whoami")[1]
    mine = bb.parse_line(bb.Board(live.url, key=bb.Key.load(tmp_path / "k" / "key.json")).post("general", "keep me"))["ulid"]
    code, out = run("renew", mine)
    assert code == 0 and '"renew":"' + mine + '"' in out and '"expires_at"' in out
    assert "renewed=1/200" in run("whoami")[1]
    code, out = run("renew", task)  # someone else's post
    assert code == 1 and "not_author" in out
    code, out = run("post", "general", "x" * 600)
    assert code == 1 and "too_long" in out
    assert "bridges" in run("search", "bridges")[1]


def test_mcp_proxy_signs_locally(live, tmp_path):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    key_path = tmp_path / "mcp" / "key.json"
    params = StdioServerParameters(command=sys.executable, args=[str(ROOT / "client" / "bb_mcp.py")],
                                   env={"BB_URL": live.url, "BB_KEY": str(key_path), "BB_PROFILE": "mcp-bot"})

    async def session():
        async with stdio_client(params) as (r, w), ClientSession(r, w) as s:
            await s.initialize()
            names = {t.name for t in (await s.list_tools()).tools}

            async def call(name, **args):
                res = await s.call_tool(name, args)
                return res.content[0].text

            posted = await call("post_note", group="tasks", text="map the north trail", data={"status": "open"})
            task = bb.parse_line(posted)["ulid"]
            feed1 = await call("read_feed", group="tasks", only_new=True)
            feed2 = await call("read_feed", group="tasks", only_new=True)
            open1 = await call("open_tasks", group="tasks")
            claimed = await call("claim_task", task=task, ttl="15m")
            open2 = await call("open_tasks", group="tasks")
            finished = await call("finish_task", task=task, note="mapped 4km")
            bad = await call("post_note", group="Nope", text="x")
            found = await call("search_notes", q="north")
            renewed = await call("renew_note", ulid=task)
            return names, posted, feed1, feed2, open1, claimed, open2, finished, bad, found, renewed

    names, posted, feed1, feed2, open1, claimed, open2, finished, bad, found, renewed = asyncio.run(session())
    assert names == {"post_note", "read_feed", "search_notes", "open_tasks", "claim_task", "finish_task",
                     "renew_note"}
    assert '"renew":' in renewed and '"expires_at"' in renewed
    assert ":mcp-bot |" in posted
    assert "map the north trail" in feed1 and feed2 == "(nothing new)"
    assert "map the north trail" in open1
    assert '"status":"claimed"' in claimed and open2 == "(no open tasks)"
    assert '"status":"done"' in finished
    assert bad.startswith("error bad_group")
    assert "north" in found
    key = json.loads(key_path.read_text())  # generated on first run, on the agent's side
    assert key["agent_id"] in posted
    stored = [json.loads(x) for p in sorted(live.s.data_dir.glob("log-*.ndjson"))
              for x in p.read_text(encoding="utf-8").splitlines()]
    assert {x["via"] for x in stored} == {"mcp"}


def test_mcp_only_new_cursor_ignores_plain_reads(live, tmp_path):
    """A plain read_feed (latest page) must not move the only_new cursor past posts it never showed."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(command=sys.executable, args=[str(ROOT / "client" / "bb_mcp.py")],
                                   env={"BB_URL": live.url, "BB_KEY": str(tmp_path / "mcp" / "key.json")})
    other = bb.Board(live.url, key=new_key())

    async def session():
        async with stdio_client(params) as (r, w), ClientSession(r, w) as s:
            await s.initialize()

            async def call(name, **args):
                return (await s.call_tool(name, args)).content[0].text

            other.post("general", "first note")
            await call("read_feed", group="general", only_new=True)
            other.post("general", "second note")
            other.post("general", "third note")
            latest = await call("read_feed", group="general", limit=1)
            return latest, await call("read_feed", group="general", only_new=True)

    latest, new = asyncio.run(session())
    assert "third note" in latest and "second note" not in latest
    assert "second note" in new and "third note" in new


def test_mcp_reports_an_unusable_key_file_as_one_line(live, tmp_path):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    key_path = tmp_path / "mcp" / "key.json"
    key_path.parent.mkdir()
    key_path.write_text("not a key")
    params = StdioServerParameters(command=sys.executable, args=[str(ROOT / "client" / "bb_mcp.py")],
                                   env={"BB_URL": live.url, "BB_KEY": str(key_path)})

    async def session():
        async with stdio_client(params) as (r, w), ClientSession(r, w) as s:
            await s.initialize()
            return [(await s.call_tool(name, args)).content[0].text for name, args in (
                ("post_note", {"group": "general", "text": "hi"}),
                ("finish_task", {"task": "01M48WACVMNJ3P99AN6JW6VAKY"}))]

    for out in asyncio.run(session()):
        assert out.startswith("error bad_key: ") and str(key_path) in out, out
    assert key_path.read_text() == "not a key"  # never overwritten
