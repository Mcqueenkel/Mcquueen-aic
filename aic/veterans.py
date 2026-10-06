"""Make room in trained-character storage by dropping a surplus duplicate.

An idle career produces one trained character ("veteran"). Storage is finite -
MEASURED 260 on viewer 319865323652, 2026-09-03 - and once it is full the server
refuses ``idle_single_mode/start`` with result code **2511**. The account then
farms nothing at all until a human deletes something, which on an unattended farm
means it is simply dead until somebody notices.

The rule, chosen by the user: **delete a copy of whichever trainee has the most
duplicates.** On a farm that trains one character over and over that is exactly
the dead weight - MEASURED on the same account, 248 of 260 slots were copies of
one character, and every one of those 248 had ``succession_num`` 0, i.e. had
never been used for anything.

WHAT MAKES THIS SAFE, AND WHY IT IS NOT OPTIONAL
------------------------------------------------
Deletion is permanent and this runs unattended forever, so the candidate filter
is the whole design. Four protection signals live OUTSIDE the character's own row
and none of them were read by this codebase before:

* ``is_locked`` on the row - the only way a user says "never delete this".
* ``trained_chara_favorite_array`` - a sibling array. Identical to the locked set
  on this account, but a capture exists with a favourite that is NOT locked, so
  both are checked.
* ``team_data_array`` - the Team Trials roster, addressed by trained_chara_id.
  Its members are frequently neither locked nor favourited.
* ``single_mode_chara_light`` succession parents, and the parents the pending
  start is about to use.

MEASURED on the live account: of the 248 copies of the most-duplicated character,
**6 were protected** (Team Trials members and favourites). A rule that only
counted duplicates would have deleted a team member on its first run.

Every signal above arrives inside the ``load/index`` payload the farm already
holds, so all of this costs zero extra API calls.

Deliberately NOT done here:

* No capacity check. The server's 2511 is the only trigger, because it is
  authoritative for whatever the cap is on any given account; 260 is a log
  annotation, not a threshold.
* One deletion per call, never a batch. ``trained_chara/remove`` accepts an
  array, but no capture of it exists anywhere in the dumps - the request shape
  comes from a comment - so the blast radius of a mistake is held at one.
  One 50-minute career produces one veteran, so one deletion per career keeps up.
"""

from __future__ import annotations

import time

from .logging_setup import get

log = get("veterans")

#: Never delete twice for the same account inside this window.
#:
#: The bound matters because the caller retries. If a delete succeeds but the
#: retried start still refuses, rotate fails the visit and comes back in
#: RETRY_SECONDS (300s) - which without this would delete another veteran every
#: five minutes, about 96 overnight, while looking like it was fixing something.
#: One career takes ~50 minutes and produces exactly one veteran, so legitimate
#: cleanup never needs to run more than once per account per career; 30 minutes
#: leaves room for a short career without ever licensing a runaway.
MIN_SECONDS_BETWEEN_DELETES = 1800.0

#: viewer_id -> when this process last deleted for it. In memory only: a restart
#: re-arms the guard, which is the safe direction (a restart cannot delete more
#: than one per account before the guard applies again).
_last_delete: dict[int, float] = {}

#: Arrays in ``load/index`` whose entries reference a veteran that is in use.
#: Missing keys are skipped: not every account has every game mode unlocked.
_IN_USE_ARRAYS = (
    "trained_chara_favorite_array",
    "team_data_array",
    "daily_race_playing_info",
    "legend_race_playing_info",
    "room_match_entry_chara_id_array",
)


def _ids(value) -> set[int]:
    """Every trained_chara_id inside a list of rows, or of bare ids."""
    out: set[int] = set()
    if not isinstance(value, list):
        return out
    for entry in value:
        if isinstance(entry, dict):
            raw = entry.get("trained_chara_id")
        elif isinstance(entry, int):
            raw = entry
        else:
            continue
        try:
            n = int(raw or 0)
        except (TypeError, ValueError):
            continue
        if n:
            out.add(n)
    return out


def protected_ids(load_index: dict) -> set[int]:
    """Veterans that must never be deleted, from the home payload alone.

    Errs towards keeping things: an unreadable or absent signal contributes
    nothing to the candidate pool rather than silently making more deletable.
    """
    idx = load_index or {}
    keep: set[int] = set()

    for key in _IN_USE_ARRAYS:
        keep |= _ids(idx.get(key))

    # The character the account is currently training with, if any.
    light = idx.get("single_mode_chara_light") or {}
    for key in ("succession_trained_chara_id_1", "succession_trained_chara_id_2"):
        try:
            n = int(light.get(key) or 0)
        except (TypeError, ValueError):
            n = 0
        if n:
            keep.add(n)

    # The user's own "never delete this" marker.
    for row in idx.get("trained_chara") or []:
        if isinstance(row, dict) and row.get("is_locked"):
            try:
                keep.add(int(row.get("trained_chara_id") or 0))
            except (TypeError, ValueError):
                pass
    keep.discard(0)
    return keep


def _sort_key(row: dict) -> tuple:
    """Worst first: lowest score, then lowest rank, then the oldest id.

    Higher ``rank_score`` is better (selection.py sorts eligible parents
    ``reverse=True`` on it). ``rank`` is a coarse bucket of the same number and
    only ever breaks exact ties.
    """
    def n(key):
        try:
            return int(row.get(key) or 0)
        except (TypeError, ValueError):
            return 0
    return n("rank_score"), n("rank"), n("trained_chara_id")


def pick_victim(load_index: dict, keep: set[int] | None = None) -> dict | None:
    """The one veteran to delete, or ``None`` if nothing may be.

    Groups every owned character by ``card_id // 100`` - the CHARACTER, not the
    exact card, because the game's parent-legality rule is character-level, so
    six alt-outfit copies occupy six slots while offering one usable parent.

    Counting happens over ALL rows and the protection filter is applied only when
    choosing the victim inside a group, so protected copies still make their
    group look big (they do occupy the slots). A group with nothing deletable
    falls through to the next largest rather than being dropped from the count.
    """
    rows = [r for r in (load_index or {}).get("trained_chara") or []
            if isinstance(r, dict) and r.get("trained_chara_id")]
    if not rows:
        return None

    keep = set(keep or ()) | protected_ids(load_index)

    groups: dict[int, list[dict]] = {}
    for row in rows:
        try:
            chara = int(row.get("card_id") or 0) // 100
        except (TypeError, ValueError):
            continue
        groups.setdefault(chara, []).append(row)

    for chara, members in sorted(groups.items(),
                                 key=lambda kv: (-len(kv[1]), kv[0])):
        if len(members) < 2:
            # Never thin a character down to nothing: a single copy is not a
            # duplicate, and the request was to delete duplicates.
            continue
        usable = [r for r in members
                  if int(r.get("trained_chara_id") or 0) not in keep]
        if not usable:
            log.debug("character %s has %s copies but all are protected; "
                      "trying the next group.", chara, len(members))
            continue
        victim = min(usable, key=_sort_key)
        victim = dict(victim)
        victim["_group_size"] = len(members)
        victim["_group_deletable"] = len(usable)
        victim["_chara"] = chara
        return victim
    return None


def make_room(session, keep: set[int] | None = None) -> dict | None:
    """Delete one surplus duplicate. Returns the deleted row, or ``None``.

    Re-reads home first so the decision is made against the server's current
    roster rather than a view built earlier in the visit, and verifies the
    deletion by re-reading rather than by trusting the response body - no
    capture of ``trained_chara/remove``'s response exists to parse against.
    """
    index = session.refresh_home() or {}

    viewer = int((index.get("user_info") or {}).get("viewer_id") or 0)
    last = _last_delete.get(viewer, 0.0)
    waited = time.time() - last
    if last and waited < MIN_SECONDS_BETWEEN_DELETES:
        log.error("Storage is full again only %.0f min after the last deletion "
                  "for %s. Refusing to delete another - deleting one per retry "
                  "would work through the collection. Something other than "
                  "capacity is refusing the start.", waited / 60, viewer)
        return None

    victim = pick_victim(index, keep)
    if victim is None:
        held = len((index or {}).get("trained_chara") or [])
        log.error("Storage is full (%s veterans) but nothing may be deleted: "
                  "every duplicate is locked, favourited, on the Team Trials "
                  "roster, or in use. Free a slot in the game client.", held)
        return None

    vid = int(victim["trained_chara_id"])
    log.warning("Trained-character storage is full. Deleting 1 of the %s copies "
                "of character %s: id %s, rank %s, score %s, registered %s. "
                "(%s of that group are deletable.)",
                victim.get("_group_size"), victim.get("_chara"), vid,
                victim.get("rank"), victim.get("rank_score"),
                victim.get("register_time") or victim.get("create_time") or "?",
                victim.get("_group_deletable"))

    try:
        # retry_208/205 OFF: this write is irreversible, and a retry of a call
        # the server already applied would delete a SECOND veteran. Success is
        # confirmed below by re-reading, so a lost reply costs nothing.
        session.client.call("trained_chara/remove",
                            {"trained_chara_id_array": [vid]},
                            retry_208=0, retry_205=0)
    except Exception as exc:  # noqa: BLE001 - verified by re-read either way
        log.warning("trained_chara/remove raised (%s); checking whether it "
                    "applied anyway.", type(exc).__name__)

    after = session.refresh_home() or {}
    still = {int(r.get("trained_chara_id") or 0)
             for r in (after.get("trained_chara") or []) if isinstance(r, dict)}
    if vid in still:
        log.error("Veteran %s is still present after the delete; storage was "
                  "not freed.", vid)
        return None
    _last_delete[viewer] = time.time()
    log.info("Deleted veteran %s. %s veterans remain.", vid, len(still))
    return victim
