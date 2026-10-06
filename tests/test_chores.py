"""Chore scheduling: cadence gating, daily-reset boundary, one-account rollout,
and the rule that a chore which RAISES still records its attempt (so a broken
flow can't re-fire every 60s and hammer the API).
"""

from __future__ import annotations

import json
import sys
import time
import types
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aic import chores  # noqa: E402

FAILURES: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if cond else 'FAIL'} {label}" + (f"  {detail}" if not cond else ""))
    if not cond:
        FAILURES.append(label)


def ts(s: str) -> float:
    return datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc).timestamp()


def test_periodic_gate() -> None:
    print("periodic cadence: due only after the period elapses")
    now = 1_000_000.0
    check("never-run is due", chores._due_periodic({}, "club", 8 * 3600, now))
    check("just-ran is not due", not chores._due_periodic({"club": now - 10}, "club", 8 * 3600, now))
    check("7h59m not due", not chores._due_periodic({"club": now - (8 * 3600 - 60)}, "club", 8 * 3600, now))
    check("8h01m is due", chores._due_periodic({"club": now - (8 * 3600 + 60)}, "club", 8 * 3600, now))


def test_daily_reset_boundary() -> None:
    print("daily gate flips exactly at the 15:00 UTC reset")
    assert chores.RESET_HOUR_UTC == 15
    before = ts("2026-09-13 14:59")   # still the previous game-day
    after = ts("2026-09-13 15:01")    # new game-day
    check("14:59 and 15:01 are different game-days", chores.reset_day(before) != chores.reset_day(after),
          f"{chores.reset_day(before)} vs {chores.reset_day(after)}")
    check("done-before-reset is due again after reset",
          chores._due_daily({"legend": chores.reset_day(before)}, "legend", after))
    check("done-after-reset is not due again same day",
          not chores._due_daily({"legend": chores.reset_day(after)}, "legend", after + 3600))


class FakeClient:
    def __init__(self, board=None, boom=False, gifts=None, count=None,
                 received=None, server_stock=None, refuse=None):
        self.calls = []
        self.args = {}
        self.stages = []               # every get_request_data body that staged a donate
        self._board = board or []
        self._boom = boom
        self._gifts = gifts or []      # served by room_enter, like the real circle
        self._count = count            # daily_donated_count on room_enter
        self._received = received      # shoes handed over by circle_item_request/receive
        self._server_stock = server_stock  # current_item_data_array on the stage reply
        self._refuse = refuse          # payload -> rc to refuse a donate_multiple with
        self.commits = []              # every donate_multiple payload

    def call(self, ep, args=None):
        self.calls.append(ep)
        self.args[ep] = args
        if self._boom:
            raise RuntimeError("simulated API failure")
        data = {}
        if ep == "circle/room_enter":
            data = {"circle_item_donate_array": self._gifts}
            if self._count is not None:
                data["daily_donated_count"] = self._count
        elif ep == "circle_item_request/receive" and self._received:
            data = {"reward_summary_info": {"add_item_list": [
                {"item_id": chores.CLUB_ITEM_ID, "number": self._received}]}}
        elif ep == "circle_item_request/get_request_data":
            data = {"circle_item_request_array": self._board}
            if (args or {}).get("request_id_array"):
                self.stages.append(json.loads(json.dumps(args)))
                if self._server_stock is not None:
                    data["current_item_data_array"] = [
                        {"item_id": chores.CLUB_ITEM_ID, "number": self._server_stock}]
        elif ep == "circle_item_request/donate_multiple":
            self.commits.append(json.loads(json.dumps(args)))
            rc = self._refuse(args) if self._refuse else None
            if rc == "net":
                raise RuntimeError("connection reset")     # no result code
            if rc:
                raise Refused(rc)
            staged = (args or {}).get("request_id_array") or []
            # the real response lists every gift on the staged requests (others' too)
            # like the real reply: EVERY gift on the staged requests - another
            # member's, our older ones (room_enter's) and our new one (2 items each)
            data = {"circle_item_donate_array":
                    [{"donate_id": 1, "viewer_id": 999, "request_id": r, "item_num": 2} for r in staged[:1]]
                    + [g for g in self._gifts if g.get("request_id") in staged]
                    + [{"donate_id": 1000 + r, "viewer_id": 111, "request_id": r, "item_num": 2}
                       for r in staged]}
            if self._count is not None:
                self._count += len(staged)
                data["daily_donated_count"] = self._count
        elif ep == "team_stadium/index":
            data = {"term_state": 1, "race_status": 0, "team_stadium_user": {"team_class": 1}}
        elif ep == "team_stadium/opponent_list":
            # as captured: strength 1 is the STRONGEST (highest evaluation_point)
            data = {"opponent_info_array": [
                {"strength": 1, "opponent_viewer_id": 11, "evaluation_point": 31842, "team_data_array": []},
                {"strength": 2, "opponent_viewer_id": 22, "evaluation_point": 27676, "team_data_array": []},
                {"strength": 3, "opponent_viewer_id": 33, "evaluation_point": 22905, "team_data_array": []}]}
        elif ep == "team_stadium/start":
            self.rp = getattr(self, "rp", 3) - 1
            data = {"rp_info": {"current_rp": self.rp, "max_rp": 5}}
        elif ep == "team_stadium/all_race_end":
            data = {"add_fan_info_array": [{"add_fan": 100}, {"add_fan": 50}], "final_win_type": 1}
        elif ep == "daily_legend_race/index":
            data = {"daily_legend_race_record_array": [{"daily_legend_race_id": 10001, "is_played": 0}]}
        # Real client.call() shape: payload at res["data"] (single level),
        # headers at res["data_headers"] (top level).
        return {"data": data, "data_headers": {"result_code": 1}}


class Refused(Exception):
    def __init__(self, rc):
        super().__init__(f"refused {rc}")
        self.result_code = rc


def _session(client, viewer=111, load_index=None):
    return types.SimpleNamespace(client=client, viewer_id=viewer, load_index=load_index or {})


def _settings(tmp: Path):
    s = types.SimpleNamespace(state_dir=tmp)
    return s


def _gift(rid, viewer, n=2, did=None):
    return {"donate_id": did or f"{rid}-{viewer}-{n}", "request_id": rid, "viewer_id": viewer, "item_num": n}


def test_donate_batch() -> None:
    print("donate batch: every request that can still take OUR gift, any item we hold, oldest first")
    ITEM = chores.CLUB_ITEM_ID
    now = ts("2026-09-21 16:12")
    live, dead = "2026-09-21 20:00:00", "2026-09-18 01:59:00"
    board = [{"viewer_id": 777, "request_id": 7, "item_id": ITEM, "end_time": live},  # we gave 1 -> open
             {"viewer_id": 111, "request_id": 1, "item_id": ITEM, "end_time": live},  # mine
             {"viewer_id": 666, "request_id": 6, "item_id": ITEM, "end_time": live},  # open, 8 given
             {"viewer_id": 333, "request_id": 3, "item_id": 10, "end_time": live},    # long shoes, open
             {"viewer_id": 334, "request_id": 9, "item_id": 83, "end_time": live},    # a sash we hold none of
             {"viewer_id": 444, "request_id": 4, "item_id": ITEM, "end_time": live},  # FULL 5x2
             {"viewer_id": 555, "request_id": 5, "item_id": ITEM, "end_time": live},  # we gave 2
             {"viewer_id": 888, "request_id": 8, "item_id": ITEM, "end_time": dead},  # EXPIRED, 8 given
             {"viewer_id": 222, "request_id": 2, "item_id": ITEM, "end_time": live}]  # open, 2 given
    gifts = ([_gift(2, 901)]
             + [_gift(4, 900 + i) for i in range(5)]
             + [_gift(5, 111)]
             + [_gift(6, 910 + i) for i in range(4)]
             + [_gift(7, 111, n=1)]
             + [_gift(8, 920 + i) for i in range(4)])
    stock = {ITEM: 500, 10: 30}
    rid, items, tally = chores._donate_batch(board, gifts, 111, stock, 10, now=now)
    check("other items are donated too, oldest first across items (expired/full/given/own skipped)",
          rid == [2, 3, 6, 7], str(rid))
    check("each request declares our stock of ITS item",
          items == [{"item_id": ITEM, "number": 500}, {"item_id": 10, "number": 30},
                    {"item_id": ITEM, "number": 500}, {"item_id": ITEM, "number": 500}], str(items))
    check("tally per item, own request left out",
          tally[ITEM] == {"open": 3, "expired": 1, "full": 1, "already given": 1, "refused today": 0}
          and tally[10]["open"] == 1 and tally[83]["open"] == 1 and len(tally) == 3, str(tally))
    check("an item we hold none of is never staged", 9 not in rid)
    summary = chores._board_summary(tally, stock)
    check("board summary names the items and why",
          summary.startswith(f"{chores._item_name(ITEM)} x6 (3 open, 1 expired, 1 full, 1 already given)")
          and f"{chores._item_name(10)} x1 (1 open)" in summary
          and f"{chores._item_name(83)} x1 (1 open, we hold none)" in summary, summary)
    names = chores._item_names()
    check("in-game item names from master.mdb (when it is there)",
          not names or (names.get(7) == "Medium Racing Shoes"
                        and names.get(89) == "Hanshin J.F. Winner's Sash"), str(names.get(7)))
    check("names fall back without master.mdb", chores.SHOE_NAMES[7] == "medium shoes"
          and chores._items_text({7: 16, 89: 2, 10: 0})
          == f"16x {chores._item_name(7)}, 2x {chores._item_name(89)}")
    check("expiring within the commit slack (60s) is dropped",
          chores._donate_batch([{"viewer_id": 2, "request_id": 9, "item_id": ITEM,
                                 "end_time": "2026-09-21 16:12:30"}], [], 111, stock, 10, now=now)[0] == [])
    check("unparseable end_time is treated as live",
          chores._donate_batch([{"viewer_id": 2, "request_id": 9, "item_id": ITEM, "end_time": "?"}],
                               [], 111, stock, 10, now=now)[0] == [9])
    big = [{"viewer_id": 900 + i, "request_id": 100 + i, "item_id": (ITEM, 10, 83)[i % 3]}
           for i in reversed(range(20))]
    many = {ITEM: 99, 10: 99, 83: 99}
    rid2, items2, _ = chores._donate_batch(big, [], 111, many, 10, now=now)
    check("batch never exceeds the cap", len(rid2) == 10 == len(items2), str(len(rid2)))
    check("oldest requests first across items (lowest request_id, like the game client)",
          rid2 == list(range(100, 110)), str(rid2))
    check("batch shrinks to what is left of today",
          chores._donate_batch(big, [], 111, many, 3, now=now)[0] == [100, 101, 102])
    check("nothing left -> empty batch", chores._donate_batch(big, [], 111, many, -2, now=now)[0] == [])
    low = chores._donate_batch(big, [], 111, {ITEM: 3, 10: 1}, 10, now=now)[0]
    check("an item gets only as many requests as its stock covers (2 per request)",
          [r for r in low if (r - 100) % 3 == 0] == [100, 103]
          and [r for r in low if (r - 100) % 3 == 1] == [101], str(low))
    check("_split: one batch per item, each item's oldest first, oldest item first",
          chores._split([3, 5, 8], [{"item_id": 10}, {"item_id": 7}, {"item_id": 10}])
          == [([3, 8], [{"item_id": 10}, {"item_id": 10}]), ([5], [{"item_id": 7}])])


def test_circle_view() -> None:
    print("circle view: fresh board, gifts unioned, count from room_enter")
    entered = {"data": {"circle_item_request_array": [{"request_id": 1}, {"request_id": 2}],
                        "circle_item_donate_array": [_gift(1, 9, did=50)], "daily_donated_count": 4},
               "data_headers": {"servertime": 111}}
    polled = {"data": {"circle_item_donate_array": [_gift(2, 8, did=51)]}}
    got = {"data": {"circle_item_request_array": [{"request_id": 1}],      # 2 was claimed meanwhile
                    "circle_item_donate_array": [_gift(1, 9, did=50)]},
           "data_headers": {"servertime": 222}}
    board, gifts, count, now = chores._circle_view(entered, polled, got)
    check("board is get_request_data's (no resurrected requests)",
          [r["request_id"] for r in board] == [1], str(board))
    check("gifts unioned and de-duplicated by donate_id", len(gifts) == 2, str(gifts))
    check("count from room_enter", count == 4, str(count))
    check("server time from the board read", now == 222, str(now))
    board2, _, count2, now2 = chores._circle_view(entered, None, None)
    check("board read failed -> room_enter's board", len(board2) == 2, str(board2))
    check("no count / no servertime -> None",
          chores._circle_view(None, None, {"data": {}})[2:] == (None, None))
    check("junk responses do not raise", chores._circle_view("x", 5, None)[0] == [])


def test_club_stock_stays_true() -> None:
    print("club: the declared stock follows claims and the server's own count")
    ITEM = chores.CLUB_ITEM_ID
    board = [{"viewer_id": 222, "request_id": 2, "item_id": ITEM}]
    idx = {"item_list": [{"item_id": ITEM, "number": 100}]}
    msgs = []
    real_ping = chores._ping
    chores._ping = lambda who, did, title="Club": msgs.append(list(did))
    try:
        # the claim hands over 10 shoes -> the donate declares 110, not the stale 100
        c = FakeClient(board=board, received=10)
        chores.club(_session(c, load_index=idx), chores.log)
        declared = c.commits[0]["client_item_data_array"][0]["number"] if c.commits else None
        check("claimed shoes are added before declaring the stock", declared == 110, str(declared))
        check("discord says what happened: request, claim, donation",
              msgs and msgs[-1][:2] == ["received 10 shoes from members", "requested shoes from the club"],
              str(msgs))
        check("the board read is followed straight by donate_multiple (the client's own pair)",
              c.calls[-2:] == ["circle_item_request/get_request_data", "circle_item_request/donate_multiple"]
              and not c.stages, str(c.calls))
        # nothing to claim -> no 'received' line
        c3 = FakeClient(board=board)
        chores.club(_session(c3, load_index=idx), chores.log)
        check("nothing claimed -> no 'received' message", not any("received" in m for m in msgs[-1]),
              str(msgs[-1]))
    finally:
        chores._ping = real_ping
    check("_added_items reads add_item_list",
          chores._added_items({"data": {"reward_summary_info": {"add_item_list": [
              {"item_id": 7, "number": 4}, {"item_id": 7, "number": 6}]}}}) == {7: 10})


def test_club_request_then_donate_phases() -> None:
    print("club: request phase first, donate only once every account has requested")
    ITEM = chores.CLUB_ITEM_ID
    settings = _settings(Path(__import__("tempfile").mkdtemp()))
    real_roster, real_wait = chores._roster_ids, chores.CLUB_REQUEST_WAIT_SECONDS
    chores._roster_ids = lambda: {111, 222, 333}
    chores.CLUB_REQUEST_WAIT_SECONDS = 10 ** 9    # the 3h valve is checked separately below
    board = [{"viewer_id": 222, "request_id": 2, "item_id": ITEM},
             {"viewer_id": 333, "request_id": 3, "item_id": ITEM}]
    idx = {"item_list": [{"item_id": ITEM, "number": 100}]}

    def run(viewer):
        c = FakeClient(board=board)
        sess = types.SimpleNamespace(client=c, viewer_id=viewer, load_index=idx, settings=settings)
        chores.club(sess, chores.log)
        return c
    try:
        c = run(111)
        check("request phase: the request is posted", "circle_chat/send_item_request" in c.calls, str(c.calls))
        check("request phase: no donating (2 of 3 accounts still to request)",
              "circle_item_request/donate_multiple" not in c.calls, str(c.calls))
        c = run(222)
        check("still waiting on account 333 -> no donate", "circle_item_request/donate_multiple" not in c.calls)
        c = run(333)
        check("the LAST account's request opens the donate phase right away",
              "circle_item_request/donate_multiple" in c.calls, str(c.calls))
        c = run(111)
        check("every account donates on its next visit", "circle_item_request/donate_multiple" in c.calls)
        rec = chores._club_day(settings, time.time())
        check("today's requests recorded", sorted(rec["requested"]) == [111, 222, 333], str(rec))
    finally:
        chores._roster_ids, chores.CLUB_REQUEST_WAIT_SECONDS = real_roster, real_wait

    # the safety valve: an account that never requests cannot block the rest all day
    s2 = _settings(Path(__import__("tempfile").mkdtemp()))
    reset = ts("2026-10-04 15:00")
    chores._mark_requested(s2, 111, now=reset + 600)
    ready, why = chores._donate_phase(s2, now=reset + 3600, roster={111, 222})
    check("1h after the reset with one account missing -> keep waiting", not ready, why)
    ready, why = chores._donate_phase(s2, now=reset + chores.CLUB_REQUEST_WAIT_SECONDS + 60,
                                      roster={111, 222})
    check("past the wait -> donate anyway, naming the missing account",
          ready and "222" in why and "anyway" in why, why)
    ready, _ = chores._donate_phase(s2, now=ts("2026-10-05 15:05"), roster={111, 222})
    check("a new game-day starts the request phase again", not ready)
    check("no roster -> never blocks", chores._donate_phase(s2, now=reset, roster=set())[0])


def test_club_runs_full_flow(tmp_path_factory=None) -> None:
    print("club: enter -> poll -> request -> bulk-donate to members' requests, any item")
    ITEM = chores.CLUB_ITEM_ID
    board = [{"viewer_id": 111, "request_id": 1, "item_id": ITEM},   # mine, excluded
             {"viewer_id": 222, "request_id": 2, "item_id": ITEM},   # shoes, donatable
             {"viewer_id": 333, "request_id": 3, "item_id": 10}]     # long shoes, donatable
    c = FakeClient(board=board)
    # the account holds 100 medium and 40 long shoes, so it CAN donate to 2 and 3
    sess = _session(c, load_index={"item_list": [{"item_id": ITEM, "number": 100},
                                                 {"item_id": 10, "number": 40}]})
    ok = chores.club(sess, chores.log)
    check("it ran", ok is True)
    check("it polled to establish the session", "circle_chat/polling" in c.calls, str(c.calls))
    check("it posted the daily request", "circle_chat/send_item_request" in c.calls, str(c.calls))
    check("it called donate_multiple", "circle_item_request/donate_multiple" in c.calls, str(c.calls))
    donate_stage = c.args.get("circle_item_request/donate_multiple")
    check("donate declares the account's real stock as number",
          donate_stage and donate_stage["client_item_data_array"][0]["number"] == 100,
          str(donate_stage))
    check("both items go in ONE batch, like the game's bulk-donate button",
          donate_stage and donate_stage["request_id_array"] == [2, 3]
          and [i["item_id"] for i in donate_stage["client_item_data_array"]] == [ITEM, 10]
          and donate_stage["client_item_data_array"][1]["number"] == 40, str(donate_stage))
    check("one commit", len(c.commits) == 1, str(c.commits))

    # what was given, item by item, from OUR new rows only (not an older gift of ours)
    said, seen2 = [], []
    real_ping = chores._ping
    chores._ping = lambda who, did, title="Club": said.append(list(did))

    class Cap2:
        def info(self, msg, *a): seen2.append(msg % a if a else msg)
    try:
        mixed = FakeClient(board=board, gifts=[_gift(2, 111, n=1, did="older")])
        chores.club(_session(mixed, load_index={"item_list": [{"item_id": ITEM, "number": 100},
                                                              {"item_id": 10, "number": 40}]}), Cap2())
    finally:
        chores._ping = real_ping
    want = f"2x {chores._item_name(ITEM)}, 2x {chores._item_name(10)}"
    check("discord says which items were donated, and how many",
          said and f"donated 4 items to 2 member request(s) ({want})" in said[-1], str(said))
    check("...and so does the log", any(f"bulk-donated to 2 member request(s) ({want})" in m
                                        for m in seen2), str(seen2[-2:]))

    # a visit crossing the 15:00 UTC reset: the server counter went BACKWARDS (7 -> 2)
    class Reset(FakeClient):
        def call(self, ep, args=None):
            res = super().call(ep, args)
            if ep == "circle_item_request/donate_multiple":
                res["data"]["daily_donated_count"] = 2
            return res
    seen2.clear()
    chores.club(_session(Reset(board=board, count=7), load_index={"item_list": [
        {"item_id": ITEM, "number": 100}, {"item_id": 10, "number": 40}]}), Cap2())
    check("counter going backwards at the reset -> count from our new rows, never negative",
          any("bulk-donated to 2 member request(s)" in m for m in seen2), str(seen2[-2:]))

    # 7 donates used today -> batch of at most 3, and the reported count comes from
    # the server's daily_donated_count, not the response rows (which include others')
    many = [{"viewer_id": 900 + i, "request_id": 50 + i, "item_id": ITEM} for i in range(6)]
    part = FakeClient(board=many, count=7)
    seen = []
    class Cap:
        def info(self, msg, *a): seen.append(msg % a if a else msg)
    chores.club(_session(part, load_index={"item_list": [{"item_id": ITEM, "number": 100}]}), Cap())
    staged = (part.args.get("circle_item_request/donate_multiple") or {}).get("request_id_array")
    check("batch sized to what is left today (10 - 7 = 3)", staged == [50, 51, 52], str(staged))
    check("donated count from the server counter",
          any("bulk-donated to 3 member" in m and "today 10/10" in m for m in seen), str(seen[-3:]))

    # malformed circle data -> no exception out of club()
    class Bad(FakeClient):
        def call(self, ep, args=None):
            res = super().call(ep, args)
            if ep == "circle/room_enter":
                res["data"]["daily_donated_count"] = "garbage"
            return res
    bad = Bad(board=board)
    try:
        chores.club(_session(bad, load_index={"item_list": [{"item_id": ITEM, "number": 100}]}), chores.log)
        check("malformed circle data does not raise out of club()", True)
    except Exception as exc:  # noqa: BLE001
        check("malformed circle data does not raise out of club()", False, repr(exc))

    # room_enter says today's donates are spent -> no stage/commit call at all
    capped = FakeClient(board=board, count=chores.CLUB_DONATE_CAP)
    chores.club(_session(capped, load_index={"item_list": [{"item_id": ITEM, "number": 100}]}), chores.log)
    check("no donate call once the daily cap is reached",
          "circle_item_request/donate_multiple" not in capped.calls, str(capped.calls))
    check("...but it still posts its own request",
          "circle_chat/send_item_request" in capped.calls, str(capped.calls))

    # the only shoes request is one room_enter shows we already gave 2 to -> skip
    given = FakeClient(board=board, gifts=[_gift(2, 111)])
    chores.club(_session(given, load_index={"item_list": [{"item_id": ITEM, "number": 100}]}), chores.log)
    check("no donate when every open request already has our gift",
          "circle_item_request/donate_multiple" not in given.calls, str(given.calls))

    # we hold NO shoes -> nothing to donate
    c2 = FakeClient(board=[{"viewer_id": 222, "request_id": 2, "item_id": ITEM}])
    chores.club(_session(c2, load_index={"item_list": []}), chores.log)
    check("no donate when we hold no shoes",
          "circle_item_request/donate_multiple" not in c2.calls, str(c2.calls))


def test_club_refused_mixed_batch() -> None:
    print("club: a refused batch is split down to the one bad request, which is then left out")
    ITEM = chores.CLUB_ITEM_ID
    settings = _settings(Path(__import__("tempfile").mkdtemp()))
    board = [{"viewer_id": 222, "request_id": 2, "item_id": ITEM},
             {"viewer_id": 333, "request_id": 3, "item_id": 10},
             {"viewer_id": 444, "request_id": 4, "item_id": ITEM}]
    stock = {"item_list": [{"item_id": ITEM, "number": 100}, {"item_id": 10, "number": 40}]}
    seen, msgs = [], []
    bad = {3}

    class Cap:
        def info(self, msg, *a): seen.append(msg % a if a else msg)

    def refuse(args):     # the server refuses any batch that holds a "bad" request
        return 501 if bad & set(args["request_id_array"]) else None

    def run(count=0, refuse_=refuse):
        c = FakeClient(board=board, count=count, refuse=refuse_)
        sess = types.SimpleNamespace(client=c, viewer_id=111, load_index=stock, settings=settings)
        chores.club(sess, Cap())
        return [b["request_id_array"] for b in c.commits]
    real_ping, real_phase = chores._ping, chores._donate_phase
    chores._ping = lambda who, did, title="Club": msgs.append(list(did))
    chores._donate_phase = lambda s, now=None, roster=None: (True, "test")
    try:
        sent = run()
        check("mixed batch first, then per item (oldest item first), bad one pinned down",
              sent == [[2, 3, 4], [2, 4], [3]], str(sent))
        check("the good ones still went through",
              any(f"bulk-donated to 2 member request(s) (4x {chores._item_name(ITEM)})" in m
                  for m in seen), str(seen[-6:]))
        check("discord names what was donated",
              msgs and f"donated 4 items to 2 member request(s) (4x {chores._item_name(ITEM)})"
              in msgs[-1], str(msgs))
        check("only the single refused request is remembered, with the stock we declared",
              chores._refused_today(settings, 111) == {"3:40"}, str(chores._refused_today(settings, 111)))
        check("same board next visit: the refused request is left out before the cut",
              run() == [[2, 4]], str(seen[-3:]))
        check("...and the board line says so", any("1 refused today" in m for m in seen), str(seen[-4:]))
        check("with one donate left, the refused (older) request no longer blocks a good one",
              run(count=9) == [[2]], str(seen[-3:]))
        stock["item_list"][1]["number"] = 41
        check("a different declared stock is a different gift: tried again",
              run()[0] == [2, 3, 4], str(seen[-3:]))
        stock["item_list"][1]["number"] = 40
        bad.clear()
        bad.add(4)
        board.append({"viewer_id": 555, "request_id": 5, "item_id": ITEM})
        sent = run(refuse_=lambda a: 501 if 4 in a["request_id_array"] else None)
        check("a refused single-item batch is split per request",
              sent == [[2, 4, 5], [2], [4], [5]], str(sent))
        check("network error (no code): no split, nothing remembered",
              run(refuse_=lambda a: "net") == [[2, 5]]
              and chores._refused_today(settings, 111) == {"3:40", "3:41", "4:100"},
              str(chores._refused_today(settings, 111)))
        rec = chores._club_day(settings, time.time())
        rec["refused"], rec["requested"] = "garbage", ["x", 222]
        chores._club_save(settings, rec)
        check("malformed records read safely",
              chores._refused_today(settings, 111) == set()
              and chores._club_day(settings, time.time())["requested"] == [222])
    finally:
        chores._ping, chores._donate_phase = real_ping, real_phase


def _tc(tcid, card, score, dist=None, style=None, dirt=1, turf=7):
    """A trained_chara row: dist = {distance_type: aptitude}, style = {1..4: aptitude}."""
    dist = dist or {}
    style = style or {}
    return {"trained_chara_id": tcid, "card_id": card, "rank_score": score,
            "proper_ground_turf": turf, "proper_ground_dirt": dirt,
            "proper_distance_short": dist.get(1, 1), "proper_distance_mile": dist.get(2, 1),
            "proper_distance_middle": dist.get(3, 1), "proper_distance_long": dist.get(4, 1),
            "proper_running_style_nige": style.get(1, 1), "proper_running_style_senko": style.get(2, 1),
            "proper_running_style_sashi": style.get(3, 1), "proper_running_style_oikomi": style.get(4, 1)}


def test_team_builder() -> None:
    print("team builder: the client's evaluation formula, legal slots, best members")
    a = _tc(1, 100601, 15000, dist={3: 7, 4: 8}, style={2: 7})        # S-rank stayer
    check("member point: A middle, A turf, A style -> full score",
          chores.member_point(a, 3, 2) == 15000, str(chores.member_point(a, 3, 2)))
    check("member point: S long -> 102% x 100% x 100%",
          chores.member_point(a, 4, 2) == 15300, str(chores.member_point(a, 4, 2)))
    check("best style is the highest aptitude", chores.best_style(a) == 2)
    trained = [a,
               _tc(2, 100602, 20000, dist={3: 7}, style={2: 7}),          # SAME uma (1006), better
               _tc(3, 101101, 9000, dist={1: 7}, style={1: 7}),           # sprinter
               _tc(4, 101201, 8000, dist={2: 7}, style={3: 7}, dirt=7),   # mile/dirt
               {**_tc(5, 101301, 30000, dist={3: 8}), "owner_viewer_id": 999}]  # borrowed copy
    array, total = chores.best_team(trained, team_class=1)
    members = {s["trained_chara_id"] for s in array if s["trained_chara_id"]}
    check("15 slots, empty ones zeroed", len(array) == 15
          and all(s["running_style"] == 0 for s in array if not s["trained_chara_id"]), str(array))
    check("class 1 fields one member per distance",
          all(s["member_id"] == 1 for s in array if s["trained_chara_id"]), str(array))
    check("one uma once: the better 1006 copy, not both", 2 in members and 1 not in members, str(members))
    check("a borrowed copy is never fielded", 5 not in members, str(members))
    check("total is the sum of its members",
          total == sum(chores.member_point({c["trained_chara_id"]: c for c in trained}[s["trained_chara_id"]],
                                           s["distance_type"], s["running_style"])
                       for s in array if s["trained_chara_id"]), str(total))
    _, total3 = chores.best_team(trained, team_class=5)
    check("a higher class can field more", total3 >= total, f"{total3} vs {total}")

    # 6 umas, class 2: four strong stayers + one weak sprinter + one weak dirt runner.
    # Greedy-by-score alone stacks middle/long and leaves short and dirt EMPTY.
    few = [_tc(10 + i, 110001 + 100 * i, 15000, dist={3: 7, 4: 7}, style={2: 7}) for i in range(4)]
    few += [_tc(20, 120001, 3000, dist={1: 7}, style={1: 7}),
            _tc(21, 120101, 3000, dist={2: 7}, style={1: 7}, dirt=7)]
    arr, _ = chores.best_team(few, team_class=2)
    covered = {s["distance_type"] for s in arr if s["trained_chara_id"]}
    check("every distance gets a member before any is stacked", covered == {1, 2, 3, 4, 5},
          str(sorted(covered)))
    check("current team scored by the same formula",
          chores.team_point(trained, [{"distance_type": 3, "member_id": 1, "trained_chara_id": 2,
                                       "running_style": 2}]) == 20000)


def test_team_formula_matches_capture() -> None:
    print("team formula: reproduces the captured team_evaluation_point (skipped without dumps)")
    import json
    run = Path(r"C:/Users/victo/OneDrive/Documents/UmaDumpy-main/dumps/20260713_192951/transactions")
    if not run.is_dir():
        print("  skip (no dumps on this machine)")
        return
    edit = json.loads((run / "0007.json").read_text(encoding="utf-8"))["request"]["data"]
    edit = edit.get("data", edit)
    trained = json.loads((run / "0010.json").read_text(encoding="utf-8"))["response"]["data"]["data"]
    trained = trained.get("trained_chara_array") or trained.get("trained_chara") or []
    got = chores.team_point(trained, edit["team_data_array"])
    check(f"team_point {got:,} == captured {edit['team_evaluation_point']:,}",
          got == edit["team_evaluation_point"])


def test_team_after_ace() -> None:
    print("team: a just-finished ace replaces a weaker member right away")
    import os
    os.environ["AIC_TEAM"] = "1"
    os.environ.pop("AIC_TEAM_ONLY", None)
    try:
        old = _tc(7, 100601, 6_000, dist={3: 7}, style={2: 7})
        ace_ = _tc(8, 100701, 17_000, dist={3: 7}, style={2: 7})          # the new Ace Run
        idx = {"trained_chara": [old, ace_],
               "team_data_array": [{"distance_type": 3, "member_id": 1,
                                    "trained_chara_id": 7, "running_style": 2}]}
        c = FakeClient()
        chores.team_after_ace(_session(c, load_index=idx), chores.log, "McKing")
        sent = (c.args.get("team_stadium/team_edit") or {}).get("team_data_array") or []
        check("team_edit sent with the ace in the medium slot",
              any(s["trained_chara_id"] == 8 and s["distance_type"] == 3 for s in sent), str(sent))
        check("no battles from this check (RP is the chore's job)",
              "team_stadium/start" not in c.calls, str(c.calls))
        same = FakeClient()
        # the ace is already fielded and nothing else could improve the team
        idx2 = {"trained_chara": [ace_],
                "team_data_array": [{"distance_type": 3, "member_id": 1,
                                     "trained_chara_id": 8, "running_style": 2}]}
        chores.team_after_ace(_session(same, load_index=idx2), chores.log)
        check("team already optimal -> no team_edit", "team_stadium/team_edit" not in same.calls,
              str(same.calls))

        class NoTerm(FakeClient):
            def call(self, ep, args=None):
                self.calls.append(ep)
                return {"data": {"term_state": 0}, "data_headers": {"result_code": 1}}
        n = NoTerm()
        chores.team_after_ace(_session(n, load_index=idx))
        check("no active term -> no team_edit", "team_stadium/team_edit" not in n.calls)
    finally:
        os.environ.pop("AIC_TEAM", None)
    off = FakeClient()
    chores.team_after_ace(_session(off))
    check("off unless AIC_TEAM", off.calls == [], str(off.calls))


def test_team_trials_flow() -> None:
    print("team: correct bodies, weakest opponent by evaluation, every RP spent")
    import os
    os.environ["AIC_TEAM"] = "1"
    os.environ.pop("AIC_TEAM_ONLY", None)
    try:
        trained = [_tc(7, 100601, 15000, dist={3: 7}, style={2: 7})]
        idx = {"trained_chara": trained, "team_data_array": [], "rp_info": {"current_rp": 3}}
        c = FakeClient()
        c.rp = 3
        ok = chores.team_trials(_session(c, load_index=idx))
        check("it ran", ok is True)
        check("team_edit sent the better team",
              (c.args.get("team_stadium/team_edit") or {}).get("team_evaluation_point") == 15000,
              str(c.args.get("team_stadium/team_edit")))
        check("opponent_list sends no body", c.args.get("team_stadium/opponent_list") == {},
              str(c.args.get("team_stadium/opponent_list")))
        chosen = (c.args.get("team_stadium/decide_frame_order") or {}).get("opponent_info") or {}
        check("decide_frame_order carries the chosen opponent, the WEAKEST by evaluation",
              chosen.get("evaluation_point") == 22905, str(chosen))
        check("start sends item_id_array", c.args.get("team_stadium/start") == {"item_id_array": []},
              str(c.args.get("team_stadium/start")))
        check("replay_check sends round 5", c.args.get("team_stadium/replay_check") == {"round": 5},
              str(c.args.get("team_stadium/replay_check")))
        check("one battle per RP (3)", c.calls.count("team_stadium/start") == 3, str(c.calls))

        # the team already set is as good -> no team_edit
        same = FakeClient()
        idx2 = {**idx, "team_data_array": [{"distance_type": 3, "member_id": 1,
                                            "trained_chara_id": 7, "running_style": 2}],
                "rp_info": {"current_rp": 0}}
        chores.team_trials(_session(same, load_index=idx2))
        check("no team_edit when the set team is already best",
              "team_stadium/team_edit" not in same.calls, str(same.calls))
        check("no battle with 0 RP", "team_stadium/start" not in same.calls, str(same.calls))

        # no active term -> skip early, no battle
        class NoTerm(FakeClient):
            def call(self, ep, args=None):
                self.calls.append(ep)
                return {"data": {"term_state": 0}, "data_headers": {"result_code": 1}}
        n = NoTerm()
        chores.team_trials(_session(n))
        check("skips when no active term", "team_stadium/start" not in n.calls, str(n.calls))

        os.environ["AIC_TEAM_ONLY"] = "555"
        other = FakeClient()
        check("AIC_TEAM_ONLY skips other accounts",
              chores.team_trials(_session(other)) is False and not other.calls, str(other.calls))
    finally:
        os.environ.pop("AIC_TEAM", None)
        os.environ.pop("AIC_TEAM_ONLY", None)
    check("off unless AIC_TEAM", chores.team_trials(_session(FakeClient())) is False)


def test_daily_legend_flow() -> None:
    print("legend: server-resolved race, race_start carries no result array")
    c = FakeClient()
    ok = chores.daily_legend(_session(c))
    check("it ran", ok is True)
    for ep in ("daily_legend_race/index", "daily_legend_race/race_entry",
               "daily_legend_race/reflect_item_effect", "daily_legend_race/race_start",
               "daily_legend_race/replay_check"):
        check(f"called {ep}", ep in c.calls, str(c.calls))
    check("race_start sends NO race_result_array (server-resolved)",
          c.args.get("daily_legend_race/race_start") == {}, str(c.args.get("daily_legend_race/race_start")))
    check("reflect sets running_style + is_short",
          c.args.get("daily_legend_race/reflect_item_effect") == {"running_style": 1, "is_short": 0},
          str(c.args.get("daily_legend_race/reflect_item_effect")))

    # already played -> no race
    class Played(FakeClient):
        def call(self, ep, args=None):
            self.calls.append(ep); self.args[ep] = args
            if ep == "daily_legend_race/index":
                return {"data": {"daily_legend_race_record_array": [{"is_played": 1}]}, "data_headers": {"result_code": 1}}
            return {"data": {}, "data_headers": {"result_code": 1}}
    p = Played()
    chores.daily_legend(_session(p))
    check("skips when already played today", "daily_legend_race/race_entry" not in p.calls, str(p.calls))


def test_daily_chore_records_reset_day() -> None:
    print("a daily chore records the reset-day marker, not a raw timestamp")
    import os
    os.environ["AIC_CHORES"] = "1"; os.environ.pop("AIC_CHORES_ONLY", None)
    tmp = Path(__import__("tempfile").mkdtemp())
    settings = _settings(tmp)
    target = types.SimpleNamespace(trainer_id=555, label="LegendTester")
    # Inject a daily chore (real legend is parked out of _CHORES) to test the mark.
    chores._CHORES["_dailytest"] = (lambda s, l, lbl="": None,
                                    lambda rec, now: chores._due_daily(rec, "_dailytest", now),
                                    chores.reset_day)
    try:
        chores.run_due(_session(FakeClient()), settings, target)
        rec = chores._load(settings).get("555", {})
        check("daily chore recorded as today's reset-day string",
              rec.get("_dailytest") == chores.reset_day(), str(rec.get("_dailytest")))
        check("and is NOT due again the same day",
              not chores._due_daily(rec, "_dailytest", time.time()), str(rec))
    finally:
        chores._CHORES.pop("_dailytest", None)
        os.environ.pop("AIC_CHORES", None)


def test_run_due_records_even_on_failure() -> None:
    print("a chore that RAISES still records its attempt (no 60s re-fire storm)")
    import os

    os.environ["AIC_CHORES"] = "1"
    os.environ.pop("AIC_CHORES_ONLY", None)
    tmp = Path(__import__("tempfile").mkdtemp())
    settings = _settings(tmp)
    target = types.SimpleNamespace(trainer_id=999, label="Tester")

    # Force the club runner to raise.
    orig = chores._CHORES["club"]
    def boom(session, log_, label=""): raise RuntimeError("kaboom")
    chores._CHORES["club"] = (boom, orig[1], orig[2])
    try:
        chores.run_due(_session(FakeClient()), settings, target)
        state = chores._load(settings)
        check("attempt recorded despite the raise", "club" in state.get("999", {}), str(state))
        # immediately due again? no - it was just recorded
        rec = state["999"]
        check("not due again right after", not chores._due_periodic(rec, "club", chores.CLUB_EVERY_SECONDS, time.time()))
    finally:
        chores._CHORES["club"] = orig
        os.environ.pop("AIC_CHORES", None)


def test_one_account_rollout_gate() -> None:
    print("AIC_CHORES_ONLY limits chores to a single account")
    import os
    os.environ["AIC_CHORES"] = "1"
    os.environ["AIC_CHORES_ONLY"] = "943517106211"
    tmp = Path(__import__("tempfile").mkdtemp())
    settings = _settings(tmp)
    ran = {"n": 0}
    orig = chores._CHORES["club"]
    chores._CHORES["club"] = (lambda s, l, lbl="": ran.__setitem__("n", ran["n"] + 1),
                              lambda rec, now: True, lambda now: now)
    try:
        chores.run_due(_session(FakeClient()), settings, types.SimpleNamespace(trainer_id=111, label="Other"))
        check("other account skipped", ran["n"] == 0)
        chores.run_due(_session(FakeClient()), settings, types.SimpleNamespace(trainer_id=943517106211, label="Chosen"))
        check("chosen account ran", ran["n"] == 1)
    finally:
        chores._CHORES["club"] = orig
        os.environ.pop("AIC_CHORES", None)
        os.environ.pop("AIC_CHORES_ONLY", None)


# --------------------------------------------------------------------------- #
# gift box + daily races (shaped like capture dumps/20261006_194059)
# --------------------------------------------------------------------------- #
def _present(pid, rtype, rid, n):
    return {"present_id": pid, "state": 0, "reward_type": rtype, "reward_id": rid,
            "reward_count": n, "message_id": 50, "reward_limit_time": 4133948399}


class GiftClient(FakeClient):
    """FakeClient + the present/ and daily_race endpoints. ``total`` overrides the
    box's own unclaimed count (no_receive_present_num) for the >100 case;
    ``want`` is the count receive_all accepts (others are refused 5401)."""
    def __init__(self, box=None, cleared=(5,), refuse=None, total=None, want=None, **kw):
        super().__init__(**kw)
        self.box, self.cleared = list(box or []), set(cleared)
        self.refuse_ep, self.total, self.want = refuse or {}, total, want
        self.bodies = []

    def call(self, ep, args=None):
        self.bodies.append((ep, args))
        if ep not in ("present/index", "present/receive_all", "daily_race/index",
                      "daily_race_skip/race_skip"):
            return super().call(ep, args)
        self.calls.append(ep)
        self.args[ep] = args
        if ep in self.refuse_ep:
            raise Refused(self.refuse_ep[ep])
        ok = {"result_code": 1}
        if ep == "present/index":
            n = len(self.box) if self.total is None else self.total
            return {"data": {"present_array": self.box[:100], "no_receive_present_num": {
                "no_time_limit_present_num": n, "time_limit_present_num": 0}}, "data_headers": ok}
        if ep == "present/receive_all":
            if self.want is not None and args["current_rest_present_num"] != self.want:
                raise Refused(5401)
            items, fcoin = {}, 0
            taken, self.box = self.box[:100], self.box[100:]     # the server's 100 a press
            self.total = None
            for p in taken:
                if p["reward_type"] == 90:
                    fcoin += p["reward_count"]
                else:
                    items[p["reward_id"]] = items.get(p["reward_id"], 0) + p["reward_count"]
            return {"data": {"reward_summary_info": {
                "add_item_list": [{"item_id": i, "number": k} for i, k in items.items()],
                "add_fcoin": fcoin}, "receive_present_num": len(taken),
                "rest_present_num": len(self.box)}, "data_headers": ok}
        if ep == "daily_race/index":
            return {"data": {"daily_race_record_array": [
                {"daily_race_id": i, "is_cleared": int(i in self.cleared),
                 "is_played": int(i in self.cleared)} for i in range(1, 9)], "purchase_num": 0},
                "data_headers": ok}
        run = {"rank": 1, "reward_summary_info": {"add_item_list": [{"item_id": 110, "number": 550}],
                                                  "add_fcoin": 0}}
        return {"data": {"race_reward_array": [run] * args["race_skip_count"],
                         "item_info_array": [{"item_id": 96, "number": 0}]}, "data_headers": ok}


def _tr(tcid, score, style=3, owner=0):
    return {"trained_chara_id": tcid, "rank_score": score, "rank": score // 1000,
            "running_style": style, "owner_viewer_id": owner,
            "proper_running_style_nige": 2, "proper_running_style_senko": 7,
            "proper_running_style_sashi": 7, "proper_running_style_oikomi": 4}


def _idx(tickets=3, state=0, trained=None, records=None):
    info = {"state": state, "trained_chara_id": 0}
    if records is not None:
        info["daily_race_record_array"] = records
    return {"item_list": [{"item_id": 96, "number": tickets}, {"item_id": 7, "number": 100}],
            "daily_race_playing_info": info,
            "trained_chara": trained if trained is not None
            else [_tr(3, 9000), _tr(8, 17000), _tr(9, 30000, owner=4242)]}


def _pings():
    msgs, real = [], chores._ping
    chores._ping = lambda who, did, title="Club": msgs.append((title, list(did)))
    return msgs, real


def test_gifts_flow() -> None:
    print("gifts: index -> receive_all -> index, claimed items folded into load_index")
    box = [_present(1, 90, 43, 1000), _present(2, 90, 43, 50), _present(3, 30, 110, 10000),
           _present(4, 40, 96, 2), _present(5, 20, 32, 1)]
    msgs, real = _pings()
    try:
        c = GiftClient(box=box)
        s = _session(c, load_index=_idx(tickets=1))
        check("it ran", chores.gifts(s, chores.log, "McKing") is True)
        check("the client's calls: present/index, receive_all, present/index (never load/index)",
              c.calls == ["present/index", "present/receive_all", "present/index"], str(c.calls))
        check("receive_all body is the captured one, count = unclaimed total",
              c.args["present/receive_all"] == {"time_filter_type": 0, "category_filter_type": [0],
                                                "is_asc": True, "current_rest_present_num": 5},
              str(c.args["present/receive_all"]))
        check("present/index body is the captured one",
              c.bodies[0][1] == {"time_filter_type": 0, "category_filter_type": [0],
                                 "offset": 0, "limit": 100, "is_asc": True}, str(c.bodies[0][1]))
        items = {r["item_id"]: r["number"] for r in s.load_index["item_list"]}
        check("claimed items folded into load_index (tickets 1+2, new SP, shoes untouched)",
              items.get(96) == 3 and items.get(110) == 10000 and items.get(7) == 100, str(items))
        check("discord says count and carats",
              msgs == [("Gifts", ["claimed 5 gift(s) (+1,050 carats)"])], str(msgs))
        cc = GiftClient(box=box)
        cc.item_map, cc.coin_info = {96: 1, 32: 0}, {"fcoin": 200, "coin": 7}
        sc = _session(cc, load_index={**_idx(tickets=1), "coin_info": cc.coin_info})
        chores.gifts(sc)
        check("the client's own caches follow too (TP recovery declares from them)",
              cc.item_map.get(96) == 3 and cc.item_map.get(32) == 1, str(cc.item_map))
        check("+1,050 free carats counted once in the shared coin_info, paid untouched",
              cc.coin_info == {"fcoin": 1250, "coin": 7}, str(cc.coin_info))
        e = GiftClient(box=[])
        msgs.clear()
        check("empty box -> index only, True, no discord line",
              chores.gifts(_session(e)) is True and e.calls == ["present/index"] and not msgs, str(e.calls))
        msgs.clear()
        many = GiftClient(box=[_present(i, 90, 43, 10) for i in range(230)])
        check("230 gifts: Claim All pressed until empty (100 + 100 + 30)",
              chores.gifts(_session(many), chores.log, "Mizura") is True and many.box == []
              and [b[1]["current_rest_present_num"] for b in many.bodies if b[0] == "present/receive_all"]
              == [230, 130, 30], str([b for b in many.bodies if b[0] == "present/receive_all"]))
        check("...one discord line for the lot", msgs == [("Gifts", ["claimed 230 gift(s) (+2,300 carats)"])],
              str(msgs))
        stuck = GiftClient(box=[_present(i, 20, 32, 1) for i in range(700)])
        chores.gifts(_session(stuck))
        check("at most GIFT_ROUNDS presses a visit", stuck.calls.count("present/receive_all") == chores.GIFT_ROUNDS,
              str(stuck.calls.count("present/receive_all")))
        big = GiftClient(box=[_present(i, 20, 32, 1) for i in range(100)], total=150, want=100)
        check("box over 100: count refused -> retried once with the listed count",
              chores.gifts(_session(big)) is True and
              [b[1].get("current_rest_present_num") for b in big.bodies if b[0] == "present/receive_all"]
              == [150, 100], str(big.bodies))
        r1 = GiftClient(box=box, refuse={"present/index": 102})
        check("index refused -> False, no raise", chores.gifts(_session(r1)) is False)
        r2 = GiftClient(box=box, refuse={"present/receive_all": 5401})
        check("receive_all refused -> True, no raise, no follow-up",
              chores.gifts(_session(r2)) is True and r2.calls == ["present/index", "present/receive_all"],
              str(r2.calls))
    finally:
        chores._ping = real


def test_daily_race_flow() -> None:
    print("daily race: one multi race with every ticket on a cleared race, best own trainee")
    msgs, real = _pings()
    try:
        c = GiftClient(cleared=(5,))
        c.item_map = {96: 3, 110: 100}
        sr = _session(c, load_index=_idx(tickets=3))
        check("it ran", chores.daily_race(sr, chores.log, "McKing"))
        items = {r["item_id"]: r["number"] for r in sr.load_index["item_list"]}
        check("tickets spent and rewards folded into both copies (load_index + client)",
              items.get(96) == 0 and items.get(110) == 1650
              and c.item_map == {96: 0, 110: 1750}, f"{items} {c.item_map}")
        check("calls are exactly index then race_skip (the client's pair)",
              c.calls == ["daily_race/index", "daily_race_skip/race_skip"] and c.args["daily_race/index"] == {},
              str(c.calls))
        check("race_skip body: cleared race, best OWN trainee, all tickets, its own style",
              c.args["daily_race_skip/race_skip"] == {"daily_race_id": 5, "trained_chara_id": 8,
                                                      "race_skip_count": 3, "client_own_num": 3,
                                                      "running_style": 3},
              str(c.args["daily_race_skip/race_skip"]))
        check("discord line: runs, wins, rewards", len(msgs) == 1 and msgs[0][0] == "Daily Race"
              and msgs[0][1][0].startswith("multi race x3: 3 win(s)"), str(msgs))
        p = GiftClient(cleared=(2, 6, 7))
        chores.daily_race(_session(p, load_index=_idx(tickets=2)))
        check("the easiest cleared race first (default 5,1,6,2,...)",
              (p.args.get("daily_race_skip/race_skip") or {}).get("daily_race_id") == 6,
              str(p.args.get("daily_race_skip/race_skip")))
        six = GiftClient()
        chores.daily_race(_session(six, load_index=_idx(tickets=6)))
        b = six.args["daily_race_skip/race_skip"]
        check("6 held -> count 6, own_num 6 (never more than held)",
              b["race_skip_count"] == 6 == b["client_own_num"], str(b))
        ns = GiftClient()
        chores.daily_race(_session(ns, load_index=_idx(trained=[_tr(8, 17000, style=0)])))
        check("no stored style -> best aptitude", ns.args["daily_race_skip/race_skip"]["running_style"] == 2,
              str(ns.args["daily_race_skip/race_skip"]))
        none_cleared = [{"daily_race_id": i, "is_cleared": 0} for i in range(1, 9)]
        for why, idx in (("no tickets", _idx(tickets=0)), ("race left open", _idx(state=2)),
                         ("no own trainee", _idx(trained=[_tr(9, 30000, owner=4242)])),
                         ("load/index shows nothing cleared", _idx(records=none_cleared))):
            z = GiftClient()
            chores.daily_race(_session(z, load_index=idx))
            check(f"{why} -> no API call", z.calls == [], str(z.calls))
        n = GiftClient(cleared=())
        check("nothing cleared on the board -> index only, tickets kept",
              chores.daily_race(_session(n, load_index=_idx())) is True and n.calls == ["daily_race/index"],
              str(n.calls))
        r1 = GiftClient(refuse={"daily_race/index": 102})
        check("index refused -> False", chores.daily_race(_session(r1, load_index=_idx())) is False)
        r2 = GiftClient(refuse={"daily_race_skip/race_skip": 5401})
        check("race_skip refused -> True", chores.daily_race(_session(r2, load_index=_idx())) is True)
        seen = set()
        for cl in (c, p, six, ns, n, r1, r2):
            seen |= set(cl.calls)
        check("only daily_race/index and race_skip ever (no recovery_ticket = no carats)",
              seen <= {"daily_race/index", "daily_race_skip/race_skip"}, str(seen))
    finally:
        chores._ping = real


def test_gifts_and_races_through_run_due() -> None:
    print("run_due: off by default, gifts first, gifted tickets raced, once a day, never on ace_once")
    import os
    os.environ["AIC_CHORES"] = "1"
    real_club = chores._CHORES["club"]
    chores._CHORES["club"] = (lambda s, l, lbl="": None, lambda rec, now: False, lambda now: now)
    msgs, real = _pings()
    try:
        settings = _settings(Path(__import__("tempfile").mkdtemp()))
        target = types.SimpleNamespace(trainer_id=555, label="McKing")
        off = GiftClient(box=[_present(1, 90, 43, 50)])
        chores.run_due(_session(off, load_index=_idx()), settings, target)
        check("AIC_GIFTS / AIC_DAILY_RACE off by default -> no call, nothing recorded",
              off.calls == [] and not chores._load(settings).get("555"), str(off.calls))
        os.environ.update({"AIC_GIFTS": "1", "AIC_DAILY_RACE": "1"})
        c = GiftClient(box=[_present(1, 40, 96, 2)])
        chores.run_due(_session(c, load_index=_idx(tickets=0)), settings, target)
        check("gifts ran before daily_race, and the 2 gifted tickets were raced",
              c.calls.index("present/receive_all") < c.calls.index("daily_race_skip/race_skip")
              and c.args["daily_race_skip/race_skip"]["race_skip_count"] == 2, str(c.calls))
        rec = chores._load(settings).get("555", {})
        check("both marked with today's reset-day",
              rec.get("gifts") == chores.reset_day() == rec.get("daily_race"), str(rec))
        again = GiftClient(box=[_present(2, 90, 43, 50)])
        chores.run_due(_session(again, load_index=_idx()), settings, target)
        check("not again the same game-day", again.calls == [], str(again.calls))
        once = GiftClient(box=[_present(3, 90, 43, 50)])
        chores.run_due(_session(once, load_index=_idx()), settings,
                       types.SimpleNamespace(trainer_id=777, label="ela", ace_once=5))
        check("a one-time Ace Run account is left alone", once.calls == [], str(once.calls))
        os.environ["AIC_DAILY_RACE_ONLY"] = "222"
        o = GiftClient()
        chores.run_due(_session(o, load_index=_idx()), settings,
                       types.SimpleNamespace(trainer_id=111, label="A"))
        check("AIC_DAILY_RACE_ONLY another account -> no race here",
              "daily_race_skip/race_skip" not in o.calls, str(o.calls))
    finally:
        chores._ping = real
        chores._CHORES["club"] = real_club
        for k in ("AIC_CHORES", "AIC_GIFTS", "AIC_DAILY_RACE", "AIC_DAILY_RACE_ONLY"):
            os.environ.pop(k, None)


def main() -> int:
    for fn in (test_periodic_gate, test_daily_reset_boundary, test_donate_batch,
               test_circle_view, test_club_stock_stays_true,
               test_club_request_then_donate_phases, test_club_runs_full_flow,
               test_club_refused_mixed_batch,
               test_team_builder,
               test_team_formula_matches_capture, test_team_after_ace, test_team_trials_flow,
               test_daily_legend_flow, test_daily_chore_records_reset_day,
               test_run_due_records_even_on_failure, test_one_account_rollout_gate,
               test_gifts_flow, test_daily_race_flow, test_gifts_and_races_through_run_due):
        fn()
        print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
        return 1
    print("all chore checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
