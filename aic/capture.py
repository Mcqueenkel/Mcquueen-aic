"""Capture credentials from the running game with frida.

``auth_key`` cannot be derived offline - it is the account's own credential,
bound to the ``udid`` in the same request header. The only way to obtain it is
to watch the real client send it. This attaches to the game, hooks Unity's TLS
write, reassembles the outgoing HTTP POST and reads the wire envelope:

    <u32 header_len> | HEAD | sid(16) | udid(16) | rand(32) [| auth_key(48)]
                     | AES-CBC(msgpack payload) | key(32)

The header gives ``udid`` and ``auth_key``; decrypting the body with that udid
gives the Steam ticket, ``steam_id`` and the device fields. Headers alongside it
carry ``APP-VER`` / ``RES-VER``.

The agent is uma-reroll-farm's, which handles the unauthenticated case too: a
Steam account with no linked game account only sends pre_signup/signup, with no
auth_key, but the Steam ticket is in the body either way.

``kill_after`` defaults to **False**: Steam cancels the app session ticket when
the game exits, so leaving the game open is what keeps the captured ticket
usable.
"""

from __future__ import annotations

import base64
import os
import struct
import subprocess
import time
from pathlib import Path

from .auth import GAME_PROCESS, game_is_running, load_auth_config, save_auth_config
from .config import Settings
from .logging_setup import get

log = get("capture")

APP_ID = "3224770"

#: Frida agent: hooks the unitytls write vtable entries and reassembles POSTs.
_AGENT_PATH = Path(__file__).resolve().parent.parent / "vendor" / "frida_agent.js"
#: Third-party (Icarus) code, kept in vendor/ like uma_api - not in git. Empty when
#: missing, so importing this module never fails; capture() says what to copy in.
try:
    FRIDA_AGENT = _AGENT_PATH.read_text(encoding="utf-8")
except OSError:
    FRIDA_AGENT = ""


class CaptureError(RuntimeError):
    pass


def launch_game() -> None:
    if os.name != "nt":
        # On Linux the game is a Proton/Wine process and frida-server must be
        # injected *at launch*; that is handled together with the attach in
        # capture(), not here. See aic/proton.py.
        raise CaptureError("use capture() on Linux; launch and attach are coupled")
    os.startfile(f"steam://rungameid/{APP_ID}")  # noqa: S606


def kill_game() -> None:
    if os.name != "nt":
        from . import proton
        proton.kill_game()
        return
    try:
        subprocess.run(["taskkill", "/IM", GAME_PROCESS, "/F"],
                       capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        pass


def udid_from_envelope(header_hex: str) -> list[str]:
    """Candidate udids from the binary header.

    Layout: ``HEAD | sid(16) | udid(16) | rand(32) [| auth(48)]`` - so with an
    auth_key the udid sits at ``[-96:-80]``, and without one at ``[-48:-32]``.
    Both are returned because an unauthenticated request shifts everything.
    """
    out = []
    for lo, hi in ((-96, -80), (-48, -32)):
        chunk = header_hex[len(header_hex) + 2 * lo:len(header_hex) + 2 * hi]
        if len(chunk) == 32:
            out.append(f"{chunk[:8]}-{chunk[8:12]}-{chunk[12:16]}-"
                       f"{chunk[16:20]}-{chunk[20:]}")
    return out


def decrypt_request_body(body_b64: str, udid: str) -> dict:
    """Decrypt a captured request body into its msgpack payload.

    Mirrors ``uma_api.client.pack``: the AES key travels in the clear as the
    last 32 bytes and the IV comes from the udid.
    """
    import msgpack
    from Crypto.Cipher import AES
    from Crypto.Util.Padding import unpad
    from uma_api.client import get_iv

    raw = base64.b64decode(body_b64)
    header_len = struct.unpack("<I", raw[:4])[0]
    rest = raw[4 + header_len:]
    key, cipher = rest[-32:], rest[:-32]
    plain = unpad(AES.new(key, AES.MODE_CBC, get_iv(udid)).decrypt(cipher), 16)
    size = struct.unpack("<I", plain[:4])[0]
    return msgpack.unpackb(plain[4:4 + size], raw=False, strict_map_key=False)


def _acquire_session(frida, deadline: float, launch: bool):
    """Attach to the game, returning ``(session, device)``.

    Windows: attach directly to the native process. Linux: the game is a
    Proton/Wine process, so frida-server has to be injected into its session at
    launch and reached over TCP - which means Linux capture owns the launch and
    cannot attach to a hand-started game (that one has no server in it). See
    aic/proton.py.
    """
    if os.name == "nt":
        if launch and not game_is_running():
            log.info("Launching the game through Steam...")
            launch_game()
        elif game_is_running():
            log.info("Game already running; attaching to it.")
        while time.time() < deadline:
            try:
                return frida.attach(GAME_PROCESS), None
            except Exception:  # noqa: BLE001 - frida raises many process errors
                time.sleep(1)
        raise CaptureError(
            f"{GAME_PROCESS} did not appear within the timeout. Make sure Steam "
            "launches the game and you reach the title screen.")

    from . import proton
    if launch:
        # The server is injected at launch, so an already-running instance can
        # only be used if it was started instrumented. Try the existing server
        # first; relaunch (killing the current session) only if none answers.
        if game_is_running():
            try:
                return proton.connect_and_attach(min(deadline, time.time() + 6))
            except proton.ProtonError:
                log.info("Running game has no injected server; relaunching it.")
                proton.kill_game()
                time.sleep(2)
        proton.launch_instrumented(frida.__version__)
    elif not game_is_running():
        raise CaptureError(
            "no game running and --no-launch given. On Linux capture must launch "
            "the game itself to inject frida-server; drop --no-launch.")
    try:
        return proton.connect_and_attach(deadline)
    except proton.ProtonError as exc:
        raise CaptureError(str(exc)) from exc


def capture(settings: Settings, timeout: int = 180, launch: bool = True,
            kill_after: bool = False) -> dict:
    """Attach to the game and write fresh credentials into auth_config.

    ``kill_after=False`` on purpose: Steam cancels the app session ticket when
    the game exits, so leaving it running is what keeps the captured ticket
    usable afterwards.
    """
    from . import bootstrap

    bootstrap.bind(settings)  # capture needs uma_api.client for get_iv
    try:
        import frida
    except ImportError as exc:
        hint = ("pip install frida  (into this interpreter's environment)"
                if os.name != "nt" else
                "It ships with Icarus' requirements; run this with the Icarus "
                "virtualenv interpreter.")
        raise CaptureError(f"frida is not installed. {hint}") from exc

    # A first capture has nothing to start from - it is what writes the config.
    # Everything the client needs (viewer_id, udid, auth_key, the device
    # fingerprint fields, app/res versions, the Steam ticket) comes out of the
    # captured request below, so an empty base is complete on its own; an
    # existing config is only carried over to preserve unrelated fields such as
    # the stored Steam refresh token.
    previous = load_auth_config(settings, missing_ok=True)
    if not previous:
        log.info("No existing credentials for profile %r - capturing from "
                 "scratch.", settings.profile)
    deadline = time.time() + timeout

    session, _device = _acquire_session(frida, deadline, launch)

    captured: dict = {}

    def on_message(message, _data):
        if message.get("type") == "error":
            log.debug("frida: %s", message.get("description"))
            return
        payload = message.get("payload") or {}
        if payload.get("type") != "creds":
            return
        if not (payload.get("app_ver") and payload.get("res_ver")):
            return
        # An authenticated request is worth more (it carries viewer_id +
        # auth_key); never downgrade to an unauthenticated one.
        if captured.get("auth_key_len") == 48 and payload.get("auth_key_len") != 48:
            return
        captured.clear()
        captured.update(payload)

    try:
        if not FRIDA_AGENT:
            raise CaptureError(f"{_AGENT_PATH} is missing - copy it in from an Icarus checkout")
        script = session.create_script(FRIDA_AGENT)
        script.on("message", on_message)
        script.load()
        log.info("Hook loaded. Waiting for the game to make a request "
                 "(up to %ds)...", int(deadline - time.time()))
        first_unauth = None
        while time.time() < deadline:
            if captured.get("body"):
                if captured.get("auth_key_len") == 48:
                    break
                # Give an authenticated request a moment to turn up before
                # settling for one without auth (still enough for the ticket).
                first_unauth = first_unauth or time.time()
                if time.time() - first_unauth > 20:
                    break
            time.sleep(0.4)
    finally:
        try:
            session.detach()
        except Exception:  # noqa: BLE001
            pass

    if not captured.get("body"):
        if kill_after:
            kill_game()
        raise CaptureError(
            "No request captured before the timeout. Reach the title screen "
            "(or tap through to the home screen) so the game talks to the API.")

    cfg = dict(previous)
    authenticated = captured.get("auth_key_len") == 48
    if authenticated:
        cfg.update({
            "viewer_id": captured.get("viewer_id"),
            "udid": captured.get("udid"),
            "auth_key": captured.get("auth_key"),
            "auth_key_len": captured.get("auth_key_len"),
        })
    else:
        log.warning("Captured an unauthenticated request - this Steam account "
                    "has no linked game account. The Steam ticket is usable, "
                    "but viewer_id/auth_key are unchanged.")
    cfg.update({
        "app_ver": captured.get("app_ver"),
        "res_ver": captured.get("res_ver"),
        "endpoint": captured.get("endpoint"),
        "body": captured.get("body"),
    })

    # Without an auth_key the udid sits at a different offset, so try both.
    payload = None
    resolved_udid = None
    for candidate in [captured["udid"]] + [
            u for u in udid_from_envelope(captured.get("header_hex", ""))
            if u != captured["udid"]]:
        try:
            payload = decrypt_request_body(captured["body"], candidate)
            resolved_udid = candidate
            break
        except Exception:  # noqa: BLE001
            continue
    if payload is None:
        if kill_after:
            kill_game()
        raise CaptureError("Request captured but its body could not be decrypted.")
    if authenticated:
        cfg["udid"] = resolved_udid

    for key in ("steam_id", "steam_session_ticket", "device_id", "device_name",
                "graphics_device_name", "ip_address", "platform_os_version",
                "locale"):
        value = payload.get(key)
        if value:
            cfg[key] = str(value) if key == "steam_id" else value

    # The payload's viewer_id is authoritative. The ViewerID *header* can carry
    # a per-response session alias; using that gives 709 -> 501 on the next
    # start_session.
    if payload.get("viewer_id"):
        cfg["viewer_id"] = payload["viewer_id"]

    now = int(time.time())
    cfg["captured_at"] = now
    cfg["steam_ticket_ts"] = now
    cfg["captured_endpoint"] = captured.get("endpoint")
    save_auth_config(settings, cfg)

    log.info("Captured from %s: viewer %s, app_ver %s, res_ver %s",
             captured.get("endpoint"), cfg.get("viewer_id"),
             cfg.get("app_ver"), cfg.get("res_ver"))

    if kill_after:
        kill_game()
        log.warning("Game closed - Steam has cancelled the captured ticket. "
                    "A new one will be minted on the next command.")
    else:
        log.info("Leaving the game running so the Steam ticket stays valid.")
    return cfg
