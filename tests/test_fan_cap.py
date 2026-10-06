"""Offline checks for the daily fan cap. No network, no credentials.

    python tests/test_fan_cap.py

Mirrors Icarus' tests/test_fan_daily_baseline.py, because it guards the same
bug: a mid-day restart that resets the baseline farms the whole target twice.
"""

from __future__ import annotations

import calendar
import os
import pathlib
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aic import fancap  # noqa: E402
from aic.config import Settings  # noqa: E402
from aic.state import read_account  # noqa: E402

FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label} {detail}")
        FAILURES.append(label)


def settings_in(tmp: str) -> Settings:
    root = Path(tmp)
    return Settings(icarus_root=root, profile="test",
                    runtime_dir=root / "runtime", state_dir=root / "state")


def utc(y, mo, d, h, mi=0) -> float:
    return calendar.timegm((y, mo, d, h, mi, 0, 0, 0, 0))


# --------------------------------------------------------------------------- #
# the game day
# --------------------------------------------------------------------------- #

def test_day_key() -> None:
    print("day key (15:00 UTC rollover)")
    check("just before the reset is still the previous day",
          fancap.day_key(utc(2026, 8, 31, 14, 59)) == "2026-08-30")
    check("the reset moment starts the new day",
          fancap.day_key(utc(2026, 8, 31, 15, 0)) == "2026-08-31")
    check("an hour after the reset is the same day",
          fancap.day_key(utc(2026, 8, 31, 16, 0)) == "2026-08-31")
    check("midnight UTC still belongs to the day that started at 15:00",
          fancap.day_key(utc(2026, 9, 1, 0, 30)) == "2026-08-31")
    check("reset-to-reset is exactly one key",
          fancap.day_key(utc(2026, 8, 31, 15, 0))
          == fancap.day_key(utc(2026, 9, 1, 14, 59)))


def test_seconds_to_reset() -> None:
    print("seconds to reset")
    check("one hour before the reset", fancap.seconds_to_reset(utc(2026, 8, 31, 14, 0)) == 3600)
    check("at the reset it is a full day, never zero",
          fancap.seconds_to_reset(utc(2026, 8, 31, 15, 0)) == 86400)
    check("just after the reset rolls to tomorrow's",
          fancap.seconds_to_reset(utc(2026, 8, 31, 15, 1)) == 86340)
    for hour in range(24):
        wait = fancap.seconds_to_reset(utc(2026, 8, 31, hour))
        if not 0 < wait <= 86400:
            check(f"bounded at {hour:02d}:00", False, f"got {wait}")
            return
    check("bounded to (0, 86400] at every hour", True)


# --------------------------------------------------------------------------- #
# the persisted baseline - the restart bug
# --------------------------------------------------------------------------- #

def test_baseline() -> None:
    print("persisted day baseline")
    with tempfile.TemporaryDirectory() as tmp:
        s = settings_in(tmp)
        first = fancap.baseline(s, 111, 500_000_000, "2026-08-31")
        check("first call of the day records the current lifetime total",
              first == 500_000_000, f"got {first}")

        # THE BUG THIS EXISTS FOR: without persistence a restart re-baselines to
        # the total it finds, so the day's 9.7M already gained reads as 0 and the
        # account farms the whole target again.
        restart = fancap.baseline(s, 111, 509_700_000, "2026-08-31")
        check("a restart mid-day reuses today's baseline",
              restart == 500_000_000, f"got {restart}")

        # A new game-day carries the PREVIOUS day's last reading forward as the
        # baseline (not the current total), so fans banked across the 15:00 reset
        # still count today - e.g. a career that started before the reset and is
        # banked just after it. Here 509.7M was yesterday's last reading and
        # today's first reads 533M, so the 23.3M banked across the boundary lands
        # in the new day rather than vanishing into a "fresh" baseline.
        tomorrow = fancap.baseline(s, 111, 533_000_000, "2026-09-01")
        check("a new day carries yesterday's last reading forward (cross-reset fans count)",
              tomorrow == 509_700_000, f"got {tomorrow}")

        check("the store is on disk under state_dir",
              fancap.store_path(s).is_file(), str(fancap.store_path(s)))

    with tempfile.TemporaryDirectory() as tmp:
        s = settings_in(tmp)
        a = fancap.baseline(s, 111, 500_000_000, "2026-08-31")
        b = fancap.baseline(s, 222, 400_000_000, "2026-08-31")
        check("two accounts keep separate baselines", a != b)
        check("and neither disturbs the other",
              fancap.baseline(s, 111, 505_000_000, "2026-08-31") == a == 500_000_000
              and fancap.baseline(s, 222, 402_000_000, "2026-08-31") == b == 400_000_000)

    with tempfile.TemporaryDirectory() as tmp:
        s = settings_in(tmp)
        s.state_dir.mkdir(parents=True, exist_ok=True)
        fancap.store_path(s).write_text("{ not json", encoding="utf-8")
        val = fancap.baseline(s, 111, 500_000_000, "2026-08-31")
        check("a corrupt store starts over rather than raising",
              val == 500_000_000, f"got {val}")


# --------------------------------------------------------------------------- #
# the gate
# --------------------------------------------------------------------------- #

class _Env:
    """Set env vars for a block and put them back afterwards."""

    def __init__(self, **kw):
        self.kw = kw
        self.old: dict[str, str | None] = {}

    def __enter__(self):
        for k, v in self.kw.items():
            self.old[k] = os.environ.get(k)
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = str(v)
        return self

    def __exit__(self, *exc):
        for k, v in self.old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def raises_cap(s, viewer_id, lifetime) -> bool:
    try:
        fancap.check(s, viewer_id, lifetime)
    except fancap.FanCapReached:
        return True
    return False


def test_check() -> None:
    print("the gate")
    with tempfile.TemporaryDirectory() as tmp:
        s = settings_in(tmp)
        with _Env(AIC_FAN_TARGET=None, AIC_FAN_STOP_MARGIN=None):
            check("no AIC_FAN_TARGET means no cap at all",
                  not raises_cap(s, 111, 500_000_000))
            # CONTRACT CHANGE 2026-09-05: this used to assert nothing was
            # written with the cap off. The cap was removed as unreachable, and
            # the per-day tally it shares a module with now feeds the roster
            # total on the Discord card - so the record must be written whether
            # or not a target is set.
            check("but the day tally IS still recorded, for the webhook",
                  fancap.store_path(s).exists()
                  and dict(fancap.day_breakdown(s)).get(111) == 0,
                  str(fancap.day_breakdown(s)))

    with tempfile.TemporaryDirectory() as tmp:
        s = settings_in(tmp)
        with _Env(AIC_FAN_TARGET=33_000_000, AIC_FAN_STOP_MARGIN=1_100_000):
            check("the first cycle of the day is allowed",
                  not raises_cap(s, 111, 500_000_000))
            check("still allowed at +10M", not raises_cap(s, 111, 510_000_000))
            check("still allowed just under target minus margin",
                  not raises_cap(s, 111, 531_800_000))
            check("capped once the margin is reached",
                  raises_cap(s, 111, 531_900_000))
            check("still capped after a restart", raises_cap(s, 111, 531_900_000))
            check("a second account is judged on its own day",
                  not raises_cap(s, 222, 900_000_000))

    with tempfile.TemporaryDirectory() as tmp:
        s = settings_in(tmp)
        with _Env(AIC_FAN_TARGET=33_000_000, AIC_FAN_STOP_MARGIN=0):
            fancap.check(s, 111, 500_000_000)
            check("margin 0 caps exactly at the target",
                  raises_cap(s, 111, 533_000_000)
                  and not raises_cap(s, 111, 532_999_999))

    with tempfile.TemporaryDirectory() as tmp:
        s = settings_in(tmp)
        with _Env(AIC_FAN_TARGET=33_000_000):
            # A reading we do not trust must not stop the farm dead.
            check("an absent/zero fan reading does not cap",
                  not raises_cap(s, 111, 0))
            check("a missing viewer_id does not cap", not raises_cap(s, 0, 500_000_000))

    with tempfile.TemporaryDirectory() as tmp:
        s = settings_in(tmp)
        with _Env(AIC_FAN_TARGET="33,000,000"):
            check("a target written with separators parses", fancap.target() == 33_000_000)
        with _Env(AIC_FAN_TARGET="not a number"):
            check("an unparseable target disables the cap rather than crashing",
                  fancap.target() == 0 and not raises_cap(s, 111, 500_000_000))

    with tempfile.TemporaryDirectory() as tmp:
        s = settings_in(tmp)
        with _Env(AIC_FAN_TARGET=33_000_000, AIC_FAN_STOP_MARGIN=1_100_000):
            try:
                fancap.check(s, 111, 500_000_000)
                fancap.check(s, 111, 540_000_000)
            except fancap.FanCapReached as exc:
                check("the exception carries seconds to the reset",
                      0 < exc.seconds_to_reset <= 86400, str(exc.seconds_to_reset))
                check("and says how much was farmed", "today" in str(exc))
            else:
                check("the exception carries seconds to the reset", False, "did not raise")


# --------------------------------------------------------------------------- #
# the reading the gate depends on
# --------------------------------------------------------------------------- #

def test_margin_cannot_swallow_the_target() -> None:
    print("a margin at or above the target would farm nothing at all")
    with tempfile.TemporaryDirectory() as tmp:
        s = settings_in(tmp)
        # The easy mistake: a 1M target left with the 1.1M default margin. Without
        # the guard, gained >= goal - margin is true at ZERO fans, so every account
        # is capped on every visit and the farm silently never runs.
        with _Env(AIC_FAN_TARGET=1_000_000, AIC_FAN_STOP_MARGIN=None):
            check("the margin is cut below the target",
                  fancap.stop_margin(1_000_000) < 1_000_000,
                  str(fancap.stop_margin(1_000_000)))
            check("so a fresh account is NOT capped before it farms",
                  not raises_cap(s, 111, 500_000_000))
        with _Env(AIC_FAN_TARGET=1_000_000, AIC_FAN_STOP_MARGIN=5_000_000):
            check("an explicitly oversized margin is cut too",
                  fancap.stop_margin(1_000_000) == 100_000,
                  str(fancap.stop_margin(1_000_000)))
        with _Env(AIC_FAN_TARGET=33_000_000, AIC_FAN_STOP_MARGIN=1_100_000):
            check("a sane pair is left alone",
                  fancap.stop_margin(33_000_000) == 1_100_000)


def test_deferral_is_one_concept() -> None:
    print("both 'come back later' causes carry the same handle")
    from aic import flow
    d = flow.StartDeferred("out of clocks", 3600.0)
    check("StartDeferred exposes .seconds", d.seconds == 3600.0)
    check("and is an IdleModeError, so nothing else has to learn a new type",
          isinstance(d, __import__("aic.independent", fromlist=["x"]).IdleModeError))
    c = fancap.FanCapReached("done for today", 7200.0)
    check("FanCapReached exposes .seconds too", c.seconds == 7200.0)
    check("and keeps its original attribute", c.seconds_to_reset == 7200.0)
    check("so one except clause serves both",
          all(hasattr(e, "seconds") for e in (c, d)))


def test_read_account_fans() -> None:
    print("lifetime fans out of load/index")
    acct = read_account({"user_info": {"viewer_id": 123, "fan": 411_796_815}})
    check("user_info.fan lands on AccountState.fans",
          acct.fans == 411_796_815, f"got {acct.fans}")
    check("and the viewer_id still does", acct.viewer_id == 123)
    check("an absent user_info is 0, not a crash", read_account({}).fans == 0)
    check("a string fan count is tolerated",
          read_account({"user_info": {"fan": "1234"}}).fans == 1234)


def test_day_total() -> None:
    """The webhook's roster figure: every account's day, summed."""
    print("roster total for the day")
    with tempfile.TemporaryDirectory() as tmp:
        settings, day = settings_in(tmp), "2026-09-02"
        # Three accounts visited; the third is on YESTERDAY's game-day.
        fancap.baseline(settings, 111, 1_000_000, day)
        fancap.baseline(settings, 111, 1_400_000, day)     # +400,000
        fancap.baseline(settings, 222, 5_000_000, day)
        fancap.baseline(settings, 222, 5_250_000, day)     # +250,000
        fancap.baseline(settings, 333, 9_000_000, "1999-01-01")

        total, seen = fancap.day_total(settings, day)
        check("only today's accounts counted", seen == 2, str(seen))
        check("gains are summed", total == 650_000, str(total))

        # An account seen once has a baseline but no gain yet - it must count as
        # an account with 0, not be skipped, or the card understates the roster.
        fancap.baseline(settings, 444, 7_000_000, day)
        total, seen = fancap.day_total(settings, day)
        check("an account with no gain yet still counts", seen == 3, str(seen))
        check("and adds nothing", total == 650_000, str(total))

    with tempfile.TemporaryDirectory() as tmp:
        check("a missing store reads as zero, not a crash",
              fancap.day_total(settings_in(tmp)) == (0, 0))


def test_baseline_survives_the_latest_field() -> None:
    """Recording `latest` must not move the day's baseline."""
    print("baseline is still the first reading of the day")
    with tempfile.TemporaryDirectory() as tmp:
        settings, day = settings_in(tmp), "2026-09-02"
        first = fancap.baseline(settings, 555, 2_000_000, day)
        for n in (2_100_000, 2_900_000, 3_400_000):
            again = fancap.baseline(settings, 555, n, day)
            check(f"baseline unchanged at {n:,}", again == first, f"{again} != {first}")
        total, _ = fancap.day_total(settings, day)
        check("the gain tracks the newest reading", total == 1_400_000, str(total))


def test_no_target_still_accounts() -> None:
    """The cap is gone; the day tally that feeds the webhook is not.

    AIC_FAN_TARGET was removed 2026-09-05 (unreachable: ~17M/account/game-day
    against a 33M target). check() must still record the baseline and the
    latest reading, because day_breakdown sums those onto the Discord card.
    """
    print("no target: records, never raises")
    with tempfile.TemporaryDirectory() as tmp:
        s_ = settings_in(tmp)
        old = os.environ.pop("AIC_FAN_TARGET", None)
        try:
            fancap.check(s_, 111, 500_000_000)
            fancap.check(s_, 111, 504_000_000)
            fancap.check(s_, 222, 100_000_000)
            check("it never raises with no target", True)
        except fancap.FanCapReached:
            check("it never raises with no target", False, "it capped anyway")
        finally:
            if old is not None:
                os.environ["AIC_FAN_TARGET"] = old

        rows = fancap.day_breakdown(s_)
        by = dict(rows)
        check("the gain is recorded for the webhook",
              by.get(111) == 4_000_000, str(rows))
        check("an account with no gain yet is still counted",
              222 in by and by[222] == 0, str(rows))

        # A garbage reading must not create a bogus record.
        fancap.check(s_, 333, 0)
        check("a zero lifetime total records nothing",
              333 not in dict(fancap.day_breakdown(s_)),
              str(fancap.day_breakdown(s_)))


def test_setting_a_target_re_arms_it() -> None:
    """Removing the cap must not have removed the ability to have one."""
    print("a target still caps when set")
    with tempfile.TemporaryDirectory() as tmp:
        s_ = settings_in(tmp)
        old = os.environ.get("AIC_FAN_TARGET")
        oldm = os.environ.get("AIC_FAN_STOP_MARGIN")
        os.environ["AIC_FAN_TARGET"] = "1000000"
        os.environ["AIC_FAN_STOP_MARGIN"] = "100000"
        try:
            fancap.check(s_, 444, 10_000_000)          # baseline
            try:
                fancap.check(s_, 444, 10_950_000)      # +950k, over 1M - 100k
            except fancap.FanCapReached as exc:
                check("it caps again once a target is set", True)
                check("and says how long until the reset", exc.seconds > 0,
                      str(exc.seconds))
            else:
                check("it caps again once a target is set", False, "no raise")
        finally:
            for k, v in (("AIC_FAN_TARGET", old), ("AIC_FAN_STOP_MARGIN", oldm)):
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


def main() -> int:
    for fn in (test_no_target_still_accounts,
               test_setting_a_target_re_arms_it, test_day_total, test_baseline_survives_the_latest_field,
               test_day_key, test_seconds_to_reset, test_baseline, test_check,
               test_margin_cannot_swallow_the_target, test_deferral_is_one_concept,
               test_read_account_fans):
        fn()
        print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
        return 1
    print("all fan-cap checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
