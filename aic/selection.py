"""Choosing what to run a career with: trainee, succession parents, deck.

Everything here enforces the game's "no duplicate character" rules client-side.
The server rejects a bad combination with result code 102, which is expensive
(and, for the succession-rank-point path, ambiguous), so it is worth catching.
"""

from __future__ import annotations

import itertools
import random
import re
from dataclasses import dataclass, field
from typing import Sequence

from . import gamedata as gd_module
from .gamedata import GameData, SIRIUS_MEMBERS
from .logging_setup import get
from .state import AccountState

log = get("selection")

DECK_SIZE = 5

#: Characters that cannot start an idle career, by ``chara_id``.
#:
#: **1052 (Haru Urara).** Her route in ``single_mode_route_race`` has no named
#: race in the classic year - those goals are *fan-count* objectives
#: (5 000 / 9 000 / 12 000) rather than programs - so ``route_targets(1052)``
#: yields ``y1=1, y2=0, y3=4``. Every start on record with an empty year 2 was
#: refused, and she is 0-for-7 across three accounts, two code generations, five
#: different succession rank points (55/124/146/148/154/185) and race plans of
#: both 5 and 36 races, while every other trainee tried has succeeded.
#:
#: Skipped during automatic selection and refused when named explicitly, so a
#: farm run does not spend an account on a start that cannot be accepted. Set
#: ``AIC_ALLOW_TRAINEE=1052`` to override and try anyway.
UNSTARTABLE_CHARAS = {
    1052: "its route has no classic-year race (fan-count objectives instead), "
          "and no start with an empty year 2 has ever been accepted",
}


def _allowed_overrides() -> set[int]:
    import os

    out = set()
    for part in (os.environ.get("AIC_ALLOW_TRAINEE") or "").replace(",", " ").split():
        try:
            out.add(int(part))
        except ValueError:
            continue
    return out


def unstartable_reason(card_id: int) -> str:
    """Why this trainee cannot start a career, or ``""``."""
    chara = int(card_id or 0) // 100
    if chara in _allowed_overrides():
        return ""
    return UNSTARTABLE_CHARAS.get(chara, "")


class SelectionError(RuntimeError):
    """No valid combination could be assembled from what the account owns."""


@dataclass
class Parent:
    trained_chara_id: int
    card_id: int
    rank: int = 0
    rank_score: int = 0
    scenario_id: int = 0
    is_locked: bool = False
    #: Its own parents, needed to score succession affinity.
    succession_chara_array: list = field(default_factory=list)
    win_saddle_id_array: list = field(default_factory=list)
    #: trained_chara_ids of its own two parents.
    parent_ids: tuple = ()

    @property
    def chara_id(self) -> int:
        return self.card_id // 100

    @property
    def has_lineage(self) -> bool:
        """Whether this character has grandparents recorded.

        Decisive for whether the succession rank point can be computed exactly.
        See :func:`pick_parents`.
        """
        return bool(self.succession_chara_array)

    def as_family_node(self) -> dict:
        """The shape :mod:`aic.succession` scores."""
        return {
            "card_id": self.card_id,
            "succession_chara_array": self.succession_chara_array,
            "win_saddle_id_array": self.win_saddle_id_array,
        }


@dataclass
class StartSelection:
    card_id: int
    support_card_ids: list[int]
    parent_id_1: int
    parent_id_2: int
    deck_id: int
    friend_viewer_id: int = 0
    friend_card_id: int = 0
    #: Idle careers need a fixed running style up front (1 front / 2 pace /
    #: 3 late / 4 end) because no one is there to pick one per race.
    running_style: int = 1
    #: Affinity total for this trainee/parent pair - see :mod:`aic.succession`.
    #: This is the real client's 7-link formula (trainee vs its two direct
    #: parents, plus the parent tree), not the 5-link parent-only figure.
    succession_rank_point: int = 0
    #: The 5-link parent tree alone, with no trainee; tried as a cheap
    #: secondary candidate if the first is refused. Zero when it matches the
    #: primary value, so it adds no extra call.
    succession_parent_only: int = 0
    #: Identifies this tree by its characters rather than by account-local
    #: ids, so an offset learned on one farmed account serves every other
    #: account holding the same lineage - see :func:`aic.succession.tree_signature`.
    succession_signature: str = ""
    #: Per-link breakdown, logged only when the server refuses.
    succession_detail: list = field(default_factory=list)
    deck_source: str = ""
    notes: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# parents
# --------------------------------------------------------------------------- #

def read_parents(load_index: dict) -> list[Parent]:
    """Owned trained characters, usable as succession parents."""
    parents = []
    for row in load_index.get("trained_chara") or []:
        try:
            tcid = int(row.get("trained_chara_id") or 0)
            card_id = int(row.get("card_id") or 0)
        except (TypeError, ValueError):
            continue
        if not tcid or not card_id:
            continue
        # Borrowed/guest entries carry an owner; only our own are freely usable.
        if int(row.get("owner_viewer_id") or 0):
            continue
        parents.append(Parent(
            trained_chara_id=tcid,
            card_id=card_id,
            rank=int(row.get("rank") or 0),
            rank_score=int(row.get("rank_score") or 0),
            scenario_id=int(row.get("scenario_id") or 0),
            is_locked=bool(row.get("is_locked")),
            succession_chara_array=list(row.get("succession_chara_array") or []),
            win_saddle_id_array=list(row.get("win_saddle_id_array") or []),
            parent_ids=tuple(
                int(row.get(f"succession_trained_chara_id_{n}") or 0)
                for n in (1, 2)),
        ))
    return parents


def pick_friend_card(pre_single_mode: dict, support_card_ids: Sequence[int],
                     trainee_card_id: int, gamedata: GameData,
                     rng: random.Random, fan_scorer=None,
                     only_card_id: int = 0) -> tuple[int, int]:
    """The borrowed sixth support card: ``(viewer_id, support_card_id)``.

    A career runs six support cards - five from your deck plus one borrowed from
    another trainer - and the game will not start without the sixth. Candidates
    come from ``pre_single_mode/index``; the character rules apply to it exactly
    as they do to the deck.

    ``fan_scorer(support_card_id, limit_break_count) -> float`` optionally ranks
    the eligible offers by fan bonus (used by the auto fan-bonus deck) instead of
    the default rarity/uncap ordering.

    ``only_card_id`` takes that card and nothing else (highest uncap first),
    returning ``(0, 0)`` quietly when it is not on offer so the caller can
    refresh the list and look again.

    Returns ``(0, 0)`` if nothing eligible is on offer, which lets the caller
    proceed and let the server have the final say.
    """
    offers = ((pre_single_mode.get("friend_support_card_data") or {})
              .get("support_card_data_array") or [])
    if only_card_id:
        offers = [o for o in offers if int(o.get("support_card_id") or 0) == int(only_card_id)]
        if not offers:
            return 0, 0
    if not offers:
        log.warning("pre_single_mode listed no borrowable support cards.")
        return 0, 0

    # Judge the borrowed card with deck_conflict, the SAME check the deck itself
    # passes, rather than a name-derived key of its own.
    #
    # The old code compared gamedata.support_identity(sid) - one key per card.
    # That is blind to a GROUP card, which counts as several characters at once:
    # 30081 counts as charas 1001, 1002, 1013, 1016, 1030 and 1035, and 30067 as
    # 1003, 1017 and 1073. Borrowing one of those alongside a deck already
    # holding any of those characters builds an illegal six-card set, and the
    # server refuses the start. deck_conflict reads the exact
    # support_card_id -> chara_id map from master.mdb and already covers
    # duplicate characters, the trainee's own uma and Team Sirius, so this both
    # fixes the hole and deletes three rules that were duplicated here.
    deck = list(support_card_ids)

    eligible = []
    for offer in offers:
        sid = int(offer.get("support_card_id") or 0)
        viewer = int(offer.get("viewer_id") or 0)
        if not sid or not viewer:
            continue
        if deck_conflict(deck + [sid], trainee_card_id, gamedata):
            continue
        eligible.append((sid, viewer, int(offer.get("limit_break_count") or 0)))

    if not eligible:
        if only_card_id:
            log.warning("Support card %s is on offer but clashes with this deck or "
                        "trainee (same character).", only_card_id)
            return 0, 0
        log.warning("No borrowable support card is compatible with this deck "
                    "and trainee; starting without one.")
        return 0, 0

    # Best available. Shuffle first so equal-ranked offers are not always the
    # same trainer's. With a fan scorer, rank by fan bonus (then rarity/LB);
    # otherwise the default rarity-then-limit-break ordering.
    rng.shuffle(eligible)
    if fan_scorer is not None:
        key = lambda row: (fan_scorer(row[0], row[2]),
                           gamedata.support_rarity_rank(row[0]), row[2])
    else:
        key = lambda row: (gamedata.support_rarity_rank(row[0]), row[2])
    sid, viewer, breaks = max(eligible, key=key)
    log.info("Borrowed card: %s (LB%s) from viewer %s",
             gamedata.support_name(sid), breaks, viewer)
    return viewer, sid


def resolve_trainee_label(label: str, owned_card_ids, gamedata) -> tuple[int, str]:
    """A written trainee name (``"Oguri_alt"``) -> a card id this account OWNS.

    The roster carries what the operator wrote, not a card id, because a card id
    is per-account and unmemorable. Matching is on the card's display name, so
    ``"Oguri_alt"`` finds ``"Oguri Cap (Alt)"`` - every word of the label must
    appear in the name. Ported from Icarus' ``resolve_trainee``, which solves the
    same problem for its sweep.

    Returns ``(card_id, note)``. **card_id 0 means "could not resolve"**, and the
    caller must fall back to the automatic pick rather than guess: picking the
    wrong trainee silently spends a whole career on the wrong build.
    """
    words = [w for w in re.split(r"[^a-z0-9]+", str(label or "").lower()) if w]
    if not words:
        return 0, ""
    names = {int(c): str(gamedata.chara_name(int(c))).lower() for c in owned_card_ids}
    hits = [c for c, n in names.items() if all(w in n for w in words)]

    if len(hits) == 1:
        return hits[0], ""
    if not hits:
        return 0, f"no owned trainee matches {label!r}"

    # "Oguri" matches both "Oguri Cap" and "Oguri Cap (Alt)". When the label does
    # NOT say alt, the plain card is what was meant - otherwise every base name
    # would be permanently ambiguous on an account holding both.
    if "alt" not in words:
        plain = [c for c in hits if "(alt)" not in names[c]]
        if len(plain) == 1:
            return plain[0], f"{label!r} -> {gamedata.chara_name(plain[0])}"
    return 0, (f"ambiguous {label!r}: "
               + ", ".join(gamedata.chara_name(c) for c in hits[:4]))


def resolve_parents(parents: Sequence[Parent], spec: str) -> tuple[Parent, Parent]:
    """Look up an explicit ``"id1,id2"`` pair of trained_chara_ids.

    Deliberately skips the character-conflict checks: this exists to reproduce a
    known-good combination exactly, so the server gets the final say.
    """
    try:
        ids = [int(part) for part in str(spec).replace(" ", "").split(",") if part]
    except ValueError as exc:
        raise SelectionError(f"--parents must be two ids, e.g. 1980,1950 (got {spec!r})") from exc
    if len(ids) != 2:
        raise SelectionError(f"--parents needs exactly two ids (got {spec!r})")

    by_id = {p.trained_chara_id: p for p in parents}
    missing = [i for i in ids if i not in by_id]
    if missing:
        raise SelectionError(
            f"trained_chara_id(s) {missing} are not owned by this account "
            f"(it has {len(parents)} usable parents)"
        )
    return by_id[ids[0]], by_id[ids[1]]


def pick_parents(parents: Sequence[Parent], trainee_card_id: int,
                 gamedata: GameData,
                 preferred_pairs: set | None = None) -> tuple[Parent, Parent]:
    """Best-ranked pair of parents that is legal for this trainee.

    Legality: neither parent may be the same character as the trainee, and the
    two parents may not be the same character as each other. Rank is only a
    tiebreak - any legal pair works - but picking the strongest costs nothing.

    ``preferred_pairs`` (``{(id1, id2), ...}``) wins over rank. It exists for
    the rotation: the server validates a succession rank point we can only
    compute approximately, and finding the right one costs a bounded search
    *per pair*. Every retirement adds a stronger character, so ranking alone
    would pick a new pair each cycle and pay that search every time. Sticking
    to a pair whose offset is already known makes the start a single call.
    """
    if len(parents) < 2:
        raise SelectionError(
            f"need at least 2 trained characters to start a career, found {len(parents)}"
        )

    trainee_key = gamedata.chara_identity(trainee_card_id)
    eligible = [p for p in parents if gamedata.chara_identity(p.card_id) != trainee_key]
    if len(eligible) < 2:
        raise SelectionError(
            f"only {len(eligible)} trained characters differ from the trainee "
            f"{gamedata.chara_name(trainee_card_id)}; pick a different trainee"
        )

    eligible.sort(key=lambda p: (p.rank_score, p.rank), reverse=True)

    def lineage(p: Parent) -> set[int]:
        """The trained characters this parent contributes to the tree."""
        return {p.trained_chara_id} | {i for i in p.parent_ids if i}

    def related(a: Parent, b: Parent) -> bool:
        """Whether the two share a trained character in their trees.

        A **preference**, not a rule. Overlapping ancestry (a shared
        grandparent, or one being the other's parent) is ordinary pedigree
        building and the game allows it - so this only breaks ties toward more
        varied lineage, and never rejects a pair outright. Treating it as a
        hard constraint was wrong and could leave a small pool with no pair at
        all.
        """
        return bool(lineage(a) & lineage(b))

    def legal(a: Parent, b: Parent) -> bool:
        """The one rule the game actually documents: not the same character."""
        return gamedata.chara_identity(a.card_id) != gamedata.chara_identity(b.card_id)

    if preferred_pairs:
        by_id = {p.trained_chara_id: p for p in eligible}
        for a_id, b_id in preferred_pairs:
            a, b = by_id.get(a_id), by_id.get(b_id)
            if a and b and legal(a, b):
                log.info("Reusing a parent pair with a known succession offset.")
                return a, b

    # All pairs in rank order, not just pairs containing the top-ranked one:
    # the strongest parent is often the one just retired, which is related to
    # every other good candidate.
    pairs = [(a, b) for a, b in itertools.combinations(eligible, 2) if legal(a, b)]
    if not pairs:
        raise SelectionError(
            f"no legal parent pair among {len(eligible)} candidates - every "
            "combination is the same character. Pass --parents explicitly."
        )

    # Parents WITHOUT grandparents come first, and this matters far more than
    # rank. The succession rank point the server validates is scored over five
    # links in the family tree; with no grandparents only one of them (the
    # parent-to-parent link) can contribute, so the value is exact and the start
    # is accepted on the first call. Add grandparents and the other four kick in
    # - and the formula is measurably wrong there (209 computed against 295 the
    # real client sent), which costs a bounded search or an outright refusal.
    #
    # This is what made starts look random: a fresh account's starter characters
    # have no lineage and always work, while every career adds a higher-ranked
    # character that does have lineage, so ranking alone drifts onto the broken
    # case as the rotation goes on. Parent strength is irrelevant when farming
    # fans, so exactness wins.
    # Kept because it costs nothing, but do not expect it to fire: every
    # trained character the game hands out already carries a full six-entry
    # succession_chara_array, including the starters on a brand-new account.
    # Checked across four accounts (6 to 259 characters): zero without lineage.
    # So the rank point is always an estimate, and the search always matters.
    clean = [(a, b) for a, b in pairs if not a.has_lineage and not b.has_lineage]
    if clean:
        candidates = clean
        log.info("Using parents with no grandparents: the succession rank point "
                 "is exact for those.")
    else:
        # Every owned character carries a family tree (verified across accounts
        # of 6 to 259 characters), so the rank point is always an estimate and
        # the start needs a short search. The estimate's error is a small
        # undercount of shared G1 wins that grows with how many G1s the parents
        # carry - so the fewer wins a pair has, the closer the estimate and the
        # faster the search. Parent strength buys nothing when farming fans, so
        # order the pairs by fewest combined wins: the cheapest to start wins.
        pairs.sort(key=lambda ab: (len(ab[0].win_saddle_id_array)
                                   + len(ab[1].win_saddle_id_array),
                                   -ab[0].rank_score - ab[1].rank_score))
        candidates = pairs
        log.debug("Every owned character carries a family tree; ordering by "
                  "fewest wins so the rank-point search is shortest.")

    # Within the chosen group, still prefer varied lineage; never require it.
    for a, b in candidates:
        if not related(a, b):
            return a, b
    return candidates[0]


# --------------------------------------------------------------------------- #
# trainee
# --------------------------------------------------------------------------- #

def pick_trainee(account: AccountState, parents: Sequence[Parent],
                 gamedata: GameData, rng: random.Random,
                 preferred_card_id: int = 0) -> int:
    """A trainee the account owns that at least two parents differ from.

    Preferring a character absent from the top of the parent list keeps
    :func:`pick_parents` from having to discard the strongest candidates.
    """
    owned = [cid for cid in account.owned_card_ids if cid]
    if not owned:
        raise SelectionError("account owns no trainee cards (card_list was empty)")

    if preferred_card_id:
        if preferred_card_id not in owned:
            raise SelectionError(
                f"card_id {preferred_card_id} is not owned by this account"
            )
        blocked = unstartable_reason(preferred_card_id)
        if blocked:
            raise SelectionError(
                f"{gamedata.chara_name(preferred_card_id)} (card "
                f"{preferred_card_id}) cannot start an idle career: {blocked}.\n"
                f"  Pick another trainee, or set AIC_ALLOW_TRAINEE="
                f"{preferred_card_id // 100} to try anyway."
            )
        return preferred_card_id

    top_parent_keys = {
        gamedata.chara_identity(p.card_id)
        for p in sorted(parents, key=lambda p: p.rank_score, reverse=True)[:2]
    }

    def usable(card_id: int) -> bool:
        key = gamedata.chara_identity(card_id)
        distinct = {
            gamedata.chara_identity(p.card_id)
            for p in parents
            if gamedata.chara_identity(p.card_id) != key
        }
        return len(distinct) >= 2

    startable = [c for c in owned if not unstartable_reason(c)]
    skipped = len(owned) - len(startable)
    if skipped:
        log.debug("skipping %s trainee card(s) that cannot start a career", skipped)

    candidates = [c for c in startable if usable(c)]
    if not candidates:
        raise SelectionError(
            "no owned trainee leaves two differently-charactered parents available"
            + (f" (and {skipped} were skipped as unable to start a career)"
               if skipped else "")
        )

    preferred = [c for c in candidates
                 if gamedata.chara_identity(c) not in top_parent_keys]
    return rng.choice(preferred or candidates)


# --------------------------------------------------------------------------- #
# deck
# --------------------------------------------------------------------------- #

def _deck_conflict_by_chara(support_card_ids: Sequence[int], trainee_card_id: int,
                            gamedata: GameData, chara_map: dict) -> str | None:
    """The duplicate-character rule in character-id space (exact)."""
    trainee_chara = int(trainee_card_id or 0) // 100
    claimed: dict[int, int] = {}
    for sid in support_card_ids:
        charas = chara_map.get(int(sid))
        if not charas:
            continue
        for chara in charas:
            if chara in claimed and claimed[chara] != int(sid):
                return (f"duplicate character: {gamedata.support_name(sid)} and "
                        f"{gamedata.support_name(claimed[chara])} both count as "
                        f"character {chara}")
            claimed[chara] = int(sid)
        if trainee_chara and trainee_chara in charas:
            return (f"deck contains {gamedata.support_name(sid)}, the same "
                    f"character as trainee {gamedata.chara_name(trainee_card_id)}")
    return None


def deck_conflict(support_card_ids: Sequence[int], trainee_card_id: int,
                  gamedata: GameData) -> str | None:
    """Reason this deck+trainee combination is illegal, or ``None``.

    Prefers the exact ``support_card_id -> chara_id`` map from master.mdb, which
    subsumes the name matching *and* the hardcoded Team Sirius rule: a card that
    counts as several characters (30081, 30067) simply carries several ids, so
    one intersection test covers every case. Falls back to the name-based checks
    below when the map is unavailable.
    """
    chara_map = gd_module.support_card_charas()
    if chara_map:
        conflict = _deck_conflict_by_chara(support_card_ids, trainee_card_id,
                                           gamedata, chara_map)
        # A card missing from the map still needs the name-based check, so only
        # a positive result short-circuits.
        if conflict:
            return conflict

    seen: dict[str, int] = {}
    has_sirius = False
    for sid in support_card_ids:
        key = gamedata.support_identity(sid)
        if key in seen:
            return (f"duplicate character: {gamedata.support_name(sid)} and "
                    f"{gamedata.support_name(seen[key])}")
        seen[key] = sid
        if gamedata.is_sirius(sid):
            has_sirius = True

    trainee_key = gamedata.chara_identity(trainee_card_id)
    if trainee_key in seen:
        return (f"deck contains {gamedata.support_name(seen[trainee_key])}, the same "
                f"character as trainee {gamedata.chara_name(trainee_card_id)}")

    if has_sirius:
        if trainee_key in SIRIUS_MEMBERS:
            return f"trainee {gamedata.chara_name(trainee_card_id)} conflicts with Team Sirius"
        for key, sid in seen.items():
            if key in SIRIUS_MEMBERS:
                return f"{gamedata.support_name(sid)} conflicts with Team Sirius"
    return None


def build_best_deck(account: AccountState, trainee_card_id: int,
                    gamedata: GameData, rng: random.Random,
                    settings=None) -> tuple[list[int], str]:
    """The deck to use when none of the saved ones is usable.

    Built from the highest **Fan Bonus** cards the account owns (level and uncap
    applied) - fans are the point of a farming career, and it costs nothing over
    picking at random. Falls back to the rarity-weighted random build when the
    fan-bonus data is unavailable (no master.mdb and no cache).
    """
    if settings is not None:
        try:
            from . import fanbonus, supportdata

            support = supportdata.load(settings)
            deck, chosen = fanbonus.build_fan_deck(
                account, gamedata, support, trainee_card_id)
            total = sum(c.fan_pct for c in chosen)
            return deck, f"built from fan bonus ({total:.0f}% total)"
        except Exception as exc:  # noqa: BLE001 - never block a start on this
            log.debug("fan-bonus deck unavailable (%s); building at random", exc)
    return build_random_deck(account, trainee_card_id, gamedata, rng), "randomly built"


def choose_deck(account: AccountState, trainee_card_id: int, gamedata: GameData,
                rng: random.Random, deck_id: int = 0,
                settings=None) -> tuple[int, list[int], str]:
    """Use an existing saved deck if one is valid, else build a random one.

    Returns ``(deck_id, support_card_ids, source)``. A built deck is written to
    the account's deck slot by the caller so ``select_deck_id`` on start refers
    to something real - the server reads the deck from its own state, not from
    the ``support_card_ids`` we send.
    """
    decks = {d["deck_id"]: d for d in account.support_card_decks}

    if deck_id:
        deck = decks.get(deck_id)
        if not deck:
            raise SelectionError(f"deck {deck_id} does not exist on this account")
        conflict = deck_conflict(deck["support_card_ids"], trainee_card_id, gamedata)
        if conflict:
            raise SelectionError(f"deck {deck_id} is not usable: {conflict}")
        return deck_id, list(deck["support_card_ids"]), f"saved deck {deck_id}"

    # Preference order: the deck the account last used, then any other saved
    # deck, then a freshly rolled one.
    order = []
    if account.last_selected_deck_id in decks:
        order.append(decks[account.last_selected_deck_id])
    order += [d for d in account.support_card_decks
              if d["deck_id"] != account.last_selected_deck_id]

    for deck in order:
        ids = deck["support_card_ids"]
        if len(ids) != DECK_SIZE:
            continue
        if not all(sid in set(account.owned_support_card_ids) for sid in ids):
            log.debug("deck %s references unowned cards; skipping", deck["deck_id"])
            continue
        conflict = deck_conflict(ids, trainee_card_id, gamedata)
        if conflict:
            log.debug("deck %s unusable (%s); trying the next one",
                      deck["deck_id"], conflict)
            continue
        return deck["deck_id"], list(ids), f"saved deck {deck['deck_id']} ({deck['name']})"

    built, source = build_best_deck(account, trainee_card_id, gamedata, rng,
                                    settings=settings)
    target = account.last_selected_deck_id or (
        account.support_card_decks[0]["deck_id"] if account.support_card_decks else 1
    )
    return target, built, source


def build_random_deck(account: AccountState, trainee_card_id: int,
                      gamedata: GameData, rng: random.Random) -> list[int]:
    """Five owned support cards, all different characters, none the trainee's.

    Selection is random among eligible cards as asked, but weighted toward
    higher rarity so the resulting career is not actively bad.
    """
    trainee_key = gamedata.chara_identity(trainee_card_id)
    pool = [sid for sid in dict.fromkeys(account.owned_support_card_ids)
            if gamedata.support_identity(sid) != trainee_key]
    if len(pool) < DECK_SIZE:
        raise SelectionError(
            f"only {len(pool)} eligible support cards owned; need {DECK_SIZE}"
        )

    # Shuffle first so ties among equal-rarity cards are genuinely random, then
    # a stable sort by rarity keeps that randomness within each rarity band.
    rng.shuffle(pool)
    pool.sort(key=gamedata.support_rarity_rank, reverse=True)

    deck: list[int] = []
    for sid in pool:
        # Ask deck_conflict about each candidate against the deck so far and
        # skip the ones that do not fit. Filtering by name alone used to build a
        # deck that the (stricter, character-id based) check then rejected
        # outright - one unrecognised card, e.g. a group card counting as
        # several umas, and the whole career start failed.
        if deck_conflict(deck + [sid], trainee_card_id, gamedata):
            continue
        deck.append(sid)
        if len(deck) == DECK_SIZE:
            break

    if len(deck) < DECK_SIZE:
        raise SelectionError(
            f"could not assemble {DECK_SIZE} distinct-character support cards "
            f"(got {len(deck)})"
        )
    return deck


def describe_deck(support_card_ids: Sequence[int], gamedata: GameData) -> str:
    return ", ".join(
        f"{gamedata.support_name(sid)} [{gamedata.support_type(sid)}/"
        f"{gamedata.support_info(sid).get('rarity', '?')}]"
        for sid in support_card_ids
    )
