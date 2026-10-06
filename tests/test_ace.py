"""Ace Run: the per-day count, and borrowing one named support card.

aic.ace decides whether an account's next career is an Ace Run (Grand
Live, no races, borrowed Light Hello, skills bought): exactly RUNS (5) per
week (Monday 15:00 UTC reset); flow.borrow_card finds the wanted card,
refreshing the offer list the way the real client does.
"""

from __future__ import annotations

import os
import random
import sys
import tempfile
import types
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aic import flow, ace  # noqa: E402
from aic import selection as sel  # noqa: E402

FAILURES: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if cond else 'FAIL'} {label}" + (f"  {detail}" if not cond else ""))
    if not cond:
        FAILURES.append(label)


def ts(s: str) -> float:
    return datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc).timestamp()


def _settings():
    return types.SimpleNamespace(state_dir=Path(tempfile.mkdtemp()))


def _env(**kv):
    for k in ("AIC_ACE", "AIC_ACE_ONLY"):
        os.environ.pop(k, None)
    os.environ.update({k: str(v) for k, v in kv.items()})


def test_daily_count() -> None:
    print("ace run: exactly RUNS (5) a WEEK, the week starting with Monday's game-day")
    s, v = _settings(), 111
    _env()
    check("off unless AIC_ACE", ace.wanted(s, v) is False)
    _env(AIC_ACE=1, AIC_ACE_ONLY=222)
    check("AIC_ACE_ONLY skips other accounts", ace.wanted(s, v) is False)
    _env(AIC_ACE=1)
    check("5 runs a week by default", ace.RUNS == 5 and ace.PERIOD == "week")
    check("the week turns at Monday 15:00 UTC (Monday's game-day)",
          ace.period_key(ts("2026-10-05 14:59")) == "2026-W40"
          and ace.period_key(ts("2026-10-05 15:00")) == "2026-W41"
          and ace.period_key(ts("2026-10-12 14:59")) == "2026-W41")
    t = ts("2026-10-05 16:00")                   # Monday, just after the weekly reset
    for n, score in enumerate((ace.MIN_SCORE - 1, 5_000, 20_000, 15_000, 9_000), 1):
        check(f"run {n}: still wanted", ace.wanted(s, v, t))
        ace.record_start(s, v, t)
        check("a started Ace Run is flagged running", ace.is_running(s, v))
        ace.record_finish(s, v, {"rank_score": score}, t)
        check("finishing clears the flag", not ace.is_running(s, v))
    rec = ace.status(s, v, t)
    check("5 runs, 2 aces, best kept", (rec["started"], rec["good"], rec["best"]) == (5, 2, 20_000),
          str(rec))
    check("5 done -> fan farming", not ace.wanted(s, v, t))
    check("still done on the daily resets that follow (Tuesday .. Sunday)",
          not any(ace.wanted(s, v, ts(f"2026-10-{d:02d} 16:00")) for d in range(6, 12)))
    check("still done just before Monday's reset", not ace.wanted(s, v, ts("2026-10-12 14:59")))
    check("Monday 15:00 UTC starts 5 more", ace.wanted(s, v, ts("2026-10-12 15:01")))


def test_running_survives_reset() -> None:
    print("ace run: a career started before the weekly reset is still counted after it")
    _env(AIC_ACE=1)
    s, v = _settings(), 111
    ace.record_start(s, v, ts("2026-10-05 14:30"))      # last game-day of week 40
    check("still running across Monday 15:00", ace.is_running(s, v))
    rec = ace.record_finish(s, v, {"rank_score": 16_000}, ts("2026-10-05 15:20"))
    check("counted in the new week", rec["good"] == 1 and rec["period"] == "2026-W41", str(rec))
    check("...which still has all 5 runs to do", rec["started"] == 0
          and ace.wanted(s, v, ts("2026-10-05 15:21")), str(rec))


def test_caps() -> None:
    print("ace run: failed starts park the account until the next DAILY reset, not the week")
    _env(AIC_ACE=1)
    v, t = 111, ts("2026-10-06 16:00")
    s2 = _settings()
    ace.record_start(s2, v, t)
    ace.record_finish(s2, v, {"rank_score": 15_000}, t)
    for _ in range(ace.MAX_FAILED_STARTS):
        ace.record_failed_start(s2, v, t)
    check(f"{ace.MAX_FAILED_STARTS} failed starts -> stop for the day", not ace.wanted(s2, v, t))
    check("...back the next game-day, same week, run count kept",
          ace.wanted(s2, v, ts("2026-10-07 15:30")) and ace.status(s2, v, ts("2026-10-07 15:30"))["started"] == 1)
    check("junk summary counts as a try, not a crash",
          ace.record_finish(_settings(), v, None, t)["good"] == 0)


def test_one_time_job() -> None:
    print("ace run: a one-time job (roster ace_once) - N finished runs in total, then retired")
    import json as _json
    from aic import chores, rotate
    _env(AIC_ACE=1)
    s, v = _settings(), 294678112668
    acc = rotate.RosterAccount(v, "pw", "Ace_2668", ace_once=5, club=False)
    t = ts("2026-10-05 17:30")
    for n in range(1, 4):
        check(f"one-time run {n}: wanted", ace.wanted(s, v, t, once=5))
        ace.record_start(s, v, t)
        check("not retired while a run is in flight", not rotate._once_finished(s, acc))
        ace.record_finish(s, v, {"rank_score": 15_000}, t)
        ace.record_once_finish(s, v)
    check("a one-time job counts its own finished runs", ace.wanted(s, v, t, once=5))
    t2 = ts("2026-10-06 15:30")                  # after the next reset
    check("the count survives the reset (its own file)", ace.once_done(s, v) == 3)
    for _ in range(2):
        ace.record_start(s, v, t2)
        ace.record_finish(s, v, {"rank_score": 15_000}, t2)
        ace.record_once_finish(s, v)
    check("5 finished -> no more Ace Runs", not ace.wanted(s, v, t2, once=5))
    check("...and the account is retired from the rotation", rotate._once_finished(s, acc))
    check("a normal account is never 'retired'",
          not rotate._once_finished(s, rotate.RosterAccount(111, "pw")))
    s3 = _settings()
    for _ in range(ace.MAX_FAILED_STARTS):
        ace.record_failed_start(s3, v, t)
    check("failed starts still pause a one-time job until the reset",
          not ace.wanted(s3, v, t, once=5) and ace.wanted(s3, v, ts("2026-10-06 15:30"), once=5))

    roster = Path(tempfile.mkdtemp()) / "reroll_accounts.json"
    roster.write_text(_json.dumps([
        {"trainer_id": "111 111 111 111", "password": "a", "name": "Farm"},
        {"trainer_id": "294678112668", "password": "b", "ace_once": 5, "club": False}]))
    accounts, _ = rotate.load_roster(roster)
    check("roster: ace_once / club parsed, defaults kept",
          [(a.ace_once, a.club) for a in accounts] == [(0, True), (5, False)], str(accounts))
    bad = Path(tempfile.mkdtemp()) / "r.json"
    bad.write_text(_json.dumps([{"trainer_id": "1", "password": "b", "ace_once": "five"}]))
    try:
        rotate.load_roster(bad)
        check("a bad ace_once is refused", False)
    except rotate.RotationError:
        check("a bad ace_once is refused", True)
    real = rotate.load_roster
    rotate.load_roster = lambda *a, **k: (accounts, {})
    try:
        check("club gate ignores \"club\": false accounts", chores._roster_ids() == {111111111111})
    finally:
        rotate.load_roster = real
    ran = []
    real_chores = dict(chores._CHORES)
    os.environ["AIC_CHORES"] = os.environ["AIC_TEAM"] = "1"     # as on the VPS
    for name in chores._CHORES:
        chores._CHORES[name] = (lambda sess, l, lbl="", n=name: ran.append(n),
                                lambda rec, now: True, lambda now: now)
    try:
        chores.run_due(types.SimpleNamespace(), _settings(), acc)
        check("no club chore for a \"club\": false account, team still runs",
              "club" not in ran and "team" in ran, str(ran))
    finally:
        chores._CHORES.clear()
        chores._CHORES.update(real_chores)
        os.environ.pop("AIC_CHORES", None)
        os.environ.pop("AIC_TEAM", None)

    # the owner's own turn-by-turn career on a one-time account is never ended
    ended = []
    sess = types.SimpleNamespace(viewer_id=v, load_index={}, refresh_home=lambda: {})
    saved = (rotate._ensure_bound, rotate.flow.connect, rotate.chores.run_due,
             rotate.flow.inspect_career, rotate.flow.end_running_career)
    rotate._ensure_bound = lambda *a, **k: None
    rotate.flow.connect = lambda *a, **k: sess
    rotate.chores.run_due = lambda *a, **k: None
    rotate.flow.inspect_career = lambda *a, **k: types.SimpleNamespace(
        active=True, kind="normal", seconds_remaining=None, simulation_done=False)
    rotate.flow.end_running_career = lambda *a, **k: ended.append(1)
    try:
        res = rotate.visit(_settings(), None, acc)
        check("a career the farm did not start is left alone (no force-delete)",
              not ended and res.ok and res.source == "ace-once" and res.remaining_seconds == 3600,
              f"{ended} {res}")
    finally:
        (rotate._ensure_bound, rotate.flow.connect, rotate.chores.run_due,
         rotate.flow.inspect_career, rotate.flow.end_running_career) = saved


class _GD:
    def support_rarity_rank(self, sid):
        return 3

    def support_name(self, sid):
        return f"card {sid}"


def _apt(short=1, mile=1, middle=1, long=1, turf=7, dirt=1, style=7):
    """Card base aptitudes (1..8 = G..S), styles all at ``style``."""
    return {"proper_distance_short": short, "proper_distance_mile": mile,
            "proper_distance_middle": middle, "proper_distance_long": long,
            "proper_ground_turf": turf, "proper_ground_dirt": dirt,
            "proper_running_style_nige": style, "proper_running_style_senko": style,
            "proper_running_style_sashi": style, "proper_running_style_oikomi": style}


def test_pick_trainee() -> None:
    print("trainee: a character NOT on the Team Trials team, the one the team needs most")
    _env(AIC_ACE=1)
    s, v, t = _settings(), 111, ts("2026-10-02 16:00")
    apts = {100601: _apt(middle=7, long=7),            # Oguri - already on the team
            101401: _apt(middle=7, long=7),            # El Condor - stayer
            101101: _apt(short=7, mile=6),             # a sprinter
            101201: _apt(mile=7, dirt=7),              # a dirt runner
            102201: _apt(short=8)}                     # S sprinter, but unstartable (below)
    oguri = {"trained_chara_id": 1, "card_id": 100601, "rank_score": 17_000, **_apt(middle=7, long=7)}
    index = {"card_list": [{"card_id": c} for c in apts],
             "trained_chara": [oguri],
             "team_stadium_user": {"team_class": 1},
             "team_data_array": [{"distance_type": 3, "member_id": 1,
                                  "trained_chara_id": 1, "running_style": 2}]}
    real = sel.UNSTARTABLE_CHARAS.copy()
    sel.UNSTARTABLE_CHARAS[1022] = "test: cannot start"
    try:
        card, why = ace.pick_trainee(s, v, index, t, apts=apts)
        check("never the uma already on the team (Oguri)", card // 100 != 1006, str(card))
        check("never an unstartable trainee", card != 102201, str(card))
        check("an open slot beats nothing: a full-score pick", card in (101401, 101101, 101201)
              and "open" in why, f"{card} {why}")
        # used for an Ace Run this week -> not again this week
        ace.record_start(s, v, t, chara=card // 100)
        card2, _ = ace.pick_trainee(s, v, index, t, apts=apts)
        check("not the same uma twice in one week", card2 // 100 != card // 100 and card2, str(card2))
        check("this week's umas are remembered, across the daily resets",
              card // 100 in ace.status(s, v, t)["charas"]
              and card // 100 in ace.status(s, v, t + 2 * 86400)["charas"])
        check("...and forgotten at Monday's reset",
              ace.status(s, v, ts("2026-10-05 15:01")).get("charas") in (None, []))
        # everyone owned is on the team or used -> no pick (roster trainee is used)
        index2 = {**index, "card_list": [{"card_id": 100601}]}
        check("nothing eligible -> (0, '')", ace.pick_trainee(s, v, index2, t, apts=apts) == (0, ""))
        # only this week's Ace-Run uma is left off the team -> reuse it, never an on-team one
        index3 = {**index, "card_list": [{"card_id": 100601}, {"card_id": card}]}
        check("week used up -> no pick by default",
              ace.pick_trainee(s, v, index3, t, apts=apts) == (0, ""))
        again, _ = ace.pick_trainee(s, v, index3, t, apts=apts, reuse_week=True)
        check("...reuse_week runs this week's uma again, not the on-team Oguri",
              again == card, f"{again} vs {card}")
    finally:
        sel.UNSTARTABLE_CHARAS.clear()
        sel.UNSTARTABLE_CHARAS.update(real)

    # a full team: pick whoever beats the weakest member by the most
    s3 = _settings()
    weak = {"trained_chara_id": 2, "card_id": 101101, "rank_score": 3_000, **_apt(short=7)}
    full = {"card_list": [{"card_id": 100601}, {"card_id": 101101}, {"card_id": 101201}],
            "trained_chara": [oguri, weak],
            "team_stadium_user": {"team_class": 1},
            "team_data_array": [{"distance_type": d, "member_id": 1, "trained_chara_id": tc,
                                 "running_style": 1}
                                for d, tc in ((1, 2), (2, 1), (3, 1), (4, 1), (5, 1))]}
    card, why = ace.pick_trainee(s3, v, full, t, apts=apts)
    check("full team: the dirt/mile runner, the only uma not on it", card == 101201, f"{card} {why}")

    # the Ace Run that JUST ended (not on the team yet) is skipped via exclude
    s4 = _settings()
    open_team = {**index, "card_list": [{"card_id": c} for c in (101401, 101101)]}
    first, _ = ace.pick_trainee(s4, v, open_team, t, apts=apts)
    other, _ = ace.pick_trainee(s4, v, open_team, t, apts=apts, exclude={first // 100})
    check("exclude skips the uma whose Ace Run just ended", other and other // 100 != first // 100,
          f"{first} -> {other}")
    # an uma that already HAS an ace (S+ trainee, off the team) comes last
    aced = {"trained_chara_id": 9, "card_id": 101401, "rank_score": 17_909, **_apt(middle=7, long=7)}
    with_ace = {**open_team, "trained_chara": [oguri, aced]}
    card, why = ace.pick_trainee(_settings(), v, with_ace, t, apts=apts)
    check("an uma with no ace yet beats one that already has an ace", card == 101101, f"{card} {why}")
    only_aced = {**with_ace, "card_list": [{"card_id": 101401}]}
    card, why = ace.pick_trainee(_settings(), v, only_aced, t, apts=apts)
    check("...but it is still used once every fresher uma is gone",
          card == 101401 and "fresher" in why, f"{card} {why}")


def _offer(viewer, sid, lb=4):
    return {"viewer_id": viewer, "support_card_id": sid, "limit_break_count": lb}


def test_borrow_card() -> None:
    print("borrow: take the wanted card, refreshing until it shows up")
    real = sel.deck_conflict
    # Light Hello: 10083 (R) and 30052 (SSR) are the same character
    sel.deck_conflict = lambda ids, t, g: ("same uma" if {10083, 30052} <= set(ids) else None)
    try:
        sent = []

        class Client:
            def __init__(self, lists):
                self.lists = list(lists)

            def call(self, ep, body):
                sent.append((ep, body))
                if isinstance(self.lists[0], Exception):
                    raise self.lists.pop(0)
                return {"data": {"support_card_data_array": self.lists.pop(0),
                                 "summary_user_info_array": []}}

        def session(first, reloads):
            return types.SimpleNamespace(
                refresh_pre_single_mode=lambda: {"friend_support_card_data":
                                                 {"support_card_data_array": first}},
                client=Client(reloads))

        rng = random.Random(1)
        got = flow.borrow_card(session([_offer(1, 30010), _offer(2, 30052, 3), _offer(3, 30052, 4)], []),
                               30052, 5, [20001], 100601, _GD(), rng)
        check("on the first list: the highest-uncap copy", got == (3, 30052), str(got))
        check("no refresh needed", not sent, str(sent))

        sent.clear()
        got = flow.borrow_card(session([_offer(1, 30010)], [[_offer(4, 30107)], [_offer(5, 30052)]]),
                               30052, 5, [20001], 100601, _GD(), rng)
        check("found after two refreshes", got == (5, 30052), str(got))
        check("refresh is friend_support_card_reload", sent[0][0] == "pre_single_mode/friend_support_card_reload")
        check("each refresh excludes every viewer seen so far",
              sent[0][1] == {"exclude_viewer_id_array": [1]}
              and sent[1][1] == {"exclude_viewer_id_array": [1, 4]}, str(sent))

        sent.clear()
        got = flow.borrow_card(session([_offer(1, 30010, 2)], [[_offer(4, 30107)], []]),
                               30052, 2, [20001], 100601, _GD(), rng)
        check("not found after the reloads -> best card of ALL lists seen", got == (4, 30107), str(got))

        got = flow.borrow_card(session([_offer(1, 30010, 4)], [[]]),
                               30052, 1, [20001], 100601, _GD(), rng)
        check("an empty refresh still falls back to the first list", got == (1, 30010), str(got))

        got = flow.borrow_card(session([_offer(1, 30010)], [RuntimeError("boom")]),
                               30052, 3, [20001], 100601, _GD(), rng)
        check("a failed refresh falls back instead of raising", got == (1, 30010), str(got))

        sent.clear()
        got = flow.borrow_card(session([_offer(1, 30052), _offer(2, 30010)], [[], []]),
                               30052, 2, [10083], 100601, _GD(), rng)
        check("an owned Light Hello in the deck blocks borrowing 30052", got == (2, 30010), str(got))
        check("...and it stops refreshing (the card IS offered)", not sent, str(sent))
    finally:
        sel.deck_conflict = real


def main() -> int:
    for fn in (test_daily_count, test_running_survives_reset, test_caps, test_one_time_job,
               test_pick_trainee, test_borrow_card):
        fn()
        print()
    _env()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
        return 1
    print("all Ace Run checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
