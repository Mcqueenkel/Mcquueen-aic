"""
Renew the Steam session ticket without opening the game or entering 2FA.

Requires a prior login to have stored a refresh token in auth_config.json
(steam_refresh_token field). First-time setup still needs the game capture:
    python tools/capture_auth.py

Usage:
    python tools/renew_ticket.py
    python tools/renew_ticket.py --profile default
"""

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from uma_api import auth
from tools.steam_session import SteamSession


def main():
    ap = argparse.ArgumentParser(description="Renew Steam session ticket using the stored refresh token.")
    ap.add_argument("--profile", default="default")
    ap.add_argument("--ticket-age", type=int, default=auth.DEFAULT_MAX_TICKET_AGE,
                    help="Only renew if the current ticket is older than this many seconds.")
    args = ap.parse_args()

    cfg = auth.load_auth(args.profile)
    if not cfg:
        print("[-] No auth_config found. Run capture_auth.py first.")
        return 1

    age = auth.ticket_age(cfg)
    if age < args.ticket_age:
        print(f"[*] Ticket is still fresh ({int(age)}s old, limit {args.ticket_age}s). Nothing to do.")
        return 0

    refresh_token = cfg.get("steam_refresh_token", "")
    if not refresh_token:
        print("[-] No refresh token stored. A full login is needed (run capture_auth.py).")
        return 1

    username = cfg.get("steam_username", "")
    print(f"[*] Renewing ticket for Steam account: {username or '(unknown)'}")

    try:
        with SteamSession(username="", password="", refresh_token=refresh_token) as session:
            steam_id, ticket = session.gen_ticket()
            new_refresh = session.refresh_token
    except Exception as e:
        print(f"[-] Failed to renew ticket: {e}")
        return 1

    cfg["steam_id"] = steam_id
    cfg["steam_session_ticket"] = ticket
    cfg["ticket_ts"] = time.time()
    if new_refresh:
        cfg["steam_refresh_token"] = new_refresh

    auth.save_auth(args.profile, cfg)
    print(f"[OK] Ticket renewed. steam_id={steam_id}, ticket length={len(ticket)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
