#!/bin/bash
# Install / update the AIC Discord control bot. Run as root on the VPS:
#   bash /opt/aic/scripts/discord_setup.sh                 first time: asks for the token + ids
#   bash /opt/aic/scripts/discord_setup.sh --reconfigure   ask for them again
# Any later run just reinstalls the code from /opt/aic and restarts the bot.
# The token is typed here (hidden) and written only to /etc/aic-discord.env (600 root).
# Never touches aic.service.
set -e
[ "$(id -u)" = 0 ] || { echo "run as root"; exit 1; }
SRC=/opt/aic
[ -x /opt/aic-discord/venv/bin/python ] || { echo "missing /opt/aic-discord/venv - see deploy/vps/RESTORE.md"; exit 1; }
command -v claude >/dev/null || echo "note: Claude Code is not installed yet - !ask will fail (see RESTORE.md)"

# Claude's user: full sudo, so !ask can fix anything on the box (that is the point of it)
id claudeops >/dev/null 2>&1 || useradd -m -s /bin/bash claudeops
echo "claudeops ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/claudeops.new
chmod 440 /etc/sudoers.d/claudeops.new
visudo -cqf /etc/sudoers.d/claudeops.new && mv /etc/sudoers.d/claudeops.new /etc/sudoers.d/claudeops
install -d -o claudeops -g claudeops -m 700 /home/claudeops/.claude /home/claudeops/aic-ops
install -o root -g root -m 644 "$SRC/deploy/vps/claude-ops/CLAUDE.md" /home/claudeops/.claude/CLAUDE.md

# root-owned copies: the bot is root and must not load anything umabot can write
install -d -o root -g root -m 755 /opt/aic-discord
install -o root -g root -m 644 "$SRC/scripts/discord_bot.py" /opt/aic-discord/discord_bot.py
sed 's/\r$//' "$SRC/deploy/vps/aic-discord.service" > /etc/systemd/system/aic-discord.service

if [ ! -s /etc/aic-discord.env ] || [ "$1" = --reconfigure ]; then
  read -rsp "Discord bot token: " TOKEN; echo
  read -rp  "Your Discord user ID (only you will be obeyed): " USERID
  read -rp  "Control channel ID (a private channel): " CHANID
  [ -n "$TOKEN" ] || { echo "no token given"; exit 1; }
  [[ "$USERID" =~ ^[0-9]{15,22}$ ]] || { echo "that user ID does not look right"; exit 1; }
  [[ "$CHANID" =~ ^[0-9]{15,22}$ ]] || { echo "that channel ID does not look right"; exit 1; }
  install -m 600 -o root -g root /dev/null /etc/aic-discord.env.new     # 600 before the token lands
  cat > /etc/aic-discord.env.new <<EOF
DISCORD_BOT_TOKEN=$TOKEN
CONTROL_AUTHORIZED_USERS=$USERID
CONTROL_ALLOWED_CHANNELS=$CHANID
CLAUDE_USER=claudeops
CLAUDE_WORKDIR=/home/claudeops/aic-ops
CLAUDE_TIMEOUT=1800
EOF
  mv /etc/aic-discord.env.new /etc/aic-discord.env
  unset TOKEN
fi

systemctl daemon-reload
systemctl enable aic-discord >/dev/null 2>&1
systemctl restart aic-discord
sleep 8
systemctl is-active aic-discord || true
journalctl -u aic-discord -n 8 --no-pager -o cat | grep -E "logged in|refusing|Error|Intents|bad Discord" || true
echo
echo "Developer Portal > your app > Bot: keep 'Public Bot' OFF and 'Message Content Intent' ON."
echo "Then type !help in your channel."
