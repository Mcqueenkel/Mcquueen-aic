"""Daily-chore cycles that ride on the career rotation's per-account login.

Each account is logged in one at a time during :func:`aic.rotate.visit`; while it
is, run whatever chores are DUE for it, gated by per-account timestamps in
``chores_state.json`` (next to ``career_clock.json``). Career farming stays the
primary driver: every chore is best-effort and NEVER raises into the visit, so an
account that is not in a circle, has no team, or has a locked race just logs and
moves on without touching the career.

Cadences:
  gifts       1x/day      claim the whole gift box (AIC_GIFTS)
  club        AIC_CLUB_EVERY  post the shoes request, then bulk-donate any item
  team        every 1h    Team Trials: best team, spend every RP (AIC_TEAM)
  daily_race  1x/day      every Daily Race Ticket in one multi race (AIC_DAILY_RACE)
  legend      parked
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path

from .config import Settings
from .logging_setup import get

log = get("chores")

STORE_NAME = "chores_state.json"

CLUB_EVERY_SECONDS = float(os.environ.get("AIC_CLUB_EVERY") or 8 * 3600)
TEAM_EVERY_SECONDS = float(os.environ.get("AIC_TEAM_EVERY") or 3600)

#: The circle's "shoes" item id, tracked by circle_chat/polling. The cold-start
#: capture dumps/20260917_190147 used item 7; tune if the circle's item differs.
CLUB_ITEM_ID = int(os.environ.get("AIC_CLUB_ITEM") or 7)

#: Server limits, straight from load/index common_define (dumps/20260921_231145):
#: max_donate_count_day 10 (gifts per account per game-day, counted by
#: daily_donated_count), max_donate_num_total 10 (items that fill a request) and
#: max_donate_num_for_request 2 (items one member may give a request).
CLUB_DONATE_CAP = int(os.environ.get("AIC_CLUB_DONATE_CAP") or 10)
CLUB_REQUEST_ITEMS = 10
CLUB_GIFT_ITEMS = 2

#: Requests live 8h (item_request_expire) but EXPIRED, FULL and ALREADY-GIVEN ones
#: all stay listed on the board for days, and one of them in a batch gets the whole
#: donate refused (5021). The expired ones were what blocked every account while
#: requests sat "open" - the filter in _donate_batch keeps them out.

#: The game's daily reset in UTC. The farm already treats 15:00 UTC as the reset
#: (see rotate.visit's fan-cap note); the once-a-day chores key off the same line
#: so "done today" flips exactly when the game's dailies refill.
RESET_HOUR_UTC = int(os.environ.get("AIC_RESET_HOUR_UTC") or 15)


def enabled() -> bool:
    return (os.environ.get("AIC_CHORES") or "").strip().lower() in {"1", "true", "yes", "on"}


def _only_account() -> str:
    """During a staged rollout, AIC_CHORES_ONLY=<trainer_id> limits chores to one
    account so a new flow is proven on a single live account before all 14."""
    return (os.environ.get("AIC_CHORES_ONLY") or "").strip()


# --------------------------------------------------------------------------- #
# per-account chore state
# --------------------------------------------------------------------------- #
def store_path(settings: Settings) -> Path:
    return settings.state_dir / STORE_NAME


def _load(settings: Settings) -> dict:
    try:
        return json.loads(store_path(settings).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        log.warning("chores state unreadable (%s); starting fresh", exc)
        return {}


def _save(settings: Settings, state: dict) -> None:
    p = store_path(settings)
    tmp = p.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
        tmp.replace(p)
    except OSError as exc:
        log.warning("could not write chores state (%s)", exc)


def reset_day(now: float | None = None) -> str:
    """The game-day string (YYYY-MM-DD) that the current time falls in, where a
    day starts at RESET_HOUR_UTC. Used to gate once-a-day chores."""
    dt = datetime.fromtimestamp(now if now is not None else time.time(), timezone.utc)
    return (dt - timedelta(hours=RESET_HOUR_UTC)).strftime("%Y-%m-%d")


def _due_periodic(rec: dict, key: str, period: float, now: float) -> bool:
    return (now - float(rec.get(key) or 0)) >= period


def _due_daily(rec: dict, key: str, now: float) -> bool:
    return rec.get(key) != reset_day(now)


# --------------------------------------------------------------------------- #
# club
# --------------------------------------------------------------------------- #
def _rc(res) -> int:
    """result_code out of a client response, or 0 when it is not shaped that way.

    client.call() returns the response body: the payload is at ``res["data"]``
    (single level) and the headers at ``res["data_headers"]`` (top level) - the
    same shape aic.account and Session.refresh_home read.
    """
    if not isinstance(res, dict):
        return 0
    dh = res.get("data_headers") or {}
    return int(dh.get("result_code") or res.get("response_code") or 0)


def _exc_rc(exc) -> int:
    """The API result_code carried on a client.call exception, or 0."""
    try:
        from . import bootstrap
        return int(bootstrap.result_code(exc) or 0)
    except Exception:  # noqa: BLE001
        return 0


def _ping(who: str, did: list, title: str = "Club") -> None:
    """One concise Discord line when a chore actually did something. Never raises."""
    if not did:
        return
    try:
        from . import notify
        icon = "🎽" if title == "Club" else "🏟️"
        notify.post({"username": "AIC", "content": f"{icon} {title} — {who}: {', '.join(did)}."})
    except Exception:  # noqa: BLE001 - a notify failure must not cost the chore
        pass


#: Club runs in two phases each game-day. Every account first posts its shoes
#: request; only once ALL roster accounts have one up do they donate - so each
#: donor sees every request and its 10 daily donates are not spent on the first
#: one or two on the board (or refused). The wait is the safety valve: that long
#: after the 15:00 UTC reset, donate anyway even if some account never requested
#: (not in a circle, failing visits), so one account can never block the rest.
CLUB_STATE_NAME = "club_state.json"
CLUB_REQUEST_WAIT_SECONDS = float(os.environ.get("AIC_CLUB_REQUEST_WAIT") or 3 * 3600)


def _roster_ids() -> set[int]:
    """Trainer ids of the rotation roster (reroll_accounts.json) that take part in
    the club (not ``"club": false``), or empty."""
    try:
        from .rotate import load_roster
        accounts, _ = load_roster()
        return {int(a.trainer_id) for a in accounts if getattr(a, "club", True)}
    except Exception:  # noqa: BLE001 - no roster: the gate degrades to "donate"
        return set()


def _club_day(settings: Settings, now: float) -> dict:
    """Today's ``{day, requested: [viewer ids]}`` from club_state.json."""
    try:
        st = json.loads((settings.state_dir / CLUB_STATE_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        st = {}
    day = reset_day(now)
    if not isinstance(st, dict) or st.get("day") != day:
        st = {"day": day, "requested": []}
    req = st.get("requested")
    st["requested"] = [int(i) for i in req if str(i).isdigit()] if isinstance(req, list) else []
    return st


def _club_save(settings: Settings, st: dict) -> None:
    p = settings.state_dir / CLUB_STATE_NAME
    tmp = p.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(st, indent=1), encoding="utf-8")
        tmp.replace(p)
    except OSError as exc:
        log.warning("could not write %s (%s)", p.name, exc)


def _mark_requested(settings: Settings, viewer_id: int, now: float | None = None) -> None:
    """Record that this account has a shoes request up this game-day."""
    now = time.time() if now is None else now
    st = _club_day(settings, now)
    st["requested"] = sorted({int(i) for i in st.get("requested") or []} | {int(viewer_id)})
    _club_save(settings, st)


def _refused_key(request_id, number) -> str:
    """A refusal is remembered with the stock we declared: a retry declaring a
    different stock (the cause behind past 501s) is a different gift."""
    return f"{int(request_id)}:{int(number)}"


def _refused_today(settings: Settings, viewer_id: int, now: float | None = None) -> set[str]:
    """Requests the server refused this account's gift for today (``_refused_key``).
    Club runs on every visit, so without this a request that always fails would be
    re-sent all day - and, sitting oldest on the board, crowd out good ones."""
    st = _club_day(settings, time.time() if now is None else now)
    ref = st.get("refused")
    got = ref.get(str(viewer_id)) if isinstance(ref, dict) else None
    return {str(s) for s in got} if isinstance(got, list) else set()


def _mark_refused(settings: Settings, viewer_id: int, key: str, now: float | None = None) -> None:
    st = _club_day(settings, time.time() if now is None else now)
    ref = st.get("refused") if isinstance(st.get("refused"), dict) else {}
    mine = ref.get(str(viewer_id)) if isinstance(ref.get(str(viewer_id)), list) else []
    if key not in mine:
        mine.append(key)
    ref[str(viewer_id)] = mine[-200:]
    st["refused"] = ref
    _club_save(settings, st)


def _donate_phase(settings: Settings, now: float | None = None,
                  roster=None) -> tuple[bool, str]:
    """``(donate now?, why)``: yes once every roster account has requested today,
    or once CLUB_REQUEST_WAIT has passed since the reset."""
    now = time.time() if now is None else now
    roster = _roster_ids() if roster is None else {int(i) for i in roster}
    if not roster:
        return True, "no roster to wait for"
    done = {int(i) for i in _club_day(settings, now).get("requested") or []}
    missing = roster - done
    if not missing:
        return True, f"all {len(roster)} accounts have posted today's request"
    reset_at = (datetime.strptime(reset_day(now), "%Y-%m-%d").replace(tzinfo=timezone.utc)
                + timedelta(hours=RESET_HOUR_UTC)).timestamp()
    waited = now - reset_at
    if waited >= CLUB_REQUEST_WAIT_SECONDS:
        return True, (f"{len(missing)} account(s) still without a request {waited / 3600:.1f}h "
                      f"after the reset ({', '.join(str(i)[-4:] for i in sorted(missing))}) - "
                      f"donating anyway")
    return False, (f"request phase: {len(roster) - len(missing)}/{len(roster)} accounts have "
                   f"posted today's request")


def _added_items(res) -> dict[int, int]:
    """``{item_id: count}`` a response handed over (reward_summary_info.add_item_list)."""
    data = res.get("data") if isinstance(res, dict) else None
    rows = ((data or {}).get("reward_summary_info") or {}).get("add_item_list") or []
    out: dict[int, int] = {}
    for r in rows:
        try:
            out[int(r.get("item_id"))] = out.get(int(r.get("item_id")), 0) + int(r.get("number") or 0)
        except (TypeError, ValueError, AttributeError):
            continue
    return out


def _circle_view(entered, polled, got):
    """(board, gifts, daily_donated_count, servertime) from the circle responses.

    The board is get_request_data's (freshest; room_enter's only if that read
    failed - merging would resurrect requests claimed in between). Gifts
    (donate_id, request_id, viewer_id, item_num) are unioned from all three:
    room_enter and get_request_data carry every member's gifts on the listed
    requests, polling only new ones. daily_donated_count comes from room_enter.
    """
    def data(res):
        d = res.get("data") if isinstance(res, dict) else None
        return d if isinstance(d, dict) else {}

    board = data(got).get("circle_item_request_array")
    if board is None:
        board = data(entered).get("circle_item_request_array") or []
    gifts = {}
    for res in (entered, polled, got):
        for g in data(res).get("circle_item_donate_array") or []:
            gifts[g.get("donate_id") or (g.get("request_id"), g.get("viewer_id"),
                                         g.get("create_time"))] = g
    count = data(entered).get("daily_donated_count")
    now = None
    for res in (got, entered):
        st = ((res or {}).get("data_headers") or {}).get("servertime") if isinstance(res, dict) else None
        if st:
            now = float(st)
            break
    return (list(board), list(gifts.values()),
            None if count is None else int(count), now)


def _expired(req, now) -> bool:
    """end_time is naive UTC 'YYYY-MM-DD HH:MM:SS'. 60s of slack covers the gap
    between our board read and the commit. Unparseable -> treated as live."""
    try:
        end = datetime.strptime(str(req.get("end_time")), "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return False
    return end.replace(tzinfo=timezone.utc).timestamp() <= now + 60


#: The 5 racing shoes. Every other requestable item (master.mdb item_data
#: enable_request=1: 39 in all) is a Winner's Sash. Names are only for the log.
SHOE_NAMES = {1: "sprint shoes", 4: "mile shoes", 7: "medium shoes", 10: "long shoes",
              13: "dirt shoes"}


@lru_cache(maxsize=2)
def _read_item_names(path: str) -> dict[int, str]:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return {int(i): str(t) for i, t in
                con.execute('SELECT "index", text FROM text_data WHERE category = 23')}
    finally:
        con.close()


def _item_names() -> dict[int, str]:
    """``{item_id: in-game name}`` from master.mdb text_data category 23
    ("Medium Racing Shoes", "Hanshin J.F. Winner's Sash"); {} when unreadable.
    Only a successful read is cached, so a master.mdb that shows up later is used."""
    from .racedata import find_master_mdb
    found = find_master_mdb()
    if not found:
        return {}
    try:
        return _read_item_names(str(found))
    except sqlite3.Error:
        return {}


def _item_name(item_id: int) -> str:
    return (_item_names().get(int(item_id)) or SHOE_NAMES.get(int(item_id))
            or f"sash {item_id}")


def _items_text(given: dict) -> str:
    """'16x Medium Racing Shoes, 2x Hanshin J.F. Winner's Sash' - most given first."""
    return ", ".join(f"{n}x {_item_name(i)}" for i, n in
                     sorted(given.items(), key=lambda kv: (-kv[1], kv[0])) if n)


def _donate_batch(board, gifts, viewer_id, stock, cap, now=None, refused=()):
    """The requests to bulk-donate to - what the game's bulk-donate button picks.

    Requests from OTHER members, for ANY item we hold, that have not expired, are
    not full (< CLUB_REQUEST_ITEMS given) and that we have not maxed out ourselves
    (< CLUB_GIFT_ITEMS given by us). Oldest request first across all items, cut to
    ``cap`` (what is LEFT of today's donates) - the client's own rule in every
    capture (20260713 #18 items 1+10, 20260913 items 13+10, 20260922 its 10 lowest
    open request_ids). ``stock`` is ``{item_id: count}``; an item gets at most
    ceil(count/2) requests, since the server hands out min(2, what is left) per
    request in array order. ``number`` DECLARES our stock of that request's item,
    repeated per request. ``refused`` (``_refused_key``s) are left out BEFORE the
    cut, so a request the server keeps refusing never takes a good one's slot.
    Returns (request_id_array, client_item_data_array, tally) where tally =
    {item_id: {"open", "expired", "full", "already given", "refused today"}}.
    """
    now = time.time() if now is None else now
    given, mine = {}, {}
    for g in gifts:
        rid_ = int(g.get("request_id") or 0)
        n = int(g.get("item_num") or 0)
        given[rid_] = given.get(rid_, 0) + n
        if g.get("viewer_id") == viewer_id:
            mine[rid_] = mine.get(rid_, 0) + n
    tally, open_ = {}, []
    for r in board:
        if r.get("viewer_id") == viewer_id or not r.get("request_id"):
            continue
        rid_, iid = int(r["request_id"]), int(r.get("item_id") or 0)
        t = tally.setdefault(iid, {"open": 0, "expired": 0, "full": 0, "already given": 0,
                                   "refused today": 0})
        if _expired(r, now):
            t["expired"] += 1
        elif given.get(rid_, 0) >= CLUB_REQUEST_ITEMS:
            t["full"] += 1
        elif mine.get(rid_, 0) >= CLUB_GIFT_ITEMS:
            t["already given"] += 1
        elif _refused_key(rid_, stock.get(iid) or 0) in refused:
            t["refused today"] += 1
        else:
            t["open"] += 1
            open_.append((rid_, iid))
    room = {iid: -(-max(int(stock.get(iid) or 0), 0) // CLUB_GIFT_ITEMS) for iid in tally}
    picked = []
    for rid_, iid in sorted(open_):
        if len(picked) >= cap:
            break
        if room[iid] > 0:
            room[iid] -= 1
            picked.append((rid_, iid))
    return ([r for r, _ in picked],
            [{"item_id": iid, "number": int(stock[iid])} for _, iid in picked], tally)


def _board_summary(tally, stock) -> str:
    """'medium shoes x3 (1 expired, 2 already given); sash 83 x2 (2 open, we hold none)'."""
    parts = []
    for iid in sorted(tally, key=lambda i: (i not in SHOE_NAMES, i)):
        t = tally[iid]
        why = ", ".join(f"{v} {k}" for k, v in t.items() if v)
        held = "" if int(stock.get(iid) or 0) > 0 else ", we hold none"
        parts.append(f"{_item_name(iid)} x{sum(t.values())} ({why}{held})")
    return "; ".join(parts) or "no member requests"


def _split(rid, items):
    """A refused batch, smaller: a mixed one as one batch per item (each item's
    oldest request first), a single-item one as single requests - so whatever
    the server objects to is pinned down to the one request, and only that one
    is remembered."""
    if len({it["item_id"] for it in items}) == 1:
        return [([r], [it]) for r, it in zip(rid, items)]
    groups = {}
    for r, it in zip(rid, items):
        groups.setdefault(it["item_id"], ([], []))
        groups[it["item_id"]][0].append(r)
        groups[it["item_id"]][1].append(it)
    return sorted(groups.values(), key=lambda g: g[0][0])


def _commit(client, viewer_id, rid, items, count, log_, known=frozenset()):
    """One bulk donate - donate_multiple(payload) right after the board read, the
    official client's own sequence in all four captures. Never raises. Returns
    ``(n, after, rc, given)``: our donates, the server's new daily_donated_count
    (or None), 1 on success or the refusal code (0 = no code, e.g. a network
    error), and ``{item_id: items we gave}``. ``known`` = donate_ids already on
    the board before this commit."""
    names = ", ".join(dict.fromkeys(_item_name(it["item_id"]) for it in items))
    payload = {"request_id_array": list(rid), "client_item_data_array": [dict(it) for it in items]}
    try:
        donated = client.call("circle_item_request/donate_multiple", payload)
        dd = donated.get("data") or {}
        after = dd.get("daily_donated_count")
        after = None if after is None else int(after)
        # The response lists EVERY gift on the staged requests (others' and our
        # older ones too); OUR new rows say what we just gave (item_num each).
        item_of = {r: it["item_id"] for r, it in zip(rid, items)}
        ours = [x for x in dd.get("circle_item_donate_array") or []
                if x.get("viewer_id") == viewer_id and x.get("request_id") in item_of
                and x.get("donate_id") not in known]
        given: dict[int, int] = {}
        for x in ours:
            iid = item_of[x["request_id"]]
            given[iid] = given.get(iid, 0) + int(x.get("item_num") or 0)
        # count our donates from the server's own counter; else (or when it went
        # BACKWARDS - a visit crossing the 15:00 UTC reset) from our new rows
        n = after - count if after is not None and count is not None and after >= count else len(ours)
        log_.info("club: donate_multiple rc=%s - bulk-donated to %d member request(s) (%s)%s",
                  _rc(donated), n, _items_text(given) or names,
                  "" if after is None else f" (today {after}/{CLUB_DONATE_CAP})")
        return n, after, 1, given
    except Exception as exc:  # noqa: BLE001
        rc = _exc_rc(exc)
        if rc in (102, 5020, 5021, 5022):
            log_.info("club: donate refused (rc %s, %s) - a staged request changed after our "
                      "board read, or today's donates are spent", rc, names)
        elif rc == 501:
            log_.info("club: donate refused (rc 501, %s) - a staged request filled up first", names)
        else:
            log_.info("club: donate failed (rc %s, %s: %s)", rc, names, exc)
        return 0, None, rc, {}


def club(session, log_=log, label: str = "") -> bool:
    """Post this account's shoes request, then bulk-donate to every member request
    it can fill - any shoes or Winner's Sash it holds, like the game's button.

    Never raises. The sequence:

        circle/room_enter
        circle_chat/polling({item_id})      <- WITHOUT this, send_item_request 102s
        circle_item_request/receive({})     <- claim our filled request first
        circle_chat/send_item_request({item_id})      <- today's shoes request
        circle_item_request/get_request_data({})      <- read the board
        circle_item_request/donate_multiple({request_id_array,
            client_item_data_array:[{item_id, number = my stock of it}]})

    The last two are the official client's own pair in every capture (the dumps'
    labels are shifted by one: 20260917's "get_request_data(payload)" is really
    donate_multiple - 20260713_192951 is the correctly repaired session). The
    `number` DECLARES the account's stock of that request's item; the server
    gives min(2, what the request still needs, what is left of that stock) per
    request and counts one donate per request in daily_donated_count. Pings
    Discord on any rc=1 write so club is visible with the PC off.
    """
    from .state import read_account
    client = session.client
    who = label or f"viewer {session.viewer_id}"
    try:
        entered = client.call("circle/room_enter", {})
    except Exception as exc:  # noqa: BLE001 - not in a circle, or circle call refused
        log_.info("club: skipped (room_enter failed: %s) - account likely not in a circle", exc)
        return False

    # Establish the chat session with the tracked item. Missing this -> 102.
    polled = None
    try:
        polled = client.call("circle_chat/polling", {"item_id": CLUB_ITEM_ID})
    except Exception as exc:  # noqa: BLE001
        log_.info("club: polling failed (%s)", exc)

    item_map = {}
    try:
        item_map = read_account(session.load_index or {}).item_map or {}
    except Exception:  # noqa: BLE001
        pass

    did: list[str] = []

    # 0) CLAIM any fulfilled request first. Once ~5 members donate to this
    # account's request it becomes "done" and must be received/acknowledged, or it
    # blocks every new send_item_request with 5020 (dumps/20260921_231145:
    # room_enter shows the donations received, then circle_item_request/receive
    # clears it). This gate is what stopped the account from requesting again.
    # Parameterless; best-effort - nothing to claim just returns rc=1/no-op.
    try:
        claimed = client.call("circle_item_request/receive", {})
        # The claim HANDS OVER the shoes members gave us (reward_summary_info.
        # add_item_list - dumps/20260921_231145: 10 of the requested item). The
        # stock read at login is now short by that much, and a donate declaring a
        # stale stock is refused whole (501) - which is what failed ~half the
        # donors each day. Keep item_map current.
        got_items = _added_items(claimed)
        for iid, n in got_items.items():
            item_map[iid] = int(item_map.get(iid, 0)) + n
        shoes = got_items.get(CLUB_ITEM_ID, 0)
        log_.info("club: receive - claimed fulfilled request(s)%s",
                  f": +{shoes} shoes from members" if shoes else " (nothing to claim)")
        if shoes:
            did.append(f"received {shoes} shoes from members")
    except Exception as exc:  # noqa: BLE001 - nothing to claim is fine
        log_.info("club: receive skipped (%s)", exc)

    # 1) Post today's shoes REQUEST (members then donate to it). send_item_request
    # NEEDS {item_id} - the farm's fresh-takeover session carries none of the
    # implicit game state the capture had, so an empty body 102s (proved live:
    # {} -> 102, {item_id:7} -> rc=1). A 5020 AFTER a successful receive means
    # genuinely already requested this game-day; a 5020 without receiving first
    # was the unclaimed-request gate.
    settings = getattr(session, "settings", None)
    try:
        client.call("circle_chat/send_item_request", {"item_id": CLUB_ITEM_ID})
        log_.info("club: send_item_request rc=1 - posted today's shoes request (item %s)",
                  CLUB_ITEM_ID)
        did.append("requested shoes from the club")
        if settings is not None:
            _mark_requested(settings, session.viewer_id)
    except Exception as exc:  # noqa: BLE001
        rc = _exc_rc(exc)
        if rc in (102, 5020):
            log_.info("club: shoes request already done this game-day (rc %s)", rc)
            if settings is not None:
                _mark_requested(settings, session.viewer_id)
        else:
            log_.info("club: send_item_request skipped (%s)", exc)

    # Donate only once every account's request is up (see CLUB_REQUEST_WAIT).
    if settings is not None:
        ready, why = _donate_phase(settings)
        if not ready:
            log_.info("club: %s - donating once every account has one (or %.0fh after "
                      "the reset)", why, CLUB_REQUEST_WAIT_SECONDS / 3600)
            _ping(who, did)
            return True
        log_.info("club: donate phase - %s", why)

    # 2) BULK donate to every member request that can still take a gift, for any
    # item we hold - what the game's bulk-donate button does: get_request_data
    # (payload) then donate_multiple(payload) (request_id_array + client_item_data_
    # array, number = our stock of that request's item) - PROVED LIVE 2026-09-23
    # (rc=1); donate_multiple({}) EMPTY 102s. The board alone cannot tell a dead
    # request from an open one: the gifts and end_time pick out the expired / full /
    # already-given ones, and daily_donated_count sizes the batch to what is left
    # of today's donates (and skips the call once they are spent).
    me = session.viewer_id
    got = None
    try:
        got = client.call("circle_item_request/get_request_data", {})
    except Exception as exc:  # noqa: BLE001
        log_.info("club: board read failed (%s)", exc)
    try:
        board, gifts, count, now = _circle_view(entered, polled, got)
        left = CLUB_DONATE_CAP - (count or 0)
        refused = _refused_today(settings, me) if settings is not None else set()
        rid, items, tally = _donate_batch(board, gifts, me, item_map, left, now, refused)
        known = frozenset(g.get("donate_id") for g in gifts)
    except Exception as exc:  # noqa: BLE001 - malformed circle data must not raise
        log_.info("club: could not read the circle board (%s); donating nothing this visit", exc)
        _ping(who, did)
        return True
    if left <= 0:
        log_.info("club: today's %d donates are spent (%d/%d) - more after the %02d:00 UTC reset",
                  CLUB_DONATE_CAP, count, CLUB_DONATE_CAP, RESET_HOUR_UTC)
        _ping(who, did)
        return True
    log_.info("club: board=%d request(s): %s - %d to donate now, donated today %s/%d",
              len(board), _board_summary(tally, item_map), len(rid),
              "?" if count is None else count, CLUB_DONATE_CAP)
    if not rid:
        log_.info("club: no member requests open to us for an item we hold right now")
        _ping(who, did)
        return True

    # One mixed batch, like the client. Should the server refuse it, split it
    # (per item, then per request) and retry, so one bad request never costs the
    # others; the single request still refused is remembered for today and left
    # out of every later batch. No code (rc 0, e.g. a network error): next visit.
    batches = [(rid, items)]
    total, names, given = 0, [], {}
    while batches:
        b_rid, b_items = batches.pop(0)
        n, after, rc, got_ = _commit(client, me, b_rid, b_items, count, log_, known)
        if after is not None:
            count = after
        elif count is not None:
            count += n
        if n:
            total += n
            names += [_item_name(i) for i in sorted({it["item_id"] for it in b_items})]
        for iid, k in got_.items():
            given[iid] = given.get(iid, 0) + k
        if rc in (0, 1):
            continue
        if len(b_rid) > 1:
            log_.info("club: %d-request batch refused (rc %s) - retrying it in parts", len(b_rid), rc)
            batches = _split(b_rid, b_items) + batches
        elif settings is not None:
            _mark_refused(settings, me, _refused_key(b_rid[0], b_items[0]["number"]))
    if total:
        # what we gave, item by item; just the item names if the server listed no rows
        what = _items_text(given) or ", ".join(dict.fromkeys(names))
        items_n = sum(given.values())
        did.append(f"donated {f'{items_n} items ' if items_n else ''}to {total} member "
                   f"request(s) ({what})")

    _ping(who, did)
    return True


# --------------------------------------------------------------------------- #
# team trials
# --------------------------------------------------------------------------- #
#: Team Trials evaluation, from master.mdb team_stadium_evaluation_rate (units of
#: 1/10000, by aptitude 1..8 = G..S). Distance and ground share one table.
APT_RATE = {1: 3000, 2: 4000, 3: 5000, 4: 6000, 5: 7000, 6: 9000, 7: 10000, 8: 10200}
STYLE_RATE = {1: 7000, 2: 7500, 3: 8000, 4: 8500, 5: 9000, 6: 9500, 7: 10000, 8: 10200}
#: distance_type -> the trained_chara aptitude it reads. 5 is dirt, which reads
#: the MILE distance aptitude plus proper_ground_dirt (this mapping reproduced the
#: captured team_evaluation_point 61,684 exactly; "short" for dirt did not).
DIST_FIELD = {1: "proper_distance_short", 2: "proper_distance_mile",
              3: "proper_distance_middle", 4: "proper_distance_long", 5: "proper_distance_mile"}
STYLE_FIELD = {1: "proper_running_style_nige", 2: "proper_running_style_senko",
               3: "proper_running_style_sashi", 4: "proper_running_style_oikomi"}
#: Members per distance by team_class (master.mdb team_stadium_class.unit_max_num).
TEAM_UNITS = {1: 1, 2: 2}            # classes 3-6: 3
#: Which offered opponent to fight, by evaluation_point (NOT the `strength`
#: number: both captured lists show strength 1 is the STRONGEST).
TEAM_OPPONENT = (os.environ.get("AIC_TEAM_OPPONENT") or "weakest").strip().lower()
#: Battles per visit at most (RP caps at 5).
TEAM_MAX_BATTLES = 5


def team_switch() -> bool:
    return (os.environ.get("AIC_TEAM") or "").strip().lower() in {"1", "true", "yes", "on"}


def team_enabled(viewer_id) -> bool:
    only = (os.environ.get("AIC_TEAM_ONLY") or "").strip()
    return team_switch() and (not only or only == str(viewer_id))


def member_point(chara: dict, distance_type: int, style: int) -> int:
    """One member's share of team_evaluation_point (the client's formula)."""
    try:
        ground = chara["proper_ground_dirt" if distance_type == 5 else "proper_ground_turf"]
        return (int(chara.get("rank_score") or 0)
                * APT_RATE.get(int(chara[DIST_FIELD[distance_type]]), 0)
                * APT_RATE.get(int(ground), 0)
                * STYLE_RATE.get(int(chara[STYLE_FIELD[style]]), 0)) // 10000 ** 3
    except (KeyError, TypeError, ValueError):
        return 0


def best_style(chara: dict) -> int:
    return max(STYLE_FIELD, key=lambda s: STYLE_RATE.get(int(chara.get(STYLE_FIELD[s]) or 0), 0))


def best_team(trained, team_class) -> tuple[list[dict], int]:
    """The strongest legal team from the account's own trained characters.

    Greedy: every (character, distance) pairing scored with the member's best
    running style, taken best-first, each trained character and each uma (card
    // 100) used once, up to the class's members per distance. Returns the full
    15-slot team_data_array (empty slots zeroed, as the client sends it) and its
    team_evaluation_point.
    ponytail: greedy, not an optimal assignment - a Hungarian solve if a few
    hundred points of evaluation ever matter.
    """
    units = TEAM_UNITS.get(int(team_class or 1), 3)
    cands = []
    for c in trained or []:
        if not isinstance(c, dict) or c.get("owner_viewer_id") or not c.get("trained_chara_id"):
            continue                       # borrowed copies are not ours to field
        style = best_style(c)
        for dist in DIST_FIELD:
            pt = member_point(c, dist, style)
            if pt > 0:
                cands.append((pt, int(c["trained_chara_id"]), int(c.get("card_id") or 0) // 100,
                              dist, style))
    cands.sort(reverse=True)
    used_tc, used_uma = set(), set()
    slots: dict[int, list[tuple[int, int, int]]] = {d: [] for d in DIST_FIELD}
    # Member 1 of EVERY distance first, then stack: an empty distance loses its
    # race outright, which no extra member elsewhere makes up for.
    for cap in sorted({1, units}):
        for pt, tc, uma, dist, style in cands:
            if tc in used_tc or uma in used_uma or len(slots[dist]) >= cap:
                continue
            slots[dist].append((tc, style, pt))
            used_tc.add(tc)
            used_uma.add(uma)
    array, total = [], 0
    for dist in DIST_FIELD:
        for member in range(1, 4):
            tc, style, pt = (slots[dist][member - 1] if member <= len(slots[dist]) else (0, 0, 0))
            array.append({"distance_type": dist, "member_id": member,
                          "trained_chara_id": tc, "running_style": style})
            total += pt
    return array, total


def team_point(trained, team_data_array) -> int:
    """The current team's evaluation, by the same formula."""
    by_id = {int(c.get("trained_chara_id") or 0): c for c in trained or [] if isinstance(c, dict)}
    total = 0
    for slot in team_data_array or []:
        c = by_id.get(int(slot.get("trained_chara_id") or 0))
        if c:
            style = int(slot.get("running_style") or 0) or best_style(c)
            total += member_point(c, int(slot.get("distance_type") or 0), style)
    return total


def _update_team(session, idx_data: dict, log_, label: str = "") -> None:
    """team_edit when the best team beats the one set. Never raises."""
    index = session.load_index or {}
    trained = index.get("trained_chara") or []
    team_class = ((idx_data.get("team_stadium_user") or index.get("team_stadium_user") or {})
                  .get("team_class") or 1)
    current = index.get("team_data_array") or []
    try:
        array, total = best_team(trained, team_class)
        now = team_point(trained, current)
    except Exception as exc:  # noqa: BLE001
        log_.info("team: could not build a team (%s)", exc)
        return
    covered = lambda arr: {int(s.get("distance_type") or 0) for s in arr or []
                           if s.get("trained_chara_id")}
    if total <= now or not covered(current) <= covered(array):
        log_.info("team: current team (%s) is already the best (class %s)",
                  f"{now:,}", team_class)
        return
    try:
        res = session.client.call("team_stadium/team_edit",
                                  {"team_data_array": array, "team_evaluation_point": total})
        d = res.get("data") or {}
        log_.info("team: team_edit rc=%s - evaluation %s -> %s (team rank %s -> %s)",
                  _rc(res), f"{now:,}", f"{total:,}", d.get("before_rank"), d.get("after_rank"))
        _ping(label, [f"team evaluation {now:,} -> {total:,}"], title="Team Trials")
    except Exception as exc:  # noqa: BLE001 - battles still run on the old team
        log_.info("team: team_edit refused (%s); keeping the current team", exc)


def team_after_ace(session, log_=log, label: str = "") -> None:
    """Put a just-finished Ace Run trainee on the Team Trials team right away.

    The team chore runs at the START of a visit, before the career ends, and at
    most hourly - so a new ace used to wait one to two visits to be fielded.
    rotate.visit calls this right after an Ace Run is banked (load_index already
    refreshed with the new trainee): if it beats a member, team_edit replaces
    them now. Same rules as the chore (best_team, only when strictly better and
    no distance left empty). Never raises.
    """
    if not team_enabled(session.viewer_id):
        return
    try:
        idx = session.client.call("team_stadium/index", {})
    except Exception as exc:  # noqa: BLE001 - team trials locked/unavailable
        log_.info("team: post-Ace-Run team check skipped (%s)", exc)
        return
    d = idx.get("data") or {}
    if d.get("term_state") not in (1, 3):
        return
    _update_team(session, d, log_, label or f"viewer {session.viewer_id}")


def _pick_opponent(offers: list) -> dict:
    ranked = sorted(offers, key=lambda o: int(o.get("evaluation_point") or 0))
    return {"strongest": ranked[-1], "middle": ranked[len(ranked) // 2]}.get(TEAM_OPPONENT, ranked[0])


def team_trials(session, log_=log, label: str = "") -> bool:
    """Field the best team, then spend every RP on Team Trials battles. Never raises.

    Bodies from the client classes in global-metadata.dat (the 20260913 capture
    this was first built from holds each REQUEST one slot early, which is why
    decide_frame_order kept 102ing):

        team_stadium/index {}
        team_stadium/team_edit {team_data_array, team_evaluation_point}  (if better)
        team_stadium/opponent_list {}             -> opponent_info_array (3)
        team_stadium/decide_frame_order {opponent_info}
        team_stadium/start {item_id_array: []}    -> rp_info, 5 race results
        team_stadium/replay_check {round: 5}
        team_stadium/all_race_end {}              -> fans, score, circle_point

    One battle costs 1 RP; RP caps at 5 and refills 1 per 2h (~12 a day).
    """
    if not team_enabled(session.viewer_id):
        return False
    client = session.client
    try:
        idx = client.call("team_stadium/index", {})
    except Exception as exc:  # noqa: BLE001 - team trials locked/unavailable
        log_.info("team: skipped (index failed: %s) - team trials likely unavailable", exc)
        return False
    d = idx.get("data") or {}
    term = d.get("term_state")
    if term not in (1, 3):
        log_.info("team: no active term (term_state=%s); skipping", term)
        return False
    who = label or f"viewer {session.viewer_id}"
    _update_team(session, d, log_, who)
    if term != 1:
        log_.info("team: between terms (term_state %s) - team set, no battles", term)
        return True

    if d.get("race_status"):
        # A battle was left mid-way (e.g. a crash after start): finish it first.
        try:
            client.call("team_stadium/replay_check", {"round": 5})
            client.call("team_stadium/all_race_end", {})
            log_.info("team: finished a battle left open (race_status %s)", d.get("race_status"))
        except Exception as exc:  # noqa: BLE001
            log_.info("team: could not finish the open battle (%s); skipping", exc)
            return True

    rp = ((session.load_index or {}).get("rp_info") or {}).get("current_rp")
    rp = TEAM_MAX_BATTLES if rp is None else int(rp)
    battles = fans = 0
    while rp > 0 and battles < TEAM_MAX_BATTLES:
        try:
            offers = (client.call("team_stadium/opponent_list", {}).get("data") or {}
                      ).get("opponent_info_array") or []
            if not offers:
                log_.info("team: no opponents offered")
                break
            chosen = _pick_opponent(offers)
            client.call("team_stadium/decide_frame_order", {"opponent_info": chosen})
            started = client.call("team_stadium/start", {"item_id_array": []})
            client.call("team_stadium/replay_check", {"round": 5})
            ended = client.call("team_stadium/all_race_end", {})
        except Exception as exc:  # noqa: BLE001 - out of RP, or the term just closed
            log_.info("team: battle stopped (%s)", exc)
            break
        battles += 1
        ed = ended.get("data") or {}
        got = sum(int(f.get("add_fan") or 0) for f in ed.get("add_fan_info_array") or [])
        fans += got
        left = ((started.get("data") or {}).get("rp_info") or {}).get("current_rp")
        rp = rp - 1 if left is None else int(left)
        log_.info("team: battle %s vs eval %s - win_type %s, +%s fans, RP left %s",
                  battles, chosen.get("evaluation_point"), ed.get("final_win_type"),
                  f"{got:,}", rp)
    if battles:
        # Logged, not pinged: ~12 single battles per account per day would flood
        # the channel. A team change (above) is what gets a Discord line.
        log_.info("team: %s battle(s) this visit, +%s fans", battles, f"{fans:,}")
    return True


# --------------------------------------------------------------------------- #
# daily legend race
# --------------------------------------------------------------------------- #
def daily_legend(session, log_=log, label: str = "") -> bool:
    """The once-a-day Legend Race. Never raises.

    Capture: daily_legend_race/index -> race_entry({item_id_array:[]}) ->
    reflect_item_effect({running_style, is_short}) -> race_start({viewer_id only})
    -> replay_check({}). Unlike Daily Race, legend's race_start is SERVER-resolved
    (it carries no race_result_array), so no client race sim is needed.
    running_style 1 (front runner), is_short 0 - the captured choice.
    """
    client = session.client
    try:
        idx = client.call("daily_legend_race/index", {})
    except Exception as exc:  # noqa: BLE001
        log_.info("legend: skipped (index failed: %s)", exc)
        return False
    recs = (idx.get("data") or {}).get("daily_legend_race_record_array") or []
    unplayed = [r for r in recs if not r.get("is_played")]
    log_.info("legend: index rc=%s, %d race(s), %d unplayed", _rc(idx), len(recs), len(unplayed))
    if not unplayed:
        log_.info("legend: already played today; nothing to do")
        return True
    try:
        client.call("daily_legend_race/race_entry", {"item_id_array": []})
        client.call("daily_legend_race/reflect_item_effect", {"running_style": 1, "is_short": 0})
        started = client.call("daily_legend_race/race_start", {})
        client.call("daily_legend_race/replay_check", {})
        log_.info("legend: race_start rc=%s - legend race done", _rc(started))
    except Exception as exc:  # noqa: BLE001 - already played, or entry refused
        log_.info("legend: race flow stopped (%s)", exc)
    return True


# --------------------------------------------------------------------------- #
# gift box + daily races (capture dumps/20261006_194059, read from its
# _preshift_backup: the dumper's own de-shift left every request one slot early)
# --------------------------------------------------------------------------- #
def _switch(name: str, account=None) -> bool:
    """AIC_<NAME>=1 turns a chore on; AIC_<NAME>_ONLY=<trainer_id> limits it to
    one account for a first live run (the AIC_TEAM / AIC_TEAM_ONLY shape)."""
    on = (os.environ.get(f"AIC_{name}") or "").strip().lower() in {"1", "true", "yes", "on"}
    only = (os.environ.get(f"AIC_{name}_ONLY") or "").strip()
    return on and (account is None or not only or only == str(account))


def _fold_items(session, added: dict, fcoin: int = 0) -> None:
    """Apply what a claim or race changed (item deltas, +free carats) to BOTH
    copies of the account this visit keeps - load_index (club declares its stock
    from it, a stale stock is refused whole; the career start sends its gold) and
    the client's own item_map / coin_info (TP recovery declares client_own_num
    from those) - since no load/index is re-read until the next login."""
    idx = getattr(session, "load_index", None)
    cl = getattr(session, "client", None)
    im = getattr(cl, "item_map", None)
    if isinstance(idx, dict) and added:
        rows = idx.get("item_list")
        if not isinstance(rows, list):
            rows = idx["item_list"] = []
        have = {int(r.get("item_id") or 0): r for r in rows if isinstance(r, dict)}
        for iid, n in added.items():
            if iid in have:
                have[iid]["number"] = max(0, int(have[iid].get("number") or 0) + n)
            elif n > 0:
                rows.append({"item_id": iid, "number": n})
    if isinstance(im, dict):
        for iid, n in added.items():
            im[iid] = max(0, int(im.get(iid) or 0) + n)
    if fcoin:
        seen = set()
        for ci in (getattr(cl, "coin_info", None), (idx or {}).get("coin_info")):
            if isinstance(ci, dict) and id(ci) not in seen:   # often one shared dict
                seen.add(id(ci))
                ci["fcoin"] = int(ci.get("fcoin") or 0) + fcoin


_PRESENT_INDEX = {"time_filter_type": 0, "category_filter_type": [0],
                  "offset": 0, "limit": 100, "is_asc": True}
#: Claim All presses per visit (100 gifts a press): empties a box of up to 500.
GIFT_ROUNDS = 5


def gifts(session, log_=log, label: str = "") -> bool:
    """Claim the whole gift box, like the client's Claim All. Never raises.

        present/index {time_filter_type:0, category_filter_type:[0], offset:0, limit:100, is_asc:true}
        present/receive_all {time_filter_type:0, category_filter_type:[0], is_asc:true,
                             current_rest_present_num: unclaimed count}
        present/index (same body)    <- the client's follow-up; NEVER load/index here (1808)
    Receiving spends nothing; carats arrive as reward_summary_info.add_fcoin.
    """
    client = session.client
    try:
        box = client.call("present/index", dict(_PRESENT_INDEX)).get("data") or {}
    except Exception as exc:  # noqa: BLE001
        log_.info("gifts: skipped (present/index failed: %s)", exc)
        return False
    # receive_all takes at most 100 a press (MEASURED 2026-10-06: boxes of 178-244
    # gave exactly 100 each), so press again - the follow-up present/index is the
    # next round's read - until the box is empty or a press claims nothing.
    took_all = fcoin_all = rounds = left = 0
    refused = False
    while box is not None and rounds < GIFT_ROUNDS:
        # no_receive_present_num is a DICT {no_time_limit_present_num, time_limit_present_num}
        # (18 + 2 = the 20 the capture sent); present_array stops at limit 100, so the
        # two can differ on a big box - a refusal is retried once with the other.
        counts = box.get("no_receive_present_num")
        total = sum(int(v or 0) for v in counts.values()) if isinstance(counts, dict) else 0
        listed = len(box.get("present_array") or [])
        tries = [n for n in dict.fromkeys((total or listed, listed)) if n]
        if not tries:
            break
        got = None
        for n in tries:
            try:
                got = client.call("present/receive_all", {
                    "time_filter_type": 0, "category_filter_type": [0], "is_asc": True,
                    "current_rest_present_num": n})
                break
            except Exception as exc:  # noqa: BLE001
                log_.info("gifts: receive_all refused with count %d (rc %s; box says %d, lists %d)",
                          n, _exc_rc(exc), total, listed)
        if got is None:
            refused = True
            break
        rounds += 1
        d = got.get("data") or {}
        took = int(d.get("receive_present_num") or 0)
        left = int(d.get("rest_present_num") or 0)
        fcoin = int((d.get("reward_summary_info") or {}).get("add_fcoin") or 0)
        _fold_items(session, _added_items(got), fcoin)
        took_all, fcoin_all = took_all + took, fcoin_all + fcoin
        try:
            box = client.call("present/index", dict(_PRESENT_INDEX)).get("data") or {}
        except Exception:  # noqa: BLE001 - the claim already landed
            box = None
        if not took or not left:
            break
    if not rounds:
        if not refused:                  # a refusal was already logged above
            log_.info("gifts: the gift box is empty")
        return True
    log_.info("gifts: claimed %d gift(s) in %d press(es), +%s free carats%s", took_all, rounds,
              f"{fcoin_all:,}", f"; {left} still in the box" if left else "")
    if took_all:
        _ping(label or f"viewer {session.viewer_id}",
              [f"claimed {took_all} gift(s)" + (f" (+{fcoin_all:,} carats)" if fcoin_all else "")],
              title="Gifts")
    return True


#: master.mdb item_data 96 "Daily Race Ticket" - 1 per race, refilled to 3 at the reset.
DAILY_RACE_TICKET = 96
#: Which CLEARED race the multi race runs, first match wins. master.mdb daily_race:
#: 1-4 Moonlight Sho (Monies) and 5-8 Jupiter Cup (Support Points), each Easy,
#: Normal, Hard, Very Hard. Easiest first (a sure 1st place), Jupiter before
#: Moonlight - the owner's own pick in the capture was 5.
DAILY_RACE_IDS = [int(x) for x in (os.environ.get("AIC_DAILY_RACE_IDS") or "5,1,6,2,7,3,8,4"
                                   ).split(",") if x.strip().isdigit()]


def daily_race(session, log_=log, label: str = "") -> bool:
    """Spend every Daily Race Ticket in one MULTI RACE. Never raises.

        daily_race/index {}  -> daily_race_record_array [{daily_race_id, is_cleared, is_played}]
        daily_race_skip/race_skip {daily_race_id, trained_chara_id,
            race_skip_count, client_own_num, running_style}
            -> race_reward_array (one entry per ticket), item_info_array [{96, left}]
    The server decides every race and its rewards. Only a race the account has
    CLEARED can be multi-raced, and clearing one takes a played race whose
    replay_check carries a client-computed result array - not automated (win it
    once by hand). Tickets only: daily_race/recovery_ticket (100 carats) is never
    called, and the count sent is the count held.
    """
    from .state import read_account
    from .veterans import _sort_key
    idx = session.load_index or {}
    try:
        tickets = int(read_account(idx).item_map.get(DAILY_RACE_TICKET, 0))
    except Exception:  # noqa: BLE001
        tickets = 0
    if tickets <= 0:
        log_.info("daily race: no tickets left today")
        return True
    info = idx.get("daily_race_playing_info") or {}
    if info.get("state"):
        log_.info("daily race: a race was left open in the client (state %s) - not touching it",
                  info.get("state"))
        return True
    # load/index already says which races are cleared: no call at all where none is
    known = info.get("daily_race_record_array")
    if isinstance(known, list) and not any(r.get("is_cleared") for r in known if isinstance(r, dict)):
        log_.info("daily race: no race cleared yet - win one once in the client "
                  "(Race > Daily Program > Daily Races); %d ticket(s) left unused", tickets)
        return True
    own = [c for c in idx.get("trained_chara") or []
           if isinstance(c, dict) and c.get("trained_chara_id") and not c.get("owner_viewer_id")]
    if not own:
        log_.info("daily race: no trainee of our own to run")
        return False
    chara = max(own, key=_sort_key)          # highest rank_score, like the owner's pick
    client = session.client
    try:
        recs = (client.call("daily_race/index", {}).get("data") or {}
                ).get("daily_race_record_array") or []
    except Exception as exc:  # noqa: BLE001 - locked (team rank) or unavailable
        log_.info("daily race: skipped (index failed: %s)", exc)
        return False
    cleared = {int(r.get("daily_race_id") or 0) for r in recs if r.get("is_cleared")}
    race = next((i for i in DAILY_RACE_IDS if i in cleared), None)
    if race is None:
        log_.info("daily race: no race cleared yet - win one once in the client "
                  "(Race > Daily Program > Daily Races); %d ticket(s) left unused", tickets)
        return True
    body = {"daily_race_id": race, "trained_chara_id": int(chara["trained_chara_id"]),
            "race_skip_count": tickets, "client_own_num": tickets,
            # the trainee's own stored style, as the client sent in both captures
            "running_style": int(chara.get("running_style") or 0) or best_style(chara)}
    try:
        res = client.call("daily_race_skip/race_skip", body)
    except Exception as exc:  # noqa: BLE001
        # ponytail: the client library retries a 208; a retried skip that had
        # already landed is refused here (count no longer matches) - harmless.
        log_.info("daily race: race_skip refused (rc %s: %s)", _exc_rc(exc), exc)
        return True
    d = res.get("data") or {}
    runs = d.get("race_reward_array") or []
    got: dict[int, int] = {}
    for r in runs:
        for iid, k in _added_items({"data": r}).items():
            got[iid] = got.get(iid, 0) + k
    wins = sum(1 for r in runs if r.get("rank") == 1)
    left = next((i.get("number") for i in d.get("item_info_array") or []
                 if int(i.get("item_id") or 0) == DAILY_RACE_TICKET), "?")
    # the rewards (Monies from Moonlight Sho - the career start sends current_money)
    # and the spent tickets, into this visit's copies of the account
    _fold_items(session, {**got, DAILY_RACE_TICKET: got.get(DAILY_RACE_TICKET, 0) - len(runs)})
    log_.info("daily race: race_skip rc=%s - race %s x%d with trainee %s (score %s): "
              "%d win(s), %s; tickets left %s", _rc(res), race, len(runs),
              chara["trained_chara_id"], chara.get("rank_score"), wins,
              _items_text(got) or "no items", left)
    if runs:
        _ping(label or f"viewer {session.viewer_id}",
              [f"multi race x{len(runs)}: {wins} win(s)" + (f", {_items_text(got)}" if got else "")],
              title="Daily Race")
    return True


# --------------------------------------------------------------------------- #
# dispatch
# --------------------------------------------------------------------------- #
#: name -> (runner, is-due predicate, mark). ``mark(now)`` returns the value
#: stored after a run: a timestamp for periodic chores, a reset-day string for
#: once-a-day ones (so the daily gate compares like-for-like).
_CHORES = {
    # FIRST: what it claims lands in load_index before club (stock) and
    # daily_race (tickets) read it. Gated by AIC_GIFTS in run_due.
    "gifts": (gifts, lambda rec, now: _due_daily(rec, "gifts", now), reset_day),
    "club": (club, lambda rec, now: _due_periodic(rec, "club", CLUB_EVERY_SECONDS, now),
             lambda now: now),
    # Team Trials: gated by AIC_TEAM (+ AIC_TEAM_ONLY for a one-account trial)
    # inside team_trials, so the cadence below is only how often it looks.
    "team": (team_trials,
             lambda rec, now: team_switch() and _due_periodic(rec, "team", TEAM_EVERY_SECONDS, now),
             lambda now: now),
    # daily_legend is PARKED: its bodies have the same one-slot capture shift
    # team_trials had (metadata: race_entry {daily_legend_race_id,
    # trained_chara_id}, reflect_item_effect {item_id_array}, race_start
    # {running_style, is_short}).
    # "legend": (daily_legend, lambda rec, now: _due_daily(rec, "legend", now), reset_day),
    # Every Daily Race Ticket in one multi race on a race already cleared, once a
    # game-day. Gated by AIC_DAILY_RACE in run_due.
    "daily_race": (daily_race, lambda rec, now: _due_daily(rec, "daily_race", now), reset_day),
}


def run_due(session, settings: Settings, target, *, log_=log) -> None:
    """Run every due chore for ``target`` while it is logged in. Never raises."""
    if not enabled():
        return
    only = _only_account()
    tid = str(target.trainer_id)
    if only and tid != only:
        return

    now = time.time()
    state = _load(settings)
    rec = state.setdefault(tid, {})
    changed = False
    for name, (runner, is_due, mark) in _CHORES.items():
        if name == "team" and not team_enabled(tid):
            continue                     # AIC_TEAM_ONLY trial: not this account
        if name == "club" and getattr(target, "club", True) is False:
            continue                     # roster "club": false - not in the farm's club
        if name in ("gifts", "daily_race"):
            if not _switch(name.upper(), tid):
                continue                 # AIC_GIFTS / AIC_DAILY_RACE off, or _ONLY elsewhere
            if getattr(target, "ace_once", 0):
                continue                 # a one-time Ace Run account is the owner's own game
        try:
            if not is_due(rec, now):
                continue
        except Exception:  # noqa: BLE001
            continue
        log_.info("chore '%s' is due for %s", name, target.label)
        try:
            runner(session, log_, target.label)
        except Exception as exc:  # noqa: BLE001 - a chore must never break the visit
            log_.warning("chore '%s' raised for %s (%s); career unaffected",
                         name, target.label, exc)
        rec[name] = mark(now)    # record the attempt either way: a failing chore
        changed = True           # should not re-fire every 60s and hammer the API
    if changed:
        _save(settings, state)
