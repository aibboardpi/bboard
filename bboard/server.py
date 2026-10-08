"""Runs the board (one Store = one SQLite writer) plus the expiry sweeper, in one process.

  host:port   the board, reads and writes for everyone
"""

from __future__ import annotations

import asyncio
import logging

import uvicorn

from .app import create_app
from .config import Settings
from .store import Store

log = logging.getLogger("bboard.server")


async def _every(secs: int, fn, name: str):
    while True:
        await asyncio.sleep(secs)
        try:
            out = await asyncio.to_thread(fn)
            if out:
                log.info("%s: %s", name, out)
        except Exception:
            log.exception("%s failed", name)


async def serve(s: Settings, store: Store | None = None, ready: asyncio.Event | None = None,
                stop_event: asyncio.Event | None = None):
    store = store or Store(s)
    # the reverse proxy connects from loopback and sets X-Forwarded-For to the real client (overwriting
    # any the client sent), so trusting it from loopback gives every peer its true address
    server = uvicorn.Server(uvicorn.Config(create_app(store, s), host=s.host, port=s.port, proxy_headers=True,
                                           forwarded_allow_ips=s.trusted_proxies, log_level="warning",
                                           timeout_graceful_shutdown=3))
    jobs = [asyncio.create_task(_every(s.sweep_secs, store.sweep, "sweep"))]
    if stop_event is not None:
        async def _watch_stop():
            await stop_event.wait()
            server.should_exit = True
        jobs.append(asyncio.create_task(_watch_stop()))

    log.info("bboard on %s:%d", s.host, s.port)
    task = asyncio.create_task(server.serve())
    if ready is not None:
        while not server.started and not task.done():
            await asyncio.sleep(0.05)
        ready.set()
    try:
        await task
    finally:
        for j in jobs:
            j.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)
        store.close()
    return server
