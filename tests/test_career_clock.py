"""The rotation must schedule from each career's own clock, not a fixed slot.

    python tests/test_career_clock.py

No network: ``visit`` and the account calls are stubbed, and ``time.sleep`` is
replaced with a fake clock so a 50-minute career takes no wall time.

The defect these guard: the old scheduler visited on blind ``career_minutes / N``
slots and threw away the real finish time the server had already given it. An
early arrival found a career still running, did nothing, and did not come back
for a FULL LAP - so the account sat finished and idle for up to a whole career,
every lap, forever.
"""

from __future__ import annotations

import sys
import os
import tempfile
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aic import careerclock, rotate  # noqa: E402
from aic.config import Settings  # noqa: E402

FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label} {detail}")
        FAILURES.append(label)


class FakeClock:
    """A clock that only moves when something sleeps."""

    def __init__(self, start: float = 1_756_700_000.0):
        self.now = start

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += max(0.0, seconds)


class Harness:
    """Runs rotate.run against scripted careers, recording the visit order."""

    def __init__(self, tmp: str, careers: dict, roster: list[tuple[int, str]]):
        #: viewer_id -> seconds the career has left when visited. A callable gets
        #: (visit_number_for_this_account) so a script can vary per visit.
        self.careers = careers
        self.accounts = [rotate.RosterAccount(vid, "pw", name) for vid, name in roster]
        self.settings = Settings(icarus_root=Path(tmp), profile="t",
                                 runtime_dir=Path(tmp) / "rt",
                                 state_dir=Path(tmp) / "state")
        self.settings.state_dir.mkdir(parents=True, exist_ok=True)
        self.clock = FakeClock()
        self.order: list[tuple[float, str]] = []
        self.counts: dict[int, int] = {vid: 0 for vid, _ in roster}

    def _visit(self, settings, spec, target, **kw):
        self.counts[target.trainer_id] += 1
        self.order.append((self.clock.now, target.label))
        script = self.careers[target.trainer_id]
        remaining = script(self.counts[target.trainer_id]) if callable(script) else script
        r = rotate.VisitResult(account=target, ok=True)
        if remaining is None:                      # the visit failed
            r.ok = False
            r.source = "failed"
            return r
        r.started = True
        r.source = "started"
        r.remaining_seconds = float(remaining)
        r.started_at = self.clock.now
        r.finishes_at = self.clock.now + float(remaining)
        return r

    def run(self, cycles: int, career_minutes: float = 50.0, jitter=(0.0, 0.0), **kw):
        # The clock tests check exact timing, so the random visit wait is off
        # unless a test asks for it (test_random_wait_between_visits).
        saved_jitter, rotate.VISIT_JITTER_SECONDS = rotate.VISIT_JITTER_SECONDS, jitter
        try:
            return self._run(cycles, career_minutes, **kw)
        finally:
            rotate.VISIT_JITTER_SECONDS = saved_jitter

    def _run(self, cycles: int, career_minutes: float = 50.0, **kw):
        saved = (rotate.time, rotate.acct, rotate.visit, careerclock.time)
        rotate.time = types.SimpleNamespace(
            time=self.clock.time, sleep=self.clock.sleep,
            strftime=lambda f, t=None: "--:--:--", localtime=lambda t=None: None)
        careerclock.time = types.SimpleNamespace(time=self.clock.time)
        rotate.acct = types.SimpleNamespace(
            find_account=lambda s, v: {"trainer_id": str(v)},
            remember_account=lambda *a, **k: None,
            format_trainer_id=lambda v: str(v))
        rotate.visit = self._visit
        try:
            return rotate.run(self.settings, None, self.accounts,
                              career_minutes=career_minutes, cycles=cycles, **kw)
        finally:
            rotate.time, rotate.acct, rotate.visit, careerclock.time = saved


def labels(harness) -> list[str]:
    return [name for _, name in harness.order]


# --------------------------------------------------------------------------- #

def test_even_roster_still_round_robins() -> None:
    print("an on-schedule roster behaves exactly as the fixed slots did")
    with tempfile.TemporaryDirectory() as tmp:
        # 5 accounts, every career exactly 50 min: the old fixed slots were right
        # here, and the new scheduler must not change the behaviour.
        h = Harness(tmp, {i: 3000.0 for i in range(1, 6)},
                    [(i, f"acct{i}") for i in range(1, 6)])
        h.run(cycles=2)
        check("visits go round the roster in order",
              labels(h)[:5] == ["acct1", "acct2", "acct3", "acct4", "acct5"],
              str(labels(h)[:5]))
        check("every account is visited twice",
              sorted(h.counts.values()) == [2, 2, 2, 2, 2], str(h.counts))
        # The opening lap goes round as fast as the anti-hammer floor allows, so
        # every account is farming within minutes of a cold start rather than
        # after a full career_minutes/N sweep. Throughput is unchanged; what
        # changes is how long the roster sits idle at the beginning.
        gaps = [h.order[i + 1][0] - h.order[i][0] for i in range(4)]
        check("the opening lap is paced at MIN_INTERVAL_SECONDS, not 50/N",
              all(abs(g - rotate.MIN_INTERVAL_SECONDS) < 1.0 for g in gaps),
              str(gaps))
        check("so all 5 accounts are started within a few minutes",
              h.order[4][0] - h.order[0][0]
              <= 4 * rotate.MIN_INTERVAL_SECONDS + 1,
              str(h.order[4][0] - h.order[0][0]))
        # ...and then each is revisited on its OWN career clock, together.
        second = [t for t, n in h.order if n == "acct1"]
        check("the second visit comes a full career later",
              len(second) > 1 and 3000.0 <= second[1] - second[0] <= 3100.0,
              str(second))


def test_a_short_career_comes_back_early() -> None:
    print("an account whose career is short is revisited when IT ends")
    with tempfile.TemporaryDirectory() as tmp:
        # acct2's careers run 10 minutes, everyone else's 50. Under fixed slots
        # acct2 would still wait a full lap (50 min) between visits, idling 40.
        h = Harness(tmp, {1: 3000.0, 2: 600.0, 3: 3000.0},
                    [(1, "slow1"), (2, "quick"), (3, "slow3")])
        h.run(cycles=3)
        quick = [t for t, name in h.order if name == "quick"]
        gaps = [quick[i + 1] - quick[i] for i in range(len(quick) - 1)]
        check("the quick account is revisited on ITS clock, not a 50-min lap",
              all(g < 700.0 for g in gaps), str(gaps))
        check("so it gets more visits than the slow ones",
              h.counts[2] > h.counts[1], str(h.counts))


def test_early_arrival_does_not_cost_a_lap() -> None:
    print("arriving early no longer wastes a whole career")
    with tempfile.TemporaryDirectory() as tmp:
        # Visit 1 finds 20 minutes still to run (the schedule slipped). The old
        # loop would not return for a full lap; the new one comes back in 20 min.
        h = Harness(tmp, {1: lambda n: 1200.0 if n == 1 else 3000.0, 2: 3000.0},
                    [(1, "slipped"), (2, "other")])
        h.run(cycles=2)
        slipped = [t for t, name in h.order if name == "slipped"]
        gap = slipped[1] - slipped[0]
        check("it is revisited after its remaining 20 min, not a 50-min lap",
              1200.0 <= gap <= 1300.0, f"gap was {gap}")


def test_clock_is_written_and_resumed() -> None:
    print("the clock is written down and a restart resumes it")
    with tempfile.TemporaryDirectory() as tmp:
        h = Harness(tmp, {1: 3000.0, 2: 3000.0}, [(1, "a"), (2, "b")])
        h.run(cycles=1)

        store = careerclock.read_all(h.settings)
        check("every account has a record", set(store) == {"1", "2"}, str(list(store)))
        rec = store["1"]
        check("the record time-codes when the career started",
              rec.get("career_started_at_utc", "").endswith("Z"),
              str(rec.get("career_started_at_utc")))
        check("and when it finishes",
              rec.get("career_finishes_at_utc", "").endswith("Z"),
              str(rec.get("career_finishes_at_utc")))
        check("and carries the machine-readable due time",
              isinstance(rec.get("due_at"), float), str(rec.get("due_at")))
        check("and says what produced it", rec.get("source") == "started",
              str(rec.get("source")))
        check("duration is the two stamps apart",
              rec.get("duration_seconds") == 3000, str(rec.get("duration_seconds")))

        # A restart 10 minutes later. Neither career has ended yet, so a fresh
        # stagger would visit position 1 IMMEDIATELY (its slot is "now"); the
        # recorded clock says wait. That difference is the whole point.
        resume_at = h.clock.now + 600.0
        # Snapshot the recorded clocks BEFORE the resuming run - it rewrites
        # them as it visits, so reading afterwards measures the wrong thing.
        before = {k: v.get("due_at") for k, v in
                  careerclock.read_all(h.settings).items()}
        h2 = Harness(tmp, {1: 3000.0, 2: 3000.0}, [(1, "a"), (2, "b")])
        h2.clock.now = resume_at
        h2.run(cycles=1)
        first_delay = h2.order[0][0] - resume_at
        check("the restart waits for the recorded career instead of re-staggering",
              first_delay > 0, f"visited after {first_delay}s")
        # Derive the expectation from what was actually RECORDED rather than
        # hardcoding a number: the opening lap's pacing feeds into it, so a
        # literal here goes stale the moment that changes (it did).
        expected = min(before.values()) - resume_at
        check("it waits exactly as long as the soonest recorded clock says",
              abs(first_delay - expected) < 1.0,
              f"waited {first_delay}s, recorded clock wanted {expected}s")
        check("and that is less than a full career",
              first_delay < 3000.0 + careerclock.FINISH_GRACE, f"{first_delay}s")



def test_overdue_is_visited_at_once() -> None:
    print("a restart after the career already ended goes straight in")
    # Its own temp dir: a second run against a shared state dir would REWRITE
    # the records this one is asserting about.
    with tempfile.TemporaryDirectory() as tmp:
        h = Harness(tmp, {1: 3000.0, 2: 3000.0}, [(1, "a"), (2, "b")])
        h.run(cycles=1)
        finished = h.clock.now + 4000.0        # both careers are long over

        h2 = Harness(tmp, {1: 3000.0, 2: 3000.0}, [(1, "a"), (2, "b")])
        h2.clock.now = finished
        h2.run(cycles=1)
        check("the overdue account is visited immediately, not after a fresh lap",
              h2.order[0][0] == finished, f"waited {h2.order[0][0] - finished}s")
        check("and the second one right after it, not a stagger later",
              h2.order[1][0] - h2.order[0][0] < rotate.MIN_INTERVAL_SECONDS + 1,
              f"gap {h2.order[1][0] - h2.order[0][0]}s")


def test_stale_records_are_discarded() -> None:
    print("a record older than a day is not acted on")
    with tempfile.TemporaryDirectory() as tmp:
        s = Settings(icarus_root=Path(tmp), profile="t",
                     runtime_dir=Path(tmp) / "rt", state_dir=Path(tmp) / "state")
        s.state_dir.mkdir(parents=True, exist_ok=True)
        import time as real_time
        careerclock.record(s, 111, remaining_seconds=3000.0,
                           started_at=real_time.time(), source="started")
        check("a fresh record is usable", careerclock.due_at(s, 111) is not None)

        store = careerclock.read_all(s)
        store["111"]["due_at"] = real_time.time() - 200_000     # over two days old
        careerclock._write(s, store)
        check("a two-day-old record is discarded rather than stampeding",
              careerclock.due_at(s, 111) is None)


def test_a_stale_due_time_cannot_bypass_the_backoff() -> None:
    print("a failure backs off even when a STALE due time is on record")
    # THE OVERNIGHT BUG, 2026-09-02. careerclock.record() preserves the previous
    # due_at on a failed visit (so a restart keeps a live career's schedule), so
    # it returns a non-None - and often long-past - value. Branching on that
    # before result.ok sent every failure down the due-time path, rescheduling to
    # now+MIN_INTERVAL: a visit every 60s, 234 visits and 432 Steam 391s
    # overnight, zero careers.
    with tempfile.TemporaryDirectory() as tmp:
        h = Harness(tmp, {1: 3000.0, 2: 3000.0}, [(1, "stale"), (2, "other")])
        h.run(cycles=1)                      # writes a real due_at for both
        store = careerclock.read_all(h.settings)
        check("a due time is on record", store["1"].get("due_at") is not None)

        # Now that account only ever fails, with its stale due_at still stored.
        h2 = Harness(tmp, {1: lambda n: None, 2: 3000.0},
                     [(1, "stale"), (2, "other")])
        h2.clock.now = h.clock.now + 4000.0  # the stored due time is now in the PAST
        h2.run(cycles=4)
        stale = [t for t, name in h2.order if name == "stale"]
        gaps = [stale[i + 1] - stale[i] for i in range(len(stale) - 1)]
        check("it does NOT retry at the 60s floor",
              all(g > rotate.MIN_INTERVAL_SECONDS for g in gaps), str(gaps))
        check("the first retry uses RETRY_SECONDS",
              gaps and gaps[0] == rotate.RETRY_SECONDS, str(gaps[:1]))
        check("and it still backs off from there",
              len(gaps) < 2 or gaps[-1] >= gaps[0], str(gaps))

        rec = careerclock.read_all(h2.settings)["1"]
        check("the stored clock is still PRESERVED for a restart",
              rec.get("due_at") is not None, str(rec.get("due_at")))


def test_a_broken_account_backs_off() -> None:
    print("an account that always fails does not switch the Steam slot forever")
    with tempfile.TemporaryDirectory() as tmp:
        # 'broken' never succeeds (e.g. a career the server will not let us end).
        # Every retry costs a full account switch, so the gap must grow.
        h = Harness(tmp, {1: lambda n: None, 2: 3000.0},
                    [(1, "broken"), (2, "fine")])
        h.run(cycles=6)
        broken = [t for t, name in h.order if name == "broken"]
        gaps = [broken[i + 1] - broken[i] for i in range(len(broken) - 1)]
        check("the first retry is still prompt",
              gaps and gaps[0] == rotate.RETRY_SECONDS, str(gaps[:1]))
        check("later retries back off", len(gaps) > 1 and gaps[-1] > gaps[0], str(gaps))
        check("and are capped",
              all(g <= rotate.MAX_RETRY_SECONDS for g in gaps), str(gaps))
        # NOT "the healthy one gets more visits" - the broken account's retries
        # legitimately fill the gaps between them. What matters is that the
        # healthy account is never PUSHED BACK past its own career clock.
        fine = [t for t, name in h.order if name == "fine"]
        fine_gaps = [fine[i + 1] - fine[i] for i in range(len(fine) - 1)]
        # Bound = its own career + the finish grace + at most one MIN_INTERVAL,
        # since the anti-burst floor may hold a visit back by that much when the
        # broken account was serviced immediately before it.
        bound = 3000.0 + careerclock.FINISH_GRACE + rotate.MIN_INTERVAL_SECONDS
        check("the healthy account still runs on its own clock",
              all(g <= bound for g in fine_gaps), f"{fine_gaps} vs bound {bound}")

    with tempfile.TemporaryDirectory() as tmp:
        # A one-off failure must NOT permanently penalise a good account.
        h = Harness(tmp, {1: lambda n: None if n == 1 else 3000.0, 2: 3000.0},
                    [(1, "flaky"), (2, "fine")])
        h.run(cycles=3)
        flaky = [t for t, name in h.order if name == "flaky"]
        gaps = [flaky[i + 1] - flaky[i] for i in range(len(flaky) - 1)]
        check("recovery resets the streak",
              len(gaps) > 1 and gaps[1] > rotate.RETRY_SECONDS, str(gaps))


def test_never_hammers_one_account() -> None:
    print("a zero-length clock cannot turn into a hot loop")
    with tempfile.TemporaryDirectory() as tmp:
        # The server answering "0 seconds left" every time would otherwise put
        # this account due in FINISH_GRACE (30s) forever - a full account switch
        # every half minute against the live API.
        h = Harness(tmp, {1: 0.0, 2: 3000.0}, [(1, "zero"), (2, "other")])
        h.run(cycles=4)
        zero = [t for t, name in h.order if name == "zero"]
        gaps = [zero[i + 1] - zero[i] for i in range(len(zero) - 1)]
        check("revisits are floored at MIN_INTERVAL_SECONDS",
              all(g >= rotate.MIN_INTERVAL_SECONDS for g in gaps), str(gaps))


def test_failure_backs_off() -> None:
    print("a failed visit backs off instead of spinning")
    with tempfile.TemporaryDirectory() as tmp:
        h = Harness(tmp, {1: lambda n: None if n == 1 else 3000.0, 2: 3000.0},
                    [(1, "flaky"), (2, "fine")])
        h.run(cycles=2)
        flaky = [t for t, name in h.order if name == "flaky"]
        gap = flaky[1] - flaky[0]
        check("it is retried after RETRY_SECONDS, not immediately",
              gap >= rotate.RETRY_SECONDS, f"gap was {gap}")


def test_no_record_means_no_due_time() -> None:
    print("a visit with no clock does not invent one")
    with tempfile.TemporaryDirectory() as tmp:
        s = Settings(icarus_root=Path(tmp), profile="t",
                     runtime_dir=Path(tmp) / "rt", state_dir=Path(tmp) / "state")
        s.state_dir.mkdir(parents=True, exist_ok=True)
        check("record() returns None when there was no clock",
              careerclock.record(s, 111, remaining_seconds=None,
                                 source="failed") is None)
        check("and due_at reports unknown rather than 'now'",
              careerclock.due_at(s, 111) is None)
        check("a zero viewer_id is ignored",
              careerclock.record(s, 0, remaining_seconds=10.0) is None)


def test_outage_burst_is_paced() -> None:
    print("a roster that all came due during an outage is not drained at once")
    with tempfile.TemporaryDirectory() as tmp:
        h = Harness(tmp, {i: 3000.0 for i in range(1, 6)},
                    [(i, f"a{i}") for i in range(1, 6)])
        h.run(cycles=1)
        # Two hours down: every career ended while nothing was watching.
        h2 = Harness(tmp, {i: 3000.0 for i in range(1, 6)},
                     [(i, f"a{i}") for i in range(1, 6)])
        h2.clock.now = h.clock.now + 7200.0
        h2.run(cycles=1)
        gaps = [h2.order[i + 1][0] - h2.order[i][0] for i in range(len(h2.order) - 1)]
        check("overdue accounts are paced, not switched back to back",
              all(g >= rotate.MIN_INTERVAL_SECONDS for g in gaps), str(gaps))
        check("but they are still all caught up promptly",
              h2.order[-1][0] - h2.order[0][0] <= 5 * rotate.MIN_INTERVAL_SECONDS,
              str(h2.order[-1][0] - h2.order[0][0]))


def test_cycles_stays_a_bounded_errand() -> None:
    print("--cycles is bounded even when one account parks itself for a day")
    with tempfile.TemporaryDirectory() as tmp:
        # a1 comes back "fan capped": due in 24h. Under min(visits_made) the run
        # would keep visiting the others for a full day waiting for a1's count.
        h = Harness(tmp, {1: 86400.0, 2: 3000.0, 3: 3000.0},
                    [(1, "capped"), (2, "b"), (3, "c")])
        h.run(cycles=2)
        check("the run ends after cycles * roster visits",
              h.order and len(h.order) == 6, str(len(h.order)))
        check("and does not sit through the 24h park",
              h.clock.now - h.order[0][0] < 86400.0, str(h.clock.now - h.order[0][0]))


def test_a_failed_visit_keeps_the_known_clock() -> None:
    print("a failed visit does not erase what was already known")
    with tempfile.TemporaryDirectory() as tmp:
        s = Settings(icarus_root=Path(tmp), profile="t",
                     runtime_dir=Path(tmp) / "rt", state_dir=Path(tmp) / "state")
        s.state_dir.mkdir(parents=True, exist_ok=True)
        import time as real_time
        now = real_time.time()
        good = careerclock.record(s, 111, remaining_seconds=3000.0,
                                  started_at=now, finishes_at=now + 3000,
                                  source="started", label="acct")
        careerclock.record(s, 111, remaining_seconds=None, source="failed")
        kept = careerclock.due_at(s, 111)
        check("the good due time survives the failure",
              kept is not None and abs(kept - good) < 1.0, f"{kept} vs {good}")
        rec = careerclock.read_all(s)["111"]
        check("and so do the career time codes",
              rec["career_started_at_utc"].endswith("Z"), str(rec))
        check("while the source records that the visit failed",
              rec["source"] == "failed", str(rec["source"]))


def test_an_absurd_clock_is_bounded() -> None:
    print("a bad clock reading cannot retire an account from the rotation")
    with tempfile.TemporaryDirectory() as tmp:
        s = Settings(icarus_root=Path(tmp), profile="t",
                     runtime_dir=Path(tmp) / "rt", state_dir=Path(tmp) / "state")
        s.state_dir.mkdir(parents=True, exist_ok=True)
        import time as real_time
        due = careerclock.record(s, 111, remaining_seconds=40 * 86400.0,
                                 source="started")
        check("a 40-day reading is clamped when written",
              due - real_time.time() <= careerclock.MAX_DUE_AHEAD + 1, str(due))

        store = careerclock.read_all(s)
        store["111"]["due_at"] = real_time.time() + 40 * 86400
        careerclock._write(s, store)
        check("and a hand-edited far-future one is refused when read",
              careerclock.due_at(s, 111) is None)

        store["111"]["due_at"] = float("nan")
        careerclock._write(s, store)
        check("NaN is refused too", careerclock.due_at(s, 111) is None)


def test_random_wait_between_visits() -> None:
    print("each account waits a random 5-20 min after its career is due, never a fixed rhythm")
    with tempfile.TemporaryDirectory() as tmp:
        h = Harness(tmp, {1: 3000.0}, [(1, "solo")])
        h.run(cycles=12, jitter=(300.0, 1200.0))
        times = [t for t, _ in h.order]
        extra = [times[i + 1] - times[i] - 3000.0 - careerclock.FINISH_GRACE
                 for i in range(len(times) - 1)]
        check("every gap = career + grace + a wait inside 5-20 min",
              extra and all(300.0 - 1e-6 <= e <= 1200.0 + 1e-6 for e in extra),
              str([round(e) for e in extra]))
        check("the waits actually vary (not one fixed offset)",
              len({round(e) for e in extra}) >= 5, str([round(e) for e in extra]))
    with tempfile.TemporaryDirectory() as tmp:
        h = Harness(tmp, {1: 3000.0}, [(1, "solo")])
        h.run(cycles=4, jitter=(300.0, 1200.0))
        check("a career is never collected EARLY by the wait",
              all(b - a >= 3000.0 for (a, _), (b, _) in zip(h.order, h.order[1:])))
    saved = dict(os.environ)
    try:
        os.environ.update(AIC_VISIT_JITTER_MIN="0", AIC_VISIT_JITTER_MAX="0")
        check("0/0 turns it off", rotate._jitter_range() == (0.0, 0.0))
        os.environ.update(AIC_VISIT_JITTER_MIN="20", AIC_VISIT_JITTER_MAX="5")
        check("a max below the min is raised to the min", rotate._jitter_range() == (1200.0, 1200.0))
        os.environ.pop("AIC_VISIT_JITTER_MIN"); os.environ.pop("AIC_VISIT_JITTER_MAX")
        check("default is 5-20 minutes", rotate._jitter_range() == (300.0, 1200.0))
    finally:
        os.environ.clear()
        os.environ.update(saved)


def main() -> int:
    for fn in (test_random_wait_between_visits,
               test_even_roster_still_round_robins,
               test_a_short_career_comes_back_early,
               test_early_arrival_does_not_cost_a_lap,
               test_clock_is_written_and_resumed,
               test_overdue_is_visited_at_once,
               test_stale_records_are_discarded,
               test_never_hammers_one_account, test_a_broken_account_backs_off,
               test_a_stale_due_time_cannot_bypass_the_backoff,
               test_outage_burst_is_paced,
               test_cycles_stays_a_bounded_errand,
               test_a_failed_visit_keeps_the_known_clock,
               test_an_absurd_clock_is_bounded,
               test_failure_backs_off,
               test_no_record_means_no_due_time):
        fn()
        print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
        return 1
    print("all career-clock checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
