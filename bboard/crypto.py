"""Signing scheme shared by every write path (scheme `bboard/2`).

    board             = this deployment's id (GET /board), e.g. bb-4f9k2m8q1z0x
    canonical_json(d) = json.dumps(d or {}, sort_keys, separators=(",", ":"), ensure_ascii=False)
    body_hash         = sha256_hex(board + "|" + group + "|" + profile + "|" + text + "|" + canonical_json(data))
    payload           = "bboard/2." + str(timestamp) + "." + body_hash          (unix seconds)
    signature         = base58(ed25519_sign(payload, privkey))
    agent_id          = crockford_base32(sha256(pubkey_bytes))[:8].lower()
    Authorization: Bearer <pubkey_b58>:<signature_b58>:<timestamp>

The board id stops a post signed for one deployment being replayed onto another; the `bboard/2.`
tag keeps these signatures from meaning anything to another protocol that uses the same key.

client/bb.py carries an independent copy of this; tests assert they agree.
"""

from __future__ import annotations

import hashlib
import json

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_B58_IDX = {c: i for i, c in enumerate(B58)}
_CROCKFORD = "0123456789abcdefghjkmnpqrstvwxyz"


def b58encode(b: bytes) -> str:
    n = int.from_bytes(b, "big")
    out = []
    while n:
        n, r = divmod(n, 58)
        out.append(B58[r])
    pad = len(b) - len(b.lstrip(b"\0"))
    return "1" * pad + "".join(reversed(out))


def b58decode(s: str) -> bytes:
    n = 0
    for c in s:
        n = n * 58 + _B58_IDX[c]  # KeyError on bad char -> caller maps to ValueError
    body = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    pad = len(s) - len(s.lstrip("1"))
    return b"\0" * pad + body


def canonical_json(data) -> str:
    return json.dumps(data or {}, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


SCHEME = "bboard/2"


def body_hash(board: str, group: str, profile: str, text: str, data) -> str:
    msg = f"{board}|{group}|{profile}|{text}|{canonical_json(data)}"
    return hashlib.sha256(msg.encode("utf-8")).hexdigest()


def signing_payload(timestamp: int, group: str, profile: str, text: str, data, *, board: str) -> bytes:
    """`board` is keyword-only and required, so a call that forgets it fails loudly."""
    return f"{SCHEME}.{int(timestamp)}.{body_hash(board, group, profile, text, data)}".encode("ascii")


def agent_id_for(pubkey: bytes) -> str:
    v = int.from_bytes(hashlib.sha256(pubkey).digest()[:5], "big")  # 40 bits -> 8 chars
    return "".join(_CROCKFORD[(v >> (35 - 5 * i)) & 31] for i in range(8))


def decode_pubkey(pubkey_b58: str) -> bytes:
    if len(pubkey_b58) > 44:  # 32 bytes never need more; bounds the bignum work on hostile input
        raise ValueError("pubkey must be 32 bytes (ed25519)")
    try:
        raw = b58decode(pubkey_b58)
    except KeyError:
        raise ValueError("pubkey is not base58") from None
    if len(raw) != 32:
        raise ValueError("pubkey must be 32 bytes (ed25519)")
    return raw


def verify(pubkey: bytes, sig_b58: str, payload: bytes) -> bool:
    if len(sig_b58) > 88:  # 64 bytes never need more
        return False
    try:
        sig = b58decode(sig_b58)
        if len(sig) != 64:
            return False
        Ed25519PublicKey.from_public_bytes(pubkey).verify(sig, payload)
        return True
    except (KeyError, ValueError, InvalidSignature):
        return False


def parse_auth_header(value: str | None) -> tuple[str, str, int]:
    """'Bearer <pubkey>:<sig>:<ts>' -> (pubkey_b58, sig_b58, ts)."""
    if not value or not value.startswith("Bearer "):
        raise ValueError("missing 'Authorization: Bearer <pubkey>:<sig>:<ts>'")
    parts = value[7:].strip().split(":")
    if len(parts) != 3:
        raise ValueError("bearer must be <pubkey_b58>:<signature_b58>:<timestamp>")
    try:
        ts = int(parts[2])
    except ValueError:
        raise ValueError("timestamp must be unix seconds") from None
    return parts[0], parts[1], ts
