"""Offline checks driven by real captured traffic.

No network, no credentials. Synthetic checks always run. Two optional inputs
exercise the code against genuine payloads:

    AIC_TEST_CAPTURE   a decoded ``load/index`` response
    AIC_TEST_DUMPS     a UmaDumpy ``dumps/`` directory

    python tests/test_offline.py
"""

from __future__ import annotations

import glob
import json
import os
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aic import independent as idl  # noqa: E402
from aic import selection as sel  # noqa: E402
from aic import succession as succession_mod  # noqa: E402
from aic.gamedata import GameData, normalize_card_name  # noqa: E402
from aic.state import (  # noqa: E402
    KIND_IDLE,
    KIND_NONE,
    KIND_NORMAL,
    ServerClock,
    format_duration,
    parse_naive,
    read_account,
    read_career,
    read_progress_info,
)

ICARUS_ROOT = Path(
    os.environ.get("AIC_ICARUS_ROOT")
    or Path(__file__).resolve().parent.parent.parent / "Icarus-Dev-Build-Private-main"
)

FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label} {detail}")
        FAILURES.append(label)


def unwrap(path: str) -> dict:
    """The decoded body of a UmaDumpy / Icarus capture file."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    body = raw.get("data", raw)
    return body if "data_headers" in body or "response_code" in body else raw


# --------------------------------------------------------------------------- #

def test_name_normalization() -> None:
    print("name normalization")
    check("strips parenthetical", normalize_card_name("Oguri Cap (Christmas)") == "oguricap")
    check("case/space insensitive", normalize_card_name("Gold Ship") == "goldship")
    check("empty stays empty", normalize_card_name(None) == "")
    check("alt matches base",
          normalize_card_name("Special Week (Alt 2)") == normalize_card_name("Special Week"))


def test_server_clock() -> None:
    print("server clock")
    # Real values from 20260730_155008 load/index.
    response = {
        "data_headers": {"servertime": 1785451821},
        "data": {"user_info": {"update_time": "2026-07-30 21:39:43"}},
    }
    clock = ServerClock()
    clock.observe(response)
    check("offset derived", clock.derived is True)
    check("offset is +3600", clock.offset == 3600, f"(got {clock.offset})")

    end = clock.to_epoch("2026-07-30 22:29:43")
    remaining = end - clock.now()
    check("remaining ~39m22s", 2360 <= remaining <= 2364, f"(got {remaining})")

    check("raw epoch passes through", clock.to_epoch(1800000000) == 1800000000.0)
    check("small int rejected", clock.to_epoch(42) is None)
    check("bool rejected", clock.to_epoch(True) is None)
    check("garbage rejected", clock.to_epoch("not a time") is None)
    check("slash format parsed", parse_naive("2026/07/22 04:00:00") is not None)

    stale = ServerClock()
    stale.observe({"data_headers": {"servertime": 1785451821},
                   "data": {"user_info": {"update_time": "2020-01-01 00:00:00"}}})
    check("far-out anchor ignored", stale.derived is False and stale.offset == 3600)

    noanchor = ServerClock()
    noanchor.observe({"data_headers": {"servertime": 1785451821}, "data": {}})
    check("servertime recorded without anchor", noanchor.servertime == 1785451821.0)


def test_clock_anchor_staleness() -> None:
    """Regression: update_time is stamped when the career starts, so deep into
    a run the anchor is ~40 min stale. Rounding sent that to the wrong hour and
    inflated every countdown by 3600s, which the duration clamp then showed as
    a full-length run remaining."""
    print("server clock / stale anchor")

    # 38 minutes into a 50-minute run, true offset +3600.
    start_naive = 1785447583          # timegm("2026-07-30 21:39:43")
    servertime = start_naive + 3600 + 2280
    payload = {
        "data_headers": {"servertime": servertime},
        "data": {
            "user_info": {"update_time": "2026-07-30 21:39:43"},
            "idle_single_mode_load_info": {
                "start_time": "2026-07-30 21:39:43",
                "end_time": "2026-07-30 22:29:43",
                "single_mode_chara_light": {"card_id": 106801, "scenario_id": 3},
            },
        },
    }

    clock = ServerClock()
    clock.observe(payload)
    check("calibrated from the career window", clock.calibration == "career window")
    check("offset is +3600 not +7200", clock.offset == 3600, f"(got {clock.offset})")

    state = read_career(payload["data"], clock)
    remaining = state.seconds_remaining or 0
    check("reports ~12m left, not the full 50m",
          710 <= remaining <= 725, f"(got {remaining:.0f}s)")
    check("not pegged at the duration", abs(remaining - 3000) > 60)

    # The anchor path alone must also floor rather than round.
    anchor_only = ServerClock()
    anchor_only.observe({"data_headers": {"servertime": servertime},
                         "data": {"user_info": {"update_time": "2026-07-30 21:39:43"}}})
    check("anchor floors a 38m-stale value", anchor_only.offset == 3600,
          f"(got {anchor_only.offset})")

    fresh = ServerClock()
    fresh.observe({"data_headers": {"servertime": start_naive + 3600 + 5},
                   "data": {"user_info": {"update_time": "2026-07-30 21:39:43"}}})
    check("anchor handles a fresh value", fresh.offset == 3600)

    # A window shorter than an hour admits exactly one whole-hour offset.
    for elapsed in (0, 600, 1500, 2900):
        c = ServerClock(offset=-9 * 3600)
        c.observe({
            "data_headers": {"servertime": start_naive + 3600 + elapsed},
            "data": {"idle_single_mode_load_info": {
                "start_time": "2026-07-30 21:39:43",
                "end_time": "2026-07-30 22:29:43",
                "single_mode_chara_light": {"card_id": 1},
            }},
        })
        check(f"window unique at +{elapsed}s", c.offset == 3600, f"(got {c.offset})")

    no_time = ServerClock()
    check("declines with no servertime",
          no_time.calibrate_from_window("2026-07-30 21:39:43",
                                        "2026-07-30 22:29:43") is False)

    # A window of an hour or more is not uniquely solvable.
    wide = ServerClock()
    wide.servertime = start_naive + 3600
    check("declines on a >=1h window",
          wide.calibrate_from_window("2026-07-30 21:39:43",
                                     "2026-07-30 22:39:43") is False)

    # A run that expired hours ago yields the same unique window solution as a
    # live run under a larger offset - indistinguishable from these fields. The
    # source ranking is what keeps that from corrupting a better calibration.
    expired_args = ("2026-07-30 21:39:43", "2026-07-30 22:29:43")

    naive_clock = ServerClock()
    naive_clock.servertime = start_naive + 3600 + 4 * 3600
    naive_clock.calibrate_from_window(*expired_args)
    check("expired run does fool a bare clock (documented limitation)",
          naive_clock.offset != 3600)

    pinned = ServerClock()
    pinned.servertime = start_naive + 3600 + 4 * 3600
    pinned.calibrate_from_fresh("2026-07-30 21:39:43")   # would be wrong too
    pinned.offset, pinned.calibration = 3600, "fresh start_time"
    pinned.calibrate_from_window(*expired_args)
    check("a fresh-pinned clock is not overwritten by a window",
          pinned.offset == 3600 and pinned.calibration == "fresh start_time")

    cached = ServerClock(offset=3600, calibration="cached")
    cached.servertime = start_naive + 3600 + 4 * 3600
    cached.calibrate_from_window(*expired_args)
    check("a window still refines a merely-cached clock",
          cached.calibration == "career window")


def test_clock_source_ranking() -> None:
    print("server clock / source ranking")
    start_naive = 1785447583

    c = ServerClock()
    check("bare clock is not derived", c.derived is False)
    check("bare clock reports assumed", c.calibration == "assumed")

    c = ServerClock(offset=3600, calibration="cached")
    check("cached clock counts as derived", c.derived is True)

    fresh = ServerClock()
    fresh.servertime = start_naive + 3600 + 3
    check("fresh calibration accepted",
          fresh.calibrate_from_fresh("2026-07-30 21:39:43") is True)
    check("fresh offset correct", fresh.offset == 3600)
    check("fresh source recorded", fresh.calibration == "fresh start_time")

    # A "fresh" value that is nowhere near a whole-hour boundary is not fresh.
    bogus = ServerClock()
    bogus.servertime = start_naive + 3600 + 1800
    check("half-hour-off value rejected as fresh",
          bogus.calibrate_from_fresh("2026-07-30 21:39:43") is False)
    check("rejected value leaves clock alone", bogus.calibration == "assumed")

    # Anchor must not clobber a fresh pin.
    pinned = ServerClock()
    pinned.servertime = start_naive + 3600 + 3
    pinned.calibrate_from_fresh("2026-07-30 21:39:43")
    pinned.servertime = start_naive + 3600 + 2280
    pinned._calibrate_from_anchor("2026-07-30 20:39:43")   # an hour staler
    check("anchor cannot downgrade a fresh pin",
          pinned.offset == 3600 and pinned.calibration == "fresh start_time")


def test_clock_persistence() -> None:
    print("server clock / persistence")
    import tempfile
    from types import SimpleNamespace

    from aic.state import load_clock, save_clock

    with tempfile.TemporaryDirectory() as tmp:
        settings = SimpleNamespace(state_dir=Path(tmp))

        check("missing cache gives a default clock",
              load_clock(settings).calibration == "assumed")

        good = ServerClock(offset=3600, calibration="career window")
        save_clock(settings, good)
        restored = load_clock(settings)
        check("offset round-trips", restored.offset == 3600)
        check("restored clock is marked cached", restored.calibration == "cached")

        # An assumed clock must not overwrite a real cached value.
        save_clock(settings, ServerClock(offset=-99999))
        check("assumed clock is not persisted", load_clock(settings).offset == 3600)
        save_clock(settings, ServerClock(offset=7200, calibration="cached"))
        check("re-saving a cached clock is a no-op",
              load_clock(settings).offset == 3600)

        (Path(tmp) / "server_clock.json").write_text('{"offset": 1234}', encoding="utf-8")
        check("non-hour offset rejected", load_clock(settings).calibration == "assumed")
        (Path(tmp) / "server_clock.json").write_text('{"offset": 360000}', encoding="utf-8")
        check("out-of-range offset rejected",
              load_clock(settings).calibration == "assumed")
        (Path(tmp) / "server_clock.json").write_text("not json", encoding="utf-8")
        check("corrupt cache rejected", load_clock(settings).calibration == "assumed")


def test_career_detection() -> None:
    print("career detection")
    clock = ServerClock()
    clock.observe({"data_headers": {"servertime": 1785451821},
                   "data": {"user_info": {"update_time": "2026-07-30 21:39:43"}}})

    check("empty means idle-none", read_career({}, clock).kind == KIND_NONE)
    check("null chara_light means none",
          read_career({"single_mode_chara_light": None}, clock).active is False)
    check("empty list means none",
          read_career({"single_mode_chara_light": []}, clock).active is False)
    check("null idle info means none",
          read_career({"idle_single_mode_load_info": None}, clock).active is False)

    normal = read_career({"single_mode_chara_light": {
        "card_id": 103201, "turn": 4, "scenario_id": 2, "playing_state": 5,
    }}, clock)
    check("normal career detected", normal.kind == KIND_NORMAL and normal.active)
    check("normal has no clock", normal.seconds_remaining is None)

    idle = read_career({"idle_single_mode_load_info": {
        "playing_state": 1,
        "start_time": "2026-07-30 21:39:43",
        "end_time": "2026-07-30 22:29:43",
        "single_mode_chara_light": {"card_id": 106801, "turn": 1, "scenario_id": 3},
    }}, clock)
    check("idle career detected", idle.kind == KIND_IDLE and idle.active)
    check("idle is_idle", idle.is_idle is True)
    check("idle card parsed", idle.card_id == 106801)
    check("idle scenario parsed", idle.scenario_id == 3)
    check("idle duration 50m", idle.duration == 3000.0, f"(got {idle.duration})")
    check("idle remaining ~39m", 2350 <= (idle.seconds_remaining or 0) <= 2365,
          f"(got {idle.seconds_remaining})")
    check("idle not finished", idle.finished is False)
    check("clock source recorded",
          idle.finish_source == "idle_single_mode_load_info.end_time")

    # Idle takes precedence: the server nulls chara_light during an idle run.
    both = read_career({
        "single_mode_chara_light": None,
        "idle_single_mode_load_info": {
            "start_time": "2026-07-30 21:39:43", "end_time": "2026-07-30 22:29:43",
            "single_mode_chara_light": {"card_id": 1},
        },
    }, clock)
    check("idle wins over null chara_light", both.kind == KIND_IDLE)

    expired = read_career({"idle_single_mode_load_info": {
        "start_time": "2026-07-30 20:00:00", "end_time": "2026-07-30 20:50:00",
        "single_mode_chara_light": {"card_id": 1},
    }}, clock)
    check("expired idle reports 0 left", expired.seconds_remaining == 0.0)
    check("expired idle is finished", expired.finished is True)

    # Regression: after idle_single_mode/end the home block reports the real
    # turn and a completed state. Calling end again answers 205, so this must
    # be detected from load/index alone.
    check("live run is not marked done", idle.simulation_done is False)

    stopped = read_career({"idle_single_mode_load_info": {
        "start_time": "2026-07-30 21:39:43", "end_time": "2026-07-30 22:29:43",
        "single_mode_chara_light": {"card_id": 106801, "turn": 78,
                                    "scenario_id": 3, "state": 3},
    }}, clock)
    check("stopped run detected", stopped.simulation_done is True)
    check("stopped run keeps its real turn", stopped.turn == 78)

    by_state = read_career({"idle_single_mode_load_info": {
        "start_time": "2026-07-30 21:39:43", "end_time": "2026-07-30 22:29:43",
        "single_mode_chara_light": {"card_id": 1, "turn": 1, "state": 3},
    }}, clock)
    check("state==3 alone marks it done", by_state.simulation_done is True)

    by_turn = read_career({"idle_single_mode_load_info": {
        "start_time": "2026-07-30 21:39:43", "end_time": "2026-07-30 22:29:43",
        "single_mode_chara_light": {"card_id": 1, "turn": 78, "state": 0},
    }}, clock)
    check("a high turn alone marks it done", by_turn.simulation_done is True)

    check("normal careers are never marked done",
          read_career({"single_mode_chara_light": {
              "card_id": 1, "turn": 40, "state": 0}}, clock).simulation_done is False)

    # A wrong offset must not report more time left than the run is long.
    skewed = ServerClock(offset=-9 * 3600)
    skewed.observe({"data_headers": {"servertime": 1785451821}, "data": {}})
    clamped = read_career({"idle_single_mode_load_info": {
        "start_time": "2026-07-30 21:39:43", "end_time": "2026-07-30 22:29:43",
        "single_mode_chara_light": {"card_id": 1},
    }}, skewed)
    check("remaining clamped to duration",
          (clamped.seconds_remaining or 0) <= 3000.0,
          f"(got {clamped.seconds_remaining})")


def test_progress_info() -> None:
    print("start-response parsing")
    clock = ServerClock()
    clock.observe({"data_headers": {"servertime": 1785454308},
                   "data": {"user_info": {"update_time": "2026-07-31 00:51:48"}}})
    state = read_progress_info({
        "start_time": "2026-07-31 00:51:48",
        "end_time": "2026-07-31 01:41:48",
        "chara_info": {"card_id": 106801, "turn": 1, "scenario_id": 3, "playing_state": 1},
    }, clock)
    check("progress_info -> idle state", state.kind == KIND_IDLE and state.active)
    check("progress_info duration 50m", state.duration == 3000.0)
    check("progress_info source", state.finish_source == "progress_info.end_time")


def test_schedule() -> None:
    print("training schedule")
    pre = idl.PreStart(
        last_policy={
            "training_policy_ground_type": 1,
            "training_policy_param_rate_set_id": 2,
            "priority_skill_array": [{"priority": 1, "skill_id": 201601}],
        },
        reserved_race_decks=[
            {"deck_num": 1, "deck_name": "Agenda 1",
             "race_array": [{"year": 1, "program_id": 623}] * 36},
            {"deck_num": 2, "deck_name": "Agenda 2",
             "race_array": [{"year": 1, "program_id": 624}] * 16},
        ],
        default_deck_num=0,
    )
    s = idl.build_schedule(pre)
    check("reuses last policy", s.training_policy_param_rate_set_id == 2)
    check("keeps priority skills", len(s.priority_skill_array) == 1)
    check("picks the fullest agenda", len(s.race_array) == 36)

    s2 = idl.build_schedule(pre, race_deck_num=2)
    check("honours --race-agenda", len(s2.race_array) == 16)

    pre_def = idl.PreStart(last_policy=pre.last_policy,
                           reserved_race_decks=pre.reserved_race_decks,
                           default_deck_num=2)
    check("honours account default agenda",
          len(idl.build_schedule(pre_def).race_array) == 16)

    s3 = idl.build_schedule(pre, {"training_policy_ground_type": 2, "race_array": []})
    check("override replaces ground type", s3.training_policy_ground_type == 2)
    check("override replaces races", s3.race_array == [])
    check("override keeps untouched fields", s3.training_policy_param_rate_set_id == 2)

    empty = idl.build_schedule(idl.PreStart())
    check("falls back with no history",
          empty.training_policy_ground_type == 1 and empty.race_array == [])

    payload = s.to_payload()
    check("payload has the four start_info keys",
          set(payload) == {"training_policy_ground_type",
                           "training_policy_param_rate_set_id",
                           "priority_skill_array", "race_array"})

    try:
        pre.race_deck(99)
        check("unknown agenda raises", False)
    except idl.IdleModeError as exc:
        check("unknown agenda raises", "99" in str(exc))


def test_spec_overrides() -> None:
    print("protocol spec")
    spec = idl.Spec()
    check("default pre_start", spec.endpoint("pre_start") == "idle_single_mode/pre_start")
    check("default start", spec.endpoint("start") == "idle_single_mode/start")
    check("default end", spec.endpoint("end") == "idle_single_mode/end")
    check("default progress",
          spec.endpoint("check_progress_log") == "idle_single_mode/check_progress_log")

    over = idl.Spec(endpoints={"start": "idle_single_mode_v2/start"})
    check("override applies", over.endpoint("start") == "idle_single_mode_v2/start")
    check("non-overridden falls back",
          over.endpoint("end") == "idle_single_mode/end")

    shipped = idl.load_spec()
    check("shipped spec loads clean", shipped.endpoints == {} and shipped.use_tp == 0)


def test_tp_probe_order() -> None:
    print("TP cost probing")
    spec = idl.Spec()
    check("probes cheap first", spec.tp_candidates() == (15, 30))
    check("worst case is 30", spec.max_tp_cost == 30)
    check("pinned via CLI", spec.tp_candidates(30) == (30,))
    check("pinned via spec", idl.Spec(use_tp=15).tp_candidates() == (15,))

    check("learned value promoted",
          idl.order_tp_candidates(spec, 0, known=30) == (30, 15))
    check("learned value already first",
          idl.order_tp_candidates(spec, 0, known=15) == (15, 30))
    check("unknown learned value ignored",
          idl.order_tp_candidates(spec, 0, known=7) == (15, 30))
    check("override beats learned value",
          idl.order_tp_candidates(spec, 30, known=15) == (30,))


def test_tp_probe_calls() -> None:
    """A 102 on the cheap price must fall through to the full one."""
    print("TP probe behaviour")

    class _Err(Exception):
        def __init__(self, rc):
            super().__init__(f"API error {rc}")
            self.result_code = rc

    class _Sel:
        card_id = 1; support_card_ids = [1, 2, 3, 4, 5]
        friend_viewer_id = 0; friend_card_id = 0
        parent_id_1 = 1; parent_id_2 = 2; deck_id = 1; running_style = 1

    class _Acct:
        tp_info = {"current_tp": 62}; gold = 1; succession_rank_point = 1
        current_tp = 62

    def run(reject_first: bool, tp_available: int = 62):
        sent = []

        class _Client:
            def call(self, endpoint, body, **kwargs):
                cost = body["single_mode_start_request_common"]["use_tp"]
                sent.append(cost)
                if reject_first and cost == 15:
                    raise _Err(102)
                return {"data": {}}

        class _Session:
            client = _Client()
            clock = ServerClock()

        acct = _Acct()
        acct.current_tp = tp_available
        idl.start(_Session(), idl.Spec(), _Sel(), acct, idl.Schedule(), scenario_id=3)
        return sent

    # A learned offset must be applied to the FIRST call, not discovered again
    # by searching - that is what makes a repeat start one call instead of many.
    import tempfile as _tf
    from types import SimpleNamespace as _NS

    with _tf.TemporaryDirectory() as _tmp:
        _st = _NS(state_dir=Path(_tmp))
        succession_mod.save_offset(_st, 1, 2, -62, viewer_id=7,
                                   trainee_card_id=1)
        idl.save_known_flow(_st, "B")
        seen_points = []

        class _Once:
            def call(self, endpoint, body, **kwargs):
                seen_points.append(
                    body["single_mode_start_request_common"][
                        "current_succession_rank_point"])
                return {"data": {}}

        class _OnceSession:
            client = _Once()
            clock = ServerClock()

        class _AcctV:
            tp_info = {"current_tp": 62}
            gold = 1
            succession_rank_point = 1
            current_tp = 62
            viewer_id = 7

        class _SelV:
            card_id = 1; support_card_ids = [1, 2, 3, 4, 5]
            friend_viewer_id = 0; friend_card_id = 0
            parent_id_1 = 1; parent_id_2 = 2; deck_id = 1; running_style = 1

        idl.start(_OnceSession(), idl.Spec(), _SelV(), _AcctV(), idl.Schedule(),
                  scenario_id=3, settings=_st, succession_rank_point=148)
        # The computed value is exact (22/22 against captured accepted starts),
        # so it opens the call unmodified. Learned offsets are never applied to
        # the first call any more: every one on disk was minted by a formula
        # generation that no longer exists, and adding it can only move an
        # already-correct value away from the answer.
        check("the computed value opens the first call",
              seen_points == [148], f"(sent {seen_points})")

    check("accepts the discounted price", run(reject_first=False) == [15])
    # A rejected cost appears twice - each value is tried on both flows (the
    # full payload on `start`, then on `pre_start`) before falling through.
    check("falls through on rejection", run(reject_first=True) == [15, 15, 30])
    check("skips unaffordable candidates",
          run(reject_first=False, tp_available=20) == [15])

    # Regression: Icarus retries 205 three times by default, and its no-retry
    # list is keyed on the scenario prefix so idle_single_mode/start is not
    # covered. Without retry_205=0 every probe costs four calls.
    seen_kwargs = {}

    class _KwCheck:
        def call(self, endpoint, body, **kwargs):
            seen_kwargs.update(kwargs)
            return {"data": {}}

    class _KwSession:
        client = _KwCheck()
        clock = ServerClock()

    idl.start(_KwSession(), idl.Spec(), _Sel(), _Acct(), idl.Schedule(),
              scenario_id=3)
    check("start disables the client's 205 retry",
          seen_kwargs.get("retry_205") == 0, f"(got {seen_kwargs})")

    # Real errors must not be swallowed by the probe loop.
    class _Boom:
        def call(self, endpoint, body, **kwargs):
            raise _Err(709)

    class _S:
        client = _Boom()
        clock = ServerClock()

    try:
        idl.start(_S(), idl.Spec(), _Sel(), _Acct(), idl.Schedule(), scenario_id=3)
        check("non-TP errors propagate", False)
    except Exception as exc:  # noqa: BLE001
        check("non-TP errors propagate", getattr(exc, "result_code", None) == 709)

    # No affordable candidate at all.
    acct = _Acct()
    acct.current_tp = 3

    class _Never:
        def call(self, endpoint, body, **kwargs):
            raise AssertionError("should not have been called")

    class _S2:
        client = _Never()
        clock = ServerClock()

    try:
        idl.start(_S2(), idl.Spec(), _Sel(), acct, idl.Schedule(), scenario_id=3)
        check("too little TP raises", False)
    except idl.IdleModeError as exc:
        check("too little TP raises", "3 available" in str(exc), f"({exc})")

    # Regression: when the only affordable TP value is refused and a dearer one
    # was never tried, TP is the suspect - not the succession rank point. The
    # half-cost campaign ending produces exactly this, and sweeping rank points
    # here burns hundreds of calls on the wrong field.
    class _Refuse:
        def call(self, endpoint, body, **kwargs):
            raise _Err(205)

    class _S3:
        client = _Refuse()
        clock = ServerClock()

    poor = _Acct()
    poor.current_tp = 24          # enough for 15, not for 30
    try:
        idl.start(_S3(), idl.Spec(), _Sel(), poor, idl.Schedule(), scenario_id=3,
                  succession_rank_point=130, succession_hint=312)
        check("blames TP, not the rank point", False)
    except idl.IdleModeError as exc:
        text = str(exc)
        check("blames TP, not the rank point",
              "not enough to try 30" in text, f"({text[:120]})")
        check("explains the half-cost campaign", "half-cost campaign" in text)
        check("suggests --recover-tp", "--recover-tp" in text)
        check("does not mention a rank-point sweep",
              "succession" not in text.lower())


def test_final_turn_resolution() -> None:
    """Regression: an idle career's home summary says turn 1 for the whole run.
    factor_select/finish need the real final turn (78) or they answer 205."""
    print("final turn resolution")
    from aic.career import _SUSPICIOUS_FINAL_TURN, _resolve_final_turn
    from aic.state import CareerState

    class _Session:
        def __init__(self, client):
            self.client = client
            self.clock = ServerClock()

    # 1. end's own response carries it - no extra call needed.
    class _NoCall:
        api_prefix = "single_mode_live"

        def call(self, endpoint, body, **kwargs):
            raise AssertionError(f"should not have called {endpoint}")

    end_data = {"end_info": {"chara_info": {"turn": 78, "card_id": 106801,
                                            "scenario_id": 3, "state": 3}}}
    chara = idl.final_chara_info(_Session(_NoCall()), idl.Spec(), end_data)
    check("end_info supplies chara_info", chara.get("turn") == 78)

    # 2. end_info absent -> check_progress_log.
    class _Progress:
        api_prefix = "single_mode_live"
        calls: list = []

        def call(self, endpoint, body, **kwargs):
            _Progress.calls.append(endpoint)
            return {"data": {"chara_info": {"turn": 78}}}

    chara = idl.final_chara_info(_Session(_Progress()), idl.Spec(), {})
    check("falls back to check_progress_log", chara.get("turn") == 78)
    check("fallback hit the right endpoint",
          _Progress.calls == ["idle_single_mode/check_progress_log"])

    # 3. Both empty -> the fallback must not raise.
    class _Boom:
        api_prefix = "single_mode_live"

        def call(self, endpoint, body, **kwargs):
            raise RuntimeError("205")

    check("fallback failure is not fatal",
          idl.final_chara_info(_Session(_Boom()), idl.Spec(), {}) == {})

    # 4. A too-low turn triggers a <prefix>/load lookup.
    class _Load:
        api_prefix = "single_mode_live"
        calls: list = []

        def set_scenario(self, sid):
            pass

        def load_career(self):
            _Load.calls.append("load")
            return {"data": {"chara_info": {"turn": 78, "scenario_id": 3}}}

    state = CareerState(active=True, kind=KIND_IDLE, scenario_id=3, turn=1)
    turn = _resolve_final_turn(_Session(_Load()), state)
    check("low turn resolved via <prefix>/load", turn == 78, f"(got {turn})")
    check("<prefix>/load was actually called", _Load.calls == ["load"])

    # 5. A plausible turn is trusted without an extra round trip.
    class _Never:
        api_prefix = "single_mode_live"

        def set_scenario(self, sid):
            pass

        def load_career(self):
            raise AssertionError("should not have re-read the career")

    good = CareerState(active=True, kind=KIND_IDLE, scenario_id=3, turn=78)
    check("plausible turn is trusted",
          _resolve_final_turn(_Session(_Never()), good) == 78)
    check("threshold is below a real final turn", _SUSPICIOUS_FINAL_TURN < 78)

    # 6. Even if the lookup fails, we still produce a usable turn.
    class _LoadBoom:
        api_prefix = "single_mode_live"

        def set_scenario(self, sid):
            pass

        def load_career(self):
            raise RuntimeError("102")

    stuck = CareerState(active=True, kind=KIND_IDLE, scenario_id=3, turn=1)
    check("failed lookup degrades gracefully",
          _resolve_final_turn(_Session(_LoadBoom()), stuck) == 1)


def test_succession_points() -> None:
    """Regression: current_succession_rank_point is an affinity score from the
    parents' family tree, NOT the account's item-75 balance. Sending the balance
    gets the start refused."""
    print("succession rank points")
    from aic import succession

    relations = succession.load(str(ICARUS_ROOT))
    check("relation data loaded", relations.usable is True,
          f"({len(relations.members)} charas, {len(relations.points)} types)")

    # Two parents sharing a relation type must score above two that share none.
    def node(card_id, gp1=None, gp2=None, saddles=None):
        tree = []
        if gp1:
            tree.append({"position_id": 10, "card_id": gp1, "win_saddle_id_array": []})
        if gp2:
            tree.append({"position_id": 20, "card_id": gp2, "win_saddle_id_array": []})
        return {"card_id": card_id, "succession_chara_array": tree,
                "win_saddle_id_array": saddles or []}

    lone = succession.compute(node(100101), node(100201), relations)
    check("a bare pair scores a non-negative total", lone >= 0, f"(got {lone})")

    # Shared winning races: the rule changed (retroactively) from "+1 per
    # shared graded race" to "+3 per shared G1, G2/G3 no longer counting".
    base = succession.compute(node(100101, saddles=[]),
                              node(100201, saddles=[]), relations)
    g1_ids = sorted(succession.g1_saddles())
    if not g1_ids:
        print("  skipped G1 win bonus (master.mdb unavailable)")
    else:
        three_g1 = g1_ids[:3]
        # A saddle can stand for several races (a title such as the Triple
        # Crown), so the bonus is per distinct RACE, not per saddle id.
        n_races = len(succession._g1_wins({"win_saddle_id_array": three_g1}))
        check("a saddle can cover more than one race", n_races >= 3,
              f"(3 saddles -> {n_races} races)")
        shared = succession.compute(node(100101, saddles=three_g1),
                                    node(100201, saddles=three_g1), relations)
        check("a shared G1 win is worth 3 per distinct race",
              shared == base + n_races * succession.G1_SADDLE_POINTS,
              f"(base {base}, shared {shared}, races {n_races})")

        non_g1 = [s for s in range(1, 200) if s not in succession.g1_saddles()][:3]
        shared_other = succession.compute(node(100101, saddles=non_g1),
                                          node(100201, saddles=non_g1), relations)
        check("shared G2/G3 wins no longer count",
              shared_other == base, f"(base {base}, got {shared_other})")

        # Only wins BOTH characters have count - one-sided wins are worth zero.
        one_sided = succession.compute(node(100101, saddles=three_g1),
                                       node(100201, saddles=[]), relations)
        check("a win only one of them has scores nothing",
              one_sided == base, f"(base {base}, got {one_sided})")

        # The same race won in two different years is one race, not two: two
        # saddles sharing a race must not score it twice.
        overlap = [s for s in g1_ids
                   if len(succession.saddle_g1_races().get(s, ())) > 1][:1]
        if overlap:
            once = succession.compute(node(100101, saddles=overlap),
                                      node(100201, saddles=overlap), relations)
            races = len(succession.saddle_g1_races()[overlap[0]])
            check("a multi-race saddle scores each race once",
                  once == base + races * succession.G1_SADDLE_POINTS,
                  f"(got {once}, base {base}, races {races})")

    # Grandparents contribute through the 10-11/10-12/20-21/20-22 links.
    with_gps = succession.compute(node(100101, gp1=100301, gp2=100401),
                                  node(100201), relations)
    check("grandparents contribute", with_gps >= lone, f"({with_gps} vs {lone})")

    # With neither master.mdb nor the Icarus snapshot readable, the score must
    # degrade to 0 rather than raise. master.mdb is found independently of
    # icarus_root, so it has to be masked to exercise this.
    from aic import racedata as _racedata
    _find = _racedata.find_master_mdb
    _racedata.find_master_mdb = lambda *a, **k: None
    try:
        check("no relation data degrades to 0",
              succession.compute(node(100101), node(100201),
                                 succession.RelationData(Path("/nonexistent"))) == 0)
    finally:
        _racedata.find_master_mdb = _find

    # The seven terms the in-game calculators show. Grandparents contribute
    # through a THREE-WAY intersection (trainee + that parent + grandparent),
    # not a pairwise link - no pairwise combination reproduces the real values.
    p1 = node(100101, gp1=100301, gp2=100401)
    p2 = node(100201, gp1=100501, gp2=100601)
    expected = (relations.affinity(1009, 1001) + relations.affinity(1009, 1002)
                + relations.affinity(1001, 1002)
                + relations.line_affinity(1009, 1001, 1003)
                + relations.line_affinity(1009, 1001, 1004)
                + relations.line_affinity(1009, 1002, 1005)
                + relations.line_affinity(1009, 1002, 1006))
    check("family_points sums the seven calculator terms",
          succession.family_points(100901, p1, p2, relations) == expected,
          f"(got {succession.family_points(100901, p1, p2, relations)}, "
          f"expected {expected})")
    check("the trainee changes the value",
          succession.family_points(100901, p1, p2, relations)
          != succession.family_points(105201, p1, p2, relations))

    # A line is the three-way intersection, and is subset-bounded by any of its
    # pairwise affinities.
    check("a line never exceeds its pairwise affinity",
          relations.line_affinity(1009, 1001, 1003)
          <= min(relations.affinity(1009, 1001),
                 relations.affinity(1009, 1003)))

    # A grandparent that IS the trainee scores nothing - a calculator breakdown
    # shows 0 for exactly this (a Kitasan Black run with Kitasan Black in both
    # sub-legacy slots). Without it the three-way collapses to affinity(T, P).
    check("a grandparent equal to the trainee scores zero",
          relations.line_affinity(1007, 1001, 1007) == 0)
    check("...and that is not simply because the pair scores zero",
          relations.affinity(1007, 1007) > 0)
    self_gp = node(100101, gp1=100701, gp2=100401)
    other_gp = node(100101, gp1=100301, gp2=100401)
    check("zeroing the line lowers the total",
          succession.family_points(100701, self_gp, p2, relations)
          < succession.family_points(100701, other_gp, p2, relations)
          or relations.line_affinity(1007, 1001, 1003) == 0)

    # The formula undercounts (missing G1 wins), so the sweep must reach well
    # up; down is only a thin margin.
    check("sweep reaches well up, a little down",
          succession.SWEEP_UP >= 60 and 0 < succession.SWEEP_DOWN <= succession.SWEEP_UP)

    order = list(succession.candidates(100, hint=312, cached_offset=5))
    check("cached offset tried first", order[0] == (105, "cached offset"))
    check("account balance tried second",
          order[1] == (312, "account balance (item 75)"))
    check("then a positive sweep", order[2][0] == 101)
    check("positives before negatives",
          [v for v, _ in order].index(101) < [v for v, _ in order].index(99))
    check("reaches a modest positive offset", 106 in [v for v, _ in order])
    check("computed value never retried", 100 not in [v for v, _ in order])
    check("no duplicates", len({v for v, _ in order}) == len(order))
    check("no negatives", all(v >= 0 for v, _ in order))

    plain = list(succession.candidates(0))
    check("negative offsets skipped near zero", all(v > 0 for v, _ in plain))

    check("--no-sweep leaves only targeted candidates",
          list(succession.candidates(100, sweep=False)) == [])
    check("--no-sweep still uses the cache",
          list(succession.candidates(100, cached_offset=7, sweep=False))
          == [(107, "cached offset")])

    # Regression: in a rotation every retirement adds a stronger character, so
    # ranking alone picks a new parent pair each cycle - and each new pair costs
    # another bounded search. Known pairs must win over rank.
    import tempfile
    from types import SimpleNamespace

    with tempfile.TemporaryDirectory() as tmp:
        st = SimpleNamespace(state_dir=Path(tmp))
        check("no known pairs initially", succession.known_pairs(st) == set())
        succession.save_offset(st, 4, 6, 22)
        check("a saved offset becomes a known pair",
              succession.known_pairs(st) == {(4, 6)})
        succession.save_offset(st, 7, 6, -5)
        check("known pairs accumulate",
              succession.known_pairs(st) == {(4, 6), (7, 6)})
        # trained_chara_ids are per-account sequence numbers (every account's
        # early characters are ids 1-7), and the rotation shares one profile
        # across many accounts - so offsets must never leak between viewers.
        succession.save_offset(st, 4, 6, 99, viewer_id=111)
        check("offsets are scoped per account",
              succession.known_pairs(st, viewer_id=111) == {(4, 6)}
              and (7, 6) not in succession.known_pairs(st, viewer_id=111))
        check("another account's offset does not leak back",
              succession.load_offsets(st)[
                  succession.offset_key(4, 6, viewer_id=111)] == 99
              and succession.load_offsets(st)[
                  succession.offset_key(4, 6)] == 22)

        # The residual is trainee-dependent, not just pair-dependent: parents
        # 7+6 computed 148 for both chara 1041 and 1052, yet 1041's accepted
        # value (86) was refused for 1052. An offset must not carry across.
        succession.save_offset(st, 7, 6, -62, viewer_id=111,
                               trainee_card_id=104101)
        succession.save_offset(st, 7, 6, -13, viewer_id=111,
                               trainee_card_id=105201)
        loaded = succession.load_offsets(st)
        check("offsets are scoped per trainee",
              loaded[succession.offset_key(7, 6, 111, 104101)] == -62
              and loaded[succession.offset_key(7, 6, 111, 105201)] == -13)
        # An unseen pair still gets a sensible opening from its neighbours.
        check("account-level fallback is the median of what it has learned",
              succession.account_offset(st, viewer_id=111) in (-13, 99, -62),
              f"(got {succession.account_offset(st, viewer_id=111)})")
        check("an account with nothing learned has no fallback",
              succession.account_offset(st, viewer_id=999) is None)

    # Every farmed account is created from the same seed characters, so a tree
    # keyed by its CONTENTS lets one search serve the whole roster. Keying on
    # trained_chara_id cannot: those are per-account sequence numbers, so
    # farm01's lesson would be filed where farm02 never looks.
    def node(card_id, gp1, gp2):
        return {"card_id": card_id, "win_saddle_id_array": [],
                "succession_chara_array": [
                    {"position_id": 10, "card_id": gp1, "win_saddle_id_array": []},
                    {"position_id": 20, "card_id": gp2, "win_saddle_id_array": []},
                ]}

    p_a = node(100701, 100901, 100801)
    p_b = node(100901, 100801, 100701)
    sig = succession.tree_signature(100801, p_a, p_b)
    check("a tree signature names its characters, not account ids",
          "100701" in sig and "100901" in sig and sig.startswith("t"), f"({sig})")
    check("the same lineage on another account gives the same signature",
          succession.tree_signature(100801, node(100701, 100901, 100801),
                                    node(100901, 100801, 100701)) == sig)
    check("a different trainee gives a different signature",
          succession.tree_signature(105201, p_a, p_b) != sig)
    check("different grandparents give a different signature",
          succession.tree_signature(100801, node(100701, 104101, 100801),
                                    p_b) != sig)

    with tempfile.TemporaryDirectory() as tmp2:
        st2 = SimpleNamespace(state_dir=Path(tmp2))
        # farm01 learns it under ids 7/6...
        succession.save_offset(st2, 7, 6, -62, viewer_id=111,
                               trainee_card_id=100801, signature=sig)
        # ...and farm02, whose identical characters carry different ids, finds it.
        check("a learned offset transfers between accounts sharing a lineage",
              succession.load_offsets(st2).get(sig) == -62)

    # And pick_parents must actually honour them over rank order.
    gd = GameData(ICARUS_ROOT)
    strong = sel.Parent(trained_chara_id=9, card_id=100701, rank=30, rank_score=99999)
    known_a = sel.Parent(trained_chara_id=4, card_id=100301, rank=2, rank_score=10)
    known_b = sel.Parent(trained_chara_id=6, card_id=100401, rank=3, rank_score=20)
    pool = [strong, known_a, known_b]

    top = sel.pick_parents(pool, 105201, gd)
    check("without a hint the strongest wins", top[0].trained_chara_id == 9)

    # Regression: a freshly retired character has the previous pair as its own
    # parents, and by rank it is the obvious next pick - but selecting it
    # alongside one of them puts that one in the tree twice (parent AND
    # grandparent), which the server refuses.
    child = sel.Parent(trained_chara_id=7, card_id=100701, rank=11,
                       rank_score=50000, parent_ids=(6, 4))
    mum = sel.Parent(trained_chara_id=6, card_id=100301, rank=3, rank_score=20)
    dad = sel.Parent(trained_chara_id=4, card_id=100401, rank=2, rank_score=10)
    stranger = sel.Parent(trained_chara_id=2, card_id=100201, rank=1, rank_score=5)

    picked = sel.pick_parents([child, mum, dad, stranger], 100801, gd)
    ids = {picked[0].trained_chara_id, picked[1].trained_chara_id}
    check("prefers a pair with no shared ancestry", ids == {7, 2},
          f"(picked {ids})")

    # THE regression for "sometimes works, sometimes doesn't": the succession
    # rank point is scored over five family-tree links, but with no
    # grandparents only the parent-to-parent link exists, so the value is exact
    # and the start is accepted first call. Career-produced parents have
    # grandparents, outrank the starters, and drift the choice onto the case
    # where the formula is measurably wrong.
    tree = [{"position_id": 10, "card_id": 100301, "win_saddle_id_array": []},
            {"position_id": 20, "card_id": 100401, "win_saddle_id_array": []}]
    strong_with_tree = sel.Parent(trained_chara_id=7, card_id=100701, rank=11,
                                  rank_score=50000, succession_chara_array=tree)
    weak_clean_a = sel.Parent(trained_chara_id=6, card_id=100301, rank=3,
                              rank_score=20)
    weak_clean_b = sel.Parent(trained_chara_id=4, card_id=100801, rank=2,
                              rank_score=10)

    check("a starter has no lineage", weak_clean_a.has_lineage is False)
    check("a career-produced parent has lineage",
          strong_with_tree.has_lineage is True)

    chosen = sel.pick_parents([strong_with_tree, weak_clean_a, weak_clean_b],
                             105201, gd)
    got = {chosen[0].trained_chara_id, chosen[1].trained_chara_id}
    check("grandparent-free parents beat a far stronger one", got == {6, 4},
          f"(picked {got})")

    # But if none are clean it must still pick something rather than fail.
    other_tree = sel.Parent(trained_chara_id=8, card_id=100201, rank=4,
                            rank_score=30, succession_chara_array=tree)
    fallback = sel.pick_parents([strong_with_tree, other_tree], 105201, gd)
    check("falls back when every parent has lineage", fallback is not None)

    # Overlapping ancestry is ordinary pedigree building, NOT a server rule -
    # so it must never block a pair, only lose a tie. Getting this wrong left a
    # small pool with nothing to pick.
    only_family = sel.pick_parents([child, mum, dad], 100801, gd)
    check("a related pair is used when that is all there is",
          {only_family[0].trained_chara_id, only_family[1].trained_chara_id}
          in ({7, 6}, {7, 4}, {6, 4}))

    cousin_a = sel.Parent(trained_chara_id=7, card_id=100701, rank=11,
                          rank_score=50000, parent_ids=(6, 4))
    cousin_b = sel.Parent(trained_chara_id=1, card_id=100301, rank=2,
                          rank_score=30, parent_ids=(2, 4))
    check("a shared grandparent alone is still usable",
          sel.pick_parents([cousin_a, cousin_b], 105201, gd) is not None)

    # Only same-character remains a hard rule.
    twin_a = sel.Parent(trained_chara_id=20, card_id=100301, rank=5, rank_score=100)
    twin_b = sel.Parent(trained_chara_id=21, card_id=100302, rank=5, rank_score=90)
    try:
        sel.pick_parents([twin_a, twin_b], 105201, gd)
        check("same character is still rejected", False)
    except sel.SelectionError as exc:
        check("same character is still rejected", "same character" in str(exc))
    kept = sel.pick_parents(pool, 105201, gd, preferred_pairs={(4, 6)})
    check("a known pair beats a stronger one",
          {kept[0].trained_chara_id, kept[1].trained_chara_id} == {4, 6})
    check("an unavailable known pair falls back to rank",
          sel.pick_parents(pool, 105201, gd,
                           preferred_pairs={(111, 222)})[0].trained_chara_id == 9)


def test_succession_sweep() -> None:
    """The start must search for the rank point when the server refuses it."""
    print("succession sweep")

    class _Err(Exception):
        def __init__(self, rc):
            super().__init__(f"API error {rc}")
            self.result_code = rc

    class _Sel:
        card_id = 1; support_card_ids = [1, 2, 3, 4, 5]
        friend_viewer_id = 9; friend_card_id = 8
        parent_id_1 = 11; parent_id_2 = 22; deck_id = 1; running_style = 1

    class _Acct:
        tp_info = {"current_tp": 62}; gold = 1
        succession_rank_point = 312; current_tp = 62

    def run(accept_points, hint=312, sweep=True):  # noqa: FBT002
        tried = []

        class _Client:
            def call(self, endpoint, body, **kwargs):
                pts = body["single_mode_start_request_common"][
                    "current_succession_rank_point"]
                tried.append(pts)
                if pts != accept_points:
                    raise _Err(205)
                return {"data": {}}

        class _Session:
            client = _Client()
            clock = ServerClock()

        idl.start(_Session(), idl.Spec(), _Sel(), _Acct(), idl.Schedule(),
                  scenario_id=3, succession_rank_point=100, succession_hint=hint,
                  sweep=sweep)
        return tried

    check("accepts the computed value immediately", run(100) == [100])
    # The computed value appears once per TP candidate (phase 1 probes both),
    # then phase 2 starts trying alternative rank points.
    tried = run(312, sweep=False)
    check("accepts the account balance without sweeping",
          tried[-1] == 312 and set(tried) == {100, 312}, f"(tried {tried})")

    # With --no-sweep the search stops after the targeted candidates.
    try:
        run(103, sweep=False)
        check("stops early with --no-sweep", False)
    except idl.IdleModeError as exc:
        check("stops early with --no-sweep", True)
        check("error mentions --sweep", "--sweep" in str(exc))

    tried = run(103)
    check("finds a small positive offset by default", tried[-1] == 103,
          f"(tried {tried[:8]})")
    check("tries the account balance before sweeping", 312 in tried[:10])

    # A modest undercount (the formula's known failure mode) is reached by the
    # upward sweep within the probe cap.
    tried = run(100 + 20)
    check("a modest undercount is found", tried[-1] == 120,
          f"(after {len(tried)} calls)")

    # An arbitrary far-off offset is deliberately NOT reachable: the probe cap
    # is what stops a wrong formula turning into hundreds of rejected starts.
    # Skipping the account and retrying next lap is the intended outcome.
    try:
        run(100 + 250)
        check("an arbitrary far offset is not chased", False)
    except idl.IdleModeError:
        check("an arbitrary far offset is not chased", True)

    # Nothing works -> the rank point is not the problem. The error must say so
    # and name the other suspects rather than leaving a bare result code.
    try:
        run(999999)
        check("exhausted search raises", False)
    except idl.IdleModeError as exc:
        text = str(exc)
        check("exhausted search raises",
              "refused (205)" in text and "computed exactly" in text,
              f"({text[:100]})")
        # The race list leads the list of suspects now: it is the only cause
        # with direct A/B evidence, and the rank point is computed exactly.
        check("error names the other suspects",
              all(s in text for s in ("race list", "--scenario 3", "aic status",
                                      "borrowed support card",
                                      "duplicate character")))

    # The search is bounded - it must not grind forever.
    tried = []
    try:
        tried = run(999999, hint=0)
    except idl.IdleModeError:
        pass

    class _Counter:
        n = 0

        def call(self, endpoint, body, **kwargs):
            _Counter.n += 1
            raise _Err(205)

    class _S:
        client = _Counter()
        clock = ServerClock()

    try:
        idl.start(_S(), idl.Spec(), _Sel(), _Acct(), idl.Schedule(),
                  scenario_id=3, succession_rank_point=100, succession_hint=312)
    except idl.IdleModeError:
        pass
    # 2 TP costs x 2 flows, then at most MAX_START_PROBES candidates, the first
    # 8 of which are tried on both flows. Hundreds of rejected starts in a few
    # seconds is what preceded a 217 in practice, so this ceiling is the point.
    bound = 2 * 2 + idl.MAX_START_PROBES + 8
    check("search is bounded", _Counter.n <= bound,
          f"(made {_Counter.n} calls, bound {bound})")
    check("the bound stays well below the volume that drew a 217",
          bound < 60, f"(bound {bound})")

    # 217 = Session Verification Error: the session is dead and no further
    # call on it can succeed, so the search must stop on the first one rather
    # than burning through the rest of the sweep against a session that can
    # never accept anything.
    class _SessionDead:
        n = 0

        def call(self, endpoint, body, **kwargs):
            _SessionDead.n += 1
            raise _Err(217)

    class _S217:
        client = _SessionDead()
        clock = ServerClock()

    try:
        idl.start(_S217(), idl.Spec(), _Sel(), _Acct(), idl.Schedule(),
                  scenario_id=3, succession_rank_point=100, succession_hint=312)
        check("217 raises", False)
    except idl.IdleModeError as exc:
        check("217 raises", "217" in str(exc) and "invalidated" in str(exc))
    check("217 stops on the first hit", _SessionDead.n == 1,
          f"(made {_SessionDead.n} calls)")


def test_friend_card(gamedata: GameData) -> None:
    print("borrowed support card")
    rng = random.Random(7)
    offers = {"friend_support_card_data": {"support_card_data_array": [
        {"viewer_id": 111, "support_card_id": 30052, "limit_break_count": 4},
        {"viewer_id": 222, "support_card_id": 10001, "limit_break_count": 0},
    ]}}

    viewer, card = sel.pick_friend_card(offers, [30121, 30074], 100301, gamedata, rng)
    check("picks an offer", card in (30052, 10001) and viewer in (111, 222))
    # 30052 is absent from the shipped support table, so this also checks that
    # rarity falls back to the card id (3xxxx = SSR) instead of ranking 0.
    check("prefers the higher rarity/limit break", card == 30052,
          f"(got {card})")
    check("rarity inferred from the id for unknown cards",
          gamedata.support_rarity_rank(30052) == 3
          and gamedata.support_rarity_rank(20001) == 2
          and gamedata.support_rarity_rank(10001) == 1)

    # A card already in the deck must not be borrowed again.
    viewer, card = sel.pick_friend_card(offers, [30052], 100301, gamedata, rng)
    check("skips a card already in the deck", card == 10001, f"(got {card})")

    # Nor one matching the trainee's character (10001 is Special Week).
    viewer, card = sel.pick_friend_card(
        {"friend_support_card_data": {"support_card_data_array": [
            {"viewer_id": 222, "support_card_id": 10001, "limit_break_count": 0}]}},
        [], 100101, gamedata, rng)
    check("skips the trainee's own character", (viewer, card) == (0, 0))

    check("empty offer list degrades to none",
          sel.pick_friend_card({}, [], 100301, gamedata, rng) == (0, 0))
    check("malformed offers ignored",
          sel.pick_friend_card(
              {"friend_support_card_data": {"support_card_data_array": [
                  {"viewer_id": 0, "support_card_id": 0}]}},
              [], 100301, gamedata, rng) == (0, 0))


def test_forced_parents() -> None:
    print("pinned parents")
    parents = [sel.Parent(trained_chara_id=1980, card_id=106801, rank=16),
               sel.Parent(trained_chara_id=1950, card_id=102402, rank=16),
               sel.Parent(trained_chara_id=1798, card_id=100401, rank=30)]

    p1, p2 = sel.resolve_parents(parents, "1980,1950")
    check("resolves an explicit pair",
          (p1.trained_chara_id, p2.trained_chara_id) == (1980, 1950))
    check("order is preserved",
          sel.resolve_parents(parents, "1950,1980")[0].trained_chara_id == 1950)
    check("tolerates spaces",
          sel.resolve_parents(parents, " 1980 , 1950 ")[1].trained_chara_id == 1950)

    for bad, why in (("1980", "one id"), ("1980,1950,1798", "three ids"),
                     ("", "empty"), ("abc,def", "non-numeric")):
        try:
            sel.resolve_parents(parents, bad)
            check(f"rejects {why}", False)
        except sel.SelectionError:
            check(f"rejects {why}", True)

    try:
        sel.resolve_parents(parents, "1980,999999")
        check("rejects an unowned id", False)
    except sel.SelectionError as exc:
        check("rejects an unowned id", "999999" in str(exc))


def test_scenario_choice() -> None:
    print("scenario choice")
    from aic.flow import pick_scenario
    from aic.state import AccountState

    account = AccountState(available_scenario_ids=[1, 2, 3, 4])
    check("reuses the previous run's scenario",
          pick_scenario(account, 0, previous=3) == 3)
    check("explicit request wins", pick_scenario(account, 2, previous=3) == 2)
    # Scenario 4 IS startable in idle mode. This used to assert 3, on the
    # reasoning that 4 "unlocks but is not startable". A UmaDumpy capture of the
    # live client on 2026-09-02 shows an accepted idle start (result_code 1)
    # carrying scenario_id 4 - and once MAX_IDLE_SCENARIO was raised to match,
    # a real career started on 896245680077. The old assertion encoded the stale
    # July reading of the protocol.
    check("falls back to the newest startable scenario",
          pick_scenario(account, 0, previous=0) == 4)
    from aic.flow import MAX_IDLE_SCENARIO
    check("the cap now admits scenario 4",
          pick_scenario(account, 4) == 4 and MAX_IDLE_SCENARIO == 4)
    check("a scenario the account has NOT unlocked is still refused",
          pick_scenario(AccountState(available_scenario_ids=[1, 2, 3]), 0,
                        previous=0) == 3)
    check("ignores an unavailable previous",
          pick_scenario(AccountState(available_scenario_ids=[1, 2]), 0,
                        previous=9) == 2)
    check("uses a remembered known-good scenario",
          pick_scenario(account, 0, previous=0, known=3) == 3)
    check("a run seen this cycle beats the remembered one",
          pick_scenario(account, 0, previous=2, known=3) == 2)
    check("ignores an unavailable remembered scenario",
          pick_scenario(AccountState(available_scenario_ids=[1, 2]), 0,
                        known=4) == 2)

    # Persistence round-trip.
    import tempfile
    from types import SimpleNamespace
    with tempfile.TemporaryDirectory() as tmp:
        settings = SimpleNamespace(state_dir=Path(tmp))
        check("no remembered scenario initially",
              idl.load_known_scenario(settings) == 0)
        idl.save_known_scenario(settings, 3)
        check("remembered scenario round-trips",
              idl.load_known_scenario(settings) == 3)
        idl.save_known_scenario(settings, 0)
        check("zero is not persisted", idl.load_known_scenario(settings) == 3)
        check("None settings is safe", idl.load_known_scenario(None) == 0)
    try:
        pick_scenario(account, 9)
        check("rejects a locked scenario", False)
    except Exception as exc:  # IdleModeError
        check("rejects a locked scenario", "not unlocked" in str(exc))


def test_race_rules() -> None:
    """Race lists are per-trainee: each route has its own mandatory goal races,
    so a generic saved agenda omits them and the start is refused."""
    print("race rules (master.mdb)")
    from aic import racedata

    check("permission enum covers 1-4",
          set(racedata.PERMISSION_YEARS) == {1, 2, 3, 4})
    check("junior only", racedata.PERMISSION_YEARS[1] == {1})
    check("classic or senior", racedata.PERMISSION_YEARS[3] == {2, 3})
    check("turn 1 is year 1", racedata.year_for_turn(1) == 1)
    check("turn 24 is year 1", racedata.year_for_turn(24) == 1)
    check("turn 25 is year 2", racedata.year_for_turn(25) == 2)
    check("turn 78 clamps to year 3", racedata.year_for_turn(78) == 3)

    mdb = racedata.find_master_mdb(os.environ.get("AIC_MASTER_MDB"))
    if not mdb:
        print("  skipped live extraction (master.mdb not found)")
        return
    print(f"    master.mdb: {mdb}")

    raw = racedata.extract(mdb)
    rules = racedata.RaceRules(
        programs=raw["programs"], chara_groups=raw["chara_groups"],
        routes=raw["routes"], route_races=raw["route_races"],
        race_turns=raw["race_turns"])
    check("extracted programs", len(rules.programs) > 500)
    check("extracted routes", len(rules.routes) > 50)
    check("extracted route races", len(rules.route_races) > 50)
    check("extracted race turns", len(rules.race_turns) > 50)

    # Kitasan Black is the trainee the working capture used; its route id 51
    # and 14 goal races match that capture's chara_info exactly.
    kitasan_targets = rules.route_targets(1068)
    print(f"    Kitasan Black (1068): {len(kitasan_targets)} goal races")
    check("Kitasan has goal races", len(kitasan_targets) > 5)

    for chara, label in ((1068, "Kitasan Black"), (1061, "King Halo"),
                         (1014, "El Condor Pasa"), (1010, "Taiki Shuttle")):
        targets = rules.route_targets(chara)
        built = rules.build_race_array(chara)
        missing = [t for t in targets if t not in built]
        print(f"    {label:<16} {len(targets):>2} goals -> {len(built):>2} races")
        check(f"{label}: every goal race is included", not missing,
              f"(missing {missing})")
        check(f"{label}: every entry is legal for it",
              all(rules.is_legal(chara, e["year"], e["program_id"]) for e in built))

    # The routes genuinely differ - that is the whole point.
    sets = {c: {(t["year"], t["program_id"]) for t in rules.route_targets(c)}
            for c in (1068, 1061, 1014, 1010)}
    check("different trainees have different goal races",
          len({frozenset(s) for s in sets.values()}) == 4)
    only_kitasan = sets[1068] - sets[1061]
    print(f"    Kitasan needs {len(only_kitasan)} races King Halo does not")
    check("Kitasan's goals are not a subset of King Halo's", bool(only_kitasan))

    # Regression: some routes mandate NO races in a year - their objectives
    # there are condition_type 3 fan-count goals, not races. Haru Urara has
    # three, all in year 2, so a plan built purely from route goals left year 2
    # empty and the start was refused.
    bare = rules.build_race_array(1052)
    bare_years = {e["year"] for e in bare}
    check("Haru Urara's route really does skip year 2", 2 not in bare_years,
          f"(years {sorted(bare_years)})")

    for chara, label in ((1052, "Haru Urara"), (1007, "Gold Ship"),
                         (1068, "Kitasan Black")):
        base = rules.build_race_array(chara)
        filled = rules.fill_race_array(chara, base)
        counts = {y: sum(1 for e in filled if e["year"] == y) for y in (1, 2, 3)}
        check(f"{label}: every year gets races",
              all(counts[y] > 0 for y in (1, 2, 3)), f"({counts})")
        check(f"{label}: filled races are all legal",
              all(rules.is_legal(chara, e["year"], e["program_id"]) for e in filled))
        check(f"{label}: goal races survive the fill",
              all(t in filled for t in rules.route_targets(chara)))
        slots = [(e["year"], rules.program(e["program_id"])["month"],
                  rules.program(e["program_id"])["half"]) for e in filled]
        check(f"{label}: no two races share a turn", len(slots) == len(set(slots)))
        check(f"{label}: plan is a realistic size", 20 <= len(filled) <= 40,
              f"({len(filled)})")

    already = [{"year": y, "program_id": p} for y, p in
               ((1, 623), (2, 84), (3, 1))]
    protect_already = {(e["year"], e["program_id"]) for e in already}
    topped = rules.fill_race_array(1007, already, target=3, protect=protect_already)
    check("what was protected is still there",
          protect_already <= {(e["year"], e["program_id"]) for e in topped})
    # Program 1 needs 12,000 fans to enter, and it is protected, so the plan
    # keeps enough races before it to fund its entry rather than stranding it.
    check("races funding a protected entry are kept past the target",
          len(topped) > len(already), f"({len(topped)})")
    earned = 0
    for e in sorted(topped, key=lambda e: rules._slot(e["year"], e["program_id"])):
        if (e["year"], e["program_id"]) in protect_already:
            check_ok = rules.need_fans(e["program_id"]) <= earned
            if not check_ok:
                break
        earned += rules.fan_reward(e["program_id"])
    check("and the protected race is actually enterable", check_ok)
    check("filling is deterministic",
          rules.fill_race_array(1052, bare) == rules.fill_race_array(1052, bare))

    # An agenda built for one trainee must be filtered for another.
    agenda = [{"year": y, "program_id": p} for y, p in sets[1068]]
    legal, rejected = rules.filter_races(1061, agenda)
    print(f"    Kitasan's agenda vs King Halo: {len(legal)} legal, "
          f"{len(rejected)} rejected")
    merged = rules.build_race_array(1061, agenda)
    check("merging keeps King Halo's own goals",
          all(t in merged for t in rules.route_targets(1061)))
    check("merging drops nothing illegal",
          all(rules.is_legal(1061, e["year"], e["program_id"]) for e in merged))
    check("merged list has no duplicates",
          len({(e["year"], e["program_id"]) for e in merged}) == len(merged))
    check("merged list is sorted",
          merged == sorted(merged, key=lambda e: (e["year"], e["program_id"])))

    # Unknown programs and bad years are rejected, not crashed on.
    junk = [{"year": 3, "program_id": 999999}, {"year": 9, "program_id": 74},
            {"bad": "shape"}]
    legal, rejected = rules.filter_races(1068, junk)
    check("junk races are rejected", legal == [])


def test_ticket_reuse() -> None:
    """Regression: minting a Steam ticket is a real Steam login. Doing it on
    every command trips Steam Guard, which used to be a hard failure."""
    print("steam ticket reuse")
    import tempfile
    from types import SimpleNamespace

    from aic.auth import MAX_TICKET_AGE, refresh_steam_ticket, ticket_age

    now = time.time()
    check("explicit timestamp wins",
          abs(ticket_age({"steam_ticket_ts": now - 100}) - 100) < 2)
    check("no timestamp and no settings is infinitely old",
          ticket_age({}) == float("inf"))
    check("threshold is minutes, not seconds", 60 <= MAX_TICKET_AGE <= 3600)

    with tempfile.TemporaryDirectory() as tmp:
        cfg_path = Path(tmp) / "auth_config.json"
        cfg_path.write_text("{}", encoding="utf-8")
        settings = SimpleNamespace(state_dir=Path(tmp), auth_config_path=cfg_path)
        check("falls back to the config mtime", ticket_age({}, settings) < 5)

        # Reuse is gated on the game running as well as the ticket's age: Steam
        # cancels an app ticket when the owning process exits, so a "fresh"
        # ticket from a closed game is already dead.
        import aic.auth as auth_mod

        original = auth_mod.game_is_running
        fresh = {"steam_session_ticket": "abc", "steam_ticket_ts": now - 10,
                 "steam_username": "someone"}
        try:
            auth_mod.game_is_running = lambda: True
            check("fresh ticket is reused while the game runs",
                  refresh_steam_ticket(fresh, settings) is False)
            check("reuse leaves the ticket untouched",
                  fresh["steam_session_ticket"] == "abc")

            # Game closed -> must not reuse. No username here, so it degrades
            # rather than minting (which would need bootstrap).
            auth_mod.game_is_running = lambda: False
            closed = {"steam_session_ticket": "abc", "steam_ticket_ts": now - 10}
            check("closed game does not reuse on age alone",
                  refresh_steam_ticket(closed, settings) is False)

            # Stale, no credentials -> degrade, never raise.
            stale = {"steam_session_ticket": "abc", "steam_ticket_ts": now - 99999}
            check("stale without credentials degrades quietly",
                  refresh_steam_ticket(stale, settings) is False)
            check("degraded path keeps the stored ticket",
                  stale["steam_session_ticket"] == "abc")
            # Regression: a live session's ticket must be copied INTO cfg, not
            # merely reused. A caller that loaded cfg from disk is holding the
            # previous (possibly dead) ticket and would send that instead - which
            # is what made tool/signup answer 394 right after a logout.
            import aic.steam as steam_mod

            class _LiveSession:
                ticket = "LIVE-TICKET"
                steam_id = "76561199999999999"
                refresh_token = "rt-abc"
                alive = True
                started_at = now

            original_current = steam_mod.current
            original_live = steam_mod.has_live_session
            try:
                steam_mod.current = lambda: _LiveSession()
                steam_mod.has_live_session = lambda: True
                from_disk = {"steam_session_ticket": "STALE", "steam_id": "old"}
                changed = refresh_steam_ticket(from_disk, settings)
                check("live session ticket is adopted into cfg",
                      from_disk["steam_session_ticket"] == "LIVE-TICKET")
                check("adopting a different ticket reports a change", changed is True)
                check("live session steam_id is adopted",
                      from_disk["steam_id"] == "76561199999999999")
                check("live session refresh token is adopted",
                      from_disk["steam_refresh_token"] == "rt-abc")
                check("re-adopting the same ticket reports no change",
                      refresh_steam_ticket(from_disk, settings) is False)
            finally:
                steam_mod.current = original_current
                steam_mod.has_live_session = original_live
        finally:
            auth_mod.game_is_running = original


def test_steam_session() -> None:
    """The persistent session is what keeps a ticket alive: Steam cancels an app
    ticket when the owning process exits."""
    print("persistent steam session")
    from pathlib import Path as _P

    from aic import steam

    check("no session at rest", steam.has_live_session() is False)
    check("age is infinite with no session", steam.session_age() == float("inf"))

    s = steam.SteamSession(_P("."), username="u", password="p")
    check("a session with no process is not alive", s.alive is False)
    check("refresh token starts empty", s.refresh_token == "")
    check("targets the right appid", steam.APP_ID == 3224770)

    # The driver must keep running - an exiting process is exactly the bug.
    check("driver never calls process.exit on success",
          "type: \"ready\"" in steam.PERSISTENT_JS.replace("'", '"')
          or '"ready"' in steam.PERSISTENT_JS)
    check("driver reports the refresh token",
          'type: "refresh_token"' in steam.PERSISTENT_JS)
    check("driver accepts a refresh token",
          "--refresh-token" in steam.PERSISTENT_JS)
    check("driver marks the game as played (needed for the ticket)",
          "gamesPlayed" in steam.PERSISTENT_JS)
    check("driver retries ticket creation",
          "tryTicket" in steam.PERSISTENT_JS)

    # Credentials are required; an anonymous logon would be useless.
    try:
        steam.acquire(_P("."), {})
        check("refuses without credentials", False)
    except RuntimeError as exc:
        check("refuses without credentials", "credentials" in str(exc))

    check("guard error type exists",
          issubclass(steam.SteamGuardRequired, RuntimeError))


def test_capture() -> None:
    """Envelope parsing for the frida capture. Exercised against a real captured
    request when one is available, so the offsets are not just asserted."""
    print("frida capture")
    from aic import capture as cap

    check("agent hooks the unitytls interface",
          "il2cpp_unity_install_unitytls_interface" in cap.FRIDA_AGENT)
    check("agent reassembles POSTs", "Content-Length" in cap.FRIDA_AGENT)
    check("agent reports the raw header",
          "header_hex" in cap.FRIDA_AGENT and "auth_key_len" in cap.FRIDA_AGENT)

    # Layout: HEAD | sid(16) | udid(16) | rand(32) [| auth(48)]
    # With auth the udid is at [-96:-80]; without, at [-48:-32].
    header = ("aa" * 52) + ("bb" * 16) + ("cd" * 16) + ("ee" * 32) + ("ff" * 48)
    cands = cap.udid_from_envelope(header)
    check("two udid candidates offered", len(cands) == 2)
    check("authenticated offset picks the udid field",
          cands[0] == "cdcdcdcd-cdcd-cdcd-cdcd-cdcdcdcdcdcd", f"(got {cands[0]})")
    check("candidates are uuid-shaped",
          all(len(c) == 36 and c.count("-") == 4 for c in cands))
    check("a short header yields nothing",
          cap.udid_from_envelope("ab" * 4) == [])

    # Real envelope: an auth_config archived from an actual Icarus capture keeps
    # the wire body and the udid it was taken under.
    # Pick an archive whose body and udid actually belong together. After an
    # account switch the two can disagree - the envelope was captured under the
    # previous udid - which is a real condition, not a test artefact.
    archive = Path(__file__).resolve().parent.parent / "state" / "default" / "auth_archive"
    sample = payload = None
    for path in sorted(archive.glob("*.json")) if archive.is_dir() else []:
        cfg = json.loads(path.read_text(encoding="utf-8-sig"))
        if not (cfg.get("body") and cfg.get("udid")):
            continue
        try:
            payload = cap.decrypt_request_body(cfg["body"], cfg["udid"])
        except Exception:  # noqa: BLE001 - mismatched pair; try the next
            continue
        sample = cfg
        break
    if not sample:
        print("  (real envelope: none archived with a matching udid, skipped)")
        return
    check("real envelope decrypts", isinstance(payload, dict) and len(payload) > 5)
    check("payload carries the Steam ticket",
          len(payload.get("steam_session_ticket") or "") > 100)
    check("payload viewer_id matches the config",
          int(payload.get("viewer_id") or 0) == int(sample.get("viewer_id") or -1))
    check("payload carries the device identity",
          bool(payload.get("device_id")) and bool(payload.get("steam_id")))

    try:
        cap.decrypt_request_body(sample["body"], "00000000-0000-0000-0000-000000000000")
        check("a wrong udid fails to decrypt", False)
    except Exception:  # noqa: BLE001
        check("a wrong udid fails to decrypt", True)


def test_account_switching() -> None:
    print("account switching")
    import base64
    import tempfile
    from types import SimpleNamespace

    from aic import account as acct

    check("trainer id formatted in threes",
          acct.format_trainer_id(103444444441) == "103 444 444 441")
    check("spaced trainer id parses",
          acct.parse_trainer_id("103 444 444 441") == 103444444441)
    check("dashed trainer id parses",
          acct.parse_trainer_id("103-444-444-441") == 103444444441)
    check("plain trainer id parses",
          acct.parse_trainer_id("103444444441") == 103444444441)
    try:
        acct.parse_trainer_id("abc")
        check("non-numeric trainer id rejected", False)
    except acct.AccountError:
        check("non-numeric trainer id rejected", True)

    for bad, why in (("short1A", "too short"), ("alllowercase1", "no uppercase"),
                     ("ALLUPPERCASE1", "no lowercase"), ("NoDigitsHere", "no digit"),
                     ("WayTooLongPassword1", "too long")):
        try:
            acct.validate_password(bad)
            check(f"rejects password: {why}", False)
        except acct.AccountError:
            check(f"rejects password: {why}", True)
    try:
        acct.validate_password("MyPass2026")
        check("accepts a valid password", True)
    except acct.AccountError:
        check("accepts a valid password", False)

    # A captured switch sent an md5(password + SALT) digest here (redacted).
    # Confirms the hash is md5(password + SALT) and that SALT resolves.
    digest = acct.hash_password("MyPass2026")
    check("password hashes to 32 hex chars",
          len(digest) == 32 and all(c in "0123456789abcdef" for c in digest))
    check("hashing is deterministic", digest == acct.hash_password("MyPass2026"))
    check("different passwords differ", digest != acct.hash_password("MyPass2027"))
    check("hash is salted, not bare md5",
          digest != __import__("hashlib").md5(b"MyPass2026").hexdigest())

    # Regression: the signup config used to be an allowlist, which silently
    # dropped steam_refresh_token - so every takeover fell back to a password
    # login and a Guard prompt even with a good token on file.
    check("account identity is dropped for the signup",
          {"viewer_id", "auth_key", "udid", "device_id"}
          <= set(acct.ACCOUNT_IDENTITY_KEYS))
    for kept in ("steam_refresh_token", "steam_session_ticket", "steam_username",
                 "steam_password_seed", "steam_shared_secret", "app_ver",
                 "res_ver", "device_name", "ip_address"):
        check(f"signup config keeps {kept}",
              kept not in acct.ACCOUNT_IDENTITY_KEYS)

    check("both transition endpoints are tried",
          [e for e, _ in acct.TRANSITION_ENDPOINTS]
          == ["account/get_by_transition_code", "account/chain_by_transition_code"])

    # Regression: pre_signup takes the attestation payload, NOT the Cygames SDK
    # version - that one belongs to account/index. Getting them the wrong way
    # round (the capture's labels are shifted) answers 102 on the signup after.
    check("pre_signup sends the attestation payload",
          acct.PRE_SIGNUP_BODY == {"attestation_type": 1, "device_token": None})
    check("pre_signup does not send the SDK version",
          "cygames_id_client_sdk_version" not in acct.PRE_SIGNUP_BODY)
    check("signup body matches the known-good reroll flow",
          acct.SIGNUP_BODY["attestation_type"] == 1
          and set(acct.SIGNUP_BODY) == {"credential", "error_code", "error_message",
                                        "attestation_type", "optin_user_birth",
                                        "dma_state", "country"})
    check("there is a pause between pre_signup and signup", acct.SIGNUP_GAP > 0)

    with tempfile.TemporaryDirectory() as tmp:
        state = Path(tmp) / "default"
        state.mkdir()
        settings = SimpleNamespace(state_dir=state,
                                   auth_config_path=Path(tmp) / "auth_config.json")
        check("no accounts initially", acct.load_accounts(settings) == [])
        check("unknown viewer has no link",
              acct.find_account(settings, 123) is None)

        acct.remember_account(settings, 103444444441, "MyPass2026", True, "TrainerTest01")
        found = acct.find_account(settings, 103444444441)
        check("saved link is found", found is not None)
        check("saved link keeps the password", found["password"] == "MyPass2026")
        check("saved link records the name", found["name"] == "TrainerTest01")

        # A failed publish must not count as protection.
        acct.remember_account(settings, 999, "MyPass2026", False)
        check("failed link is not treated as saved",
              acct.find_account(settings, 999) is None)
        check("re-saving replaces, not duplicates",
              len(acct.load_accounts(settings)) == 2)

        # Archiving preserves the old credentials before they are invalidated.
        settings.auth_config_path.write_text(
            json.dumps({"viewer_id": 101999999998, "auth_key": "ab" * 48}),
            encoding="utf-8")
        dest = acct.archive_auth_config(settings, note="test")
        check("auth_config archived", dest is not None and dest.is_file())
        saved = json.loads(dest.read_text(encoding="utf-8"))
        check("archive keeps the viewer id", saved["viewer_id"] == 101999999998)
        check("archive records why", saved["_archived_note"] == "test")

        settings.auth_config_path.unlink()
        check("archiving a missing config is safe",
              acct.archive_auth_config(settings) is None)

    # Recovering credentials from a captured account switch.
    dumps = os.environ.get("AIC_TEST_DUMPS")
    switch = Path(dumps) / "20260731_093457" if dumps else None
    if switch and switch.is_dir():
        viewer_id, auth_b64, name = acct.credentials_from_capture(switch)
        check("import finds the switched-to account", viewer_id == 103444444441)
        check("import reads the account name", name == "TrainerTest01")
        check("imported auth_key is 48 bytes",
              len(base64.b64decode(auth_b64)) == 48)
        # It must prefer the transition response over the throwaway signup that
        # happens moments earlier in the same capture.
        check("import prefers the real account over the throwaway",
              viewer_id != 500111222333)

        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = Path(tmp) / "auth_config.json"
            cfg_path.write_text(json.dumps({
                "viewer_id": 101999999998, "auth_key": "ab" * 48,
                "auth_key_len": 48, "udid": "u", "app_ver": "1.33.0",
                "res_ver": "10007110",
            }), encoding="utf-8")
            settings = SimpleNamespace(state_dir=Path(tmp),
                                       auth_config_path=cfg_path)
            acct.import_credentials(settings, viewer_id, auth_b64, name)
            written = json.loads(cfg_path.read_text(encoding="utf-8-sig"))
            check("import rewrites the viewer id",
                  written["viewer_id"] == 103444444441)
            check("import stores auth_key as hex",
                  len(written["auth_key"]) == 96
                  and all(c in "0123456789abcdef" for c in written["auth_key"]))
            check("import fixes auth_key_len", written["auth_key_len"] == 48)
            check("import keeps client-level fields",
                  written["app_ver"] == "1.33.0"
                  and written["res_ver"] == "10007110"
                  and written["udid"] == "u")

        try:
            acct.credentials_from_capture(Path(tmp) / "nope")
            check("missing dump dir raises", False)
        except acct.AccountError:
            check("missing dump dir raises", True)
    else:
        print("  (capture import: skipped, set AIC_TEST_DUMPS)")


def test_rotation() -> None:
    """Visit spacing and roster parsing for the multi-account loop."""
    print("rotation")
    import tempfile
    from types import SimpleNamespace

    from aic import rotate

    def roster(n):
        return [rotate.RosterAccount(1000 + i, "ChangeMe2026", f"a{i}") for i in range(n)]

    # The whole point: N accounts over a 50-minute career = 50/N between visits,
    # so each account is revisited exactly as its own career ends.
    check("5 accounts -> 10 minutes", rotate.interval_for(roster(5)) == 600.0)
    check("20 accounts -> 2.5 minutes", rotate.interval_for(roster(20)) == 150.0)
    check("1 account -> the whole career", rotate.interval_for(roster(1)) == 3000.0)
    check("a full lap equals the career length",
          rotate.interval_for(roster(7)) * 7 == 3000.0)
    check("career length is configurable",
          rotate.interval_for(roster(4), career_minutes=60) == 900.0)

    # Too many accounts cannot each get a workable slot.
    try:
        rotate.interval_for(roster(200))
        check("refuses an impossible cadence", False)
    except rotate.RotationError as exc:
        check("refuses an impossible cadence", "too tight" in str(exc))

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "roster.json"

        # Object form with options.
        path.write_text(json.dumps({
            "career_minutes": 50,
            "accounts": [
                {"trainer_id": "103 444 444 441", "password": "ChangeMe2026",
                 "name": "alpha"},
                {"trainer_id": "102222222220", "password": "ChangeMe2026"},
            ],
        }), encoding="utf-8")
        accounts, options = rotate.load_roster(path)
        check("roster parses", len(accounts) == 2)
        check("spaced trainer id parsed", accounts[0].trainer_id == 103444444441)
        check("plain trainer id parsed", accounts[1].trainer_id == 102222222220)
        check("options come through", options.get("career_minutes") == 50)
        check("name is used as the label", accounts[0].label == "alpha")
        check("unnamed falls back to the trainer id",
              accounts[1].label == "102 222 222 220")

        # Bare list form.
        path.write_text(json.dumps([
            {"trainer_id": "111222333444", "password": "ChangeMe2026"}]),
            encoding="utf-8")
        accounts, options = rotate.load_roster(path)
        check("bare list form works", len(accounts) == 1 and options == {})

        # Duplicates are dropped, not visited twice.
        path.write_text(json.dumps([
            {"trainer_id": "111222333444", "password": "A1bcdefg"},
            {"trainer_id": "111 222 333 444", "password": "A1bcdefg"}]),
            encoding="utf-8")
        check("duplicate trainer ids collapse",
              len(rotate.load_roster(path)[0]) == 1)

        # Missing fields must fail loudly, not silently skip an account.
        for bad, why in (([{"trainer_id": "111222333444"}], "no password"),
                         ([{"password": "A1bcdefg"}], "no trainer id"),
                         ([], "empty")):
            path.write_text(json.dumps(bad), encoding="utf-8")
            try:
                rotate.load_roster(path)
                check(f"rejects roster: {why}", False)
            except rotate.RotationError:
                check(f"rejects roster: {why}", True)

        try:
            rotate.load_roster(Path(tmp) / "nope.json")
            check("missing roster explains itself", False)
        except rotate.RotationError as exc:
            check("missing roster explains itself", "rotate.bat init" in str(exc))

        # init writes only accounts with a confirmed link.
        state = Path(tmp) / "state"
        state.mkdir()
        settings = SimpleNamespace(state_dir=state / "default",
                                   auth_config_path=state / "auth.json")
        (state / "default").mkdir()
        from aic import account as acct_mod
        acct_mod.remember_account(settings, 103444444441, "ChangeMe2026", True, "alpha")
        acct_mod.remember_account(settings, 999, "ChangeMe2026", False)
        out = Path(tmp) / "generated.json"
        count = rotate.write_roster_from_saved(settings, out)
        check("init writes only linked accounts", count == 1)
        made, _ = rotate.load_roster(out)
        check("generated roster round-trips",
              made[0].trainer_id == 103444444441 and made[0].name == "alpha")
        try:
            rotate.write_roster_from_saved(settings, out)
            check("init refuses to clobber", False)
        except rotate.RotationError as exc:
            check("init refuses to clobber", "--force" in str(exc))

    # With no settings there is no clock store, so every account falls back to
    # the opening stagger - which is the only thing `interval` still sizes now
    # that each later visit is scheduled from the career's own recorded clock.
    plan = rotate.describe_plan(roster(5), 600.0, 50.0)
    check("plan states the opening stagger",
          any("stagger 10m 0s" in line for line in plan), str(plan[:2]))
    check("plan lists every account", sum(1 for l in plan if "first visit" in l) == 5)
    check("and says why each one has no clock yet",
          sum(1 for l in plan if "no career on record" in l) == 5)


def test_farm() -> None:
    """Roster writing for the account farm. Losing an account here means losing
    a real game account, so the merge must never drop or duplicate."""
    print("account farm")
    import tempfile

    from aic import farm as farm_mod
    from aic import rotate

    check("ten-pull cost", farm_mod.TEN_PULL_COST == 1500)

    # Regression: a brand-new account cannot take Icarus' login(). It also calls
    # tool/start_session/load and read_info/index, which answer 1102/102 and
    # leave the session unusable - every later account-mutating call then 217s.
    # Inspect the AST, not the text: the docstring explaining this rule mentions
    # the very names being banned.
    import ast

    tree = ast.parse(Path(farm_mod.__file__).read_text(encoding="utf-8"))
    called_methods, endpoints, names = set(), set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Attribute):
                called_methods.add(node.func.attr)
            for arg in node.args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    endpoints.add(arg.value)
        elif isinstance(node, ast.Name):
            names.add(node.id)

    check("farm never calls .login()", "login" not in called_methods)
    check("farm never requests read_info/index",
          not any(e.startswith("read_info") for e in endpoints))
    check("farm never requests start_session/load",
          "tool/start_session/load" not in endpoints)
    check("farm does request start_session and load/index",
          {"tool/start_session", "load/index"} <= endpoints)
    check("farm signs up via pre_signup then signup",
          {"tool/pre_signup", "tool/signup"} <= endpoints)
    check("signup reuses the shared bodies",
          {"PRE_SIGNUP_BODY", "SIGNUP_BODY"} <= {n for n in names} | called_methods
          or "acct" in names)

    # Regression: the server does not release a slot instantly; a signup landing
    # in that window answers 1102 (or 207/217/709) and must be retried.
    for code in ("1102", "207", "217", "709"):
        check(f"{code} is treated as a held slot",
              farm_mod._is_stale_slot(RuntimeError(f"API error {code} on x")))
    check("unrelated errors are not retried as stale",
          not farm_mod._is_stale_slot(RuntimeError("API error 500 on x")))
    check("the ticket error is not a stale slot",
          not farm_mod._is_stale_slot(RuntimeError("API error 394 on x")))
    check("stale-slot codes are covered",
          set(farm_mod.STALE_ACCOUNT_CODES) >= {"207", "217", "709"})

    # Regression: requests must NOT be individually rate-limited. uma-reroll-farm
    # defines attach_pacing and deliberately never applies it - the gacha needs
    # gacha/index -> gacha/exec back to back, and a >10s delay inside that window
    # returns 1808. Wrapping the client added ~14s before every pull.
    check("no per-request pacing wrapper", not hasattr(farm_mod, "_paced"))
    check("no per-endpoint delay table", not hasattr(farm_mod, "PACE"))
    check("gaps sit between pulls, not inside them",
          farm_mod.PULL_GAP[1] < 10 and farm_mod.CYCLE_GAP[0] >= farm_mod.PULL_GAP[1])
    check("pull gap is short", farm_mod.PULL_GAP[1] <= 3.0)

    def made(vid, name=""):
        return farm_mod.FarmedAccount(viewer_id=vid,
                                      trainer_id=acct_fmt(vid),
                                      password="ChangeMe2026", name=name,
                                      link_set=True)

    from aic.account import format_trainer_id as acct_fmt

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "roster.json"

        farm_mod._save_roster(path, [made(111222333444, "farm01")])
        data = json.loads(path.read_text(encoding="utf-8"))
        check("roster written as an object", isinstance(data, dict))
        check("career_minutes defaulted", data.get("career_minutes") == 50)
        check("first account saved", len(data["accounts"]) == 1)
        check("trainer id is formatted",
              data["accounts"][0]["trainer_id"] == "111 222 333 444")

        # Saving again with more accounts must add, not replace.
        farm_mod._save_roster(path, [made(111222333444, "farm01"),
                                     made(555666777888, "farm02")])
        data = json.loads(path.read_text(encoding="utf-8"))
        check("second account appended", len(data["accounts"]) == 2)
        check("no duplicate from re-saving",
              len({a["trainer_id"] for a in data["accounts"]}) == 2)

        # An existing hand-written roster must survive a farm run.
        path.write_text(json.dumps({
            "career_minutes": 40,
            "accounts": [{"trainer_id": "999 888 777 666", "password": "ChangeMe2026",
                          "name": "manual"}],
        }), encoding="utf-8")
        farm_mod._save_roster(path, [made(111222333444, "farm01")])
        data = json.loads(path.read_text(encoding="utf-8"))
        check("pre-existing account preserved",
              any(a["name"] == "manual" for a in data["accounts"]))
        check("farmed account added alongside", len(data["accounts"]) == 2)
        check("existing options preserved", data["career_minutes"] == 40)

        # And the result must be loadable by the rotation.
        accounts, options = rotate.load_roster(path)
        check("farmed roster loads into the rotation", len(accounts) == 2)
        check("rotation reads the career length", options["career_minutes"] == 40)

        # A corrupt roster must not silently wipe the farm's own results.
        path.write_text("{not json", encoding="utf-8")
        farm_mod._save_roster(path, [made(111222333444, "farm01")])
        check("corrupt roster is replaced, not compounded",
              len(json.loads(path.read_text(encoding="utf-8"))["accounts"]) == 1)

    _farm_smoke_test(farm_mod)


def _farm_smoke_test(farm_mod) -> None:
    """Drive farm() end to end against a fake server.

    The constant/AST checks above never *execute* the loop, so a plain typo in
    the body sailed past them and only surfaced on a live run. This runs it.
    """
    import base64
    import tempfile
    from types import SimpleNamespace

    from aic import account as acct_mod
    from aic import steam as steam_mod

    BANNER = 30111

    class _FakeClient:
        def __init__(self, viewer):
            self.viewer_id = 0
            self._next_viewer = viewer
            self.auth_key_hex = ""
            self.coin_info = {"fcoin": 0}
            self.res_ver = "10007110"
            self.calls: list[str] = []

        def regen_sid(self):
            pass

        def refresh_cached_account_state(self, _data):
            pass

        def call(self, endpoint, payload=None, **_kw):
            self.calls.append(endpoint)
            if endpoint == "tool/signup":
                self.viewer_id = self._next_viewer
                return {"data": {"viewer_id": self._next_viewer,
                                 "auth_key": base64.b64encode(b"k" * 48).decode()}}
            if endpoint == "present/index":
                return {"data": {"present_array": [{}, {}]}}
            if endpoint == "present/receive_all":
                return {"data": {"reward_summary_info": {"add_fcoin": 18000}}}
            if endpoint == "gacha/index":
                return {"data": {"gacha_info_list": [
                    {"id": BANNER, "remain_stock_num_multi": 0,
                     "is_campaign_draw_enable_multi": 0}]}}
            if endpoint == "gacha/exec":
                return {"data": {"gacha_result_list":
                                 [{"card_id": 30000 + i} for i in range(10)]}}
            if endpoint == "account/publish_transition_code":
                return {"data_headers": {"result_code": 1}}
            return {"data": {}}

    with tempfile.TemporaryDirectory() as tmp:
        state = Path(tmp) / "default"
        state.mkdir(parents=True)
        cfg_path = Path(tmp) / "auth_config.json"
        cfg_path.write_text(json.dumps({
            "viewer_id": 111222333444, "auth_key": "ab" * 48, "auth_key_len": 48,
            "udid": "u", "app_ver": "1.33.0", "res_ver": "10007110",
        }), encoding="utf-8")
        settings = SimpleNamespace(state_dir=state, auth_config_path=cfg_path,
                                   icarus_root=Path(tmp))
        roster = Path(tmp) / "roster.json"

        # The slot's current account must look protected, or farm() refuses.
        acct_mod.remember_account(settings, 111222333444, "ChangeMe2026", True)

        made_clients = []

        def fake_new_client(_settings, _cfg):
            client = _FakeClient(500000000000 + len(made_clients))
            made_clients.append(client)
            return client

        originals = (farm_mod._new_client, farm_mod.refresh_steam_ticket,
                     farm_mod.time.sleep, steam_mod.has_live_session)
        try:
            farm_mod._new_client = fake_new_client
            farm_mod.refresh_steam_ticket = lambda *a, **k: False
            farm_mod.time.sleep = lambda _s: None
            steam_mod.has_live_session = lambda: True

            result = farm_mod.farm(settings, banner=BANNER, count=2, max_pulls=3,
                                   password="ChangeMe2026", roster_path=roster,
                                   name_prefix="t")
        finally:
            (farm_mod._new_client, farm_mod.refresh_steam_ticket,
             farm_mod.time.sleep, steam_mod.has_live_session) = originals

        check("farm() runs end to end", len(result) == 2, f"(got {len(result)})")
        check("each account pulled to the cap",
              all(len(a.cards) == 30 for a in result),
              f"(got {[len(a.cards) for a in result]})")
        check("data links were published", all(a.link_set for a in result))
        check("accounts are labelled", result[0].name == "t01")

        saved = json.loads(roster.read_text(encoding="utf-8"))
        check("roster written for both", len(saved["accounts"]) == 2)
        check("roster entries are usable",
              all(a.get("trainer_id") and a.get("password")
                  for a in saved["accounts"]))

        endpoints = made_clients[-1].calls
        check("signup precedes the gacha",
              endpoints.index("tool/signup") < endpoints.index("gacha/exec"))
        check("present/index follows receive_all, not load/index",
              endpoints[endpoints.index("present/receive_all") + 1] == "present/index")
        check("the slot is freed each cycle",
              any(e == "account/steam_chain_disconnect" for c in made_clients
                  for e in c.calls))

        # free_only must not spend carats when nothing is free.
        with_free = farm_mod._pull(_FakeClient(1), BANNER, 99999, 3, free_only=True)
        check("--free-only pulls nothing when nothing is free", with_free == [])


def test_end_sequence() -> None:
    """The order and the calls that must NOT happen."""
    print("end sequence")
    from aic.career import end_career
    from aic.state import CareerState

    class _Client:
        api_prefix = "single_mode_live"

        def __init__(self):
            self.calls: list[str] = []

        def set_scenario(self, sid):
            self.api_prefix = "single_mode_live"

        def call(self, endpoint, body, **kwargs):
            self.calls.append(endpoint)
            if endpoint.endswith("idle_single_mode/end"):
                return {"data": {"end_info": {"chara_info": {"turn": 78, "state": 3}},
                                 "progress_log_info": {"event_gain_info": {}}}}
            return {"data": {}}

        def factor_select(self, current_turn=0, skill_id_array=None):
            self.calls.append(f"factor_select(turn={current_turn})")

        def finish_career(self, current_turn=0, is_force_delete=False,
                          factor_lottery_id=1):
            self.calls.append(
                f"finish(turn={current_turn},force={is_force_delete},"
                f"lottery={factor_lottery_id})")
            return {"data": {}}

    class _Session:
        def __init__(self):
            self.client = _Client()
            self.clock = ServerClock()

    # Live run just expired: end, then finish at the turn end reported.
    s = _Session()
    live = CareerState(active=True, kind=KIND_IDLE, scenario_id=3, card_id=1,
                       turn=1, simulation_done=False)
    end_career(s, live, idl.Spec())
    check("stops the idle run first",
          s.client.calls[0] == "idle_single_mode/end", f"(got {s.client.calls})")
    check("selects factors at the real turn",
          "factor_select(turn=78)" in s.client.calls, f"(got {s.client.calls})")
    check("finishes at the real turn with the first lottery",
          "finish(turn=78,force=False,lottery=1)" in s.client.calls)
    check("never rerolls (factor_lottery)",
          not any("factor_lottery" in c for c in s.client.calls))
    check("never buys skills (gain_skills)",
          not any("gain_skills" in c for c in s.client.calls))

    # Already stopped: end must not be called again.
    s = _Session()
    done = CareerState(active=True, kind=KIND_IDLE, scenario_id=3, card_id=1,
                       turn=78, state=3, simulation_done=True)
    end_career(s, done, idl.Spec())
    check("skips end when already stopped",
          not any("idle_single_mode/end" in c for c in s.client.calls),
          f"(got {s.client.calls})")
    check("still finishes at turn 78",
          "finish(turn=78,force=False,lottery=1)" in s.client.calls)
    check("no progress-log call either",
          not any("check_progress_log" in c for c in s.client.calls))

    # A normal (non-idle) career must never touch the idle endpoints.
    s = _Session()
    normal = CareerState(active=True, kind=KIND_NORMAL, scenario_id=3, card_id=1,
                         turn=78)
    end_career(s, normal, idl.Spec())
    check("normal career skips idle endpoints",
          not any("idle_single_mode" in c for c in s.client.calls),
          f"(got {s.client.calls})")


def test_deck_rules(gamedata: GameData) -> None:
    print("deck rules")
    check("distinct characters pass",
          sel.deck_conflict([10001, 10002], 100301, gamedata) is None)
    check("duplicate character rejected",
          "duplicate character" in (sel.deck_conflict([10001, 10001], 100301, gamedata) or ""))
    check("trainee collision rejected",
          "same character as trainee" in
          (sel.deck_conflict([10001], 100101, gamedata) or ""))


def test_durations() -> None:
    print("formatting")
    check("hours", format_duration(3725) == "1h 2m 5s")
    check("minutes", format_duration(125) == "2m 5s")
    check("seconds", format_duration(9) == "9s")
    check("unknown", format_duration(None) == "unknown")


def test_with_capture(path: Path, gamedata: GameData) -> None:
    print(f"live capture: {path.name}")
    body = unwrap(str(path))
    home = body.get("data") or {}
    clock = ServerClock()
    clock.observe(body)

    account = read_account(home)
    print(f"    gold={account.gold:,} tp={account.current_tp} "
          f"succession_pts={account.succession_rank_point} "
          f"decks={len(account.support_card_decks)} "
          f"support={len(account.owned_support_card_ids)} "
          f"trainees={len(account.owned_card_ids)} "
          f"scenarios={account.available_scenario_ids}")
    check("parsed support cards", len(account.owned_support_card_ids) > 0)
    check("parsed trainees", len(account.owned_card_ids) > 0)
    check("parsed decks", len(account.support_card_decks) > 0)

    parents = sel.read_parents(home)
    print(f"    usable parents: {len(parents)}")
    check("parsed parents", len(parents) >= 2)

    rng = random.Random(1234)
    trainee = sel.pick_trainee(account, parents, gamedata, rng)
    p1, p2 = sel.pick_parents(parents, trainee, gamedata)
    print(f"    trainee: {gamedata.chara_name(trainee)} ({trainee})")
    print(f"    parents: {gamedata.chara_name(p1.card_id)} (rank {p1.rank}) + "
          f"{gamedata.chara_name(p2.card_id)} (rank {p2.rank})")
    tk = gamedata.chara_identity(trainee)
    check("parent 1 differs from trainee", gamedata.chara_identity(p1.card_id) != tk)
    check("parent 2 differs from trainee", gamedata.chara_identity(p2.card_id) != tk)
    check("parents differ from each other",
          gamedata.chara_identity(p1.card_id) != gamedata.chara_identity(p2.card_id))

    deck_id, ids, source = sel.choose_deck(account, trainee, gamedata, rng)
    print(f"    deck {deck_id} ({source}): {sel.describe_deck(ids, gamedata)}")
    check("deck has 5 cards", len(ids) == 5)
    check("deck is legal", sel.deck_conflict(ids, trainee, gamedata) is None)
    check("deck cards owned", set(ids) <= set(account.owned_support_card_ids))

    built = sel.build_random_deck(account, trainee, gamedata, random.Random(99))
    print(f"    built deck: {sel.describe_deck(built, gamedata)}")
    check("built deck has 5 cards", len(built) == 5)
    check("built deck is legal", sel.deck_conflict(built, trainee, gamedata) is None)
    check("built deck has no repeats", len(set(built)) == 5)
    check("built deck cards owned", set(built) <= set(account.owned_support_card_ids))

    # Affinity on the real pair the selector chose.
    from aic import succession
    relations = succession.load(str(ICARUS_ROOT))
    points = succession.compute(p1.as_family_node(), p2.as_family_node(), relations)
    print(f"    succession rank point: {points} "
          f"(account item-75 balance is {account.succession_rank_point})")
    check("affinity computed from the tree, not the balance",
          points != account.succession_rank_point or points == 0,
          "(they matched, which would hide the bug this guards)")
    check("affinity is a sane magnitude", 0 <= points < 2000, f"(got {points})")
    check("parents carry their family tree",
          bool(p1.succession_chara_array) and bool(p2.succession_chara_array))

    state = read_career(home, clock)
    print(f"    career: kind={state.kind} active={state.active}")


def test_with_dumps(root: Path) -> None:
    """Replay every captured idle_single_mode exchange through the parsers."""
    print(f"UmaDumpy dumps: {root}")

    seen = {"pre_start": 0, "start": 0, "end": 0, "check_progress_log": 0, "load_index": 0}
    idle_runs = 0

    for f in sorted(glob.glob(str(root / "*" / "messages" / "*.json"))):
        try:
            raw = json.loads(Path(f).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if raw.get("kind") != "response":
            continue
        endpoint = str(raw.get("endpoint") or "")
        body = raw.get("data") or {}
        data = body.get("data")
        if not isinstance(data, dict):
            continue

        clock = ServerClock()
        clock.observe(body)

        if endpoint.endswith("/load/index"):
            seen["load_index"] += 1
            state = read_career(data, clock)
            if state.kind == KIND_IDLE:
                idle_runs += 1
                check(f"idle run has a clock ({Path(f).name[:30]})",
                      state.finish_at is not None)
                check(f"idle duration is positive ({Path(f).name[:30]})",
                      (state.duration or 0) > 0)
                check(f"remaining <= duration ({Path(f).name[:30]})",
                      (state.seconds_remaining or 0) <= (state.duration or 0) + 1)

        elif endpoint.endswith("idle_single_mode/pre_start"):
            seen["pre_start"] += 1
            reserved = data.get("reserved_race_info") or {}
            pre = idl.PreStart(
                last_policy=data.get("last_idle_single_mode_start_info") or {},
                reserved_race_decks=list(reserved.get("reserved_race_array") or []),
                default_deck_num=int(reserved.get("default_deck_num") or 0),
            )
            schedule = idl.build_schedule(pre)
            payload = schedule.to_payload()
            check(f"pre_start yields a schedule ({Path(f).name[:30]})",
                  isinstance(payload["race_array"], list)
                  and payload["training_policy_ground_type"] in (1, 2))
            print(f"    pre_start: {len(pre.reserved_race_decks)} agendas, "
                  f"{len(payload['race_array'])} races chosen, "
                  f"{len(payload['priority_skill_array'])} priority skills")

        elif endpoint.endswith("idle_single_mode/start"):
            seen["start"] += 1
            progress = data.get("progress_info")
            if progress:
                state = read_progress_info(progress, clock)
                check(f"start response parses ({Path(f).name[:30]})",
                      state.is_idle and state.card_id > 0 and state.duration == 3000.0)
                print(f"    start: card {state.card_id} scenario {state.scenario_id}, "
                      f"run {format_duration(state.duration)}")

        elif endpoint.endswith("idle_single_mode/end"):
            seen["end"] += 1
            info = data.get("progress_log_info")
            if info:
                check(f"end response has a log ({Path(f).name[:30]})",
                      "event_gain_info" in info)
            if data.get("end_info"):
                chara = idl.final_chara_info(None, idl.Spec(), data)
                check(f"end_info yields the final turn ({Path(f).name[:30]})",
                      int(chara.get("turn") or 0) > 10,
                      f"(got turn {chara.get('turn')})")
                print(f"    end: turn {chara.get('turn')}, "
                      f"{chara.get('fans')} fans, state {chara.get('state')}")

        elif endpoint.endswith("idle_single_mode/check_progress_log"):
            seen["check_progress_log"] += 1
            if data.get("chara_info"):
                check(f"progress log has chara_info ({Path(f).name[:30]})",
                      int(data["chara_info"].get("card_id") or 0) > 0)

    print(f"    replayed: {seen}, idle runs seen in load/index: {idle_runs}")
    check("saw a real start capture", seen["start"] > 0)
    check("saw a real pre_start capture", seen["pre_start"] > 0)
    check("saw a real end capture", seen["end"] > 0)


def test_race_goal() -> None:
    """Filling to a race goal: goals kept, picks kept, richest races chosen,
    entry requirements respected, one race per turn."""
    print("race goal filling")
    from aic import racedata

    mdb = racedata.find_master_mdb()
    if not mdb:
        print("  skipped (master.mdb not found)")
        return
    raw = racedata.extract(mdb)
    rules = racedata.RaceRules(
        programs=raw["programs"], chara_groups=raw["chara_groups"],
        routes=raw["routes"], route_races=raw["route_races"],
        race_turns=raw["race_turns"])

    check("fan rewards were extracted",
          any(p["fans"] for p in rules.programs.values()))
    check("entry requirements were extracted",
          any(p["need_fan"] for p in rules.programs.values()))

    chara = 1041
    goals = rules.route_targets(chara)
    goal_keys = {(g["year"], g["program_id"]) for g in goals}
    picks = [{"year": 2, "program_id": 1023}]
    base = rules.build_race_array(chara, picks)
    protect = goal_keys | {(2, 1023)}   # goals + the hand-picked race
    filled = rules.fill_race_array(chara, base, target=35, protect=protect)
    keys = {(e["year"], e["program_id"]) for e in filled}

    check("hits the requested race goal", len(filled) == 35, f"(got {len(filled)})")
    check("every goal race survives", goal_keys <= keys)
    check("a hand-picked race survives", (2, 1023) in keys)

    # THE bug that refused an established account's start: two races on one turn.
    slots = [rules._slot(e["year"], e["program_id"]) for e in filled]
    check("no two races share a turn", len(slots) == len(set(slots)),
          f"({len(slots) - len(set(slots))} collisions)")

    # An agenda that deliberately collides with the route goals must still come
    # out collision-free - that is exactly the real failure.
    collide_agenda = [{"year": g["year"], "program_id": g["program_id"]}
                      for g in goals]
    # add a *different* legal race in each goal's own turn slot, if one exists
    for g in goals:
        gslot = rules._slot(g["year"], g["program_id"])
        for pid_text, prog in rules.programs.items():
            pid = int(pid_text)
            if pid == g["program_id"]:
                continue
            if (rules.is_legal(chara, g["year"], pid)
                    and rules._slot(g["year"], pid) == gslot):
                collide_agenda.append({"year": g["year"], "program_id": pid})
                break
    built2 = rules.build_race_array(chara, collide_agenda)
    bslots = [rules._slot(e["year"], e["program_id"]) for e in built2]
    check("build_race_array never doubles up a turn",
          len(bslots) == len(set(bslots)),
          f"({len(bslots) - len(set(bslots))} collisions)")
    check("and it still keeps every goal race",
          goal_keys <= {(e["year"], e["program_id"]) for e in built2})

    # Every GOAL race must be enterable when it comes round (optional races that
    # end up unenterable are fine - a career earns fans from training too).
    earned, goal_locked = 0, 0
    for e in sorted(filled, key=lambda e: rules._slot(e["year"], e["program_id"])):
        if (e["year"], e["program_id"]) in goal_keys \
                and rules.need_fans(e["program_id"]) > earned:
            goal_locked += 1
        earned += rules.fan_reward(e["program_id"])
    check("every goal race is enterable when it comes round", goal_locked == 0,
          f"({goal_locked} goals unreachable)")

    # Year 3 pays the most, so a fan-hungry plan must not starve it.
    per_year = {}
    for e in filled:
        per_year[e["year"]] = per_year.get(e["year"], 0) + 1
    check("year 3 is not starved", per_year.get(3, 0) >= 10,
          f"(per year {per_year})")

    smaller = rules.fill_race_array(chara, base, target=20, protect=protect)
    check("a smaller goal yields fewer races", len(smaller) == 20,
          f"(got {len(smaller)})")
    check("goal races survive the smaller plan too",
          goal_keys <= {(e["year"], e["program_id"]) for e in smaller})
    check("the bigger plan pays more fans",
          sum(rules.fan_reward(e["program_id"]) for e in filled)
          > sum(rules.fan_reward(e["program_id"]) for e in smaller))

    # A target below the mandatory count cannot drop goals.
    tiny = rules.fill_race_array(chara, base, target=1, protect=goal_keys)
    check("goals are never dropped to meet a tiny goal",
          goal_keys <= {(e["year"], e["program_id"]) for e in tiny})
    check("the default protect set is the route goals",
          {(e["year"], e["program_id"])
           for e in rules.fill_race_array(chara, list(goals), target=1)}
          >= goal_keys)

    check("an unambiguous program resolves its own year",
          rules.place(goals[0]["program_id"]) in (0, goals[0]["year"]))


def test_parse_races() -> None:
    print("race selection parsing")
    from aic.cli import parse_races

    check("year:program pairs parse",
          parse_races("2:1023,3:4")
          == [{"year": 2, "program_id": 1023}, {"year": 3, "program_id": 4}])
    check("spaces are tolerated",
          parse_races(" 2:1023 , 3:4 ")
          == [{"year": 2, "program_id": 1023}, {"year": 3, "program_id": 4}])
    check("a bare program id defers its year",
          parse_races("1023") == [{"year": 0, "program_id": 1023}])
    check("empty means no races", parse_races("") == [] and parse_races(None) == [])
    try:
        parse_races("nonsense")
        check("garbage is rejected", False)
    except Exception as exc:  # noqa: BLE001
        check("garbage is rejected", "races" in str(exc), f"({exc})")


def test_start_request_shape() -> None:
    """Our submission body must match the real client's, field for field.

    The real submission goes to ``idle_single_mode/pre_start`` - three real
    captures against three different accounts (2026-08-02) each sent the full
    ``single_mode_start_request_common`` + ``start_info`` payload there and got
    ``progress_info`` back in the same call; the later ``idle_single_mode/start``
    call in each carried only a ``support_card_deck_array`` snapshot. See
    :mod:`aic.independent`'s module docstring.
    """
    print("start payload shape")
    dumps = os.environ.get("AIC_TEST_DUMPS")
    if not dumps:
        print("  skipped (set AIC_TEST_DUMPS)")
        return
    matches = sorted(glob.glob(str(Path(dumps) / "*" / "messages"
                                  / "*request*idle_single_mode_pre_start.json")))
    real = None
    for f in matches:
        payload = json.loads(Path(f).read_text(encoding="utf-8")).get("data") or {}
        if "single_mode_start_request_common" in payload:
            real = payload
            break
    if not real:
        print("  skipped (no captured pre_start request found)")
        return

    class _Sel:
        card_id = 1
        support_card_ids = [1, 2, 3, 4, 5]
        friend_viewer_id = 0
        friend_card_id = 0
        parent_id_1 = 1
        parent_id_2 = 2
        deck_id = 1
        running_style = 1

    class _Acct:
        tp_info = {"current_tp": 62, "max_tp": 100, "max_recovery_time": 0}
        gold = 1
        succession_rank_point = 1
        current_tp = 62

    class _Client:
        def __init__(self):
            self.calls = []

        def call(self, endpoint, body, **kwargs):
            self.calls.append((endpoint, body))
            return {"data": {}}

    class _Session:
        def __init__(self):
            self.client = _Client()
            self.clock = ServerClock()

    # Pin to the capture's price so the comparison is like for like; the probe
    # order is covered separately by test_tp_probe_calls.
    captured_tp = real["single_mode_start_request_common"]["use_tp"]
    session = _Session()
    idl.start(session, idl.Spec(), _Sel(), _Acct(),
              idl.Schedule(race_array=[{"year": 1, "program_id": 1}]),
              scenario_id=3, use_tp=captured_tp)
    # Flow B accepted immediately: one call, no deck echo (that belongs to A).
    check("one call on a flow-B success",
          len(session.client.calls) == 1, f"(got {len(session.client.calls)})")
    endpoint, ours = session.client.calls[0]

    check("flow-B submission posts to idle_single_mode/start",
          endpoint == "idle_single_mode/start")
    check("top-level keys match",
          set(ours) == {"single_mode_start_request_common", "start_info"},
          f"(got {sorted(ours)})")
    check("start_chara keys match",
          set(ours["single_mode_start_request_common"]["start_chara"])
          == set(real["single_mode_start_request_common"]["start_chara"]),
          f"(missing {set(real['single_mode_start_request_common']['start_chara']) - set(ours['single_mode_start_request_common']['start_chara'])})")
    check("common block keys match",
          set(ours["single_mode_start_request_common"])
          == set(real["single_mode_start_request_common"]))
    check("start_info keys match",
          set(ours["start_info"]) == set(real["start_info"]),
          f"(real has {sorted(real['start_info'])})")
    check("use_tp matches the capture",
          ours["single_mode_start_request_common"]["use_tp"]
          == real["single_mode_start_request_common"]["use_tp"],
          f"(ours {ours['single_mode_start_request_common']['use_tp']}, "
          f"capture {real['single_mode_start_request_common']['use_tp']})")

    # Flow-A fallback: when the `start` endpoint refuses, the same payload on
    # `pre_start` must be tried, and a success there is followed by the
    # deck-echo confirmation - the sequence the 08-02 captures show.
    class _Err(Exception):
        def __init__(self, rc):
            super().__init__(f"API error {rc}")
            self.result_code = rc

    class _BRejects:
        def __init__(self):
            self.calls = []

        def call(self, endpoint, body, **kwargs):
            self.calls.append((endpoint, json.loads(json.dumps(body))))
            if endpoint == "idle_single_mode/start" and "start_info" in body:
                raise _Err(205)
            return {"data": {}}

    class _ASession:
        def __init__(self):
            self.client = _BRejects()
            self.clock = ServerClock()

    session_a = _ASession()
    idl.start(session_a, idl.Spec(), _Sel(), _Acct(),
              idl.Schedule(race_array=[{"year": 1, "program_id": 1}]),
              scenario_id=3, use_tp=captured_tp)
    calls = session_a.client.calls
    check("flow A tried after B is refused",
          len(calls) == 3 and calls[0][0] == "idle_single_mode/start"
          and calls[1][0] == "idle_single_mode/pre_start",
          f"(got {[c[0] for c in calls]})")
    check("flow-A submission carries the full payload",
          "single_mode_start_request_common" in calls[1][1])
    deck_endpoint, deck_body = calls[2]
    check("A-path success is followed by the deck echo",
          deck_endpoint == "idle_single_mode/start"
          and len(deck_body.get("support_card_deck_array") or []) == 10)
    check("deck 1 carries the support cards used, auto_deck_flag 1",
          deck_body["support_card_deck_array"][0]
          == {"deck_id": 1, "support_card_id_array": [1, 2, 3, 4, 5],
              "auto_deck_flag": 1})
    check("decks 2-10 are empty placeholders",
          all(d == {"deck_id": d["deck_id"], "support_card_id_array": [0, 0, 0, 0, 0],
                    "auto_deck_flag": 0}
              for d in deck_body["support_card_deck_array"][1:]))


def main() -> int:
    if not (ICARUS_ROOT / "data" / "support_list.json").is_file():
        print(f"error: Icarus data not found under {ICARUS_ROOT}", file=sys.stderr)
        return 2
    # Some helpers reach into Icarus' uma_api (the SALT, the AES/IV derivation).
    # Put it on the path once, the way bootstrap.bind() would at runtime.
    if str(ICARUS_ROOT) not in sys.path:
        sys.path.insert(0, str(ICARUS_ROOT))

    gamedata = GameData(ICARUS_ROOT)
    print(f"loaded {len(gamedata.support_map)} support cards, "
          f"{len(gamedata.chara_map)} characters\n")

    test_name_normalization()
    test_server_clock()
    test_clock_anchor_staleness()
    test_clock_source_ranking()
    test_clock_persistence()
    test_career_detection()
    test_progress_info()
    test_schedule()
    test_spec_overrides()
    test_tp_probe_order()
    test_tp_probe_calls()
    test_final_turn_resolution()
    test_succession_points()
    test_succession_sweep()
    test_friend_card(gamedata)
    test_forced_parents()
    test_scenario_choice()
    test_race_rules()
    test_ticket_reuse()
    test_steam_session()
    test_capture()
    test_account_switching()
    test_rotation()
    test_farm()
    test_end_sequence()
    test_deck_rules(gamedata)
    test_durations()
    test_race_goal()
    test_parse_races()
    test_start_request_shape()

    capture = os.environ.get("AIC_TEST_CAPTURE")
    if capture and Path(capture).is_file():
        test_with_capture(Path(capture), gamedata)
    else:
        print("live capture: skipped (set AIC_TEST_CAPTURE)")

    dumps = os.environ.get("AIC_TEST_DUMPS")
    if dumps and Path(dumps).is_dir():
        test_with_dumps(Path(dumps))
    else:
        print("UmaDumpy dumps: skipped (set AIC_TEST_DUMPS)")

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
