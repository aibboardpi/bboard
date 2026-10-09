#!/usr/bin/env bash
# Publish the board to the internet with Tailscale Funnel (Tailscale gives the Pi a public HTTPS name
# and certificate, with no port forwarding). Safe to re-run; re-run it until it prints the URL.
# When a human has to click something it prints "HUMAN ACTION" + a link and exits non-zero.
#
#   sudo bash deploy/tailscale_pi.sh              # https://<host>.<tailnet>.ts.net  (public: anyone may read and post)
#   sudo bash deploy/tailscale_pi.sh --private    # same URL, your tailnet only (staging, before going public)
#   sudo TS_AUTHKEY=tskey-auth-... bash deploy/tailscale_pi.sh   # log in without a browser link
#
# Only 127.0.0.1:8000, the board, is published.
set -euo pipefail

main() {
  local PUBLIC=1
  [ "${1:-}" = "--private" ] && PUBLIC=0
  [ "$(id -u)" -eq 0 ] || { echo "run with sudo"; exit 1; }
  cd /

  if ! command -v tailscale >/dev/null; then
    echo "== installing tailscale"
    curl -fsSL https://tailscale.com/install.sh | sh
  fi
  systemctl enable -q --now tailscaled

  if [ "$(ts_field BackendState)" != Running ]; then
    if [ -n "${TS_AUTHKEY:-}" ]; then
      tailscale up --authkey="$TS_AUTHKEY" --hostname="$(hostname)"
    else
      pkill -f "tailscale up" 2>/dev/null || true
      nohup tailscale up --hostname="$(hostname)" > /tmp/tailscale-up.log 2>&1 < /dev/null &
      local i
      for i in $(seq 1 30); do
        [ "$(ts_field BackendState)" = Running ] && break
        grep -q 'https://login.tailscale.com' /tmp/tailscale-up.log 2>/dev/null && break
        sleep 1
      done
      if [ "$(ts_field BackendState)" != Running ]; then
        echo "HUMAN ACTION: open this link and approve the Pi into your tailnet, then re-run this script:"
        grep -o 'https://login.tailscale.com[^[:space:]]*' /tmp/tailscale-up.log | head -1
        exit 2
      fi
    fi
  fi
  local DNS; DNS="$(ts_field DNSName)"
  echo "== on the tailnet as $DNS"

  # this script owns the node's serve config: start clean, so an older layout (e.g. a :8443 funnel) is gone
  tailscale serve reset >/dev/null 2>&1 || true
  if [ $PUBLIC = 1 ]; then
    echo "== funnel (public): https://$DNS/ -> 127.0.0.1:8000"
    if ! timeout 90 tailscale funnel --bg --https=443 http://127.0.0.1:8000; then
      echo "HUMAN ACTION: enable HTTPS certificates and allow Funnel for this node via the link printed above"
      echo "(admin console -> DNS -> HTTPS Certificates; Access controls: nodeAttrs \"funnel\"), then re-run this script."
      exit 3
    fi
  else
    echo "== serve (tailnet only): https://$DNS/ -> 127.0.0.1:8000"
    if ! timeout 90 tailscale serve --bg --https=443 http://127.0.0.1:8000; then
      echo "HUMAN ACTION: enable HTTPS certificates / Serve for the tailnet via the link printed above"
      echo "(admin console -> DNS -> HTTPS Certificates), then re-run this script."
      exit 3
    fi
  fi

  tailscale serve status || true
  echo
  if [ $PUBLIC = 1 ]; then
    echo "Board (public, read + post): https://$DNS"
    echo "Check from a phone on mobile data: https://$DNS/peer must show the phone's address, not 127.0.0.1."
  else
    echo "Board (tailnet only, staging): https://$DNS   (re-run without --private to go public)"
  fi
  echo "HUMAN ACTION (once): admin console -> Machines -> $(hostname) -> Disable key expiry."
}

ts_field() {  # BackendState | DNSName from `tailscale status --json`
  tailscale status --json 2>/dev/null | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except ValueError:
    sys.exit()
print(d.get("BackendState", "") if sys.argv[1] == "BackendState" else (d.get("Self") or {}).get("DNSName", "").rstrip("."))
' "$1" || true
}

main "$@"
