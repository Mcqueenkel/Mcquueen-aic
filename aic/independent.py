"""The Independent Career ("idle single mode") API.

Endpoints and payload shapes below are transcribed from captures of the real
client under ``UmaDumpy-main/dumps/`` - none of this exists in Icarus.

    idle_single_mode/pre_start           -> light probe (flow B) OR the full
                                             submission (flow A) - see below
    idle_single_mode/start               -> the full submission (flow B) OR a
                                             deck-echo follow-up (flow A)
    idle_single_mode/check_progress_log  -> career state while/after it runs
    idle_single_mode/end                 -> stop the idle timer, collect the log

The real client has been captured using **two different submission flows**,
both accepted by the server:

* **Flow B** (2026-07-30, two sessions): ``pre_start`` as a light probe
  (``scenario_id: 0``, no payload) -> the full payload on ``start``. The
  career is created by ``start``; no deck echo follows.
* **Flow A** (2026-07-24 and 2026-08-02, four sessions): the full payload on
  ``pre_start`` (career created there, ``progress_info`` in its response) ->
  ``start`` as a deck-echo follow-up.

What the server has **never** been observed accepting is the hybrid this
module briefly shipped after misreading the 08-02 captures: a light
``pre_start`` probe followed by a full payload on ``pre_start`` again. On the
one account tested, that sequence answered 205 to every value of every field -
a blanket rejection that looks like a wrong field but is actually a wrong
*sequence*. Flow B is therefore the default (it is what this tool ran for
weeks of successful careers), and Flow A is attempted as a fallback whenever
B's initial probes are refused; whichever works is what the succession search
continues on. :func:`confirm_deck` sends Flow A's deck-echo follow-up and is
only used after an A-path success.

An idle run is an ordinary scenario career (``scenario_id`` 1-4) that the server
simulates on a wall-clock timer instead of turn by turn. When the timer expires
you call ``end``, and then finish it through the *scenario's* usual endpoints
(``<prefix>/factor_select``, ``<prefix>/finish``) - see :mod:`aic.career`.

``protocol/independent.json`` can override any endpoint path or payload field
without a code change, for when the game moves them.
"""

from __future__ import annotations

import json
import logging
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import bootstrap, succession
from .config import PROJECT_ROOT
from .logging_setup import get
from .state import ServerClock, read_progress_info, save_clock, walk

log = get("independent")

SPEC_PATH = PROJECT_ROOT / "protocol" / "independent.json"

API_PREFIX = "idle_single_mode"

DEFAULT_ENDPOINTS = {
    "pre_start": f"{API_PREFIX}/pre_start",
    "start": f"{API_PREFIX}/start",
    "check_progress_log": f"{API_PREFIX}/check_progress_log",
    "end": f"{API_PREFIX}/end",
}

#: TP an idle start costs. 30 normally, halved to 15 while a half-cost campaign
#: is running. The discounted price is not exposed anywhere in ``load/index``
#: (``common_define.single_mode_factor_use_tp_value`` is the factor-lottery
#: cost, not this), so it is probed: send the cheaper value first, and if the
#: server rejects the mismatch fall through to the next. Sending too *little*
#: is refused outright; sending too much risks simply being charged it, so the
#: order matters. The winner is cached per profile.
IDLE_START_TP_CANDIDATES = (15, 30)
IDLE_START_TP = IDLE_START_TP_CANDIDATES[0]

#: Result codes that mean "your use_tp does not match what I expect".
_TP_MISMATCH_CODES = (102, 205)

#: 217 - "Session Verification Error". The server invalidated the session, and
#: no further call on it can succeed, so a search must stop the moment it
#: appears rather than spending its remaining budget on certain failures.
_SESSION_INVALIDATED = 217

#: Hard ceiling on rejected start probes per career.
#:
#: Hundreds of rejected starts in a few seconds is not what a real client does,
#: and one such burst preceded a 217 (invalidated session). But the formula now
#: reproduces real values to within a small undercount that grows with the
#: parents' G1 wins, so a deeply-bred pair can sit 20-30 above the computed
#: value - out of reach of a tighter cap. 40 covers that while staying an order
#: of magnitude below the volume that drew a 217. A pair that still misses is
#: skipped and retried next lap.
MAX_START_PROBES = 40

#: Fallback training policy, used only when the account has never run an idle
#: career and ``pre_start`` therefore returns no ``last_idle_single_mode_start_info``.
#: 1 = turf; the rate-set id selects which stat spread the auto-trainer favours.
FALLBACK_POLICY = {
    "training_policy_ground_type": 1,
    "training_policy_param_rate_set_id": 2,
    "priority_skill_array": [],
}


class IdleModeError(RuntimeError):
    """The account cannot start an idle run right now."""


@contextmanager
def _quiet_client_errors():
    """Mute Icarus' per-call error logging for an expected-to-fail probe.

    Icarus logs the whole request payload at ERROR on every 205/102. That is the
    right default for its own runner, but here a rejection is how we search, so
    it buries the run in dumps. Restored on exit, including on exception.
    """
    loggers = [logging.getLogger(name) for name in ("client", "icarus.client")]
    previous = [lg.level for lg in loggers]
    for lg in loggers:
        lg.setLevel(logging.CRITICAL)
    try:
        yield
    finally:
        for lg, level in zip(loggers, previous):
            lg.setLevel(level)


# --------------------------------------------------------------------------- #
# spec overrides
# --------------------------------------------------------------------------- #

@dataclass
class Spec:
    """Optional overrides layered over the built-in protocol constants."""

    endpoints: dict[str, str] = field(default_factory=dict)
    payloads: dict[str, dict] = field(default_factory=dict)
    default_schedule: dict | None = None
    #: Pinned TP cost. ``0`` means "probe" (the normal case).
    use_tp: int = 0
    loaded_from: Path | None = None

    def endpoint(self, name: str) -> str:
        return self.endpoints.get(name) or DEFAULT_ENDPOINTS[name]

    def payload(self, name: str) -> dict:
        return dict(self.payloads.get(name) or {})

    def tp_candidates(self, override: int = 0) -> tuple[int, ...]:
        """TP values to try, cheapest first."""
        pinned = override or self.use_tp
        return (pinned,) if pinned else IDLE_START_TP_CANDIDATES

    @property
    def max_tp_cost(self) -> int:
        """Worst case, for the pre-flight "do we have enough TP" check."""
        return max(self.tp_candidates())


def load_spec(path: Path = SPEC_PATH) -> Spec:
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            raw = json.load(fh)
    except FileNotFoundError:
        return Spec()
    except (OSError, ValueError) as exc:
        log.warning("protocol spec at %s is unreadable (%s); using built-in defaults",
                    path, exc)
        return Spec()

    endpoints = {k: v for k, v in (raw.get("endpoints") or {}).items() if v}
    return Spec(
        endpoints=endpoints,
        payloads=dict(raw.get("payloads") or {}),
        default_schedule=raw.get("default_schedule") or None,
        use_tp=int(raw.get("use_tp") or 0),
        loaded_from=path,
    )


# --------------------------------------------------------------------------- #
# learned TP cost
# --------------------------------------------------------------------------- #

def _tp_cache_path(settings) -> Path:
    return settings.state_dir / "tp_cost.json"


def load_known_tp(settings) -> int:
    """The TP cost that last worked for this profile, or 0 if unknown.

    Discarded once it is a day old so the end of a half-cost campaign does not
    leave a stale value costing one rejected call per run forever.
    """
    try:
        with open(_tp_cache_path(settings), "r", encoding="utf-8") as fh:
            cached = json.load(fh)
        if time.time() - float(cached.get("learned_at") or 0) > 24 * 3600:
            return 0
        return int(cached.get("use_tp") or 0)
    except (OSError, ValueError, TypeError):
        return 0


def save_known_tp(settings, use_tp: int) -> None:
    try:
        with open(_tp_cache_path(settings), "w", encoding="utf-8") as fh:
            json.dump({"use_tp": int(use_tp), "learned_at": int(time.time())}, fh)
    except OSError as exc:
        log.debug("could not cache the TP cost: %s", exc)


def order_tp_candidates(spec: Spec, override: int = 0, known: int = 0) -> tuple[int, ...]:
    """Candidates to try, with a previously-learned value promoted to first."""
    candidates = spec.tp_candidates(override)
    if known and len(candidates) > 1 and known in candidates:
        return (known,) + tuple(c for c in candidates if c != known)
    return candidates


# --------------------------------------------------------------------------- #
# learned submission flow
# --------------------------------------------------------------------------- #

def _flow_cache_path(settings) -> Path:
    return settings.state_dir / "idle_flow.json"


def load_known_flow(settings) -> str:
    """Which submission flow last worked ("B", "A", or "" if unknown).

    Trying both flows costs a wasted call on every probe; once one is known to
    work for this profile, the other is only a fallback.
    """
    if settings is None:
        return ""
    try:
        with open(_flow_cache_path(settings), "r", encoding="utf-8") as fh:
            name = str(json.load(fh).get("flow") or "")
        return name if name in ("A", "B") else ""
    except (OSError, ValueError, TypeError, AttributeError):
        return ""


def save_known_flow(settings, name: str) -> None:
    if settings is None or name not in ("A", "B"):
        return
    if load_known_flow(settings) == name:
        return
    try:
        with open(_flow_cache_path(settings), "w", encoding="utf-8") as fh:
            json.dump({"flow": name, "seen_at": int(time.time())}, fh)
    except OSError as exc:
        log.debug("could not cache the submission flow: %s", exc)


# --------------------------------------------------------------------------- #
# known-good scenario
# --------------------------------------------------------------------------- #

def _scenario_cache_path(settings) -> Path:
    return settings.state_dir / "idle_scenario.json"


def load_known_scenario(settings) -> int:
    """A scenario this account has demonstrably run an idle career in.

    Not every unlocked scenario is necessarily startable in idle mode, and a
    refused start reports only 205 - so remembering one that worked beats
    guessing at the newest.
    """
    if settings is None:
        return 0
    try:
        with open(_scenario_cache_path(settings), "r", encoding="utf-8") as fh:
            return int(json.load(fh)["scenario_id"])
    except (OSError, ValueError, KeyError, TypeError):
        return 0


def save_known_scenario(settings, scenario_id: int) -> None:
    if settings is None or not scenario_id:
        return
    if load_known_scenario(settings) == int(scenario_id):
        return
    try:
        with open(_scenario_cache_path(settings), "w", encoding="utf-8") as fh:
            json.dump({"scenario_id": int(scenario_id),
                       "seen_at": int(time.time())}, fh)
    except OSError as exc:
        log.debug("could not cache the idle scenario: %s", exc)


# --------------------------------------------------------------------------- #
# training schedule
# --------------------------------------------------------------------------- #

@dataclass
class Schedule:
    """The ``start_info`` block: what the auto-trainer optimises for.

    * ``training_policy_ground_type``  - 1 turf, 2 dirt.
    * ``training_policy_param_rate_set_id`` - which stat-weighting preset to use.
    * ``priority_skill_array``  - ``{priority, skill_id}``, skills to buy first.
    * ``race_array`` - ``{year, program_id}``, the races to enter.
    """

    training_policy_ground_type: int = 1
    training_policy_param_rate_set_id: int = 2
    priority_skill_array: list[dict] = field(default_factory=list)
    race_array: list[dict] = field(default_factory=list)
    source: str = ""

    def to_payload(self) -> dict:
        return {
            "training_policy_ground_type": int(self.training_policy_ground_type),
            "training_policy_param_rate_set_id": int(self.training_policy_param_rate_set_id),
            "priority_skill_array": list(self.priority_skill_array),
            "race_array": list(self.race_array),
        }

    @classmethod
    def from_payload(cls, payload: dict, source: str = "") -> "Schedule":
        return cls(
            training_policy_ground_type=int(
                payload.get("training_policy_ground_type",
                            FALLBACK_POLICY["training_policy_ground_type"])),
            training_policy_param_rate_set_id=int(
                payload.get("training_policy_param_rate_set_id",
                            FALLBACK_POLICY["training_policy_param_rate_set_id"])),
            priority_skill_array=list(payload.get("priority_skill_array") or []),
            race_array=list(payload.get("race_array") or []),
            source=source,
        )


@dataclass
class PreStart:
    """Decoded ``idle_single_mode/pre_start`` response."""

    last_policy: dict = field(default_factory=dict)
    reserved_race_decks: list[dict] = field(default_factory=list)
    default_deck_num: int = 0
    needs_default_confirm: bool = False
    raw: dict = field(default_factory=dict)

    def race_deck(self, deck_num: int = 0) -> dict | None:
        """A saved race agenda: the requested one, the account default, or the
        one with the most races (a fuller agenda means fewer idle turns)."""
        decks = [d for d in self.reserved_race_decks if d.get("race_array")]
        if not decks:
            return None
        if deck_num:
            for deck in decks:
                if int(deck.get("deck_num") or 0) == deck_num:
                    return deck
            raise IdleModeError(
                f"race agenda {deck_num} does not exist or has no races "
                f"(available: {[d.get('deck_num') for d in decks]})"
            )
        if self.default_deck_num:
            for deck in decks:
                if int(deck.get("deck_num") or 0) == self.default_deck_num:
                    return deck
        return max(decks, key=lambda d: len(d.get("race_array") or []))


def pre_start(session, spec: Spec, scenario_id: int = 0) -> PreStart:
    """Open the idle-career start screen.

    Must be called before ``start``; it returns the account's last-used training
    policy plus its saved race agendas, which are what we reuse as the default
    schedule.

    ``scenario_id`` is **0** in every captured client request, even when the
    subsequent start names a real scenario - so 0 is the default here too rather
    than echoing the scenario we are about to start.
    """
    body = spec.payload("pre_start")
    body.setdefault("scenario_id", int(scenario_id))
    res = session.client.call(spec.endpoint("pre_start"), body)
    session.clock.observe(res)
    data = (res or {}).get("data") or {}

    reserved = data.get("reserved_race_info") or {}
    return PreStart(
        last_policy=data.get("last_idle_single_mode_start_info") or {},
        reserved_race_decks=list(reserved.get("reserved_race_array") or []),
        default_deck_num=int(reserved.get("default_deck_num") or 0),
        needs_default_confirm=bool(reserved.get("needs_default_confirm")),
        raw=data,
    )


def build_schedule(pre: PreStart, override: dict | None = None,
                   race_deck_num: int = 0, no_races: bool = False,
                   rules=None, chara_id: int = 0,
                   fill_years: bool = False, race_goal: int = 0,
                   extra_races: list | None = None,
                   goals_only: bool = False) -> Schedule:
    """Assemble the ``start_info`` to send.

    Default is "whatever this account used last": the policy and priority skills
    come from ``last_idle_single_mode_start_info`` and the races from a saved
    agenda. An explicit ``override`` (``--schedule file.json``) wins field by
    field, so you can replace only the races or only the policy.

    ``goals_only`` races the trainee's route goals and nothing else - not the
    saved agenda either (an Ace Run, aic.ace, trains every other turn).
    """
    if pre.last_policy:
        schedule = Schedule.from_payload(pre.last_policy, source="account's last-used policy")
    else:
        schedule = Schedule.from_payload(FALLBACK_POLICY, source="built-in fallback")
        log.info("Account has no previous idle run; using the fallback training policy.")

    if no_races:
        schedule.race_array = []
        schedule.source += " + no races (--no-races)"
        log.info("Sending an empty race list.")
        return schedule

    deck = pre.race_deck(race_deck_num)
    agenda_races = list(deck.get("race_array") or []) if deck else []
    if deck:
        schedule.source += f" + agenda {deck.get('deck_num')} ({deck.get('deck_name')!r})"
    elif not rules:
        log.warning("No saved race agenda found; starting with an empty race list.")

    # Saved agendas are generic - they are NOT tied to a trainee, but each
    # trainee's route has its own mandatory goal races. Reusing an agenda built
    # for another character therefore omits this one's goals. When master.mdb is
    # available we build the list from the trainee's own route instead and use
    # the agenda only for legal extras.
    if rules is not None and chara_id:
        targets = rules.route_targets(chara_id)
        # Hand-picked races sit on top of the goal races, never replacing them:
        # goals are mandatory, so build_race_array keeps them and adds whatever
        # of these (and of the saved agenda) is legal for this trainee.
        picked = [dict(r) for r in (extra_races or [])]
        if picked:
            for entry in picked:
                if not entry.get("year"):
                    entry["year"] = rules.place(entry["program_id"])
                    if not entry["year"]:
                        log.warning("Race %s runs in more than one year; say "
                                    "which with 'year:program_id'.",
                                    entry["program_id"])
            legal_picked, bad_picked = rules.filter_races(chara_id, picked)
            if bad_picked:
                log.warning("Ignoring %s selected race(s) not legal for this "
                            "trainee: %s", len(bad_picked),
                            ", ".join(f"y{r['year']}/p{r['program_id']}"
                                      for r in bad_picked))
            picked = legal_picked
        # An explicit --races list REPLACES the account's saved agenda; it does
        # not merge with it.
        #
        # This used to build from `picked + agenda_races`, so every career also
        # ran whatever races happened to be saved in that account's in-game
        # agenda. MEASURED 2026-09-05 across 14 accounts on one identical
        # 43-pick spec: schedules came out anywhere from 44 to 52 races, because
        # each account's saved agenda differs. "Use the same schedule on every
        # account" is impossible while a per-account default is folded in.
        #
        # With no picks the agenda is still the sensible default - that is the
        # whole point of a saved agenda - so it is only displaced when the
        # operator has actually said which races they want.
        agenda_used = [] if (picked or goals_only) else agenda_races
        built = rules.build_race_array(chara_id, picked + agenda_used)
        _, rejected = rules.filter_races(chara_id, agenda_used)

        # Goals and hand-picked races are the ones the fill must never trim;
        # the saved agenda's extras are optional and give way to reach the
        # target with the highest-fan races.
        protect = ({(t["year"], t["program_id"]) for t in targets}
                   | {(r["year"], r["program_id"]) for r in picked})
        if race_goal:
            filled = rules.fill_race_array(chara_id, built, target=race_goal,
                                           protect=protect)
        elif fill_years:
            filled = rules.fill_race_array(chara_id, built, protect=protect)
        else:
            filled = built
        added = len(filled) - len(built)

        schedule.race_array = filled
        parts = [f"route for chara {chara_id} ({len(targets)} goal races)"]
        if picked:
            parts.append(f"{len(picked)} selected")
        if agenda_used:
            # Counted, not inferred. The old expression was
            # len(built) - len(targets) - len(picked), which went NEGATIVE
            # (-5, -4, -2 seen live) whenever build_race_array deduped an
            # overlap between the picks and the trainee's goal races.
            chosen = {(r["year"], r["program_id"])
                      for r in picked} | {(t["year"], t["program_id"])
                                          for t in targets}
            extras = sum(1 for r in built
                         if (r["year"], r["program_id"]) not in chosen)
            if extras:
                parts.append(f"{extras} legal agenda extras")
        elif agenda_races:
            parts.append(f"saved agenda ignored ({len(agenda_races)} races) - "
                         + ("goal races only" if goals_only and not picked else "--races given"))
        if added:
            parts.append(f"{added} added for fans (goal {race_goal or ''}".rstrip()
                         + ")")
        schedule.source = " + ".join(parts)

        if added:
            total_fans = sum(rules.fan_reward(e["program_id"]) for e in filled)
            log.info("Race plan pays about %s fans if every race is won.",
                     f"{total_fans:,}")

        by_year: dict[int, int] = {}
        for entry in filled:
            by_year[entry["year"]] = by_year.get(entry["year"], 0) + 1
        log.info("Races per year: %s",
                 ", ".join(f"y{y}={by_year.get(y, 0)}" for y in (1, 2, 3)))
        if rejected:
            log.info("Dropped %s agenda race(s) not legal for this trainee: %s",
                     len(rejected),
                     ", ".join(f"y{r['year']}/p{r['program_id']}" for r in rejected[:10]))
        return schedule

    schedule.race_array = agenda_races

    if override:
        for key in ("training_policy_ground_type", "training_policy_param_rate_set_id"):
            if key in override:
                setattr(schedule, key, int(override[key]))
        if "priority_skill_array" in override:
            schedule.priority_skill_array = list(override["priority_skill_array"] or [])
        if "race_array" in override:
            schedule.race_array = list(override["race_array"] or [])
        schedule.source = "override file"

    return schedule


# --------------------------------------------------------------------------- #
# start / progress / end
# --------------------------------------------------------------------------- #

def start(session, spec: Spec, selection, account, schedule: Schedule,
          scenario_id: int, use_tp: int = 0, settings=None,
          succession_rank_point: int = 0, succession_hint: int = 0,
          sweep: bool = False):
    """Submit the career start, trying both captured client flows.

    The full payload goes to ``idle_single_mode/start`` first (flow B - the
    sequence this tool ran during all its historical successes), and to
    ``idle_single_mode/pre_start`` (flow A) as a fallback when B's probes are
    refused. See the module docstring for the capture evidence behind both.

    The selection block is shaped exactly like a normal ``<prefix>/start``, but
    wrapped in ``single_mode_start_request_common`` and accompanied by
    ``start_info`` (the schedule). ``running_style`` is idle-only - the
    auto-trainer needs a fixed style since nobody is there to pick one per race.

    ``use_tp`` is probed unless pinned: the half-cost campaign price is not
    published in any payload, so we try the cheap value and fall through on a
    mismatch. See :data:`IDLE_START_TP_CANDIDATES`.

    After a flow-A success, also sends the deck-echo follow-up the real client
    sends there (:func:`confirm_deck`). Its failure is logged, not raised - the
    career already exists by then.
    """
    body = spec.payload("start")
    common = body.setdefault("single_mode_start_request_common", {})
    common["start_chara"] = {
        "card_id": selection.card_id,
        "support_card_ids": list(selection.support_card_ids),
        "friend_support_card_info": {
            "viewer_id": selection.friend_viewer_id,
            "support_card_id": selection.friend_card_id,
        },
        "succession_trained_chara_id_1": selection.parent_id_1,
        "succession_trained_chara_id_2": selection.parent_id_2,
        "rental_succession_trained_chara": {
            "viewer_id": 0,
            "trained_chara_id": 0,
            "is_circle_member": False,
            "is_event_rental": False,
        },
        "scenario_id": int(scenario_id),
        "selected_difficulty_info": {"difficulty_id": 0, "difficulty": 0, "is_boost": 0},
        "select_deck_id": selection.deck_id,
        "boost_story_event_id": 0,
        "boost_factor_research_event_id": 0,
        "is_play_training_challenge": False,
        "training_challenge_mode": 0,
        "running_style": int(selection.running_style or 1),
    }
    common["tp_info"] = dict(account.tp_info or {})
    common["current_money"] = int(account.gold)
    common["current_succession_rank_point"] = int(succession_rank_point)

    body["start_info"] = schedule.to_payload()

    known = load_known_tp(settings) if settings else 0
    all_tp = order_tp_candidates(spec, use_tp, known)
    tp_options = [c for c in all_tp if c <= account.current_tp]
    # A candidate we cannot afford is a candidate we cannot rule out. If the
    # affordable ones are refused, TP is the prime suspect - not the succession
    # rank point - so remember that rather than blaming the wrong field.
    unaffordable = [c for c in all_tp if c > account.current_tp]
    if not tp_options:
        raise IdleModeError(
            f"not enough TP to start: {account.current_tp} available, need one of "
            f"{list(all_tp)}. Recover TP first (aic run --recover-tp)."
        )

    # Flow B: full payload on `start` (a light pre_start probe already happened
    # when the options were fetched, matching the 07-30 captures). Flow A: the
    # same payload on `pre_start`, matching the 07-24/08-02 captures. Whichever
    # worked last for this profile goes first, so the steady state costs one
    # call rather than two.
    flow_b = spec.endpoint("start")
    flow_a = spec.endpoint("pre_start")
    known_flow = load_known_flow(settings)
    flows = (("A", flow_a), ("B", flow_b)) if known_flow == "A" else \
            (("B", flow_b), ("A", flow_a))
    won_flow = {"name": flows[0][0]}

    def attempt(cost: int, points: int, endpoint: str = ""):
        """Returns the response, or None when the server rejects the values.

        ``retry_205=0`` is essential. Icarus' client retries 205 three times by
        default, and its no-retry exclusion list is keyed on the *scenario*
        prefix, so these endpoints are not covered - every probe would
        otherwise cost four server calls and four screens of error dump.

        A rejected probe is an expected outcome here, not an error, so Icarus'
        full-payload dump is muted for the duration; the final failure carries
        the diagnosis instead.
        """
        common["use_tp"] = int(cost)
        common["current_succession_rank_point"] = int(points)
        with _quiet_client_errors():
            try:
                return session.client.call(endpoint or flow_b, body, retry_205=0)
            except Exception as exc:  # noqa: BLE001
                code = bootstrap.result_code(exc)
                if code == _SESSION_INVALIDATED:
                    raise IdleModeError(
                        "the career-start call returned 217 (Session "
                        "Verification Error): the server invalidated this "
                        "session, so nothing further on it can succeed.\n"
                        "This usually means the account was opened somewhere "
                        "else - the real client, another instance - or the "
                        "server ended the session on its own.\n"
                        "Rotation moves on to the next account; this one is "
                        "picked up on the following lap."
                    ) from exc
                if code not in _TP_MISMATCH_CODES:
                    raise
                return None

    def attempt_both(cost: int, points: int):
        """One value, both flows: B first, then A. Remembers which flow won."""
        for name, endpoint in flows:
            res = attempt(cost, points, endpoint)
            if res is not None:
                won_flow["name"] = name
                return res
        return None

    # Open with the computed value, unmodified. It is exact - 22/22 against
    # captured server-accepted starts - so an offset can only move it away from
    # the right answer. A learned offset is consulted only as a later search
    # candidate (and only when sweeping is explicitly on): every offset on disk
    # was minted by a formula generation that no longer exists, and applying
    # those to a correct value lands 65-86 points off.
    signature = getattr(selection, "succession_signature", "")
    cached = None
    if settings and sweep:
        offsets = succession.load_offsets(settings)
        # By tree contents first - that entry transfers between accounts that
        # share a lineage, which every farmed account does.
        if signature:
            cached = offsets.get(signature)
        if cached is None:
            cached = offsets.get(succession.offset_key(
                selection.parent_id_1, selection.parent_id_2,
                viewer_id=account.viewer_id, trainee_card_id=selection.card_id))

    opening = succession_rank_point
    log.info("Succession rank point %s (computed exactly).", opening)

    # Phase 1 - probe the TP cost against the best value we have. At most two
    # calls when both the flow and the offset are already known.
    res = None
    chosen_tp = tp_options[0]
    for cost in tp_options:
        res = attempt_both(cost, opening)
        if res is not None:
            chosen_tp = cost
            break
        log.info("Start rejected at use_tp=%s.", cost)

    # Every affordable TP value was refused while a dearer one was never tried.
    # Sweeping succession rank points here would burn hundreds of calls on the
    # wrong field - the half-cost campaign ending is enough to cause this.
    if res is None and unaffordable:
        raise IdleModeError(
            f"start refused at every TP cost we can afford "
            f"({', '.join(map(str, tp_options))}), and {account.current_tp} TP is "
            f"not enough to try {', '.join(map(str, unaffordable))}.\n"
            f"An idle start costs 30 TP normally and 15 only while a half-cost "
            f"campaign is running - so this is what it looks like when that "
            f"campaign has ended.\n"
            f"  Recover TP and retry:  aic run --recover-tp\n"
            f"  Or wait for TP to regenerate to {max(unaffordable)}."
        )

    # Phase 2 - TP is not the problem, so the succession rank point is. Search
    # for it at the most likely TP cost.
    if res is None:
        # The rank point is computed exactly, so a refusal here is nearly always
        # a DIFFERENT field - an invalid race_array (two races on one turn) or a
        # deck the server rejects. Proven by A/B: two accounts minutes apart with
        # identical trees and identical rank points, one refused at 380 swept
        # values and the other accepted at the first call, differing only in the
        # deck and borrowed card.
        log.warning("Start rejected with succession rank point %s%s.", opening,
                    f"; searching (up to +{succession.SWEEP_UP}/"
                    f"-{succession.SWEEP_DOWN}) because --sweep was given"
                    if sweep else
                    " - which is computed exactly, so the refusal is almost "
                    "certainly a different field (race list or deck). Not "
                    "searching")
        if sweep:
            log.info("At most %s candidates, likeliest first; whatever works is "
                     "cached for this trainee and parent pair, so a repeat start "
                     "is a single call.", MAX_START_PROBES)

        # The breakdown is what turns the next failure into evidence rather than
        # another guess: a missing tree node or a large trainee contribution is
        # visible here.
        for line in getattr(selection, "succession_detail", None) or []:
            log.debug("%s", line)

        # The first candidates are targeted (parent-only, the residual anchors
        # from the known-good samples) - worth trying on both flows. The wide
        # sweep after them runs on the preferred flow only.
        BOTH_FLOW_CANDIDATES = 8

        attempts = 0
        for points, why in succession.candidates(
                succession_rank_point, succession_hint, cached, sweep,
                parent_only=getattr(selection, "succession_parent_only", 0),
                already_tried=(opening,)):
            if attempts >= MAX_START_PROBES:
                log.warning("Stopping after %s probes rather than grinding on. "
                            "The formula undercounts G1 wins slightly; if the "
                            "value has not appeared within %s of the computed "
                            "one it is skipped, and this account is retried next "
                            "lap.", attempts, MAX_START_PROBES)
                break
            attempts += 1
            if attempts % 10 == 0:
                log.info("  ...%s values tried (at %+d)",
                         attempts, points - succession_rank_point)
            if attempts <= BOTH_FLOW_CANDIDATES:
                res = attempt_both(chosen_tp, points)
            else:
                res = attempt(chosen_tp, points, flow_b)
            if res is None:
                continue
            offset = points - succession_rank_point
            log.info("Succession rank point corrected to %s (%+d, via %s) "
                     "after %s attempts.", points, offset, why, attempts)
            if settings:
                succession.save_offset(settings, selection.parent_id_1,
                                       selection.parent_id_2, offset,
                                       viewer_id=account.viewer_id,
                                       trainee_card_id=selection.card_id,
                                       signature=signature)
            break

    if res is None:
        # Every candidate was refused on both flows, so the rank point is
        # probably not what the server objects to. Dump the exact payload for
        # offline diffing against accepted captures, then name the other
        # suspects rather than leaving a bare result code.
        if settings:
            _dump_refused_payload(settings, body)
        tried = (f"computed {succession_rank_point}, account balance "
                 f"{succession_hint}")
        if sweep:
            tried += f", plus {MAX_START_PROBES} ranked candidates"
        raise IdleModeError(
            f"the career start was refused (205) ({tried}).\n"
            f"205 means 'data mismatch' and is NOT specific to the rank point - "
            f"that value is computed exactly (verified against 22 captured "
            f"server-accepted starts), so look elsewhere first. Most likely, in "
            f"order:\n"
            f"  1. the race list - every accepted plan on record has at least "
            f"three year-2 races and no two races on one turn. This trainee's "
            f"plan has {len(schedule.race_array)} race(s); pad it with "
            f"--race-goal 30 (or --fill-races)\n"
            f"  2. scenario {scenario_id} is not startable in idle mode - try "
            f"--scenario 3 (or 1/2)\n"
            f"  3. a career is still active - check `aic status`\n"
            f"  4. the borrowed support card is stale or invalid "
            f"(friend_card_id={selection.friend_card_id})\n"
            f"  5. the deck or parents contain a duplicate character\n"
            f"The exact payload was dumped for diffing against accepted "
            f"captures.\n"
            + ("" if sweep else
               "Searching the rank point is off by default; pass --sweep to "
               "probe around it anyway (rarely the real problem).")
        )

    session.clock.observe(res)
    data = (res or {}).get("data") or {}
    if len(tp_options) > 1:
        log.info("Career start cost %s TP.", chosen_tp)
    if settings:
        save_known_tp(settings, chosen_tp)
        # Remember the flow so the next start does not pay for a probe of the
        # other one, and re-save the offset even when the opening value was
        # accepted outright (it may have come from the account-level fallback,
        # which is worth promoting to an exact entry for this trainee).
        save_known_flow(settings, won_flow["name"])
        if cached:
            succession.save_offset(settings, selection.parent_id_1,
                                   selection.parent_id_2, cached,
                                   viewer_id=account.viewer_id,
                                   trainee_card_id=selection.card_id,
                                   signature=signature)

    progress = data.get("progress_info") or {}
    # The one unambiguous calibration point: this start_time was stamped by the
    # server seconds ago, so the gap to servertime is the offset plus round-trip.
    if progress.get("start_time"):
        if session.clock.calibrate_from_fresh(progress["start_time"]) and settings:
            save_clock(settings, session.clock)
    state = read_progress_info(progress, session.clock) if progress else None
    if state and state.finish_at:
        from .state import format_duration
        log.info("Independent training started - finishes in %s.",
                 format_duration(state.seconds_remaining))
    else:
        log.warning("start succeeded but returned no progress_info; "
                    "re-read state with `aic status`.")

    # Flow A's captures show a deck-echo `start` following the pre_start
    # submission; flow B's captures show nothing after `start` (there is no
    # second start to send), so the echo only belongs on an A-path success.
    if won_flow["name"] == "A":
        confirm_deck(session, spec, selection)
    return data, state


def _dump_refused_payload(settings, body: dict) -> None:
    """Write the exact refused submission to disk for offline diffing.

    Guessing which field a bare 205 objects to has burned days; a saved payload
    can be compared field-by-field against the accepted captures instead.
    """
    try:
        path = settings.state_dir / "last_refused_start.json"
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(body, fh, indent=1)
        log.info("The refused payload was saved to %s for diffing against "
                 "accepted captures.", path)
    except OSError as exc:
        log.debug("could not save the refused payload: %s", exc)


def confirm_deck(session, spec: Spec, selection) -> bool:
    """POST the flow-A deck-echo follow-up on ``idle_single_mode/start``.

    Not what creates the career - that already happened via the flow-A
    ``pre_start`` submission. Real captures show this call carries only
    ``support_card_deck_array``: 10 slots, deck 1 holding the five cards just
    used with ``auto_deck_flag`` 1, the rest zeroed. Its response carries no
    career state, only an echo of that same array.

    Failure here is logged, not raised: the career already exists by the time
    this runs, so there is nothing left to roll back.
    """
    # Persist the used cards into the selection's deck slot (the auto fan-bonus
    # path targets slot 10; the normal path echoes back the deck it used). Real
    # captures put the cards in slot 1 with auto_deck_flag 1 and zero the rest.
    target = getattr(selection, "deck_id", 1) or 1
    target = min(max(int(target), 1), 10)

    # The other nine slots carry their REAL current contents, never zeros.
    #
    # This used to send [0,0,0,0,0] for every non-target slot, inferred from one
    # capture taken on an account that genuinely only had deck 1 populated. On
    # every captured account with more than one deck the real client transmits
    # the true contents of all ten and zeroes only slots that are actually
    # empty. The identical array IS a write payload on
    # support_card_deck/change_party (its response echoes back exactly the decks
    # whose contents differed), so if this endpoint shares that handler the old
    # body would have wiped nine saved decks per career. It never fired only
    # because this farm runs flow B and confirm_deck is flow A only - a dormant
    # landmine, not a safe design.
    current = {}
    for deck in (getattr(session, "load_index", None) or {}).get(
            "support_card_deck_array") or []:
        try:
            current[int(deck.get("deck_id"))] = [
                int(c or 0) for c in (deck.get("support_card_id_array") or [])]
        except (TypeError, ValueError):
            continue

    slots = []
    for n in range(1, 11):
        if n == target:
            ids = list(selection.support_card_ids)
        else:
            ids = current.get(n) or []
        slots.append({
            "deck_id": n,
            "support_card_id_array": (ids + [0, 0, 0, 0, 0])[:5],
            "auto_deck_flag": 0,
        })

    body = spec.payload("start")
    body["support_card_deck_array"] = slots
    try:
        res = session.client.call(spec.endpoint("start"), body)
        session.clock.observe(res)
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("idle_single_mode/start (deck confirmation) failed (%s); "
                    "the career itself already started, so continuing.", exc)
        return False


def check_progress_log(session, spec: Spec) -> dict:
    """``idle_single_mode/check_progress_log`` - the finished run's state.

    Only valid *after* ``end``: while the timer is still running there is no
    progress log yet and the server answers 205. Returns the ``data`` block
    (``chara_info``, ``home_info``, and any scenario-specific set).

    Prefer ``end_info.chara_info`` from the :func:`end` response, which carries
    the same state without an extra call; this is the fallback.
    """
    res = session.client.call(spec.endpoint("check_progress_log"),
                              spec.payload("check_progress_log"))
    session.clock.observe(res)
    return (res or {}).get("data") or {}


def end(session, spec: Spec) -> dict:
    """``idle_single_mode/end`` - stop the idle timer and collect the log.

    This does NOT finish the career. It hands the (now fully simulated) career
    back so it can be finished through the scenario's normal endpoints.

    The response's ``end_info.chara_info`` is the authoritative final state -
    critically its ``turn`` (78 for a completed run), which ``factor_select``
    and ``finish`` must be given. The home screen's idle summary still says
    ``turn: 1``, so reading it from there is wrong.
    """
    res = session.client.call(spec.endpoint("end"), spec.payload("end"))
    session.clock.observe(res)
    data = (res or {}).get("data") or {}
    log_info = data.get("progress_log_info") or {}
    if log_info:
        gained = log_info.get("event_gain_info") or {}
        log.info("Idle run ended: %s skill points earned, %s races run.",
                 gained.get("skill_point", "?"),
                 len(log_info.get("race_history_array") or []))
    return data


def final_chara_info(session, spec: Spec, end_data: dict) -> dict:
    """The finished career's ``chara_info``, from whichever source has it.

    ``end``'s own response first; then ``check_progress_log``; the caller falls
    back to ``<prefix>/load`` if both come up empty (which is the situation
    after a partially-completed run is resumed).
    """
    chara = ((end_data or {}).get("end_info") or {}).get("chara_info") or {}
    if chara:
        return chara

    try:
        progress = check_progress_log(session, spec)
    except Exception as exc:  # noqa: BLE001
        log.debug("check_progress_log fallback failed: %s", exc)
        return {}
    return progress.get("chara_info") or {}


# --------------------------------------------------------------------------- #
# discovery
# --------------------------------------------------------------------------- #

def discover(session, spec: Spec) -> dict:
    """Dump the idle-career surface for inspection when the protocol shifts."""
    from .state import candidate_timestamps

    report: dict[str, Any] = {"generated_at": int(time.time())}
    home = session.refresh_home()

    report["idle_single_mode_load_info"] = home.get("idle_single_mode_load_info")
    report["idle_open_date"] = (home.get("common_define") or {}).get(
        "idle_single_mode_open_date")
    report["future_timestamps_in_home"] = [
        {"path": p, "value": v} for p, v in candidate_timestamps(home, session.clock)
    ]
    report["server_clock_offset"] = session.clock.offset
    report["server_clock_derived"] = session.clock.derived

    hits = []
    for path, key, value in walk(home):
        if "idle" in f"{path}.{key}".lower():
            hits.append({"path": path, "value": str(value)[:120]})
    report["home_keys_matching_idle"] = hits[:200]

    probes = {}
    for name, endpoint in DEFAULT_ENDPOINTS.items():
        if name in ("start", "end"):
            continue  # mutating; never probed
        payload = {"scenario_id": 0} if name == "pre_start" else {}
        try:
            res = session.client.call(spec.endpoint(name), payload)
        except Exception as exc:  # noqa: BLE001
            probes[endpoint] = {"ok": False, "result_code": bootstrap.result_code(exc),
                                "error": str(exc)[:200]}
            continue
        data = (res or {}).get("data") or {}
        probes[endpoint] = {"ok": True, "keys": sorted(data)[:60]}
    report["endpoint_probes"] = probes

    return report
