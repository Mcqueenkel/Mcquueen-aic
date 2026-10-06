"""Account switching: Data Link (Link Password) login and Steam unlink.

Two operations, both reconstructed from captures:

**logout** - ``account/steam_chain_disconnect``. This is the game's "Delete User
Data": it severs the Steam<->account link and frees the Steam slot for a new
account. It is **destructive**. Without a Data Link (Trainer ID + Link Password)
published beforehand, the account cannot be recovered by anyone, ever. So this
module refuses to run it unless a link password is already on record for that
viewer, or one is published as part of the same command.

**login** - takes over an existing account using Trainer ID + Link Password.
The captured sequence (dumps/20260731_093457) is:

    tool/pre_signup                      {cygames_id_client_sdk_version: "v2"}
    account/index                        {attestation_type: 1, device_token: null}
    tool/signup                          {credential, optin_user_birth, country, ...}
        -> a throwaway viewer_id + auth_key; the Steam slot now holds a new account
    account/get_by_transition_code       {password, input_viewer_id}
        -> {name, request_user_name, target_viewer_id}   (confirmation preview)
    account/chain_by_transition_code     {password, input_viewer_id}
        -> {chained_viewer_id, chained_user_name, auth_key}   (the takeover)
    tool/start_session -> load/index

The password is not sent in the clear: it is ``md5(password + SALT)``, the same
hashing ``account/publish_transition_code`` uses when the link is created.
"""

from __future__ import annotations

import base64
import hashlib
import sys
import json
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from . import bootstrap
from .auth import load_auth_config, save_auth_config
from .config import Settings
from .logging_setup import get
from .pacing import sleep as paced_sleep

log = get("account")

#: Link passwords the game accepts: 8-16 chars with upper, lower and a digit.
_PASSWORD_RE = (8, 16)

#: Endpoints tried in order to redeem a transition code. The capture shows both
#: being called with the same password - ``get_`` returns a confirmation preview
#: and ``chain_`` performs the takeover - but the dump's endpoint labels are not
#: fully reliable, so we accept whichever response actually carries the auth_key.
TRANSITION_ENDPOINTS = (
    ("account/get_by_transition_code", str),
    ("account/chain_by_transition_code", int),
)

#: ``tool/pre_signup`` body. NOT ``{"cygames_id_client_sdk_version": "v2"}`` -
#: that one belongs to ``account/index``. The capture's endpoint labels are
#: shifted, and reading them the wrong way round answers 102 on the signup that
#: follows. Taken from uma-reroll-farm, whose reroll loop creates accounts by
#: the hundred with exactly this.
PRE_SIGNUP_BODY = {"attestation_type": 1, "device_token": None}

#: ``tool/signup`` body. ``optin_user_birth``/``country``/``dma_state`` only
#: shape the throwaway account that the takeover immediately replaces.
SIGNUP_BODY = {
    "credential": "",
    "error_code": 0,
    "error_message": "",
    "attestation_type": 1,
    "optin_user_birth": 199001,
    "dma_state": 1,
    "country": "Germany",
}

#: Pause between pre_signup and signup, as both known-good implementations do.
SIGNUP_GAP = 0.83

#: Fields that identify the *account* rather than the device or the Steam login.
#: Dropped when building the config for a signup: the server refuses a new
#: account on a udid it already knows, and device_id is regenerated from the
#: hardware. Everything else - Steam credentials, tokens, app/res versions -
#: carries over.
ACCOUNT_IDENTITY_KEYS = ("viewer_id", "auth_key", "auth_key_len", "udid",
                         "device_id", "body", "endpoint", "type")


class AccountError(RuntimeError):
    """Something the user must decide about or fix."""


def format_trainer_id(viewer_id) -> str:
    """``103444444441`` -> ``103 444 444 441``, as the game shows it."""
    text = str(viewer_id)
    return " ".join(text[i:i + 3] for i in range(0, len(text), 3))


def parse_trainer_id(value: str) -> int:
    """Accept a Trainer ID with or without the game's spacing/dashes."""
    digits = re.sub(r"\D", "", str(value))
    if not digits:
        raise AccountError(f"trainer id {value!r} contains no digits")
    return int(digits)


def validate_password(password: str) -> None:
    low, high = _PASSWORD_RE
    if not (low <= len(password) <= high
            and re.search(r"[a-z]", password)
            and re.search(r"[A-Z]", password)
            and re.search(r"\d", password)):
        raise AccountError(
            f"link password must be {low}-{high} characters and contain an "
            "uppercase letter, a lowercase letter and a digit"
        )


def hash_password(password: str) -> str:
    """``md5(password + SALT)`` - how the game transmits a Link Password.

    The salt comes from Icarus rather than being copied here, so it stays in one
    place. Works either once :func:`aic.bootstrap.bind` has run or whenever
    ``uma_api`` is otherwise importable.
    """
    try:
        salt = bootstrap.icarus().SALT
    except Exception:  # noqa: BLE001 - not bound; fall back to the vendored client
        # The vendored copy ships with this tool, so the salt is available even
        # on a box with no Icarus checkout (where a bare `uma_api` import fails).
        vendor = str(Path(__file__).resolve().parent.parent / "vendor")
        if vendor not in sys.path:
            sys.path.insert(0, vendor)
        from uma_api.client import SALT as salt  # noqa: N813

    return hashlib.md5(password.encode("utf-8") + salt).hexdigest()


# --------------------------------------------------------------------------- #
# the local record of link passwords
# --------------------------------------------------------------------------- #

def _store_path(settings: Settings) -> Path:
    return settings.state_dir.parent / "accounts.json"


def load_accounts(settings: Settings) -> list[dict]:
    try:
        with open(_store_path(settings), "r", encoding="utf-8-sig") as fh:
            data = json.load(fh)
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def names_by_viewer(settings: Settings) -> dict[int, str]:
    """``{viewer_id: in-game trainer name}`` for every account on record.

    The names are the ``chained_user_name`` the server returns on a takeover,
    written by :func:`remember_account` on every successful chain - so they
    follow an in-game rename by themselves. Accounts with no name recorded are
    left out entirely rather than mapped to "", so callers can tell "no name
    known" from "named empty".

    Names are returned READY TO DISPLAY: nothing stops two accounts sharing one
    in-game name, so a shared name carries the last four digits of its trainer
    id. Disambiguating here rather than in each caller is what keeps the log and
    the Discord report calling the same account the same thing. MEASURED
    2026-09-03: 793593252166 joined the roster already called "McDonald", which
    768270152387 was called too, and a report naming both plain "McDonald" can
    not be read.
    """
    raw: dict[int, str] = {}
    for row in load_accounts(settings):
        try:
            viewer = int(row.get("viewer_id") or 0)
        except (TypeError, ValueError):
            continue
        name = str(row.get("name") or "").strip()
        if viewer and name:
            raw[viewer] = name

    # Case-insensitively, because every lookup built on these matches that way.
    folded = [n.casefold() for n in raw.values()]
    clashing = {n for n in folded if folded.count(n) > 1}
    return {viewer: (f"{name} {str(viewer)[-4:]}"
                     if name.casefold() in clashing else name)
            for viewer, name in raw.items()}


def find_account(settings: Settings, viewer_id) -> dict | None:
    for row in load_accounts(settings):
        if str(row.get("viewer_id")) == str(viewer_id) and row.get("link_set"):
            return row
    return None


def remember_account(settings: Settings, viewer_id, password: str,
                     link_set: bool, name: str = "") -> None:
    """Record a Trainer ID + Link Password so the account can be recovered.

    Stored in plain text - it is a recovery credential for a game account, and
    a password you cannot read is no use when you need to type it into the real
    client. Keep the state directory private.

    ``name`` is the account's IN-GAME trainer name. Omitting it KEEPS whatever
    is already on record rather than clearing it: this row is rebuilt from
    scratch on every call, and callers that have no name to offer (``save-link``,
    ``logout``) would otherwise wipe a name a chain had recorded - which
    :func:`names_by_viewer` and every label built on it then lose. Pass a name
    only when it came from the server; a local nickname in this field is worse
    than an empty one, because an empty one is recognisably absent.
    """
    previous = ""
    rows = []
    for row in load_accounts(settings):
        if str(row.get("viewer_id")) == str(viewer_id):
            previous = str(row.get("name") or "").strip()
            continue
        rows.append(row)
    rows.append({
        "viewer_id": int(viewer_id),
        "trainer_id": format_trainer_id(viewer_id),
        "name": str(name or "").strip() or previous,
        "password": password,
        "link_set": bool(link_set),
        "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    })
    path = _store_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(rows, fh, indent=2)
    log.info("Recorded Trainer ID %s in %s", format_trainer_id(viewer_id), path)


def archive_auth_config(settings: Settings, note: str = "") -> Path | None:
    """Copy the live auth_config aside before it is invalidated."""
    src = settings.auth_config_path
    if not src.is_file():
        return None
    try:
        cfg = json.loads(src.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        log.warning("could not archive auth_config (%s)", exc)
        return None

    dest_dir = settings.state_dir / "auth_archive"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{cfg.get('viewer_id', 'unknown')}_{int(time.time())}.json"
    cfg["_archived_note"] = note
    dest.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    log.info("Archived the previous credentials to %s", dest)
    return dest


# --------------------------------------------------------------------------- #
# operations
# --------------------------------------------------------------------------- #

def publish_link(session, password: str) -> bool:
    """Publish a Data Link so this account can be recovered later."""
    validate_password(password)
    res = session.client.call("account/publish_transition_code",
                              {"password": hash_password(password)})
    rc = (res or {}).get("data_headers", {}).get("result_code")
    if rc != 1:
        log.error("publish_transition_code returned result_code %s - the Data "
                  "Link was NOT set.", rc)
        return False
    log.info("Data Link published for Trainer ID %s.",
             format_trainer_id(session.viewer_id))
    return True


def steam_disconnect(session) -> dict:
    """``account/steam_chain_disconnect`` - unlink and free the Steam slot."""
    res = session.client.call("account/steam_chain_disconnect", {})
    rc = (res or {}).get("data_headers", {}).get("result_code")
    if rc != 1:
        raise AccountError(f"steam_chain_disconnect returned result_code {rc}")
    return res


#: Where UmaDumpy writes its captures, if not given explicitly.
DEFAULT_DUMP_ROOT = Path.home() / "Documents" / "UmaDumpy-main" / "dumps"


def credentials_from_capture(dump_dir: Path) -> tuple[int, str, str]:
    """Recover ``(viewer_id, auth_key_b64, name)`` from a captured switch.

    An account switch performed in the real client returns the new account's
    ``auth_key`` in the ``*_by_transition_code`` response - the same credential
    :func:`login_with_link` would obtain. Importing it repairs an
    ``auth_config.json`` that still points at the previous account, with no
    frida re-capture needed.

    Prefers a transition response (a real account) over ``tool/signup`` (the
    throwaway created moments earlier), and the newest of each.
    """
    if not dump_dir.is_dir():
        raise AccountError(f"{dump_dir} is not a directory")

    best_transition = best_signup = None
    for path in sorted(dump_dir.rglob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue
        if payload.get("kind") != "response":
            continue
        data = ((payload.get("data") or {}).get("data")) or {}
        if not isinstance(data, dict) or not data.get("auth_key"):
            continue

        if data.get("chained_viewer_id"):
            best_transition = (int(data["chained_viewer_id"]), data["auth_key"],
                               data.get("chained_user_name") or "", path)
        elif data.get("viewer_id"):
            best_signup = (int(data["viewer_id"]), data["auth_key"], "", path)

    found = best_transition or best_signup
    if not found:
        raise AccountError(
            f"no response carrying an auth_key found under {dump_dir}.\n"
            "  Expected an account/*_by_transition_code or tool/signup response "
            "from a captured account switch."
        )
    viewer_id, auth_b64, name, path = found
    if best_transition is None:
        log.warning("Only a tool/signup response was found - this is the "
                    "throwaway account created during a switch, not the account "
                    "you switched to.")
    log.info("Found credentials for viewer %s%s in %s",
             viewer_id, f" ({name})" if name else "", path.name)
    return viewer_id, auth_b64, name


def import_credentials(settings: Settings, viewer_id: int, auth_key_b64: str,
                       name: str = "") -> None:
    """Point this profile's auth_config at ``viewer_id`` without re-capturing.

    Only the account identity changes; the device fields, ``udid``, ``app_ver``
    and ``res_ver`` are client-level and carry over unchanged.
    """
    cfg = load_auth_config(settings)
    previous = cfg.get("viewer_id")
    auth_hex = base64.b64decode(auth_key_b64).hex()

    # Same reason as in login_with_link: the stored envelope belongs to the
    # account being replaced, so it no longer matches the udid beside it.
    for stale in ("body", "endpoint", "captured_endpoint"):
        cfg.pop(stale, None)
    cfg["viewer_id"] = int(viewer_id)
    cfg["auth_key"] = auth_hex
    cfg["auth_key_len"] = len(auth_hex) // 2
    save_auth_config(settings, cfg)
    log.info("auth_config now points at Trainer ID %s (was viewer %s).",
             format_trainer_id(viewer_id), previous)


@dataclass
class LinkResult:
    viewer_id: int = 0
    name: str = ""
    auth_key_hex: str = ""
    temp_viewer_id: int = 0
    steps: list[str] = field(default_factory=list)


def login_with_link(settings: Settings, trainer_id: int, password: str) -> LinkResult:
    """Take over an account with its Trainer ID and Link Password.

    Runs against a *fresh* client with ``viewer_id`` 0 and no auth key, because
    that is the state the Steam slot is in after a disconnect. On success the
    profile's ``auth_config.json`` is rewritten to the new account.
    """
    ick = bootstrap.bind(settings)
    hashed = hash_password(password)
    result = LinkResult()

    base = load_auth_config(settings)

    # A signup needs a udid the server has never seen: reusing the one bound to
    # the previous account answers 102, even with the Steam slot free. This
    # mirrors uma-reroll-farm's reroll_cfg(fresh_udid=True), which also drops
    # device_id so the client regenerates it. The account that comes back is
    # bound to THIS udid, so it has to be persisted with the new credentials.
    fresh_udid = str(uuid.uuid4())
    # Built by removing the account identity from the profile, not by listing
    # what to keep: an allowlist silently drops anything added later (it already
    # cost us the refresh token, which sent every takeover back to a password
    # login and a Guard prompt).
    cfg = dict(base)
    for key in ACCOUNT_IDENTITY_KEYS:
        cfg.pop(key, None)
    cfg["viewer_id"] = 0
    cfg["auth_key"] = ""
    cfg["udid"] = fresh_udid
    # Keep the captured device_id: on POSIX there is no Windows registry to
    # regenerate it from, and the client only falls back to get_hwid() (blocked
    # on POSIX) when device_id is absent. It is a per-device value, so reusing it
    # for the signup is correct on Windows too.
    if base.get("device_id"):
        cfg["device_id"] = base["device_id"]
    log.info("Using a fresh udid for the signup.")

    # The occupancy pre-check already logged in moments ago, so the stored
    # ticket is almost certainly fresh; the age check makes this a no-op rather
    # than a second real Steam login.
    from .auth import refresh_steam_ticket
    refresh_steam_ticket(cfg, settings)

    client = ick.UmaClient(cfg)
    client.viewer_id = 0
    client.auth_key_hex = ""
    client.regen_sid()

    def step(name, endpoint, payload, regen=True):
        """One pre-auth call.

        ``regen_sid()`` before each: the sid rolls forward after every response,
        but these unauthenticated calls are validated against a sid derived
        fresh from ``make_sid(viewer_id, udid)``. Icarus' working ``signup()``
        regenerates immediately before ``tool/signup`` for exactly this reason -
        omitting it answers 102.
        """
        if regen:
            client.regen_sid()
        log.info("  %s (%s)", name, endpoint)
        res = client.call(endpoint, payload)
        result.steps.append(endpoint)
        return (res or {}).get("data") or {}

    log.info("Creating a temporary account to receive the transfer...")
    step("pre_signup", "tool/pre_signup", dict(PRE_SIGNUP_BODY))
    paced_sleep(SIGNUP_GAP)

    try:
        signed = step("signup", "tool/signup", dict(SIGNUP_BODY))
    except Exception as exc:  # noqa: BLE001
        if bootstrap.result_code(exc) == 102:
            raise AccountError(
                "tool/signup was refused (102) - the server rejected the new "
                "account.\n"
                "  Most often this means the Steam slot is still occupied. Free "
                "it first:\n"
                "      account.bat logout\n"
                "  If the slot is already free, the pre_signup handshake is the "
                "suspect;\n"
                "  it must send {'attestation_type': 1, 'device_token': None}."
            ) from exc
        raise

    temp_viewer = int(signed.get("viewer_id") or 0)
    temp_auth = signed.get("auth_key") or ""
    if not temp_viewer or not temp_auth:
        raise AccountError(
            "tool/signup returned no viewer_id/auth_key; cannot continue. "
            "Free the Steam slot first with `account.bat logout`."
        )
    result.temp_viewer_id = temp_viewer
    client.viewer_id = temp_viewer
    client.auth_key_hex = base64.b64decode(temp_auth).hex()
    client.regen_sid()
    log.info("Temporary account %s created.", temp_viewer)

    log.info("Redeeming Trainer ID %s ...", format_trainer_id(trainer_id))
    chained_auth = ""
    last_error: Exception | None = None
    for endpoint, cast in TRANSITION_ENDPOINTS:
        payload = {"password": hashed, "input_viewer_id": cast(trainer_id)}
        try:
            # Authenticated as the temporary account now, so the rolled sid is
            # the correct one - do not regenerate.
            data = step("transition", endpoint, payload, regen=False)
        except Exception as exc:  # noqa: BLE001
            rc = bootstrap.result_code(exc)
            log.debug("%s failed (%s)", endpoint, rc)
            last_error = exc
            continue
        if data.get("chained_user_name") or data.get("name"):
            result.name = data.get("chained_user_name") or data.get("name") or ""
        if data.get("auth_key"):
            chained_auth = data["auth_key"]
            result.viewer_id = int(data.get("chained_viewer_id") or trainer_id)
            break

    if not chained_auth:
        raise AccountError(
            "the transfer did not return an auth_key.\n"
            f"  Trainer ID {format_trainer_id(trainer_id)} with this password was "
            "not accepted.\n"
            "  Check the Trainer ID and the Link Password, and that the Data Link "
            "has not already been consumed (a link is single-use on some flows).\n"
            f"  Last error: {last_error}"
        )

    result.auth_key_hex = base64.b64decode(chained_auth).hex()
    log.info("Transfer accepted: %s (Trainer ID %s)",
             result.name or "?", format_trainer_id(result.viewer_id))

    new_cfg = dict(base)
    # The stored envelope was captured under the OLD udid; keeping it would
    # leave a body that cannot be decrypted with the udid beside it.
    for stale in ("body", "endpoint", "captured_endpoint", "captured_at"):
        new_cfg.pop(stale, None)
    new_cfg["viewer_id"] = result.viewer_id
    new_cfg["auth_key"] = result.auth_key_hex
    new_cfg["auth_key_len"] = len(result.auth_key_hex) // 2
    # The auth_key is bound to the udid that performed the transfer, so the
    # fresh one must replace the old account's - keeping the old udid would
    # make every later call fail with 202.
    new_cfg["udid"] = fresh_udid
    new_cfg["device_id"] = client.device_id
    new_cfg["steam_id"] = cfg.get("steam_id") or new_cfg.get("steam_id")
    new_cfg["steam_session_ticket"] = (cfg.get("steam_session_ticket")
                                       or new_cfg.get("steam_session_ticket"))
    for key in ("steam_ticket_ts", "steam_refresh_token"):
        if cfg.get(key):
            new_cfg[key] = cfg[key]
    save_auth_config(settings, new_cfg)
    log.info("Wrote %s", settings.auth_config_path)

    remember_account(settings, result.viewer_id, password, True, result.name)
    return result
