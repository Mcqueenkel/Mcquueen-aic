"""Ending and starting careers.

An idle (Independent) career is finished in two stages, as the real client does:

1. ``idle_single_mode/end`` stops the wall-clock simulation and returns the
   progress log. The career still exists.
2. The scenario's own ``<prefix>/factor_select`` then ``<prefix>/finish``
   retire it and bank the trained character.

``<prefix>/factor_lottery`` sits between those two in the client, and is the
*reroll*: it costs 30 TP per roll. We never call it - passing
``factor_lottery_id=1`` to ``finish`` takes the initial roll as-is.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from . import bootstrap, independent, racedata, skills
from .config import ITEM_SUCCESSION_RANK_POINT
from .logging_setup import get
from .selection import StartSelection
from .state import KIND_IDLE, CareerState, enrich_from_chara_info, read_account, read_career

log = get("career")

#: Factor lottery slot taken at career end. 1 is the initial roll, so taking it
#: is exactly "accept the first factors, do not reroll".
FIRST_FACTOR_LOTTERY_ID = 1


class CareerError(RuntimeError):
    pass


@dataclass
class EndResult:
    ended: bool = False
    forced: bool = False
    turn: int = 0
    idle_stopped: bool = False
    summary: dict = field(default_factory=dict)
    progress_log: dict = field(default_factory=dict)
    skills_bought: int = 0


def prefix_for(client, scenario_id: int) -> str:
    client.set_scenario(int(scenario_id or 0))
    return client.api_prefix


def load_career(session, scenario_id: int) -> dict:
    """``<prefix>/load`` for a normal career, or ``{}`` if none is running."""
    prefix_for(session.client, scenario_id)
    try:
        res = session.client.load_career()
    except Exception as exc:  # noqa: BLE001
        rc = bootstrap.result_code(exc)
        if rc in (102, 201):
            log.debug("%s/load returned %s - treating as no active career",
                      session.client.api_prefix, rc)
            return {}
        raise
    session.clock.observe(res)
    return (res or {}).get("data") or {}


# --------------------------------------------------------------------------- #
# ending
# --------------------------------------------------------------------------- #

def end_career(session, career: CareerState, spec: independent.Spec,
               allow_force: bool = True, buy_skills: bool = False) -> EndResult:
    """Stop the idle timer (if any), take the first factors, and finish.

    ``buy_skills`` spends the leftover points even with AIC_BUY_SKILLS off - an
    Ace Run (aic.ace) buys them because they add to rank_score."""
    client = session.client
    result = EndResult()
    # Assigned only inside the idle branch below, but read after it: without
    # this the "already stopped" path raises NameError.
    chara = None

    if career.kind == KIND_IDLE and career.simulation_done:
        # An earlier invocation already stopped it (or it was stopped in game).
        # Calling end again answers 205, and the client spends its full retry
        # budget on that before we ever see it - so don't.
        log.info("Idle simulation is already stopped (turn %s); skipping "
                 "idle_single_mode/end.", career.turn)
    elif career.kind == KIND_IDLE:
        log.info("Stopping the idle run (%s)...", spec.endpoint("end"))
        end_data = {}
        try:
            end_data = independent.end(session, spec)
            result.progress_log = end_data.get("progress_log_info") or {}
            result.idle_stopped = True
        except Exception as exc:  # noqa: BLE001
            rc = bootstrap.result_code(exc)
            if rc in (102, 205):
                log.info("idle end returned %s (already stopped); continuing.", rc)
            else:
                raise CareerError(f"idle_single_mode/end failed ({exc})") from exc

        # The career is now an ordinary one sitting at its final turn. Its real
        # turn is NOT the home screen's idle summary (which stays at 1) - taking
        # that would make factor_select/finish fail with 205.
        chara = independent.final_chara_info(session, spec, end_data)
        if chara:
            enrich_from_chara_info(career, chara)
            log.info("Career is at turn %s (scenario %s, state %s).",
                     career.turn, career.scenario_id, career.state)

    prefix_for(client, career.scenario_id)
    turn = _resolve_final_turn(session, career)
    result.turn = turn
    if buy_skills and not chara:
        # Resumed: an earlier visit stopped the idle run, so there is no end
        # response to read the skill points from. <prefix>/load has them.
        try:
            chara = load_career(session, career.scenario_id).get("chara_info") or None
        except Exception as exc:  # noqa: BLE001 - buying is best-effort
            log.info("Could not reload the career to buy skills (%s).", exc)

    # Spend the run's leftover skill points, if asked to. OFF by default.
    #
    # This sits between the prefix flip and factor_select on purpose: the factor
    # pool is drawn from the skills the character owns at that moment, so buying
    # afterwards would change nothing. MEASURED here 2026-09-05: an idle career
    # arrives with ~2604 unspent points and ~25 buyable groups, so the
    # auto-trainer does not spend them during the run.
    if chara:
        tips = chara.get("skill_tips_array") or []
        log.info("Skill budget at finish: %s points, %s buyable group(s) offered.",
                 chara.get("skill_point", "?"), len(tips))
        if skills.buy_enabled() or buy_skills:
            result.skills_bought = skills.buy_from(session, chara, turn)

    log.info("Finishing career: card %s, scenario %s (%s), turn %s",
             career.card_id, career.scenario_id, client.api_prefix, turn)

    try:
        client.factor_select(current_turn=turn)
        log.info("Accepted the first factor roll (skipping factor_lottery, "
                 "which would cost 30 TP per reroll).")
    except Exception as exc:  # noqa: BLE001
        if bootstrap.result_code(exc) == 102:
            log.info("factor_select returned 102 (no factor step pending); continuing.")
        else:
            log.warning("factor_select failed (%s); attempting finish anyway.", exc)

    try:
        res = client.finish_career(
            current_turn=turn,
            is_force_delete=False,
            factor_lottery_id=FIRST_FACTOR_LOTTERY_ID,
        )
        result.ended = True
        result.summary = _summarize(res)
        log.info("Career finished.")
        return result
    except Exception as exc:  # noqa: BLE001
        rc = bootstrap.result_code(exc)
        if not allow_force or rc not in (102, 205):
            raise CareerError(f"finish failed ({exc})") from exc
        log.warning("Normal finish rejected with %s - the career is not at its end "
                    "turn. Force-deleting it instead (no trained character is kept).",
                    rc)

    res = client.finish_career(
        current_turn=turn,
        is_force_delete=True,
        factor_lottery_id=FIRST_FACTOR_LOTTERY_ID,
    )
    result.ended = True
    result.forced = True
    result.summary = _summarize(res)
    log.info("Career force-deleted.")
    return result


#: A completed career sits well past this. An idle career's home summary
#: reports turn 1 regardless, so a low turn here means we failed to read the
#: real one and must go ask the scenario directly.
_SUSPICIOUS_FINAL_TURN = 10


def _resolve_final_turn(session, career: CareerState) -> int:
    """The turn to send to factor_select/finish.

    ``<prefix>/load`` is the last resort: it is authoritative for a career that
    is no longer idle, which covers both a resumed run (the idle stage was
    already stopped in an earlier invocation) and any case where ``end`` did not
    return its ``chara_info``.
    """
    if career.turn > _SUSPICIOUS_FINAL_TURN:
        return career.turn

    log.info("Career turn reads %s, which is too low for a finished run; "
             "asking %s/load.", career.turn, session.client.api_prefix)
    try:
        detail = load_career(session, career.scenario_id)
    except Exception as exc:  # noqa: BLE001
        log.warning("%s/load failed (%s); proceeding with turn %s.",
                    session.client.api_prefix, exc, career.turn)
        return career.turn or 1

    chara = detail.get("chara_info") or {}
    if chara:
        enrich_from_chara_info(career, chara)
        log.info("Resolved turn %s from %s/load.",
                 career.turn, session.client.api_prefix)
    return career.turn or 1


def _summarize(finish_response: Any) -> dict:
    data = (finish_response or {}).get("data") or {}
    common = data.get("single_mode_finish_common") or {}
    my_id = common.get("trained_chara_id")
    for row in common.get("trained_chara") or []:
        if row.get("trained_chara_id") == my_id:
            return {
                "trained_chara_id": my_id,
                "card_id": row.get("card_id"),
                "rank": row.get("rank"),
                "rank_score": row.get("rank_score"),
                "fans": row.get("fans"),
                "factors": row.get("factor_info_array") or row.get("factor_id_array"),
            }
    return {"trained_chara_id": my_id}


# --------------------------------------------------------------------------- #
# starting a NORMAL (turn-by-turn) career
# --------------------------------------------------------------------------- #

def start_normal_career(session, selection: StartSelection, scenario_id: int,
                        use_tp: int = 30) -> dict:
    """Start an ordinary career through Icarus' ``<prefix>/start``.

    Not used by the idle flow; kept because it is the fallback if idle mode is
    unavailable on the account.
    """
    client = session.client
    load_index = session.refresh_home()
    account = read_account(load_index)
    session.refresh_pre_single_mode()

    if read_career(load_index, session.clock).active:
        raise CareerError("a career is already running; end it before starting a new one")
    if account.current_tp < use_tp:
        raise CareerError(
            f"not enough TP to start: have {account.current_tp}, need {use_tp}"
        )

    prefix_for(client, scenario_id)
    res = client.start_career(
        card_id=selection.card_id,
        support_card_ids=selection.support_card_ids,
        friend_viewer_id=selection.friend_viewer_id,
        friend_card_id=selection.friend_card_id,
        parent_id_1=selection.parent_id_1,
        parent_id_2=selection.parent_id_2,
        scenario_id=scenario_id,
        deck_id=selection.deck_id,
        use_tp=use_tp,
        tp_info=account.tp_info,
        current_money=account.gold,
        succession_rank_point=account.succession_rank_point,
        succession_point_hint=account.item_map.get(ITEM_SUCCESSION_RANK_POINT, 0),
    )
    return (res or {}).get("data") or {}


def recover_tp(session, needed: int, use_jewels: bool = False) -> int:
    """Spend TP potions until ``needed`` TP is available. Returns current TP.

    ``use_jewels`` lets the client fall through to ``user/recovery_trainer_point``
    once the potions run out - i.e. spend CARATS. Off by default and gated by a
    carat floor in :func:`aic.flow._recover_tp`, because this runs unattended and
    forever: premium currency is the one resource here that cannot be re-earned
    by waiting.
    """
    account = read_account(session.load_index)
    if account.current_tp >= needed:
        return account.current_tp
    chunks = max(1, (needed - account.current_tp + 29) // 30)
    log.info("TP is %s/%s; recovering %s chunk(s)%s.", account.current_tp, needed,
             chunks, " (carats allowed)" if use_jewels else " (potions only)")
    session.client.recovery_tp(count=chunks, use_jewels=use_jewels,
                               items=racedata.tp_restore_items())
    time.sleep(0.5)
    return read_account(session.refresh_home()).current_tp
