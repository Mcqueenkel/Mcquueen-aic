"""The webhook reports fans gained TODAY, per account. No network.

    python tests/test_fan_breakdown.py

The per-account numbers were always in the fan-cap store (``baseline`` is where
an account's game-day started, ``latest`` its newest reading) - only the roster
sum was ever reported. The rules that matter:

  * ``day_total`` must stay exactly the sum of ``day_breakdown``, or the headline
    and the table under it would disagree about the same day;
  * a record from a previous game-day must not leak into today (the 15:00 UTC
    rollover is what resets the cap, so a stale row would both overstate the day
    and mis-report the cap);
  * the table must never be silently trimmed - a cut list has to say so.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aic import fancap  # noqa: E402
from aic.notify import _MAX_ROWS, _breakdown_block, build_embed  # noqa: E402

FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label} {detail}")
        FAILURES.append(label)


TODAY = "2026-09-03"
YESTERDAY = "2026-09-02"


def store(records: dict):
    """A settings stub pointing at a temp fan-cap store."""
    tmp = tempfile.mkdtemp()
    settings = SimpleNamespace(state_dir=Path(tmp) / "aic")
    path = fancap.store_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(records), encoding="utf-8")
    return settings


LIVE = {
    # viewer: day, where the day started, newest reading
    "996150905943": {"day": TODAY, "baseline": 35_927_754, "latest": 36_854_163},
    "364872132559": {"day": TODAY, "baseline": 30_000_000, "latest": 30_000_000},
    "768270152387": {"day": TODAY, "baseline": 20_000_000, "latest": 21_500_000},
    "457468658151": {"day": YESTERDAY, "baseline": 1_000_000, "latest": 9_000_000},
}


def test_breakdown_is_per_account_and_ordered() -> None:
    print("one row per account, biggest gain first")
    rows = fancap.day_breakdown(store(LIVE), day=TODAY)
    check("yesterday's row is excluded", len(rows) == 3, str(rows))
    check("biggest first", rows[0] == (768270152387, 1_500_000)
          or rows[0] == (996150905943, 926_409), str(rows[0]))
    check("ordered by gain",
          [g for _, g in rows] == sorted((g for _, g in rows), reverse=True),
          str(rows))
    check("a visited-but-idle account still shows as 0",
          (364872132559, 0) in rows, str(rows))


def test_total_is_exactly_the_sum_of_the_breakdown() -> None:
    print("the headline agrees with the table")
    settings = store(LIVE)
    rows = fancap.day_breakdown(settings, day=TODAY)
    total, seen = fancap.day_total(settings, day=TODAY)
    check("total == sum(rows)", total == sum(g for _, g in rows), f"{total}")
    check("seen == len(rows)", seen == len(rows), f"{seen} vs {len(rows)}")
    check("the real numbers", total == 926_409 + 1_500_000, f"{total}")


def test_fans_banked_across_the_reset_still_count_today() -> None:
    print("a career banked just after the 15:00 reset counts today, not +0")
    # Yesterday this account's last reading was 9,000,000 lifetime. Today's FIRST
    # visit reads 9,800,000 - a career that started before the reset and banked
    # just after it added 800,000. It must show +800,000 today, not +0 (the old
    # bug folded the just-banked career into the new baseline).
    settings = store({"457468658151": {"day": YESTERDAY,
                                        "baseline": 1_000_000, "latest": 9_000_000}})
    start = fancap.baseline(settings, 457468658151, 9_800_000, day=TODAY)
    check("new-day baseline carries yesterday's last reading (not the post-bank total)",
          start == 9_000_000, str(start))
    rows = fancap.day_breakdown(settings, day=TODAY)
    check("the cross-reset career's fans count today", (457468658151, 800_000) in rows, str(rows))
    total, _ = fancap.day_total(settings, day=TODAY)
    check("and land in the roster total", total == 800_000, str(total))


def test_first_ever_visit_starts_at_zero() -> None:
    print("an account with no prior record starts today at +0")
    settings = store({})
    start = fancap.baseline(settings, 111, 5_000_000, day=TODAY)
    check("baseline = current total (no prior day to carry)", start == 5_000_000, str(start))
    check("gained is 0", fancap.day_breakdown(settings, day=TODAY) == [(111, 0)],
          str(fancap.day_breakdown(settings, day=TODAY)))


def test_first_visit_still_counts_the_career_it_banked() -> None:
    print("no prior record, but a career was banked this visit -> it counts today")
    # MEASURED 2026-10-04: after the rebuild every account's first career of the
    # day banked ~1.0M fans and showed +0, because the day started after it.
    settings = store({})
    start = fancap.baseline(settings, 111, 5_000_000, day=TODAY, just_banked=1_081_312)
    check("baseline sits below the banked career", start == 5_000_000 - 1_081_312, str(start))
    check("the banked career is today's gain",
          fancap.day_breakdown(settings, day=TODAY) == [(111, 1_081_312)],
          str(fancap.day_breakdown(settings, day=TODAY)))
    again = fancap.baseline(settings, 111, 6_000_000, day=TODAY, just_banked=999)
    check("later visits the same day keep the first baseline", again == start, str(again))
    # with a previous day's reading, the carry-over wins and just_banked is ignored
    s2 = store({"111": {"day": "2026-09-01", "baseline": 1, "latest": 4_500_000}})
    check("carry-over beats just_banked",
          fancap.baseline(s2, 111, 5_000_000, day=TODAY, just_banked=100) == 4_500_000)
    check("a junk just_banked never goes below 0",
          fancap.baseline(store({}), 222, 50, day=TODAY, just_banked=10**9) == 0)


def test_a_negative_reading_never_subtracts() -> None:
    print("a lifetime count that went backwards")
    # Fan totals only ever rise, so a latest below baseline means a bad reading,
    # not a loss. It must not eat another account's gain in the total.
    settings = store({"1": {"day": TODAY, "baseline": 500, "latest": 100},
                      "2": {"day": TODAY, "baseline": 0, "latest": 900}})
    total, _ = fancap.day_total(settings, day=TODAY)
    check("clamped at zero", total == 900, str(total))


def test_missing_or_junk_store_is_empty_not_fatal() -> None:
    print("no store, junk store")
    missing = SimpleNamespace(state_dir=Path(tempfile.mkdtemp()) / "aic")
    check("missing -> []", fancap.day_breakdown(missing) == [])
    check("missing -> (0, 0)", fancap.day_total(missing) == (0, 0))
    check("junk -> []", fancap.day_breakdown(store([1, 2, 3])) == [])


def test_the_block_is_aligned_and_marks_this_account() -> None:
    print("the rendered table")
    block = _breakdown_block([("McSpade", 996150905943, 926_409),
                              ("McDonald", 768270152387, 1_500_000),
                              ("", 793593252166, 0)],
                             highlight=996150905943)
    check("fenced for a fixed-width font", block.startswith("```"), block[:8])
    check("names present", "McSpade" in block and "McDonald" in block)
    check("thousands separators", "926,409" in block, block)
    lines = [ln for ln in block.splitlines() if ln and not ln.startswith("```")]
    check("this account is marked",
          [ln for ln in lines if ln.startswith(">")][0].find("McSpade") > 0, str(lines))
    check("only this account is marked",
          sum(ln.startswith(">") for ln in lines) == 1, str(lines))
    check("an unnamed account falls back to its id",
          "Trainer 793593252166" in block, block)
    # Right-aligned, so the comma sits wherever the magnitude puts it; what has
    # to line up is the right edge every number ends on.
    check("columns line up", len({len(ln) for ln in lines}) == 1, str(lines))


def test_a_trimmed_table_says_so() -> None:
    print("more accounts than the table holds")
    many = [(f"Acct{i:02d}", 1000 + i, 100) for i in range(_MAX_ROWS + 4)]
    block = _breakdown_block(many)
    check("says how many were dropped", "4 more account(s)" in block, block[-90:])
    check("and their fans", "400 fans" in block, block[-90:])
    check("stays inside the Discord field cap", len(block) <= 1000, str(len(block)))


def test_embed_omits_the_table_when_there_is_nothing_to_show() -> None:
    print("the embed itself")
    summary = {"fans": 926409, "rank": 9, "rank_score": 4265}
    bare = build_embed(summary, account="McSpade")
    names = [f["name"] for f in bare["embeds"][0]["fields"]]
    check("no empty table field", "Fans today, per account" not in names, str(names))

    full = build_embed(summary, account="McSpade", roster_fans=2_426_409,
                       roster_accounts=3, viewer_id=996150905943,
                       breakdown=[("McSpade", 996150905943, 926_409),
                                  ("McDonald", 768270152387, 1_500_000)])
    fields = {f["name"]: f["value"] for f in full["embeds"][0]["fields"]}
    table = [k for k in fields if k.startswith("Fans today, per")]
    check("table present when given", len(table) == 1, str(list(fields)))
    check("account named, not numbered",
          full["embeds"][0]["description"] == "McSpade",
          str(full["embeds"][0]["description"]))
    check("every field inside Discord's cap",
          all(len(v) <= 1024 for v in fields.values()),
          str({k: len(v) for k, v in fields.items()}))
    check("under Discord's 25-field limit",
          len(full["embeds"][0]["fields"]) <= 25)


def main() -> int:
    for fn in (test_breakdown_is_per_account_and_ordered,
               test_total_is_exactly_the_sum_of_the_breakdown,
               test_fans_banked_across_the_reset_still_count_today,
               test_first_ever_visit_starts_at_zero,
               test_first_visit_still_counts_the_career_it_banked,
               test_a_negative_reading_never_subtracts,
               test_missing_or_junk_store_is_empty_not_fatal,
               test_the_block_is_aligned_and_marks_this_account,
               test_a_trimmed_table_says_so,
               test_embed_omits_the_table_when_there_is_nothing_to_show):
        fn()
        print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
        return 1
    print("all fan-breakdown checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
