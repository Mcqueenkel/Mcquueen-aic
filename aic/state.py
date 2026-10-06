"""Reading account and career state out of ``load/index`` and friends.

Two kinds of career can be running:

* a **normal** turn-by-turn career, summarized in ``single_mode_chara_light``;
* an **idle** (Independent) career, in ``idle_single_mode_load_info``, which the
  server runs on a wall-clock timer and which exposes ``start_time``/``end_time``.

While an idle career runs, the top-level ``single_mode_chara_light`` is ``null``
- the idle block is the only place it appears.
"""

from __future__ import annotations

import calendar
import json
import time
from dataclasses import dataclass, field
from typing import Any, Iterator

from .config import ITEM_CLOCK, ITEM_GOLD, ITEM_SUCCESSION_RANK_POINT
from .logging_setup import get

log = get("state")

#: Timestamps outside this window are not career clocks (they are register
#: times, campaign end dates, ...). One year either side of now.
_TS_SLACK = 365 * 24 * 3600

#: Formats the server uses for naive datetime strings.
_TIME_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S")

#: Fallback seconds to add to a naive server datetime to reach real epoch.
#: Observed as +3600 (server renders local times at UTC-1) but derived at
#: runtime whenever an anchor is available - see :class:`ServerClock`.
DEFAULT_SERVER_UTC_OFFSET = 3600


def _as_int(value, default: int = 0) -> int:
    """Tolerant int(). Trace captures carry ``"<redacted>"`` in credential
    fields, and the server occasionally sends numeric ids as strings."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------------- #
# server clock
# --------------------------------------------------------------------------- #

def parse_naive(value: Any) -> float | None:
    """Seconds for a server datetime string read *as if it were UTC*.

    The real epoch is this plus :attr:`ServerClock.offset`. Kept separate so the
    offset can be corrected without re-parsing.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    for fmt in _TIME_FORMATS:
        try:
            return float(calendar.timegm(time.strptime(text, fmt)))
        except ValueError:
            continue
    return None


#: How far in the future an anchor datetime may sit before we stop believing
#: it is "the recent past" (absorbs clock jitter between server nodes).
_ANCHOR_FUTURE_TOLERANCE = 300


class ServerClock:
    """Converts the server's naive datetime strings to real epoch seconds.

    The server sends wall-clock strings with no timezone, while ``data_headers``
    carries a real ``servertime`` epoch on every response. Two ways to recover
    the offset between them, in order of trust:

    1. **A running career's own window.** An idle run is under an hour long and
       timezone offsets are whole hours, so at most one offset can place
       ``servertime`` inside ``[start_time, end_time]``. That makes it exact.

    2. **An anchor datetime**, ``user_info.update_time``. This is only ever in
       the past, and by an unknown amount - the server stamps it when account
       state last changed, which during an idle run means when the run started.
       So the offset is the *floor* of the gap, not the nearest hour: rounding
       sends a 38-minutes-stale anchor to the wrong hour, which inflates every
       countdown by 3600s.
    """

    #: Calibration sources, best first. A source never loses to a worse one.
    _RANK = {"assumed": 0, "cached": 1, "update_time anchor": 2,
             "career window": 3, "fresh start_time": 4}

    def __init__(self, offset: int = DEFAULT_SERVER_UTC_OFFSET,
                 calibration: str = "assumed"):
        self.offset = offset
        self.calibration = calibration
        self.servertime: float | None = None

    @property
    def derived(self) -> bool:
        return self.calibration != "assumed"

    def _accept(self, offset: int, source: str) -> bool:
        """Adopt ``offset`` if ``source`` is at least as trustworthy as the
        current one. Prevents a weak signal from undoing an exact one."""
        if self._RANK[source] < self._RANK[self.calibration]:
            if offset != self.offset:
                log.debug("ignoring %s offset %+ds; keeping %+ds from %s",
                          source, offset, self.offset, self.calibration)
            return False
        if offset != self.offset:
            log.debug("server clock offset %+ds (was %+ds) via %s",
                      offset, self.offset, source)
        self.offset = offset
        self.calibration = source
        return True

    def calibrate_from_fresh(self, value) -> bool:
        """Pin the offset from a datetime the server stamped just now.

        The unambiguous case: a career's ``start_time`` in the response to our
        own start request is seconds old, so the gap to ``servertime`` is the
        offset plus round-trip. Unlike the window method this cannot be fooled
        by a run that expired hours ago.
        """
        naive = parse_naive(value)
        if naive is None or self.servertime is None:
            return False
        delta = self.servertime - naive
        offset = int(round(delta / 3600.0)) * 3600
        if abs(delta - offset) > _ANCHOR_FUTURE_TOLERANCE:
            log.debug("'fresh' datetime was %.0fs off a whole hour; not trusting it",
                      delta - offset)
            return False
        return self._accept(offset, "fresh start_time")

    def observe(self, response: dict) -> None:
        """Learn what we can from a full API response (headers + data)."""
        headers = (response or {}).get("data_headers") or {}
        servertime = headers.get("servertime")
        if isinstance(servertime, (int, float)):
            self.servertime = float(servertime)

        data = (response or {}).get("data") or {}

        # Exact, when a timed career is running - prefer it over the anchor.
        idle = data.get("idle_single_mode_load_info")
        if isinstance(idle, dict) and idle:
            if self.calibrate_from_window(idle.get("start_time"), idle.get("end_time")):
                return

        self._calibrate_from_anchor((data.get("user_info") or {}).get("update_time"))

    def calibrate_from_window(self, start_value, end_value) -> bool:
        """Pin the offset from a career's ``[start_time, end_time]`` window.

        Returns whether a unique offset was found. Requires the window to be
        shorter than an hour - that is what guarantees uniqueness - and
        ``servertime`` to fall inside it, so this silently declines for a run
        that has already expired.
        """
        if self.servertime is None:
            return False
        start = parse_naive(start_value)
        end = parse_naive(end_value)
        if start is None or end is None or not 0 < end - start < 3600:
            return False

        solutions = [
            hours * 3600
            for hours in range(-11, 15)
            if start + hours * 3600 <= self.servertime <= end + hours * 3600
        ]
        if len(solutions) != 1:
            log.debug("career window gave %d candidate offsets; not calibrating",
                      len(solutions))
            return False
        # A run that expired N hours ago yields the same unique solution as a
        # live run under an offset N hours larger - the two are indistinguishable
        # from these fields. _accept keeps a better-sourced offset, which is what
        # stops that ambiguity from corrupting an already-pinned clock.
        return self._accept(solutions[0], "career window")

    def _calibrate_from_anchor(self, value) -> bool:
        anchor = parse_naive(value)
        if anchor is None or self.servertime is None:
            return False

        delta = self.servertime - anchor
        if not -_ANCHOR_FUTURE_TOLERANCE <= delta <= 14 * 3600:
            log.debug("server-time anchor is %.2fh out; keeping offset %+ds",
                      delta / 3600, self.offset)
            return False

        # floor, not round: the anchor is always in the past by an unknown
        # amount, so the whole-hour part of the gap is the offset and the
        # remainder is staleness.
        offset = int((delta + _ANCHOR_FUTURE_TOLERANCE) // 3600) * 3600
        return self._accept(offset, "update_time anchor")

    def to_epoch(self, value: Any) -> float | None:
        """Real epoch seconds for a server datetime string (or a raw epoch)."""
        if isinstance(value, bool) or value is None:
            return None
        if isinstance(value, (int, float)):
            return float(value) if value > 10**8 else None
        naive = parse_naive(value)
        return None if naive is None else naive + self.offset

    def now(self) -> float:
        """Best estimate of the server's current epoch time."""
        return self.servertime if self.servertime is not None else time.time()


# --------------------------------------------------------------------------- #
# account
# --------------------------------------------------------------------------- #

@dataclass
class AccountState:
    viewer_id: int = 0
    gold: int = 0
    clocks: int = 0
    succession_rank_point: int = 0
    tp_info: dict = field(default_factory=dict)
    item_map: dict[int, int] = field(default_factory=dict)
    support_card_decks: list[dict] = field(default_factory=list)
    last_selected_deck_id: int = 0
    owned_support_card_ids: list[int] = field(default_factory=list)
    #: Per-owned-card {"id", "exp", "limit_break_count"}, for fan-bonus scoring.
    owned_support_cards: list[dict] = field(default_factory=list)
    owned_card_ids: list[int] = field(default_factory=list)
    available_scenario_ids: list[int] = field(default_factory=list)
    idle_mode_available: bool = False
    #: LIFETIME fan total (``user_info.fan``). The daily fan cap measures its
    #: own target as a delta from this - see :mod:`aic.fancap`.
    fans: int = 0

    @property
    def current_tp(self) -> int:
        return _as_int(self.tp_info.get("current_tp"))


def read_account(load_index: dict) -> AccountState:
    items = {}
    for item in load_index.get("item_list") or []:
        iid, num = _as_int(item.get("item_id"), -1), _as_int(item.get("number"), -1)
        if iid >= 0 and num >= 0:
            items[iid] = num

    decks = []
    for deck in load_index.get("support_card_deck_array") or []:
        ids = [_as_int(x) for x in (deck.get("support_card_id_array") or [])]
        decks.append({
            "deck_id": _as_int(deck.get("deck_id")),
            "name": deck.get("name") or "",
            "support_card_ids": [i for i in ids if i],
        })

    owned_support = []
    owned_support_detail = []
    for card in load_index.get("support_card_list") or []:
        sid = _as_int(card.get("support_card_id") or card.get("id"))
        if sid:
            owned_support.append(sid)
            owned_support_detail.append({
                "id": sid,
                "exp": _as_int(card.get("exp")),
                "limit_break_count": _as_int(card.get("limit_break_count")),
            })

    owned_cards = []
    for card in load_index.get("card_list") or []:
        cid = _as_int(card.get("card_id") or card.get("id"))
        if cid:
            owned_cards.append(cid)

    common = load_index.get("common_define") or {}

    user_info = load_index.get("user_info") or {}

    return AccountState(
        viewer_id=_as_int(user_info.get("viewer_id")),
        fans=_as_int(user_info.get("fan")),
        gold=items.get(ITEM_GOLD, 0),
        clocks=items.get(ITEM_CLOCK, 0),
        succession_rank_point=items.get(ITEM_SUCCESSION_RANK_POINT, 0),
        tp_info=load_index.get("tp_info") or {},
        item_map=items,
        support_card_decks=decks,
        last_selected_deck_id=_as_int(load_index.get("last_select_support_card_deck_id")),
        owned_support_card_ids=owned_support,
        owned_support_cards=owned_support_detail,
        owned_card_ids=owned_cards,
        available_scenario_ids=[
            _as_int(x) for x in (load_index.get("single_mode_scenario_id_array") or [])
            if _as_int(x)
        ],
        # Present once the feature has unlocked for the account.
        idle_mode_available=bool(common.get("idle_single_mode_open_date")),
    )


# --------------------------------------------------------------------------- #
# career
# --------------------------------------------------------------------------- #

KIND_NONE = "none"
KIND_NORMAL = "normal"
KIND_IDLE = "idle"

#: ``chara_info.state`` once a career has played out and is awaiting its finish
#: sequence. A live career reports 0.
STATE_COMPLETE = 3

#: An idle career reports ``turn: 1`` for its whole run; the real turn only
#: appears once the simulation has been stopped. Anything above this therefore
#: means the run is already over.
LIVE_IDLE_TURN_CEILING = 10


@dataclass
class CareerState:
    active: bool = False
    kind: str = KIND_NONE
    scenario_id: int = 0
    card_id: int = 0
    turn: int = 0
    playing_state: int = 0
    state: int = 0
    start_at: float | None = None
    finish_at: float | None = None
    finish_source: str = ""
    #: For an idle career: the simulation has already been stopped with
    #: ``idle_single_mode/end`` and the career is waiting to be finished.
    #: Calling ``end`` again answers 205.
    simulation_done: bool = False
    #: Server "now" when this snapshot was taken. Paired with
    #: ``local_observed_at`` so the countdown never depends on the local clock
    #: agreeing with the server's.
    observed_at: float = 0.0
    local_observed_at: float = field(default_factory=time.time)
    raw: dict = field(default_factory=dict)

    @property
    def is_idle(self) -> bool:
        return self.kind == KIND_IDLE

    @property
    def duration(self) -> float | None:
        if self.start_at is None or self.finish_at is None:
            return None
        return self.finish_at - self.start_at

    @property
    def seconds_remaining(self) -> float | None:
        """Seconds until the idle timer expires, clamped to [0, duration].

        Measured as "what was left when the server told us" minus "how long we
        have been running since", so a local clock that disagrees with the
        server's never skews the countdown. The upper clamp guards against a
        wrong server-clock offset: a run can never have more time left than its
        own total duration.
        """
        if self.finish_at is None:
            return None
        at_snapshot = self.finish_at - (self.observed_at or self.local_observed_at)
        elapsed = max(0.0, time.time() - self.local_observed_at)
        remaining = max(0.0, at_snapshot - elapsed)
        total = self.duration
        if total is not None and total > 0:
            remaining = min(remaining, total)
        return remaining

    @property
    def finished(self) -> bool:
        rem = self.seconds_remaining
        return rem is not None and rem <= 0


def read_career(load_index: dict, clock: ServerClock | None = None) -> CareerState:
    """Detect an in-progress career from the home payload.

    An idle career takes precedence: while one runs the server nulls out the
    top-level ``single_mode_chara_light``, so finding the idle block first
    avoids ever reporting "no career" during an Independent run.
    """
    clock = clock or ServerClock()

    idle = load_index.get("idle_single_mode_load_info")
    if isinstance(idle, dict) and idle:
        # Idempotent, and covers a clock built from a response we did not
        # observe (a hand-fed payload, a replayed capture).
        clock.calibrate_from_window(idle.get("start_time"), idle.get("end_time"))
        light = idle.get("single_mode_chara_light") or {}
        state = _from_light(light, KIND_IDLE, clock)
        state.active = True
        state.playing_state = _as_int(idle.get("playing_state"), state.playing_state)
        state.start_at = clock.to_epoch(idle.get("start_time")) or state.start_at
        finish = clock.to_epoch(idle.get("end_time"))
        if finish is not None:
            state.finish_at = finish
            state.finish_source = "idle_single_mode_load_info.end_time"
        else:
            log.warning("idle career has no parseable end_time (%r)", idle.get("end_time"))
        # Once the simulation has been stopped the home block starts reporting
        # the real turn and a completed state; both stay at their initial values
        # for the whole live run.
        state.simulation_done = (state.state == STATE_COMPLETE
                                 or state.turn > LIVE_IDLE_TURN_CEILING)
        state.raw = idle
        return state

    light = load_index.get("single_mode_chara_light")
    if isinstance(light, list):
        light = light[0] if light else {}
    if not isinstance(light, dict) or not _as_int(light.get("card_id")):
        return CareerState(active=False, kind=KIND_NONE, observed_at=clock.now())

    state = _from_light(light, KIND_NORMAL, clock)
    state.active = True
    return state


def _from_light(light: dict, kind: str, clock: ServerClock) -> CareerState:
    return CareerState(
        kind=kind,
        scenario_id=_as_int(light.get("scenario_id")),
        card_id=_as_int(light.get("card_id")),
        turn=_as_int(light.get("turn")),
        playing_state=_as_int(light.get("playing_state")),
        state=_as_int(light.get("state")),
        start_at=clock.to_epoch(light.get("start_time")),
        observed_at=clock.now(),
        raw=light,
    )


def read_progress_info(progress_info: dict, clock: ServerClock) -> CareerState:
    """CareerState from an ``idle_single_mode/start`` response's ``progress_info``."""
    chara = (progress_info or {}).get("chara_info") or {}
    state = _from_light(chara, KIND_IDLE, clock)
    state.active = True
    state.start_at = clock.to_epoch(progress_info.get("start_time")) or state.start_at
    finish = clock.to_epoch(progress_info.get("end_time"))
    if finish is not None:
        state.finish_at = finish
        state.finish_source = "progress_info.end_time"
    state.raw = progress_info
    return state


def enrich_from_chara_info(state: CareerState, chara_info: dict) -> CareerState:
    """Fold a ``chara_info`` block (from check_progress_log or <prefix>/load)
    into an existing snapshot."""
    if not chara_info:
        return state
    state.active = True
    state.card_id = _as_int(chara_info.get("card_id"), state.card_id)
    state.turn = _as_int(chara_info.get("turn"), state.turn)
    state.scenario_id = _as_int(chara_info.get("scenario_id"), state.scenario_id)
    state.playing_state = _as_int(chara_info.get("playing_state"), state.playing_state)
    state.state = _as_int(chara_info.get("state"), state.state)
    return state


# --------------------------------------------------------------------------- #
# discovery helpers
# --------------------------------------------------------------------------- #

def candidate_timestamps(payload: dict, clock: ServerClock | None = None,
                         limit: int = 40) -> list[tuple[str, Any]]:
    """Fields that look like a near-future clock, for ``aic discover``."""
    clock = clock or ServerClock()
    now = clock.now()
    found = []
    for path, _key, value in walk(payload):
        epoch = clock.to_epoch(value) if isinstance(value, str) else (
            float(value) if isinstance(value, (int, float))
            and not isinstance(value, bool) and value > 10**8 else None
        )
        if epoch is not None and now < epoch < now + _TS_SLACK:
            found.append((path, value, epoch))
    found.sort(key=lambda row: row[2])
    return [(p, v) for p, v, _ in found[:limit]]


def walk(node: Any, path: str = "", depth: int = 0) -> Iterator[tuple[str, str, Any]]:
    """Yield ``(dotted_path, leaf_key, value)`` for every scalar in a payload."""
    if depth > 12:
        return
    if isinstance(node, dict):
        for key, value in node.items():
            child = f"{path}.{key}" if path else str(key)
            if isinstance(value, (dict, list)):
                yield from walk(value, child, depth + 1)
            else:
                yield child, str(key), value
    elif isinstance(node, list):
        for idx, value in enumerate(node[:200]):
            child = f"{path}[{idx}]"
            if isinstance(value, (dict, list)):
                yield from walk(value, child, depth + 1)
            else:
                yield child, path.rsplit(".", 1)[-1], value


def load_clock(settings) -> ServerClock:
    """A clock seeded with the offset last learned for this profile.

    The offset is a property of the server, not of the account or the run, so
    carrying it across runs is both safe and what makes the ambiguous cases
    (a career that expired hours ago) harmless.
    """
    path = settings.state_dir / "server_clock.json"
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            cached = json.load(fh)
        offset = int(cached["offset"])
    except (OSError, ValueError, KeyError, TypeError):
        return ServerClock()
    if offset % 3600 or not -11 * 3600 <= offset <= 14 * 3600:
        log.debug("cached server clock offset %s is implausible; ignoring", offset)
        return ServerClock()
    return ServerClock(offset=offset, calibration="cached")


def save_clock(settings, clock: ServerClock) -> None:
    if not clock.derived or clock.calibration == "cached":
        return
    path = settings.state_dir / "server_clock.json"
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"offset": clock.offset, "source": clock.calibration,
                       "learned_at": int(time.time())}, fh)
    except OSError as exc:
        log.debug("could not persist the server clock: %s", exc)


def format_duration(seconds: float | None) -> str:
    if seconds is None:
        return "unknown"
    seconds = int(max(0, seconds))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"
