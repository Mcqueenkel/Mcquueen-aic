"""Create accounts in bulk and stock them with a fixed number of pulls.

A trimmed reroll: it does not judge what it pulls. The goal here is a *roster* -
N accounts that exist, have a support deck worth training with, and are
recoverable by Link Password - so every account is kept and written straight to
``reroll_accounts.json`` for :mod:`aic.rotate`.

Each cycle:

    disconnect the slot -> signup -> name/sex -> tutorial skip
      -> claim the present box (carats) -> N ten-pulls on the banner
      -> publish a Data Link -> append to the roster

Two things here are load-bearing and were learned the hard way in
uma-reroll-farm; both are preserved:

* **Call order.** ``load/index`` goes *before* claiming presents, and after
  ``present/receive_all`` the next call must be ``present/index`` - a
  ``load/index`` there breaks the gacha with 1808. The carat balance therefore
  comes from ``receive_all``'s ``add_fcoin``, not from ``coin_info``.
* **Pacing.** Requests are paced to look human. At machine speed the edge
  (Akamai) starts answering 403. The gacha sequence itself is *not* paced -
  ``gacha/index`` and ``gacha/exec`` have to be back to back in the same session.
"""

from __future__ import annotations

import json
import random
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import account as acct
from . import steam
from .auth import load_auth_config, refresh_steam_ticket
from .config import Settings
from .logging_setup import get
from .pacing import no_delay
from .pacing import sleep as paced_sleep
from .rotate import DEFAULT_ROSTER

log = get("farm")

TEN_PULL_COST = 1500

#: Gaps that make the traffic look human, in seconds, before ``--pace`` scaling.
#: They sit **between** pulls and **between** cycles - never inside a request
#: sequence. uma-reroll-farm is explicit about why: the gacha needs
#: ``gacha/index`` -> ``gacha/exec`` back to back in the session that created the
#: account, and delays over ~10s inside that window return 1808.
PULL_GAP = (1.2, 2.6)
CYCLE_GAP = (6.0, 14.0)

#: Occasional longer pause between cycles, so the rhythm is not perfectly even.
CYCLE_BREAK_CHANCE = 0.15
CYCLE_BREAK = (20.0, 60.0)

#: Result codes meaning the slot still holds a half-deleted account.
STALE_ACCOUNT_CODES = ("207", "217", "1102", "709")


#: Hard ceiling on accounts per run when a career is started each cycle (the
#: "stress test"). This drives real signups + gacha + a career start against the
#: live server; an unbounded loop there is account-creation abuse, so it is
#: capped rather than offered as "forever".
STRESS_MAX_COUNT = 25


class FarmError(RuntimeError):
    pass


@dataclass
class FarmedAccount:
    viewer_id: int
    trainer_id: str
    password: str
    name: str = ""
    cards: list[int] = field(default_factory=list)
    link_set: bool = False


def _gap(bounds: tuple[float, float], scale: float) -> None:
    if no_delay():
        return
    low, high = bounds
    time.sleep(random.uniform(low, high) * max(0.0, scale))


def _new_client(settings: Settings, cfg: dict):
    """A client with a brand-new udid, ready to sign up.

    Deliberately **not** rate-limited per request. The signup -> gacha window is
    order- and timing-sensitive: pausing inside it returns 1808. The human-like
    gaps go between pulls and between cycles instead.
    """
    import uuid

    from . import bootstrap

    ick = bootstrap.bind(settings)
    fresh = dict(cfg)
    for key in acct.ACCOUNT_IDENTITY_KEYS:
        fresh.pop(key, None)
    fresh["udid"] = str(uuid.uuid4())
    # device_id is a per-device fingerprint, not account identity: dropping it
    # lets Windows regenerate the same value from the registry, but on POSIX
    # there is no registry to read, so keep the captured one (the client falls
    # back to get_hwid() only when it is absent, and that fallback is blocked).
    if cfg.get("device_id"):
        fresh["device_id"] = cfg["device_id"]
    return ick.UmaClient(fresh)


def _signup_and_login(client) -> None:
    """Create an account and open a session on it.

    Deliberately NOT ``UmaClient.login()``. Icarus' version also calls
    ``tool/start_session/load`` and ``read_info/index``; on a brand-new account
    those answer 1102/102 and poison the session - afterwards every endpoint
    that mutates the account (``user/change_name``, ``account/*``) returns 217
    with an empty sid. uma-reroll-farm's client carries the same warning.

    The working sequence is just: pre_signup -> signup -> start_session ->
    load/index.
    """
    import base64

    client.regen_sid()
    client.call("tool/pre_signup", dict(acct.PRE_SIGNUP_BODY))
    paced_sleep(acct.SIGNUP_GAP)

    client.regen_sid()
    signed = (client.call("tool/signup", dict(acct.SIGNUP_BODY)).get("data") or {})
    if not signed.get("viewer_id") or not signed.get("auth_key"):
        raise FarmError("tool/signup returned no viewer_id/auth_key")
    client.viewer_id = signed["viewer_id"]
    client.auth_key_hex = base64.b64decode(signed["auth_key"]).hex()

    client.regen_sid()
    client.call("tool/start_session", {"attestation_type": 0, "device_token": None})
    res = client.call("load/index", {"adid": ""})
    client.refresh_cached_account_state(res.get("data") or {})


def _setup_account(client) -> int:
    """signup -> trainer -> tutorial -> presents, in one session. Returns carats."""
    _signup_and_login(client)
    if not client.viewer_id:
        raise FarmError("signup returned no viewer_id")

    client.call("user/change_name",
                {"name": "Trainer" + format(random.randint(0, 0xFFFFFF), "06x")})
    client.call("user/change_sex", {"sex": 1})
    client.call("tutorial/skip", {})
    client.call("load/index", {"adid": ""})      # before presents, never after

    pdata = (client.call("present/index", {
        "time_filter_type": 0, "category_filter_type": [0],
        "offset": 0, "limit": 100, "is_asc": True,
    }).get("data") or {})
    # no_receive_present_num is a dict {no_time_limit_present_num, time_limit_present_num}
    counts = pdata.get("no_receive_present_num")
    count = (len(pdata.get("present_array") or [])
             or (sum(int(v or 0) for v in counts.values()) if isinstance(counts, dict)
                 else int(counts or 0)))

    received = client.call("present/receive_all", {
        "time_filter_type": 0, "category_filter_type": [0],
        "is_asc": True, "current_rest_present_num": count,
    })
    add_fcoin = int(((received.get("data") or {})
                     .get("reward_summary_info") or {}).get("add_fcoin") or 0)

    # present/index here, NOT load/index - the latter breaks the gacha (1808).
    client.call("present/index", {
        "time_filter_type": 0, "category_filter_type": [0],
        "offset": 0, "limit": 100, "is_asc": True,
    })
    return int(client.coin_info.get("fcoin", 0) or 0) + add_fcoin


def _pull(client, banner: int, carats: int, max_pulls: int,
          free_only: bool = False, pace_scale: float = 1.0) -> list[int]:
    """Up to ``max_pulls`` ten-pulls on ``banner``.

    A ceiling, not a quota: it stops early when the carats run out (or, with
    ``free_only``, when the giveaway pulls are used up). Free pulls are whatever
    the banner currently offers - campaign or stock - and cost nothing. The
    balance is tracked by hand because ``load/index``, the only source of
    ``coin_info``, breaks the gacha here.
    """
    cards: list[int] = []
    for i in range(max_pulls):
        info_list = (client.call("gacha/index", {}).get("data") or {}).get(
            "gacha_info_list") or []
        info = next((g for g in info_list if g.get("id") == banner), None)
        if info is None:
            available = sorted(g.get("id") for g in info_list if g.get("id"))
            raise FarmError(
                f"banner {banner} is not on offer. Available: {available}")

        stock = int(info.get("remain_stock_num_multi") or 0)
        campaign = int(info.get("is_campaign_draw_enable_multi") or 0)
        free = stock > 0 or campaign == 1

        if not free:
            if free_only:
                log.info("  no free pulls left after %s (--free-only).", i)
                break
            if carats < TEN_PULL_COST:
                log.info("  out of carats after %s pull(s).", i)
                break

        result = client.call("gacha/exec", {
            "gacha_id": banner, "draw_type": 1, "draw_num": 10,
            "current_num": 0 if free else carats,
            "current_stock_num": stock, "item_id": 0,
        })
        drawn = [it["card_id"] for it in
                 ((result.get("data") or {}).get("gacha_result_list") or [])
                 if it.get("card_id")]
        cards += drawn
        if not free:
            carats -= TEN_PULL_COST
        log.info("  pull %s/%s: %s cards (%s)", i + 1, max_pulls, len(drawn),
                 "free" if free else f"{carats} carats left")
        _gap(PULL_GAP, pace_scale)
    return cards


def _start_career(settings: Settings, client, cfg: dict, opts: dict) -> str:
    """Start one Independent career on the freshly-created account.

    Reuses the normal ``flow.run_cycle`` by wrapping the farm client in a
    ``Session``: with no career yet running it just builds a selection (trainee,
    deck, starter parents) and starts. A brand-new account has the starter
    trained characters this needs. Best-effort - the caller keeps the account
    even if the start is refused - and returns a short status string.
    """
    from . import flow
    from .auth import Session
    from .independent import load_spec

    session = Session(client=client, cfg=dict(cfg), settings=settings)
    result = flow.run_cycle(
        settings, spec=load_spec(), session=session,
        trainee_card_id=int(opts.get("trainee") or 0),
        running_style=int(opts.get("running_style") or 1),
        auto_deck=bool(opts.get("auto_deck")),
        build_deck_slot=int(opts.get("build_deck_slot") or 10),
        scenario_id=int(opts.get("scenario") or 0),
        race_goal=int(opts.get("race_goal") or 0),
        # Always pad the plan on a fresh account. A trainee's route alone can
        # leave a year with no races at all (chara 1052's classic-year goals are
        # fan-count objectives, not races, so its route yields y1=1/y2=0/y3=4),
        # and every start on record with an empty year-2 was refused while every
        # accepted plan had at least three. fill_race_array also guarantees one
        # race per turn, which is a separately proven 205 cause.
        fill_races=not opts.get("race_goal"),
        no_races=bool(opts.get("no_races")),
        succession_point=int(opts.get("succession_point") or 0),
        # A stress test values throughput over a single career succeeding: the
        # succession-rank-point search is ~40 live calls, and its cache is
        # per-parent-pair, so a fresh account (new starter parents each time)
        # would pay it in full every cycle. Off by default; opt in with sweep.
        sweep=bool(opts.get("sweep")),
    )
    if result.started:
        return "career started"
    if result.career_before and result.career_before.active:
        return "a career was already running"
    return "career not started"


def _disconnect_slot(settings: Settings, cfg: dict, previous) -> None:
    """Free the Steam slot, reusing the previous session when there is one."""
    if previous is not None:
        previous.call("account/steam_chain_disconnect", {})
        return
    # A fresh udid makes the signup idempotent: it returns whichever account
    # holds the slot, with its auth_key. The stored viewer_id is no use - it may
    # belong to an account already deleted (709).
    client = _new_client(settings, cfg)
    _signup_and_login(client)
    client.call("account/steam_chain_disconnect", {})


def _is_stale_slot(exc: Exception) -> bool:
    """The server still has the previous account attached to this slot."""
    text = str(exc)
    return any(code in text for code in STALE_ACCOUNT_CODES)


def _make_clean_account(settings: Settings, cfg: dict, previous, tries: int = 3):
    """Free the slot and create a fresh account on it. Returns (client, carats).

    Retried: the server does not release a slot instantly, and a signup landing
    in that window answers 207/217/1102/709. Each attempt waits longer.
    """
    last: Exception | None = None
    for attempt in range(tries):
        try:
            _disconnect_slot(settings, cfg, previous)
        except Exception as exc:  # noqa: BLE001
            if "394" in str(exc):       # the Steam ticket, not the slot
                raise
            log.debug("disconnect warning: %s", str(exc)[:120])
        previous = None

        paced_sleep(4 + 3 * attempt)    # give the server time to let go

        client = _new_client(settings, cfg)
        try:
            return client, _setup_account(client)
        except Exception as exc:  # noqa: BLE001
            last = exc
            if "394" in str(exc):
                raise
            if _is_stale_slot(exc) and attempt < tries - 1:
                log.warning("Slot still held (attempt %s/%s): %s",
                            attempt + 1, tries, str(exc)[:110])
                continue
            raise
    raise last or FarmError("could not create a clean account")


def farm(settings: Settings, banner: int, count: int, max_pulls: int, *,
         password: str = "ChangeMe2026", roster_path: Path = DEFAULT_ROSTER,
         pace_scale: float = 1.0, name_prefix: str = "",
         free_only: bool = False, start_career: bool = False,
         career_opts: dict | None = None) -> list[FarmedAccount]:
    """Create ``count`` accounts, up to ``max_pulls`` ten-pulls each, save the roster.

    With ``start_career`` (the stress test), each new account also starts one
    Independent career after its pulls, before its Data Link is saved. That path
    is capped at :data:`STRESS_MAX_COUNT` accounts: it is real signups + gacha +
    a career start against the live server, so it is bounded rather than endless.
    """
    career_opts = career_opts or {}
    if start_career and count > STRESS_MAX_COUNT:
        raise FarmError(
            f"--start-career is capped at {STRESS_MAX_COUNT} accounts per run "
            f"(asked for {count}). It drives real signups, gacha and a career "
            f"start on the live server; run it again for more, deliberately.")
    acct.validate_password(password)
    if pace_scale < 0:
        raise FarmError("--pace cannot be negative")
    if pace_scale == 0:
        log.warning("--pace 0: no gaps between pulls or accounts. Fast, and a "
                    "very distinctive traffic pattern.")
    if max_pulls < 0:
        raise FarmError("--max-pulls cannot be negative")
    log.info("Creating %s account(s), up to %s ten-pull(s) each on banner %s%s.",
             count, max_pulls, banner, " (free pulls only)" if free_only else "")

    cfg = load_auth_config(settings)
    current = cfg.get("viewer_id")
    if current and not acct.find_account(settings, current):
        raise FarmError(
            f"the account on this Steam slot ({acct.format_trainer_id(current)}) "
            f"has no Link Password on record, and farming deletes it.\n"
            f"  Save one first:   account.bat save-link <password>\n"
            f"  Or record a known one:  account.bat remember \"{acct.format_trainer_id(current)}\" <password>")

    # One Steam session for the whole run - the ticket stays valid throughout.
    refresh_steam_ticket(cfg, settings)
    if steam.has_live_session():
        log.info("Holding one Steam session for the whole run.")

    farmed: list[FarmedAccount] = []
    client = None
    for cycle in range(1, count + 1):
        log.info("")
        log.info("=== account %s/%s ===", cycle, count)
        try:
            client, carats = _make_clean_account(settings, cfg, client)
            viewer = int(client.viewer_id)
            log.info("Created %s with %s carats.",
                     acct.format_trainer_id(viewer), carats)

            if str(client.res_ver) != str(cfg.get("res_ver")):
                cfg["res_ver"] = str(client.res_ver)
                log.debug("adopted res_ver %s", client.res_ver)

            cards = _pull(client, banner, carats, max_pulls,
                          free_only=free_only, pace_scale=pace_scale)

            if start_career:
                try:
                    status = _start_career(settings, client, cfg, career_opts)
                    log.info("  %s on %s.", status, acct.format_trainer_id(viewer))
                except Exception as exc:  # noqa: BLE001 - keep the account regardless
                    log.warning("  career start failed on %s (%s); keeping the "
                                "account anyway.", acct.format_trainer_id(viewer),
                                str(exc)[:160])

            link = False
            try:
                res = client.call("account/publish_transition_code",
                                  {"password": acct.hash_password(password)})
                link = (res or {}).get("data_headers", {}).get("result_code") == 1
            except Exception as exc:  # noqa: BLE001
                log.warning("publish_transition_code failed: %s", exc)
            if not link:
                log.error("Data Link NOT set for %s - it would be lost on the "
                          "next cycle, so it is not added to the roster.",
                          acct.format_trainer_id(viewer))
                continue

            entry = FarmedAccount(
                viewer_id=viewer, trainer_id=acct.format_trainer_id(viewer),
                password=password, cards=cards, link_set=True,
                name=f"{name_prefix}{cycle:02d}" if name_prefix else "")
            farmed.append(entry)
            # entry.name is the --name-prefix label, not the account's in-game
            # name, and it reaches the roster through _save_roster below. Keep
            # it out of accounts.json's in-game-name field.
            acct.remember_account(settings, viewer, password, True)
            _save_roster(roster_path, farmed)
            log.info("Kept %s (%s cards). Roster now %s account(s).",
                     entry.trainer_id, len(cards), len(farmed))

        except Exception as exc:  # noqa: BLE001 - one bad cycle must not end the run
            log.error("Cycle %s failed: %s", cycle, str(exc)[:200])
            client = None
            paced_sleep(8)

        _gap(CYCLE_GAP, pace_scale)
        if random.random() < CYCLE_BREAK_CHANCE:
            _gap(CYCLE_BREAK, pace_scale)

    log.info("")
    log.info("Farmed %s/%s account(s) into %s", len(farmed), count, roster_path)
    return farmed


def _save_roster(path: Path, farmed: list[FarmedAccount]) -> None:
    """Write the roster after every account, so a crash loses nothing.

    Merges with whatever is already there rather than replacing it - the file is
    also the rotation's roster and may hold accounts from earlier runs.
    """
    existing: list[dict] = []
    options: dict = {"career_minutes": 50}
    if path.is_file():
        try:
            raw = json.loads(path.read_text(encoding="utf-8-sig"))
            if isinstance(raw, dict):
                options = {k: v for k, v in raw.items() if k != "accounts"}
                existing = list(raw.get("accounts") or [])
            elif isinstance(raw, list):
                existing = list(raw)
        except (OSError, ValueError) as exc:
            log.warning("could not read the existing roster (%s); starting fresh", exc)

    known = {acct.parse_trainer_id(str(r.get("trainer_id") or r.get("viewer_id") or 0))
             for r in existing if r.get("trainer_id") or r.get("viewer_id")}
    for entry in farmed:
        if entry.viewer_id in known:
            continue
        known.add(entry.viewer_id)
        existing.append({"trainer_id": entry.trainer_id,
                         "password": entry.password,
                         "name": entry.name})

    payload = dict(options)
    payload["accounts"] = existing
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(path)
