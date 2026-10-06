"""Post a finished career to Discord. Stdlib only, and never fatal.

Icarus has this (``career_bot/core/notify.py``); AIC never did - a finished idle
career only ever reached the log. On a VPS with no UI that means the one number
worth watching, fans banked, is invisible unless you go and read a file.

Two rules, both load-bearing:

* **A webhook failure must never end a career run.** The career is already
  finished and banked by the time this is called; there is nothing to roll back,
  so every failure here is logged and swallowed. A farm that dies because
  Discord was slow is worse than one that posts nothing.
* **The URL is a secret.** Anyone holding it can post to that channel, so it
  comes from the environment (``AIC_CAREER_WEBHOOK``), never from a checked-in
  file, and is never logged - not even on failure.

Off unless the variable is set, so nothing changes for an unconfigured run:

    Environment=AIC_CAREER_WEBHOOK=https://discord.com/api/webhooks/...
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from .logging_setup import get

log = get("notify")

TIMEOUT = 10.0

#: Discord sits behind Cloudflare, which BLOCKS the default Python urllib
#: User-Agent outright: HTTP 403, error code 1010. MEASURED 2026-09-01 - the same
#: POST returns 204 with any explicit UA. Icarus' notify._post has always set one;
#: this is why. Without it every webhook fails, silently and forever.
USER_AGENT = "aic-career-notify/1.0"

#: Discord rejects an embed with more than 25 fields, and truncates long values.
_MAX_FIELD = 1000

#: Rows in the per-account breakdown. A roster row is ~34 characters, so 20 sits
#: safely inside _MAX_FIELD with the header and the "+N more" line. A trimmed
#: list SAYS it was trimmed (see :func:`_breakdown_block`) - a silent cut reads
#: as "that is the whole roster" when it is not.
_MAX_ROWS = 20


def _breakdown_block(breakdown, highlight: int = 0) -> str:
    """Render ``[(name, viewer_id, fans)]`` as one aligned code block.

    A code block rather than plain lines because Discord renders embed values as
    markdown with a proportional font: without it the numbers do not line up and
    the whole point of a per-account list - comparing them at a glance - is lost.
    """
    rows = [(str(name or f"Trainer {viewer}"), int(viewer), int(fans))
            for name, viewer, fans in breakdown]
    if not rows:
        return ""

    shown, dropped = rows[:_MAX_ROWS], rows[_MAX_ROWS:]
    width = max(len(name) for name, _, _ in shown)
    lines = []
    for name, viewer, fans in shown:
        # The account this post is about is marked, so a per-account list of a
        # dozen rows still answers "which one just finished?" at a glance.
        mark = ">" if highlight and viewer == highlight else " "
        lines.append(f"{mark} {name:<{width}}  {fans:>11,}")
    if dropped:
        lines.append(f"  ... {len(dropped)} more account(s), "
                     f"{sum(f for _, _, f in dropped):,} fans")
    return "```\n" + "\n".join(lines) + "\n```"


def webhook() -> str:
    return (os.environ.get("AIC_CAREER_WEBHOOK") or "").strip()


def _rank_colour(rank_score) -> int:
    """Green for a strong retirement, amber mid, grey when we cannot tell."""
    try:
        score = int(rank_score or 0)
    except (TypeError, ValueError):
        return 0x9E9E9E
    if score >= 12000:
        return 0x43B581
    if score >= 8000:
        return 0xFAA61A
    return 0x9E9E9E


def build_embed(summary: dict, *, account: str = "", trainee: str = "",
                fans_today: int | None = None, roster_fans: int | None = None,
                roster_accounts: int = 0,
                breakdown: list | None = None, viewer_id: int = 0,
                scenario: int = 0, forced: bool = False,
                races: int = 0, ace: bool = False) -> dict:
    fans = summary.get("fans")
    factors = summary.get("factors") or []
    fields = []

    def add(name, value, inline=True):
        if value in (None, "", []):
            return
        fields.append({"name": name, "value": str(value)[:_MAX_FIELD],
                       "inline": inline})

    add("Fans banked", f"{int(fans):,}" if isinstance(fans, int) else fans)
    add("Rank", summary.get("rank"))
    add("Score", summary.get("rank_score"))
    if fans_today is not None:
        add("Fans today", f"{fans_today:,}")
    if roster_fans is not None:
        # The number actually worth watching with a roster this size. Shown
        # whole-width: it is the headline, not another per-account stat.
        across = f" across {roster_accounts} accounts" if roster_accounts else ""
        add(f"Roster today{across}", f"**{roster_fans:,}** fans", inline=False)
    if breakdown:
        # Which accounts the roster total is actually made of. Whole-width for
        # the same reason: it is a table, not a stat.
        add("Fans today, per account",
            _breakdown_block(breakdown, highlight=viewer_id), inline=False)
    add("Races run", races or None)
    add("Scenario", scenario or None)
    add("Factors", len(factors) if factors else None)

    title = "Ace Run finished" if ace else "Career finished"
    if forced:
        # A force-delete keeps no trained character, so it banks nothing. Saying
        # "finished" for that would quietly overstate the day's progress.
        title = "Career force-deleted (nothing kept)"

    return {
        "username": "AIC",
        "embeds": [{
            "title": title,
            "description": " · ".join(x for x in (account, trainee) if x) or None,
            "color": 0x9E9E9E if forced else _rank_colour(summary.get("rank_score")),
            "fields": fields,
        }],
    }


def post(payload: dict, url: str = "") -> bool:
    """POST to the career webhook. Returns success; never raises."""
    url = url or webhook()
    if not url:
        return False
    try:
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "User-Agent": USER_AGENT}, method="POST")
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            code = getattr(resp, "status", None) or resp.getcode()
            return 200 <= int(code) < 300
    except (urllib.error.URLError, OSError, ValueError) as exc:
        # The URL is a secret, so the exception text is NOT logged: urllib puts
        # the full request URL in some error strings.
        log.warning("career webhook POST failed (%s); the career itself is "
                    "unaffected.", type(exc).__name__)
        return False


def career_finished(summary: dict, **kw) -> bool:
    """Announce one finished career. No-op when no webhook is configured."""
    if not webhook():
        return False
    try:
        return post(build_embed(summary, **kw))
    except Exception as exc:  # noqa: BLE001 - never let reporting end a run
        log.warning("could not build the career notification (%s); ignoring.",
                    type(exc).__name__)
        return False
