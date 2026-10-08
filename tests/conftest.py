import asyncio
import os
import shutil
import socket
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "client"))

import bb  # noqa: E402  (client)
from fastapi.testclient import TestClient  # noqa: E402

from bboard.app import create_app  # noqa: E402
from bboard.config import Settings  # noqa: E402
from bboard.store import Store  # noqa: E402

PERMISSIVE = dict(posts_per_hour=1000, requests_per_hour=100000)


def make_site(root: Path) -> Path:
    (root / "groups").mkdir(parents=True)
    for p in (ROOT / "groups").glob("*.md"):
        shutil.copy(p, root / "groups" / p.name)
    return root


def free_port() -> int:
    with socket.socket() as sk:
        sk.bind(("127.0.0.1", 0))
        return sk.getsockname()[1]


@pytest.fixture
def settings(tmp_path):
    return Settings(root=make_site(tmp_path / "site"), fsync=False, **PERMISSIVE)


@pytest.fixture
def store(settings):
    st = Store(settings)
    yield st
    st.close()


@pytest.fixture
def client(store, settings):
    with TestClient(create_app(store, settings)) as c:
        yield c


@pytest.fixture
def key():
    return bb.Key(os.urandom(32))


def new_key():
    return bb.Key(os.urandom(32))


def board_of(client) -> str:
    return client.get("/board").text.strip()


def send(client, key, group, text, data=None, profile="tester", ts=None, body_overrides=None, headers=None):
    auth = key.auth_header(group, profile, text, data, ts, board=board_of(client))
    body = {"group": group, "profile": profile, "text": text, "data": data}
    body.update(body_overrides or {})
    return client.post("/post", json=body, headers={"Authorization": auth, **(headers or {})})


def ok(client, key, group, text, data=None, **kw) -> str:
    r = send(client, key, group, text, data, **kw)
    assert r.status_code == 201, r.text
    return r.headers["X-ULID"]


class LiveServer:
    """A real uvicorn listener in a background thread (for the CLI, MCP and proxy-header tests)."""

    def __init__(self, settings: Settings):
        self.s = settings.with_(port=free_port())
        self.url = f"http://127.0.0.1:{self.s.port}"
        self._ready = threading.Event()
        self._thread = threading.Thread(target=lambda: asyncio.run(self._main()), daemon=True)

    async def _main(self):
        from bboard.server import serve

        self.loop = asyncio.get_running_loop()
        self.stop_event, ready = asyncio.Event(), asyncio.Event()
        task = asyncio.create_task(serve(self.s, ready=ready, stop_event=self.stop_event))
        await ready.wait()
        self._ready.set()
        await task

    def __enter__(self):
        self._thread.start()
        assert self._ready.wait(15), "server did not start"
        return self

    def __exit__(self, *exc):
        self.loop.call_soon_threadsafe(self.stop_event.set)
        self._thread.join(15)


@pytest.fixture
def live(tmp_path):
    s = Settings(root=make_site(tmp_path / "live"), fsync=False, **PERMISSIVE)
    with LiveServer(s) as srv:
        yield srv
