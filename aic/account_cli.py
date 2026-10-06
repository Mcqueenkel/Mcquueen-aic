"""``account.bat`` - switch which game account this profile is bound to."""

from __future__ import annotations

import argparse
import sys

from .config import ConfigError, load_settings
from .logging_setup import setup


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="account",
        description="Log in to / out of Umamusume accounts on this Steam slot.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Typical rotation:\n"
            '  account.bat remember "103 444 444 441" MyPass2026   once per account\n'
            "  account.bat logout\n"
            '  account.bat login "103 444 444 441" MyPass2026\n'
            "\n"
            "`login` records the password it used, so every account you switch\n"
            "into is on record from then on and `logout` runs unattended.\n"
        ),
    )
    parser.add_argument("-p", "--profile", help="runtime profile (default: default)")
    parser.add_argument("--icarus-root", help="path to the Icarus-Dev-Build checkout")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--steam-code", default="", metavar="CODE",
                        help="Steam Guard code, to mint a ticket when Guard is blocking")
    parser.add_argument("--no-delay", action="store_true",
                        help="remove EVERY delay between requests, including the Icarus client's per-request floor. An experiment: the edge (Akamai) tends to answer 403 at machine speed")

    sub = parser.add_subparsers(dest="command", required=True)

    # -v also works after the subcommand (the more natural place to type it),
    # not just before it. SUPPRESS means "not given here" leaves whatever the
    # top-level parser already set, rather than clobbering True back to False.
    def add_verbose(p: argparse.ArgumentParser) -> None:
        p.add_argument("-v", "--verbose", action="store_true", default=argparse.SUPPRESS)
        p.add_argument("--no-delay", action="store_true", default=argparse.SUPPRESS)

    p_status = sub.add_parser("status", help="show the account this profile is bound to")
    add_verbose(p_status)
    p_list = sub.add_parser("list", help="list Trainer IDs with a saved Link Password")
    add_verbose(p_list)

    p_save = sub.add_parser("save-link",
                            help="publish a Data Link so this account can be recovered")
    p_save.add_argument("password", help="Link Password (8-16, upper+lower+digit)")
    add_verbose(p_save)

    p_rem = sub.add_parser(
        "remember",
        help="record a Trainer ID + Link Password you already know (no server call)")
    p_rem.add_argument("trainer_id", help='Trainer ID, e.g. "103 444 444 441"')
    p_rem.add_argument("password", help="its Link Password")
    p_rem.add_argument("--name", default="", help="optional label")
    add_verbose(p_rem)

    p_out = sub.add_parser(
        "logout",
        help="unlink this account from Steam, freeing the slot (DESTRUCTIVE)")
    p_out.add_argument("--link-password", metavar="PW",
                       help="publish this Data Link first, then unlink")
    p_out.add_argument("--yes", action="store_true",
                       help="skip the confirmation prompt asked for an account with "
                            "no Data Link on record")
    p_out.add_argument("--force", action="store_true",
                       help="unlink even with no Data Link on record. The account "
                            "becomes permanently unrecoverable")
    add_verbose(p_out)

    p_sec = sub.add_parser(
        "steam-secret",
        help="store the Steam 2FA shared_secret so logins never need a code")
    p_sec.add_argument("shared_secret", nargs="?",
                       help="base64 shared_secret from your Steam maFile "
                            "(omit to just show whether one is stored)")
    p_sec.add_argument("--clear", action="store_true", help="remove the stored secret")
    add_verbose(p_sec)

    p_cap = sub.add_parser(
        "capture",
        help="capture credentials from the running game with frida "
             "(Windows, or Linux via Proton - see aic/proton.py)")
    p_cap.add_argument("--timeout", type=int, default=180,
                       help="seconds to wait for the game and its first request")
    p_cap.add_argument("--no-launch", action="store_true",
                       help="do not start the game; attach to one already running")
    p_cap.add_argument("--kill-after", action="store_true",
                       help="close the game when done. Note Steam then cancels the "
                            "captured ticket, so a later command must mint a new one")
    add_verbose(p_cap)

    p_imp = sub.add_parser(
        "import-capture",
        help="repair auth_config from a captured account switch (no frida needed)")
    p_imp.add_argument("dump_dir", nargs="?",
                       help="UmaDumpy dump directory (default: the newest one)")
    add_verbose(p_imp)

    p_in = sub.add_parser("login", help="take over an account with Trainer ID + Link Password")
    p_in.add_argument("trainer_id", help='Trainer ID, e.g. "103 444 444 441"')
    p_in.add_argument("password", help="Link Password for that account")
    add_verbose(p_in)

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

    from . import account as acct
    from . import flow
    from .account import AccountError
    from .auth import AuthError

    try:
        return _dispatch(args, settings, log, acct, flow)
    except (AccountError, AuthError, ConfigError) as exc:
        log.error("%s", exc)
        return 1
    except KeyboardInterrupt:
        log.warning("Interrupted.")
        return 130


def _dispatch(args, settings, log, acct, flow) -> int:
    cmd = args.command

    if cmd == "list":
        rows = acct.load_accounts(settings)
        if not rows:
            log.info("No saved Link Passwords yet (state/accounts.json).")
            return 0
        log.info("%-18s %-22s %-10s %s", "TRAINER ID", "NAME", "LINK", "SAVED")
        for row in rows:
            log.info("%-18s %-22s %-10s %s", row.get("trainer_id"),
                     (row.get("name") or "")[:22],
                     "yes" if row.get("link_set") else "NO",
                     row.get("saved_at", ""))
        return 0

    if cmd == "status":
        session = flow.connect(settings, steam_code=getattr(args, "steam_code", ""))
        vid = session.viewer_id
        saved = acct.find_account(settings, vid)
        log.info("Bound to Trainer ID %s (viewer %s)", acct.format_trainer_id(vid), vid)
        log.info("Data Link on record: %s",
                 f"yes, saved {saved.get('saved_at')}" if saved
                 else "NO - `account.bat save-link <password>` before any logout")
        return 0

    if cmd == "capture":
        from .capture import CaptureError, capture

        acct.archive_auth_config(settings, note="before a frida capture")
        try:
            cfg = capture(settings, timeout=args.timeout,
                          launch=not args.no_launch, kill_after=args.kill_after)
        except CaptureError as exc:
            log.error("%s", exc)
            return 1

        vid = cfg.get("viewer_id")
        log.info("")
        log.info("auth_config updated - Trainer ID %s", acct.format_trainer_id(vid))
        log.info("Verifying by logging in...")
        session = flow.connect(settings, steam_code=getattr(args, "steam_code", ""))
        log.info("Login OK (viewer %s).", session.viewer_id)
        if not acct.find_account(settings, session.viewer_id):
            log.warning("No Link Password on record for this account. Record it "
                        "before any logout:  account.bat remember \"%s\" <password>",
                        acct.format_trainer_id(session.viewer_id))
        return 0

    if cmd == "import-capture":
        from pathlib import Path

        if args.dump_dir:
            dump_dir = Path(args.dump_dir).expanduser()
        else:
            root = acct.DEFAULT_DUMP_ROOT
            runs = sorted((p for p in root.glob("*") if p.is_dir()),
                          key=lambda p: p.stat().st_mtime, reverse=True)
            if not runs:
                log.error("No dumps found under %s - pass a directory explicitly.", root)
                return 1
            dump_dir = runs[0]
            log.info("Using the newest dump: %s", dump_dir)

        viewer_id, auth_b64, name = acct.credentials_from_capture(dump_dir)
        acct.archive_auth_config(settings, note=f"before importing viewer {viewer_id}")
        acct.import_credentials(settings, viewer_id, auth_b64, name)

        log.info("Verifying by logging in...")
        session = flow.connect(settings, steam_code=getattr(args, "steam_code", ""))
        log.info("Login OK - bound to Trainer ID %s.",
                 acct.format_trainer_id(session.viewer_id))
        if not acct.find_account(settings, session.viewer_id):
            log.warning("No Link Password on record for this account. Record it "
                        "before any logout:  account.bat remember \"%s\" <password>",
                        acct.format_trainer_id(session.viewer_id))
        return 0

    if cmd == "steam-secret":
        from .auth import load_auth_config, save_auth_config

        cfg = load_auth_config(settings)
        if args.clear:
            cfg["steam_shared_secret"] = ""
            save_auth_config(settings, cfg)
            log.info("Stored Steam shared secret cleared.")
            return 0
        if not args.shared_secret:
            have = bool(cfg.get("steam_shared_secret"))
            log.info("Steam shared secret: %s",
                     "stored - logins generate their own 2FA codes" if have
                     else "NOT stored - Steam Guard will interrupt repeated logins")
            if not have:
                log.info("")
                log.info("Find it in your Steam Desktop Authenticator maFile as")
                log.info('  "shared_secret": "...="   then:')
                log.info("  account.bat steam-secret <that value>")
            return 0

        cfg["steam_shared_secret"] = args.shared_secret
        save_auth_config(settings, cfg)
        log.info("Stored. Steam logins will now generate their own 2FA codes, so "
                 "account switching can run unattended.")
        log.info("Verifying by minting a ticket...")
        from . import bootstrap
        from .auth import refresh_steam_ticket

        # Nothing has logged in on this path, so the Icarus layer (which owns
        # the ticket generator) has not been imported yet.
        bootstrap.bind(settings)

        if refresh_steam_ticket(cfg, settings, force=True):
            save_auth_config(settings, cfg)
            log.info("Ticket minted with the stored secret - it works.")
            return 0
        log.error("Could not mint a ticket with that secret. Check it is the "
                  "shared_secret (not identity_secret) and matches this account.")
        return 1

    if cmd == "remember":
        trainer_id = acct.parse_trainer_id(args.trainer_id)
        acct.validate_password(args.password)
        acct.remember_account(settings, trainer_id, args.password, True, args.name)
        log.info("Recorded Trainer ID %s. `logout` will no longer block on it, and "
                 "`login` can restore it.", acct.format_trainer_id(trainer_id))
        log.warning("Not verified against the server - if this password is wrong "
                    "the account is not actually recoverable. Use `save-link` to "
                    "publish a Data Link you know is good.")
        return 0

    if cmd == "save-link":
        session = flow.connect(settings, steam_code=getattr(args, "steam_code", ""))
        acct.validate_password(args.password)
        ok = acct.publish_link(session, args.password)
        acct.remember_account(settings, session.viewer_id, args.password, ok)
        if not ok:
            log.error("Data Link NOT set - do not unlink this account.")
            return 1
        log.info("")
        log.info("  Trainer ID : %s", acct.format_trainer_id(session.viewer_id))
        log.info("  Password   : %s", args.password)
        log.info("  Recover with: account.bat login \"%s\" %s",
                 acct.format_trainer_id(session.viewer_id), args.password)
        return 0

    if cmd == "logout":
        session = flow.connect(settings, steam_code=getattr(args, "steam_code", ""))
        vid = session.viewer_id
        log.info("Currently bound to Trainer ID %s.", acct.format_trainer_id(vid))

        known = acct.find_account(settings, vid)

        if args.link_password:
            acct.validate_password(args.link_password)
            log.info("Publishing a Data Link before unlinking...")
            if not acct.publish_link(session, args.link_password):
                log.error("Refusing to unlink: the Data Link was not set.")
                return 1
            acct.remember_account(settings, vid, args.link_password, True)
        elif known:
            # Recoverable, so unlinking is reversible - no prompt. This is the
            # normal case in a rotation: `login` records the password it used,
            # so every account switched into is already on record.
            log.info("Data Link on record (saved %s) - this account can be "
                     "restored with `account.bat login`.", known.get("saved_at"))
        elif args.force:
            log.warning("No Data Link on record and --force given: Trainer ID %s "
                        "will be PERMANENTLY unrecoverable.",
                        acct.format_trainer_id(vid))
            if not args.yes and not _confirm(vid, acct, log):
                log.info("Aborted; nothing was changed.")
                return 1
        else:
            log.error(
                "No Data Link on record for Trainer ID %s.\n"
                "  account/steam_chain_disconnect DELETES the Steam<->account link. "
                "Without a\n"
                "  Trainer ID + Link Password this account becomes unrecoverable by "
                "anyone.\n"
                "  If it already has a Link Password, just record it:\n"
                "      account.bat remember \"%s\" <password>\n"
                "  To create one now:   account.bat logout --link-password MyPass2026\n"
                "  To accept the loss:  account.bat logout --force",
                acct.format_trainer_id(vid), acct.format_trainer_id(vid))
            return 1

        acct.archive_auth_config(settings, note=f"before unlinking viewer {vid}")
        acct.steam_disconnect(session)
        log.info("Unlinked. The Steam slot is now free.")
        log.info("Log in to another account with:  account.bat login \"<trainer id>\" <password>")
        return 0

    if cmd == "login":
        trainer_id = acct.parse_trainer_id(args.trainer_id)

        # A takeover creates a throwaway account on the Steam slot first, which
        # the server refuses (102) while the slot is still occupied. Check now
        # rather than three calls in.
        occupied = _current_viewer(settings, flow, log, args.steam_code)
        if occupied:
            if occupied == trainer_id:
                log.error(
                    "Already bound to Trainer ID %s - nothing to do.\n"
                    "  To re-take it, unlink first:  account.bat logout",
                    acct.format_trainer_id(trainer_id))
                return 1
            log.error(
                "The Steam slot still holds Trainer ID %s.\n"
                "  A takeover needs a free slot. Unlink first, then log in:\n"
                "      account.bat logout\n"
                "      account.bat login \"%s\" <password>",
                acct.format_trainer_id(occupied), args.trainer_id)
            return 1

        log.info("Taking over Trainer ID %s ...", acct.format_trainer_id(trainer_id))
        acct.archive_auth_config(settings, note=f"before switching to {trainer_id}")

        result = acct.login_with_link(settings, trainer_id, args.password)

        log.info("")
        log.info("Now bound to %s (Trainer ID %s).",
                 result.name or "?", acct.format_trainer_id(result.viewer_id))
        log.info("Verifying by logging in...")
        from .independent import load_spec

        session = flow.connect(settings, steam_code=getattr(args, "steam_code", ""))
        state = flow.inspect_career(session, load_spec())
        log.info("Login OK. Career in progress: %s",
                 f"{state.kind} (turn {state.turn})" if state.active else "none")
        return 0

    log.error("unknown command %r", cmd)
    return 2


def _current_viewer(settings, flow, log, steam_code: str = "") -> int:
    """Viewer id currently bound to this Steam slot, or 0 if the slot is free.

    A failed login is the expected state right after a logout, so it is treated
    as "free" rather than an error. Programming errors are re-raised rather than
    swallowed - a NameError hiding here made this always report "free".
    """
    try:
        return int(flow.connect(settings, steam_code=steam_code).viewer_id or 0)
    except (NameError, AttributeError, TypeError):
        raise
    except Exception as exc:  # noqa: BLE001
        log.debug("no usable session (%s: %s) - treating the slot as free",
                  type(exc).__name__, exc)
        return 0


def _confirm(viewer_id, acct, log) -> bool:
    """Interactive confirmation; declines when stdin is not a terminal."""
    if not sys.stdin or not sys.stdin.isatty():
        log.error("Refusing to unlink without --yes when stdin is not a terminal.")
        return False
    prompt = (f"\nType the Trainer ID to confirm unlinking "
              f"({acct.format_trainer_id(viewer_id)}): ")
    try:
        typed = input(prompt)
    except EOFError:
        return False
    return acct.parse_trainer_id(typed or "0") == int(viewer_id)
