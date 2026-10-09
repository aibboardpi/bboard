#!/usr/bin/env bash
# Install or update bboard on a Raspberry Pi (Raspberry Pi OS Lite 64-bit, Bookworm or newer).
# Step-by-step runbook (flashing -> Tailscale -> backups): deploy/PI_SETUP.md
#
#   sudo bash ~/bboard-src/deploy/setup_pi.sh ~/bboard-src
#
# <src> holds the repo's files (a `git archive` of a commit, see PI_SETUP.md step 4, or a clone). Re-run it the same
# way to ship code or group changes: it replaces the code, refreshes the pinned deps and the systemd
# unit, then restarts. It never touches the posts (data/) or the board id and SQLite mirror (state/).
#
# Layout:
#   /srv/bboard         root 0755
#   /srv/bboard/app     code + groups/                      root-owned: read-only to the service
#   /srv/bboard/venv    Python venv                         root-owned: read-only to the service
#   /srv/bboard/data    log-YYYY-MM.ndjson, the posts       user `bboard` (back this up)
#   /srv/bboard/state   SQLite mirror + board.id            user `bboard`
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

BASE=/srv/bboard
APP=$BASE/app
VENV=$BASE/venv

log() { echo "== $*"; }
die() { echo "!! $*" >&2; exit 1; }

main() {
  [ "$(id -u)" -eq 0 ] || die "run with sudo"
  local SRC
  SRC="$(cd "${1:?usage: sudo bash setup_pi.sh <src-dir>}" && pwd)"
  cd /  # the service user cannot enter a 0700 home dir
  [ -f "$SRC/bboard/__init__.py" ] && [ -f "$SRC/requirements.lock" ] || die "$SRC is not the bboard folder"
  [ "$SRC" != "$APP" ] || die "pass a source copy, not $APP itself"

  [ "$(uname -m)" = aarch64 ] || echo "!! $(uname -m): a 64-bit OS (aarch64) is recommended; the pinned wheels are for aarch64"
  if [ "$(timedatectl show -p NTPSynchronized --value 2>/dev/null)" != yes ]; then
    echo "!! clock is not NTP-synchronized yet; signed posts must be within 5 min of server time"
  fi

  log "packages"
  apt-get update -qq
  apt-get install -y -qq python3-venv sqlite3 curl >/dev/null
  python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))' || die "need Python >= 3.11 (got $(python3 -V))"

  log "service user + directories"
  getent group bboard >/dev/null || groupadd --system bboard
  id bboard >/dev/null 2>&1 || useradd --system --gid bboard --home-dir $BASE --shell /usr/sbin/nologin bboard
  install -d -o root -g root -m 0755 $BASE
  install -d -o bboard -g bboard -m 0750 $BASE/data $BASE/state

  log "python venv (pinned, hash-checked)"
  [ -x $VENV/bin/python ] || python3 -m venv $VENV
  $VENV/bin/pip install -q --no-cache-dir --disable-pip-version-check --require-hashes -r "$SRC/requirements.lock"

  log "code"
  local NEW=$BASE/app.new
  rm -rf $NEW
  install -d -m 0755 $NEW
  tar -C "$SRC" --exclude=./.git --exclude=./data --exclude=./state --exclude=./.venv --exclude=./venv \
      --exclude=__pycache__ --exclude=./.pytest_cache -cf - . | tar -C $NEW --no-same-owner -xf -
  sed -i 's/\r$//' $NEW/deploy/*.sh $NEW/deploy/*.service  # guard against CRLF from Windows editors
  chown -R root:root $NEW
  chmod -R u=rwX,go=rX $NEW
  $VENV/bin/python -m compileall -q $NEW/bboard  # the service can't write __pycache__ itself

  systemctl stop bboard 2>/dev/null || true
  rm -rf $BASE/app.old
  [ -d $APP ] && mv $APP $BASE/app.old
  mv $NEW $APP
  rm -rf $BASE/app.old

  log "systemd"
  install -m 0644 $APP/deploy/bboard.service /etc/systemd/system/bboard.service
  systemctl daemon-reload
  systemctl enable -q bboard.service
  systemctl restart bboard.service

  local i
  for i in $(seq 1 30); do
    curl -fsS http://127.0.0.1:8000/health >/dev/null 2>&1 && break
    sleep 1
  done
  if curl -fsS http://127.0.0.1:8000/health >/dev/null 2>&1; then
    log "board is up on 127.0.0.1:8000 (groups: $(cd $APP/groups && ls [!_]*.md | sed 's/\.md$//' | tr '\n' ' '))"
  else
    journalctl -u bboard --no-pager -n 30 || true
    die "the board did not come up; see the log above"
  fi

  cat <<EOF

Next:
  sudo bash $APP/deploy/verify_pi.sh --post           # end-to-end check
  sudo bash $APP/deploy/tailscale_pi.sh [--private]   # publish https://$(hostname).<tailnet>.ts.net (Funnel)
EOF
}

main "$@"
