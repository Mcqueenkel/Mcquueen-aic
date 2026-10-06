"""The end-to-end cycle: boot, wait out a running career, end it, start a new
Independent one.

Each step is a separate function so the CLI can run them individually
(``aic end``, ``aic start``) or as one pass (``aic run``).
"""

from __future__ import annotations

import os
import random
import time
from dataclasses import dataclass, field

from . import (career, fancap, independent, notify, racedata, veterans,
               account as account_mod, selection as sel, succession)
from .auth import Session, boot
from .config import ITEM_CLOCK, Settings
from .gamedata import load as load_gamedata
from .succession import load as load_relations
from .independent import IdleModeError, Schedule, Spec
from .logging_setup import get
from .state import (
    KIND_IDLE,
    AccountState,
    CareerState,
    enrich_from_chara_info,
    format_duration,
    read_account,
    read_career,
)

log = get("flow")

#: Ceiling on how long to sleep between polls while waiting out an idle run.
#: Captured runs last 50 minutes, so a few checks per run is plenty.
MAX_POLL_INTERVAL = 300
#: Floor, so a nearly-finished run is not polled in a tight loop.
MIN_POLL_INTERVAL = 20
#: Slack added after the clock hits zero before calling end, to absorb any
#: residual client/server clock skew.
FINISH_GRACE = 20


class StartDeferred(IdleModeError):
    """A start that cannot happen YET. Not a failure - a schedule signal.

    The farmer is a long-running service, so a resource it does not have right
    now must put it to sleep, not kill it: `Restart=on-failure` plus
    StartLimitBurst=3 turns three refusals into a dead unit inside five minutes,
    and it stays dead until somebody notices. Sleeping means the farm resumes on
    its own the moment the resource appears.

    ``seconds`` is how long to wait. Deliberately the same attribute name
    :class:`aic.fancap.FanCapReached` carries, so the loop drivers handle both
    causes with one branch.
    """

    def __init__(self, message: str, seconds: float):
        super().__init__(message)
        self.seconds = seconds


#: How long to wait before re-checking a resource the account is simply out of.
#: Bounded by the game-day reset, since that is when daily allowances refresh,
#: but re-checked hourly in case it arrives some other way (a mission, a gift).
DEFER_RETRY_SECONDS = 3600.0


@dataclass
class CycleResult:
    waited_seconds: float = 0.0
    ended: bool = False
    started: bool = False
    selection: sel.StartSelection | None = None
    schedule: Schedule | None = None
    career_before: CareerState | None = None
    career_after: CareerState | None = None
    notes: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# step 1 - authenticate and reach the main screen
# --------------------------------------------------------------------------- #

def connect(settings: Settings, refresh_ticket: bool = True,
            steam_code: str = "") -> Session:
    session = boot(settings, refresh_ticket=refresh_ticket, steam_code=steam_code)
    account = read_account(session.load_index)
    log.info(
        "Account: viewer %s | %s gold | TP %s/%s | %s clocks | %s succession pts | "
        "%s trainees | %s support cards | %s decks",
        account.viewer_id or session.viewer_id,
        f"{account.gold:,}",
        account.current_tp,
        account.tp_info.get("max_tp", "?"),
        # Item 95. An idle start appears to spend one, and it is the difference
        # between a farmable account and a 2509 - so it belongs on the one line
        # that gets logged for every account on every visit.
        account.clocks,
        account.succession_rank_point,
        len(account.owned_card_ids),
        len(account.owned_support_card_ids),
        len(account.support_card_decks),
    )
    if not account.idle_mode_available:
        log.warning("This account shows no Independent Career unlock date "
                    "(common_define.idle_single_mode_open_date is absent).")
    log.debug("server clock offset %+ds (derived=%s)",
              session.clock.offset, session.clock.derived)
    return session


# --------------------------------------------------------------------------- #
# step 2 - inspect / wait out a running career
# --------------------------------------------------------------------------- #

def inspect_career(session: Session, spec: Spec) -> CareerState:
    """Current career state, enriched with whatever detail endpoint applies."""
    state = read_career(session.load_index, session.clock)
    if not state.active:
        return state

    if state.kind == KIND_IDLE:
        # Proof this scenario is startable in idle mode - worth remembering,
        # since a refusal only ever reports 205.
        independent.save_known_scenario(session.settings, state.scenario_id)
        # Nothing more to ask for. check_progress_log is only valid once the run
        # has been stopped with idle_single_mode/end - calling it on a live run
        # answers 205 and burns the client's retry budget. The turn reported
        # here stays 1 for the whole run; the real one is read at end time by
        # career._resolve_final_turn.
        return state

    detail = career.load_career(session, state.scenario_id)
    if detail:
        enrich_from_chara_info(state, detail.get("chara_info") or {})
    return state


def wait_for_finish(session: Session, spec: Spec, state: CareerState,
                    timeout: float | None = None) -> float:
    """Block until the running idle career's clock runs out.

    Re-reads state from the server each poll rather than trusting the first
    timestamp: the run can be ended elsewhere, or its clock extended, while we
    wait. Returns seconds actually waited.
    """
    started = time.monotonic()

    if state.simulation_done:
        log.info("The idle simulation has already finished; nothing to wait for.")
        return 0.0

    if state.seconds_remaining is None:
        log.info("Career is running but has no completion clock (kind=%s) - "
                 "nothing to wait for.", state.kind)
        return 0.0

    log.info("Idle career finishes in %s (total run %s, from %s).",
             format_duration(state.seconds_remaining),
             format_duration(state.duration), state.finish_source)

    while True:
        remaining = state.seconds_remaining or 0.0
        if remaining <= 0:
            break

        elapsed = time.monotonic() - started
        if timeout is not None and elapsed >= timeout:
            log.warning("Wait timed out after %s with %s left.",
                        format_duration(elapsed), format_duration(remaining))
            return elapsed

        sleep_for = min(MAX_POLL_INTERVAL, max(MIN_POLL_INTERVAL, remaining + FINISH_GRACE))
        if timeout is not None:
            sleep_for = min(sleep_for, timeout - elapsed)
        if sleep_for <= 0:
            break

        log.info("  %s remaining; next check in %s.",
                 format_duration(remaining), format_duration(sleep_for))
        time.sleep(sleep_for)

        session.refresh_home()
        refreshed = inspect_career(session, spec)
        if not refreshed.active:
            log.info("Career is no longer running.")
            return time.monotonic() - started
        state = refreshed

    waited = time.monotonic() - started
    log.info("Idle clock has run out (waited %s).", format_duration(waited))
    return waited


# --------------------------------------------------------------------------- #
# step 3 - end the career
# --------------------------------------------------------------------------- #

def end_running_career(session: Session, state: CareerState, spec: Spec,
                       allow_force: bool = True,
                       settings: Settings | None = None,
                       ace: bool = False) -> career.EndResult:
    """``ace``: the career is an Ace Run (aic.ace) - buy its skills at the
    finish and say so on the Discord card."""
    result = career.end_career(session, state, spec, allow_force=allow_force,
                               buy_skills=ace)
    # Best-effort: the career IS ended on the server now. A refresh that fails
    # here must not fail the visit before the finish is counted (an uncounted
    # Ace Run is run again); later reads do `load_index or refresh_home()`.
    try:
        session.refresh_home()
    except Exception as exc:  # noqa: BLE001
        log.warning("could not refresh home after ending the career (%s)", exc)
        session.load_index = None
    summary = result.summary
    if summary.get("card_id"):
        log.info("Retired: card %s, rank %s, score %s, %s fans",
                 summary.get("card_id"), summary.get("rank"),
                 summary.get("rank_score"), summary.get("fans"))

    # THE one place both loops finish a career (flow.run_cycle and
    # rotate.visit), so the announcement belongs here rather than in each.
    # Everything below is best-effort: the career is already banked, so a
    # reporting failure must not surface as a career failure.
    try:
        account = read_account(session.load_index or {})
        trainee = ""
        if settings is not None and summary.get("card_id"):
            try:
                trainee = load_gamedata(str(settings.icarus_root)).chara_name(
                    int(summary["card_id"]))
            except Exception:  # noqa: BLE001 - a name is decoration, not data
                trainee = ""
        fans_today = None
        roster_fans = roster_accounts = None
        breakdown: list[tuple[str, int, int]] = []
        names: dict[int, str] = {}
        if settings is not None:
            try:
                names = account_mod.names_by_viewer(settings)
            except Exception:  # noqa: BLE001 - a name is decoration, not data
                names = {}
        if settings is not None and account.viewer_id and account.fans:
            # baseline() records this account's newest reading first, so the
            # roster total below already includes the career that just ended.
            base = fancap.baseline(
                settings, account.viewer_id, account.fans,
                just_banked=int(summary.get("fans") or 0) if result.ended else 0)
            fans_today = max(0, int(account.fans) - int(base))
            rows = fancap.day_breakdown(settings)
            roster_fans = sum(gained for _, gained in rows)
            roster_accounts = len(rows)
            # One traversal feeds both the total and the per-account table, so
            # they can never disagree about the day.
            breakdown = [(names.get(viewer, ""), viewer, gained)
                         for viewer, gained in rows]
        notify.career_finished(
            summary,
            account=(names.get(account.viewer_id)
                     or (f"Trainer {account.viewer_id}" if account.viewer_id else "")),
            trainee=trainee,
            fans_today=fans_today,
            roster_fans=roster_fans,
            roster_accounts=roster_accounts or 0,
            breakdown=breakdown,
            viewer_id=account.viewer_id or 0,
            scenario=state.scenario_id,
            forced=result.forced,
            ace=ace,
        )
    except Exception as exc:  # noqa: BLE001 - reporting must never end a run
        log.debug("career notification skipped (%s)", type(exc).__name__)
    return result


# --------------------------------------------------------------------------- #
# steps 4-5 - build the selection
# --------------------------------------------------------------------------- #

def save_deck(session: Session, slot: int, support_ids: list[int]) -> bool:
    """Write ``support_ids`` into deck ``slot`` server-side. Never fatal.

    Why this exists: it is not settled whether an idle career takes its cards
    from the start body or from the saved slot. A capture proves the body wins
    for the turn-by-turn endpoint (a card in NO saved deck came back in the
    career), but the one idle capture sends body and slot identical, so it
    discriminates nothing. Saving first makes both readings agree.

    The other nine slots are echoed back with their REAL contents. The server
    writes exactly the slots whose contents differ, so a zero there would delete
    a saved deck - which is why this never sends zeros for a populated slot.
    """
    slot = min(max(int(slot), 1), 10)
    index = session.load_index or {}
    current: dict[int, list[int]] = {}
    for deck in index.get("support_card_deck_array") or []:
        try:
            current[int(deck.get("deck_id"))] = [
                int(c or 0) for c in (deck.get("support_card_id_array") or [])]
        except (TypeError, ValueError):
            continue

    was = current.get(slot) or []
    if [i for i in was if i] == [int(i) for i in support_ids]:
        log.debug("deck slot %s already holds this deck; not rewriting.", slot)
        return True

    array = []
    for n in range(1, 11):
        ids = list(support_ids) if n == slot else (current.get(n) or [])
        array.append({"deck_id": n,
                      "support_card_id_array": ([int(i) for i in ids]
                                                + [0, 0, 0, 0, 0])[:5],
                      "auto_deck_flag": 0})
    try:
        session.client.change_support_card_deck(array)
    except Exception as exc:  # noqa: BLE001 - the start still sends the ids
        log.warning("could not save the fan deck to slot %s (%s); starting on "
                    "the cards in the request body alone.", slot, exc)
        return False

    # Confirm from the server rather than the echo, which is only a diff.
    after = session.refresh_home() or {}
    for deck in after.get("support_card_deck_array") or []:
        if int(deck.get("deck_id") or 0) != slot:
            continue
        got = [int(c or 0) for c in (deck.get("support_card_id_array") or []) if c]
        ok = got == [int(i) for i in support_ids]
        log.info("Saved the fan deck to slot %s%s.", slot,
                 "" if ok else f" - but it reads back as {got}")
        return ok
    log.warning("deck slot %s did not read back after the save.", slot)
    return False


def borrow_card(session: Session, want: int, reloads: int, support_ids, trainee: int,
                gamedata, rng, fan_scorer=None) -> tuple[int, int]:
    """Borrow support card ``want``, refreshing the offer list until it shows up.

    The list is pre_single_mode/index; each refresh is
    pre_single_mode/friend_support_card_reload with every viewer already seen in
    ``exclude_viewer_id_array`` (the real client's body, dumps
    20260713_192951/0028-0029), which deals ~10 new offers. Offers from EARLIER
    lists stay borrowable - that capture refreshed twice and then started on a
    card from the first list (accepted) - so every list seen is searched, and
    after ``reloads`` refreshes without the card the best other one of all of
    them is taken rather than not starting.
    """
    offers: dict[int, dict] = {}            # viewer_id -> offer, every list seen
    pool = {"friend_support_card_data": {"support_card_data_array": []}}
    pre = session.refresh_pre_single_mode()
    for attempt in range(max(int(reloads), 0) + 1):
        for o in ((pre.get("friend_support_card_data") or {})
                  .get("support_card_data_array") or []):
            offers.setdefault(int(o.get("viewer_id") or 0), o)
        pool["friend_support_card_data"]["support_card_data_array"] = list(offers.values())
        viewer, card = sel.pick_friend_card(pool, support_ids, trainee, gamedata, rng,
                                            only_card_id=want)
        if card:
            if attempt:
                log.info("Found %s after %s refresh(es).", gamedata.support_name(want), attempt)
            return viewer, card
        if attempt == reloads or any(int(o.get("support_card_id") or 0) == int(want)
                                     for o in offers.values()):
            break       # out of refreshes, or it IS offered but clashes with the deck
        try:
            res = session.client.call("pre_single_mode/friend_support_card_reload",
                                      {"exclude_viewer_id_array": sorted(v for v in offers if v)})
        except Exception as exc:  # noqa: BLE001 - fall back to the lists we have
            log.warning("Refreshing the borrowable cards failed (%s).", exc)
            break
        data = (res or {}).get("data") or {}
        pre = {"friend_support_card_data": {
            "support_card_data_array": data.get("support_card_data_array") or []}}
    log.warning("Could not borrow %s (%s offer(s) seen); borrowing the best other card "
                "instead.", gamedata.support_name(want), len(offers))
    return sel.pick_friend_card(pool, support_ids, trainee, gamedata, rng,
                                fan_scorer=fan_scorer)


def build_selection(session: Session, settings: Settings, *,
                    trainee_card_id: int = 0, deck_id: int = 0,
                    running_style: int = 1, forced_parents: str = "",
                    succession_point: int = 0, no_friend_card: bool = False,
                    auto_deck: bool = False, build_deck_slot: int = 10,
                    seed: int | None = None, want_friend_card: int = 0,
                    friend_reloads: int = 0) -> tuple[sel.StartSelection, AccountState]:
    gamedata = load_gamedata(str(settings.icarus_root))
    rng = random.Random(seed)

    load_index = session.load_index or session.refresh_home()
    account = read_account(load_index)
    parents = sel.read_parents(load_index)
    log.info("Found %s owned trained characters usable as parents.", len(parents))

    trainee = sel.pick_trainee(account, parents, gamedata, rng,
                               preferred_card_id=trainee_card_id)
    log.info("Trainee: %s (card %s)", gamedata.chara_name(trainee), trainee)

    if forced_parents:
        p1, p2 = sel.resolve_parents(parents, forced_parents)
        log.info("Using the parent pair given on the command line.")
    else:
        # Prefer a pair whose succession offset we already learned: every
        # retirement adds a stronger character, so picking purely by rank would
        # choose a new pair each cycle and re-run the rank-point search every
        # time.
        p1, p2 = sel.pick_parents(
            parents, trainee, gamedata,
            preferred_pairs=succession.known_pairs(settings, account.viewer_id))
    for label, parent in (("Parent 1", p1), ("Parent 2", p2)):
        lineage = [i for i in parent.parent_ids if i]
        log.info("%s: %s (id %s, rank %s%s)", label,
                 gamedata.chara_name(parent.card_id), parent.trained_chara_id,
                 parent.rank,
                 f", own parents {lineage}" if lineage else ", no lineage")

    fan_scorer = None
    if auto_deck:
        from . import fanbonus, supportdata
        support = supportdata.load(settings)
        support_ids, chosen = fanbonus.build_fan_deck(
            account, gamedata, support, trainee,
            reserve=[want_friend_card] if want_friend_card else ())
        chosen_deck_id = build_deck_slot
        source = f"auto fan-bonus deck -> slot {build_deck_slot}"
        fan_scorer = lambda sid, lb: fanbonus.friend_fan_score(
            gamedata, support, sid, lb)
        log.info("Auto fan-bonus deck (%.1f%% fan bonus): %s",
                 sum(c.fan_pct for c in chosen),
                 ", ".join(f"{c.name} {c.fan_pct}%" for c in chosen))
        save_deck(session, build_deck_slot, support_ids)
    else:
        chosen_deck_id, support_ids, source = sel.choose_deck(
            account, trainee, gamedata, rng, deck_id=deck_id, settings=settings
        )
    log.info("Deck %s (%s): %s", chosen_deck_id, source,
             sel.describe_deck(support_ids, gamedata))

    # A career runs six support cards; the sixth is borrowed. Refresh the offer
    # list first - it is per-session and goes stale.
    if no_friend_card:
        friend_viewer, friend_card = 0, 0
        log.info("Starting without a borrowed 6th support card (--no-friend-card).")
    elif want_friend_card:
        friend_viewer, friend_card = borrow_card(
            session, want_friend_card, friend_reloads, support_ids, trainee,
            gamedata, rng, fan_scorer)
    else:
        friend_viewer, friend_card = sel.pick_friend_card(
            session.refresh_pre_single_mode(), support_ids, trainee, gamedata, rng,
            fan_scorer=fan_scorer,
        )

    # Affinity from the trainee and the parents' family tree. NOT the account's
    # item-75 balance: the server validates this and rejects a wrong value.
    relations = load_relations(str(settings.icarus_root))
    node_1, node_2 = p1.as_family_node(), p2.as_family_node()
    # family_points is the real 7-link formula (trainee vs its two direct
    # parents, plus the 5-link parent tree) - what Icarus' own client computes.
    # parent_only (the 5-link tree alone) is kept as a cheap secondary
    # candidate, not the primary value.
    points = succession.family_points(trainee, node_1, node_2, relations)
    parent_only = succession.compute(node_1, node_2, relations)
    detail = succession.explain(node_1, node_2, relations, trainee)

    if succession_point:
        log.info("Succession rank point: %s (given on the command line; "
                 "computed value was %s)", succession_point, points)
        points = succession_point
        parent_only = 0
    else:
        log.info("Succession rank point: %s (trainee + parent tree; %s "
                 "parent-only; parent ranks %s/%s)",
                 points, parent_only, p1.rank, p2.rank)
        missing = [line for line in detail if "MISSING" in line]
        if missing:
            log.warning("Family tree is incomplete - %s node(s) absent, so the "
                        "computed value is short by their links: %s",
                        len(missing), ", ".join(m.split()[0] for m in missing))

    gamedata.warn_about_unknown()

    selection = sel.StartSelection(
        card_id=trainee,
        support_card_ids=support_ids,
        parent_id_1=p1.trained_chara_id,
        parent_id_2=p2.trained_chara_id,
        deck_id=chosen_deck_id,
        friend_viewer_id=friend_viewer,
        friend_card_id=friend_card,
        running_style=running_style,
        deck_source=source,
        succession_rank_point=points,
        succession_parent_only=parent_only if parent_only != points else 0,
        succession_signature=succession.tree_signature(trainee, node_1, node_2),
        succession_detail=detail,
    )
    return selection, account


#: Never spend carats on TP if it would take the free balance to or below this.
#: Carats are the one resource here that waiting does not replace, and this loop
#: runs unattended forever - so the floor is the difference between "top up TP
#: when it is short" and "drain the account while nobody is looking". Tune with
#: AIC_TP_JEWEL_FLOOR; 0 disables the floor entirely (spend down to nothing).
DEFAULT_JEWEL_FLOOR = 5000


def jewel_floor() -> int:
    raw = (os.environ.get("AIC_TP_JEWEL_FLOOR") or "").strip().replace(",", "")
    if not raw:
        return DEFAULT_JEWEL_FLOOR
    try:
        return max(0, int(raw))
    except ValueError:
        log.warning("AIC_TP_JEWEL_FLOOR=%r is not a number; using %s.",
                    raw, DEFAULT_JEWEL_FLOOR)
        return DEFAULT_JEWEL_FLOOR


#: What one 30-TP carat chunk costs. MEASURED at 10 (TP 16 -> 46 took carats
#: 9,307 -> 9,297 on 2026-09-02), but deliberately set well above that: this
#: number is only used to decide how many FREE carats to keep in reserve, and
#: under-estimating it is what lets a spend reach the paid bucket. Over-
#: estimating costs nothing but a few unspent free carats.
DEFAULT_CARAT_PRICE = 50


def carat_price() -> int:
    """Assumed price of a 30-TP chunk, for reserving free carats."""
    raw = (os.environ.get("AIC_TP_CARAT_PRICE") or "").strip().replace(",", "")
    if not raw:
        return DEFAULT_CARAT_PRICE
    try:
        return max(1, int(raw))
    except ValueError:
        log.warning("AIC_TP_CARAT_PRICE=%r is not a number; using %s.",
                    raw, DEFAULT_CARAT_PRICE)
        return DEFAULT_CARAT_PRICE


def _carat_balances(session: Session) -> tuple[int, int]:
    """``(free, paid)``. Paid carats are bought with real money and are NEVER
    a resource this farm may spend on its own."""
    ci = session.client.coin_info or {}
    return int(ci.get("fcoin", 0) or 0), int(ci.get("coin", 0) or 0)


def _recover_tp(session: Session, needed: int,
                use_jewels: bool = False) -> AccountState:
    """Spend TP potions - and optionally carats - to reach ``needed``."""
    account = read_account(session.load_index)
    # The real item_category-20 table, so event items (the +30 "Handmade
    # Chocolate" ones) count as TP too instead of being invisible.
    restore = racedata.tp_restore_items()
    have = [(iid, account.item_map.get(iid, 0))
            for iid in sorted(restore, key=lambda i: -restore[i])
            if account.item_map.get(iid, 0) > 0]
    carats, paid_before = _carat_balances(session)
    floor = jewel_floor()

    # What the spend below will actually cost, mirroring career.recover_tp's
    # chunk maths so the reserve matches the purchase.
    chunks = max(1, (needed - int(account.current_tp) + 29) // 30)
    cost = carat_price() * chunks

    # Decide about carats BEFORE spending anything, and say why either way.
    jewels_ok = False
    if use_jewels:
        if carats - cost >= floor:
            jewels_ok = True
        elif carats > floor:
            # THE paid-carat leak. The gate used to be `carats > floor`, so with
            # floor 0 a single free carat authorised a full-price refill and the
            # server funded the shortfall from coin_info["coin"] - real money.
            # Requiring the FREE balance to cover the whole purchase means a
            # floor of 0 spends free carats to zero and never past it.
            log.warning("Only %s free carats left and a refill needs about %s - "
                        "not spending, because the shortfall would come out of "
                        "PAID carats. (%s paid on this account.)",
                        f"{carats:,}", f"{cost:,}", f"{paid_before:,}")
        else:
            log.warning("Carat balance %s is at or below the floor of %s - not "
                        "spending carats on TP. Lower AIC_TP_JEWEL_FLOOR to "
                        "allow it.", f"{carats:,}", f"{floor:,}")

    if not any(n for _, n in have) and not jewels_ok:
        log.warning("TP is %s/%s, the account holds none of the %s TP items%s.",
                    account.current_tp, needed, len(restore),
                    "" if use_jewels else " and carat recovery is off "
                                          "(--recover-tp-jewels)")
        return account

    log.info("Recovering TP: %s -> %s (items %s; carats %s, floor %s%s)",
             account.current_tp, needed,
             ", ".join(f"{n}x#{i}(+{restore[i]}TP)" for i, n in have) or "none",
             f"{carats:,}", f"{floor:,}",
             ", carats ALLOWED" if jewels_ok else "")
    try:
        career.recover_tp(session, needed, use_jewels=jewels_ok)
    except Exception as exc:  # noqa: BLE001 - potions may run out mid-way
        log.warning("TP recovery stopped early (%s)", exc)
    account = read_account(session.refresh_home())
    after, paid_after = _carat_balances(session)
    if paid_after < paid_before:
        # Should now be unreachable, and is the one thing here worth shouting
        # about: this farm is never meant to spend money. Reported whatever the
        # free-carat delta says, because the old log read fcoin only and would
        # have shown a 3-free/7-paid spend as "spent 3".
        log.error("PAID carats were spent: %s -> %s (%s gone). Free carats %s "
                  "-> %s. This should not happen - raise AIC_TP_CARAT_PRICE "
                  "(now %s) so more free carats are held in reserve.",
                  f"{paid_before:,}", f"{paid_after:,}",
                  f"{paid_before - paid_after:,}", f"{carats:,}", f"{after:,}",
                  f"{carat_price():,}")
    if after < carats:
        log.info("TP is now %s. Carats %s -> %s (spent %s).", account.current_tp,
                 f"{carats:,}", f"{after:,}", f"{carats - after:,}")
    else:
        log.info("TP is now %s.", account.current_tp)
    return account


#: Highest scenario the auto-pick will guess at.
#:
#: WAS 3, on the reasoning that "every capture uses 3, and scenario 4 - though it
#: unlocks - has never started in idle mode". A UmaDumpy capture of the live game
#: on 2026-09-02 DISPROVES that: the real client's accepted idle start
#: (result_code 1) carries **scenario_id 4**. The old note was true of the July
#: captures the module was built from and has since gone stale.
MAX_IDLE_SCENARIO = 4


def pick_scenario(account: AccountState, requested: int = 0,
                  previous: int = 0, known: int = 0) -> int:
    """Which scenario the idle career runs.

    Deliberately conservative: prefer a scenario this account has actually run
    an idle career in, because not every *unlocked* scenario is necessarily
    startable in idle mode and a refusal reports only 205. Order is the explicit
    request, then a career seen this run, then the remembered one, then the
    newest unlocked that is *known to work in idle mode* (never above
    :data:`MAX_IDLE_SCENARIO`).
    """
    available = account.available_scenario_ids or [1]
    if requested:
        if requested not in available:
            raise IdleModeError(
                f"scenario {requested} is not unlocked (available: {available})"
            )
        return requested
    for candidate in (previous, known):
        if candidate and candidate in available:
            return candidate
    startable = [s for s in available if s <= MAX_IDLE_SCENARIO]
    return max(startable) if startable else max(available)


# --------------------------------------------------------------------------- #
# steps 6-7 - schedule and start
# --------------------------------------------------------------------------- #

def start_independent(session: Session, spec: Spec, selection: sel.StartSelection,
                      account: AccountState, scenario_id: int,
                      schedule_override: dict | None = None,
                      race_deck_num: int = 0, use_tp: int = 0,
                      settings: Settings | None = None, sweep: bool = True,
                      no_races: bool = False, recover_tp: bool = False,
                      fill_races: bool = False, race_goal: int = 0,
                      extra_races: list | None = None,
                      tp_jewels: bool = False, goals_only: bool = False):
    """pre_start -> build the schedule -> start.

    ``pre_start`` is not optional: it is how the server learns which scenario is
    being configured, and it supplies the account's last-used training policy
    and saved race agendas, which are the default schedule.
    """
    # The daily fan cap, at the ONE point every start routes through (aic start,
    # flow.run_cycle, rotate.visit). Before pre_start, which is the first live
    # call here, so a capped account costs no API traffic. Raises FanCapReached;
    # the loop drivers treat that as "skip until the reset", not as a failure.
    if settings is not None:
        fancap.check(settings, account.viewer_id, account.fans)

    # scenario_id 0, matching every captured client request.
    pre = independent.pre_start(session, spec, 0)
    log.info("pre_start: %s saved race agenda(s), last policy %s",
             len(pre.reserved_race_decks),
             "present" if pre.last_policy else "absent")

    # Race lists are per-trainee: build from its own route when master.mdb is
    # available, else fall back to the (generic, possibly wrong) saved agenda.
    rules = None
    if not no_races:
        try:
            rules = racedata.load(settings) if settings else None
        except racedata.RaceDataUnavailable as exc:
            log.warning("%s", exc)
            log.warning("Falling back to the saved agenda, which is not "
                        "trainee-specific and may be refused.")

    schedule = independent.build_schedule(
        pre, schedule_override, race_deck_num, no_races=no_races,
        rules=rules, chara_id=selection.card_id // 100, fill_years=fill_races,
        race_goal=race_goal, extra_races=extra_races, goals_only=goals_only,
    )
    log.info("Training schedule (%s): ground=%s rate_set=%s, %s priority skills, "
             "%s races",
             schedule.source,
             schedule.training_policy_ground_type,
             schedule.training_policy_param_rate_set_id,
             len(schedule.priority_skill_array), len(schedule.race_array))

    known = independent.load_known_tp(settings) if settings else 0
    candidates = independent.order_tp_candidates(spec, use_tp, known)

    # Only recover as much as a start actually costs. A pinned --use-tp is
    # exact; a price we have learned (15 during a half-cost event, 30 normally)
    # is trusted next; otherwise top up to the full 30 to be safe. This is what
    # keeps an event loop from burning potions to reach 30 when 15 will do.
    recover_target = use_tp or known or max(candidates)

    if recover_tp and account.current_tp < recover_target:
        account = _recover_tp(session, recover_target, use_jewels=tp_jewels)

    if account.current_tp < min(candidates):
        raise IdleModeError(
            f"not enough TP to start: have {account.current_tp}, "
            f"need at least {min(candidates)}"
        )

    # OBSERVED 2026-09-01 on a live account: with a valid trainee-specific race
    # list and 69 TP, idle_single_mode/start still answered 2509 - and the only
    # empty resource was item 95, the idle-mode "clock". read_account has always
    # parsed it into AccountState.clocks and nothing has ever looked at it.
    #
    # WARN, do not refuse: that 2509 -> clocks link is inferred from one account,
    # not proven, and a hard gate built on a guess would block a setup that works.
    # The point is that the next 2509 arrives with its likeliest cause attached
    # instead of as a bare server code.
    if account.clocks <= 0:
        log.info("Clock balance (item %s) is 0.", ITEM_CLOCK)
    if account.current_tp < max(candidates) and not (use_tp or known):
        log.warning("TP is %s: enough for the half-cost price (%s) but not the "
                    "full one (%s). If the half-cost campaign has ended the "
                    "start will be refused - use --recover-tp.",
                    account.current_tp, min(candidates), max(candidates))

    def _attempt():
        return independent.start(
            session, spec, selection, account, schedule, scenario_id,
            use_tp=use_tp, settings=settings,
            succession_rank_point=selection.succession_rank_point,
            succession_hint=account.succession_rank_point,
            sweep=sweep,
        )

    try:
        try:
            data, state = _attempt()
        except Exception as exc:  # noqa: BLE001 - only 2511 is handled here
            if "2511" not in str(exc) and "2051" not in str(exc):
                raise
            # Trained-character storage is full, so the server will not start a
            # career it has nowhere to put. Drop one surplus duplicate and try
            # ONCE more. The parents this start is about to use are excluded by
            # id - deleting one of them would make the retry fail on a stale
            # reference instead, which surfaces as a misleading 102.
            keep = {int(getattr(selection, "parent_id_1", 0) or 0),
                    int(getattr(selection, "parent_id_2", 0) or 0)}
            keep.discard(0)
            if veterans.make_room(session, keep) is None:
                raise
            data, state = _attempt()
    except Exception as exc:  # noqa: BLE001 - re-raised either way, just annotated
        if "2509" in str(exc):
            # 2509 is a refusal the server gives without saying why, and it is
            # NOT the clock balance: MEASURED 2026-09-02, an account holding 1
            # clock, TP 100/100, 729 succession points, 20 roster slots free, no
            # active career and a legal trainee-specific race plan still got it -
            # while the same account with --scenario 1 got 205 instead. So the
            # cause is configuration-dependent and currently unidentified.
            #
            # It is treated as a DEFERRAL rather than a failure regardless: the
            # farm cannot fix it by retrying hard, and a hard failure would count
            # against the account's backoff and eventually park it for an hour
            # anyway - but noisily, and as if something were broken.
            #
            # NO local `from . import fancap` here: a function-scoped import makes
            # the name local for the WHOLE function, so it would shadow the
            # module-level import and make the fancap.check() call at the top of
            # start_independent raise UnboundLocalError.
            wait = min(DEFER_RETRY_SECONDS, fancap.seconds_to_reset())
            raise StartDeferred(
                f"idle start refused (2509) - the server did not say why. "
                f"Checked and fine: TP {account.current_tp}, succession points "
                f"{account.succession_rank_point}, clocks {account.clocks}, "
                f"{len(schedule.race_array)} races in a trainee-specific plan, "
                f"scenario {scenario_id}. Retrying later.",
                wait,
            ) from exc
        raise
    return data, state, schedule


# --------------------------------------------------------------------------- #
# the whole cycle
# --------------------------------------------------------------------------- #

def run_cycle(settings: Settings, *, spec: Spec, trainee_card_id: int = 0,
              deck_id: int = 0, seed: int | None = None,
              wait_timeout: float | None = None, allow_force_end: bool = True,
              dry_run: bool = False, schedule_override: dict | None = None,
              scenario_id: int = 0, race_deck_num: int = 0,
              running_style: int = 1, use_tp: int = 0, sweep: bool = False,
              no_races: bool = False, forced_parents: str = "",
              succession_point: int = 0, recover_tp: bool = False,
              no_friend_card: bool = False, race_goal: int = 0,
              fill_races: bool = False, tp_jewels: bool = False,
              extra_races: list | None = None,
              auto_deck: bool = False, build_deck_slot: int = 10,
              session: Session | None = None) -> CycleResult:
    result = CycleResult()
    if session is None:
        session = connect(settings)
    else:
        # On a --loop second pass the cached home payload predates the career
        # we just started.
        session.refresh_home()

    state = inspect_career(session, spec)
    result.career_before = state

    if state.active:
        log.info("%s career: card %s, scenario %s, turn %s",
                 "Finished idle" if state.simulation_done else f"A {state.kind}",
                 state.card_id, state.scenario_id, state.turn)
        if state.simulation_done:
            log.info("Its simulation has already run; going straight to the finish.")
        elif state.seconds_remaining is not None and state.seconds_remaining > 0:
            if dry_run:
                log.info("[dry run] would wait %s for the idle clock.",
                         format_duration(state.seconds_remaining))
            else:
                result.waited_seconds = wait_for_finish(session, spec, state,
                                                        timeout=wait_timeout)
                session.refresh_home()
                state = inspect_career(session, spec)

        if state.active:
            if dry_run:
                log.info("[dry run] would end the career here.")
            else:
                end_result = end_running_career(session, state, spec,
                                                allow_force=allow_force_end,
                                                settings=settings)
                result.ended = end_result.ended
                if end_result.forced:
                    result.notes.append("career was force-deleted (not at end turn)")
    else:
        log.info("No career is currently running.")

    selection, account = build_selection(
        session, settings, trainee_card_id=trainee_card_id, deck_id=deck_id,
        running_style=running_style, forced_parents=forced_parents,
        succession_point=succession_point, no_friend_card=no_friend_card,
        auto_deck=auto_deck, build_deck_slot=build_deck_slot, seed=seed,
    )
    result.selection = selection
    previous = 0
    if result.career_before and result.career_before.kind == KIND_IDLE:
        previous = result.career_before.scenario_id
    known = independent.load_known_scenario(settings)
    chosen_scenario = pick_scenario(account, scenario_id, previous, known)
    log.info("Scenario: %s (available: %s; this run saw %s, remembered %s)",
             chosen_scenario, account.available_scenario_ids,
             previous or "-", known or "-")

    if dry_run:
        log.info("[dry run] would start an Independent career with the above "
                 "selection; stopping before any mutation.")
        return result

    _, started_state, schedule = start_independent(
        session, spec, selection, account, chosen_scenario,
        schedule_override=schedule_override, race_deck_num=race_deck_num,
        use_tp=use_tp, settings=settings, sweep=sweep, no_races=no_races,
        recover_tp=recover_tp, race_goal=race_goal, fill_races=fill_races,
        extra_races=extra_races, tp_jewels=tp_jewels,
    )
    result.started = True
    result.schedule = schedule
    result.career_after = started_state
    independent.save_known_scenario(settings, chosen_scenario)
    return result
