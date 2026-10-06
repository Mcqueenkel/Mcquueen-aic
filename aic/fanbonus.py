"""Rank owned support cards by their Fan Bonus, honouring deck-legality.

Fan bonus values (accounting for level and uncap) come from
:mod:`aic.supportdata`; character identity and the "no duplicate uma" / "support
uma != trainee uma" rules come from :mod:`aic.gamedata` - the same identity
helpers the deck builder already uses, so this ranking and an actual deck agree.
"""

from __future__ import annotations

from dataclasses import dataclass

from .gamedata import GameData, SIRIUS_MEMBERS
from .selection import DECK_SIZE, SelectionError, deck_conflict
from .state import AccountState
from .supportdata import SupportData


@dataclass
class FanCard:
    card_id: int
    name: str
    identity: str
    rarity_rank: int
    level: int
    limit_break_count: int
    fan_pct: float
    eligible: bool = True
    skip_reason: str = ""


def rank_owned(account: AccountState, gamedata: GameData, support: SupportData,
               trainee_card_id: int = 0) -> list[FanCard]:
    """Owned support cards sorted best-fan-bonus first, with eligibility flags.

    Eligibility mirrors what a deck allows: a card whose character is the
    trainee's is barred, and among cards of the same character only the strongest
    stays eligible (a deck cannot hold two of the same uma). Ineligible cards are
    still returned - ranked and labelled - so the report shows everything owned.
    """
    trainee_key = gamedata.chara_identity(trainee_card_id) if trainee_card_id else None
    # Exact identity from master.mdb when available, so this agrees with
    # deck_conflict (which now works in character-id space). Without it the
    # ranking could call two cards distinct that the deck check then rejects.
    from .gamedata import support_card_charas
    chara_map = support_card_charas()
    trainee_chara = int(trainee_card_id or 0) // 100

    cards: list[FanCard] = []
    for owned in account.owned_support_cards:
        sid = int(owned.get("id") or 0)
        if not sid:
            continue
        result = support.card_fan_bonus(sid, owned.get("exp") or 0,
                                        owned.get("limit_break_count") or 0)
        if result is None:
            level = support.level_for(
                gamedata.support_rarity_rank(sid),  # rank == rarity int here
                owned.get("exp") or 0, owned.get("limit_break_count") or 0)
            fan_pct = 0.0
        else:
            level, fan_pct = result
        cards.append(FanCard(
            card_id=sid,
            name=gamedata.support_name(sid),
            identity=gamedata.support_identity(sid),
            rarity_rank=gamedata.support_rarity_rank(sid),
            level=level,
            limit_break_count=int(owned.get("limit_break_count") or 0),
            fan_pct=round(fan_pct, 2),
        ))

    # Best first: fan bonus, then rarity, then level as tie-breakers.
    cards.sort(key=lambda c: (c.fan_pct, c.rarity_rank, c.level), reverse=True)

    seen_identity: set[str] = set()
    claimed_charas: set[int] = set()
    for card in cards:
        charas = chara_map.get(card.card_id)
        if charas and trainee_chara and trainee_chara in charas:
            card.eligible = False
            card.skip_reason = "same uma as trainee"
        elif charas and (charas & claimed_charas):
            card.eligible = False
            card.skip_reason = "duplicate uma (weaker copy)"
        elif trainee_key and not charas and card.identity == trainee_key:
            card.eligible = False
            card.skip_reason = "same uma as trainee"
        elif not charas and card.identity in seen_identity:
            card.eligible = False
            card.skip_reason = "duplicate uma (weaker copy)"
        else:
            seen_identity.add(card.identity)
            claimed_charas |= charas or frozenset()
    return cards


def top_eligible(ranked: list[FanCard], count: int = DECK_SIZE) -> list[FanCard]:
    """The strongest ``count`` deck-eligible cards from a ranked list."""
    return [c for c in ranked if c.eligible][:count]


def build_fan_deck(account: AccountState, gamedata: GameData, support: SupportData,
                   trainee_card_id: int, size: int = DECK_SIZE,
                   reserve=()) -> tuple[list[int], list[FanCard]]:
    """The ``size`` highest-fan-bonus owned cards that form a legal deck.

    ``reserve`` are cards that will join the career on top of the deck - the
    borrowed card an Ace Run insists on - so nothing that clashes with
    them (same character: an owned Light Hello vs a borrowed one) is taken.

    Returns ``(support_card_ids, chosen)``. The uma-dedup and trainee-exclusion
    are already applied by :func:`rank_owned`; this adds the Team Sirius
    incompatibility rule (the one remaining deck constraint) while walking the
    ranking best-first, and validates the result with the same ``deck_conflict``
    the normal deck path uses.
    """
    ranked = rank_owned(account, gamedata, support, trainee_card_id)

    deck: list[int] = []
    chosen: list[FanCard] = []
    for card in ranked:
        if not card.eligible:
            continue
        # Validate each candidate against the deck so far and simply move on
        # when it does not fit, rather than assembling a deck and rejecting the
        # whole thing at the end. That covers every rule deck_conflict knows -
        # duplicate character (including cards that count as several), the
        # trainee's own uma, Team Sirius - without duplicating any of them here,
        # and it is what keeps a career startable instead of erroring out.
        if deck_conflict(deck + [card.card_id] + list(reserve), trainee_card_id, gamedata):
            continue
        deck.append(card.card_id)
        chosen.append(card)
        if len(deck) == size:
            break

    if len(deck) < size:
        raise SelectionError(
            f"only {len(deck)} of the owned support cards can form a legal deck; "
            f"need {size}")
    return deck, chosen


def friend_fan_score(gamedata: GameData, support: SupportData,
                     support_card_id: int, limit_break_count: int) -> float:
    """Fan bonus of a *borrowable* card, at the max level its uncap allows.

    Friend offers carry no exp, only ``limit_break_count``, so the best estimate
    is the value at that uncap's level cap - which is also how a borrowed card is
    usually maxed. Cards with no fan bonus score 0.
    """
    rarity = gamedata.support_rarity_rank(support_card_id)
    max_level = support.max_level(rarity, limit_break_count)
    pct = support.fan_bonus_at_level(support_card_id, max_level)
    return float(pct or 0.0)
