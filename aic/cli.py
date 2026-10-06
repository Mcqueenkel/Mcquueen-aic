"""Command-line interface."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from . import __version__
# Safe at module level: fancap imports only .config and .logging_setup, so unlike flow /
# independent / racedata it does not depend on bootstrap's path+env setup. It has to be
# here rather than in main() - _dispatch() is where FanCapReached is caught, and main()'s
# local import is not visible from there.
from . import fancap
from .config import ConfigError, Settings, load_settings
from .logging_setup import setup


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="aic",
        description="Headless driver for Umamusume Independent (idle) Careers.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Credentials come from the Icarus checkout's\n"
            "uma_runtime/<profile>/auth_config.json. Run Icarus once to capture\n"
            "them from the game before using this tool."
        ),
    )
    parser.add_argument("--version", action="version", version=f"aic {__version__}")
    parser.add_argument("-p", "--profile", help="Icarus runtime profile (default: default)")
    parser.add_argument("--icarus-root", help="path to the Icarus-Dev-Build checkout")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    parser.add_argument("--steam-code", default="", metavar="CODE",
                        help="Steam Guard code, to mint a ticket when Guard is "
                             "blocking. For unattended use store steam_shared_secret "
                             "instead (account.bat steam-secret)")

    parser.add_argument("--no-ticket-refresh", action="store_true",
                        help="reuse the saved Steam ticket instead of minting a fresh one")
    parser.add_argument("--no-delay", action="store_true",
                        help="remove EVERY delay between requests, including the Icarus client's per-request floor. An experiment: the edge (Akamai) tends to answer 403 at machine speed")

    sub = parser.add_subparsers(dest="command", required=True)

    # -v also works after the subcommand (the more natural place to type it),
    # not just before it. SUPPRESS means "not given here" leaves whatever the
    # top-level parser already set, rather than clobbering True back to False.
    def add_verbose(p: argparse.ArgumentParser) -> None:
        p.add_argument("-v", "--verbose", action="store_true", default=argparse.SUPPRESS)
        # Accepted after the subcommand too, which is where it gets typed.
        p.add_argument("--no-delay", action="store_true", default=argparse.SUPPRESS)

    p_login = sub.add_parser("login", help="authenticate and reach the main screen")
    p_login.add_argument("--json", action="store_true", help="print the account summary as JSON")
    add_verbose(p_login)

    p_status = sub.add_parser("status", help="show account and current-career state")
    add_verbose(p_status)

    p_fan = sub.add_parser(
        "fan-cards",
        help="rank owned support cards by Fan Bonus (accounts for level + uncap)")
    p_fan.add_argument("--trainee", type=int, default=0, metavar="CARD_ID",
                       help="mark support cards of the trainee's own character "
                            "ineligible (you cannot deck an uma against itself)")
    p_fan.add_argument("--all", action="store_true",
                       help="also list cards that have no fan bonus")
    p_fan.add_argument("--json", action="store_true", help="print as JSON")
    add_verbose(p_fan)

    p_wait = sub.add_parser("wait", help="wait for the running idle career to finish")
    p_wait.add_argument("--timeout", type=float, default=None,
                        help="give up after N seconds")
    add_verbose(p_wait)

    p_end = sub.add_parser("end", help="stop the idle run, take the first factors, finish")
    p_end.add_argument("--no-force", action="store_true",
                       help="fail instead of force-deleting an unfinished career")
    add_verbose(p_end)

    p_plan = sub.add_parser("plan", help="show the trainee/parents/deck that would be used")
    _selection_args(p_plan)
    add_verbose(p_plan)

    p_sched = sub.add_parser("schedule",
                             help="show the training schedule that would be sent")
    _schedule_args(p_sched)
    p_sched.add_argument("--trainee", type=int, default=0, metavar="CARD_ID",
                         help="build the race list for this trainee's route "
                              "(default: show the raw agenda)")
    add_verbose(p_sched)

    p_start = sub.add_parser("start", help="start a new Independent career")
    _selection_args(p_start)
    _schedule_args(p_start)
    add_verbose(p_start)

    p_run = sub.add_parser("run", help="the whole cycle: wait, end, start")
    _selection_args(p_run)
    _schedule_args(p_run)
    p_run.add_argument("--timeout", type=float, default=None,
                       help="give up waiting after N seconds")
    p_run.add_argument("--no-force", action="store_true")
    p_run.add_argument("--dry-run", action="store_true",
                       help="report what would happen without mutating anything")
    p_run.add_argument("--loop", action="store_true",
                       help="keep cycling: after starting, wait that run out too")
    p_run.add_argument("--max-cycles", type=int, default=0,
                       help="stop after N cycles when --loop is set (0 = unlimited)")
    add_verbose(p_run)

    p_discover = sub.add_parser("discover", help="dump the idle-career surface for inspection")
    add_verbose(p_discover)

    return parser


def _selection_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--trainee", type=int, default=0, metavar="CARD_ID",
                   help="force a specific trainee card id")
    p.add_argument("--deck", type=int, default=0, metavar="DECK_ID",
                   help="force a saved support deck (default: first usable, else build one)")
    p.add_argument("--auto-deck", action="store_true",
                   help="auto-build the deck from the highest Fan Bonus cards you "
                        "own (level + uncap applied, no duplicate uma, none the "
                        "trainee's own uma). Overrides --deck")
    p.add_argument("--build-deck-slot", type=int, default=10, metavar="N",
                   help="deck slot (1-10) the auto-built deck is written to "
                        "(default: 10)")
    p.add_argument("--seed", type=int, default=None,
                   help="RNG seed, for reproducible random picks")
    p.add_argument("--running-style", type=int, default=1, choices=(1, 2, 3, 4),
                   help="1 front, 2 pace, 3 late, 4 end (default: 1)")
    p.add_argument("--parents", metavar="ID1,ID2",
                   help="force a specific pair of trained_chara_ids as succession "
                        "parents, bypassing the rank-based pick")
    p.add_argument("--succession-point", type=int, default=0, metavar="N",
                   help="send this current_succession_rank_point verbatim instead "
                        "of the computed value (skips the search entirely)")
    p.add_argument("--no-friend-card", action="store_true",
                   help="start without a borrowed 6th support card (friend_viewer_id/"
                        "friend_card_id both 0). A career normally needs one; use "
                        "this to isolate whether a stale/invalid borrow is what a "
                        "205 objects to")


def _schedule_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--scenario", type=int, default=0,
                   help="scenario id (default: highest unlocked)")
    p.add_argument("--schedule", type=Path, metavar="FILE",
                   help="JSON overriding any of training_policy_ground_type, "
                        "training_policy_param_rate_set_id, priority_skill_array, race_array")
    p.add_argument("--race-agenda", type=int, default=0, metavar="N",
                   help="saved race agenda deck_num to use (default: account default, "
                        "else the fullest - more races means more fans)")
    p.add_argument("--race-goal", type=int, default=0, metavar="N",
                   help="schedule about N races in total (35 is a good target). "
                        "The trainee's mandatory goal races and anything given "
                        "with --races are always kept; the rest are filled in "
                        "with the highest-fan races it can actually enter")
    p.add_argument("--races", metavar="SPEC",
                   help="races to add on top of the goal races, as "
                        "'year:program_id' separated by commas, e.g. "
                        "'2:1023,3:4'. A bare program id defaults to the only "
                        "year it is legal in. Never replaces a goal race")
    p.add_argument("--fill-races", action="store_true",
                   help="fill to the built-in default target without naming one "
                        "(--race-goal N is the explicit form)")
    p.add_argument("--no-races", action="store_true",
                   help="send an empty race list. Saved agendas are generic, but "
                        "each trainee has its own route and forced goal races, so an "
                        "agenda built for another character can be refused - use this "
                        "to isolate whether the race list is what the server rejects")
    p.add_argument("--use-tp", type=int, default=0, metavar="N",
                   help="pin the start's TP cost instead of probing "
                        "(30 normally, 15 during a half-cost campaign)")
    p.add_argument("--recover-tp-jewels", action="store_true",
                       help="when the TP potions run out, spend CARATS to finish the "
                            "top-up (user/recovery_trainer_point). Off by default: this "
                            "loop runs unattended and carats cannot be re-earned by "
                            "waiting. AIC_TP_JEWEL_FLOOR (default 5000) is never spent "
                            "below")
    p.add_argument("--recover-tp", action="store_true",
                   help="spend TP potions before starting if TP is short of the "
                        "full 30 cost. Without this, a run with only enough TP for "
                        "the half-cost price will be refused once that campaign ends")
    # The rank point is computed exactly (22/22 against captured accepted
    # starts), so the search is OFF by default: a 205 is nearly always a
    # different field - an invalid race_array or deck - and sweeping ~40 live
    # calls at the wrong field only hides the real error.
    p.add_argument("--sweep", action="store_true",
                   help="if the start is refused, search for a succession rank "
                        "point the server accepts. Off by default: the value is "
                        "computed exactly, so a refusal almost always means a "
                        "different field is wrong. Debugging aid only")
    p.add_argument("--no-sweep", action="store_true",
                   help=argparse.SUPPRESS)  # accepted for compatibility; now the default


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if getattr(args, "no_delay", False):
        from . import pacing
        pacing.enable()

    try:
        settings = load_settings(args.profile, args.icarus_root, args.verbose)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    log = setup(settings.state_dir, args.verbose)
    log.debug("profile=%s icarus_root=%s", settings.profile, settings.icarus_root)

    # Imported after settings resolve so bootstrap's path/env setup is in order.
    from . import flow, independent, racedata
    from .auth import AuthError
    from .career import CareerError
    # bind() rather than icarus(): the bootstrap binds LAZILY (flow.connect is
    # what normally triggers it), so icarus() here raises "bind() has not been
    # called yet". bind() is idempotent and this is the point the module
    # docstring says it belongs - before anything touches the wire.
    from .bootstrap import bind
    ApiError = bind(settings).ApiError
    from .independent import IdleModeError, load_spec
    from .selection import SelectionError

    globals()["racedata"] = racedata

    spec = load_spec()
    if spec.endpoints or spec.payloads:
        log.info("Applying protocol overrides from %s", spec.loaded_from)

    try:
        return _dispatch(args, settings, spec, log, flow, independent)
    # ApiError included: a bare server refusal (2509, 1808, ...) reaching the top
    # printed a full traceback, which reads like a crash in the tool rather than a
    # decision by the server.
    except (AuthError, CareerError, SelectionError, IdleModeError,
            ConfigError, ApiError) as exc:
        log.error("%s", exc)
        return 1
    except KeyboardInterrupt:
        log.warning("Interrupted.")
        return 130


def _dispatch(args, settings: Settings, spec, log, flow, independent) -> int:
    from .state import format_duration, read_account

    refresh_ticket = not args.no_ticket_refresh
    cmd = args.command

    if cmd == "login":
        session = flow.connect(settings, refresh_ticket=refresh_ticket, steam_code=args.steam_code)
        if getattr(args, "json", False):
            account = read_account(session.load_index)
            print(json.dumps({
                "viewer_id": account.viewer_id or session.viewer_id,
                "gold": account.gold,
                "tp": account.current_tp,
                "succession_rank_point": account.succession_rank_point,
                "scenarios": account.available_scenario_ids,
                "idle_mode_available": account.idle_mode_available,
                "decks": account.support_card_decks,
            }, indent=2))
        return 0

    if cmd == "status":
        session = flow.connect(settings, refresh_ticket=refresh_ticket, steam_code=args.steam_code)
        state = flow.inspect_career(session, spec)
        if not state.active:
            log.info("No career in progress.")
            return 0
        log.info("Career in progress (%s):", state.kind)
        log.info("  trainee card : %s", state.card_id)
        log.info("  scenario     : %s", state.scenario_id)
        log.info("  turn         : %s", state.turn)
        log.info("  playing_state: %s", state.playing_state)
        if state.simulation_done:
            log.info("  simulation   : already stopped - ready to finish "
                     "(run `aic end`)")
        if state.finish_at is not None:
            log.info("  total run    : %s", format_duration(state.duration))
            log.info("  finishes in  : %s", format_duration(state.seconds_remaining))
            log.info("  finishes at  : %s (local)",
                     time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(state.finish_at)))
            log.info("  clock source : %s", state.finish_source)
            log.info("  server clock : %+ds via %s",
                     session.clock.offset, session.clock.calibration)
        else:
            log.info("  finishes in  : no completion clock (turn-based career)")
        return 0

    if cmd == "fan-cards":
        from . import fanbonus, supportdata
        from .gamedata import load as load_gamedata

        session = flow.connect(settings, refresh_ticket=refresh_ticket,
                               steam_code=args.steam_code)
        account = read_account(session.load_index)
        gamedata = load_gamedata(str(settings.icarus_root))
        try:
            support = supportdata.load(settings)
        except supportdata.SupportDataUnavailable as exc:
            log.error("%s", exc)
            return 1

        ranked = fanbonus.rank_owned(account, gamedata, support,
                                     trainee_card_id=args.trainee)
        if not args.all:
            ranked = [c for c in ranked if c.fan_pct > 0]
        gamedata.warn_about_unknown()

        if args.json:
            print(json.dumps([{
                "card_id": c.card_id, "name": c.name, "fan_bonus_pct": c.fan_pct,
                "level": c.level, "limit_break": c.limit_break_count,
                "eligible": c.eligible, "skip_reason": c.skip_reason,
            } for c in ranked], indent=2))
            return 0

        rarity = {3: "SSR", 2: "SR", 1: "R", 0: "?"}
        if not ranked:
            log.info("No owned support cards with a fan bonus were found.")
            return 0
        log.info("Owned support cards by Fan Bonus (level + uncap applied):")
        log.info("  %-4s %-6s %-3s %-3s  %-28s %s",
                 "fan%", "rarity", "lv", "LB", "card", "note")
        for card in ranked:
            note = "" if card.eligible else f"— {card.skip_reason}"
            log.info("  %4.1f %-6s %3d %2d★  %-28s %s",
                     card.fan_pct, rarity.get(card.rarity_rank, "?"),
                     card.level, card.limit_break_count, card.name[:28], note)

        top = fanbonus.top_eligible(ranked)
        if top:
            total = sum(c.fan_pct for c in top)
            log.info("")
            log.info("Best %d deck-eligible fan-bonus cards (the 6th slot is a "
                     "borrowed friend card):", len(top))
            for card in top:
                log.info("  %4.1f%%  %s (lv%d, %d★)",
                         card.fan_pct, card.name, card.level, card.limit_break_count)
            log.info("  combined fan bonus from these %d: %.1f%%", len(top), total)
        return 0

    if cmd == "wait":
        session = flow.connect(settings, refresh_ticket=refresh_ticket, steam_code=args.steam_code)
        state = flow.inspect_career(session, spec)
        if not state.active:
            log.info("No career in progress; nothing to wait for.")
            return 0
        flow.wait_for_finish(session, spec, state, timeout=args.timeout)
        return 0

    if cmd == "end":
        session = flow.connect(settings, refresh_ticket=refresh_ticket, steam_code=args.steam_code)
        state = flow.inspect_career(session, spec)
        if not state.active:
            log.info("No career in progress; nothing to end.")
            return 0
        flow.end_running_career(session, state, spec, allow_force=not args.no_force)
        return 0

    if cmd == "plan":
        session = flow.connect(settings, refresh_ticket=refresh_ticket, steam_code=args.steam_code)
        selection, account = flow.build_selection(
            session, settings, trainee_card_id=args.trainee, deck_id=args.deck,
            running_style=args.running_style, forced_parents=args.parents or "",
            succession_point=args.succession_point,
            no_friend_card=args.no_friend_card,
            auto_deck=args.auto_deck, build_deck_slot=args.build_deck_slot,
            seed=args.seed,
        )
        print(json.dumps({
            "card_id": selection.card_id,
            "support_card_ids": selection.support_card_ids,
            "friend_viewer_id": selection.friend_viewer_id,
            "friend_card_id": selection.friend_card_id,
            "parent_id_1": selection.parent_id_1,
            "parent_id_2": selection.parent_id_2,
            "succession_rank_point": selection.succession_rank_point,
            "deck_id": selection.deck_id,
            "deck_source": selection.deck_source,
            "running_style": selection.running_style,
            "scenarios_available": account.available_scenario_ids,
        }, indent=2))
        return 0

    if cmd == "schedule":
        session = flow.connect(settings, refresh_ticket=refresh_ticket, steam_code=args.steam_code)
        account = read_account(session.load_index)
        scenario = flow.pick_scenario(account, args.scenario)
        pre = independent.pre_start(session, spec, scenario)
        rules = None
        if not args.no_races:
            try:
                rules = racedata.load(settings)
            except racedata.RaceDataUnavailable as exc:
                log.warning("%s", exc)
        schedule = independent.build_schedule(
            pre, _read_schedule(args.schedule), args.race_agenda, args.no_races,
            rules=rules, chara_id=args.trainee // 100,
            fill_years=args.fill_races, race_goal=args.race_goal,
            extra_races=parse_races(args.races),
        )
        log.info("Source: %s", schedule.source)
        for deck in pre.reserved_race_decks:
            log.info("  agenda %s %r: %s races", deck.get("deck_num"),
                     deck.get("deck_name"), len(deck.get("race_array") or []))
        print(json.dumps(schedule.to_payload(), indent=2))
        return 0

    if cmd == "start":
        session = flow.connect(settings, refresh_ticket=refresh_ticket, steam_code=args.steam_code)
        state = flow.inspect_career(session, spec)
        if state.active:
            log.error("A career is already running. End it first (aic end).")
            return 1
        selection, account = flow.build_selection(
            session, settings, trainee_card_id=args.trainee, deck_id=args.deck,
            running_style=args.running_style, forced_parents=args.parents or "",
            succession_point=args.succession_point,
            no_friend_card=args.no_friend_card,
            auto_deck=args.auto_deck, build_deck_slot=args.build_deck_slot,
            seed=args.seed,
        )
        # Prefer a scenario this account has actually run an idle career in.
        # Without this, an account with scenario 4 unlocked defaults to it -
        # and scenario 4 is not startable in idle mode, so the start 205s for a
        # reason that looks like the succession value. The loop already passes
        # this; the bare `start` command must too.
        scenario = flow.pick_scenario(
            account, args.scenario,
            known=independent.load_known_scenario(settings))
        try:
            flow.start_independent(session, spec, selection, account, scenario,
                                   schedule_override=_read_schedule(args.schedule),
                                   race_deck_num=args.race_agenda,
                                   use_tp=args.use_tp, settings=settings,
                                   sweep=_sweep_enabled(args), no_races=args.no_races,
                                   recover_tp=args.recover_tp,
                                   tp_jewels=args.recover_tp_jewels,
                                   fill_races=args.fill_races,
                                   race_goal=args.race_goal,
                                   extra_races=parse_races(args.races))
        except (fancap.FanCapReached, flow.StartDeferred) as later:
            # "Not yet" is a SUCCESS, not a failure - so it is caught here and
            # returns 0, rather than being added to main()'s error tuple (which
            # returns 1 and would make systemd call a healthy wait a fault).
            log.info("%s", later)
        return 0

    if cmd == "run":
        session = flow.connect(settings, refresh_ticket=refresh_ticket, steam_code=args.steam_code)
        override = _read_schedule(args.schedule)
        cycles = 0
        while True:
            cycles += 1
            log.info("=== cycle %s ===", cycles)
            try:
                result = flow.run_cycle(
                    settings, spec=spec, trainee_card_id=args.trainee, deck_id=args.deck,
                    seed=args.seed, wait_timeout=args.timeout,
                    allow_force_end=not args.no_force, dry_run=args.dry_run,
                    schedule_override=override, scenario_id=args.scenario,
                    race_deck_num=args.race_agenda, running_style=args.running_style,
                    use_tp=args.use_tp, sweep=_sweep_enabled(args),
                    no_races=args.no_races, forced_parents=args.parents or "",
                    succession_point=args.succession_point,
                    no_friend_card=args.no_friend_card,
                    recover_tp=args.recover_tp,
                    tp_jewels=args.recover_tp_jewels,
                    race_goal=args.race_goal,
                    fill_races=args.fill_races,
                    extra_races=parse_races(args.races),
                    auto_deck=args.auto_deck, build_deck_slot=args.build_deck_slot,
                    session=session,
                )
            except (fancap.FanCapReached, flow.StartDeferred) as later:
                # Either the day's target is met, or a resource is missing. Both
                # mean "come back later", and neither is a reason to exit: this
                # runs under Restart=on-failure with StartLimitBurst=3, so
                # exiting on a wait would kill the unit inside five minutes.
                # Any career that was running is already ended and banked; only
                # the next START is blocked.
                log.info("%s", later)
                if not args.loop:
                    return 0
                wait = max(60.0, float(later.seconds))
                # "Next attempt in" is matched by health_alert.ANNOUNCED_WAIT, so
                # a long deliberate sleep does not read as a wedged farmer.
                log.info("Next attempt in %s.", format_duration(wait))
                time.sleep(wait + 60)
                session.refresh_home()
                continue
            for note in result.notes:
                log.warning("note: %s", note)
            if not args.loop or args.dry_run:
                return 0
            if args.max_cycles and cycles >= args.max_cycles:
                log.info("Reached --max-cycles=%s.", args.max_cycles)
                return 0

    if cmd == "discover":
        session = flow.connect(settings, refresh_ticket=refresh_ticket, steam_code=args.steam_code)
        report = independent.discover(session, spec)
        settings.capture_dir.mkdir(parents=True, exist_ok=True)
        out = settings.capture_dir / f"discover_{int(time.time())}.json"
        with open(out, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2, default=str)
        log.info("Discovery report written to %s", out)
        log.info("idle unlock date : %s", report.get("idle_open_date"))
        log.info("server clock     : %+ds (derived=%s)",
                 report.get("server_clock_offset"), report.get("server_clock_derived"))
        info = report.get("idle_single_mode_load_info")
        log.info("idle_single_mode_load_info: %s",
                 "absent/None (no idle run)" if not info else sorted(info))
        for endpoint, res in (report.get("endpoint_probes") or {}).items():
            log.info("  probe %-42s %s", endpoint,
                     f"ok, keys={res.get('keys')}" if res.get("ok")
                     else f"rc={res.get('result_code')}")
        return 0

    log.error("unknown command %r", cmd)
    return 2


def _sweep_enabled(args) -> bool:
    """Searching is opt-in now that the rank point is computed exactly.

    An explicit ``--succession-point`` is authoritative, so never search around
    that either.
    """
    return bool(getattr(args, "sweep", False)) and not getattr(args, "succession_point", 0)


def parse_races(spec: str) -> list[dict]:
    """``"2:1023,3:4"`` -> ``[{"year": 2, "program_id": 1023}, ...]``.

    A bare program id is accepted and left for the race rules to place, which
    they can do whenever the program is legal in exactly one year.
    """
    races: list[dict] = []
    for part in (spec or "").replace(" ", "").split(","):
        if not part:
            continue
        year, sep, program = part.partition(":")
        try:
            if sep:
                races.append({"year": int(year), "program_id": int(program)})
            else:
                races.append({"year": 0, "program_id": int(year)})
        except ValueError:
            raise ConfigError(
                f"--races entry {part!r} is not 'year:program_id' or a program id"
            ) from None
    return races


def _read_schedule(path: Path | None):
    if not path:
        return None
    with open(path, "r", encoding="utf-8-sig") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must contain a JSON object")
    return data
