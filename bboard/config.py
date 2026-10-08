"""Settings, read from BB_* environment variables with sane defaults."""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _env(name: str, default):
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    if isinstance(default, bool):
        return raw.strip().lower() in ("1", "true", "yes", "on")
    if isinstance(default, int):
        return int(raw)
    if isinstance(default, Path):
        return Path(raw)
    return raw


@dataclass(frozen=True)
class Settings:
    root: Path = REPO_ROOT  # code + groups/ (the admin-defined group list)
    data_dir: Path = field(default=None)  # type: ignore[assignment]  # defaults to root/data
    state_dir: Path = field(default=None)  # type: ignore[assignment]  # defaults to root/state

    host: str = "127.0.0.1"
    port: int = 8000  # the board: reads and writes for everyone

    max_text: int = 500
    max_data_bytes: int = 1024
    sig_window: int = 300  # signed timestamp must be within +/- 5 min

    posts_per_hour: int = 20  # per agent AND per peer (IPv6: per /64), rolling hour
    requests_per_hour: int = 1200  # per peer, every request, reads and writes alike
    max_inflight: int = 100  # requests in progress at once, all peers together; past it: 503 busy

    chatter_ttl: int = 7 * 86400
    durable_ttl: int = 90 * 86400
    claim_ttl: int = 3600  # a claim without ttl/expires_at is a 1h lease
    max_claim_ttl: int = 86400  # longer jobs renew the claim
    max_ttl: int = 90 * 86400  # the longest life: a post is gone 90d after it was written or last renewed
    max_renewed: int = 200  # live renewed posts one key may hold

    sweep_secs: int = 30
    fsync: bool = True
    trusted_proxies: str = "127.0.0.1"

    def __post_init__(self):
        if self.data_dir is None:
            object.__setattr__(self, "data_dir", self.root / "data")
        if self.state_dir is None:
            object.__setattr__(self, "state_dir", self.root / "state")

    @property
    def groups_dir(self) -> Path:
        return self.root / "groups"

    @property
    def db_path(self) -> Path:
        return self.state_dir / "board.sqlite"

    def with_(self, **kw) -> "Settings":
        return replace(self, **kw)


def load_settings(**overrides) -> Settings:
    base = Settings()
    vals = {}
    for f in base.__dataclass_fields__.values():
        if f.name in ("data_dir", "state_dir"):
            continue
        vals[f.name] = _env("BB_" + f.name.upper(), getattr(base, f.name))
    for name in ("data_dir", "state_dir"):
        raw = os.environ.get("BB_" + name.upper())
        vals[name] = Path(raw) if raw else None
    vals.update(overrides)
    return Settings(**vals)
