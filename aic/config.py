"""Paths, profile resolution and the on-disk settings file.

Nothing in here imports the vendored client - ``bootstrap`` needs the resolved
paths *before* those imports can happen, so this module must stay dependency
free.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SETTINGS_PATH = PROJECT_ROOT / "settings.json"

#: Game data (data/*.json, node_modules/steam-user) when AIC_DATA_ROOT is unset.
DEFAULT_DATA_ROOT = PROJECT_ROOT / "gamedata"

#: Item ids read out of ``load/index``'s ``item_list``. These are the only ones
#: the start flow needs; item 75 in particular is the succession rank point
#: balance the server validates ``current_succession_rank_point`` against.
ITEM_GOLD = 59
ITEM_SUCCESSION_RANK_POINT = 75
ITEM_CLOCK = 95
#: TP restore items, biggest first, mapped to what each one actually gives.
#:
#: FALLBACK ONLY - :func:`aic.racedata.tp_restore_items` reads the real table
#: from master.mdb. These two are the ids that exist on every version.
#:
#: History, because this was wrong twice. It shipped as ``(32, 12, 11)``
#: ("limited 30, 30, 10"); ids 11 and 12 are not in item_data at all, so only 32
#: ever matched. The fix for that added Carrot Jelly (35) and Carrot Jelly Mini
#: (34) on the strength of ``effect_type_1 = 2`` - which they do have. That was
#: also wrong, and worse: MEASURED 2026-09-02 on account 909146445823, using one
#: item 34 returned ``result_code 1``, decremented the held count 180 -> 179, and
#: left TP at 9 with ``tp_info`` null in the response. Using one item 32 took TP
#: 9 -> 39. The jellies are RP items and were being burned for nothing.
#:
#: The discriminator is ``item_data.item_category``: **20 is TP** (Toughness 30,
#: plus the +30 "Handmade Chocolate" event items), 21 is RP. ``effect_type_1``
#: is 2 for both and does not separate them.
TP_RESTORE = {32: 30, 174: 30}

#: Spend order: the biggest restore first, so a 30-TP start costs one Toughness
#: rather than thirty Minis.
TP_POTION_ITEMS = tuple(sorted(TP_RESTORE, key=lambda i: -TP_RESTORE[i]))


class ConfigError(RuntimeError):
    """Raised for anything the user can fix by editing settings/paths."""


def _read_json(path: Path) -> dict:
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise ConfigError(f"could not read {path}: {exc}") from exc
    return data if isinstance(data, dict) else {}


@dataclass
class Settings:
    """Resolved run configuration.

    ``runtime_dir`` holds ``auth_config.json`` (the captured login and the Steam
    refresh token); ``state_dir`` holds everything the farm learns and logs.
    """

    data_root: Path
    profile: str
    runtime_dir: Path
    state_dir: Path
    verbose: bool = False

    @property
    def auth_config_path(self) -> Path:
        return self.runtime_dir / "auth_config.json"

    @property
    def capture_dir(self) -> Path:
        """Where ``aic discover`` drops raw endpoint responses."""
        return self.state_dir / "captures"


def load_settings(profile: str | None = None,
                  data_root: str | os.PathLike | None = None,
                  verbose: bool = False) -> Settings:
    """Resolve settings from (in precedence order) args, env, settings.json,
    then the built-in defaults."""
    file_cfg = _read_json(SETTINGS_PATH)

    root = (
        data_root
        or os.environ.get("AIC_DATA_ROOT")
        or file_cfg.get("data_root")
        or DEFAULT_DATA_ROOT
    )
    root = Path(root).expanduser().resolve()

    prof = (
        profile
        or os.environ.get("AIC_PROFILE")
        or file_cfg.get("profile")
        or "default"
    )

    runtime_dir = PROJECT_ROOT / "runtime" / prof
    state_dir = PROJECT_ROOT / "state" / prof
    state_dir.mkdir(parents=True, exist_ok=True)

    return Settings(
        data_root=root,
        profile=prof,
        runtime_dir=runtime_dir,
        state_dir=state_dir,
        verbose=verbose,
    )
