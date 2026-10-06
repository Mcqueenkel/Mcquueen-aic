"""Offline checks for the career notification. No network.

    python tests/test_notify.py

The rule these guard: a webhook failure must NEVER end a career run. By the time
this code is reached the career is already finished and banked, so there is
nothing to roll back - a farm that dies because Discord was slow is worse than
one that posts nothing.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aic import notify  # noqa: E402

FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label} {detail}")
        FAILURES.append(label)


SUMMARY = {"card_id": 100801, "rank": "A+", "rank_score": 13500,
           "fans": 812345, "factors": [1, 2, 3]}


class _Env:
    def __init__(self, value):
        self.value = value

    def __enter__(self):
        self.old = os.environ.get("AIC_CAREER_WEBHOOK")
        if self.value is None:
            os.environ.pop("AIC_CAREER_WEBHOOK", None)
        else:
            os.environ["AIC_CAREER_WEBHOOK"] = self.value

    def __exit__(self, *exc):
        if self.old is None:
            os.environ.pop("AIC_CAREER_WEBHOOK", None)
        else:
            os.environ["AIC_CAREER_WEBHOOK"] = self.old


def test_off_by_default() -> None:
    print("off unless configured")
    with _Env(None):
        check("no webhook = silent no-op", notify.career_finished(SUMMARY) is False)
        check("post() refuses an empty url", notify.post({"a": 1}, "") is False)


def test_embed() -> None:
    print("the embed")
    e = notify.build_embed(SUMMARY, account="Trainer 319865323652", trainee="Vodka",
                           fans_today=9_700_000, roster_fans=104_500_000,
                           roster_accounts=11, scenario=3)
    emb = e["embeds"][0]
    f = {x["name"]: x["value"] for x in emb["fields"]}
    check("fans are thousands-separated", f.get("Fans banked") == "812,345", str(f))
    check("today's account figure carries no target",
          f.get("Fans today") == "9,700,000", str(f))
    check("the 33M per-account target is gone from the card",
          not any("33,000,000" in str(v) for v in f.values()), str(f))
    check("the roster total is shown, named with the account count",
          f.get("Roster today across 11 accounts") == "**104,500,000** fans", str(f))
    check("the roster total is full width",
          all(not x["inline"] for x in emb["fields"] if x["name"].startswith("Roster")),
          str(emb["fields"]))
    only_one = notify.build_embed(SUMMARY, roster_fans=5, roster_accounts=0)
    check("no account count = no 'across' clause",
          "Roster today" in {x["name"] for x in only_one["embeds"][0]["fields"]},
          str(only_one["embeds"][0]["fields"]))
    check("rank and score carried", f.get("Rank") == "A+" and f.get("Score") == "13500")
    check("account and trainee in the description", "Vodka" in emb["description"])
    check("a strong score is green", emb["color"] == 0x43B581, hex(emb["color"]))

    forced = notify.build_embed(SUMMARY, forced=True)["embeds"][0]
    check("a force-delete says nothing was kept",
          "force-deleted" in forced["title"], forced["title"])
    check("and is not coloured like a success", forced["color"] == 0x9E9E9E)

    empty = notify.build_embed({"fans": None, "rank": None})["embeds"][0]
    check("absent values are dropped, not sent as None", empty["fields"] == [])

    long = notify.build_embed({"fans": 1, "factors": list(range(500))})["embeds"][0]
    check("no field exceeds Discord's length cap",
          all(len(x["value"]) <= 1000 for x in long["fields"]))


def test_failure_is_never_fatal() -> None:
    print("a broken webhook cannot end a run")
    with _Env("http://127.0.0.1:9/nope"):
        check("an unreachable host returns False rather than raising",
              notify.career_finished(SUMMARY) is False)
    with _Env("not-a-url"):
        check("a malformed url returns False rather than raising",
              notify.career_finished(SUMMARY) is False)
    with _Env("http://127.0.0.1:9/nope"):
        check("a summary of the wrong shape is survivable",
              notify.career_finished({"fans": object()}) is False)


def test_user_agent_is_set() -> None:
    print("the header without which Discord silently drops everything")
    import urllib.request
    seen = {}

    def fake(req, timeout=None):
        seen["ua"] = req.get_header("User-agent")
        raise OSError("not sending")

    real = urllib.request.urlopen
    urllib.request.urlopen = fake
    try:
        notify.post({"content": "x"}, "https://discord.com/api/webhooks/1/x")
    finally:
        urllib.request.urlopen = real
    check("an explicit User-Agent is sent", bool(seen.get("ua")), str(seen))
    check("and it is not python's default",
          "python" not in (seen.get("ua") or "").lower(), str(seen.get("ua")))


def main() -> int:
    for fn in (test_off_by_default, test_embed, test_failure_is_never_fatal,
               test_user_agent_is_set):
        fn()
        print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
        return 1
    print("all notification checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
