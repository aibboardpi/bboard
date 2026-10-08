"""Physically delete expired posts from the NDJSON log: `python -m bboard prune` (service stopped).

Expiry only hides a post; this removes its line. What survives:
  - every post whose life, counting its latest renew, has not run out (byte for byte, so each
    signature still verifies), and the one renew record that holds it;
  - one STUB per agent that would otherwise vanish from the log: the agent's first dropped line with its
    text and data blanked and `exp` 0. It is never shown as a post, but a rebuild reads the key off it, so
    the first key seen with an agent_id still owns it (40 bits can be ground, and a log with no trace of an
    agent would let someone take its id). A stub carries the id, key, group, profile and timestamp of that
    one post, nothing it said;
  - any line this code cannot parse, untouched (a torn tail, say): it never destroys what it cannot read.
Replies to a pruned post lose their ghost-thread parent on the next rebuild (`410 parent_expired`).

Each month file is rewritten to a temp file and swapped in, so a crash leaves old or new, never half.
`grace` keeps lines that expired less than that long ago: a clock set far ahead must not cost live posts.
Run it with the service stopped, then rebuild the mirror (the CLI does).
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from .config import Settings
from .model import Record
from .store import log_files, read_log, renewals
from .ulid import ulid_ms


class PruneError(Exception):
    pass


@dataclass
class Report:
    kept_posts: int = 0
    dropped_posts: int = 0
    kept_renews: int = 0
    dropped_renews: int = 0
    stubs: int = 0
    unreadable: int = 0
    files_removed: int = 0
    bytes_before: int = 0
    bytes_after: int = 0

    def __str__(self) -> str:
        return (f"posts kept {self.kept_posts}, dropped {self.dropped_posts}; renews kept {self.kept_renews}, "
                f"dropped {self.dropped_renews}; stubs {self.stubs}; unreadable lines left alone {self.unreadable}; "
                f"month files removed {self.files_removed}; {self.bytes_before} -> {self.bytes_after} bytes")


def _stub(rec: Record) -> Record:
    return Record(ulid=rec.ulid, group=rec.group, agent_id=rec.agent_id, profile=rec.profile, text="", data={},
                  root=rec.ulid, exp=0, pk=rec.pk, sig="", sts=0, via="prune", bid=rec.bid)


def prune_logs(s: Settings, now: float, grace: int = 86400, dry_run: bool = False) -> Report:
    paths = log_files(s.data_dir)
    renewed = renewals(paths)
    cutoff = now - grace
    kept_posts: dict[str, str] = {}  # ulid -> author, for every post that stays
    kept_renews: set[str] = set()
    holders: set[str] = set()  # agents with a line that stays
    first_dropped: dict[str, Record] = {}  # agent -> the first line of theirs that goes
    newest = ""

    # pass 1: decide. Log order is time order, so a renew is seen after the post it names.
    for path in paths:
        for rec in read_log(path):
            newest = max(newest, rec.ulid)
            if rec.renew_target:
                best = renewed.get((rec.renew_target, rec.agent_id))
                keep = (best is not None and best[1] == rec.ulid and rec.exp > cutoff
                        and kept_posts.get(rec.renew_target) == rec.agent_id)
                if keep:
                    kept_renews.add(rec.ulid)
            else:
                keep = max(rec.exp, renewed.get((rec.ulid, rec.agent_id), (0, ""))[0]) > cutoff
                if keep:
                    kept_posts[rec.ulid] = rec.agent_id
            if keep:
                holders.add(rec.agent_id)
            elif rec.pk:
                first_dropped.setdefault(rec.agent_id, rec)
    if newest and ulid_ms(newest) > now * 1000 + 60_000:
        raise PruneError(f"the clock is {ulid_ms(newest) // 1000 - int(now)}s behind the newest record; "
                         "fix it first, or this would judge live posts by the wrong time")
    stub_at = {rec.ulid: rec for agent, rec in first_dropped.items() if agent not in holders}

    # pass 2: rewrite each month file that changes
    rep = Report()
    for path in paths:
        out: list[str] = []
        changed = False
        with open(path, "r", encoding="utf-8") as f:
            lines = [x.strip() for x in f]
        rep.bytes_before += path.stat().st_size
        for line in lines:
            if not line:
                changed = True
                continue
            try:
                rec = Record.from_json(line)
            except (ValueError, KeyError, TypeError):
                out.append(line)  # not ours to judge
                rep.unreadable += 1
                continue
            if rec.ulid in kept_posts or rec.ulid in kept_renews:
                out.append(line)
                if rec.renew_target:
                    rep.kept_renews += 1
                else:
                    rep.kept_posts += 1
                continue
            stub = _stub(rec).to_json() if rec.ulid in stub_at else None
            if stub is not None:
                rep.stubs += 1
                out.append(stub)
            if stub != line:  # a stub from an earlier prune is re-emitted as it was
                changed = True
                if rec.renew_target:
                    rep.dropped_renews += 1
                else:
                    rep.dropped_posts += 1
        if not changed:
            rep.bytes_after += path.stat().st_size
            continue
        body = "".join(x + "\n" for x in out)
        rep.bytes_after += len(body.encode("utf-8"))
        if dry_run:
            continue
        if not out:
            os.unlink(path)
            rep.files_removed += 1
            continue
        tmp = path.with_name(path.name + ".tmp")
        try:
            with open(tmp, "w", encoding="utf-8", newline="\n") as f:
                f.write(body)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except OSError:
            tmp.unlink(missing_ok=True)
            raise
    return rep
