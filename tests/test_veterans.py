"""Deleting a veteran is permanent, so the candidate filter is the whole design.

Shaped from a REAL account, viewer 319865323652, read 2026-09-03:

    260 veterans, 10 distinct characters, 248 of them one character
    is_locked                   {0: 253, 1: 7}
    trained_chara_favorite_array [121, 145, 190, 299, 352, 358, 398]
    team_data_array              [2, 3, 4, 5, 6, 38, 223, 242, ...]
    of the 248 duplicates       6 were PROTECTED

That last line is why this file exists. A rule that only counted duplicates would
have deleted a Team Trials member on its first run, on a live account, unattended.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aic import veterans  # noqa: E402

FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label} {detail}")
        FAILURES.append(label)


def vet(tid, card_id, score, locked=0, rank=None):
    return {"trained_chara_id": tid, "card_id": card_id, "rank_score": score,
            "rank": rank if rank is not None else score // 400,
            "is_locked": locked, "register_time": "2026-09-02 20:26:16"}


def account(rows, **extra):
    idx = {"trained_chara": rows}
    idx.update(extra)
    return idx


def live_shaped():
    """The real account: 248 copies of chara 1006 plus a thin tail."""
    rows = [vet(1000 + i, 100602, 3400 + i * 7) for i in range(248)]
    rows += [vet(2, 100901, 9000), vet(3, 100901, 9100),
             vet(4, 102402, 8000), vet(5, 102402, 8100),
             vet(6, 103001, 7000), vet(38, 103001, 7100),
             vet(121, 100701, 9500, locked=1),
             vet(223, 104101, 6000), vet(242, 105201, 6100),
             vet(299, 100801, 5000, locked=1)]
    # Six of the 248 are protected, exactly as measured: two locked, two on the
    # Team Trials roster, two favourited.
    rows[0]["is_locked"] = 1
    rows[1]["is_locked"] = 1
    return account(
        rows,
        trained_chara_favorite_array=[{"trained_chara_id": 121},
                                      {"trained_chara_id": 299},
                                      {"trained_chara_id": 1002},
                                      {"trained_chara_id": 1003}],
        team_data_array=[{"trained_chara_id": i} for i in (2, 3, 4, 5, 6, 38,
                                                           223, 242, 1004, 1005)],
        single_mode_chara_light={},
    )


def test_the_measured_account() -> None:
    print("the real 248-duplicate account")
    idx = live_shaped()
    keep = veterans.protected_ids(idx)
    check("all four signals are collected",
          {1000, 1001} <= keep and {1002, 1003} <= keep
          and {1004, 1005} <= keep and 121 in keep, str(sorted(keep)[:14]))

    v = veterans.pick_victim(idx)
    check("it picks from the 248-copy character", v and v["_chara"] == 1006,
          str(v and v.get("_chara")))
    check("the group size counts protected copies too",
          v and v["_group_size"] == 248, str(v and v.get("_group_size")))
    check("but only unprotected ones are deletable",
          v and v["_group_deletable"] == 242, str(v and v.get("_group_deletable")))
    check("THE regression: it is not one of the 6 protected",
          v and v["trained_chara_id"] not in keep, str(v and v["trained_chara_id"]))
    check("it is the lowest-scoring deletable copy",
          v and v["trained_chara_id"] == 1006, str(v and v["trained_chara_id"]))


def test_locked_and_favourite_are_separate_signals() -> None:
    print("a favourite that is not locked is still safe")
    rows = [vet(1, 100602, 100), vet(2, 100602, 200), vet(3, 100602, 300)]
    idx = account(rows, trained_chara_favorite_array=[{"trained_chara_id": 1}])
    check("the unlocked favourite is protected", 1 in veterans.protected_ids(idx))
    v = veterans.pick_victim(idx)
    check("so the next-worst dies instead", v["trained_chara_id"] == 2,
          str(v["trained_chara_id"]))


def test_team_members_survive() -> None:
    print("a Team Trials member is never deleted")
    rows = [vet(1, 100602, 100), vet(2, 100602, 200), vet(3, 100602, 300)]
    idx = account(rows, team_data_array=[{"trained_chara_id": 1}])
    v = veterans.pick_victim(idx)
    check("the team member is skipped", v["trained_chara_id"] == 2,
          str(v["trained_chara_id"]))


def test_running_career_parents_survive() -> None:
    print("the career in progress keeps its parents")
    rows = [vet(1, 100602, 100), vet(2, 100602, 200), vet(3, 100602, 300)]
    idx = account(rows, single_mode_chara_light={
        "succession_trained_chara_id_1": 1, "succession_trained_chara_id_2": 2})
    v = veterans.pick_victim(idx)
    check("both parents skipped", v["trained_chara_id"] == 3,
          str(v["trained_chara_id"]))


def test_pending_start_parents_survive() -> None:
    print("the parents of the start about to be retried survive"
          " (caller-supplied)")
    rows = [vet(1, 100602, 100), vet(2, 100602, 200), vet(3, 100602, 300)]
    v = veterans.pick_victim(account(rows), keep={1, 2})
    check("caller ids are honoured", v["trained_chara_id"] == 3,
          str(v["trained_chara_id"]))


def test_group_by_character_not_card() -> None:
    print("duplicates are counted per CHARACTER, not per card")
    # 4 copies of chara 1006 split across two outfits, vs 3 of one exact card.
    rows = ([vet(i, 100601, 500 + i) for i in (1, 2)]
            + [vet(i, 100602, 500 + i) for i in (3, 4)]
            + [vet(i, 103001, 100 + i) for i in (5, 6, 7)])
    v = veterans.pick_victim(account(rows))
    check("the 4-copy character wins over the 3-copy one",
          v["_chara"] == 1006 and v["_group_size"] == 4,
          f"chara={v['_chara']} size={v['_group_size']}")


def test_a_fully_protected_group_falls_through() -> None:
    print("a protected top group yields to the next largest")
    rows = ([vet(i, 100602, 900 + i) for i in (1, 2, 3)]      # all protected
            + [vet(i, 103001, 100 + i) for i in (4, 5)])
    idx = account(rows, team_data_array=[{"trained_chara_id": i} for i in (1, 2, 3)])
    v = veterans.pick_victim(idx)
    check("it moves to the smaller unprotected group",
          v["_chara"] == 1030 and v["trained_chara_id"] == 4,
          f"chara={v['_chara']} id={v['trained_chara_id']}")


def test_never_deletes_a_singleton() -> None:
    print("a lone copy is not a duplicate")
    rows = [vet(1, 100602, 100), vet(2, 103001, 200), vet(3, 104101, 300)]
    check("nothing to delete when nothing is duplicated",
          veterans.pick_victim(account(rows)) is None)
    check("an empty account is None, not a crash",
          veterans.pick_victim({}) is None)
    check("everything protected is None, not a fallback",
          veterans.pick_victim(
              account([vet(1, 100602, 1, locked=1), vet(2, 100602, 2, locked=1)])
          ) is None)


def test_make_room_verifies_by_rereading() -> None:
    print("the delete is confirmed by re-reading, not by the response")
    rows = [vet(1, 100602, 100), vet(2, 100602, 200), vet(3, 100602, 300)]

    class Client:
        def __init__(self, actually_delete=True, raise_it=False):
            self.actually_delete = actually_delete
            self.raise_it = raise_it
            self.calls = []
            self.rows = [dict(r) for r in rows]

        def call(self, ep, args=None, **kw):
            self.calls.append((ep, args, kw))
            if self.raise_it:
                raise RuntimeError("timeout")
            if self.actually_delete:
                gone = set(args["trained_chara_id_array"])
                self.rows = [r for r in self.rows
                             if r["trained_chara_id"] not in gone]
            return {"data": {}}

    class Session:
        def __init__(self, client):
            self.client = client

        def refresh_home(self):
            return {"trained_chara": self.client.rows}

    veterans._last_delete.clear()
    c = Client()
    got = veterans.make_room(Session(c))
    check("it deleted the worst copy", got and got["trained_chara_id"] == 1,
          str(got and got["trained_chara_id"]))
    check("exactly one call", len(c.calls) == 1, str(c.calls))
    check("one id, never a batch",
          c.calls[0][1]["trained_chara_id_array"] == [1], str(c.calls[0][1]))
    check("retries are OFF for this irreversible write",
          c.calls[0][2].get("retry_208") == 0 and c.calls[0][2].get("retry_205") == 0,
          str(c.calls[0][2]))

    # The server said OK but nothing went away: must report failure, not success.
    veterans._last_delete.clear()
    c = Client(actually_delete=False)
    check("a no-op delete is reported as failure",
          veterans.make_room(Session(c)) is None)

    # It raised, but the write had landed anyway - the re-read is the truth.
    veterans._last_delete.clear()
    c = Client(actually_delete=True, raise_it=False)
    check("a successful delete after a raised call still counts",
          veterans.make_room(Session(c)) is not None)

    veterans._last_delete.clear()
    c = Client(raise_it=True)
    check("a raise with nothing deleted is a failure, not a crash",
          veterans.make_room(Session(c)) is None)


def test_deletion_is_rate_limited() -> None:
    """A delete that does not unstick the account must not delete again.

    rotate retries a failed visit in 300s. Without a floor, a start that keeps
    refusing after a successful delete would eat a veteran every five minutes -
    ~96 overnight - while the log read as if it were repairing something.
    """
    print("at most one deletion per account per 30 minutes")
    rows = [vet(i, 100602, 100 * i) for i in (1, 2, 3, 4)]

    class Client:
        def __init__(self):
            self.calls = []
            self.rows = [dict(r) for r in rows]

        def call(self, ep, args=None, **kw):
            self.calls.append(args)
            gone = set(args["trained_chara_id_array"])
            self.rows = [r for r in self.rows if r["trained_chara_id"] not in gone]
            return {"data": {}}

    class Session:
        def __init__(self, client):
            self.client = client

        def refresh_home(self):
            return {"trained_chara": self.client.rows,
                    "user_info": {"viewer_id": 777}}

    veterans._last_delete.clear()
    c = Client()
    sess = Session(c)
    check("the first deletion goes through", veterans.make_room(sess) is not None)
    check("the second, moments later, is refused",
          veterans.make_room(sess) is None)
    check("and it really made only one call", len(c.calls) == 1, str(c.calls))

    # Once the window has passed it is allowed again.
    veterans._last_delete[777] = time.time() - veterans.MIN_SECONDS_BETWEEN_DELETES - 1
    check("after the window it deletes again",
          veterans.make_room(sess) is not None)
    check("two calls total", len(c.calls) == 2, str(c.calls))

    # The guard is per account, not global.
    veterans._last_delete.clear()
    veterans._last_delete[999] = time.time()
    check("another account is unaffected",
          veterans.make_room(sess) is not None)
    veterans._last_delete.clear()


def main() -> int:
    for fn in (test_the_measured_account,
               test_locked_and_favourite_are_separate_signals,
               test_team_members_survive, test_running_career_parents_survive,
               test_pending_start_parents_survive, test_group_by_character_not_card,
               test_a_fully_protected_group_falls_through,
               test_never_deletes_a_singleton,
               test_make_room_verifies_by_rereading,
               test_deletion_is_rate_limited):
        fn()
        print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
        return 1
    print("all veteran-cleanup checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
