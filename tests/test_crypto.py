import os

import bb

from bboard import crypto
from bboard.model import dump_data, format_line
from bboard.ulid import ULIDGen, is_ulid, ulid_floor, ulid_ms


def test_client_and_server_schemes_agree():
    for n in (0, 1, 31, 32, 64):
        raw = b"\0\0" + os.urandom(n)
        assert bb.b58encode(raw) == crypto.b58encode(raw)
        assert crypto.b58decode(bb.b58encode(raw)) == raw
    data = {"z": 1, "a": "é|x", "lat": 50.3}
    assert bb.canonical_json(data) == crypto.canonical_json(data) == '{"a":"é|x","lat":50.3,"z":1}'
    assert crypto.canonical_json(None) == "{}"
    k = bb.Key(os.urandom(32))
    assert k.agent_id == crypto.agent_id_for(k.pub)
    assert len(k.agent_id) == 8 and k.agent_id.isalnum() and k.agent_id.islower()


def test_signature_binds_every_field():
    k = bb.Key(os.urandom(32))
    args = (1_760_000_000, "trail-reports", "scout", "creek is high", {"severity": "warn"})
    sig = k.sign(bb.signing_payload(*args, board="bb-one"))
    assert bb.signing_payload(*args, board="bb-one") == crypto.signing_payload(*args, board="bb-one")
    assert bb.signing_payload(*args, board="bb-one").startswith(b"bboard/2.1760000000.")
    pk = crypto.decode_pubkey(k.pubkey)
    assert crypto.verify(pk, sig, crypto.signing_payload(*args, board="bb-one"))
    assert not crypto.verify(pk, sig, crypto.signing_payload(*args, board="bb-two")), "board not bound"
    for i, alt in ((0, 1_760_000_001), (1, "other-group"), (2, "scout2"), (3, "creek is low"), (4, {"severity": "info"})):
        tampered = list(args)
        tampered[i] = alt
        assert not crypto.verify(pk, sig, crypto.signing_payload(*tampered, board="bb-one")), f"field {i} not bound"
    assert not crypto.verify(pk, "notbase58!", crypto.signing_payload(*args, board="bb-one"))


def test_auth_header_roundtrip():
    k = bb.Key(os.urandom(32))
    h = k.auth_header("general", "p", "hi", None, ts=123, board="bb-one")
    pub, sig, ts = crypto.parse_auth_header(h)
    assert pub == k.pubkey and ts == 123
    assert crypto.verify(crypto.decode_pubkey(pub), sig,
                         crypto.signing_payload(123, "general", "p", "hi", None, board="bb-one"))


def test_ulid_monotonic_and_floor():
    g = ULIDGen()
    ids = [g.new(1_700_000_000_000) for _ in range(1000)]
    assert ids == sorted(ids) and len(set(ids)) == 1000
    assert all(is_ulid(u) for u in ids)
    assert ulid_ms(ids[0]) == 1_700_000_000_000
    assert ulid_floor(1_700_000_000_000) < ids[0]
    assert g.new(1_600_000_000_000) > ids[-1]  # clock went backwards: still monotonic


def test_line_roundtrip_with_hostile_text():
    text = "a | b\nline2\\n not newline\ttab | {\"x\":1}"
    data = {"note": "pipe | inside", "reply_to": "01J0000000000000000000000Z"}
    line = format_line("01J0000000000000000000000A", "2026-10-06T00:00:00Z", "general", "abcd1234", "p",
                       text, dump_data(data))
    assert "\n" not in line
    rec = bb.parse_line(line)
    assert rec["text"] == text
    assert rec["data"] == data
    assert rec["agent_id"] == "abcd1234" and rec["group"] == "general"
