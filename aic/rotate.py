"""Rotate a roster of accounts so their idle careers overlap seamlessly.

An idle career takes ~50 minutes of wall clock. With *N* accounts, visiting one
every ``50 / N`` minutes means each account is revisited exactly when its own
career finishes - so every account is always mid-career and no time is wasted.
Five accounts is a visit every 10 minutes; twenty is every 2.5.

Each visit:

    logout (frees the Steam slot)  ->  login the next account
      ->  end its finished career  ->  start a fresh one

Visits are scheduled from **each career's own recorded clock**, not from a fixed
slot: every visit writes down when that account's career started and when it is
due (see :mod:`aic.careerclock`), and the rotation always goes to whichever
account is due soonest. So a slow visit does not push later ones back, an early
one does not cost a whole lap, and a restart picks the real schedule back up
instead of guessing. The ``career_minutes / N`` spacing is only the opening
stagger, for accounts nothing has been recorded about yet.

Everything runs in **one process**, which matters: the Steam session is held open
for the whole rotation, so one login covers every switch (see :mod:`aic.steam`).
"""

from __future__ import annotations

import json
import os
import random
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import account as acct
from . import career, careerclock, chores, fancap, flow, independent, ace
from . import selection as sel
from .config import PROJECT_ROOT, ConfigError, Settings
from .logging_setup import get
from .state import KIND_IDLE, format_duration, read_account

log = get("rotate")

DEFAULT_ROSTER = PROJECT_ROOT / "reroll_accounts.json"

#: How long an idle career runs. Every capture shows exactly 50 minutes.
CAREER_MINUTES = 50.0

#: Don't schedule visits closer together than this - a visit is ~6 API calls
#: plus two account switches, and slots any tighter would overlap.
MIN_INTERVAL_SECONDS = 60.0

#: How long to wait before retrying an account whose visit failed.
RETRY_SECONDS = 300.0

#: Ceiling on the backoff for an account that keeps failing. Every retry costs a
#: full Steam-slot switch (disconnect + throwaway signup + chain), so an account
#: with a permanent fault - a career the server will not let us end, say - must
#: not be re-tried at RETRY_SECONDS forever while the rest of the roster works.
MAX_RETRY_SECONDS = 3600.0
#: Failed visits in a row after which a one-time Ace Run account is parked.
ONCE_MAX_FAILURES = 3


def _jitter_range() -> tuple[float, float]:
    """AIC_VISIT_JITTER_MIN / _MAX (minutes, default 5 / 20); 0 / 0 turns it off."""
    def minutes(name, default):
        try:
            return max(0.0, float(os.environ.get(name) or default))
        except ValueError:
            return float(default)
    lo, hi = minutes("AIC_VISIT_JITTER_MIN", 5), minutes("AIC_VISIT_JITTER_MAX", 20)
    return lo * 60, max(lo, hi) * 60


#: A random wait added once an account's career is due, before it is visited, so
#: visits never fall on a fixed career-length rhythm (seen from outside, e.g.
#: uma.moe activity, a 50-minute metronome per account). Costs throughput: the
#: average cycle grows by the midpoint (12.5 min on the 5-20 default).
VISIT_JITTER_SECONDS = _jitter_range()


def _visit_jitter() -> float:
    lo, hi = VISIT_JITTER_SECONDS
    return random.uniform(lo, hi) if hi > 0 else 0.0


class RotationError(RuntimeError):
    pass


@dataclass
class RosterAccount:
    trainer_id: int
    password: str
    name: str = ""
    #: Trainee to train on THIS account, as written by the operator (e.g.
    #: "Oguri_alt"). Resolved against the account's own owned cards at visit
    #: time - a card id would be per-account and unmemorable. Empty = let the
    #: selection pick.
    trainee: str = ""
    #: The account's own IN-GAME trainer name ("McSpade"), filled in by
    #: :func:`apply_ingame_names` from the accounts.json that
    #: ``aic.account.remember_account`` rewrites on every successful chain.
    #: Wins over ``name`` for display: that is a roster bookkeeping nickname
    #: ("Oguri_5943"), this is what the account is actually called in the
    #: client, so a log line names something you can find in game.
    ingame_name: str = ""
    #: One-time Ace Run job (roster ``"ace_once": 5``): this many Ace Runs in
    #: total, never a fan career, then out of the rotation for good. 0 = a normal
    #: farm account.
    ace_once: int = 0
    #: ``"club": false`` keeps the account out of the club chore AND out of the
    #: club's "every account has requested" gate - for an account that is not in
    #: the farm's club, which would otherwise hold everyone's donating back 3h.
    club: bool = True

    @property
    def label(self) -> str:
        return (self.ingame_name or self.name
                or acct.format_trainer_id(self.trainer_id))


@dataclass
class VisitResult:
    account: RosterAccount
    ok: bool = False
    ended: bool = False
    started: bool = False
    finishes_at: float | None = None
    #: When the career the visit saw/started began, server epoch. Recorded for
    #: reading (aic/careerclock.py), never used for scheduling.
    started_at: float | None = None
    #: Seconds until this account's career finishes, measured locally at visit
    #: time. Preferred over ``finishes_at`` for scheduling: that is
    #: server-epoch, and the server clock runs hours from local time.
    remaining_seconds: float | None = None
    #: What produced ``remaining_seconds``: "started", "still-running",
    #: "fan-cap" or "failed". Written into the clock file, and decides the
    #: fallback when there is no clock at all.
    source: str = ""
    note: str = ""


@dataclass
class RotationState:
    visits: int = 0
    failures: int = 0
    started_careers: int = 0
    results: list[VisitResult] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# roster
# --------------------------------------------------------------------------- #

def load_roster(path: Path = DEFAULT_ROSTER,
                settings: Settings | None = None) -> tuple[list[RosterAccount], dict]:
    """Read ``reroll_accounts.json``.

    Accepts either a bare list of accounts or an object with an ``accounts``
    key plus overrides (``career_minutes``).

    Pass ``settings`` to label the entries with their in-game trainer names
    (:func:`apply_ingame_names`); without it they keep their roster nicknames.
    """
    if not path.is_file():
        raise RotationError(
            f"no roster at {path}\n"
            f"  Create it, or generate one from the accounts already recorded:\n"
            f"      rotate.bat init"
        )
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            raw = json.load(fh)
    except (OSError, ValueError) as exc:
        raise RotationError(f"could not read {path}: {exc}") from exc

    options: dict = {}
    if isinstance(raw, dict):
        options = {k: v for k, v in raw.items() if k != "accounts"}
        rows = raw.get("accounts") or []
    elif isinstance(raw, list):
        rows = raw
    else:
        raise RotationError(f"{path} must contain a list or an object")

    accounts: list[RosterAccount] = []
    seen: set[int] = set()
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            raise RotationError(f"{path}: entry {i} is not an object")
        raw_id = row.get("trainer_id") or row.get("viewer_id")
        password = row.get("password") or row.get("link_password") or ""
        if not raw_id or not password:
            raise RotationError(
                f"{path}: entry {i} needs both 'trainer_id' and 'password'")
        trainer_id = acct.parse_trainer_id(str(raw_id))
        if trainer_id in seen:
            log.warning("roster lists %s twice; keeping the first",
                        acct.format_trainer_id(trainer_id))
            continue
        seen.add(trainer_id)
        try:
            once = max(0, int(row.get("ace_once") or 0))
        except (TypeError, ValueError):
            raise RotationError(f"{path}: entry {i} has a bad 'ace_once' "
                                f"({row.get('ace_once')!r}) - a number of Ace Runs") from None
        accounts.append(RosterAccount(trainer_id, str(password),
                                      str(row.get("name") or ""),
                                      str(row.get("trainee") or ""),
                                      ace_once=once, club=row.get("club") is not False))

    if not accounts:
        raise RotationError(f"{path} lists no accounts")
    if settings is not None:
        apply_ingame_names(accounts, settings)
    return accounts, options


def apply_ingame_names(accounts: list[RosterAccount], settings: Settings) -> None:
    """Label each roster entry with its in-game trainer name, where known.

    The names come from ``accounts.json``, which
    :func:`aic.account.remember_account` rewrites with the ``chained_user_name``
    the server returns on every successful chain - so a rename in game follows
    on its own, and an account this profile has never chained (a fresh roster
    addition) simply keeps its nickname.
    """
    # names_by_viewer already resolves accounts that share a name, so the label
    # here and the one in the Discord report cannot drift apart.
    known = acct.names_by_viewer(settings)
    for account in accounts:
        name = known.get(account.trainer_id, "")
        if not name:
            continue
        was = account.label
        account.ingame_name = name
        if account.label != was:
            # Says why a name in the log changed: a first chain replacing the
            # nickname, an in-game rename, or a tail appearing because another
            # account turned up wearing the same name.
            log.info("%s is now labelled %s.", was, account.label)


def write_roster_from_saved(settings: Settings, path: Path = DEFAULT_ROSTER,
                            overwrite: bool = False) -> int:
    """Seed a roster from the link passwords already on record."""
    if path.exists() and not overwrite:
        raise RotationError(f"{path} already exists (pass --force to replace it)")
    saved = [r for r in acct.load_accounts(settings) if r.get("link_set")]
    if not saved:
        raise RotationError(
            "no accounts with a saved Link Password yet.\n"
            "  Record them first:  account.bat remember \"<trainer id>\" <password>")
    payload = {
        "career_minutes": CAREER_MINUTES,
        "accounts": [
            {"trainer_id": r.get("trainer_id"), "password": r.get("password"),
             "name": r.get("name") or ""}
            for r in saved
        ],
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return len(payload["accounts"])


def pick_account(accounts: list[RosterAccount], spec: str) -> RosterAccount:
    """The roster entry ``spec`` names - by name, or by trainer id digits."""
    text = str(spec).strip()
    digits = "".join(ch for ch in text if ch.isdigit())

    # Either name matches: the roster nickname keeps working after the in-game
    # name takes over the label, and the label you read in the log is a valid
    # --only argument.
    wanted = text.lower()
    by_name = [a for a in accounts
               if wanted in {n.lower() for n in (a.name, a.ingame_name, a.label) if n}]
    if len(by_name) == 1:
        return by_name[0]
    by_id = [a for a in accounts if digits and str(a.trainer_id) == digits]
    if len(by_id) == 1:
        return by_id[0]

    names = ", ".join(a.label for a in accounts)
    raise RotationError(
        f"--only {spec!r} does not match exactly one roster account.\n"
        f"  Roster: {names}")


def interval_for(accounts, career_minutes: float = CAREER_MINUTES) -> float:
    """Seconds between visits so each account is revisited as its career ends."""
    seconds = career_minutes * 60.0 / max(1, len(accounts))
    if seconds < MIN_INTERVAL_SECONDS:
        raise RotationError(
            f"{len(accounts)} accounts over {career_minutes:g} minutes is one "
            f"visit every {seconds:.0f}s, which is too tight to complete a "
            f"switch. Use fewer accounts, or raise career_minutes.")
    return seconds


# --------------------------------------------------------------------------- #
# one visit
# --------------------------------------------------------------------------- #

def _ensure_bound(settings: Settings, target: RosterAccount, log_) -> None:
    """Make this Steam slot hold ``target``, unlinking whatever is there."""
    current = 0
    try:
        current = int(flow.connect(settings).viewer_id or 0)
    except Exception as exc:  # noqa: BLE001 - an unlinked slot fails to log in
        if _version_lockout(exc):
            raise                        # the game wants a newer client: no slot switch
        log_.debug("no usable session (%s); treating the slot as free", exc)

    if current == target.trainer_id:
        log_.info("Already bound to %s.", target.label)
        return

    if current:
        # steam_chain_disconnect is the game's "Delete User Data": it PERMANENTLY
        # severs the Steam<->account link, and the only way back in is a Link
        # Password. `run` pre-registers every roster TARGET's password below, but
        # the account already holding the slot when a rotation starts is not
        # necessarily on the roster - point this profile at a live identity and
        # visit #1 would destroy it. archive_auth_config saves the now-dead
        # auth_key, not a recovery credential, so it is no protection.
        #
        # account.sh logout gates this (account_cli.py) and farm() gates it; this
        # was the one path that did not. HANDOFF §4.1: "gate it to roster accounts
        # with Link Passwords on record".
        if not acct.find_account(settings, current):
            raise RotationError(
                f"the account on this Steam slot ({acct.format_trainer_id(current)}) has no "
                f"Link Password on record, and switching away from it DELETES it.\n"
                f"  Record one first:   account.sh save-link <password>\n"
                f"  Or, if you know it: "
                f"account.sh remember \"{acct.format_trainer_id(current)}\" <password>\n"
                f"  Roster accounts are registered automatically; this one is not on the roster."
            )
        log_.info("Unlinking %s ...", acct.format_trainer_id(current))
        session = flow.connect(settings)
        acct.archive_auth_config(settings, note=f"rotation: leaving {current}")
        acct.steam_disconnect(session)

    log_.info("Logging in %s ...", target.label)
    # The chain writes this account's in-game name into accounts.json on the way
    # through (login_with_link -> remember_account), and run() relabels the whole
    # roster from that file after every visit. Taking the name from the
    # LinkResult here instead would be the same name but the WRONG place: only a
    # roster-wide pass can see that the name collides with another account's and
    # needs a trainer-id tail to stay readable. MEASURED 2026-09-03: account
    # 793593252166 joined the roster already called "McDonald" - the same name
    # 768270152387 answers to.
    acct.login_with_link(settings, target.trainer_id, target.password)


def _version_lockout(exc) -> bool:
    """A visit failed because the game wants a newer client (result code 204)."""
    return re.search(r"API error 204\b", str(exc)) is not None


def _once_finished(settings: Settings, account: RosterAccount) -> bool:
    """A one-time Ace Run account whose runs are all done (and none in flight)."""
    return bool(account.ace_once) and not ace.is_running(settings, account.trainer_id) \
        and ace.once_done(settings, account.trainer_id) >= account.ace_once


def visit(settings: Settings, spec, target: RosterAccount, *,
          trainee_card_id: int = 0, deck_id: int = 0, scenario_id: int = 0,
          race_deck_num: int = 0, running_style: int = 1, use_tp: int = 0,
          sweep: bool = True, seed: int | None = None,
          race_goal: int = 0, extra_races: list | None = None,
          recover_tp: bool = False, tp_jewels: bool = False,
          auto_deck: bool = False, build_deck_slot: int = 10) -> VisitResult:
    """Switch to ``target``, retire any finished career, start a fresh one."""
    result = VisitResult(account=target)
    _ensure_bound(settings, target, log)

    session = flow.connect(settings)

    # Daily chores (club, etc.) ride on this login, gated by their own 8h/daily
    # cadence so most visits are a no-op. Run before the career branch: circle
    # actions work regardless of career state - VERIFIED that an active career
    # does NOT block them; the writes only needed their explicit item payloads
    # (send_item_request{item_id}, donate_multiple{request_id_array,...}).
    # Best-effort; never breaks the visit.
    chores.run_due(session, settings, target, log_=log)
    viewer = int(session.viewer_id or target.trainer_id)

    state = flow.inspect_career(session, spec)
    just_aced: set[int] = set()

    if (target.ace_once and state.active and state.kind != KIND_IDLE
            and not ace.is_running(settings, viewer)):
        # A turn-by-turn career on a one-time account is the OWNER's own game in
        # progress: ending it here would force-delete it (no trainee kept). Leave
        # it alone and look again in an hour.
        log.warning("%s has a %s career in progress that the farm did not start - not "
                    "touching it; its one-time Ace Runs wait until it is finished.",
                    target.label, state.kind)
        result.ok = True
        result.source = "ace-once"
        result.remaining_seconds = 3600
        result.note = "owner's own career in progress - left alone"
        return result

    if state.active:
        remaining = state.seconds_remaining
        if remaining and remaining > 0 and not state.simulation_done:
            # The schedule slipped, or this account was started late. Leaving it
            # be is right: ending early throws the career away.
            result.ok = True
            result.finishes_at = state.finish_at
            result.started_at = state.start_at
            result.remaining_seconds = remaining
            result.source = "still-running"
            result.note = f"still running, {format_duration(remaining)} left"
            log.info("Career still has %s to run - leaving it alone.",
                     format_duration(remaining))
            return result

        log.info("Ending the finished career...")
        was_ace = ace.is_running(settings, viewer)
        end = flow.end_running_career(session, state, spec, settings=settings,
                                      ace=was_ace)
        result.ended = end.ended
        summary = end.summary
        if summary.get("fans"):
            log.info("Banked %s fans (rank %s).", f"{summary['fans']:,}",
                     summary.get("rank"))
        if was_ace and end.ended:
            ace.record_finish(settings, viewer, summary)
            if target.ace_once and not end.forced:     # a force-deleted run kept no trainee
                log.info("One-time Ace Runs for %s: %s/%s done.", target.label,
                         ace.record_once_finish(settings, viewer), target.ace_once)
            # Field the new ace now if it beats a team member, instead of waiting
            # for the hourly team chore (which ran before this career ended).
            chores.team_after_ace(session, log, target.label)
            # Not on the team until the next visit's team chore: tell the
            # trainee pick about it directly, or it could pick the same uma.
            just_aced.add(int(summary.get("card_id") or 0) // 100)
    elif ace.is_running(settings, viewer):
        # The Ace Run is gone without us ending it (finished or deleted in
        # the client). Clear the flag, or the next FAN career would be ended with
        # skills bought and counted as an Ace Run.
        ace.clear_running(settings, viewer)

    # A trainee written on the roster entry wins over the global --trainee: the
    # whole point of putting it there is that each account trains its own uma.
    wanted = trainee_card_id
    if target.trainee:
        from .gamedata import load as _load_gamedata
        account_now = read_account(session.load_index or session.refresh_home())
        resolved, note = sel.resolve_trainee_label(
            target.trainee, account_now.owned_card_ids,
            _load_gamedata(str(settings.icarus_root)))
        if resolved:
            if note:
                log.info("%s", note)
            wanted = resolved
        else:
            # Never guess. A wrong trainee spends a whole 50-minute career on the
            # wrong build, and the operator would have no idea why.
            log.warning("roster asks for trainee %r on %s but %s; letting the "
                        "selection choose instead.", target.trainee, target.label,
                        note or "it could not be resolved")

    # Ace Run (aic.ace): 5 a week from Monday's reset - Grand Live, no race schedule,
    # borrowed Light Hello, skills bought at the finish, and a trainee who is NOT
    # on the Team Trials team yet (the roster's trainee is for fan careers).
    want_ace = ace.wanted(settings, viewer, once=target.ace_once)
    if want_ace:
        available = read_account(session.load_index or session.refresh_home()
                                 ).available_scenario_ids or []
        if ace.SCENARIO not in available:
            log.warning("%s cannot do an Ace Run: scenario %s is not unlocked "
                        "(available %s). %s", target.label, ace.SCENARIO, available,
                        "Nothing else to do on it." if target.ace_once else "Fan farming instead.")
            want_ace = False
    if target.ace_once and not want_ace:
        # A one-time Ace Run account never fan farms: done, or blocked today
        # (failed starts / scenario locked) and tried again after the reset.
        done = ace.once_done(settings, viewer)
        result.ok = True
        result.source = "ace-once"
        result.remaining_seconds = fancap.seconds_to_reset()
        if done >= target.ace_once:
            result.note = f"one-time Ace Runs done ({done}/{target.ace_once})"
            log.info("%s has done its %s one-time Ace Runs - leaving the rotation.",
                     target.label, target.ace_once)
        else:
            result.note = f"one-time Ace Runs {done}/{target.ace_once}, blocked until the reset"
            log.info("%s: no Ace Run possible right now (%s/%s done) - trying again "
                     "after the reset.", target.label, done, target.ace_once)
        return result
    if want_ace:
        q = ace.status(settings, viewer)
        if target.ace_once:
            log.info("One-time Ace Run %s/%s for %s.", ace.once_done(settings, viewer) + 1,
                     target.ace_once, target.label)
        else:
            log.info("Ace Run %s/%s %s for %s (%s ace(s) so far).",
                     q["started"] + 1, ace.RUNS, ace.PER, target.label, q["good"])
        scenario_id, extra_races, race_goal = ace.SCENARIO, None, 0
        try:
            idx = session.load_index or session.refresh_home()
            card, why = ace.pick_trainee(settings, viewer, idx, exclude=just_aced)
            if not card:
                # every off-team uma already had its Ace Run this week: run one
                # of them again rather than an uma that is on the team
                card, why = ace.pick_trainee(settings, viewer, idx, exclude=just_aced,
                                             reuse_week=True)
        except Exception as exc:  # noqa: BLE001 - the roster trainee still works
            card, why = 0, ""
            log.warning("Could not choose an Ace Run trainee (%s); using the roster's.", exc)
        if card:
            log.info("Ace Run trainee: card %s - %s.", card, why)
            wanted = card
        else:
            log.info("No owned trainee is off the Team Trials team; using the roster's.")

    try:
        selection, account_state = flow.build_selection(
            session, settings, trainee_card_id=wanted, deck_id=deck_id,
            running_style=running_style, seed=seed,
            auto_deck=auto_deck, build_deck_slot=build_deck_slot,
            want_friend_card=ace.FRIEND_CARD if want_ace else 0,
            friend_reloads=ace.RELOADS if want_ace else 0)
        scenario = flow.pick_scenario(account_state, scenario_id,
                                      known=independent.load_known_scenario(settings))
    except Exception:
        if want_ace:
            ace.record_failed_start(settings, viewer)
        raise

    try:
        _, started, _ = flow.start_independent(
            session, spec, selection, account_state, scenario,
            race_deck_num=race_deck_num, use_tp=use_tp, settings=settings,
            sweep=sweep, race_goal=race_goal, extra_races=extra_races,
            recover_tp=recover_tp, tp_jewels=tp_jewels, goals_only=want_ace)
    except (fancap.FanCapReached, flow.StartDeferred) as capped:
        # Had its day. That is a success, not a failure: the career that was
        # running has been ended and banked above, and this account is simply
        # done until the 15:00 UTC reset. A multi-account rotation moves on to
        # the next slot; the single-account loop sleeps to the reset because
        # remaining_seconds is what it schedules the next visit from.
        log.info("%s", capped)
        result.ok = True
        result.remaining_seconds = capped.seconds
        if want_ace and isinstance(capped, flow.StartDeferred):
            # 2509 depends on the configuration, so on an Ace Run start it may be
            # Grand Live itself being refused: count it, and come back soon to
            # try (and, after MAX_FAILED_STARTS, fan farm) instead of in an hour.
            ace.record_failed_start(settings, viewer)
            result.remaining_seconds = min(capped.seconds, RETRY_SECONDS)
        result.source = ("fan-cap" if isinstance(capped, fancap.FanCapReached)
                         else "deferred")
        result.note = str(capped)
        return result
    except Exception:
        if want_ace:
            ace.record_failed_start(settings, viewer)
        raise

    result.ok = True
    result.started = True
    result.source = "started"
    if want_ace:
        ace.record_start(settings, viewer, chara=selection.card_id // 100)
    if started is not None:
        result.finishes_at = started.finish_at
        result.started_at = started.start_at
        result.remaining_seconds = started.seconds_remaining
        if started.seconds_remaining:
            log.info("New career finishes in %s.",
                     format_duration(started.seconds_remaining))
    return result


# --------------------------------------------------------------------------- #
# the rotation
# --------------------------------------------------------------------------- #

def describe_plan(accounts, interval: float, career_minutes: float,
                  settings: Settings | None = None) -> list[str]:
    """The opening schedule, showing each account's recorded career clock.

    ``interval`` is only the stagger for accounts nothing is known about; an
    account with a career on record is due when *that* career says so.
    """
    lines = [
        f"{len(accounts)} accounts, {career_minutes:g}-minute careers",
        f"opening stagger {format_duration(interval)} apart; after that each "
        f"account is due when its own career ends",
        "",
    ]
    store = careerclock.read_all(settings) if settings is not None else {}
    now = time.time()
    for i, a in enumerate(accounts):
        recorded = (careerclock.due_at(settings, a.trainer_id, store)
                    if settings is not None else None)
        due = recorded if recorded is not None else now + i * interval
        when = time.strftime("%H:%M:%S", time.localtime(due))
        if recorded is None:
            lines.append(f"  {i + 1:>3}. {a.label:<28} first visit {when}"
                         f"   (no career on record)")
        else:
            overdue = " OVERDUE" if due <= now else ""
            lines.append(f"  {i + 1:>3}. {a.label:<28} due {when}{overdue}"
                         f"   ({careerclock.describe(settings, a.trainer_id, store)})")
    return lines


def run(settings: Settings, spec, accounts: list[RosterAccount], *,
        career_minutes: float = CAREER_MINUTES, cycles: int = 0,
        dry_run: bool = False, start_at: int = 0,
        **visit_kwargs) -> RotationState:
    """Farm the roster forever (or until every account has had ``cycles`` visits).

    **Whichever account is due soonest goes next.** A due time is that account's
    own career clock, read off the server on the last visit and written down
    (:mod:`aic.careerclock`), so:

    * arriving early no longer costs a lap - the account is simply re-due when
      its career actually ends, not one full rotation later;
    * ``career_minutes`` being wrong costs nothing after the first visit, since
      every later one is scheduled from a measured clock rather than the guess;
    * a restart resumes the real schedule instead of re-staggering from now.

    ``start_at`` is a 1-based roster position to open the stagger at, for
    resuming a run interrupted before anything was recorded. It only affects
    accounts with no career on record; a recorded one is due when its own career
    says so, whatever position it sits at.
    """
    interval = interval_for(accounts, career_minutes)
    state = RotationState()

    for line in describe_plan(accounts, MIN_INTERVAL_SECONDS, career_minutes,
                              settings):
        log.info("%s", line)

    offset = 0
    if start_at:
        if not 1 <= start_at <= len(accounts):
            raise RotationError(
                f"--start-at must be between 1 and {len(accounts)} "
                f"(got {start_at})")
        offset = start_at - 1
        log.info("Opening the stagger at %s (position %s); positions 1-%s are "
                 "assumed to have careers already running.",
                 accounts[offset].label, start_at, offset)

    if dry_run:
        log.info("[dry run] nothing was changed.")
        return state

    # Record every roster password so `logout` never blocks mid-rotation. NO
    # name: a.name is a roster nickname ("Oguri_2166"), and accounts.json's name
    # is the in-game one. Writing the nickname there would make a never-chained
    # account indistinguishable from a named one, so apply_ingame_names could no
    # longer fall back - and the first chain fills the field in properly anyway.
    for a in accounts:
        if not acct.find_account(settings, a.trainer_id):
            acct.remember_account(settings, a.trainer_id, a.password, True)

    # Seed one due time per account: the clock recorded last run when there is a
    # usable one, else the opening lap. careerclock.due_at discards anything over
    # a day old, so a long outage re-opens cleanly instead of stampeding.
    #
    # THE OPENING LAP GOES ROUND AS FAST AS IT IS ALLOWED TO: every account with
    # no career on record is due one MIN_INTERVAL_SECONDS after the last, so the
    # rotation visits them back to back, starts a career on each, and only then
    # comes back to claim the first. It used to spread them `interval` apart
    # (career_minutes / N, i.e. 12.5 min for four), which takes ~37 minutes to
    # touch a four-account roster before anything is farming.
    #
    # Throughput is identical either way - each account still runs one career per
    # career-length - but this way all of them are running within a few minutes
    # of a cold start, and they finish together so one pass claims them all.
    # MIN_INTERVAL_SECONDS still paces the burst, because each visit is a full
    # Steam-slot switch and four back-to-back with no gap is the hammer that
    # floor exists to prevent.
    store = careerclock.read_all(settings)
    now = time.time()
    due: dict[int, float] = {}
    for pos, a in enumerate(accounts):
        recorded = careerclock.due_at(settings, a.trainer_id, store)
        if _once_finished(settings, a):
            due[a.trainer_id] = float("inf")
            log.info("%s has done its one-time Ace Runs; not visiting it.", a.label)
        elif recorded is None:
            due[a.trainer_id] = now + ((pos - offset) % len(accounts)) * MIN_INTERVAL_SECONDS
        else:
            due[a.trainer_id] = recorded
            log.info("%s resumes its recorded clock: due in %s (%s).", a.label,
                     format_duration(max(0.0, recorded - now)),
                     careerclock.describe(settings, a.trainer_id, store))

    visits_made = {a.trainer_id: 0 for a in accounts}
    version_switched = False
    #: accounts whose last visit failed on a 204 - released at once on a switch
    locked_out: set[int] = set()
    #: Consecutive failures per account, for the backoff below.
    failed_runs = {a.trainer_id: 0 for a in accounts}
    #: Nothing is visited sooner than this. A burst of overdue accounts (every
    #: career ended during an outage) would otherwise be drained back to back -
    #: N account switches in seconds, which is exactly what interval_for refuses
    #: to schedule. At steady state due times are already further apart than this,
    #: so it never binds.
    next_visit_allowed = 0.0

    while True:
        # Bounded the way the old index walk was: cycles * roster passes. NOT
        # min(visits_made), which a fan-capped account (due +24h) would stretch to
        # a day - `rotate run --cycles 2` has to stay a bounded errand.
        if cycles and state.visits >= cycles * len(accounts):
            log.info("Completed %s full rotation(s).", cycles)
            return state

        # Soonest due wins; roster order breaks ties, so an untouched schedule
        # still runs 1, 2, 3, ... exactly as the fixed slots used to.
        pos, target = min(enumerate(accounts),
                          key=lambda it: (due[it[1].trainer_id], it[0]))
        if due[target.trainer_id] == float("inf"):
            log.info("Every roster account is retired; nothing left to visit.")
            return state

        delay = max(due[target.trainer_id], next_visit_allowed) - time.time()
        if delay > 0:
            log.info("")
            log.info("Next visit in %s: %s.", format_duration(delay), target.label)
            time.sleep(delay)
        elif delay < -interval:
            log.warning("%s is %s overdue; visits are taking longer than the "
                        "%s stagger.", target.label, format_duration(-delay),
                        format_duration(interval))

        next_visit_allowed = time.time() + MIN_INTERVAL_SECONDS
        state.visits += 1
        visits_made[target.trainer_id] += 1
        log.info("")
        log.info("=== visit %s | %s (its visit %s) ===", state.visits,
                 target.label, visits_made[target.trainer_id])

        try:
            result = visit(settings, spec, target, **visit_kwargs)
        except Exception as exc:  # noqa: BLE001 - one bad account must not stop the rest
            state.failures += 1
            log.error("Visit to %s failed: %s", target.label, exc)
            # A 391 is the held Steam ticket being cancelled. Without dropping
            # the session here the next visit sends the same dead ticket, and so
            # does every visit after it - forever.
            from .auth import note_dead_ticket, refresh_app_version
            note_dead_ticket(exc)
            # 204 = the game shipped a client update and refuses our app_ver:
            # read the new version off Steam and switch to it (rate-limited).
            if _version_lockout(exc):
                locked_out.add(target.trainer_id)
                try:
                    version_switched = bool(refresh_app_version(settings))
                except Exception as vexc:  # noqa: BLE001 - must never end the run
                    log.warning("game version check failed (%s)", vexc)
            log.error("Continuing with the next account.")
            result = VisitResult(account=target, ok=False, source="failed",
                                 note=str(exc)[:200])
        else:
            locked_out.discard(target.trainer_id)
            if result.started:
                state.started_careers += 1
        failed_runs[target.trainer_id] = (
            0 if result.ok else failed_runs[target.trainer_id] + 1)

        # A successful chain has just rewritten accounts.json with this account's
        # in-game name, so re-read it here: an account visited for the first time
        # stops showing its roster nickname, an in-game rename lands on the next
        # visit rather than the next restart, and - the part only a roster-wide
        # pass can do - a name that collides with another account's gets its
        # trainer-id tail. One small JSON read per visit, minutes apart.
        try:
            apply_ingame_names(accounts, settings)
        except Exception as exc:  # noqa: BLE001 - a label must never end a run
            log.debug("could not refresh the in-game labels (%s)",
                      type(exc).__name__)

        state.results.append(result)

        # ONE place where a visit turns into the next due time, and the same
        # place it is written down - so what a restart reads back is exactly
        # what this process is scheduling on.
        try:
            recorded = careerclock.record(
                settings, target.trainer_id,
                remaining_seconds=result.remaining_seconds,
                started_at=result.started_at, finishes_at=result.finishes_at,
                source=result.source or ("ok" if result.ok else "failed"),
                label=target.label)
        except Exception as exc:  # noqa: BLE001 - bookkeeping must never end the run
            log.warning("could not record %s's career clock (%s); scheduling it "
                        "from the visit instead.", target.label, exc)
            recorded = (time.time() + result.remaining_seconds + careerclock.FINISH_GRACE
                        if result.remaining_seconds is not None else None)

        # BRANCH ON result.ok FIRST. careerclock.record() deliberately PRESERVES
        # the previous due_at when a visit fails (so a restart does not lose a
        # live career's schedule) - which means it returns a non-None, and often
        # long-stale, value on failure. Testing `recorded is not None` before
        # `result.ok` therefore sent every failure down the due-time branch and
        # the backoff below never ran.
        #
        # MEASURED 2026-09-02: three accounts held due times ~3.5 h in the past,
        # so each failure re-scheduled to now+MIN_INTERVAL - a visit every 60s,
        # 234 visits and 432 Steam 391s overnight, for zero careers. The fourth
        # account had no stored due_at, fell through to the backoff, and correctly
        # sat at "failure 10 in a row, retrying in 1h". That contrast is the bug.
        if not result.ok:
            # Back off on REPEATED failure. A transient one still retries in
            # RETRY_SECONDS; a broken account doubles out to MAX_RETRY_SECONDS
            # rather than burning a Steam-slot switch every minute forever.
            streak = failed_runs[target.trainer_id]
            wait = min(RETRY_SECONDS * (2 ** min(streak - 1, 20)), MAX_RETRY_SECONDS)
            due[target.trainer_id] = time.time() + wait
            log.info("Retrying %s in %s (failure %s in a row).", target.label,
                     format_duration(wait), streak)
            if target.ace_once and streak >= ONCE_MAX_FAILURES:
                # A one-time account that keeps failing (a wrong link password)
                # is not worth an account switch every hour forever: park it.
                due[target.trainer_id] = float("inf")
                log.warning("%s failed %s visits in a row - parking its one-time Ace "
                            "Runs until the next restart.", target.label, streak)
        elif recorded is not None:
            # Never re-visit one account sooner than the minimum switch gap. A
            # server answering "0 seconds left" would otherwise put it due in
            # FINISH_GRACE, and the rotation would hammer a full account switch
            # every 30 seconds against the live API.
            wait = _visit_jitter()
            due[target.trainer_id] = max(recorded, time.time() + MIN_INTERVAL_SECONDS) + wait
            log.info("%s next due in %s (incl. %s random wait).", target.label,
                     format_duration(max(0.0, due[target.trainer_id] - time.time())),
                     format_duration(wait))
        else:
            # Something happened but no clock came back. Assume a full career
            # rather than hammering; the next visit measures it properly.
            # Succeeded but returned no clock: assume a full career rather than
            # hammering. The next visit measures it properly.
            due[target.trainer_id] = (time.time() + career_minutes * 60 + careerclock.FINISH_GRACE
                                      + _visit_jitter())
        if _once_finished(settings, target):
            due[target.trainer_id] = float("inf")     # its one-time Ace Runs are done
        if version_switched:
            # Every account the old version locked out goes again now, instead of
            # sitting out its failure backoff (up to MAX_RETRY_SECONDS).
            version_switched = False
            for a in accounts:
                if a.trainer_id in locked_out and not _once_finished(settings, a):
                    failed_runs[a.trainer_id] = 0
                    due[a.trainer_id] = time.time()
            locked_out.clear()
            log.info("Retrying every locked-out account now, on the new client version.")
