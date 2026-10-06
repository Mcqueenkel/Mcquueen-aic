"""Static game data, read from the Icarus checkout's ``data/`` directory.

We read Icarus' copies rather than vendoring our own so a data refresh there
(new cards, new characters) is picked up automatically.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

from .logging_setup import get

log = get("gamedata")

#: Support cards for these characters cannot be combined with a Team Sirius
#: card, and a Team Sirius card cannot be used with any of them as trainee.
SIRIUS_MEMBERS = frozenset({
    "mejiromcqueen",
    "riceshower",
    "winningticket",
    "naritabrian",
    "silencesuzuka",
    "specialweek",
})


def normalize_card_name(name) -> str:
    """Collapse a card name to a character identity key.

    ``"Oguri Cap (Christmas)"`` and ``"Oguri Cap"`` both become ``"oguricap"``,
    which is how the game's "no duplicate character" rule is enforced. Support
    card ids carry no character id, so name matching is the only handle we have.
    """
    without_suffix = re.sub(r"\([^)]*\)", "", str(name or ""))
    return re.sub(r"[^a-z0-9]+", "", without_suffix.lower())


class GameData:
    def __init__(self, icarus_root: Path):
        self.root = Path(icarus_root) / "data"
        self.support_map: dict[str, dict] = self._load("support_list.json")
        self.chara_map: dict[str, str] = self._load("chara_list.json")
        self._unknown: set[int] = set()
        self._warned = False

    def _load(self, name: str) -> dict:
        path = self.root / name
        try:
            with open(path, "r", encoding="utf-8-sig") as fh:
                return json.load(fh)
        except (OSError, ValueError) as exc:
            log.warning("could not read %s (%s); name-based checks degraded", path, exc)
            return {}

    # -- support cards ----------------------------------------------------- #

    def support_info(self, support_card_id: int) -> dict:
        return self.support_map.get(str(int(support_card_id)), {})

    def support_name(self, support_card_id: int) -> str:
        return self.support_info(support_card_id).get("name") or f"Support {support_card_id}"

    def support_identity(self, support_card_id: int) -> str:
        """Character key for a support card, or a per-id fallback.

        The fallback is unique per card id so an unknown card is never wrongly
        merged with another card's character. It does mean the "no duplicate
        character" rule cannot be enforced for cards missing from the table -
        hence :meth:`warn_about_unknown`.
        """
        key = normalize_card_name(self.support_info(support_card_id).get("name"))
        if key:
            return key
        self._unknown.add(int(support_card_id))
        return f"#unknown-support-{support_card_id}"

    def support_rarity_rank(self, support_card_id: int) -> int:
        """3 SSR, 2 SR, 1 R, 0 unknown.

        Falls back to the card id, whose leading digit encodes rarity
        (``1xxxx`` R, ``2xxxx`` SR, ``3xxxx`` SSR). That keeps ranking correct
        for cards released since the name table was last refreshed.
        """
        rank = {"SSR": 3, "SR": 2, "R": 1}.get(
            self.support_info(support_card_id).get("rarity"))
        if rank:
            return rank
        self._unknown.add(int(support_card_id))
        return {1: 1, 2: 2, 3: 3}.get(int(support_card_id) // 10000, 0)

    def warn_about_unknown(self) -> None:
        """Report cards missing from the table, once per run.

        Worth surfacing rather than silently degrading: an unknown card cannot
        be character-matched, so it slips past the duplicate-character checks.
        """
        if not self._unknown or self._warned:
            return
        self._warned = True
        log.warning(
            "%d support card(s) are missing from %s: %s. Rarity is inferred "
            "from the card id, but duplicate-character checks cannot cover "
            "them. Refresh the table to fix "
            "(uma-reroll-farm/tools/update_support_list.py rebuilds it from "
            "umapyoi.net).",
            len(self._unknown), self.root / "support_list.json",
            sorted(self._unknown),
        )

    def support_type(self, support_card_id: int) -> str:
        return self.support_info(support_card_id).get("type") or "Unknown"

    def is_sirius(self, support_card_id: int) -> bool:
        return "teamsirius" in normalize_card_name(
            self.support_info(support_card_id).get("name")
        )

    # -- trainees ---------------------------------------------------------- #

    def chara_name(self, card_id: int) -> str:
        return self.chara_map.get(str(int(card_id))) or f"Chara {card_id}"

    def chara_identity(self, card_id: int) -> str:
        """Character key for a trainee card.

        Prefers the name (so "Oguri Cap (Christmas)" matches the support card
        "Oguri Cap"), falling back to the numeric character id embedded in the
        card id, which is always correct for card-vs-card comparisons.
        """
        key = normalize_card_name(self.chara_map.get(str(int(card_id))))
        return key or f"#chara-{int(card_id) // 100}"


#: On-disk cache of the card -> character map, so a machine without the game
#: install keeps the exact identity checks.
_CHARA_MAP_CACHE = Path(__file__).resolve().parent.parent / "state" / "support_card_charas.json"


@lru_cache(maxsize=1)
def support_card_charas() -> dict[int, frozenset]:
    """``support_card_id`` -> the character id(s) that card counts as.

    Read from ``master.mdb``, and worth preferring over name matching wherever
    it is available: it is exact, it covers cards too new for Icarus'
    ``support_list.json`` (whose absence silently disables the duplicate check),
    and ``support_card_group`` gives the handful of cards that count as
    **several** characters at once - 30081 (Team Sirius, 6) and 30067 (3).
    Character ids 9001-9008 are support-only characters that are never
    trainees; they still collide with their own other rarity, which is correct.
    """
    import shutil
    import sqlite3
    import tempfile

    from . import racedata

    def read_cache() -> dict[int, frozenset]:
        try:
            with open(_CHARA_MAP_CACHE, "r", encoding="utf-8") as fh:
                return {int(k): frozenset(v) for k, v in json.load(fh).items()}
        except (OSError, ValueError, TypeError):
            return {}

    try:
        src = racedata.find_master_mdb()
        if not src:
            return read_cache()
        tmp = Path(tempfile.gettempdir()) / "aic_cardcharas.mdb"
        if not tmp.exists() or tmp.stat().st_mtime < src.stat().st_mtime:
            shutil.copy2(src, tmp)
        con = sqlite3.connect(f"file:{tmp}?mode=ro", uri=True)
        try:
            out: dict[int, set] = {}
            for cid, chara in con.execute(
                    "SELECT id, chara_id FROM support_card_data"):
                if chara:
                    out.setdefault(int(cid), set()).add(int(chara))
            for cid, chara in con.execute(
                    "SELECT support_card_id, chara_id FROM support_card_group"):
                out.setdefault(int(cid), set()).add(int(chara))
        finally:
            con.close()
        result = {k: frozenset(v) for k, v in out.items()}
        try:
            _CHARA_MAP_CACHE.parent.mkdir(parents=True, exist_ok=True)
            with open(_CHARA_MAP_CACHE, "w", encoding="utf-8") as fh:
                json.dump({str(k): sorted(v) for k, v in result.items()}, fh)
        except OSError as exc:
            log.debug("could not cache the card->chara map: %s", exc)
        return result
    except Exception as exc:  # noqa: BLE001 - never break a start over this
        log.debug("could not read the card->chara map (%s); using names", exc)
        return read_cache()


@lru_cache(maxsize=4)
def load(icarus_root: str) -> GameData:
    return GameData(Path(icarus_root))
