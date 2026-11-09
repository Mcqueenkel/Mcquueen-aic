# AutoIndependentCareer (`aic`)

## The Independent Career API

Internally the game calls it **idle single mode**. Endpoints (transcribed from
`UmaDumpy-main/dumps/`, none of this exists in Icarus):

| Endpoint | Purpose |
|---|---|
| `idle_single_mode/pre_start` | light probe (flow B) **or** the full submission (flow A) — see below |
| `idle_single_mode/start` | the full submission (flow B) **or** a deck-echo follow-up (flow A) |
| `idle_single_mode/check_progress_log` | career state while/after it runs |
| `idle_single_mode/end` | stops the timer, returns the progress log |

### Two submission flows, and the trap between them

The real client has been captured starting idle careers **two different
ways**, both accepted:

* **Flow B** (2026-07-30, two sessions): `pre_start` as a light probe
  (`scenario_id: 0`, no payload) → the **full payload on `start`**. The career
  is created by `start`; nothing follows.
* **Flow A** (2026-07-24 and 2026-08-02, four sessions): the **full payload on
  `pre_start`** — the career is created there, `progress_info` comes back in
  the same response — then `start` as a deck-echo follow-up carrying only
  `support_card_deck_array` (deck 1 = the five cards just used,
  `auto_deck_flag` 1; nine empty slots).

What has **never** been observed being accepted is the hybrid this tool
briefly shipped after misreading the 08-02 captures: a light `pre_start` probe
followed by a full payload on `pre_start` again. On the account it was tested
against, that sequence answered 205 to *every* value of *every* field — a
blanket rejection that looks like a wrong field but is actually a wrong
**sequence**. Days went into diffing succession values, race lists, and
borrowed cards that were never the problem.

`aic` therefore runs **flow B by default** — the sequence it ran during all of
its historical successes — and automatically retries each early candidate on
**flow A** as a fallback. Whichever flow accepts is the one the
succession-rank-point search continues on, and an A-path success is followed
by the deck echo (`aic.independent.confirm_deck`). A final refusal on *both*
flows saves the exact payload to `state/<profile>/last_refused_start.json` so
it can be diffed against the accepted captures instead of guessed about.

The 08-02 captures also produced a second real succession-rank-point ground
truth (trainee 100701, parents 4+6, accepted at **127**) — see
[Succession rank points](#succession-rank-points) below.

An idle run is an ordinary scenario career (`scenario_id` 1–4) that the server
simulates on a wall clock instead of turn by turn. Captured runs last **50
minutes**.

Starting costs **30 TP normally, 15 during a half-cost campaign**. The
discounted price is not published in any payload
(`common_define.single_mode_factor_use_tp_value` is the *factor-lottery* cost),
so `aic` probes it: it sends 15, and on a mismatch rejection retries with 30.
Cheap-first matters — too little is refused outright, whereas too much risks
simply being charged. The winner is cached per profile for a day, so the probe
costs one extra call at most when the campaign state flips. Pin it with
`--use-tp 30` to skip probing.

> **Sitting between 15 and 30 TP is a trap.** With, say, 24 TP the probe can only
> try 15; if the campaign has ended, that is refused and 30 is never reachable.
> The failure then looks like it is about something else entirely. `aic` detects
> this case specifically and says so instead of searching other fields.
> `--recover-tp` spends TP potions (items 32/12/11) to reach the full 30 first.

Its state lives in `load/index` → `idle_single_mode_load_info`, which carries
`start_time` / `end_time` / `playing_state` plus a `single_mode_chara_light`.
**While an idle run is active the top-level `single_mode_chara_light` is
`null`**, so the idle block is the only place a running career appears.

Ending is two stages, exactly as the client does it:

1. `idle_single_mode/end` — stops the simulation. The career still exists.
2. `<scenario_prefix>/factor_select` then `<scenario_prefix>/finish` with
   `factor_lottery_id: 1` — retires it and banks the trained character.

Three things about this sequence are easy to get wrong, and each answers 205:

- **The turn number is not where you would expect it.** `load/index` reports
  `turn: 1` for an idle career for its entire run; the real final turn (78) only
  appears in `end`'s response under `end_info.chara_info`. `aic` reads it from
  there, falling back to `check_progress_log` and then `<prefix>/load`, and
  treats any turn ≤ 10 on a supposedly-finished career as a signal to go look
  it up rather than send a value that will bounce.
- **`check_progress_log` is only valid after `end`.** While the timer is
  running there is no progress log yet.
- **`end` is only valid once.** After it succeeds, `load/index`'s idle block
  starts reporting the real turn and `state: 3` — so a second `end` is refused.
  `aic` detects that state (`CareerState.simulation_done`) and skips straight to
  the finish, which makes an interrupted run resumable: just re-run.

That last point matters because a rejected call is not cheap to observe —
Icarus' client retries 205 three times with 208 back-off in between, so a call
you should not have made looks like a hard failure for several seconds before
the tolerated-error path is even reached.

`<prefix>/factor_lottery` sits between those two in the client and is the
**reroll**: it costs 30 TP per roll. `aic` never calls it, which is what "take
the first factors, don't reroll" means at the protocol level.

The client also calls `<prefix>/gain_skills` before finishing, to spend leftover
skill points. `aic` skips it: this is a **fan-farming** tool, so skills and
factor quality do not matter, and skipping keeps the TP and the call count down.
Fans come from races, which is why the race agenda defaults to the fullest one
available — see below.

## Status

All seven steps are implemented.

| # | Step | Notes |
|---|------|-------|
| 1 | Auth → `/load` main screen | full boot sequence, Steam ticket refreshed each run |
| 2 | Detect a running career, read remaining time, wait | reads `idle_single_mode_load_info.end_time` |
| 3 | End: stop the idle run, first factors, finish | never calls `factor_lottery` or `gain_skills` |
| 4 | Start with two eligible parents | never the trainee's character, never each other's |
| 5 | Deck: use a saved one, else build a random legal one | no duplicate characters |
| 6 | Training schedule | defaults to the account's last-used policy + saved race agenda |
| 7 | Start the independent training | payload verified against the captured request |

Not yet done: multi-account juggling (uma-reroll-farm's logout/login).
`--profile` already selects an independent credential set, which is the seam
that would grow into it.

## Setup

Requires the Icarus dependencies (`curl_cffi`, `msgpack`, `pycryptodome`) and
Node.js for Steam ticket minting. Simplest is to run with Icarus' virtualenv
interpreter, which already has them:

```powershell
.\aic.bat status
```

The launcher finds `..\Icarus-Dev-Build-Private-main\.venv\Scripts\python.exe`.
If your checkout is elsewhere, point at it with `--icarus-root PATH`, the
`AIC_ICARUS_ROOT` environment variable, or a `settings.json` here:

```json
{ "icarus_root": "C:\\path\\to\\Icarus-Dev-Build-Private-main", "profile": "default" }
```

### Linux

Everything runs on Linux, including the frida capture (via Proton — see below);
`./aic.sh`, `./account.sh` and `./rotate.sh` are the launcher counterparts. They run `~/.venvs/aic`'s
interpreter (a venv cannot live on an exFAT drive — no symlinks), overridable
with `AIC_PY`:

```bash
uv venv --python 3.12 ~/.venvs/aic
uv pip install --python ~/.venvs/aic/bin/python \
    curl_cffi==0.7.4 msgpack==1.1.0 pycryptodome==3.14.1
# steam-user, for ticket minting. --no-bin-links because exFAT cannot hold
# the .bin symlinks (a transitive dep ships one); nothing requires them.
npm install --no-bin-links --prefix "$AIC_ICARUS_ROOT"
./aic.sh status
```

Python 3.12, not newer: the pinned wire-protocol deps predate 3.13. Node can
be a plain tarball unpacked into `~/.local` (the launchers put `~/.local/bin`
on `PATH`). `aic.bootstrap` patches out Icarus' Windows-registry device
fingerprinting on POSIX — every fingerprint field comes from the captured
`auth_config.json`, so a config captured on Windows keeps working here.

### Capturing on Linux (Proton)

`account.sh capture` works on Linux too, against the game running under Proton.
The game is a Windows PE inside a pressure-vessel container, so instead of
attaching a host-side frida (which cannot see `GameAssembly.dll`), the port runs
a **Windows `frida-server.exe` inside the game's own Wine session** and drives it
over TCP with the **unchanged** agent. `aic/proton.py` handles this:

* it launches the game through the Steam Linux Runtime + Proton chain with
  `PROTON_REMOTE_DEBUG_CMD` pointing at `frida-server.exe`, so Proton starts the
  server in the same prefix / container / wineserver as the game;
* the host frida client connects to `127.0.0.1:27042` (pressure-vessel shares
  the host network namespace) and loads the agent.

Setup:

```bash
uv pip install --python ~/.venvs/aic/bin/python frida    # host client
# Windows frida-server matching that client version (see the exact command
# aic/proton.py prints if it is missing):
V=$(~/.venvs/aic/bin/python -c "import frida;print(frida.__version__)")
curl -L "https://github.com/frida/frida/releases/download/$V/frida-server-$V-windows-x86_64.exe.xz" \
  | unxz > tools/frida-server-$V-windows-x86_64.exe
./account.sh capture --timeout 240
```

Because the server is injected at launch, Linux capture **owns the launch** — it
starts (or restarts) the game itself; it cannot attach to a hand-started one, so
`--no-launch` only works against a game a previous `capture` left running. Paths
are auto-detected from Steam; override with `AIC_STEAM_ROOT`, `AIC_PROTON`,
`AIC_SLR_ENTRY_POINT`, `AIC_GAME_LIBRARY`, or `AIC_FRIDA_SERVER` if needed.

Alternatively, skip capture entirely: move an `auth_config.json` over as a file,
or restore one from `state/<profile>/auth_archive/` / `account.sh import-capture`
/ `account.sh login`.

Credentials can also be captured on Windows:

```powershell
.\account.bat capture
```

`auth_key` cannot be derived offline — it is the account's own credential, bound
to the `udid` in the same request header — so it has to be read off the wire.
See [Capturing credentials](#capturing-credentials). (Running Icarus itself
still works too; this is the same mechanism without the web UI.)

## Commands

```
aic login              authenticate and reach the main screen
aic status             account state + current career, with time remaining
aic wait [--timeout N] block until the idle clock runs out
aic end [--no-force]   stop the idle run, take the first factors, finish
aic fan-cards          rank owned support cards by Fan Bonus (level + uncap)
aic plan               print the trainee / parents / deck that would be used
aic schedule           print the training schedule that would be sent
aic start              start a new Independent career
aic run                the whole cycle: wait, end, start
aic discover           dump the idle-career surface for inspection
```

Flags: `-p/--profile`, `--trainee CARD_ID`, `--deck DECK_ID`, `--auto-deck`,
`--build-deck-slot N`, `--seed N`, `--running-style {1,2,3,4}`, `--scenario N`,
`--schedule FILE`, `--race-agenda N`, `--use-tp N`, `--dry-run`, `--loop`,
`--max-cycles N`, `--timeout N`, `--no-force`, `-v`.

### Fan Bonus decks

`aic fan-cards` logs in and ranks every owned support card by its **Fan Bonus**
(support-card effect type 16 in `master.mdb`), computed at the card's *actual*
level and uncap. It marks cards you cannot deck together: a card of the trainee's
own uma (`--trainee CARD_ID`), and the weaker copy when you own two of the same
uma. It ends with the best five deck-eligible cards (the sixth is a borrowed
friend card).

`--auto-deck` builds a career deck from those cards automatically — the five
highest fan-bonus cards that form a legal deck (no duplicate uma, none the
trainee's, Team Sirius rules respected), and biases the borrowed 6th card toward
fan bonus too. It works on `plan` (preview), `start`, and `run`, and overrides
`--deck`. The built deck is written to slot 10 by default; change it with
`--build-deck-slot N`. Because the career start sends the cards directly, the
slot is where the deck is *saved* for reuse — if a start is ever refused on
`select_deck_id`, try `--build-deck-slot 1`.

```bash
./aic.sh fan-cards --trainee 106801           # see the ranking
./aic.sh plan --auto-deck --trainee 106801    # preview the built deck (no mutation)
./aic.sh run --loop --auto-deck --trainee 106801 --race-goal 40
```

## Getting started

Work up in this order — each step is read-only until the last.

```powershell
# 1. Does auth work and reach the main screen?
.\aic.bat login

# 2. What is the account doing right now, and how long is left?
.\aic.bat status

# 3. What trainee / parents / deck would it pick?
.\aic.bat plan

# 4. What training schedule would it send? (lists your saved race agendas)
.\aic.bat schedule

# 5. Full rehearsal - authenticates, reports everything, mutates nothing.
.\aic.bat run --dry-run

# 6. For real: wait out the current run, end it, start the next.
.\aic.bat run
```

Steps 1–5 cannot change anything on the account. Step 6 is the first one that
writes.

Once that works, farm continuously — each cycle waits out a 50-minute run, ends
it, and starts the next:

```powershell
.\aic.bat run --loop
.\aic.bat run --loop --max-cycles 5      # stop after 5 careers
```

### Fan farming

Fans come from races, so the race agenda is the knob that matters. `aic schedule`
lists yours with race counts:

```
agenda 1 'Agenda 1': 36 races
agenda 2 'Agenda 2': 16 races
```

The default picks the fullest one (36 above). To pin a specific agenda:

```powershell
.\aic.bat run --loop --race-agenda 1
```

If you build a better fan-farming agenda in-game, it shows up here automatically
— `aic` reads the agendas live from `pre_start` every cycle.

### Stress test (create → pull → start career → save, in a loop)

`rotate.sh farm` creates fresh accounts, pulls a fixed number of times, and saves
each with a Data Link. Adding `--start-career` turns it into an end-to-end stress
test: each new account also **starts an Independent career** after its pulls,
before its credentials are saved. Per account it does:

```
free the Steam slot → signup (fresh udid) → name/sex/tutorial → claim carats
  → N ten-pulls on the banner → start an Independent career
  → publish a Data Link → append to reroll_accounts.json
```

```bash
./rotate.sh banners                              # find a gacha_id to pull on
./rotate.sh farm --banner <id> --count 3 --pulls 5 --start-career --auto-deck
```

The career step reuses the normal start flow (`flow.run_cycle`) on the new
account — a brand-new account's starter trained characters satisfy trainee and
parent selection. It is best-effort: if a start is refused the account is still
kept and saved. Career knobs pass straight through: `--trainee`, `--auto-deck`,
`--build-deck-slot`, `--scenario`, `--race-goal`, `--running-style`.

This drives **real signups, gacha and career starts against the live server**, so
`--start-career` is **hard-capped at 25 accounts per run** — there is no "forever"
mode. Run it again, deliberately, for more. Each account created this way is
abandoned (recoverable only via its saved Link Password), so it is account
creation at scale against Cygames — your ToS risk, on your own Steam slot.

## Scenario choice

An idle run is an ordinary scenario career, but **not every unlocked scenario is
necessarily startable in idle mode**, and a refusal reports only 205 — the same
code as a dozen unrelated problems. So the scenario is chosen conservatively:
an explicit `--scenario`, then one seen running this cycle, then one remembered
from a previous successful start (`state/<profile>/idle_scenario.json`), and
only as a last guess the newest unlocked.

Any scenario an idle career is *observed* running in is recorded, since that is
proof it works.

## Race lists are per-trainee

**Saved in-game race agendas are generic — they are not bound to a trainee.**
But every trainee has its own *route*, and that route's goal races are
mandatory. Reusing an agenda built around one character therefore produces a
race list missing another character's goals, which the server refuses with 205.

The evidence, from `master.mdb`:

| Trainee | Route | Goal races |
|---|---|---|
| Kitasan Black (card 106801) | 51 | 10 |
| King Halo | 19 | 9 |
| El Condor Pasa | 22 | 8 |
| Taiki Shuttle | 4 | 8 |

Route 51 for Kitasan matches the `route_id` in a captured working start exactly.
Kitasan needs 8 races King Halo does not.

So `aic` builds the race list **from the trainee's own route**, using a saved
agenda only for extras that are also legal for it.

**Send the route goals and little else.** An earlier version topped every career
year up to a dozen legal races (~36 total) on the theory that a year with no
races is refused. That theory was never confirmed, and it is now disproven:

Three accepted starts (2026-08-02) all used trainee card 100701, each described
by the player as "the base goal races plus 3 I picked". The races **common to
all three** are therefore exactly the mandatory goal set — and
`route_targets(1007)` reproduces all **9 of 9**, with nothing extra. The route
data was never the problem.

| | Races sent | Races the server never saw |
|---|---|---|
| Real client | 12 (9 goals + 3 chosen) | — |
| Old default (fill on) | 36 | **26** |
| Current default (fill off) | 9 | **0** |

Filling invented 26 races per start that no accepted capture contains, and the
server's answer is a bare 205 — which no succession rank point can rescue, and
which looks exactly like a wrong number. `--fill-races` restores the old
behaviour for comparison. Four tables carry everything needed:

```
single_mode_chara_program   chara_id   -> program groups it may enter
single_mode_program         program_id -> group, race_permission, month/half
single_mode_route           chara_id   -> race_set_id (its route)
single_mode_route_race      race_set_id-> the goal races, by turn
```

### What `is_legal` does and does not prove

Two captured **accepted** starts were checked against this tool's legality
model, and it clears every one of their entries:

| Check | Result |
|---|---|
| Entries the server accepted that `is_legal` rejects | **0 of 67** |
| Array size the server accepted | 24 and **43** — size alone is not the limit |
| Turn-slot collisions in a generated list | 0 |
| Year-1 months used vs months the server accepted | identical (6–12) |
| Junior-permission programs earlier than month 6 | none exist |

That proves the model never rejects a *valid* race — but not the converse. It
says nothing about whether a race the model accepts is one the trainee can
actually enter, and `single_mode_program` carries at least one requirement the
model ignores: `need_fan_count`. Auto-filling picked races demanding hundreds
of fans in the junior year, when a fresh trainee has almost none.

So a large race list is not itself the problem — those 24- and 43-race
captures were *player-built* agendas of races that trainee could enter. An
auto-generated list of the same size is a different thing entirely, which is
why the default is now the route goals alone.

`race_permission` is an **enum, not a bitmask** — verified against 213 real
agenda entries with zero violations:

| Value | Years the race may be run in |
|---|---|
| 1 | junior (year 1) |
| 2 | classic (year 2) |
| 3 | classic **or** senior (years 2–3) |
| 4 | senior (year 3) |

`master.mdb` is read from the Steam install
(`steamapps\common\UmamusumePrettyDerby\...\Persistent\master\master.mdb`),
overridable with `AIC_MASTER_MDB`. It is copied to a temp file and opened
read-only — never written to — and the extracted tables are cached to
`state/race_rules.json`, so it only has to be present once.

Without it, `aic` warns and falls back to the raw agenda (which may be refused).
`--no-races` sends an empty list, which starts but earns no fans.

## Succession rank points

`current_succession_rank_point` in the start payload is **not** the account's
banked balance (item 75). It is an affinity score computed from the trainee and
its two selected parents' family tree, and the server validates it. Sending the
balance gets the start refused.

The value is the sum of **seven terms**, named as the in-game compatibility
calculators label them:

| Term | What it is |
|---|---|
| Main Char — Legacy 1 | `affinity(trainee, parent1)` |
| Main Char — Legacy 2 | `affinity(trainee, parent2)` |
| Legacies' affinity | `affinity(parent1, parent2)` |
| Sub-Legacy 1-1 / 1-2 line | `line(trainee, parent1, gp)` |
| Sub-Legacy 2-1 / 2-2 line | `line(trainee, parent2, gp)` |

Two things about this took a long time to find:

- **A grandparent contributes through a three-way intersection** — the relation
  types common to the trainee, *that parent*, **and** the grandparent — not a
  pairwise link. Four of the seven terms are three-way, so no combination of
  pairwise links can ever reproduce the real values. Every pairwise search
  failed for this reason.
- **A grandparent that is the trainee's own character scores zero.** Otherwise
  the three-way collapses to `affinity(trainee, parent)` and pays out large.

On top of that, each of the five *parent* pairings adds **3 per distinct G1
race both of its members won**. The win rule changed retroactively:

| | Old | Current |
|---|---|---|
| Shared G1 | +1 | **+3** |
| Shared G2 / G3 | +1 | **0 — no longer counts** |
| Winning *titles* (Triple Crown etc.) | a separate variable | no longer used |

Four details matter and each was needed to reproduce a real value:

- **Wins only.** `win_saddle_id_array` is the winning-saddle list, and only
  races **both** characters won score (a set intersection).
- **Per distinct race, not per saddle.** A saddle can stand for several
  `race_instance` rows (15 of the 181 do), and the same race run in a different
  year is separate instances but the same `race_id`. Winning Arima Kinen in
  both classic and senior year counts **once**.
- **Per real race, not per `race_id`.** The same race can carry several
  `race_id` rows: the JBC races move venue every year and each venue is its own
  id (four rows all named "JBC Classic"), and scenario re-issues duplicate ids
  the same way (two Takarazuka Kinens, two Kikuka Shos, ...). Ids are collapsed
  to one canonical id per race **name** (`text_data` category 32). Without this
  the value undercounts by 3 per cross-variant shared win — the exact −6/−12
  residuals two accepted starts used to show; both land at 0 with it.
- **G1 is `race.grade == 100`** — grades run 100/200/300, not 1/2/3.

The extracted saddle map is cached to `state/g1_saddle_races.json`, so only one
machine ever needs the game installed; `master.mdb` is also discovered in
mounted `SteamLibrary` folders (including this project's own drive) and via
`AIC_MASTER_MDB`.

**Validated against seven independent references — every real accepted value
reproduces exactly:**

| Reference | Computed | Expected | |
|---|---|---|---|
| Calculator — Kitasan Black run | 98 | 98 | ✅ every line term matches (0, 19, 8) |
| Calculator — Admire Vega run | 199 | 199 | ✅ |
| Calculator — Curren Chan run | 186 | 187 | −1, likely a name→id lookup |
| Real accepted start, trainee 102401 | 295 | 295 | ✅ heavy G1 wins |
| Real accepted start, trainee 106001 | 396 | 396 | ✅ needs the JBC collapse |
| Real accepted start, trainee 106801 | 404 | 404 | ✅ rental parent; ditto |
| Real accepted start, trainee 100701 | 127 | 127 | ✅ seed tree, zero wins |

One caution when validating against captures: `trained_chara_id` is a
per-account sequence number **and ids are reused after a remove** — resolving a
start's parents in an unrelated (or stale) dump silently yields a different
character's tree. The 396 run above first "failed" by −114 for exactly that
reason; filtering the tree lookup by the start's own `viewer_id` fixed it.

The affinity primitive itself is independently confirmed: a private-server
implementation computes it with the same SQL and its worked example
(`1001+1002=27, 1001+1003=25, 1002+1003=20 → 72`) reproduces exactly.

`saddle_g1_races()` reads the G1 mapping from master.mdb and returns empty if
master.mdb is missing, so the bonus vanishes rather than being applied wrongly.
Note the win bonus is invisible on freshly farmed accounts, whose characters
have empty win lists.

**The formula is exact — 22/22.** Against every server-accepted start captured
from the real client (`UmaDumpy-main/dumps`) it reproduces the accepted
`current_succession_rank_point` exactly: 5 accounts, 12 trainee cards, rental
and non-rental parents, parents holding 0–38 G1 saddles, both the `single_mode`
and `idle_single_mode` flows. The set was rebuilt three independent times by
three different roster-resolution rules, all agreeing.

Ablation confirms every term is load-bearing (dropping any one scores 0/22), the
grandparent term must be the **three-way** intersection, a grandparent that is
the trainee's own character must score **zero**, and the win bonus must count
per **race name** (raw `race_id` scores 12/18).

Two traps, recorded so they are not re-entered:

* **The tree is three generations, not four.** Six candidate great-grandparent
  terms were tested; none is silent across the samples, so any non-zero weight
  breaks a perfect fit. The trees that put the trainee in its own
  great-grandparent layer fit exactly *without* one. (An earlier version of this
  document argued the opposite; it was wrong.)
* **The server accepts a band above the true value, not just the value.** On one
  byte-identical tree the real client sent **127** and the server also accepted
  **146**. So a search "finding" a higher number never proved the computed one
  wrong.

Because of that band, a 205 refusal is *not* evidence the rank point is wrong.
Proven by A/B from the run logs: two accounts minutes apart, identical trainee,
identical parents, identical affinity breakdown and identical race plan — one
swept **380 values and was refused at every one**, the other was accepted at the
first call. They differed only in the deck and borrowed card. A second, directly
proven cause is an invalid `race_array` (two races on one turn).

So **the search is off by default**. `aic` sends the computed value; if that is
refused it fails loudly and dumps the payload to
`state/<profile>/last_refused_start.json` for diffing, rather than burning ~40
live calls on the wrong field. `--sweep` re-enables the search as a debugging
aid. Learned offsets are no longer applied to the first call: every entry that
existed on disk was minted by a formula generation that has since been replaced,
and adding one to a correct value lands 65–86 points off.

**Haru Urara cannot be trained.** Chara 1052 is blocked outright: her route has
no named classic-year race (those goals are fan-count objectives), so she always
produces `y2=0`, and she is 0-for-7 across three accounts, two code generations
and five different rank points while every other trainee tried has succeeded.
She is skipped in automatic selection and `--trainee 105201` is refused with an
explanation. Set `AIC_ALLOW_TRAINEE=1052` to try anyway.

**Fresh accounts need a padded race plan.** A trainee's route alone can leave a
year with no races at all — chara 1052's classic-year goals are fan-count
objectives rather than races, so its route yields `y1=1, y2=0, y3=4`. Every
start on record with an empty year 2 was refused; every accepted plan had at
least three year-2 races. `rotate.sh farm --start-career` therefore always pads
(and padding also guarantees one race per turn).

## Rotating many accounts (`rotate.bat`)

An idle career takes 50 minutes. With *N* accounts, visiting one every
`50 / N` minutes means each is revisited exactly as its own career finishes — so
every account is always mid-career and no time is wasted.

```
rotate.bat banners           list the gacha ids currently on offer
rotate.bat farm --banner ID  create accounts and add them to the roster
rotate.bat init              or: build the roster from saved Link Passwords
rotate.bat plan              print the schedule, change nothing
rotate.bat run --dry-run     validate the roster and print the plan
rotate.bat run               farm careers forever
rotate.bat run --cycles 2    stop after 2 full passes
```

### Creating the accounts

```powershell
rotate.bat banners                                     # find the gacha id
rotate.bat farm --banner 30111 --count 20 --pulls 3
rotate.bat plan
rotate.bat run
```

`farm` is a trimmed reroll: it does **not** judge what it pulls, because the goal
is a roster rather than a lucky account. Each cycle disconnects the slot, signs
up, skips the tutorial, claims the present box, does `--pulls` ten-pulls on the
banner, publishes a Data Link, and appends the account to
`reroll_accounts.json`. The roster is rewritten after **every** account, so an
interrupted run loses nothing.

Two details carried over from uma-reroll-farm, both load-bearing:

- **Call order.** `load/index` goes *before* claiming presents, and the call
  after `present/receive_all` must be `present/index` — a `load/index` there
  breaks the gacha with 1808. So the carat balance comes from `receive_all`'s
  `add_fcoin`, not from `coin_info`.
- **Where the pauses go.** Requests are *not* individually rate-limited — the
  gacha needs `gacha/index` → `gacha/exec` back to back in the session that
  created the account, and a delay over ~10s inside that window returns 1808.
  The human-like gaps sit **between** pulls (~2s) and **between** accounts
  (~6–14s, with an occasional longer break). `--pace` scales those; `--pace 0`
  removes them.

An account whose Data Link fails to publish is **not** added to the roster: it
would be destroyed by the next cycle with no way back.

> Creating many accounts from one IP is exactly the pattern account-integrity
> systems look for, and it is against the game's terms. `--pace 1.0` is roughly
> 5 minutes per account for a reason.

| Accounts | Visit every | Full lap |
|---|---|---|
| 2 | 25 min | 50 min |
| 5 | 10 min | 50 min |
| 20 | 2.5 min | 50 min |

Each visit is: **logout** (frees the Steam slot) → **login** the next account →
**end** its finished career → **start** a fresh one.

`reroll_accounts.json` (see `reroll_accounts.example.json`):

```json
{
  "career_minutes": 50,
  "accounts": [
    { "trainer_id": "103 444 444 441", "password": "ChangeMe2026", "name": "alpha" }
  ]
}
```

Trainer IDs may be spaced, dashed or plain. The password is the account's **Link
Password**, which is both how it is taken over and how it is recovered after a
logout — the roster is registered with the link store at startup so `logout`
never blocks mid-run.

`name` is a **fallback** label, not the one you will normally read. Once an
account has been taken over once, the log and the Discord report call it by its
**in-game trainer name** (the `chained_user_name` the server returns, kept in
`state/accounts.json`), so a line names something you can find in the client.
The roster `name` shows only until that first takeover — which is exactly what
a freshly added account displays. Both are valid `--only` arguments, as is the
label you see in the log; two accounts sharing an in-game name each keep the
last four digits of their trainer id to stay distinguishable.

Things it does deliberately:

- **Slots are absolute times** computed from the start, not delays added up as
  it goes, so a slow visit does not push every later one back. Falling behind is
  reported rather than silently absorbed.
- **A career still running is left alone.** If the schedule slips, ending early
  would throw the career away, so the visit is skipped with a note.
- **One failed account does not stop the rotation** — it is logged and the next
  account is visited on time.
- **Everything runs in one process**, so a single Steam session covers the whole
  rotation. That is what keeps Steam Guard out of the loop entirely.

> `reroll_accounts.json` holds recovery credentials in plain text. It is in
> `.gitignore`; keep it that way.

## Account switching (`account.bat`)

```
account.bat status                          which account this profile is bound to
account.bat list                            Trainer IDs with a saved Link Password
account.bat remember "<trainer id>" <pw>    record credentials you already know
account.bat save-link <password>            publish a Data Link
account.bat logout [--link-password PW]     unlink from Steam, freeing the slot
account.bat login "<trainer id>" <password> take over another account
account.bat capture                         capture credentials from the game (frida)
account.bat import-capture [DUMP_DIR]       repair auth_config from a captured switch
```

A rotation only needs `remember` once per account — `login` records the password
it used, so every account switched into is on record from then on and `logout`
runs unattended with no prompt.

Trainer IDs are accepted spaced, dashed or plain — `"103 444 444 441"` and
`103444444441` are the same thing.

> **`logout` is destructive.** `account/steam_chain_disconnect` is the game's
> "Delete User Data": it severs the Steam↔account link. Without a Data Link
> (Trainer ID + Link Password), that account is unrecoverable by anyone.
> With a link on record it runs silently — the account is restorable, so
> unlinking is reversible. Without one it refuses, and suggests `remember`,
> `--link-password`, or `--force` (which then asks you to type the Trainer ID
> back unless `--yes`).

Link passwords are recorded in `state/accounts.json` **in plain text** — a
recovery credential you can't read is useless when you need to type it into the
real client. The previous `auth_config.json` is archived to
`state/<profile>/auth_archive/` before either operation.

### Capturing credentials

```powershell
account.bat capture                  # launches the game, attaches, captures
account.bat capture --no-launch      # attach to a game already running
account.bat capture --kill-after     # close the game afterwards (see the caveat)
```

[capture.py](aic/capture.py) attaches with frida, hooks Unity's TLS write,
reassembles the outgoing HTTP POST and reads the wire envelope:

```
<u32 header_len> | HEAD | sid(16) | udid(16) | rand(32) [| auth_key(48)]
                 | AES-CBC(msgpack payload) | key(32)
```

The header yields `udid` and `auth_key`; decrypting the body with that udid
yields the Steam ticket, `steam_id` and the device fields; the HTTP headers give
`APP-VER` / `RES-VER`.

Two cases it handles that a naive reader would not:

- **No linked game account.** Then the client only sends pre_signup/signup, with
  no `auth_key` — and the udid sits at a *different offset* in the envelope
  (`[-48:-32]` rather than `[-96:-80]`). Both are tried. The ticket is still
  recovered; the existing `viewer_id`/`auth_key` are left alone.
- **The `ViewerID` header lies.** It can carry a per-response session alias, so
  the *payload's* `viewer_id` wins. Using the header gives 709 → 501 on the next
  `start_session`.

`--kill-after` is off by default on purpose: Steam cancels the app session
ticket when the game exits, so leaving it running is what keeps the captured
ticket usable.

### How login works

Reconstructed from `dumps/20260731_093457`. Taking over an account first creates
a *throwaway* account on the free Steam slot, then transfers the target onto it:

```
tool/pre_signup                    {attestation_type: 1, device_token: null}
tool/signup                        {credential, optin_user_birth, country, ...}
    -> throwaway viewer_id + auth_key
account/get_by_transition_code     {password, input_viewer_id}   confirmation preview
account/chain_by_transition_code   {password, input_viewer_id}   the takeover
    -> {chained_viewer_id, chained_user_name, auth_key}
tool/start_session -> load/index
```

Three things make this work, and each answers **102** if wrong:

- **The Steam slot must be free.** Run `logout` first — a new account cannot be
  created while one is linked.
- **The signup needs a fresh `udid`** (`uuid4()`), not the previous account's.
  `device_id` is dropped too and regenerated. The account that comes back is
  bound to *that* udid, so it is persisted with the new credentials — keeping
  the old udid makes every later call fail with 202.
- **`tool/pre_signup` takes `{attestation_type: 1, device_token: null}`.** The
  `cygames_id_client_sdk_version` payload belongs to `account/index`; the dump's
  endpoint labels are shifted and reading them the wrong way round fails the
  signup that follows.

The password is never sent in the clear: it is `md5(password + SALT)`, the same
hashing `account/publish_transition_code` uses when the link is created. The
returned `auth_key` is base64; it is stored as hex in `auth_config.json`.

The dump's endpoint labels are shifted in places, so both transition endpoints
are called in order and whichever response carries the `auth_key` is used.

### When credentials go stale

Switching accounts *anywhere else* — the real client, or `account.bat login` on
another machine — leaves `auth_config.json` pointing at the previous account.
Login then fails with **709 → 501** (viewer mismatch, then a rejected auth_key)
or **202** (viewer right, auth_key rejected).

`account.bat import-capture` repairs this without frida when the switch itself
was captured: the `*_by_transition_code` response contains the new account's
`auth_key`, and only the account identity needs replacing — `udid`, `app_ver`,
`res_ver` and the device fields are client-level and carry over.

It does **not** help when the switch happened under a different `udid`, because
auth_key is bound to the udid in the same request header. Then only a fresh
frida capture through Icarus will do.

## Selection rules

Enforced client-side, because the server answers a bad combination with a bare
result code 102:

- Neither succession parent may be the same **character** as the trainee.
- The two parents may not be the same character as each other.
- **The two parents may not share any trained character in their family trees.**
  Two shapes, both seen live:
  - one is the other's own parent (parent *and* grandparent);
  - they share a parent, so that character is a grandparent on both sides —
    e.g. id 7 (parents 6, 4) with id 1 (parents 2, 4): id 4 twice.

  This bites constantly in a rotation, because the character just retired has
  the previous pair as its parents and by rank is the obvious next pick. All
  pairs are considered in rank order, not just pairs containing the strongest.
  Note this is *trained character* identity — the same uma via different trained
  copies is ordinary pedigree building and stays allowed.
- No two support cards in a deck may be the same character.
- No support card may be the trainee's character.
- Team Sirius conflicts (Mejiro McQueen, Rice Shower, Winning Ticket, Narita
  Brian, Silence Suzuka, Special Week) are respected.

A career runs **six** support cards: five from a deck plus one borrowed from
another trainer, taken from `pre_single_mode/index`. The borrowed card obeys the
same character rules, and the offer list is refreshed immediately before each
start because it is per-session.

Character identity comes from card *names*, normalized by stripping
parentheticals and non-alphanumerics — support card ids carry no character id,
so `Oguri Cap (Christmas)` and `Oguri Cap` collapse to the same key. Names are
read from the Icarus checkout's `data/support_list.json` and `data/chara_list.json`.

**If that table is out of date, duplicate-character checks silently stop
covering the missing cards** — there is no way to know a card's character from
its id alone. `aic` warns once per run listing the unknown ids. Rarity does
degrade gracefully (the leading digit of the id encodes it: `1xxxx` R, `2xxxx`
SR, `3xxxx` SSR), but the character cannot. `uma-reroll-farm/tools/update_support_list.py`
rebuilds the table from umapyoi.net.

Parents are chosen in this order:

1. a pair with **no grandparents** — the succession rank point is exact for
   those, so the start is accepted on the first call (see below);
2. within that, a pair with no shared ancestry, as a tie-break only;
3. otherwise the highest-`rank_score` legal pair, with a warning.

Rank is deliberately last. Parent strength does not matter when farming fans,
and it is precisely the strong career-produced parents that break the rank-point
calculation. The trainee is random among
owned cards that leave two differently-charactered parents available. Deck
preference: `--deck` → the account's last-used deck → any other saved deck that
is legal → a freshly built one (random among eligible cards, weighted toward
rarity). There is no deck-save endpoint; the game persists the deck from the
`support_card_ids` + `select_deck_id` pair in the start payload.

## Training schedule

`start_info` in the start request, with four fields:

| Field | Meaning |
|---|---|
| `training_policy_ground_type` | 1 turf, 2 dirt |
| `training_policy_param_rate_set_id` | which stat-weighting preset the auto-trainer uses |
| `priority_skill_array` | `{priority, skill_id}` — skills to buy first |
| `race_array` | `{year, program_id}` — the races to enter |

**The default is whatever the account used last**, read live from `pre_start`:
the policy and priority skills come from `last_idle_single_mode_start_info`, and
the races from a saved race agenda (`--race-agenda N`, else the account default,
else the fullest). That is what makes step 6 work without you having to specify
anything yet.

To override, pass a JSON file — fields you omit keep their live default:

```powershell
.\aic.bat run --schedule my_schedule.json
```

```json
{
  "training_policy_ground_type": 1,
  "training_policy_param_rate_set_id": 2,
  "priority_skill_array": [{ "priority": 1, "skill_id": 201601 }],
  "race_array": [{ "year": 1, "program_id": 624 }]
}
```

`aic schedule` prints exactly what would be sent, plus the available agendas.

## Steam tickets

Minting a Steam session ticket is a **real Steam login** — `steam-user` signs in
each time. Doing that on every command trips Steam Guard, which then blocks
everything until a refresh token is re-cached.

So the stored ticket is reused while it is under **7 minutes** old (matching
uma-reroll-farm), stamped with `steam_ticket_ts` in `auth_config.json`, falling
back to the file's mtime when there is no stamp. `--no-ticket-refresh` (a global
flag, before the subcommand) skips minting entirely.

A minting failure is never fatal: the stored ticket is usually still good, and
if it is not the login error says so. Treating Steam Guard as fatal turned a
rate-limited Steam into a dead tool.

### The persistent session (what actually solves this)

**Steam cancels an app session ticket when the process that owns it exits.**
Icarus' `get_ticket` spawns node, mints a ticket and returns — so the ticket
starts dying immediately and every command needs a fresh **Steam login**. At any
real rotation rate that trips Steam Guard.

[steam.py](aic/steam.py) keeps the `steam-user` process alive instead. The ticket
is minted inside the `loggedOn` handler and stays valid for as long as the
process lives, so **one login covers the whole run** however many accounts get
switched. Held per process and closed at exit.

Two things make it unattended after the first run:

- Steam emits a **refresh token** after a 2FA login. It is stored as
  `steam_refresh_token` in `auth_config.json`, and later runs log in with it —
  no password, no Guard code.
- If that token is ever rejected, it falls back to a password login, and to a
  one-shot `get_ticket` if the session cannot start at all.

First run needs one Guard code, entered at the prompt or via `--steam-code`:

```powershell
aic.bat --steam-code 12345 status
```

After that it is automatic. For a shell-driven rotation each command is its own
process, so each does one refresh-token login — light, and Guard-free. A single
long-running process (`aic.bat run --loop`) reuses one session throughout.

### Keeping the game open also works

**Steam cancels an app session ticket when the process that owns it exits.** So a
ticket's usable life is bounded by the game process, not by a timer — a
five-second-old ticket from a closed game is already dead.

That is why uma-reroll-farm can create hundreds of accounts without ever
tripping Steam Guard: it does not log into Steam. `ensure_fresh_ticket` keeps
the game running (`kill_after=False`) and lifts the ticket out of the live
process with frida (`capture_from_game`). Its `steam_user_ticket` — the
`steam-user` login this tool uses — is documented there as *"Fallback... only as
a last resort"*.

This tool currently only has the fallback, so it gates reuse on **age and
`game_is_running()`**, and says so when the game is closed. For a high-rate
rotation, leaving the game open is worth far more than any 2FA arrangement:
no Steam logins, no Guard, no rate limit.

### The refresh token is the normal path

`TICKET_GEN_JS` caches a Steam **refresh token** at
`uma_runtime/<profile>/steam_auth_data/<username>_refresh.txt`. When it exists,
minting uses it — no password, no 2FA, no Guard prompt. Guard only appears when
that file is missing or Steam rejected it (the generator deletes it and falls
back to password login).

So a Guard prompt usually means *the refresh token is gone*, not that you need a
shared secret. One successful login re-seeds it:

```powershell
aic.bat --steam-code 12345 status
```

### Unattended account switching needs the 2FA secret

Every account switch needs a valid ticket, and minting one means logging into
Steam — which Steam Guard will interrupt. Storing your **shared secret** lets
`steam-user` generate the 2FA code itself, so switching runs unattended forever:

```powershell
account.bat steam-secret <shared_secret>     # from your Steam maFile
account.bat steam-secret                     # show whether one is stored
```

It is the `shared_secret` field in a Steam Desktop Authenticator `.maFile`
(**not** `identity_secret`). Stored in `auth_config.json` and verified by
minting a ticket immediately. Without it, `--steam-code 12345` passes a one-off
code by hand.

How much this matters scales with the rotation:

| Rotation | Ticket mints | Needs the secret? |
|---|---|---|
| One account, 50-min careers | ~1 per cycle | no |
| A few accounts an hour | a few per hour | probably not |
| ~20 accounts per 50 min | ~10 per 50 min, sustained | yes — the refresh token *will* be rejected eventually, and without the secret the rotation stops dead |

A ticket lives about 5–7 minutes, so at a switch every 2.5 minutes one ticket
covers roughly two switches. The cache is what keeps that number at ~10 rather
than ~20.

## Server clock

The server sends naive datetime strings with no timezone (observed rendering at
UTC−1), while `data_headers.servertime` on every response is a real epoch.
Recovering the offset between them is what makes "time remaining" correct, and
it is the fiddliest part of this tool. Four sources, best wins:

| Source | Exact? | When |
|---|---|---|
| `fresh start_time` | yes | our own `start` response — the timestamp is seconds old |
| `career window` | yes for a live run | only one whole-hour offset can put `servertime` inside a sub-hour `[start, end]` |
| `update_time anchor` | approximate | `user_info.update_time`, floored to the hour |
| `cached` / `assumed` | — | last learned value for the profile, else +3600 |

Two traps worth knowing, both of which produced wrong answers before:

- **`user_info.update_time` is stamped when the career starts, not when you log
  in.** Deep into a 50-minute run it is ~40 minutes stale, and rounding that gap
  to the *nearest* hour lands an hour late — inflating every countdown by 3600s,
  which the duration clamp then displays as a full-length run remaining. The gap
  is floored, not rounded, because staleness is always non-negative.
- **A run that expired N hours ago gives the same unique window solution as a
  live run under an offset N hours larger.** These fields cannot distinguish
  the two. That is why the offset is persisted per profile
  (`state/<profile>/server_clock.json`) and why a weaker source never overwrites
  a stronger one — a clock pinned from a real start stays pinned.

Countdowns are measured as "what was left when the server told us" minus "how
long we have been running since", so a local clock disagreeing with the server's
never skews them, and remaining time is clamped to the run's own duration as a
last-resort guard. `aic status` prints the offset and which source set it.

## Tests

```powershell
$env:AIC_TEST_CAPTURE = "path\to\..._load_index_incoming.json"
$env:AIC_TEST_DUMPS   = "C:\Users\...\UmaDumpy-main\dumps"
..\Icarus-Dev-Build-Private-main\.venv\Scripts\python.exe tests\test_offline.py
```

No network and no credentials. Synthetic checks always run. With the two env
vars set it additionally:

- replays every captured `idle_single_mode` exchange through the parsers;
- exercises account parsing, parent selection and deck building on real data;
- asserts the `start` body we generate has the same key structure as the real
  client's captured request — top level, `start_chara`, the common block,
  `start_info`, and `use_tp`;
- covers the TP probe: cheap-first ordering, fall-through on a mismatch
  rejection, skipping unaffordable candidates, and that unrelated errors (709,
  session loss) still propagate instead of being swallowed by the retry loop.

## Layout

```
aic_cli.py              entry point            aic.bat  launcher
aic/
  config.py             paths, profile resolution, item ids
  bootstrap.py          puts the Icarus checkout on sys.path (ordering matters)
  auth.py               credential load, Steam ticket refresh, boot to /load
  state.py              load/index parsing, career detection, ServerClock
  gamedata.py           support/chara name tables from the Icarus data dir
  selection.py          trainee, parents, deck; all the legality rules
  independent.py        the idle_single_mode API + schedule building
  career.py             end sequence (idle end -> factor_select -> finish)
  flow.py               the seven steps, composed
  cli.py                argument parsing and dispatch
protocol/independent.json   optional endpoint/payload overrides
state/<profile>/        logs and discovery captures
```

## Notes

- Credentials are redacted on the console. Icarus' trace logs under
  `uma_runtime/*/trace_logs/` and UmaDumpy's `dumps/` are **not** redacted and
  contain live Steam session tickets.
- UmaDumpy labels request/response inconsistently between `messages/` and
  `_preshift_backup/`; trust the payload contents, not the filename.
- **Icarus' `[ERROR 205] Payload sent:` dump truncates every list to 20 items**
  (`redact_for_console`). A 36-race agenda shows as 20 there — the real request
  is complete. Do not chase it.
- Icarus retries 205 three times by default, and its no-retry exclusion list is
  keyed on the *scenario* prefix, so `idle_single_mode/*` is not covered. `aic`
  passes `retry_205=0` on start probes; without it each probe costs four server
  calls and four screens of error dump.
- One `UmaClient` serializes all calls internally, so a single process is safe;
  do not share a session across threads.
