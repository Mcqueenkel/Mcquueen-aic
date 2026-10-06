"""``rotate.bat`` - run a whole roster of accounts on one seamless loop."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .cli import parse_races
from .config import ConfigError, load_settings
from .farm import STRESS_MAX_COUNT as _STRESS_CAP
from .logging_setup import setup


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rotate",
        description="Farm a roster of accounts, spacing visits so every idle "
                    "career finishes just as its account comes round again.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Build a roster, then farm it:\n"
            "  rotate.bat banners                            which gacha ids exist\n"
            "  rotate.bat farm --banner 30111 --count 20 --pulls 3\n"
            "  rotate.bat init                               or: from saved logins\n"
            "  rotate.bat plan                               show the schedule\n"
            "  rotate.bat run                                farm careers forever\n"
            "\n"
            "The 50/N spacing is only the OPENING stagger. After the first visit\n"
            "each account is scheduled from its own recorded career clock, so a\n"
            "short career comes back early and a restart resumes mid-schedule.\n"
        ),
    )
    parser.add_argument("-p", "--profile", help="runtime profile (default: default)")
    parser.add_argument("--icarus-root", help="path to the Icarus-Dev-Build checkout")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--steam-code", default="", metavar="CODE",
                        help="Steam Guard code, if one is needed to start the session")
    parser.add_argument("--roster", type=Path, metavar="FILE",
                        help="roster file (default: reroll_accounts.json)")
    parser.add_argument("--career-minutes", type=float, default=0,
                        help="career length used to space visits (default: 50)")
    parser.add_argument("--no-delay", action="store_true",
                        help="remove EVERY delay between requests, including the Icarus client's per-request floor. An experiment: the edge (Akamai) tends to answer 403 at machine speed")

    sub = parser.add_subparsers(dest="command", required=True)

    # -v also works after the subcommand (the more natural place to type it),
    # not just before it. SUPPRESS means "not given here" leaves whatever the
    # top-level parser already set, rather than clobbering True back to False.
    def add_verbose(sub_parser: argparse.ArgumentParser) -> None:
        sub_parser.add_argument("-v", "--verbose", action="store_true",
                                default=argparse.SUPPRESS)
        sub_parser.add_argument("--no-delay", action="store_true",
                                default=argparse.SUPPRESS)

    p_init = sub.add_parser("init", help="write a roster from saved Link Passwords")
    p_init.add_argument("--force", action="store_true", help="overwrite an existing roster")
    add_verbose(p_init)

    p_farm = sub.add_parser(
        "farm",
        help="create accounts, pull a fixed number of times, add them to the roster")
    p_farm.add_argument("--banner", type=int, required=True,
                        help="gacha_id to pull on (see `rotate.bat banners`)")
    p_farm.add_argument("--count", type=int, default=20,
                        help="how many accounts to create (default: 20)")
    p_farm.add_argument("--max-pulls", "--pulls", type=int, default=3,
                        dest="max_pulls", metavar="N",
                        help="upper bound on ten-pulls per account (default: 3). "
                             "Stops early when the carats run out, so this is a "
                             "ceiling, not a quota")
    p_farm.add_argument("--free-only", action="store_true",
                        help="only take pulls the banner gives away (campaign or "
                             "stock); never spend carats")
    p_farm.add_argument("--password", default="ChangeMe2026",
                        help="Link Password set on each account (8-16, upper+lower+digit)")
    p_farm.add_argument("--name-prefix", default="farm",
                        help="label accounts <prefix>01, <prefix>02, ...")
    p_farm.add_argument("--pace", type=float, default=1.0,
                        help="scale the gaps between pulls and between accounts "
                             "(1.0 = ~2s between pulls). Lower is faster and easier "
                             "to detect; 0 removes them entirely")
    p_farm.add_argument("--start-career", action="store_true",
                        help="stress test: also start an Independent career on "
                             "each new account after its pulls (capped at "
                             "%d accounts/run)" % _STRESS_CAP)
    p_farm.add_argument("--trainee", type=int, default=0, metavar="CARD_ID",
                        help="trainee card id for the started career (default: auto)")
    p_farm.add_argument("--auto-deck", action="store_true",
                        help="build the career deck from highest Fan Bonus cards")
    p_farm.add_argument("--build-deck-slot", type=int, default=10, metavar="N",
                        help="deck slot the auto-built deck is written to (default 10)")
    p_farm.add_argument("--scenario", type=int, default=0,
                        help="scenario id for the started career (default: highest)")
    p_farm.add_argument("--race-goal", type=int, default=0, metavar="N",
                        help="schedule about N races in the started career")
    p_farm.add_argument("--running-style", type=int, default=1, choices=(1, 2, 3, 4),
                        help="1 front, 2 pace, 3 late, 4 end (default 1)")
    add_verbose(p_farm)

    p_banners = sub.add_parser("banners", help="list the gacha ids currently on offer")
    add_verbose(p_banners)

    p_plan = sub.add_parser("plan", help="print the schedule and exit")
    p_plan.add_argument("--only", metavar="ACCOUNT",
                        help="show the recorded clock for one roster entry")
    add_verbose(p_plan)

    p_run = sub.add_parser("run", help="run the rotation")
    p_run.add_argument("--cycles", type=int, default=0,
                       help="stop after N full passes (0 = forever)")
    p_run.add_argument("--start-at", type=int, default=0, metavar="N",
                       help="open the stagger at roster position N (1-based) "
                            "instead of the first account, for a first run where "
                            "the accounts before N already have careers going. "
                            "Only affects accounts with no career on record - a "
                            "recorded one is due when its own career says so")
    p_run.add_argument("--dry-run", action="store_true",
                       help="validate the roster and print the plan, change nothing")
    p_run.add_argument("--only", metavar="ACCOUNT",
                       help="loop a single roster account by name or trainer id "
                            "instead of the whole roster. No other accounts are "
                            "touched; each visit comes back when that career's "
                            "own clock says it has ended")
    p_run.add_argument("--trainee", type=int, default=0, metavar="CARD_ID")
    p_run.add_argument("--deck", type=int, default=0, metavar="DECK_ID")
    p_run.add_argument("--scenario", type=int, default=0)
    p_run.add_argument("--race-agenda", type=int, default=0, metavar="N")
    p_run.add_argument("--race-goal", type=int, default=0, metavar="N",
                       help="schedule about N races per career (35 is a good "
                            "target). Goal races and --races picks are always "
                            "kept; the rest are filled with the highest-fan "
                            "races the trainee can enter")
    p_run.add_argument("--races", metavar="SPEC",
                       help="races to add on top of the goal races, as "
                            "'year:program_id' separated by commas, e.g. "
                            "'2:1023,3:4'. Never replaces a goal race")
    p_run.add_argument("--running-style", type=int, default=1, choices=(1, 2, 3, 4))
    p_run.add_argument("--auto-deck", action="store_true",
                       help="build the highest-fan-bonus legal deck from the "
                            "cards the account owns, save it to --build-deck-slot "
                            "and run the career on it. Re-evaluated every career, "
                            "so cards levelling up are picked up on their own. "
                            "Your hand-made decks are never touched.")
    p_run.add_argument("--build-deck-slot", type=int, default=10, metavar="N",
                       help="deck slot (1-10) the auto-built deck is written to "
                            "(default: 10, which is normally unused - do not "
                            "point this at a deck you curated by hand)")
    p_run.add_argument("--use-tp", type=int, default=0, metavar="N")
    p_run.add_argument("--recover-tp-jewels", action="store_true",
                           help="when the TP potions run out, spend CARATS to finish the "
                                "top-up (user/recovery_trainer_point). Off by default: this "
                                "loop runs unattended and carats cannot be re-earned by "
                                "waiting. AIC_TP_JEWEL_FLOOR (default 5000) is never spent "
                                "below")
    p_run.add_argument("--recover-tp", action="store_true",
                       help="spend TP potions before a start when TP is short of "
                            "the full 30 cost, so a run is never skipped for "
                            "energy. Needed when looping one account, which comes "
                            "round faster than TP regenerates")
    p_run.add_argument("--seed", type=int, default=None)
    # Off by default - the rank point is computed exactly; see aic/cli.py.
    p_run.add_argument("--sweep", action="store_true",
                       help="search for a succession rank point on refusal "
                            "(off by default; debugging aid)")
    p_run.add_argument("--no-sweep", action="store_true",
                       help=argparse.SUPPRESS)
    add_verbose(p_run)

    return parser


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

    from . import rotate
    from .account import AccountError
    from .auth import AuthError
    from .farm import FarmError
    from .independent import load_spec
    from .rotate import RotationError
    # bind() rather than icarus(): the bootstrap binds LAZILY (flow.connect is
    # what normally triggers it), so icarus() here raises "bind() has not been
    # called yet". bind() is idempotent and this is the point the module
    # docstring says it belongs - before anything touches the wire.
    from .bootstrap import bind
    ApiError = bind(settings).ApiError

    roster_path = args.roster or rotate.DEFAULT_ROSTER

    try:
        if args.command == "init":
            count = rotate.write_roster_from_saved(settings, roster_path,
                                                   overwrite=args.force)
            log.info("Wrote %s with %s account(s).", roster_path, count)
            log.info("Check it, then:  rotate.bat plan")
            return 0

        if args.command == "banners":
            from . import flow

            session = flow.connect(settings, steam_code=args.steam_code)
            data = session.client.call("gacha/index", {}).get("data") or {}
            rows = data.get("gacha_info_list") or []
            if not rows:
                log.info("No banners returned.")
                return 0
            log.info("%-10s %-8s %-10s %s", "GACHA ID", "TYPE", "FREE", "STOCK")
            for row in rows:
                log.info("%-10s %-8s %-10s %s", row.get("id"), row.get("gacha_type"),
                         "yes" if int(row.get("is_campaign_draw_enable_multi") or 0)
                         else "-", row.get("remain_stock_num_multi"))
            log.info("")
            log.info("Use one with:  rotate.bat farm --banner <id> --count 20 --pulls 3")
            return 0

        if args.command == "farm":
            from .farm import farm

            made = farm(settings, banner=args.banner, count=args.count,
                        max_pulls=args.max_pulls, password=args.password,
                        roster_path=roster_path, pace_scale=args.pace,
                        name_prefix=args.name_prefix, free_only=args.free_only,
                        start_career=args.start_career,
                        career_opts={
                            "trainee": args.trainee,
                            "auto_deck": args.auto_deck,
                            "build_deck_slot": args.build_deck_slot,
                            "scenario": args.scenario,
                            "race_goal": args.race_goal,
                            "running_style": args.running_style,
                        })
            if not made:
                log.error("No accounts were kept.")
                return 1
            log.info("Next:  rotate.bat plan")
            return 0

        accounts, options = rotate.load_roster(roster_path, settings)
        minutes = (args.career_minutes or options.get("career_minutes")
                   or rotate.CAREER_MINUTES)

        only = getattr(args, "only", "")
        if only:
            accounts = [rotate.pick_account(accounts, only)]

        if args.command == "plan":
            if only:
                log.info("Looping just %s.", accounts[0].label)
                for line in rotate.describe_plan(accounts, minutes * 60,
                                                 minutes, settings):
                    log.info("%s", line)
                return 0
            interval = rotate.interval_for(accounts, minutes)
            for line in rotate.describe_plan(accounts, interval, minutes, settings):
                log.info("%s", line)
            return 0

        if args.command == "run":
            state = rotate.run(
                settings, load_spec(), accounts,
                career_minutes=minutes, cycles=args.cycles, dry_run=args.dry_run,
                start_at=args.start_at,
                trainee_card_id=args.trainee, deck_id=args.deck,
                scenario_id=args.scenario, race_deck_num=args.race_agenda,
                running_style=args.running_style, use_tp=args.use_tp,
                sweep=bool(args.sweep), seed=args.seed,
                race_goal=args.race_goal,
                extra_races=parse_races(args.races),
                recover_tp=args.recover_tp,
                tp_jewels=args.recover_tp_jewels,
                auto_deck=args.auto_deck,
                build_deck_slot=args.build_deck_slot,
            )
            log.info("")
            log.info("%s visit(s), %s career(s) started, %s failure(s).",
                     state.visits, state.started_careers, state.failures)
            return 1 if state.failures and not state.started_careers else 0

    except (RotationError, FarmError, AccountError, AuthError, ConfigError,
            ApiError) as exc:
        log.error("%s", exc)
        return 1
    except KeyboardInterrupt:
        log.warning("Stopped. Careers already started keep running server-side.")
        return 130

    log.error("unknown command %r", args.command)
    return 2
