"""Offline checks for the health alert's log reading. No network, no systemd.

    python tests/test_health_alert.py

The branch worth guarding is the ANNOUNCED-WAIT one: AIC legitimately says nothing
for up to a whole game-day while the fan cap waits out the reset, and for a full
50-minute career on a single-account rotation. A check that called those a wedge
would fire so often that nobody would read the one that mattered.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("aic_health", ROOT / "scripts" / "health_alert.py")
health = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(health)

FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label} {detail}")
        FAILURES.append(label)


def write_log(tmp: str, lines: list[str], age_seconds: float = 0.0) -> Path:
    path = Path(tmp) / "aic.log"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if age_seconds:
        old = time.time() - age_seconds
        os.utime(path, (old, old))
    return path


STALE = 75 * 60

FRESH_RUN = [
    "[2026-09-01 10:00:00] INFO    aic.cli: === cycle 3 ===",
    "[2026-09-01 10:00:04] INFO    aic.flow: Idle career finishes in 49m 12s (total run 50m).",
]


def test_missing_and_fresh() -> None:
    print("the easy states")
    with tempfile.TemporaryDirectory() as tmp:
        facts, problems = health.check_log(Path(tmp) / "nope.log", STALE)
        check("an absent log is a fault", len(problems) == 1 and "no log at" in problems[0])

    with tempfile.TemporaryDirectory() as tmp:
        path = write_log(tmp, FRESH_RUN)
        facts, problems = health.check_log(path, STALE)
        check("a log written just now is healthy", problems == [], str(problems))
        check("and the last line is reported as a fact",
              "Idle career finishes" in facts.get("last line", ""))


def test_announced_waits() -> None:
    print("announced waits are not wedges")
    cases = [
        ("an idle career's own poll", "[..] INFO aic.flow:   12m 4s remaining; next check in 5m 0s."),
        ("a rotation slot", "[..] INFO aic.rotate: Next visit in 49m 30s: McKing."),
        ("the fan cap", "[..] INFO aic.cli: Sleeping until the reset."),
        ("the fan cap message itself",
         "[..] INFO aic.rotate: fan cap reached for 111: +33,100,000 today. Resets in 6.2 h."),
    ]
    for label, line in cases:
        with tempfile.TemporaryDirectory() as tmp:
            path = write_log(tmp, FRESH_RUN + [line], age_seconds=2 * 3600)
            _, problems = health.check_log(path, STALE)
            check(f"{label} is silence it announced", problems == [], str(problems))


def test_wedges() -> None:
    print("wedges")
    with tempfile.TemporaryDirectory() as tmp:
        path = write_log(tmp, FRESH_RUN, age_seconds=2 * 3600)
        _, problems = health.check_log(path, STALE)
        check("stale on a line that announced nothing is a wedge",
              len(problems) == 1 and "not a wait it announced" in problems[0], str(problems))

    with tempfile.TemporaryDirectory() as tmp:
        # An announced wait is still bounded: the longest legitimate one is the
        # fan cap's single game-day.
        path = write_log(tmp, FRESH_RUN + ["[..] INFO aic.cli: Sleeping until the reset."],
                         age_seconds=30 * 3600)
        _, problems = health.check_log(path, STALE)
        check("an announced wait past a whole game-day is a wedge",
              len(problems) == 1 and "longest legitimate wait" in problems[0], str(problems))

    with tempfile.TemporaryDirectory() as tmp:
        path = write_log(tmp, FRESH_RUN + [
            "[2026-09-01 10:05:00] ERROR   aic.rotate: Visit to McKing failed: API error 709 on load/index",
        ])
        facts, problems = health.check_log(path, STALE)
        check("a trailing error with nothing after it is a fault", len(problems) == 1)
        check("and the alert carries what to do about 709",
              "Data Transfer" in problems[0], problems[0][-200:] if problems else "")
        check("the error line is a fact too", "709" in facts.get("last error", ""))


def test_only_the_real_pre_sleep_lines_count() -> None:
    print("only lines that ACTUALLY precede a sleep count as announced")
    # rotate.run logs "Retrying X in Y" and "X next due in Y" and then keeps
    # going - it always logs "Next visit in ..." immediately before it sleeps. So
    # a log that STOPS on one of the first two means the loop died there, and
    # matching them would report a dead rotation as healthy.
    for label, line in [
        ("the retry notice", "[..] INFO aic.rotate: Retrying McKing in 5m 0s."),
        ("the next-due notice", "[..] INFO aic.rotate: McKing next due in 49m 30s."),
    ]:
        with tempfile.TemporaryDirectory() as tmp:
            path = write_log(tmp, FRESH_RUN + [line], age_seconds=2 * 3600)
            _, problems = health.check_log(path, STALE)
            check(f"stopping on {label} is a wedge, not a wait",
                  len(problems) == 1, str(problems))


def test_error_then_recovery() -> None:
    print("a recovered error is history, not a fault")
    with tempfile.TemporaryDirectory() as tmp:
        path = write_log(tmp, [
            "[..] ERROR   aic.rotate: Visit to McKing failed: API error 391 on load/index",
            "[..] INFO    aic.rotate: === visit 12 | cycle 3 | Silence ===",
            "[..] INFO    aic.flow: New career finishes in 50m 0s.",
        ])
        _, problems = health.check_log(path, STALE)
        check("progress after the error clears it", problems == [], str(problems))

    with tempfile.TemporaryDirectory() as tmp:
        path = write_log(tmp, [
            "[..] INFO    aic.rotate: === visit 12 | cycle 3 | Silence ===",
            "[..] ERROR   aic.rotate: Visit to Silence failed: API error 205 on start",
            "[..] ERROR   aic.rotate: Continuing with the next account.",
        ])
        _, problems = health.check_log(path, STALE)
        check("an error after the last progress is a fault", len(problems) == 1, str(problems))


def test_a_handled_refusal_is_not_a_wedge() -> None:
    print("a refusal the loop already handled is not a wedge")
    # The live shape on 2026-09-01: the start is refused, the explanation is
    # logged, and the loop parks itself. It is ALIVE and waiting - reporting that
    # every 30 minutes forever is how an alert becomes noise.
    with tempfile.TemporaryDirectory() as tmp:
        path = write_log(tmp, [
            "[..] INFO    aic: === cycle 1 ===",
            "[..] ERROR   aic.independent: the career start was refused (2509).",
            "[..] ERROR   aic.independent: Searching the rank point is off by default.",
            "[..] INFO    aic: idle start refused with 2509 and this account has 0 clocks.",
            "[..] INFO    aic: Next attempt in 1h 0m 0s.",
        ])
        _, problems = health.check_log(path, STALE)
        check("parked after a handled refusal is healthy", problems == [], str(problems))

    with tempfile.TemporaryDirectory() as tmp:
        # But an error the loop did NOT get past still counts.
        path = write_log(tmp, [
            "[..] INFO    aic: === cycle 1 ===",
            "[..] ERROR   aic.auth: login failed: API error 709 on load/index",
        ])
        _, problems = health.check_log(path, STALE)
        check("an error with nothing after it is still a wedge",
              len(problems) == 1, str(problems))


def test_tail_is_bounded() -> None:
    print("tail reading")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "aic.log"
        filler = ["[..] INFO aic.flow: padding %05d" % i for i in range(40_000)]
        path.write_text("\n".join(filler + FRESH_RUN) + "\n", encoding="utf-8")
        check("the log is bigger than the tail window",
              path.stat().st_size > health.TAIL_BYTES, str(path.stat().st_size))
        lines = health.tail(path)
        check("tail reads only the end", len("\n".join(lines)) <= health.TAIL_BYTES)
        check("and still sees the real last line",
              "Idle career finishes" in lines[-1], lines[-1])
        check("no half line survives the cut", lines[0].startswith("[..]"), lines[0])


def test_a_unit_left_off_is_not_a_fault() -> None:
    print("a deliberately-off unit is not alerted on")
    calls = {}

    def fake(verb, unit):
        return calls.get(verb, "")

    saved = health._systemctl
    health._systemctl = fake
    try:
        calls.update({"is-enabled": "not-found", "is-active": "inactive"})
        state, problem = health.check_unit("aic")
        check("a unit that is not installed is not a fault", problem is None, str(problem))
        check("but the state says so plainly", "deliberately off" in state, state)

        calls.update({"is-enabled": "disabled", "is-active": "inactive"})
        check("a disabled unit is not a fault either",
              health.check_unit("aic")[1] is None)

        calls.update({"is-enabled": "masked", "is-active": "inactive"})
        check("nor a masked one", health.check_unit("aic")[1] is None)

        # The one that MUST still alert: enabled, so somebody meant it to run.
        calls.update({"is-enabled": "enabled", "is-active": "failed"})
        state, problem = health.check_unit("aic")
        check("an ENABLED unit that is not active IS a fault",
              problem is not None and "not active" in problem, str(problem))

        calls.update({"is-enabled": "enabled", "is-active": "active"})
        check("and a healthy one is silent", health.check_unit("aic")[1] is None)

        calls.update({"is-enabled": "disabled", "is-active": "active"})
        state, problem = health.check_unit("aic")
        check("a running-but-disabled unit still gets its log checked",
              problem is None and state.startswith("active"), state)
    finally:
        health._systemctl = saved


def _stamped(when, text):
    return f"[{when.strftime('%Y-%m-%d %H:%M:%S')},000] INFO    aic.rotate: {text}"


def _write_log(tmp, lines):
    path = Path(tmp) / "aic.log"
    path.write_text('\n'.join(lines) + '\n', encoding="utf-8")
    return path


def test_productivity() -> None:
    print("looking busy is not the same as working")
    from datetime import datetime, timedelta
    now = datetime.now()

    def spread(hours, texts, tmp):
        """`texts` repeated across `hours` of history, oldest first."""
        out = []
        for i in range(int(hours * 2)):
            when = now - timedelta(hours=hours) + timedelta(hours=i / 2.0)
            out += [_stamped(when, t) for t in texts]
        return _write_log(tmp, out)

    # THE OVERNIGHT SHAPE: busy, producing nothing, explaining nothing.
    with tempfile.TemporaryDirectory() as tmp:
        path = spread(6, ["=== visit 1 | Oguri_alt ===",
                          "Visit to Oguri_alt failed: API error 391 on tool/signup"], tmp)
        facts, problems = health.check_productivity(path, 3.0)
        check("a busy log that starts NO careers is a fault",
              len(problems) == 1, str(problems)[:110])
        check("the alert says so plainly",
              bool(problems) and "no career has started" in problems[0])
        check("facts show zero started",
              facts.get("careers started", "").startswith("0"), str(facts))

    # THE CURRENT STATE: also zero careers - but every cycle explained itself.
    with tempfile.TemporaryDirectory() as tmp:
        path = spread(6, ["=== visit 1 | Oguri_alt ===",
                          "idle start refused with 2509 and this account has 0 clocks",
                          "Next visit in 24m 59s: Maruzensky_alt."], tmp)
        facts, problems = health.check_productivity(path, 3.0)
        check("zero careers WITH a reason is healthy", problems == [], str(problems)[:110])
        check("and the parked count is reported",
              facts.get("parked (explained)", 0) > 0, str(facts))

    # Working normally.
    with tempfile.TemporaryDirectory() as tmp:
        path = spread(6, ["=== visit 1 ===", "New career finishes in 50m 0s."], tmp)
        _, problems = health.check_productivity(path, 3.0)
        check("careers starting is healthy", problems == [], str(problems)[:110])

    # A couple of blips must not alert - the backoff already absorbs those.
    with tempfile.TemporaryDirectory() as tmp:
        lines = [_stamped(now - timedelta(hours=5), "=== visit 1 ===")]
        lines += [_stamped(now - timedelta(minutes=m), "Visit to X failed: boom")
                  for m in (10, 30)]
        path = _write_log(tmp, lines)
        _, problems = health.check_productivity(path, 3.0)
        check("two failures is a blip, not a wedge", problems == [], str(problems)[:110])

    # Not enough history: must NOT alert right after a restart or log rotation.
    with tempfile.TemporaryDirectory() as tmp:
        path = _write_log(tmp, [_stamped(now - timedelta(minutes=m),
                                         "Visit to X failed: boom")
                                for m in range(10)])
        facts, problems = health.check_productivity(path, 3.0)
        check("a fresh log is never judged", problems == [], str(problems)[:110])
        check("and says why", "not enough history" in facts.get("productivity", ""))

    with tempfile.TemporaryDirectory() as tmp:
        path = spread(6, ["Visit to X failed: boom"], tmp)
        _, problems = health.check_productivity(path, 0)
        check("--productive-hours 0 disables it", problems == [])


def test_a_working_farm_is_never_wedged() -> None:
    """THE 2026-09-03 false alarm: 'careers started 70 in 3h' AND 'WEDGED'.

    The 30-minute check landed between a login's tolerated error and the next
    progress line. The trailing-error rule fired even though the same report
    said 70 careers had started. Those cannot both be true.
    """
    print("a farm that is starting careers is not wedged")
    import datetime as _dt

    now = _dt.datetime.now()

    def stamp(minutes_ago, text):
        t = now - _dt.timedelta(minutes=minutes_ago)
        return f"[{t:%Y-%m-%d %H:%M:%S},000] {text}"

    # The real shape: a career started recently, then a login whose last line is
    # the tolerated story_event error.
    lines = [
        stamp(40, "INFO    aic.independent: Independent training started - finishes in 49m 59s."),
        stamp(20, "INFO    aic.independent: Independent training started - finishes in 49m 59s."),
        stamp(1, "INFO    aic.auth: Logging in..."),
        stamp(0, "ERROR   icarus.client: You asked to start a career with Event "
                 "Boost, but there's no story event running right now, so the "
                 "boost can't be applied. (code 9001)"),
    ]
    with tempfile.TemporaryDirectory() as tmp:
        path = write_log(tmp, lines)
        starts = health.recent_starts(path, 3.0)
        check("it counts the recent starts", starts == 2, str(starts))

        _, problems = health.check_log(path, STALE, starts=starts)
        check("so a trailing error is NOT reported as a wedge",
              problems == [], str(problems))

        # The old behaviour, for contrast: with no evidence of starts it still
        # reports, because then the trailing error IS the only signal there is.
        _, problems = health.check_log(path, STALE, starts=0)
        check("with zero starts it still reports the trailing error",
              len(problems) == 1, str(problems))

    with tempfile.TemporaryDirectory() as tmp:
        # A genuinely dead farm: errors, and nothing started.
        path = write_log(tmp, [
            stamp(30, "ERROR   aic.rotate: Visit to Silence failed: API error 391 on load/index"),
            stamp(0, "ERROR   aic.rotate: Continuing with the next account."),
        ])
        check("no starts means no veto", health.recent_starts(path, 3.0) == 0)
        _, problems = health.check_log(path, STALE,
                                       starts=health.recent_starts(path, 3.0))
        check("and a dead farm is still caught", len(problems) == 1, str(problems))


def main() -> int:
    for fn in (test_missing_and_fresh, test_announced_waits, test_wedges,
               test_only_the_real_pre_sleep_lines_count,
               test_error_then_recovery, test_a_handled_refusal_is_not_a_wedge,
               test_tail_is_bounded,
               test_a_unit_left_off_is_not_a_fault, test_productivity,
               test_a_working_farm_is_never_wedged):
        fn()
        print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
        return 1
    print("all health-alert checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
