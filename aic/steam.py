"""A persistent Steam session, so one login serves a whole run.

Steam cancels an app session ticket when the process that owns it exits. Icarus'
``get_ticket`` spawns node, mints a ticket and returns - so the ticket starts
dying immediately, and every command needs a brand new **Steam login**. At any
real rotation rate that trips Steam Guard.

This keeps the ``steam-user`` process alive instead. The ticket is minted inside
the ``loggedOn`` handler and stays valid for as long as the process lives, so one
login covers the whole run no matter how many accounts are switched.

Ported from uma-reroll-farm's ``tools/steam_session.py``, with two additions:

* the **refresh token** Steam emits after a 2FA login is persisted, so later
  runs log in with no password and no code;
* Steam Guard is only prompted for interactively when there is a terminal to
  prompt on - otherwise it raises, rather than hanging a background job.
"""

from __future__ import annotations

import atexit
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from .logging_setup import get

log = get("steam")

APP_ID = 3224770

#: Node driver. Differs from Icarus' one-shot generator in that it never exits:
#: the live connection to Steam is what keeps the ticket valid.
PERSISTENT_JS = r"""
const SteamUser = require("steam-user");
const readline = require("readline");

function out(o) { process.stdout.write(JSON.stringify(o) + "\n"); }

const args = process.argv.slice(2);
let username = "", password = "", appid = 3224770, code = "", refreshToken = "";
for (let i = 0; i < args.length; i++) {
  if (args[i] === "--username") username = args[++i];
  else if (args[i] === "--password") password = args[++i];
  else if (args[i] === "--appid") appid = parseInt(args[++i]);
  else if (args[i] === "--code") code = args[++i];
  else if (args[i] === "--refresh-token") refreshToken = args[++i];
}

if (!refreshToken && (!username || !password)) {
  out({ type: "error", message: "missing username/password" });
  process.exit(1);
}

const client = new SteamUser();

// Emitted after a successful login; storing it lets later runs skip 2FA.
client.on("refreshToken", (t) => out({ type: "refresh_token", token: t }));

let opts;
if (refreshToken) {
  opts = { refreshToken: refreshToken };
} else {
  opts = { accountName: username, password: password };
  if (code) opts.twoFactorCode = code;
}
client.logOn(opts);

let guardCallback = null;
client.on("steamGuard", (domain, cb, lastCodeWrong) => {
  guardCallback = cb;
  out({ type: "guard_needed", domain: domain || null, last_code_wrong: !!lastCodeWrong });
});

const rl = readline.createInterface({ input: process.stdin });
rl.on("line", (line) => {
  const v = line.trim();
  if (guardCallback && v) {
    const cb = guardCallback;
    guardCallback = null;
    cb(v);
  }
});

client.on("error", (err) => { out({ type: "error", message: err.message }); process.exit(1); });

client.on("loggedOn", () => {
  // createAuthSessionTicket needs the ownership ticket and a GC token, which
  // arrive after loggedOn - hence marking the game as played, and the retries.
  client.setPersona(SteamUser.EPersonaState.Online);
  client.gamesPlayed([appid]);

  let attempts = 0;
  const tryTicket = () => {
    attempts++;
    client.createAuthSessionTicket(appid, (err, ticket) => {
      if (err) {
        if (attempts < 5) { setTimeout(tryTicket, 5000); return; }
        out({ type: "error", message: "ticket: " + err.message });
        process.exit(1);
        return;
      }
      const buf = Buffer.isBuffer(ticket) ? ticket : (ticket.sessionTicket || ticket);
      out({
        type: "ready",
        steam_id: client.steamID.getSteamID64(),
        session_ticket: Buffer.from(buf).toString("hex").toUpperCase()
      });
    });
  };
  setTimeout(tryTicket, 5000);
});
"""


class SteamGuardRequired(RuntimeError):
    """Steam wants a 2FA code and there is no way to supply one."""


class SteamSession:
    """A live ``steam-user`` process holding a valid app session ticket."""

    def __init__(self, node_dir: Path, username: str = "", password: str = "",
                 code: str = "", refresh_token: str = "", appid: int = APP_ID,
                 timeout: int = 120):
        self.node_dir = str(node_dir)
        self.username = username
        self.password = password
        self.code = code
        self.refresh_token_in = refresh_token
        #: Whatever Steam handed back this login; worth persisting.
        self.refresh_token = ""
        self.appid = appid
        self.timeout = timeout
        self.proc: subprocess.Popen | None = None
        self.steam_id = ""
        self.ticket = ""
        self.started_at = 0.0
        self._lines: list[dict] = []
        self._lock = threading.Lock()

    # -- lifecycle --------------------------------------------------------- #

    @property
    def alive(self) -> bool:
        return bool(self.proc and self.proc.poll() is None and self.ticket)

    def _check_deps(self) -> None:
        if not shutil.which("node"):
            raise RuntimeError(
                "node is not installed or not on PATH - needed to mint Steam tickets")
        if not os.path.exists(os.path.join(self.node_dir, "node_modules")):
            npm = shutil.which("npm") or shutil.which("npm.cmd")
            if not npm:
                raise RuntimeError(
                    f"node_modules missing and npm not found; run `npm install` "
                    f"in {self.node_dir}")
            subprocess.run([npm, "install", "--silent"], check=True, cwd=self.node_dir)

    def _read_loop(self) -> None:
        assert self.proc and self.proc.stdout
        for raw in self.proc.stdout:
            line = raw.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if msg.get("type") == "refresh_token":
                self.refresh_token = msg.get("token") or ""
                continue
            with self._lock:
                self._lines.append(msg)

    def _wait_for(self, kinds: set[str], timeout: float) -> dict | None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                for i, msg in enumerate(self._lines):
                    if msg.get("type") in kinds:
                        return self._lines.pop(i)
            if self.proc and self.proc.poll() is not None:
                break
            time.sleep(0.05)
        return None

    def start(self) -> "SteamSession":
        self._check_deps()
        # "--dummy" pads argv: `node -e` leaves no script path, so the JS's
        # slice(2) would otherwise eat the first real argument.
        cmd = ["node", "-e", PERSISTENT_JS, "--", "--dummy",
               "--username", self.username, "--password", self.password,
               "--appid", str(self.appid)]
        if self.code:
            cmd += ["--code", self.code]
        if self.refresh_token_in:
            cmd += ["--refresh-token", self.refresh_token_in]

        self.proc = subprocess.Popen(
            cmd, cwd=self.node_dir, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, bufsize=1,
        )
        threading.Thread(target=self._read_loop, daemon=True).start()

        while True:
            msg = self._wait_for({"ready", "guard_needed", "error"}, self.timeout)
            if not msg:
                self.close()
                raise RuntimeError("Steam: timed out waiting for login/ticket")

            if msg["type"] == "guard_needed":
                self._answer_guard(msg)
                continue

            if msg["type"] == "error":
                detail = msg.get("message") or "unknown"
                self.close()
                if "refresh" in detail.lower() or "token" in detail.lower():
                    raise RuntimeError(f"Steam rejected the stored refresh token: {detail}")
                raise RuntimeError(f"Steam error: {detail}")

            self.steam_id = str(msg.get("steam_id") or "")
            self.ticket = msg.get("session_ticket") or ""
            self.started_at = time.time()
            log.info("Steam session live (ticket held open; it stays valid while "
                     "this command runs).")
            return self

    def _answer_guard(self, msg: dict) -> None:
        if msg.get("last_code_wrong"):
            log.warning("Steam says the previous code was wrong.")
        if self.code:
            # Already supplied up front and still refused; do not loop.
            self.close()
            raise SteamGuardRequired("Steam rejected the supplied Guard code")
        if not (sys.stdin and sys.stdin.isatty()):
            self.close()
            raise SteamGuardRequired(
                "Steam Guard wants a code and there is no terminal to ask on.\n"
                "  Supply one with --steam-code, or store the 2FA secret "
                "(account.bat steam-secret <shared_secret>)."
            )
        where = msg.get("domain")
        source = f"emailed to @{where}" if where else "from your authenticator"
        try:
            code = input(f"  Steam Guard code ({source}): ")
        except EOFError:
            self.close()
            raise SteamGuardRequired(
                "Steam asked for a Guard code but stdin gave EOF - run this "
                "directly in a terminal, or pass --steam-code <code>."
            ) from None
        if not code.strip():
            self.close()
            raise SteamGuardRequired("no Guard code entered")
        assert self.proc and self.proc.stdin
        self.proc.stdin.write(code.strip() + "\n")
        self.proc.stdin.flush()

    def close(self) -> None:
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=5)
            except (OSError, subprocess.SubprocessError):
                try:
                    self.proc.kill()
                except OSError:
                    pass
        self.proc = None

    def __enter__(self) -> "SteamSession":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.close()


# --------------------------------------------------------------------------- #
# process-wide session
# --------------------------------------------------------------------------- #

_SESSION: SteamSession | None = None


def _shutdown() -> None:
    global _SESSION
    if _SESSION is not None:
        _SESSION.close()
        _SESSION = None


atexit.register(_shutdown)


def acquire(node_dir: Path, cfg: dict, code: str = "") -> SteamSession:
    """The live session for this process, starting one if needed.

    Reused across every call in a run: that is the whole point, since the ticket
    only stays valid while the session is open. A stored refresh token is tried
    first so no password or 2FA is involved.
    """
    global _SESSION
    if _SESSION is not None and _SESSION.alive:
        return _SESSION

    refresh_token = cfg.get("steam_refresh_token") or ""
    username = cfg.get("steam_username") or ""
    password = cfg.get("steam_password_seed") or ""

    if not refresh_token and not (username and password):
        raise RuntimeError(
            "no Steam credentials: auth_config needs steam_username + "
            "steam_password_seed, or a stored steam_refresh_token")

    attempts = []
    if refresh_token:
        attempts.append(("stored refresh token", {"refresh_token": refresh_token}))
    if username and password:
        attempts.append(("password login", {"username": username,
                                            "password": password, "code": code}))

    last: Exception | None = None
    for label, kwargs in attempts:
        log.info("Starting Steam session via %s...", label)
        session = SteamSession(node_dir, **kwargs)
        try:
            _SESSION = session.start()
        except SteamGuardRequired:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("%s failed: %s", label, exc)
            last = exc
            continue
        return _SESSION

    raise RuntimeError(f"could not establish a Steam session: {last}")


def current() -> SteamSession | None:
    """The live session, if this process is holding one."""
    return _SESSION if has_live_session() else None


def has_live_session() -> bool:
    """Whether this process is holding a Steam session open.

    NOT the same as "the ticket still works". :meth:`SteamSession.alive` checks
    only that the node process is running; Steam can and does cancel the ticket
    underneath a perfectly healthy process. MEASURED 2026-09-02: one session
    minted a ticket, served 16 signups, then had it cancelled - and because this
    kept reporting the session live, refresh_steam_ticket went on copying the
    same dead ticket into every request for 216 consecutive 391s, with no way
    back short of restarting the unit.

    So a 391 from the server is the ONLY reliable signal that this ticket is
    finished - see :func:`aic.auth.note_dead_ticket`, which drops the session so
    the next call mints a new one.
    """
    return _SESSION is not None and _SESSION.alive


def session_age() -> float:
    live = current()
    return time.time() - live.started_at if live else float("inf")


def close() -> None:
    """Drop the session (and with it the ticket). Called at process exit."""
    _shutdown()
