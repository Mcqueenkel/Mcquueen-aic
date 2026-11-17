# AutoIndependentCareer (`aic`)

Farms **Umamusume: Pretty Derby** (Global, Steam) on autopilot. One Steam account
rotates through many in-game accounts with Data Link, and for each account the farm:

- ends the finished Auto Training (idle) career and starts the next one (Unity Cup by default);
- stops the account for the day once it has gained 28M fans (the game day resets at 15:00 UTC);
- does the daily chores: gift box, club shoe requests and donations, Team Trials, daily races
  and Ace Runs;
- posts every finished career (fans, carats) to Discord.

It runs 24/7 as a systemd service on a small Linux VPS.

## Requirements

**Server**
- Ubuntu 22.04 or 24.04 with root SSH access
- 1 CPU, 2 GB RAM (plus 2 GB swap) and 5 GB of disk
- Python 3.12 and Node.js 20 (step 2 installs both)

**Windows PC** (for the first setup, and after game updates)
- The game installed through Steam
- [Python 3.12](https://www.python.org/downloads/) for the one-time login capture

**Accounts**
- One Steam account that owns the game; the farm signs in with it
- A Data Link (Trainer ID + Link Password) for every in-game account you want farmed
- Optional: a Discord webhook for career reports and alerts

**Files to bring.** Gather these before you start:

| File | Where it comes from | Where it goes on the server |
|---|---|---|
| `reroll_accounts.json` | you write it, see [Roster](#roster) | `/opt/aic/reroll_accounts.json` |
| `auth_config.json` | captured on the PC in step 1 | `/opt/aic/runtime/aic/auth_config.json` |
| game data: `support_list.json`, `chara_list.json`, `succession_relation.json`, `succession_relation_member.json` | your game-data backup folder | `/opt/umabot/data/` |
| `master.mdb` | on the PC: `Steam\steamapps\common\UmamusumePrettyDerby\UmamusumePrettyDerby_Data\Persistent\master\master.mdb` | `/opt/umabot/master.mdb` |

## Install

### 1. PC: capture the game login (once)

```powershell
git clone https://github.com/Mcqueenkel/Mcquueen-aic.git aic
cd aic
py -3.12 -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.\account.bat capture
```

`capture` starts the game and reads the login off it into `runtime\default\auth_config.json`.

### 2. Server: base system (as root)

```bash
fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
echo '/swapfile none swap sw 0 0' >> /etc/fstab

useradd -m -s /bin/bash umabot
curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
apt-get install -y nodejs git

sudo -u umabot -H bash -c 'cd ~ && curl -LsSf https://astral.sh/uv/install.sh | sh'
sudo -u umabot -H bash -c 'cd ~ && ~/.local/bin/uv venv --python 3.12 ~/.venvs/aic && ~/.local/bin/uv pip install --python ~/.venvs/aic/bin/python curl_cffi==0.7.4 msgpack==1.1.0 pycryptodome==3.14.1'

git clone https://github.com/Mcqueenkel/Mcquueen-aic.git /opt/aic
mkdir -p /opt/umabot /opt/aic/runtime/aic /opt/aic/state/aic
npm install --prefix /opt/umabot steam-user@5.3.0
```

The repository is private, so `git clone` asks for your GitHub user name and a
[personal access token](https://github.com/settings/tokens) as the password. Or copy the
project folder over with `scp` instead.

### 3. PC: copy your files to the server

Replace `PORT` and `SERVER`, and point the last two lines at your own folders:

```powershell
scp -P PORT reroll_accounts.json root@SERVER:/opt/aic/
scp -P PORT runtime\default\auth_config.json root@SERVER:/opt/aic/runtime/aic/
scp -P PORT -r "C:\path\to\your\data" root@SERVER:/opt/umabot/data
scp -P PORT "C:\Program Files (x86)\Steam\steamapps\common\UmamusumePrettyDerby\UmamusumePrettyDerby_Data\Persistent\master\master.mdb" root@SERVER:/opt/umabot/
```

### 4. Server: settings and permissions (as root)

```bash
cp /opt/aic/deploy/vps/umabot.env.example /etc/umabot.env
chmod 600 /etc/umabot.env
nano /etc/umabot.env        # paste your Discord webhook(s), or delete those two lines

ln -sfn /opt/umabot/node_modules /opt/aic/vendor/node_modules
chown -R umabot:umabot /opt/aic /opt/umabot
chmod 600 /opt/aic/reroll_accounts.json /opt/aic/runtime/aic/auth_config.json
```

### 5. Server: run the tests

```bash
cd /opt/aic && sudo -u umabot env AIC_DATA_ROOT=/opt/umabot AIC_MASTER_MDB=/opt/umabot/master.mdb \
  bash -c 'for t in tests/test_*.py; do /home/umabot/.venvs/aic/bin/python $t >/dev/null 2>&1 && echo "ok   $t" || echo "FAIL $t"; done'
```

Every line should say `ok`.

### 6. Server: Steam login (once)

```bash
sudo -u umabot -H bash /opt/aic/scripts/steam_first_login.sh
```

It asks for the Steam account name, its password and a Steam Guard code, then stores a
refresh token in `auth_config.json`, so the farm never needs them again. It ends with
`STEAM TOKEN STORED`.

### 7. Server: start the farm

```bash
cp /opt/aic/deploy/vps/aic.service /opt/aic/deploy/vps/aic-health.service /opt/aic/deploy/vps/aic-health.timer /etc/systemd/system/
mkdir -p /etc/systemd/journald.conf.d && cp /opt/aic/deploy/vps/journald-aic.conf /etc/systemd/journald.conf.d/aic.conf
systemctl daemon-reload && systemctl enable --now aic aic-health.timer && systemctl restart systemd-journald
tail -f /opt/aic/state/aic/logs/aic.log
```

Within a minute you should see `=== visit 1 | ...`, then `Transfer accepted` and
`Independent training started`.

## Roster

`/opt/aic/reroll_accounts.json` has one entry per account:

```json
[
  { "trainer_id": "123 456 789 012", "password": "LinkPassword1", "name": "farm-1", "trainee": "Oguri" },
  { "trainer_id": "234 567 890 123", "password": "LinkPassword2", "name": "ace-only", "ace_once": 5, "club": false }
]
```

| Field | Meaning |
|---|---|
| `trainer_id` | the account's Trainer ID; spaces are optional |
| `password` | its Link Password (from Data Link) |
| `name` | a label, used until the farm learns the in-game name |
| `trainee` | optional: the uma to train, by name (`Oguri`, `Oguri_alt`, …); left out, the farm picks one |
| `ace_once` | optional: do only this many Ace Runs, then leave the rotation |
| `club` | optional: `false` if the account is not in the farm's club, so the club chores are skipped |

The farm only reads the roster when it starts, so restart it once after an edit (see
[Day to day](#day-to-day)).

## Settings

**`/etc/umabot.env`** (template: `deploy/vps/umabot.env.example`):

| Key | Template value | Meaning |
|---|---|---|
| `AIC_CHORES` | `1` | daily chores on/off |
| `AIC_CLUB_EVERY` | `60` | seconds between club visits per account |
| `AIC_ACE` | `1` | Ace Runs: 5 per account per week (resets Monday 15:00 UTC) |
| `AIC_TEAM` | `1` | Team Trials battles |
| `AIC_GIFTS` | `1` | claim the gift box |
| `AIC_DAILY_RACE` | `1` | use the daily race tickets |
| `AIC_FAN_TARGET` | `28000000` | fans per account per game day, then that account waits for the reset |
| `AIC_FAN_STOP_MARGIN` | `0` | stop this many fans before the target |
| `AIC_VISIT_JITTER_MIN` / `_MAX` | `0` | random extra wait between visits, in minutes |
| `AIC_CAREER_WEBHOOK` | | Discord webhook for finished careers |
| `AIC_ALERT_WEBHOOK` | | Discord webhook for health alerts |

**`deploy/vps/aic.service`** holds the career options on its `ExecStart` line: `--scenario 2`
(Unity Cup; 1 = URA, 3 = Grand Concert, 4 = Trackblazer), the fan races to enter, TP
refills and the auto-built fan-bonus deck.

## Day to day

```bash
tail -f /opt/aic/state/aic/logs/aic.log     # what it is doing
systemctl status aic                        # is it running
```

- **Never `systemctl stop aic`.** To apply a change (settings, roster, code), restart it
  once, while the log's last line is `Next visit in ...`, so no visit is cut off:

  ```bash
  tail -1 /opt/aic/state/aic/logs/aic.log
  systemctl restart aic
  ```

- **Game update:** the farm notices (API error 204), reads the new version from Steam and
  switches by itself. Afterwards, open the game on the PC once, copy its new `master.mdb` to
  `/opt/umabot/master.mdb` and restart once.
- **Steam token expired** (the log shows 391/394 or Steam Guard errors): run step 6 again.

## Updating the code

Test in a copy first and install only if every test passes, so the live farm never runs
broken code:

```bash
rm -rf /tmp/aic_test && cp -a /opt/aic /tmp/aic_test
git -C /tmp/aic_test pull                   # or copy the changed files in
chown -R umabot:umabot /tmp/aic_test && cd /tmp/aic_test
sudo -u umabot env AIC_DATA_ROOT=/opt/umabot AIC_MASTER_MDB=/opt/umabot/master.mdb \
  bash -c 'for t in tests/test_*.py; do /home/umabot/.venvs/aic/bin/python $t >/dev/null 2>&1 || echo "FAIL $t"; done'
```

No `FAIL` lines? Then run `git -C /opt/aic pull` (or copy the same files in) and restart
once as above. If a service file changed, copy it to `/etc/systemd/system/` and run
`systemctl daemon-reload` before the restart.

## Discord control bot (optional)

Control the farm from a private Discord channel: `!status`, `!fans`, `!ace`, `!team`,
`!club`, `!log`, `!restart` (waits for a gap), and `!ask` to ask Claude on the server.

```bash
mkdir -p /opt/aic-discord && cd /opt/aic-discord
/home/umabot/.local/bin/uv venv --python 3.12 venv
/home/umabot/.local/bin/uv pip install --python venv/bin/python discord.py==2.4.0
npm install -g @anthropic-ai/claude-code@2.1.112   # only for !ask; newer builds need SSE4.2/AVX
bash /opt/aic/scripts/discord_setup.sh             # asks for the bot token, your user ID, the channel ID
sudo -u claudeops -H claude                         # only for !ask: /login once, then /exit
```

Re-run `discord_setup.sh` after changing the bot code; `--reconfigure` asks for the
settings again.

## Where things live

| Path | What |
|---|---|
| `/opt/aic` | the code; `reroll_accounts.json` |
| `/opt/aic/runtime/aic/auth_config.json` | game login + Steam refresh token |
| `/opt/aic/state/aic/` | `logs/aic.log`, the fan/carat/Ace/club day state, card inventories |
| `/opt/aic/state/accounts.json` | Link Passwords the farm has used |
| `/opt/umabot` | game data: `data/*.json`, `master.mdb`, `node_modules` |
| `/etc/umabot.env` | settings and webhooks |

How it works under the hood (the game API, selection rules, the measurements behind the
defaults): [docs/REFERENCE.md](docs/REFERENCE.md).
