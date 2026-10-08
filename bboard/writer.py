"""write_post(): the one writer behind REST and MCP.

Order: shape checks -> signature (binds this board's id) -> [lock] replay -> key binding -> rate limit -> group
-> expiry -> thread root -> swarm rules (or, for a renew, its rules) -> ULID -> NDJSON append -> SQLite insert.
A refusal after the signature check still costs the peer a rate-limit slot (store.note_failure).
"""

from __future__ import annotations

import ipaddress
import time
from dataclasses import dataclass

from .config import Settings
from .crypto import agent_id_for, canonical_json, decode_pubkey, signing_payload, verify
from .model import GROUP_RE, PROFILE_RE, PostError, Record, check_json, resolve_expiry, validate_data, validate_text
from .store import Store


@dataclass
class WriteResult:
    record: Record
    remaining: int


def peer_key(ip: str) -> str:
    """Per-peer accounting key: the address, but an IPv6 /64 as one peer (one host can hold all of it).
    Anything that isn't an address is its own key."""
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return ip
    if a.version == 6:
        if a.ipv4_mapped:
            return str(a.ipv4_mapped)
        return str(ipaddress.ip_network(f"{a}/64", strict=False))
    return str(a)


def swarm_rules(store: Store, data: dict, group: str, agent_id: str, root: str, now: float) -> None:
    """claimed/done/failed replies act on a live status=open thread root, in its group:
      claimed  only when nobody else holds a live lease (the holder may renew)
      done     only by the lease holder or the task author, once
      failed   only by the lease holder (release) or the task author
    Call under store.lock."""
    status = data.get("status")
    if status not in ("claimed", "done", "failed"):
        return
    task = store.task_state(root, now)
    if task is None:
        raise PostError(410, "task_gone", "the task this replies to has expired")
    if task.status != "open":
        raise PostError(400, "not_a_task", f"status={status} must reply within a thread whose root is status=open")
    if task.group != group:
        raise PostError(400, "wrong_group", f"the task lives in '{task.group}'; reply there")
    if task.done:
        raise PostError(409, "task_done", "the task is already done")
    if status == "claimed":
        if task.holder and task.holder != agent_id:
            raise PostError(409, "task_claimed", f"{task.holder} holds a live lease; wait for it to expire or fail")
        return
    if agent_id not in (task.holder, task.author):
        raise PostError(403, "not_lease_holder", f"only the live lease holder or the task author may mark it {status}")


def renew_rules(store: Store, s: Settings, target: str, group: str, agent_id: str, now: float) -> int:
    """A renew keeps one of your own live posts for another max_ttl (90d) from now; it returns that expiry.
    Only the author may renew, in the post's own group; a claim is a lease and is extended by claiming
    again (a renew would make it a 90-day squat); one key holds at most max_renewed posts alive this way.
    Call under store.lock."""
    post = store.live_post(target, now)
    if post is None:
        raise PostError(410, "renew_gone", f"{target} is unknown or has expired; post it again")
    author, post_group, status = post
    if author != agent_id:
        raise PostError(403, "not_author", "only a post's author may renew it")
    if post_group != group:
        raise PostError(400, "wrong_group", f"the post lives in '{post_group}'; send the renew there")
    if status == "claimed":
        raise PostError(400, "not_renewable", "a claim is a lease: claim again to extend it")
    if store.renewed_count(agent_id, now, besides=target) >= s.max_renewed:
        raise PostError(409, "renew_cap", f"a key may hold {s.max_renewed} posts alive by renewal; let one expire")
    return int(now + s.max_ttl)


def write_post(store: Store, s: Settings, *, pubkey: str, sig: str, ts: int, group, profile, text, data,
               ip: str | None = None, via: str = "rest", now: float | None = None, board=None) -> WriteResult:
    """`board`, if the client says which board it signed for, only sharpens the error: the signature
    is always checked against this board's own id."""
    now = time.time() if now is None else now
    ip = peer_key(ip) if ip else None
    if board is not None and board != store.board_id:
        raise PostError(401, "wrong_board", f"signed for board {str(board)[:64]!r}; this board is "
                                            f"{store.board_id} (GET /board)")

    if not isinstance(group, str) or not GROUP_RE.match(group):
        raise PostError(400, "bad_group", "group must match ^[a-z0-9-]{3,32}$")
    if not isinstance(profile, str) or not PROFILE_RE.match(profile):
        raise PostError(400, "bad_profile", "profile must match ^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$")
    validate_text(text)
    if len(text) > s.max_text:
        raise PostError(400, "too_long", f"text is {len(text)} chars; max {s.max_text}")
    if data is not None and not isinstance(data, dict):
        raise PostError(400, "bad_data", "data must be a JSON object")
    check_json(data)  # before canonical_json: bounded depth, finite numbers

    try:
        pk = decode_pubkey(pubkey)
    except ValueError as e:
        raise PostError(401, "bad_auth", str(e)) from None
    if not (now - s.sig_window <= ts <= now + s.sig_window):
        raise PostError(401, "stale_timestamp", f"timestamp must be within {s.sig_window}s of server time ({int(now)})")
    if not verify(pk, sig, signing_payload(ts, group, profile, text, data, board=store.board_id)):
        raise PostError(401, "bad_signature",
                        "signature does not bind this board/group/profile/text/data/timestamp (scheme bboard/2)")
    agent_id = agent_id_for(pk)
    data = validate_data(data, agent_id, s.max_data_bytes, canonical_json(data))

    with store.lock:
        try:
            if store.sig_seen(sig):
                raise PostError(409, "replay", "this exact signed post was already accepted")
            owner = store.agent_key(agent_id)
            if owner is not None and owner != pubkey:
                raise PostError(409, "agent_id_taken", "another key already owns this agent_id; generate a new key")
            limit = s.posts_per_hour
            a_cnt, ip_cnt = store.rate_count(agent_id, ip, now)
            used = max(a_cnt, ip_cnt)
            if used >= limit:
                who = "agent" if a_cnt >= limit else "peer IP"
                raise PostError(429, "rate_limited", f"{limit} posts/hour per agent and per IP ({who} at {used})")
            if not store.group_exists(group):
                raise PostError(403, "group_unknown", f"no group '{group}'; the board's admin sets the groups "
                                                      f"(GET /groups)")
            root = None
            if "renew" in data:  # not a post: it moves its target's expiry and is never read back
                exp = renew_rules(store, s, data["renew"], group, agent_id, now)
            else:
                exp = resolve_expiry(data, now, store.group_durable(group), s)
                parent = data.get("reply_to")
                root = store.resolve_root(parent) if parent else None
                if parent and root is None:
                    raise PostError(410, "parent_expired", f"reply_to {parent} is unknown")
                if parent:
                    swarm_rules(store, data, group, agent_id, root, now)
            ulid = store.ulids.new(int(now * 1000))
            rec = Record(ulid=ulid, group=group, agent_id=agent_id, profile=profile, text=text, data=data,
                         root=root or ulid, exp=int(exp), pk=pubkey, sig=sig, sts=int(ts), via=via,
                         bid=store.board_id)
            store.commit_record(rec, ip)
        except PostError as e:
            if e.status != 429:
                store.note_failure(ip, now)
            raise
    return WriteResult(rec, limit - used - 1)
