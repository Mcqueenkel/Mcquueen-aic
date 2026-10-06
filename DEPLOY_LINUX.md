# Deploying AIC on the VPS, beside the Icarus bot

This is Path A of `lmao-repo-linux-port/HANDOFF_AIC_INTEGRATION.md`: AIC runs as a **sidecar**
next to the already-deployed `/opt/umabot`, as its own systemd unit. Nothing in `/opt/umabot`
is edited.

```
/opt/umabot        the Icarus bot (already deployed)  - AIC uses its DATA
/opt/aic           this checkout (new)                - AIC's own code and state
```

---

## 0. Four corrections to the handoff — read these first

The handoff was written against an older AIC. Verified against this checkout on 2026-09-01:

**1. AIC does NOT import Icarus' `uma_api.client`.** The client is **vendored** at
`vendor/uma_api/` and `aic/bootstrap.py:52-66` makes it authoritative ("The vendored copy is
authoritative: one client, always the one that ships with this tool"). The Icarus checkout is
used for *data only*: `data/*.json`, `master.mdb`, and `node_modules/steam-user`.

Consequences the handoff gets wrong as a result:

* §2.3's "a version bump you push there is picked up by AIC too **because AIC imports this
  repo's client**" — the reason is wrong, but **the conclusion still holds**: both clients read
  the same `UMA_APP_VER` from `/etc/umabot.env`, so one `push_app_ver.py` still serves both.
* §5's "keep this repo's `uma_api/client.py` hardening" does **not** apply to Path A. AIC ships
  its own client, so that hardening is simply not in play here.

**2. ⚠ AIC's vendored client ADOPTS the server's `viewer_id` on result_code 709. Icarus refuses
to.** The handoff (§3, §5) treats the 709 refusal as a security boundary to preserve — and it is
— but places the risk in Path B only. It is not: because the client is vendored, **Path A already
ships the adopting behaviour.**

```python
# vendor/uma_api/client.py:1158-1166   -- what runs on the VPS
if rc == 709:
    new_vid = dh.get("viewer_id") or ...
    if new_vid and new_vid != self.viewer_id:
        log.warning("VIEWER ID MISMATCH on 709 (viewer id updated)")
        self.viewer_id = new_vid          # <-- adopted
        self.regen_sid()
    raise Exception(f"709 on {ep}")
```

Icarus' `uma_api/client.py:1240-1251` deliberately does the opposite, with a corpus check behind
it: "the client never once legitimately SENT the sentinel id ... Adopting it would silently run
the rest of the loop as the wrong account and 102 forever."

**Nothing in this deployment changes that** — porting the refusal into `vendor/uma_api/client.py`
is a separate decision, not part of Path A. What this deployment does instead is make the
consequence *visible*: a 709 is one of the codes `scripts/health_alert.py` explains, and AIC
running as the wrong account shows up as the log going quiet or erroring.

**3. `UMA_STEAM_SHARED_SECRET` has no effect on AIC** (handoff §4.4). That is umabot's variable.
AIC reads its Steam credentials out of `auth_config.json` — `steam_refresh_token`,
`steam_username`, `steam_password_seed` (`aic/steam.py:314-316`) — and the `steam_shared_secret`
key it does understand is only reached on a fallback path (`aic/auth.py:275-286`), never on the
default persistent session. **A stored `steam_refresh_token` is the only thing that makes AIC
unattended.** See §3 below.

Also: the handoff says a Guard prompt makes a systemd job "hang/fail". It does not.
`aic/auth.py:253-256` catches `SteamGuardRequired` and logs *"Continuing with the stored
ticket"* — so the real failure mode is a **silent** degrade into 391/394 login errors hours
later. That is precisely why the health timer exists.

**4. `rotate` used to unlink accounts with no way back.** `rotate._ensure_bound` disconnects
whatever account holds the Steam slot before logging in the next roster entry — and it did that
with no check that the outgoing account had a recoverable Link Password. Following handoff §2.3
option 1 (point AIC at the live carried identity) and then running `rotate run` would have
destroyed that account on visit #1. **Fixed**: it now refuses, with instructions. See §6.

---

## 1. One-time setup

Python **must** be 3.12 — the pinned wire deps predate 3.13. The venv cannot live on the exFAT
drive, so it goes under `$HOME`.

```bash
git -C /opt clone <this repo> aic        # or rsync the checkout to /opt/aic
chown -R umabot:umabot /opt/aic

uv venv --python 3.12 /home/umabot/.venvs/aic
uv pip install --python /home/umabot/.venvs/aic/bin/python -r /opt/aic/requirements.txt

# steam-user lives in the Icarus checkout; AIC reuses it.
# --no-bin-links: exFAT cannot hold the .bin symlinks.
npm install --no-bin-links --prefix /opt/umabot
```

`frida` in `requirements.txt` is only for `account capture` on Linux via Proton. Skip it unless
you want the VPS to capture its own auth (handoff §2.3 option 2).

---

## 2. Accounts: AIC and umabot must be disjoint

Both spend the one Steam session ticket for the one Steam slot. Two services on the same in-game
account fight over it — this is the `CLAUDE.md` "never run two of your accounts at once" rule,
now spread across two units.

`deploy/aic.service` enforces this **by path**, with `AIC_PROFILE=aic`:

| | credentials | state |
|---|---|---|
| umabot | `/var/lib/umabot/auth_config.json` | `/var/lib/umabot` |
| AIC | `/opt/umabot/uma_runtime/aic/auth_config.json` | `/opt/aic/state/aic` |

Different files, so the two cannot silently become the same account. Seed AIC's:

```bash
sudo -u umabot mkdir -p /opt/umabot/uma_runtime/aic
# copy the capture for AIC's OWN account from the Windows machine, then:
sudo -u umabot cp auth_config.json /opt/umabot/uma_runtime/aic/
```

Or repair one straight from a UmaDumpy dump, no Frida:

```bash
cd /opt/aic && AIC_PROFILE=aic ./account.sh import-capture <dump>
```

---

## 3. The one interactive step — do this before enabling the unit

```bash
cd /opt/aic
export AIC_ICARUS_ROOT=/opt/umabot AIC_PROFILE=aic
sudo -u umabot -i ./aic.sh login        # ON A TERMINAL. Answer the Guard prompt.
```

`aic/steam.py:243` only prompts for a Steam Guard code when `stdin.isatty()`. A systemd job has
no tty, so it never asks — it carries on with the stale ticket and fails later. The successful
interactive login **stores a `steam_refresh_token`** (`aic/auth.py:266`, logged as *"Stored a
Steam refresh token — later runs will not need a password or a Guard code"*), and that token is
what makes every later start unattended.

If the health alert ever reports a repeating 391, this is the step to repeat.

---

## 4. The read-only ramp

Do all of it before farming. Nothing here mutates an account.

```bash
cd /opt/aic && export AIC_ICARUS_ROOT=/opt/umabot AIC_PROFILE=aic

./aic.sh status              # G1+G2: the vendored client imports, the identity logs in
./aic.sh plan                # G3a: a legal trainee/parents/deck
./aic.sh schedule            # G3b: the schedule that would be sent
./aic.sh run --dry-run       # G3c: builds a selection + schedule, submits nothing
```

Then one live cycle, watched:

```bash
./aic.sh run                 # G4: start -> wait ~50 min -> end -> finish
```

**Read the banked fan count off that cycle and set `AIC_FAN_STOP_MARGIN` above it** (§5). It is
the one number this deployment cannot know in advance: idle-career yield is not the turn-by-turn
yield the 1,100,000 default was measured from. This is the handoff's own open question (§7).

---

## 5. The daily fan cap

New in this deployment (`aic/fancap.py`), ported from Icarus' `_daily_baseline` /
`sweep_day_fans.json`. AIC previously had **no** fan target and **no** daily cap.

* The target is **fans gained this game-day**, not a lifetime total — lifetime is in the hundreds
  of millions, so an absolute number is meaningless.
* The baseline is **persisted** at `state/aic/fan_day.json`, keyed by `viewer_id`. This is the
  whole point: keeping it in the process reset it on every restart, so a mid-day restart farmed
  the entire target a *second* time. Measured in Icarus on 2026-08-31: 33M farmed again over 9.7M
  already gained, ~43M in one day.
* It **rolls over on its own** at the 15:00 UTC reset, because the day key changes and no stored
  record matches. No timer, no cron.
* It gates **starting** a career, never finishing one. A career already running is always ended
  and banked first; only the next start is refused.

Configured entirely by environment, so one `Environment=` line in the unit serves every entry
point:

```
AIC_FAN_TARGET=33000000        # unset or 0 = no cap at all (the old behaviour)
AIC_FAN_STOP_MARGIN=1100000    # stop this far short so the in-flight career cannot overshoot
```

The gate lives at `aic/flow.py::start_independent`, which is the single point all three start
paths route through — `aic start`, `flow.run_cycle` (so `aic run --loop`), and `rotate.visit`.
It sits *before* the first live API call, so a capped account costs no traffic.

When the cap is hit: `aic run --loop` sleeps to the reset; a single-account rotation schedules its
next visit at the reset; a multi-account rotation skips that account and moves to the next slot.

---

## 4a. Two prerequisites a VPS does not have by default

Both were measured on this box on 2026-09-01, in this order - each one only becomes visible once
the previous is fixed.

**1. `master.mdb` - or every start is refused with 205.** `aic/racedata.py` builds the race list
from the game's database. It probes five Steam/Proton paths, none of which exists on a server with
no game install, and falls back to the account's saved agenda - which is not trainee-specific, and
which the server rejects. Copy it up and point at it:

```bash
# from the Windows machine (16 MB)
gzip -c "C:/Program Files (x86)/Steam/steamapps/common/UmamusumePrettyDerby/UmamusumePrettyDerby_Data/Persistent/master/master.mdb"   | ssh root@<vps> 'gunzip -c > /opt/umabot/master.mdb && chown umabot:umabot /opt/umabot/master.mdb'
```

`AIC_MASTER_MDB=/opt/umabot/master.mdb` is set in `deploy/aic.service`. **Refresh it on patch day**,
same trip as `UMA_APP_VER`. With it present the plan becomes trainee-specific and legal - `y1=8,
y2=13, y3=14` on the account tested, against the "at least three year-2 races" rule.

**2. Clocks (item 95) - or the start is refused with 2509.** With a legal race list, 69 TP, 854
succession points and a roster at 187/260, `idle_single_mode/start` still refused. The only empty
resource was item 95, which `aic/config.py` has always listed among "the only ones the start flow
needs" and which nothing ever checked.

**An idle career appears to spend one, and nothing in this repo can mint them** - they are earned
or bought in game. This is a hard prerequisite for idle farming, not a bug to fix here.

`flow.start_independent` now warns when the balance is 0 and annotates a 2509 with it, so the next
occurrence says so instead of raising a bare server code. The link is *inferred from one account*,
so it warns rather than refusing - a hard gate built on a guess would block a setup that works.

---

## 5a. The career clock — how the rotation decides when to come back

`CAREER_MINUTES = 50.0` was always an assumption. The server tells you the real
answer on every visit (`idle_single_mode_load_info.end_time`), and the rotation used
to read it and then throw it away for any roster bigger than one, scheduling on blind
`50/N` slots instead. That cost a whole career every time a slot slipped: arriving
early found a run still going, did nothing, and did not come back for a full lap — so
the account sat **finished and idle** for up to 50 minutes, every lap.

Now every visit writes that account's clock to `state/<profile>/career_clock.json`:

```json
{"221579896969": {
   "name": "El_condor_pasa",
   "due_at": 1756761843.0,
   "due_at_utc":             "2026-09-01 21:04:03Z",
   "career_started_at_utc":  "2026-09-01 20:14:03Z",
   "career_finishes_at_utc": "2026-09-01 21:04:03Z",
   "duration_seconds": 3000,
   "source": "started"}}
```

and the loop visits **whichever account is due soonest**. So a short career comes back
early, a wrong `career_minutes` costs nothing after the first visit, and a restart
resumes the real schedule instead of re-staggering from now. `50/N` is only the
opening stagger, for accounts with nothing on record.

`due_at` is **local** epoch built from `seconds_remaining`, not from the server's
`end_time`. `seconds_remaining` measures "what was left when the server told us" minus
"how long we have run since", so it cancels a wrong `ServerClock` offset — and that
offset is derived, falling back to an assumed +3600. The server's own stamps are
recorded beside it for reading, never for arithmetic.

Guards worth knowing, all covered by `tests/test_career_clock.py`:

* a due time is clamped to **25 h** ahead and refused if older than a day — one bad
  clock reading cannot retire an account from the rotation;
* a **failed** visit keeps the clock already on record rather than nulling it, so a
  restart after a bad visit does not re-stagger;
* consecutive visits are floored at `MIN_INTERVAL_SECONDS`, so a roster that all came
  due during an outage is paced instead of drained in one burst of account switches;
* `--cycles N` still means N roster passes, so a fan-capped account (parked 24 h) can
  never stretch a bounded run into a day.

Read it live with `status_aic.sh`, which prints the file.

---

## 6. Rotation, and the account-deletion guard

`account/steam_chain_disconnect` is the game's **"Delete User Data"**: permanent, and recoverable
only with a Link Password. `rotate` calls it on every account switch.

`aic/rotate.py::_ensure_bound` now refuses to disconnect an account that has no Link Password on
record:

```
the account on this Steam slot (103 444 444 441) has no Link Password on record,
and switching away from it DELETES it.
  Record one first:   account.sh save-link <password>
  Or, if you know it: account.sh remember "103 444 444 441" <password>
```

Roster targets are registered automatically by `rotate.run`; it is the account *already holding
the slot* that was unprotected. `tests/test_rotate_guard.py` covers it.

`aic.sh run --loop` never touches `account/*` at all — if you only farm one account, that path
cannot delete anything.

**Still true and still yours to avoid:** never point AIC's profile at `/var/lib/umabot`'s
identity and then run `rotate`. §2's separate profile is what keeps that from being possible by
accident.

---

## 7. Install the units

```bash
sudo cp /opt/aic/deploy/aic.service /etc/systemd/system/
sudo cp /opt/aic/deploy/aic-health.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now aic
sudo systemctl enable --now aic-health.timer
```

`deploy/aic.service` mirrors `umabot.service`'s hardening, including the two details that unit
documents as measured, not guessed:

* `StartLimitIntervalSec` / `StartLimitBurst` are in **`[Unit]`**. systemd ignores them in
  `[Service]` with only a log warning, which silently disables the backoff.
* `RestartSec=120`, not 30. A dead ticket at 3am with a short `RestartSec` is a Steam password
  login every two minutes until Steam locks the account.

It differs from `umabot.service` in one place: **`ProtectHome=read-only`, not `true`.** AIC's
interpreter is `~/.venvs/aic/bin/python` (the exFAT drive cannot hold a venv), and
`ProtectHome=true` empties `/home`, so the unit could not exec its own Python. Nothing AIC writes
lives under `$HOME` — the Steam refresh token is stored in `auth_config.json`.

`ReadWritePaths` covers everything AIC writes, which under `ProtectSystem=strict` fails
**silently** if missed (a refreshed `auth_key` or a learned succession offset is just discarded):

```
/opt/aic/state                 logs, fan_day.json, succession_offsets.json, tp_cost.json,
                               server_clock.json, race_rules.json, accounts.json, auth_archive/
/opt/umabot/uma_runtime        auth_config.json + the client's api_library/ and logs
-/opt/aic/reroll_accounts.json written by `rotate init` / the account farmer only
```

To rotate a roster instead of looping one account, swap the `ExecStart` for
`/opt/aic/rotate.sh run` and read §6 first. Do not enable both — they would fight over the Steam
slot exactly the way AIC and umabot would.

---

## 8. Watching it

```bash
ssh root@<vps> "bash /opt/aic/status_aic.sh"     # read-only: unit, fans today, recent log
```

`scripts/health_alert.py` runs every 30 minutes (`aic-health.timer`, offset from umabot's) and
posts to Discord when AIC is **wedged**. Same design rules as Icarus' version:

* **State, never transition.** No edge detection and no "already told you" file: every run that
  finds a fault posts again, and the timer interval *is* the re-alert interval. By the time
  anyone looks, the fault is already the steady state.
* **Exit 1 when wedged is deliberate**, so `systemctl is-failed aic-health` carries the verdict
  even when the webhook is what is broken.
* It writes nothing, anywhere.

AIC's signals differ from umabot's — there is no web UI to poll and no `api_library`. What it has
is one log, and the useful property that **it announces every long sleep before taking it**. So:

* a stale log whose last line announced a wait (a rotation slot, an idle career's next poll, the
  fan-cap wait) is **healthy**, up to one game-day;
* a stale log that stopped anywhere else is a **wedge**;
* an `ERROR` with no progress logged after it is a **wedge**, and the alert carries what to do
  about 709 / 1055 / 391 / 394 / 204 / 501 / 214 / 205 / 102.

`--stale-minutes 75` must stay above the longest *unannounced* gap: a single-account rotation
waits a full 50-minute career between visits.

Webhook: `AIC_ALERT_WEBHOOK`, else `UMA_ALERT_WEBHOOK` from `/etc/umabot.env`.

```bash
# check it by hand, post nothing
/home/umabot/.venvs/aic/bin/python /opt/aic/scripts/health_alert.py \
    --unit aic --log /opt/aic/state/aic/logs/aic.log --dry-run
```

---

## 9. Verification gates

Mirrors the handoff's G1–G6.

| | check | how |
|---|---|---|
| G1 | imports clean, no `get_hwid` crash | `./aic.sh status` |
| G2 | reaches `load/index` (rc 1) on the carried identity, no game, no Frida | `./aic.sh status` |
| G3 | builds a legal selection + schedule, submits nothing | `./aic.sh run --dry-run` |
| G4 | one live cycle banks fans, `factor_lottery_id=1` | `./aic.sh run` |
| G5 | AIC and umabot never hold one account at once | separate `AIC_PROFILE` (§2) |
| G6 | a restart mid-cycle resumes without double-charging | restart `aic`, watch one cycle |
| **G7** | **the fan cap survives a restart** | `python tests/test_fan_cap.py` |
| **G8** | **rotation cannot delete an unrecoverable account** | `python tests/test_rotate_guard.py` |
| **G9** | **the health check does not cry wolf on announced waits** | `python tests/test_health_alert.py` |

G7–G9 are offline — no network, no credentials, runnable on the dev box:

```bash
python tests/test_fan_cap.py && python tests/test_health_alert.py && python tests/test_rotate_guard.py
```

**G4 is the one that still needs measuring.** Read its banked fan count and set
`AIC_FAN_STOP_MARGIN` above it before committing accounts to idle farming.
