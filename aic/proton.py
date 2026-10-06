"""Linux capture support: drive frida into the game's Proton/Wine session.

On Windows the game is a native process and ``frida.attach`` reaches it
directly. On Linux the game is a Windows PE run under Proton (Wine) inside a
pressure-vessel container, so the same agent cannot be loaded from a host-side
frida - the ``GameAssembly.dll`` module and its unitytls vtable only exist in
the Wine view of the world.

The port keeps the agent identical and moves *frida itself* into that world.
Proton's ``PROTON_REMOTE_DEBUG_CMD`` makes it run ``wine <cmd>`` in the game's
own session (same prefix, same container, same wineserver) alongside the game
(see the Proton ``run()`` method). We point that at a Windows build of
``frida-server.exe``; it lands next to the game where it can enumerate and
inject it, and listens on a TCP port. pressure-vessel shares the host network
namespace, so the host-side frida client reaches that port over loopback and
loads the unchanged agent through it.

Because the server has to be injected *at launch*, Linux capture always owns the
launch: it cannot attach to a game the user started by hand (that instance has
no server in it).
"""

from __future__ import annotations

import glob
import os
import shutil
import subprocess
import time
from pathlib import Path

from .logging_setup import get

log = get("proton")

APP_ID = "3224770"
GAME_EXE = "UmamusumePrettyDerby.exe"
GAME_INSTALL_DIR = "UmamusumePrettyDerby"
#: Loopback port frida-server listens on inside the prefix; reachable from the
#: host because pressure-vessel shares the network namespace.
FRIDA_PORT = 27042


class ProtonError(RuntimeError):
    """A Linux/Proton precondition the user can fix (paths, missing server)."""


# --------------------------------------------------------------------------- #
# path discovery
# --------------------------------------------------------------------------- #

def _env(*names: str) -> str | None:
    for n in names:
        v = os.environ.get(n)
        if v:
            return v
    return None


def steam_root() -> Path:
    """The Steam install root."""
    override = _env("AIC_STEAM_ROOT", "STEAM_ROOT")
    candidates = [override] if override else [
        "~/.local/share/Steam",
        "~/.steam/steam",
        "~/.steam/root",
    ]
    for c in candidates:
        p = Path(c).expanduser()
        if (p / "steamapps").is_dir():
            return p.resolve()
    raise ProtonError(
        "Steam root not found. Set AIC_STEAM_ROOT to the directory that "
        "contains 'steamapps' (e.g. ~/.local/share/Steam).")


def prefix_dir(steam: Path) -> Path:
    p = steam / "steamapps" / "compatdata" / APP_ID
    if not (p / "pfx").is_dir():
        raise ProtonError(
            f"Proton prefix for app {APP_ID} not found at {p}. Launch the game "
            "through Steam once so Proton creates its prefix.")
    return p


def game_exe_path(steam: Path) -> Path:
    # The library holding the game may differ from the main Steam root.
    libs = [steam]
    override = _env("AIC_GAME_LIBRARY")
    if override:
        libs.insert(0, Path(override).expanduser())
    for lib in libs:
        p = lib / "steamapps" / "common" / GAME_INSTALL_DIR / GAME_EXE
        if p.is_file():
            return p.resolve()
    raise ProtonError(
        f"{GAME_EXE} not found under {steam}/steamapps/common/{GAME_INSTALL_DIR}. "
        "Set AIC_GAME_LIBRARY if the game is on another Steam library drive.")


def detect_proton(steam: Path, prefix: Path) -> Path:
    """The Proton the game actually uses.

    Preference: the tool named in the prefix's ``config_info`` (the Proton that
    last set up this prefix), matched against every install location; then an
    explicit override; then the newest Proton found.
    """
    override = _env("AIC_PROTON")
    if override:
        p = Path(override).expanduser()
        if p.name != "proton":
            p = p / "proton"
        if p.is_file():
            return p.resolve()
        raise ProtonError(f"AIC_PROTON does not point at a proton script: {p}")

    wanted = ""
    try:
        wanted = (prefix / "config_info").read_text().splitlines()[0].strip()
    except (OSError, IndexError):
        pass

    search_globs = [
        str(steam / "compatibilitytools.d" / "*" / "proton"),
        str(steam / "steamapps" / "common" / "Proton*" / "proton"),
        str(Path("~/.steam/root/compatibilitytools.d").expanduser() / "*" / "proton"),
    ]
    found: list[Path] = []
    for g in search_globs:
        found.extend(Path(p) for p in glob.glob(g))
    if not found:
        raise ProtonError(
            "No Proton install found. Set AIC_PROTON to the Proton directory "
            "the game uses (the one under compatibilitytools.d or steamapps/common).")

    if wanted:
        for p in found:
            # version file is "<timestamp> <name>"; dir name also carries it.
            token = ""
            try:
                token = (p.parent / "version").read_text().split()[-1].strip()
            except (OSError, IndexError):
                pass
            if token == wanted or p.parent.name.startswith(wanted):
                return p.resolve()
        log.warning("Proton '%s' from config_info not found; falling back.", wanted)

    found.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return found[0].resolve()


def slr_entry_point(steam: Path) -> Path:
    """The Steam Linux Runtime entry point that sets up the container.

    Proton (steamrt) must run inside its pressure-vessel container; the entry
    point is what enters it. Skipping it runs Proton bare and it fails to find
    the runtime libraries.
    """
    override = _env("AIC_SLR_ENTRY_POINT")
    if override and Path(override).expanduser().is_file():
        return Path(override).expanduser().resolve()
    matches = glob.glob(str(steam / "steamapps" / "common" / "SteamLinuxRuntime*" / "_v2-entry-point"))
    if not matches:
        raise ProtonError(
            "SteamLinuxRuntime entry point not found. Install the Steam Linux "
            "Runtime (Proton pulls it in as a dependency), or set "
            "AIC_SLR_ENTRY_POINT.")
    matches.sort(reverse=True)  # prefer the newest runtime (…_4 over …_soldier)
    return Path(matches[0]).resolve()


def frida_server_exe(frida_version: str) -> Path:
    """The Windows frida-server whose version matches the host frida client.

    The client and server must be the same frida version. We name the bundled
    binary with its version and refuse a mismatch rather than fail cryptically
    at connect time.
    """
    override = _env("AIC_FRIDA_SERVER")
    project_root = Path(__file__).resolve().parent.parent
    candidates = []
    if override:
        candidates.append(Path(override).expanduser())
    candidates += [
        project_root / "tools" / f"frida-server-{frida_version}-windows-x86_64.exe",
        project_root / "tools" / "frida-server.exe",
    ]
    for c in candidates:
        if c.is_file():
            return c.resolve()
    url = (f"https://github.com/frida/frida/releases/download/{frida_version}/"
           f"frida-server-{frida_version}-windows-x86_64.exe.xz")
    raise ProtonError(
        f"frida-server.exe (windows x86_64, v{frida_version}) not found.\n"
        f"  Download it to tools/frida-server-{frida_version}-windows-x86_64.exe:\n"
        f"    curl -L {url} | unxz > "
        f"tools/frida-server-{frida_version}-windows-x86_64.exe\n"
        f"  It must match the installed frida client ({frida_version}).")


# --------------------------------------------------------------------------- #
# process control
# --------------------------------------------------------------------------- #

def game_is_running() -> bool:
    try:
        out = subprocess.run(["pgrep", "-f", GAME_EXE],
                             capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return bool(out.strip())


def kill_game() -> None:
    # Kill the game, its Proton chain, and the injected frida-server - a stale
    # server left holding the port would shadow the next capture's fresh one.
    for pattern in (GAME_EXE,
                    f"proton waitforexitandrun.*{GAME_EXE}",
                    "frida-server.exe"):
        try:
            subprocess.run(["pkill", "-9", "-f", pattern],
                          capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            pass


# --------------------------------------------------------------------------- #
# launch with frida-server injected
# --------------------------------------------------------------------------- #

def launch_instrumented(frida_version: str, port: int = FRIDA_PORT) -> subprocess.Popen:
    """Launch the game through Proton with frida-server injected into its session.

    Returns the launcher process. frida-server.exe is copied into the prefix's
    ``drive_c`` (a path guaranteed to be mounted inside the container - unlike
    ``/tmp``, which pressure-vessel makes private) and started via
    ``PROTON_REMOTE_DEBUG_CMD`` with a POSIX-absolute path (Proton mangles a
    first token that is not POSIX-absolute).
    """
    steam = steam_root()
    prefix = prefix_dir(steam)
    proton = detect_proton(steam, prefix)
    entry = slr_entry_point(steam)
    game = game_exe_path(steam)
    server_src = frida_server_exe(frida_version)

    server_dst = prefix / "pfx" / "drive_c" / "frida-server.exe"
    shutil.copyfile(server_src, server_dst)

    env = dict(os.environ)
    env.update({
        "STEAM_COMPAT_CLIENT_INSTALL_PATH": str(steam),
        "STEAM_COMPAT_DATA_PATH": str(prefix),
        "STEAM_COMPAT_INSTALL_PATH": str(game.parent),
        "STEAM_COMPAT_LIBRARY_PATHS": str(steam / "steamapps"),
        "STEAM_COMPAT_MOUNTS": str(entry.parent),
        "STEAM_COMPAT_TOOL_PATHS": f"{proton.parent}:{entry.parent}",
        "STEAM_COMPAT_APP_ID": APP_ID,
        "SteamAppId": APP_ID,
        "SteamGameId": APP_ID,
        "STEAM_COMPAT_FLAGS": "search-cwd",
        "PROTON_REMOTE_DEBUG_CMD": f"{server_dst} -l 0.0.0.0:{port}",
    })

    log.info("Launching %s under Proton with frida-server injected...", GAME_EXE)
    log.debug("proton=%s entry=%s prefix=%s", proton, entry, prefix)
    return subprocess.Popen(
        [str(entry), "--verb=waitforexitandrun", "--",
         str(proton), "waitforexitandrun", str(game)],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


# --------------------------------------------------------------------------- #
# host-side frida connection
# --------------------------------------------------------------------------- #

def connect_and_attach(deadline: float, port: int = FRIDA_PORT):
    """Wait for the injected frida-server, then attach to the game.

    Returns ``(session, device)``. Raises ProtonError if the server never comes
    up or the game process never appears in its view before the deadline.
    """
    import frida

    device = None
    while time.time() < deadline:
        try:
            device = frida.get_device_manager().add_remote_device(f"127.0.0.1:{port}")
            device.enumerate_processes()  # forces a real round-trip
            break
        except Exception:  # noqa: BLE001 - frida raises transport errors many ways
            device = None
            time.sleep(1)
    if device is None:
        raise ProtonError(
            f"frida-server never answered on 127.0.0.1:{port}. Proton may not "
            "have started it (check that this Proton honours "
            "PROTON_REMOTE_DEBUG_CMD).")

    while time.time() < deadline:
        target = next((p for p in device.enumerate_processes()
                       if p.name.lower() == GAME_EXE.lower()), None)
        if target is not None:
            log.info("Attaching to %s (wine pid %s) via injected frida-server.",
                     target.name, target.pid)
            return device.attach(target.pid), device
        time.sleep(1)
    raise ProtonError(
        f"{GAME_EXE} did not appear in frida-server's process list before the "
        "timeout. Make sure the game reaches the title screen.")
