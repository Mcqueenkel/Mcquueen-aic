#!/bin/bash
# Turn the game-API proxy relay on or off (README "Proxy relay"). Run as root.
#
#   sudo bash /opt/aic/scripts/proxy_setup.sh          # on, but only if the game refuses this server
#   sudo bash /opt/aic/scripts/proxy_setup.sh --force  # on even if the game answers directly
#   sudo bash /opt/aic/scripts/proxy_setup.sh --off    # off: the farm talks to the game directly
#
# It never stops or restarts aic.service: if the farm is already running, restart it once
# yourself while the log's last line is "Next visit in ..." (README "Day to day").
set -e
cd "$(dirname "$0")/.."
[ "$(id -u)" = 0 ] || { echo "run as root (sudo)"; exit 1; }
U=/etc/systemd/system
GAME=https://api.games.umamusume.com/

if [ "$1" = "--off" ]; then
  systemctl disable --now aic-proxy 2>/dev/null || true
  rm -f "$U/aic.service.d/proxy.conf" "$U/aic-proxy.service"
  systemctl daemon-reload
  echo "Proxy relay off. Restart the farm once so it talks to the game directly."
  exit 0
fi

code=$(curl -s -o /dev/null -m 15 -w '%{http_code}' "$GAME" || true)
if [ "$code" = 404 ] && [ "$1" != "--force" ]; then
  echo "The game answers this server directly (404): no proxy needed. (--force turns it on anyway.)"
  exit 0
fi
echo "Direct to the game: $code (404 = answers). Turning the proxy relay on."

list=$(grep -E '^AIC_PROXY_LIST=' /etc/umabot.env 2>/dev/null | tail -1 | cut -d= -f2-)
if [ -n "$list" ]; then
  [ -r "$list" ] || { echo "AIC_PROXY_LIST=$list in /etc/umabot.env, but that file is missing"; exit 1; }
  chown root:umabot "$list"; chmod 640 "$list"
  echo "Using your own proxies from $list."
else
  echo "Using the free public proxy list (AIC_PROXY_LIST in /etc/umabot.env sets your own)."
fi

cmp -s deploy/vps/aic-proxy-dropin.conf "$U/aic.service.d/proxy.conf" && same=1 || same=
install -m 644 deploy/vps/aic-proxy.service "$U/aic-proxy.service"
mkdir -p "$U/aic.service.d"
install -m 644 deploy/vps/aic-proxy-dropin.conf "$U/aic.service.d/proxy.conf"
mkdir -p /opt/aic/state/aic && chown umabot:umabot /opt/aic/state /opt/aic/state/aic
systemctl daemon-reload
systemctl enable aic-proxy >/dev/null 2>&1
systemctl restart aic-proxy

echo "Waiting for a working proxy (up to 3 minutes)..."
for i in $(seq 1 60); do
  code=$(curl -s -o /dev/null -m 15 -x http://127.0.0.1:8899 -w '%{http_code}' "$GAME" || true)
  [ "$code" = 404 ] && break
  sleep 3
done
if [ "$code" = 404 ]; then
  echo "OK: the game answers through the relay."
  [ -z "$same" ] && systemctl is-active -q aic && echo "The farm is running: restart it once (at a 'Next visit in' line) to use the relay." || true
else
  echo "No answer through the relay yet ($code). Watch it with: journalctl -u aic-proxy -f"
  exit 1
fi
