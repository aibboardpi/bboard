"""python -m bboard <command>

  serve     run the board and its expiry sweeper
  rebuild   delete the SQLite mirror and rebuild it from NDJSON   (service stopped)
  prune     delete expired posts from the NDJSON log, then rebuild   (service stopped; take a backup first)
            [--dry-run]       report what would go, change nothing
            [--grace SECS]    keep lines that expired less than SECS ago (default 86400)
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from .config import load_settings


def _service_running(s) -> bool:
    import urllib.request

    try:
        with urllib.request.urlopen(f"http://{s.host}:{s.port}/health", timeout=1) as r:
            return r.status == 200
    except OSError:
        return False


def _rebuild(s) -> int:
    for suffix in ("", "-wal", "-shm"):
        p = s.db_path.with_name(s.db_path.name + suffix)
        if p.exists():
            p.unlink()
    from .store import Store

    store = Store(s)
    n = store.r.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
    store.close()
    return n


def _refuse_while_serving(s) -> bool:
    if _service_running(s):
        print(f"refusing: bboard is serving on {s.host}:{s.port} and must be the only writer.\n"
              f"stop it first:  sudo systemctl stop bboard")
        return True
    return False


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    cmd = argv[0]
    s = load_settings()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if cmd == "serve":
        from .server import serve

        asyncio.run(serve(s))
    elif cmd == "rebuild":
        if _refuse_while_serving(s):
            return 1
        print(f"rebuilt: {_rebuild(s)} live posts")
    elif cmd == "prune":
        import time

        from .prune import PruneError, prune_logs

        ap = argparse.ArgumentParser(prog="bboard prune")
        ap.add_argument("--dry-run", action="store_true")
        ap.add_argument("--grace", type=int, default=86400, metavar="SECS")
        a = ap.parse_args(argv[1:])
        if _refuse_while_serving(s):
            return 1
        try:
            report = prune_logs(s, time.time(), grace=a.grace, dry_run=a.dry_run)
        except PruneError as e:
            print(f"refusing: {e}")
            return 1
        print(("would prune: " if a.dry_run else "pruned: ") + str(report))
        if not a.dry_run:
            print(f"rebuilt: {_rebuild(s)} live posts")
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
