# Rebuilding the farm VPS from this laptop

The VPS (`ssh -p <port> root@<VPS-IP>`, Ubuntu 22.04) runs **only AIC** - no
Icarus bot, web UI or tunnel. It was wiped once (2026-10-04) and rebuilt from
this folder plus the private backup beside it. Everything needed is here:

| what | where on the laptop | where on the VPS |
|---|---|---|
| code | `AutoIndependentCareer-dist/` (this project) | `/opt/aic` |
| roster (14 accounts) | `reroll_accounts.json` | `/opt/aic/reroll_accounts.json` (600) |
| service units | `deploy/vps/aic.service`, `aic-health.{service,timer}` | `/etc/systemd/system/` |
| journal cap | `deploy/vps/journald-aic.conf` | `/etc/systemd/journald.conf.d/aic.conf` |
| settings + webhooks | `../aic-vps-backup/umabot.env` | `/etc/umabot.env` (600 root) |
| game login + Steam token | `../aic-vps-backup/auth_config.json` | `/opt/aic/runtime/aic/auth_config.json` (600 umabot) |
| link passwords (15) | `../aic-vps-backup/accounts.json` | `/opt/aic/state/accounts.json` (600 umabot) |
| game data | `../aic-vps-backup/support_list.json` + the other `data/*.json` from `OneDrive/Documents/BOt dumps/Mcqueen Uma auto/data` | `/opt/umabot/data/` |
| game database | the installed game's `.../Persistent/master/master.mdb` | `/opt/umabot/master.mdb` |
| Steam library | `../lmao-repo-linux-port/node_modules` | `/opt/umabot/node_modules` |

`aic-vps-backup/` holds secrets (Steam token, webhooks, link passwords). Keep it
off shared drives.

## Steps (as root)

1. Base: `useradd -m -s /bin/bash umabot`; Node 20 (`curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && apt-get install -y nodejs`);
   as umabot, `cd ~`, install uv, then
   `uv venv --python 3.12 ~/.venvs/aic && uv pip install --python ~/.venvs/aic/bin/python curl_cffi==0.7.4 msgpack==1.1.0 pycryptodome==3.14.1`.
2. Copy the project to `/opt/aic`, the data/mdb/node_modules to `/opt/umabot`, the backup files to the
   paths above; `chown -R umabot:umabot /opt/aic /opt/umabot`; set the modes shown.
3. **Strip Windows line endings** (the units fail silently otherwise):
   `sed -i 's/\r$//' /opt/aic/*.sh /opt/aic/scripts/*.sh /etc/systemd/system/aic*.service /etc/systemd/system/aic-health.timer`.
4. `ln -sfn /opt/umabot/node_modules /opt/aic/vendor/node_modules`.
5. `systemctl daemon-reload && systemctl enable --now aic aic-health.timer && systemctl restart systemd-journald`.
6. Check: `tail -f /opt/aic/state/aic/logs/aic.log` - visits take over each account by link password.

If the Steam token in the backup has expired, the user runs once on the VPS:
`sudo -u umabot -H bash /opt/aic/scripts/steam_first_login.sh` (Steam password + Guard code for the farm's Steam account).

7. Swap (1.9 GB RAM): `fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile`,
   plus `/swapfile none swap sw 0 0` in `/etc/fstab`.

## Discord control bot (optional, `scripts/discord_bot.py`)

1. `mkdir -p /opt/aic-discord && cd /opt/aic-discord && /home/umabot/.local/bin/uv venv --python 3.12 venv &&
   /home/umabot/.local/bin/uv pip install --python venv/bin/python discord.py==2.4.0`.
2. Claude Code **pinned to 2.1.112**: `npm install -g @anthropic-ai/claude-code@2.1.112`. This VPS's
   "Common KVM processor" has no SSE4.2/AVX, and every later build is a native binary that just
   hangs on it; 2.1.112 is the last one that runs on Node.
3. `bash /opt/aic/scripts/discord_setup.sh`: creates `claudeops` (passwordless sudo), installs
   Claude's rules (`deploy/vps/claude-ops/CLAUDE.md` -> `/home/claudeops/.claude/`), a root-owned copy
   of the bot in `/opt/aic-discord/`, the unit, and asks for the bot token (hidden), the owner's
   Discord user ID and the private channel ID -> `/etc/aic-discord.env` (600). Re-run it after
   changing the bot code; `--reconfigure` asks again.
4. Log Claude in once: `sudo -u claudeops -H claude` -> `/login`, then `/exit`.
5. In the channel: `!help`, `!status`, `!ask new hello`.

## Rules

* **Never `systemctl stop aic`.** A change is: copy `/opt/aic` to `/tmp`, overlay it, run the tests there
  (`AIC_ICARUS_ROOT=/opt/umabot`), install only if green (back up replaced files), then ONE
  `systemctl restart aic` while the log's last line is `Next visit in ...`.
* Patch day: set `app_ver` in `auth_config.json` to the game's new version, refresh `master.mdb`, restart once.
