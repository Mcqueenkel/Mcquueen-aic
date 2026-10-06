"""Authentication and the boot sequence up to the main (/load) screen.

This mirrors what Icarus does on startup, minus the FastAPI layer and minus the
frida capture. We only implement the *headless bypass* path: reuse the
``auth_config.json`` a previous frida capture wrote, mint a fresh Steam session
ticket, and log in. If the saved config is stale or absent the user has to run
Icarus once to re-capture - there is no way to derive ``auth_key`` ourselves.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any

from . import bootstrap, steam
from .config import Settings
from .logging_setup import get
from .state import ServerClock, load_clock, save_clock

log = get("auth")

#: Steam session tickets are single-use and short-lived. Reusing a saved one can
#: appear to work for load/index and then expire the session mid-run with API
#: 391/201, so we always try to mint a fresh one when we have credentials.
_TICKET_REFRESH_TIMEOUT = 90


class AuthError(RuntimeError):
    """Credentials are missing, stale, or rejected by the server."""


# --------------------------------------------------------------------------- #
# credential storage
# --------------------------------------------------------------------------- #

def _deobfuscate(value: Any) -> Any:
    """Reverse Icarus' ``enc:`` scheme and uma-reroll-farm's ``enc3:`` DPAPI one.

    Neither is encryption in any meaningful sense; ``enc:`` is reversed base64.
    We support both so an auth_config written by either tool loads here.
    """
    if not isinstance(value, str):
        return value
    if value.startswith("enc:"):
        try:
            return base64.b64decode(value[4:]).decode("utf-8")[::-1]
        except (binascii.Error, UnicodeDecodeError, ValueError):
            return value
    if value.startswith("enc3:"):
        try:
            return _dpapi_unprotect(base64.b64decode(value[5:]))
        except Exception:  # noqa: BLE001 - fall back to the raw string
            log.debug("enc3: value could not be unprotected via DPAPI")
            return value
    return value


def _obfuscate(value: Any) -> Any:
    if not isinstance(value, str) or not value or value.startswith(("enc:", "enc3:")):
        return value
    return "enc:" + base64.b64encode(value[::-1].encode("utf-8")).decode("ascii")


def _dpapi_unprotect(blob: bytes) -> str:
    import ctypes
    from ctypes import wintypes

    class _Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD),
                    ("pbData", ctypes.POINTER(ctypes.c_char))]

    src = _Blob(len(blob), ctypes.cast(ctypes.create_string_buffer(blob),
                                       ctypes.POINTER(ctypes.c_char)))
    out = _Blob()
    ok = ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(src), None, None, None, None, 0, ctypes.byref(out)
    )
    if not ok:
        raise OSError("CryptUnprotectData failed")
    try:
        return ctypes.string_at(out.pbData, out.cbData).decode("utf-8")
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)


_SECRET_KEYS = ("steam_username", "steam_password_seed", "steam_shared_secret")


def load_auth_config(settings: Settings, missing_ok: bool = False) -> dict:
    """The profile's credentials.

    ``missing_ok`` returns ``{}`` instead of raising when there is no file yet.
    That is what a first capture needs: it *creates* the config, so demanding one
    up front made a fresh install impossible to bootstrap.
    """
    path = settings.auth_config_path
    if not path.is_file():
        if missing_ok:
            return {}
        raise AuthError(
            f"no saved credentials at {path}\n"
            "  Capture them from the running game:  account.bat capture\n"
            "  (on Linux: ./account.sh capture - see the README's Proton section).\n"
            "  auth_key cannot be derived offline."
        )
    with open(path, "r", encoding="utf-8-sig") as fh:
        cfg = json.load(fh)

    for key in _SECRET_KEYS:
        if key in cfg:
            cfg[key] = _deobfuscate(cfg[key])
    return cfg


def save_auth_config(settings: Settings, cfg: dict) -> None:
    out = dict(cfg)
    for key in _SECRET_KEYS:
        if key in out:
            out[key] = _obfuscate(out[key])
    path = settings.auth_config_path
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=4)
    # 0600 before the swap: the file holds the Steam ticket + refresh token, and the
    # default umask left it world-readable (644) after every takeover rewrite.
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    tmp.replace(path)


def validate_auth_config(cfg: dict) -> None:
    """Icarus' ``has_fresh_auth_config`` gate, as an exception-raising check."""
    app_ver = str(cfg.get("app_ver") or "").strip()
    res_ver = str(cfg.get("res_ver") or "").strip()
    if not app_ver or not res_ver:
        raise AuthError("auth_config is missing app_ver/res_ver - re-capture with Icarus")

    auth_key = str(cfg.get("auth_key") or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]+", auth_key) or len(auth_key) < 32 or len(auth_key) % 2:
        raise AuthError("auth_config auth_key is malformed - re-capture with Icarus")
    if int(cfg.get("auth_key_len") or 0) != 48:
        raise AuthError("auth_config auth_key_len != 48 - re-capture with Icarus")

    udid = str(cfg.get("udid") or "").strip()
    if len(udid) != 36 or udid.count("-") != 4:
        raise AuthError("auth_config udid is not a UUID - re-capture with Icarus")

    if not cfg.get("viewer_id"):
        raise AuthError("auth_config has no viewer_id - re-capture with Icarus")
    if not cfg.get("steam_id"):
        raise AuthError("auth_config has no steam_id - re-capture with Icarus")


#: A stored ticket younger than this is reused instead of minting a new one.
#: Matches uma-reroll-farm. Minting is a real Steam login every time, and doing
#: it on every command is what trips Steam Guard.
MAX_TICKET_AGE = 420  # 7 minutes

#: PREVENTION for the 394 wedge (2026-09-09). A persistent Steam session's ticket
#: is otherwise reused indefinitely - a stale one 394'd the whole farm for 8h
#: before note_dead_ticket (reactive) could catch it. So recycle the session
#: proactively once it passes this age: re-minting from the stored refresh_token
#: is cheap, unattended, and one login covers the whole 14-account rotation.
#:
#: The server-side session TTL is unknown; the dead session had run well past 8h.
#: 6h stays comfortably under that at ~4 re-mints/day (nowhere near a Steam rate
#: limit). It is a calibration knob, not a constant - tune with the env var if a
#: 394 ever slips through before the cap.
def session_max_age() -> float:
    raw = (os.environ.get("AIC_STEAM_SESSION_MAX_AGE") or "").strip()
    if not raw:
        return 6 * 3600.0
    try:
        return max(600.0, float(raw))   # never below 10 min, or logins thrash
    except ValueError:
        log.warning("AIC_STEAM_SESSION_MAX_AGE=%r is not a number; using 6h.", raw)
        return 6 * 3600.0

#: PREVENTION for the rate-limit wedge (2026-09-12). When the stored ticket
#: dies, every rotation visit tries to re-mint; 13 accounts cycling every ~50s
#: fired ~130 Steam logins in 2h and tripped Steam's RateLimitExceeded - which
#: then blocks the refresh-token mint that is the ONLY unattended recovery, so
#: the farm can never climb out. The fix: on a rate-limit, stop minting globally
#: for a cooldown. Visits fail fast on the stale ticket (no Steam hit) until it
#: lapses, Steam's limiter cools, and the next mint recovers the whole roster.
_mint_block_until = 0.0


def mint_cooldown() -> float:
    raw = (os.environ.get("AIC_STEAM_MINT_COOLDOWN") or "").strip()
    if not raw:
        return 1800.0  # 30 min; a calibration knob, not a constant
    try:
        return max(60.0, float(raw))
    except ValueError:
        log.warning("AIC_STEAM_MINT_COOLDOWN=%r is not a number; using 30m.", raw)
        return 1800.0


#: The game process. Steam cancels an app session ticket when the process that
#: owns it exits, so a ticket is only usable while this is alive - regardless of
#: how recently it was minted.
GAME_PROCESS = "UmamusumePrettyDerby.exe"


def game_is_running() -> bool:
    if os.name != "nt":
        from . import proton
        return proton.game_is_running()
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {GAME_PROCESS}"],
            capture_output=True, text=True, timeout=10,
        ).stdout or ""
    except (OSError, subprocess.SubprocessError):
        return False
    return GAME_PROCESS.lower() in out.lower()


def ticket_age(cfg: dict, settings: Settings | None = None) -> float:
    """Seconds since the stored ticket was minted.

    Falls back to the auth_config's mtime when there is no timestamp - Icarus
    rewrites the file whenever it captures, so that is a fair proxy - and to
    infinity when even that is unavailable.
    """
    stamped = cfg.get("steam_ticket_ts")
    if isinstance(stamped, (int, float)) and stamped > 0:
        return max(0.0, time.time() - float(stamped))
    if settings is not None:
        try:
            return max(0.0, time.time() - settings.auth_config_path.stat().st_mtime)
        except OSError:
            pass
    return float("inf")


#: Result codes that mean "the Steam ticket we are holding is finished."
#: 391 = ticket rejected/cancelled; 394 = "game session expired" (same meaning
#: from our side). MEASURED 2026-09-09: a persistent node session's ticket died
#: with 394 and the farm reused it for 8 hours - has_live_session() stayed true,
#: so refresh_steam_ticket copied the SAME dead ticket into every request and
#: nothing re-minted, because this recovery was gated to 391 alone.
_DEAD_TICKET_CODES = (391, 394)


def note_dead_ticket(exc: BaseException) -> bool:
    """A 391/394 means the ticket we are holding is finished. Drop the session.

    Steam cancels an auth session ticket while the node process that minted it
    stays perfectly healthy, so ``steam.has_live_session()`` keeps saying "live"
    and :func:`refresh_steam_ticket` keeps copying the SAME dead ticket into
    every request. Nothing else ever re-mints, so a long-running loop wedges
    permanently on the first cancellation.

    Closing the session here is what makes the next call mint a fresh one.
    Returns whether a session was actually dropped.
    """
    rc = bootstrap.result_code(exc)
    if rc not in _DEAD_TICKET_CODES:
        return False
    if not steam.has_live_session():
        return False
    log.warning("Steam rejected the held ticket (%s) - dropping the session so "
                "the next call mints a fresh one.", rc)
    try:
        steam.close()
    except Exception as close_exc:  # noqa: BLE001 - never mask the original error
        log.debug("closing the Steam session failed (%s)", close_exc)
    return True


def refresh_steam_ticket(cfg: dict, settings: Settings | None = None,
                         max_age: float = MAX_TICKET_AGE, force: bool = False,
                         steam_code: str = "") -> bool:
    """Mint a fresh Steam session ticket in place, if the stored one is stale.

    Returns whether a new ticket was obtained. A failure is never fatal here:
    the stored ticket is often still good, and if it is not the login error says
    so. Raising instead would turn a rate-limited Steam into a dead tool.
    """
    global _mint_block_until
    # A session we are holding open keeps its ticket valid indefinitely, so
    # neither the age nor the game matters. Copy its ticket into cfg rather than
    # just returning: a caller that built cfg from disk is holding the previous,
    # possibly dead ticket, and would send that instead.
    live = steam.current()
    if live is not None and not force:
        cap = session_max_age()
        if steam.session_age() > cap:
            log.info("Recycling the Steam session proactively (%.1fh old, cap "
                     "%.1fh) so a stale ticket cannot 394 the farm - re-minting "
                     "from the refresh token.",
                     steam.session_age() / 3600, cap / 3600)
            try:
                steam.close()
            except Exception as exc:  # noqa: BLE001 - fall through to a fresh mint
                log.debug("closing the aged session failed (%s)", exc)
            live = None
    if not force and live is not None:
        changed = cfg.get("steam_session_ticket") != live.ticket
        cfg["steam_session_ticket"] = live.ticket
        cfg["steam_id"] = live.steam_id or cfg.get("steam_id")
        cfg["steam_ticket_ts"] = int(time.time())
        if live.refresh_token:
            cfg["steam_refresh_token"] = live.refresh_token
        log.debug("using the live Steam session's ticket (%ds old, %s)",
                  int(steam.session_age()),
                  "adopted into cfg" if changed else "already current")
        return changed

    age = ticket_age(cfg, settings)
    # An explicit Guard code means the caller wants a new ticket now.
    if not force and not steam_code and cfg.get("steam_session_ticket") and age <= max_age:
        # Age alone is not enough: Steam cancels the ticket when the game
        # process exits, so a "fresh" ticket from a closed game is already dead.
        # uma-reroll-farm gates reuse on both, and that is why its 394s do not
        # look like ours did.
        if game_is_running():
            log.info("Reusing the stored Steam ticket (%ds old, game running).",
                     int(age))
            return False
        log.debug("ticket is %ds old but the game is closed; Steam has cancelled "
                  "it - minting a new one", int(age))

    username = cfg.get("steam_username")
    if not username:
        log.warning("no saved Steam username; reusing the stored ticket "
                    "(expect session expiry mid-run)")
        return False

    # Steam rate-limited a recent mint: hold off every mint (force included - a
    # forced mint into a live limiter is exactly what keeps it tripped) until the
    # cooldown lapses. Reuse the stored ticket meanwhile; visits fail fast on it
    # without touching Steam, so the limiter can cool.
    remaining = _mint_block_until - time.time()
    if remaining > 0:
        log.warning("Skipping the Steam mint: rate-limit cooldown active for %ds "
                    "more. Reusing the stored ticket until Steam's limiter cools.",
                    int(remaining))
        return False

    # Preferred: a persistent steam-user process. The ticket stays valid while
    # that process lives, so one login covers the whole run however many
    # accounts get switched. Icarus' get_ticket exits immediately, which is why
    # its tickets die within minutes and every command needs a fresh login.
    if settings is not None and not cfg.get("steam_disable_persistent"):
        try:
            session = steam.acquire(settings.icarus_root, cfg, steam_code)
        except steam.SteamGuardRequired as exc:
            log.warning("%s", exc)
            log.warning("Continuing with the stored ticket (%ds old).", int(age))
            return False
        except Exception as exc:  # noqa: BLE001
            if "ratelimit" in str(exc).lower():
                _mint_block_until = time.time() + mint_cooldown()
                log.warning("Steam rate-limited the mint (%s); backing off all "
                            "mints for %ds so the retry storm cannot keep the "
                            "limiter tripped. The stored ticket is reused until "
                            "then.", exc, int(mint_cooldown()))
                return False
            log.warning("persistent Steam session unavailable (%s); falling back "
                        "to a one-shot ticket", exc)
        else:
            cfg["steam_id"] = session.steam_id or cfg.get("steam_id")
            cfg["steam_session_ticket"] = session.ticket
            cfg["steam_ticket_ts"] = int(time.time())
            if session.refresh_token:
                # Storing this is what lets later runs skip password and 2FA.
                cfg["steam_refresh_token"] = session.refresh_token
                log.info("Stored a Steam refresh token - later runs will not need "
                         "a password or a Guard code.")
            return True

    # Minting goes through Icarus' get_ticket, so Icarus has to be bound. Most
    # callers have booted already, but `farm` reaches the one-shot mint before
    # it ever builds a client - and only when the persistent Steam session
    # failed, which is why this stayed hidden. bind() is idempotent.
    ick = bootstrap.bind(settings) if settings is not None else bootstrap.icarus()
    secret = cfg.get("steam_shared_secret", "")
    log.info("Minting a one-shot Steam ticket%s...",
             " with the supplied Guard code" if steam_code else
             " (2FA from the stored shared secret)" if secret else "")
    try:
        steam_id, ticket = ick.get_ticket(
            username,
            cfg.get("steam_password_seed", ""),
            steam_code,
            secret,
        )
    except Exception as exc:  # noqa: BLE001 - the generator raises bare Exceptions
        if "STEAM_GUARD_REQUIRED" in str(exc):
            log.warning(
                "Steam Guard wants a code, so no new ticket was minted - "
                "falling back to the stored one (%ds old, limit %ds).\n"
                "  Minting is a real Steam login; several in a few minutes will "
                "trigger this.\n"
                "  If the login below fails, the stored ticket is spent. Then "
                "either:\n"
                "    - pass a code:      --steam-code 12345\n"
                "    - store the 2FA secret once, for good: "
                "account.bat steam-secret <shared_secret>\n"
                "    - or leave the GAME RUNNING: Steam only cancels a ticket "
                "when the game\n"
                "      process exits, so a live game keeps the ticket valid and "
                "needs no Steam\n"
                "      login at all (this is how uma-reroll-farm avoids Guard "
                "entirely)",
                int(age), int(max_age))
        else:
            log.warning("ticket refresh failed (%s); reusing the stored ticket", exc)
        return False

    cfg["steam_id"] = steam_id
    cfg["steam_session_ticket"] = ticket
    cfg["steam_ticket_ts"] = int(time.time())
    log.info("Steam ticket refreshed.")
    return True


# --------------------------------------------------------------------------- #
# boot sequence
# --------------------------------------------------------------------------- #

@dataclass
class Session:
    """A logged-in client plus the payloads the boot sequence collected."""

    client: Any
    cfg: dict
    settings: Settings
    clock: ServerClock = field(default_factory=ServerClock)
    load_index: dict = field(default_factory=dict)
    pre_single_mode: dict = field(default_factory=dict)
    trained_chara: dict = field(default_factory=dict)
    story_event: dict = field(default_factory=dict)

    @property
    def viewer_id(self) -> int:
        return int(self.client.viewer_id or 0)

    def refresh_home(self) -> dict:
        """Re-issue ``load/index`` and refresh the client's cached account state.

        TP, money and the succession-rank-point balance all go stale quickly and
        the server validates them on career start, so this is called immediately
        before any start. Also re-anchors the server clock.
        """
        res = self.client.call("load/index", {"adid": ""})
        self.clock.observe(res)
        self.load_index = res.get("data", {}) or {}
        self.client.refresh_cached_account_state(self.load_index)
        return self.load_index

    def refresh_pre_single_mode(self) -> dict:
        res = self.client.pre_single_mode()
        self.pre_single_mode = res.get("data", {}) or {}
        return self.pre_single_mode


def boot(settings: Settings, refresh_ticket: bool = True,
         steam_code: str = "") -> Session:
    """Authenticate and walk the full sequence to the main screen.

    The call order matches the real client and Icarus:
      tool/start_session -> tool/start_session/load -> load/index ->
      read_info/index -> story_event/index -> trained_chara/load ->
      pre_single_mode/index

    ``UmaClient.login()`` performs the first four; the rest are what the home
    screen needs before any career action.
    """
    ick = bootstrap.bind(settings)

    cfg = load_auth_config(settings)
    validate_auth_config(cfg)
    log.info("Loaded credentials for profile %r (viewer %s).",
             settings.profile, cfg.get("viewer_id"))

    ticket_refreshed = (refresh_steam_ticket(cfg, settings, steam_code=steam_code)
                        if refresh_ticket else False)

    # Persist a new ticket / refresh token BEFORE attempting the login. The
    # login can legitimately fail (an unlinked slot answers 501) and losing a
    # hard-won refresh token to that would force another Guard prompt.
    if ticket_refreshed:
        try:
            save_auth_config(settings, cfg)
        except OSError as exc:
            log.warning("could not persist the refreshed Steam ticket: %s", exc)

    client = ick.UmaClient(cfg)
    log.info("Logging in...")
    try:
        res = client.login()
    except Exception as exc:  # noqa: BLE001
        rc = bootstrap.result_code(exc)
        raise AuthError(_login_hint(rc, exc)) from exc

    data = (res or {}).get("data") or {}
    if not data:
        raise AuthError("load/index returned no data - login did not reach the main screen")

    clock = load_clock(settings)
    clock.observe(res)
    save_clock(settings, clock)
    session = Session(client=client, cfg=cfg, settings=settings, clock=clock,
                      load_index=data)
    log.info("Reached the main screen (load/index OK).")

    # The client may have adopted a newer res_ver via result code 214 during
    # login; persist it (and the fresh ticket) so the next run starts current.
    # Persist a freshly minted ticket (with its timestamp, so the next command
    # can reuse it) and any res_ver the client adopted via result code 214.
    if ticket_refreshed or str(client.res_ver) != str(cfg.get("res_ver")):
        cfg["res_ver"] = str(client.res_ver)
        cfg["app_ver"] = str(client.app_ver)
        try:
            save_auth_config(settings, cfg)
        except OSError as exc:
            log.warning("could not persist refreshed auth_config: %s", exc)

    # Optional home-screen calls. story_event returns 9001 when no event is
    # running, and trained_chara/load is a multi-MB payload we only need for
    # parent selection - neither is fatal.
    session.story_event = _optional(client, "story_event/index", {}, ignore={9001})
    session.trained_chara = _optional(client, "trained_chara/load", {})
    session.pre_single_mode = _optional(client, "pre_single_mode/index", {})

    return session


def _optional(client, endpoint: str, payload: dict, ignore: set[int] | None = None) -> dict:
    ignore = ignore or set()
    try:
        res = client.call(endpoint, payload)
    except Exception as exc:  # noqa: BLE001
        rc = bootstrap.result_code(exc)
        if rc in ignore or rc == 102:
            log.debug("%s returned %s (tolerated)", endpoint, rc)
        else:
            log.warning("%s failed (%s); continuing without it", endpoint, exc)
        return {}
    return (res or {}).get("data") or {}


#: Result codes that mean "these credentials no longer identify this account".
#: All of them need a fresh frida capture - auth_key cannot be derived offline
#: and is bound to the udid in the same request header.
_STALE_CREDENTIAL_CODES = (202, 501, 709)

_STALE_HINT = (
    "The stored auth_key is not valid for the account currently on this Steam "
    "slot.\n"
    "  This is what you get after switching accounts anywhere else - in the real\n"
    "  client, or with `account.bat login` from a different machine.\n"
    "  auth_key is bound to the udid in the request header and cannot be derived\n"
    "  offline, so it has to be re-captured:\n"
    "    1. Run Icarus (Start Icarus.bat) and let it capture auth from the game.\n"
    "    2. Rerun this command.\n"
    "  `account.bat import-capture` only works when the switch itself was captured\n"
    "  with the same udid the tool is using."
)


def _login_hint(rc, exc) -> str:
    hints = {
        201: "session expired - the Steam ticket was stale; retry",
        214: "client data version is behind the server; re-capture auth with Icarus",
        204: "client version is behind the server; re-capture auth with Icarus",
        217: "session was invalidated elsewhere (another client logged in)",
        391: "Steam ticket rejected - mint a fresh one (is Steam running/online?)",
    }
    base = f"login failed: {exc}"
    if rc in _STALE_CREDENTIAL_CODES:
        return f"{base}\n  {_STALE_HINT}"
    hint = hints.get(rc)
    return f"{base}\n  {hint}" if hint else base
