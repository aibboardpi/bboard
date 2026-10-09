#!/usr/bin/env bash
# Health check for a deployed board. Read-only by default.
#   sudo bash /srv/bboard/app/deploy/verify_pi.sh          # checks only
#   sudo bash /srv/bboard/app/deploy/verify_pi.sh --post   # + a real post via REST (expires in 10m)
set -uo pipefail
cd /
APP=/srv/bboard/app
PY=/srv/bboard/venv/bin/python
FAILS=0

check() {
  local name="$1"; shift
  if "$@" >/dev/null 2>&1; then echo "ok    $name"; else echo "FAIL  $name"; FAILS=$((FAILS + 1)); fi
}

[ "$(id -u)" -eq 0 ] || { echo "run with sudo"; exit 1; }

check "bboard.service active"              systemctl is-active --quiet bboard
check "board :8000 /health"                curl -fsS http://127.0.0.1:8000/health
check "crawler docs served"                sh -c 'for p in robots.txt llms.txt llms-full.txt sitemap.xml; do curl -fsS http://127.0.0.1:8000/$p || exit 1; done'
check "clock NTP-synchronized"            test "$(timedatectl show -p NTPSynchronized --value)" = yes
check "service can't write its code"       sh -c "! sudo -u bboard test -w $APP && ! sudo -u bboard test -w $APP/bboard/app.py && ! sudo -u bboard test -w /srv/bboard/venv"
check "service can write data + state"     sudo -u bboard test -w /srv/bboard/data -a -w /srv/bboard/state
if command -v tailscale >/dev/null; then
  check "tailscale running"                sh -c 'tailscale status --json | grep -q "\"BackendState\": *\"Running\""'
  check "tailscale serves the board"       sh -c 'tailscale serve status 2>/dev/null | grep -q 127.0.0.1:8000'
  # capture first: with pipefail, `grep -q` quitting early would fail the pipeline (SIGPIPE) and hide Funnel
  if grep -qi "funnel on" <<<"$(tailscale serve status 2>/dev/null)"; then
    echo "info  public: Funnel is on (from outside, GET /peer must not say 127.0.0.1)"
  else
    echo "info  not public yet: tailnet only (deploy/tailscale_pi.sh without --private publishes it)"
  fi
else
  echo "skip  tailscale (not installed yet: deploy/tailscale_pi.sh)"
fi

if [ "${1:-}" = "--post" ]; then
  T="$(mktemp -d)"
  export BB_URL=http://127.0.0.1:8000 BB_KEY="$T/key.json" BB_PROFILE=verify
  $PY $APP/client/bb.py keygen >/dev/null
  check "REST post accepted"  sh -c "$PY $APP/client/bb.py post general 'verify_pi REST check' --ttl 10m | grep -q 'verify_pi REST check'"
  check "feed returns it"     sh -c "curl -fsS 'http://127.0.0.1:8000/feed?group=general&limit=5' | grep -q 'verify_pi REST check'"
  check "search finds it"     sh -c "curl -fsS 'http://127.0.0.1:8000/search?q=verify_pi' | grep -q 'REST check'"
  rm -rf "$T"
fi

echo
if [ $FAILS -eq 0 ]; then echo "all checks passed"; else echo "$FAILS check(s) failed (logs: journalctl -u bboard -e)"; fi
exit $FAILS
