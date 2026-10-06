# Testing drop — read this first

This is a **code-only** copy of `aic`. It deliberately contains **no accounts and
no credentials**: no `auth_config.json`, no `state/`, no `settings.json`, no
`accounts.json`, no `reroll_accounts.json`. Any Trainer ID you see in the
README, the tests or `reroll_accounts.example.json` is a **placeholder**, not a
real account.

You have to bring two things yourself: Python deps, and your own credentials.

## 1. The wire client — already included

Nothing to do. Icarus' `uma_api` client is **vendored** at `vendor/uma_api/`, so
this drop is self-contained: no separate checkout, no `AIC_ICARUS_ROOT`.

If you *do* have an Icarus checkout, point at it with `--icarus-root PATH` (or
`AIC_ICARUS_ROOT`) and it will be used for its data and its `uma_runtime/`
credentials — but the vendored client is always the one that runs, so the two
cannot silently diverge.

## 2. Python deps

Python **3.12** (not newer — the pinned wire-protocol deps predate 3.13):

```bash
uv venv --python 3.12 ~/.venvs/aic
uv pip install --python ~/.venvs/aic/bin/python -r requirements.txt
```

`frida` is only needed for `account capture`; everything else runs without it.
The launchers (`aic.sh`, `account.sh`, `rotate.sh`) use `~/.venvs/aic` and honour
`AIC_PY=/path/to/python`. On Windows use the `.bat` counterparts.

## 3. Your own credentials

Nothing here will authenticate as anyone. Capture your own:

```bash
./account.sh capture          # Windows, or Linux via Proton (see README)
```

On Linux the capture drives a Windows `frida-server.exe` inside the game's
Proton prefix; it is **not** bundled (67 MB, and it must match your installed
`frida` version). `aic/proton.py` prints the exact download command if missing.

## What you can run with no credentials at all

```bash
python tests/test_offline.py     # the offline suite - synthetic checks always run
./aic.sh --help
```

Two optional env vars widen that suite if you have the data:
`AIC_TEST_DUMPS` (UmaDumpy dump dir) and `AIC_TEST_CAPTURE` (a captured
`auth_config.json`).

## Where game data comes from

Race rules, support-card fan bonus, the card→character map and the succession
relation tables are all read from the game's own `master.mdb` and cached under
`state/`. It is found automatically in a normal Steam install; override with
`AIC_MASTER_MDB=/path/to/master.mdb`. Without it those features degrade rather
than guess.

## Please report

- The exact command you ran and the full output (`-v` for debug logging).
- For a refused career start (result code 205): the tool writes the exact
  payload to `state/<profile>/last_refused_start.json` — that file is the single
  most useful thing to send. **Scrub your viewer id from it first.**

Careful: `account.sh logout` and `rotate.sh farm` are **destructive** — they
delete the account on the Steam slot. Save a Link Password first
(`./account.sh save-link <password>`), or use a throwaway account.

---

# Quickstart: capture → 25 accounts → loop Independent Training

Run these in order. **Read the two warnings first.**

> **Quit the Steam client before step 2.** The tool logs into Steam itself; the
> desktop client holds the only allowed session, so leaving it open fails with
> `LoggedInElsewhere`. (Playing a non-Steam game is fine.)

> **Step 2 deletes the account currently on your Steam slot.** Save its Link
> Password first (step 1c) or use a throwaway Steam account. Without a Data
> Link an account is gone permanently.

### 1. Capture your auth (game must be running)

```bash
./account.sh capture              # Linux (Proton) or Windows: account.bat capture
./account.sh status               # confirm which Trainer ID you are bound to
./account.sh save-link MyPass2026 # REQUIRED before farming - makes it recoverable
```

On Linux the capture drives a Windows `frida-server.exe` inside the game's
Proton prefix. It is not bundled; fetch the build matching your `frida`:

```bash
V=$(python -c "import frida;print(frida.__version__)")
mkdir -p tools && curl -L \
  "https://github.com/frida/frida/releases/download/$V/frida-server-$V-windows-x86_64.exe.xz" \
  | unxz > "tools/frida-server-$V-windows-x86_64.exe"
```

### 2. Create 25 accounts, 5 ten-pulls each

```bash
./rotate.sh banners                                   # pick a gacha_id
./rotate.sh farm --banner <ID> --count 25 --pulls 5 --password MyPass2026
```

Each account is created, tutorial-skipped, given its carats, pulled on, assigned
a Data Link, and appended to `reroll_accounts.json`. The roster is rewritten
after **every** account, so an interrupted run loses nothing. Try `--count 1`
first to confirm the whole chain works before committing to 25.

Useful flags: `--free-only` (never spend carats), `--pace 0.5` (faster, more
detectable), `--name-prefix test`.

### 3. Loop Independent Training across those accounts

```bash
./rotate.sh plan                  # the visit schedule, changes nothing
./rotate.sh run --dry-run         # full rehearsal, still changes nothing
./rotate.sh run --race-goal 30    # the real loop, forever
```

A career takes ~50 minutes, so 25 accounts means a visit every ~2 minutes and
every account is always mid-career. `--cycles 2` stops after two full passes;
`--only <name-or-trainer-id>` loops a single account.

### Optional

```bash
./rotate.sh farm --banner <ID> --count 25 --pulls 5 --start-career --auto-deck
```

`--start-career` also starts one career on each account as it is created (capped
at 25/run). `--auto-deck` builds the deck from your highest Fan Bonus cards -
which is the default for any deck the tool builds itself.

```bash
./aic.sh fan-cards --trainee <CARD_ID>   # rank your cards by Fan Bonus
./aic.sh run --loop --race-goal 40       # loop ONE account instead of a roster
```

`--no-delay` on any command removes every inter-request delay. It is an
experiment and will probably earn 403s from the edge.
