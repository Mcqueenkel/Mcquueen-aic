#!/usr/bin/env bash
# Read-only status for the AIC idle farmer. Counterpart of /opt/umabot/status_sweep.sh.
#
#   ssh root@<vps> "bash /opt/aic/status_aic.sh"
#
# Touches nothing: systemctl queries, the log AIC already writes, and the fan-cap store.
set -u

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT="${AIC_UNIT:-aic}"
PROFILE="${AIC_PROFILE:-aic}"
STATE="$HERE/state/$PROFILE"
LOG="${AIC_LOG:-$STATE/logs/aic.log}"

echo "=============================================================="
echo " AIC idle farmer - $(date -u '+%Y-%m-%d %H:%M:%S UTC')"
echo "=============================================================="
echo
echo "-- unit ------------------------------------------------------"
systemctl is-active "$UNIT" 2>/dev/null || true
systemctl is-enabled "$UNIT" 2>/dev/null || true
systemctl show "$UNIT" -p ActiveEnterTimestamp -p NRestarts 2>/dev/null || true
echo
echo "  health timer: $(systemctl is-active "${UNIT}-health.timer" 2>/dev/null || echo 'not installed')"
echo "  last verdict: $(systemctl is-failed "${UNIT}-health.service" 2>/dev/null || true)  (failed = wedged, that is deliberate)"
echo

echo "-- fans today ------------------------------------------------"
# The game-day rolls at 15:00 UTC, so shift back 15h to get the current key -
# the same fold aic/fancap.py:day_key uses.
DAY="$(date -u -d '15 hours ago' '+%Y-%m-%d' 2>/dev/null || date -u '+%Y-%m-%d')"
echo "  game-day        $DAY  (rolls at 15:00 UTC)"
echo "  target          ${AIC_FAN_TARGET:-<unset - no cap>}"
echo "  stop margin     ${AIC_FAN_STOP_MARGIN:-1100000 (default)}"
if [ -f "$STATE/fan_day.json" ]; then
    echo "  baselines       $STATE/fan_day.json"
    sed -e 's/^/    /' "$STATE/fan_day.json"
else
    echo "  baselines       none recorded yet ($STATE/fan_day.json absent)"
fi
echo

echo "-- career clocks ---------------------------------------------"
# What the rotation schedules on: when each account's career started, when it
# finishes, and when that account is next due. Written by aic/careerclock.py.
if [ -f "$STATE/career_clock.json" ]; then
    sed -e 's/^/  /' "$STATE/career_clock.json"
else
    echo "  none recorded yet ($STATE/career_clock.json absent)"
    echo "  (only 'rotate run' writes these; 'aic run --loop' does not rotate)"
fi
echo

echo "-- career ----------------------------------------------------"
if [ -f "$LOG" ]; then
    echo "  log             $LOG"
    echo "  size            $(du -h "$LOG" | cut -f1), last written $(date -u -r "$LOG" '+%H:%M:%S UTC')"
    echo
    echo "  last banked:"
    grep -a "Banked" "$LOG" | tail -3 | sed -e 's/^/    /'
    echo "  last start:"
    grep -aE "New career finishes in|Idle career finishes in" "$LOG" | tail -2 | sed -e 's/^/    /'
    echo "  recent errors (last 5):"
    grep -aE " ERROR | CRITICAL " "$LOG" | tail -5 | sed -e 's/^/    /'
    echo
    echo "-- last 15 log lines -----------------------------------------"
    tail -15 "$LOG" | sed -e 's/^/  /'
else
    echo "  no log at $LOG"
    echo "  (wrong profile? this script reads AIC_PROFILE, currently '$PROFILE')"
fi
echo
echo "-- reminder --------------------------------------------------"
echo "  AIC and umabot must never hold the SAME in-game account at once."
echo "  umabot: $(systemctl is-active umabot 2>/dev/null || echo unknown)"
echo "=============================================================="
