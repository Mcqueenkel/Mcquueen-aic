import base64
import json
import os
import re
import threading
import time
import uuid
from curl_cffi import requests
import hashlib
import random
import struct
import msgpack
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad, unpad
import subprocess
import platform
import socket
import shutil
from datetime import datetime
from pathlib import Path


class StateRecoveryError(Exception):
    pass


class ApiError(Exception):
    """Server API error carrying the structured result code.

    Callers should branch on ``err.result_code`` instead of substring-matching
    the message (``"102" in str(e)``), which misfires when the message text
    embeds ids/turns. The message keeps the legacy ``"API error {rc} on {ep}"``
    format so any remaining string checks still work during migration.
    """

    def __init__(self, result_code, endpoint, message, response=None):
        super().__init__(message)
        self.result_code = int(result_code) if result_code is not None else None
        self.endpoint = endpoint
        self.response = response


def error_code(exc):
    """Best-effort structured result code for any exception: the ApiError
    attribute when present, else None (callers keep their string fallback)."""
    return getattr(exc, "result_code", None)


BASE_URL = "https://api.games.umamusume.com/umamusume/"

#: HTTP statuses worth repeating the request for. These come from the edge, not
#: the game: the body is an HTML error page instead of msgpack, so the call never
#: reached game logic and a retry cannot double-apply anything. 4xx is excluded -
#: those are our own bad request and repeating them just wastes time.
_RETRY_HTTP_STATUSES = frozenset({429, 500, 502, 503, 504})
DIR = str(Path(__file__).resolve().parent.parent)
LAST_TICKET_GEN_RESULT = None
LAST_SAVED_CONFIG = None

#: scenario_id -> career endpoint prefix. THE routing source of truth for the
#: client. Mirrors the strategy classes' api_prefix trait in
#: career_bot/scenarios/ (the runner warns at start if the two disagree) -
#: update BOTH when adding a scenario.
SCENARIO_API_PREFIXES = {
    1: "single_mode",  # URA Finals
    2: "single_mode_team",  # Unity Cup
    3: "single_mode_live",  # Grand Live (Grand Concert)
    4: "single_mode_free",  # MANT
}
DEFAULT_API_PREFIX = "single_mode"

# Central logging (career_bot.logging_utils); falls back to a bare logger so
# uma_api stays importable standalone.
try:
    from career_bot.logging_utils import get_logger

    log = get_logger("client")
except Exception:  # noqa: BLE001
    import logging

    log = logging.getLogger("uma.client")

# Plain-English error lines for the Logs console (career_bot.error_help);
# fall back to the raw technical message if the module is unavailable.
try:
    from career_bot.error_help import explain_api_error, describe_endpoint
except Exception:  # noqa: BLE001
    def explain_api_error(rc, ep=None):  # noqa: D103
        return None

    def describe_endpoint(ep):  # noqa: D103
        return str(ep or "a request")


def runtime_output_root():
    override = os.environ.get("UMA_RUNTIME_DIR")
    if override:
        return Path(override).expanduser().resolve()

    here = Path(__file__).resolve()
    for candidate in here.parents:
        if (candidate / ".git").exists():
            return candidate / "uma_runtime"
    return here.parent.parent.parent / "uma_runtime"


TRACE_DIR = runtime_output_root() / "trace_logs"

TICKET_GEN_JS = """const SteamUser = require("steam-user");
const fs = require("fs");
const path = require("path");

const args = process.argv.slice(2);
let username = "";
let password = "";
let appid = 3224770;
let code = "";
let sharedSecret = "";
let dataDir = "./steam_auth_data";

for (let i = 0; i < args.length; i++) {
  if (args[i] === "--username") username = args[++i];
  else if (args[i] === "--password") password = args[++i];
  else if (args[i] === "--appid") appid = parseInt(args[++i]);
  else if (args[i] === "--code") code = args[++i];
  else if (args[i] === "--shared-secret") sharedSecret = args[++i];
  else if (args[i] === "--data-dir") dataDir = args[++i];
}

if (!username) {
  process.stderr.write(
    "Usage: node ticket_gen.js --username X [--password Y] [--code Z] [--shared-secret S]\\n"
  );
  process.exit(1);
}

if (!fs.existsSync(dataDir)) {
  fs.mkdirSync(dataDir, { recursive: true });
}

const tokenFile = path.join(dataDir, username + '_refresh.txt');

const client = new SteamUser({ dataDirectory: dataDir });

let loginOpts = {};

let usingRefreshToken = false;
if (fs.existsSync(tokenFile)) {
  loginOpts.refreshToken = fs.readFileSync(tokenFile, 'utf8').trim();
  usingRefreshToken = true;
} else {
  loginOpts.accountName = username;
  loginOpts.password = password;
  if (code) {
    loginOpts.twoFactorCode = code;
    loginOpts.authCode = code;
  } else if (sharedSecret) {
    const SteamTotp = require('steam-totp');
    loginOpts.twoFactorCode = SteamTotp.generateAuthCode(sharedSecret);
  }
}

let isRetry = false;

function doLogon() {
  try {
    client.logOn(loginOpts);
  } catch (err) {
    handleLogonError(err);
  }
}

// EResults that mean "Steam is refusing us right now", NOT "these credentials
// are wrong": 2 Fail, 3 NoConnection, 20 ServiceUnavailable,
// 84 RateLimitExceeded, 87 AccountLoginDeniedThrottle.
const TRANSIENT_LOGON_ERRORS = [2, 3, 20, 84, 87];
// The SAME conditions by name. A real user report showed the bare stderr line
// "ERROR:AccountLoginDeniedThrottle" — i.e. the throttle arrived with no usable
// numeric eresult, so an eresult-only check would have missed it and gone on to
// delete the refresh token and retry with a password: the two things that turn a
// short throttle into a long one.
const TRANSIENT_LOGON_NAMES = [
  "accountlogindeniedthrottle",
  "ratelimitexceeded",
  "serviceunavailable",
  "noconnection",
  "tryanothercm",
];
let hasLoggedOn = false;

function handleLogonError(err) {
  const er = Number(err && err.eresult) || 0;
  const msg = String((err && (err.message || err.eresult)) || "");
  const named = TRANSIENT_LOGON_NAMES.indexOf(
    msg.toLowerCase().replace(/[^a-z]/g, "")
  ) !== -1;

  // Throttled or Steam is down. Bail out to Python with a distinct exit code so
  // it can back off. Critically: do NOT delete the refresh token and do NOT fall
  // back to a password logon — a password logon is the exact thing Steam
  // throttles, and dropping the refresh token turns a temporary throttle into a
  // permanent one ("Account login denied throttle/in").
  if (named || TRANSIENT_LOGON_ERRORS.indexOf(er) !== -1) {
    process.stderr.write("THROTTLED:" + er + ":" + msg + "\\n");
    process.exit(3);
  }

  // An error AFTER a successful logon is a disconnect, not bad credentials —
  // retrying the logon would just burn another attempt.
  if (hasLoggedOn) {
    process.stderr.write("ERROR:" + ((err && err.message) || "disconnected") + "\\n");
    process.exit(1);
  }

  if (usingRefreshToken) {
    usingRefreshToken = false;
    if (fs.existsSync(tokenFile)) {
      fs.unlinkSync(tokenFile);
    }
    if (password) {
      process.stderr.write("Refresh token invalid, falling back to password.\\n");
      loginOpts = {
        accountName: username,
        password: password
      };
      if (code) {
        loginOpts.twoFactorCode = code;
        loginOpts.authCode = code;
      } else if (sharedSecret) {
        const SteamTotp = require('steam-totp');
        loginOpts.twoFactorCode = SteamTotp.generateAuthCode(sharedSecret);
      }
      // Space the retries out — back-to-back logons look like a credential
      // attack and are what earns the throttle in the first place.
      setTimeout(doLogon, 5000);
      return;
    }
  }

  if (!isRetry && !code && password) {
    isRetry = true;
    loginOpts.password = password;
    setTimeout(doLogon, 5000);
  } else {
    process.stderr.write("ERROR:" + ((err && err.message) || "logon failed") + "\\n");
    process.exit(1);
  }
}

client.on("refreshToken", (token) => {
  fs.writeFileSync(tokenFile, token);
});

client.on("steamGuard", (domain, callback) => {
  process.stderr.write(
    "NEED_GUARD:" + (domain || "2fa") + "\\n"
  );
  process.exit(2);
});

client.on("error", handleLogonError);

client.on("loggedOn", () => {
  hasLoggedOn = true;
  process.stderr.write(
    "Logged in as " + client.steamID.getSteamID64() + "\\n"
  );
  client.createAuthSessionTicket(appid, (err, sessionTicket) => {
    if (err) {
      process.stderr.write("Ticket error: " + err.message + "\\n");
      process.exit(1);
    }
    const buf = Buffer.isBuffer(sessionTicket) ? sessionTicket : sessionTicket.sessionTicket || sessionTicket;
    const result = {
      steam_id: client.steamID.getSteamID64(),
      session_ticket: Buffer.from(buf).toString("hex").toUpperCase(),
    };
    process.stdout.write(JSON.stringify(result) + "\\n");
    process.stderr.write(
      "Ticket generated (" + Buffer.from(buf).length + " bytes)\\n"
    );
    setTimeout(() => process.exit(0), 1500);
  });
});

doLogon();
"""

SALT = b"co!=Y;(UQCGxJ_n82"
HEAD = bytes.fromhex(
    "6b20e2ab6c311330f761d737ce3f3025750850665eea58b6372f8d2f57501eb344bdb7270a9067f5b63cd61f152cfb986cbfbf7a"
)
SENSITIVE_ERROR_KEYS = {
    "auth_key",
    "steam_session_ticket",
    "sid",
    "udid",
    "device_id",
    "viewer_id",
}

# Credential / session material that must NEVER be persisted to trace files or
# the run report (account-takeover risk). Broader than SENSITIVE_ERROR_KEYS,
# which only governs one-line console error details.
CREDENTIAL_KEYS = {
    "auth_key",
    "steam_session_ticket",
    "steam_id",
    "sid",
    "udid",
    "device_id",
    "device_token",
    "viewer_id",
    "dmm_viewer_id",
    "dmm_onetime_token",
    "keychain",
    "ip_address",
    "steam_ticket",
}


def redact_credentials(value, key="", _depth=0):
    """Deep-copy that replaces credential/session values with "<redacted>",
    keeping all other data intact (no truncation) so trace files stay useful
    for protocol debugging without leaking account material."""
    if key in CREDENTIAL_KEYS and value not in (None, "", 0):
        return "<redacted>"
    if _depth > 40:
        return value
    if isinstance(value, dict):
        return {k: redact_credentials(v, k, _depth + 1) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_credentials(item, key, _depth + 1) for item in value]
    return value


def redact_for_console(value, key=""):
    if key in SENSITIVE_ERROR_KEYS:
        return "<redacted>"
    if isinstance(value, dict):
        return {k: redact_for_console(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_for_console(item, key) for item in value[:20]]
    if isinstance(value, str) and len(value) > 160:
        return value[:160] + "...<truncated>"
    return value


def format_api_error(ep, rc, res):
    details = {
        "endpoint": ep,
        "response_code": res.get("response_code"),
        "result_code": rc,
        "data_headers": redact_for_console(res.get("data_headers") or {}),
    }
    data = res.get("data")
    if isinstance(data, dict):
        interesting = {}
        for key in (
            "error_code",
            "error_message",
            "message",
            "result_code",
            "viewer_id",
            "current_turn",
            "chara_info",
            "single_mode_chara_light",
        ):
            if key in data:
                interesting[key] = data[key]
        if interesting:
            details["data"] = redact_for_console(interesting)
    elif data is not None:
        details["data"] = redact_for_console(data)
    return json.dumps(details, ensure_ascii=False, default=str)


def sm5(data):
    h = hashlib.md5()
    h.update(data)
    h.update(SALT)
    return h.digest()


def make_sid(vid, udid):
    return sm5((str(vid) + udid).encode())


def next_sid(sid):
    return sm5(sid.encode())


def gen_key():
    out = b""
    while len(out) < 32:
        out += format(random.randint(0, 65535), "x").encode()
    return out[:32]


def get_iv(udid):
    return udid.replace("-", "").lower()[:16].encode()


def get_raw_udid(udid):
    return bytes.fromhex(udid.replace("-", "").lower())


def pack(sid, udid_raw, auth, payload, udid):
    key = gen_key()
    p = msgpack.packb(payload, use_bin_type=True)
    body = (
        AES.new(key, AES.MODE_CBC, get_iv(udid)).encrypt(
            pad(struct.pack("<I", len(p)) + p, 16)
        )
        + key
    )
    h = HEAD + sid + udid_raw + os.urandom(32)
    if auth:
        h += auth
    return base64.b64encode(struct.pack("<I", len(h)) + h + body)


def unpack(text, udid):
    raw = base64.b64decode(text)
    key, cipher = raw[-32:], raw[:-32]
    p = unpad(AES.new(key, AES.MODE_CBC, get_iv(udid)).decrypt(cipher), 16)
    return msgpack.unpackb(
        p[4 : 4 + struct.unpack("<I", p[:4])[0]], raw=False, strict_map_key=False
    )


def get_gpu():
    if platform.system() != "Windows":
        raise RuntimeError(
            f"Unsupported OS: {platform.system()}. Only Windows is supported for PC info consistency."
        )

    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Video"
        ) as video_key:
            for i in range(winreg.QueryInfoKey(video_key)[0]):
                adapter_guid = winreg.EnumKey(video_key, i)
                adapter_path = (
                    rf"SYSTEM\CurrentControlSet\Control\Video\{adapter_guid}\0000"
                )
                try:
                    with winreg.OpenKey(
                        winreg.HKEY_LOCAL_MACHINE, adapter_path
                    ) as adapter_key:
                        value, _ = winreg.QueryValueEx(
                            adapter_key, "HardwareInformation.AdapterString"
                        )
                        if isinstance(value, bytes):
                            value = value.decode("utf-16-le", errors="ignore")
                        gpu_name = str(value).replace("\x00", "").strip()
                        if gpu_name:
                            return gpu_name
                except OSError:
                    continue
    except Exception as e:
        raise RuntimeError(f"Failed to fetch GPU info: {e}") from e

    raise RuntimeError("Failed to fetch GPU info: display adapter registry value empty")


def get_os():
    return f"Windows 11  ({platform.version()}) 64bit"


def get_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.connect(("8.8.8.8", 80))
    ip = s.getsockname()[0]
    s.close()
    return ip


def get_hwid(seed_string="default"):
    guid = str(uuid.uuid4()).lower()

    if platform.system() != "Windows":
        raise RuntimeError(
            f"Unsupported OS: {platform.system()}. Only Windows is supported for HWID consistency."
        )

    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\BIOS"
        ) as bios_key:
            device_name, _ = winreg.QueryValueEx(bios_key, "SystemProductName")
            device_name = str(device_name).strip()
            try:
                board_mfg, _ = winreg.QueryValueEx(bios_key, "BaseBoardManufacturer")
                if board_mfg:
                    device_name = f"{device_name} ({str(board_mfg).strip()})"
            except OSError:
                pass
        if not device_name:
            raise RuntimeError("System product name returned empty. Refusing to start.")

        machine_guid = ""
        try:
            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography"
            ) as crypto_key:
                machine_guid, _ = winreg.QueryValueEx(crypto_key, "MachineGuid")
        except OSError:
            pass

    except Exception as e:
        raise RuntimeError(
            f"Failed to fetch system product name: {e}. Refusing to start."
        )

    hardware_string = f"{device_name}_{machine_guid}_{seed_string}"
    device_id = hashlib.sha1(hardware_string.encode()).hexdigest()

    return {
        "device_name": device_name,
        "graphics_device_name": get_gpu(),
        "platform_os_version": get_os(),
        "ip_address": get_ip(),
        "udid": guid,
        "device_id": device_id,
    }


def check_deps():
    if not shutil.which("node"):
        raise Exception("node missing")
    if not os.path.exists(os.path.join(DIR, "node_modules")):
        # On Windows npm is npm.cmd; CreateProcess can't launch a bare "npm",
        # so resolve the real executable (raises a clear error if absent).
        npm = shutil.which("npm")
        if not npm:
            raise Exception("npm missing (install Node.js, then rerun)")
        subprocess.run([npm, "install", "--silent"], check=True, cwd=DIR)


class SteamThrottleError(Exception):
    """Steam refused the logon because it is rate-limiting us, not because the
    credentials are wrong (EResult 84 RateLimitExceeded / 87
    AccountLoginDeniedThrottle, and the transient 2/3/20 family).

    Retrying immediately makes it worse and can escalate a minutes-long throttle
    into an hours-long one, so this is always paired with the persistent backoff
    below.
    """

    def __init__(self, eresult=87, message=""):
        super().__init__(message or f"Steam login throttled (EResult {eresult})")
        self.eresult = int(eresult or 0)


def _ticket_cooldown_path():
    return runtime_output_root() / "steam_login_cooldown.json"


def steam_login_cooldown():
    """(seconds_remaining, strikes). Persisted, because the thing users do when
    a login fails is restart the app - an in-memory backoff would be erased by
    exactly the behaviour it needs to survive."""
    try:
        d = json.loads(_ticket_cooldown_path().read_text(encoding="utf-8"))
        return max(0.0, float(d.get("until", 0)) - time.time()), int(d.get("strikes", 0))
    except Exception:
        return 0.0, 0


def _note_ticket_throttle():
    """Record a throttle and return how long to wait. Exponential with jitter:
    60s, 2m, 4m, 8m, 16m, capped at 30m."""
    _, strikes = steam_login_cooldown()
    strikes = min(strikes + 1, 6)
    base = min(60.0 * (2 ** (strikes - 1)), 1800.0)
    delay = base * random.uniform(0.75, 1.25)
    try:
        path = _ticket_cooldown_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"until": time.time() + delay, "strikes": strikes}),
            encoding="utf-8",
        )
    except Exception:
        pass
    return delay


def clear_steam_login_cooldown():
    try:
        _ticket_cooldown_path().unlink()
    except Exception:
        pass


def get_ticket(u, p, c="", shared_secret=""):
    global LAST_TICKET_GEN_RESULT
    check_deps()

    wait, strikes = steam_login_cooldown()
    if wait > 0:
        raise SteamThrottleError(
            87,
            f"Steam is rate-limiting logins — {int(wait)}s left on the backoff "
            f"(attempt {strikes}). Icarus will use the saved session meanwhile.",
        )

    data_dir = str(runtime_output_root() / "steam_auth_data")

    cmd = [
        "node",
        "-e",
        TICKET_GEN_JS,
        "--",
        "--dummy",
        "--username",
        u,
        "--password",
        p,
        "--data-dir",
        data_dir,
    ]
    if c:
        cmd += ["--code", c]
    if shared_secret:
        cmd += ["--shared-secret", shared_secret]

    # 90s, not 60: the helper now spaces its logon retries by 5s each.
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=90, cwd=DIR)
    LAST_TICKET_GEN_RESULT = {
        "stdout": proc.stdout,
        "stderr": proc.stderr,
        "returncode": proc.returncode,
    }
    if proc.returncode == 2:
        raise Exception("STEAM_GUARD_REQUIRED")

    _stderr = proc.stderr or ""
    if proc.returncode == 3 or re.search(
        r"AccountLoginDeniedThrottle|RateLimitExceeded", _stderr
    ):
        match = re.search(r"THROTTLED:(\d+)", _stderr)
        delay = _note_ticket_throttle()
        raise SteamThrottleError(
            int(match.group(1)) if match else 87,
            f"Steam login throttled — backing off {int(delay)}s before the next "
            "attempt. Your saved session is still used in the meantime.",
        )

    out = (proc.stdout or "").strip()
    if not out or proc.returncode != 0:
        error_msg = (proc.stderr or "").strip() or "fail"
        raise Exception(error_msg)

    # The node helper writes the ticket JSON to stdout, but some environments print
    # extra text before/around it (warnings, log lines) — sometimes on the SAME line.
    # The result object is flat (no nested braces), so gather candidates from each
    # line AND from the first '{'..last '}' slice, and take the first that parses
    # with the ticket fields.
    candidates = [ln.strip() for ln in out.split("\n") if ln.strip()]
    if "{" in out and "}" in out:
        candidates.append(out[out.index("{"): out.rindex("}") + 1])
    for cand in candidates:
        if "session_ticket" not in cand:
            continue
        try:
            d = json.loads(cand)
        except Exception:
            continue
        if "steam_id" in d and "session_ticket" in d:
            # A clean logon clears the backoff ladder.
            clear_steam_login_cooldown()
            return d["steam_id"], d["session_ticket"]

    # Nothing parseable — raise a credential-safe diagnostic (ticket hex/ids masked).
    import re as _re
    _err_safe = _re.sub(r"\d{9,}", "<id>", (proc.stderr or "").strip())[-200:]
    _head = _re.sub(r"[0-9A-Fa-f]{16,}", "<hex>", out[:48])
    raise Exception(
        f"bad json (rc={proc.returncode}, out_len={len(out)}, "
        f"head={_head!r}, err={_err_safe!r})"
    )


class UmaClient:
    # Learned corrections for /start's succession rank point, keyed by
    # (viewer_id, parent_1, parent_2). Survives client re-instantiation so a
    # systematic formula gap costs one discovery sweep per process, not per career.
    _succession_point_cache = {}
    # viewer_id -> the succession-point offset last found by a sweep for this
    # account. The client formula tends to be off by a CONSTANT amount for an
    # account regardless of parents, so once one sweep finds it, every later
    # start (any parent pair) can try computed+offset first and skip the sweep.
    _succession_offset_by_viewer = {}
    # Where the two caches above are mirrored so a bot restart doesn't have to
    # re-run the discovery sweep. Loaded lazily on first use, rewritten whenever
    # a sweep learns something new.
    _succession_cache_path = os.path.join("data", "succession_offsets.json")
    _succession_cache_loaded = False

    @classmethod
    def _load_succession_cache(cls):
        if cls._succession_cache_loaded:
            return
        cls._succession_cache_loaded = True
        try:
            with open(cls._succession_cache_path, "r", encoding="utf-8") as fh:
                blob = json.load(fh)
        except (OSError, ValueError):
            return
        # JSON has no tuple keys, so pairs are stored as "viewer:p1:p2" strings.
        for key, offset in (blob.get("pairs") or {}).items():
            try:
                v, p1, p2 = (int(part) for part in key.split(":"))
            except ValueError:
                continue
            cls._succession_point_cache.setdefault((v, p1, p2), int(offset))
        for key, offset in (blob.get("accounts") or {}).items():
            try:
                cls._succession_offset_by_viewer.setdefault(int(key), int(offset))
            except ValueError:
                continue

    @classmethod
    def _save_succession_cache(cls):
        blob = {
            "pairs": {
                "%d:%d:%d" % key: int(off)
                for key, off in cls._succession_point_cache.items()
            },
            "accounts": {
                str(v): int(off) for v, off in cls._succession_offset_by_viewer.items()
            },
        }
        try:
            os.makedirs(os.path.dirname(cls._succession_cache_path), exist_ok=True)
            with open(cls._succession_cache_path, "w", encoding="utf-8") as fh:
                json.dump(blob, fh, indent=1)
        except OSError:
            # A cache we can't write is a slow next start, not a failed one.
            pass

    def __init__(self, cfg, trace_enabled=True):
        profile = get_hwid(cfg.get("steam_password_seed", "default"))

        self.viewer_id = cfg.get("viewer_id", 0)
        self.udid_str = cfg.get("udid") or profile["udid"]
        self.auth_key_hex = cfg.get("auth_key", "")
        self.steam_id = str(cfg.get("steam_id", ""))
        self.steam_ticket = cfg.get("steam_session_ticket", "")

        self.device_id = cfg.get("device_id") or profile["device_id"]
        self.device_name = cfg.get("device_name") or profile["device_name"]
        self.graphics_device = (
            cfg.get("graphics_device_name") or profile["graphics_device_name"]
        )
        self.ip_address = cfg.get("ip_address") or profile["ip_address"]
        self.platform_os = (
            cfg.get("platform_os_version") or profile["platform_os_version"]
        )
        self.locale = cfg.get("locale", "JPN")

        self.unity_ver = cfg.get("unity_ver", "2022.3.62f2")
        self.app_ver = cfg.get("app_ver", "")
        self.res_ver = cfg.get("res_ver", "")
        if not self.app_ver or not self.res_ver:
            log.warning("client constructed without app_ver/res_ver - the "
                        "server will reject calls until these are set")

        self.sid = bytes(16)
        # Serialize ALL game-server calls: the rolling sid + shared session make
        # concurrent calls corrupt each other (career loop vs dailies vs scheduler
        # vs veterans). Reentrant so call()'s own retry recursion doesn't deadlock.
        self._call_lock = threading.RLock()
        self.cached_load_data = {}
        self.tp_info = {}
        self.rp_info = {}
        self.coin_info = {}
        self.item_map = {}
        self.event_boost_info = None
        self.session = requests.Session()
        self.update_headers()
        self.api_jitter = random.uniform(-0.02, 0.02)
        self.api_prefix = "single_mode_free"
        self.scenario_id = 4

        self.on_api_log = None
        self.trace_file = None
        if trace_enabled:
            self._init_trace_log()

    def _init_trace_log(self):
        # ponytail: tracing OFF unless AIC_TRACE=1. This wrote ~5.7 GB/day of
        # api_payloads/*.jsonl with no rotation, no pruning and no reader.
        # Set AIC_TRACE=1 in /etc/umabot.env + restart to get it back for a
        # debugging session, then remove it.
        if os.environ.get("AIC_TRACE") != "1":
            self.trace_file = None
            return
        try:
            log_dir = TRACE_DIR / "api_payloads"
            log_dir.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            suffix = uuid.uuid4().hex[:6]
            self.trace_file = log_dir / f"{ts}_{suffix}_payloads.jsonl"
        except Exception as e:
            log.error(f"Error initializing trace log: {e}")
            self.trace_file = None

    def set_trace_enabled(self, enabled):
        if enabled and not self.trace_file:
            self._init_trace_log()
        elif not enabled:
            self.trace_file = None

    def api_log(self, direction, ep, data, req_id=None):
        # Strip credentials/session material before it reaches any persistent
        # sink (trace jsonl + run report). Endpoints, turns, game data and
        # decision context are preserved for debugging.
        safe_data = redact_credentials(data)
        log_entry = {
            "ts": time.time(),
            "direction": direction,
            "endpoint": ep,
            "data": safe_data,
        }
        if req_id:
            log_entry["req_id"] = req_id

        if callable(self.on_api_log):
            try:
                self.on_api_log(direction, ep, safe_data, req_id)
            except Exception:
                pass

        if self.trace_file:
            try:

                def _json_default(obj):
                    if isinstance(obj, bytes):
                        return obj.hex()
                    return str(obj)

                with open(self.trace_file, "a", encoding="utf-8") as f:
                    f.write(
                        json.dumps(log_entry, ensure_ascii=False, default=_json_default)
                        + "\n"
                    )
            except Exception as e:
                log.error(f"Error writing to log: {e}")

    def api_payload_summary(self, ep, payload):
        payload = payload or {}
        summary = {"current_turn": payload.get("current_turn")}
        if ep == f"{self.api_prefix}/gain_skills":
            summary["gain_skill_info_array"] = (
                payload.get("gain_skill_info_array") or []
            )
        elif ep == f"{self.api_prefix}/multi_item_exchange":
            summary["exchange_item_info_array"] = (
                payload.get("exchange_item_info_array") or []
            )
        elif ep == f"{self.api_prefix}/multi_item_use":
            summary["use_item_info_array"] = payload.get("use_item_info_array") or []
        return summary

    def safe_payload(self, payload):
        return dict(payload or {})

    def response_summary(self, res):
        data = res.get("data") or {}
        headers = res.get("data_headers") or {}
        chara = data.get("chara_info") or data.get("single_mode_chara_light") or {}
        home = data.get("home_info") or {}
        events = data.get("unchecked_event_array") or []
        race = data.get("race_start_info") or {}
        summary = {
            "result_code": headers.get("result_code"),
            "keys": list(data.keys()),
        }
        if chara:
            summary["chara"] = {
                "turn": chara.get("turn"),
                "vital": chara.get("vital"),
                "max_vital": chara.get("max_vital"),
                "skill_point": chara.get("skill_point"),
                "fans": chara.get("fans"),
                "playing_state": chara.get("playing_state"),
            }
        if home:
            summary["commands"] = [
                {
                    "type": item.get("command_type"),
                    "id": item.get("command_id"),
                    "group": item.get("command_group_id"),
                    "enable": item.get("is_enable"),
                    "fail": item.get("failure_rate"),
                }
                for item in home.get("command_info_array") or []
            ]
        if events:
            summary["events"] = [
                {
                    "event_id": item.get("event_id"),
                    "chara_id": item.get("chara_id"),
                    "choices": len(
                        (
                            (item.get("event_contents_info") or {}).get("choice_array")
                            or []
                        )
                    ),
                }
                for item in events
            ]
        if race:
            summary["race_start_info"] = {
                "program_id": race.get("program_id"),
                "race_instance_id": race.get("race_instance_id"),
                "is_short": race.get("is_short"),
            }
        return summary

    def auth_bytes(self):
        if not self.auth_key_hex or self.auth_key_hex == "YOUR_AUTH_KEY_HERE":
            return b""
        return bytes.fromhex(self.auth_key_hex)

    def has_captured_auth(self):
        try:
            int(self.viewer_id)
            bytes.fromhex(str(self.auth_key_hex))
        except (TypeError, ValueError):
            return False
        return bool(
            self.viewer_id
            and self.udid_str
            and self.auth_key_hex
            and self.auth_key_hex != "YOUR_AUTH_KEY_HERE"
            and self.steam_id
            and self.steam_ticket
        )

    def refresh_cached_account_state(self, data):
        if not data:
            return
        self.cached_load_data.update(data)
        if data.get("tp_info"):
            self.tp_info = data["tp_info"]
        if data.get("rp_info"):
            self.rp_info = data["rp_info"]
        if data.get("coin_info"):
            self.coin_info = data["coin_info"]

        item_list = (
            data.get("user_item")
            or data.get("user_item_array")
            or data.get("item_list")
        )
        if isinstance(item_list, list):
            for item in item_list:
                iid = item.get("item_id")
                num = item.get("number")
                if iid is not None and num is not None:
                    self.item_map[int(iid)] = int(num)

    def regen_sid(self):
        self.sid = make_sid(self.viewer_id, self.udid_str)

    def set_scenario(self, scenario_id):
        self.scenario_id = int(scenario_id)
        prefix = SCENARIO_API_PREFIXES.get(self.scenario_id)
        if prefix is None:
            prefix = DEFAULT_API_PREFIX
            log.warning(
                "unknown scenario_id %s - routing career calls to %r",
                self.scenario_id,
                prefix,
            )
        self.api_prefix = prefix
        log.debug("scenario set: id=%s prefix=%s", self.scenario_id, prefix)

    def common(self):
        return {
            "viewer_id": self.viewer_id,
            "device": 4,
            "device_id": self.device_id,
            "device_name": self.device_name,
            "graphics_device_name": self.graphics_device,
            "ip_address": self.ip_address,
            "platform_os_version": self.platform_os,
            "carrier": "",
            "keychain": 0,
            "locale": self.locale,
            "button_info": "",
            "dmm_viewer_id": None,
            "dmm_onetime_token": None,
            "steam_id": self.steam_id,
            "steam_session_ticket": self.steam_ticket,
        }

    def update_headers(self):
        self.session.headers.update(
            {
                "User-Agent": f"UnityPlayer/{self.unity_ver} (UnityWebRequest/1.0, libcurl/8.10.1-DEV)",
                "Accept": "*/*",
                "Accept-Encoding": "deflate, gzip",
                "Content-Type": "application/x-msgpack",
                "X-Unity-Version": self.unity_ver,
            }
        )

    def call(self, ep, args=None, retry_208=6, retry_205=3):
        # Hold the reentrant lock across the whole call (rate-limit wait, request,
        # sid roll, and retry recursion) so no other thread interleaves.
        with self._call_lock:
            return self._call_impl(ep, args, retry_208, retry_205)

    def _call_impl(self, ep, args=None, retry_208=6, retry_205=3):
        if not hasattr(self, "_last_raw_call_ts"):
            self._last_raw_call_ts = 0
        elapsed = time.time() - self._last_raw_call_ts
        if elapsed < 0.14:
            time.sleep(0.14 - elapsed)
        self._last_raw_call_ts = time.time()

        req_id = str(uuid.uuid4())[:8]
        # Copy before merging common() so the caller's dict is never mutated
        # (previously the 205/102 payload dumps leaked session credentials
        # because args had been polluted with common()'s auth fields).
        payload = dict(args or {})
        payload.update(self.common())

        if ep in (
            f"{self.api_prefix}/race_out",
            f"{self.api_prefix}/team_race_out",
        ):
            click_x = int(random.gauss(9913872, 638182))
            click_y = int(random.gauss(1462394, 341199))
            click_ts = int(time.time())
            btn = {
                "ViewerId": self.viewer_id,
                "DeviceId": 4,
                "ScenarioId": getattr(self, "scenario_id", 4),
                "ClickPosX": click_x,
                "ClickPosY": click_y,
                "ClickServerTime": click_ts,
            }
            payload["button_info"] = json.dumps(btn, separators=(",", ":"))

        self.api_log(
            "REQ",
            ep,
            {
                "payload": payload,
            },
            req_id,
        )

        pack_payload = dict(payload)
        if (
            ep == f"{self.api_prefix}/gain_skills"
            and "gain_skill_info_array" in pack_payload
        ):
            clean_array = []
            for item in pack_payload["gain_skill_info_array"] or []:
                clean_item = dict(item)
                clean_item.pop("name", None)
                clean_array.append(clean_item)
            pack_payload["gain_skill_info_array"] = clean_array

        body = pack(
            self.sid,
            get_raw_udid(self.udid_str),
            self.auth_bytes(),
            pack_payload,
            self.udid_str,
        )
        headers = {
            "SID": self.sid.hex(),
            "Device": "4",
            "ViewerID": str(self.viewer_id),
            "APP-VER": self.app_ver,
            "RES-VER": self.res_ver,
        }

        max_retries = 8
        # A network error is indistinguishable from "the server committed the
        # write and the reply was lost", so replaying a CURRENCY spend can
        # charge twice (up to 8x). Missing a refill costs one visit; the next
        # rotation retries. Spending real carats twice cannot be undone.
        if ep in self.CURRENCY_ENDPOINTS:
            max_retries = 1
        for attempt in range(max_retries):
            try:
                resp = self.session.post(
                    BASE_URL + ep, data=body, headers=headers, timeout=30
                )
            except Exception as e:
                if attempt < max_retries - 1:
                    wait_time = min(1.0 + (attempt * 2.5), 15.0)
                    log.debug(
                        "network retry %s/%s on %s in %.1fs: %s",
                        attempt + 1,
                        max_retries,
                        ep,
                        wait_time,
                        e,
                    )
                    time.sleep(wait_time)
                    continue
                self.api_log("ERR", ep, {"error": str(e)}, req_id)
                raise Exception(f"Network error on {ep}: {e}")

            # A 5xx (or 429) is the EDGE failing, not the game refusing: the body
            # is an HTML error page rather than msgpack, so the request never
            # reached game logic and repeating it cannot double-apply a training
            # turn or a race. Retrying here is what stops a momentary server
            # hiccup from killing a whole career - one user's log had 36 of these
            # and 29 of them crashed the runner outright, on exec_command,
            # check_event, race_start, race_end and race_entry.
            if resp.status_code in _RETRY_HTTP_STATUSES and attempt < max_retries - 1:
                wait_time = min(1.5 + (attempt * 2.5), 15.0)
                log.warning(
                    "Game servers returned HTTP %s on %s - retrying in %.1fs "
                    "(attempt %s/%s).",
                    resp.status_code, ep, wait_time, attempt + 1, max_retries,
                )
                time.sleep(wait_time)
                continue
            break

        if resp.status_code != 200:
            body_preview = resp.text[:500] if resp.text else ""
            self.api_log(
                "ERR",
                ep,
                {"http_status": resp.status_code, "body": body_preview},
                req_id,
            )
            log.error(f"HTTP error on {ep}: status={resp.status_code} body={body_preview}")
            raise Exception(f"HTTP {resp.status_code} on {ep}: {body_preview}")

        res = unpack(resp.text.strip(), self.udid_str)
        dh = res.get("data_headers", {})
        rc = dh.get("result_code", 0)

        self.api_log("RES", ep, res, req_id)

        data = res.get("data", {})

        if isinstance(data, dict):
            if data.get("tp_info"):
                self.tp_info = data["tp_info"]
            if data.get("rp_info"):
                self.rp_info = data["rp_info"]
            if data.get("coin_info"):
                self.coin_info = data["coin_info"]
            item_list = data.get("user_item") or data.get("user_item_array")
            if isinstance(item_list, list):
                for item in item_list:
                    iid = item.get("item_id")
                    num = item.get("number")
                    if iid is not None and num is not None:
                        self.item_map[int(iid)] = int(num)

        if rc == 709:
            new_vid = dh.get("viewer_id") or (
                data.get("viewer_id") if isinstance(data, dict) else None
            )
            if new_vid and new_vid != self.viewer_id:
                log.warning("VIEWER ID MISMATCH on 709 (viewer id updated)")
                self.viewer_id = new_vid
                self.regen_sid()
            raise Exception(f"709 on {ep}")
        if dh.get("sid") and isinstance(dh["sid"], str) and dh["sid"].strip():
            self.sid = next_sid(dh["sid"])

        if rc != 1:
            if rc == 205:
                if ep.endswith("/continue"):
                    # Expected + non-fatal: the server declines a clock rewind on
                    # a race that isn't retryable (i.e. not a lost goal race). The
                    # runner catches this and accepts the result, so keep it a
                    # quiet INFO trace instead of a scary ERROR + payload dump —
                    # users kept reporting these handled 205s as a "problem".
                    _t = (args or {}).get("current_turn") if isinstance(args, dict) else None
                    log.info(
                        "Clock rewind not available (205) on %s (turn %s) - accepting the race result.",
                        ep,
                        _t,
                    )
                else:
                    # Exact payload at DEBUG only: it lands in the exported log
                    # file for bug reports without frightening the Logs console.
                    log.debug(f"[205 detail] Endpoint: {ep}")
                    log.debug(
                        f"Payload sent: {json.dumps(redact_for_console(args or {}), ensure_ascii=False)}"
                    )

            if rc == 102:
                log.debug(f"[102 detail] Endpoint: {ep}")
                log.debug(
                    f"Payload sent: {json.dumps(redact_for_console(args or {}), ensure_ascii=False)}"
                )

            if rc == 205 and retry_205 > 0:
                # Do not retry 205 for state-dependent payloads (items/skills/start/commands).
                # Force an immediate exception so the runner can pull a fresh career state.
                if ep in {
                    f"{self.api_prefix}/gain_skills",
                    f"{self.api_prefix}/multi_item_exchange",
                    f"{self.api_prefix}/multi_item_use",
                    f"{self.api_prefix}/start",
                    f"{self.api_prefix}/exec_command",
                    # clock rewind: 205 means "this race isn't retryable" — a
                    # retry re-sends the same payload and just 205s again, so
                    # raise immediately and let the runner accept the result.
                    f"{self.api_prefix}/continue",
                    "single_mode/start",
                    "single_mode_free/start",
                    # Independent Training. This one matters more than the rest:
                    # idle_single_mode/start reports a wrong succession point as
                    # a bare 205, so idle_start_corrected has to *search* for the
                    # value the game accepts. Retrying each candidate 3x turns a
                    # ~160-call search into a ~640-call one and drags a 208 storm
                    # behind it — the search then takes ~11 minutes and looks
                    # like a hang. Each candidate must cost exactly one call.
                    "idle_single_mode/start",
                    # Grand Live (scenario 3): master_square/live_start mutate
                    # state (spend tokens, reroll the board, perform the live) -
                    # the same exec_command-class write already excluded above.
                    # A stale retry would resend a square_id against a board
                    # that's already changed.
                    f"{self.api_prefix}/master_square",
                    f"{self.api_prefix}/live_start",
                    f"{self.api_prefix}/reserve_square",
                }:
                    pass
                else:
                    _t = (args or {}).get("current_turn") if isinstance(args, dict) else None
                    log.warning(
                        "The game's data got briefly out of step (205) during %s (turn %s) - retrying automatically (%d left).",
                        describe_endpoint(ep), _t, retry_205,
                    )
                    time.sleep(max(0.14, min(0.19, random.gauss(0.166, 0.0083))))
                    return self.call(
                        ep, args, retry_208=retry_208, retry_205=retry_205 - 1
                    )

            if rc == 208:
                _turn = (args or {}).get("current_turn") if isinstance(args, dict) else None
                if ep in {
                    f"{self.api_prefix}/gain_skills",
                    f"{self.api_prefix}/multi_item_exchange",
                    f"{self.api_prefix}/multi_item_use",
                    # Manual TP-refill spends (currency): a 208 may mean the spend
                    # already landed, so retrying would double-spend drinks/carats.
                    "user/recovery_trainer_point",
                    "item/use_recovery_item",
                }:
                    # Idempotent-write endpoints: a 208 here may mean the write already
                    # landed, so retrying risks a double-spend. Surface it, don't retry.
                    log.debug(
                        "208 (SERVER BUSY) on %s (turn %s) treated as applied, not retried.",
                        ep, _turn,
                    )
                    return res

                if retry_208 > 0:
                    # Capped exponential backoff with jitter (0.3,0.6,1.2,2.4,4.8,5.0s)
                    # so retries actually outlast a real server-busy window. Pass
                    # retry_205 through so a 208 retry can't reset the 205 budget.
                    attempt = 6 - retry_208 + 1
                    delay = min(0.3 * (2 ** (attempt - 1)), 5.0)
                    delay += delay * random.uniform(0.0, 0.25)
                    log.warning(
                        "Game servers are busy (208) during %s (turn %s) - waiting %.1fs and retrying (attempt %d/6).",
                        describe_endpoint(ep), _turn, delay, attempt,
                    )
                    time.sleep(delay)
                    return self.call(
                        ep, args, retry_208=retry_208 - 1, retry_205=retry_205
                    )

                log.warning(
                    "Game servers stayed busy (208) through 6 retries during %s (turn %s) - giving up on this attempt.",
                    describe_endpoint(ep), _turn,
                )

            # 214 = client resource_version behind the server (a game data
            # update). The error carries the server's resource_version, so adopt
            # it and retry once — this keeps the bot connected through data
            # patches without a manual re-login. Guard: only retry when it's a
            # genuinely newer value (self.res_ver != new), so a persistent 214
            # can't loop forever.
            if rc == 214:
                new_rv = ""
                if isinstance(dh, dict):
                    new_rv = str(dh.get("resource_version") or "")
                if not new_rv and isinstance(data, dict):
                    new_rv = str(data.get("resource_version") or "")
                if new_rv and new_rv != str(self.res_ver):
                    log.warning(
                        "The game just shipped a data update (214) - Icarus adopted it automatically and is retrying."
                    )
                    log.debug(
                        f"214 detail: res_ver {self.res_ver!r} -> {new_rv!r}, retrying {ep}."
                    )
                    self.res_ver = new_rv
                    self.update_headers()
                    cb = getattr(self, "on_res_ver_update", None)
                    if callable(cb):
                        try:
                            cb(new_rv)
                        except Exception:
                            pass
                    return self.call(ep, args, retry_208=retry_208, retry_205=retry_205)

            err_detail = format_api_error(ep, rc, res)
            # NOTE: err_msg is parsed by error handlers all over main.py
            # ("102" in err_msg etc.) - its format must never change.
            err_msg = f"API error {rc} on {ep}: {err_detail}"
            _quiet = (
                rc == 102
                and ep in {f"{self.api_prefix}/race_end", f"{self.api_prefix}/race_reward", f"{self.api_prefix}/race_out"}
            ) or (
                # handled by the runner (accepts the race result); logged as INFO above
                rc == 205
                and ep.endswith("/continue")
            ) or (
                # "no story event is running" - the NORMAL state between events.
                # aic.auth calls this through _optional(ignore={9001}) and carries
                # on; it logged at ERROR anyway, with a message about Event Boost
                # that has nothing to do with this endpoint, and the health check
                # read that trailing ERROR as a wedged farm.
                rc == 9001
                and ep.endswith("story_event/index")
            )
            if not _quiet:
                # The Logs console gets ONE plain-English line (what happened +
                # what to do); the technical message goes to the file at DEBUG.
                friendly = explain_api_error(rc, ep)
                if friendly:
                    log.error(friendly)
                    log.debug(err_msg)
                else:
                    log.error(err_msg)
            raise ApiError(rc, ep, err_msg, response=res)

        return res

    def hard_reset(self):
        self.sid = bytes(16)
        self.regen_sid()
        self.session.close()
        self.session = requests.Session()
        self.update_headers()
        try:
            self.call(
                "tool/start_session", {"attestation_type": 0, "device_token": None}
            )
            
            try:
                ssl_res = self.call("tool/start_session/load", {})
                actual_res_ver = str(ssl_res.get("data", {}).get("resource_version", ""))
                local_res_ver = str(self.res_ver)
                if actual_res_ver:
                    if actual_res_ver != local_res_ver:
                        log.warning(f"res_ver update: adopting server {actual_res_ver} (was {local_res_ver}).")
                        self.res_ver = actual_res_ver
                        self.update_headers()
                    else:
                        log.debug(f"Version matches! Server res_ver: {actual_res_ver} | Local res_ver: {local_res_ver}")
            except Exception as e:
                log.debug(f"Failed to check resource_version early: {e}")
                
            res = self.call("load/index", {"adid": ""})
            data = res.get("data", {})
            self.refresh_cached_account_state(data)
            self.read_info()

            try:
                sm_res = self.call(f"{self.api_prefix}/load", {})
                chara = sm_res.get("data", {}).get("chara_info")
                if not chara:
                    raise StateRecoveryError(
                        f"No chara_info returned in {self.api_prefix}/load after hard reset."
                    )
                return sm_res
            except Exception as e:
                if isinstance(e, StateRecoveryError):
                    raise
                if error_code(e) in (201, 102):
                    raise StateRecoveryError(f"Cannot recover training state: {e}")
                log.error(f"{self.api_prefix}/load during hard_reset failed: {e}")
                raise StateRecoveryError(f"Failed to load career state after hard reset: {e}")
        except StateRecoveryError:
            raise
        except Exception as e:
            log.error(f"Hard Reset Failure: {e}")
            raise

    def signup(self):
        self.regen_sid()
        self.call("tool/pre_signup")
        time.sleep(0.83)
        self.regen_sid()
        res = self.call(
            "tool/signup",
            {
                "error_code": 0,
                "error_message": "",
                "attestation_type": 0,
                "optin_user_birth": 199801,
                "dma_state": 0,
                "country": "Canada",
                "credential": "",
            },
        )
        d = res.get("data", {})
        if d.get("viewer_id"):
            self.viewer_id = d["viewer_id"]
        if d.get("auth_key"):
            self.auth_key_hex = base64.b64decode(d["auth_key"]).hex()
        self.save_config()
        return res

    def login(self, max_retries=3):
        using_existing_auth = self.has_captured_auth()
        if not using_existing_auth:
            self.signup()
            using_existing_auth = self.has_captured_auth()

        old_h = dict(self.session.headers)
        self.session.close()
        self.session = requests.Session()
        self.session.headers.update(old_h)

        for attempt in range(max_retries + 1):
            try:
                self.regen_sid()
                self.call(
                    "tool/start_session", {"attestation_type": 0, "device_token": None}
                )
                
                try:
                    ssl_res = self.call("tool/start_session/load", {})
                    actual_res_ver = str(ssl_res.get("data", {}).get("resource_version", ""))
                    local_res_ver = str(self.res_ver)
                    if actual_res_ver:
                        if actual_res_ver != local_res_ver:
                            log.warning(f"VERSION MISMATCH DETECTED EARLY! Server res_ver: {actual_res_ver} | Local res_ver: {local_res_ver}")
                        else:
                            log.debug(f"Version matches! Server res_ver: {actual_res_ver} | Local res_ver: {local_res_ver}")
                except Exception as e:
                    log.debug(f"Failed to check resource_version early: {e}")
                    
                res = self.call("load/index", {"adid": ""})
                data = res.get("data", {})
                self.refresh_cached_account_state(data)
                self.read_info()
                return res
            except Exception as e:
                err = str(e)
                if "709" in err and attempt < max_retries:
                    time.sleep(0.83)
                    continue
                if "394" in err and attempt < max_retries:
                    time.sleep(2.5)
                    continue
                if "202" in err and attempt < max_retries:
                    time.sleep(4.15)
                    continue
                raise

    # TP recovery items (Toughness drinks), best value first: 32 = Limited
    # Toughness 30, 12 = Toughness 30, 11 = Toughness 10.
    _TP_ITEMS = ((32, 30), (12, 30), (11, 10))

    def tp_recovery_inventory(self):
        """How many 30-TP restores the owned Toughness drinks are worth, plus
        the raw item counts - for the manual-refill popup."""
        counts = {iid: int(self.item_map.get(iid, 0)) for iid, _ in self._TP_ITEMS}
        tp_from_items = sum(counts[iid] * amt for iid, amt in self._TP_ITEMS)
        return {"item_counts": counts, "tp_from_items": tp_from_items}

    def recover_tp(self, tp_target, source="auto"):
        """Recover up to `tp_target` TP (the caller caps this at the current TP
        deficit). Drinks fill toward the target in their own increments; carats
        top up the rest in 30-TP chunks (the only unit the game sells).

        Crucially it does NOT round the target up to a 30-chunk first - so it
        never buys an extra carat chunk or burns extra drinks past what the
        target needs. source: 'items' (drinks only), 'jewels' (carats only), or
        'auto' (drinks first, then carats). Returns (tp_info, summary); summary
        carries `shortfall` when the chosen source ran out before the target.
        """
        tp_target = max(0, int(tp_target or 0))
        tp_recovered = 0
        items_used = 0
        carat_chunks = 0

        if tp_target > 0 and source in ("items", "auto"):
            for item_id, restore in self._TP_ITEMS:
                if tp_recovered >= tp_target:
                    break
                available = int(self.item_map.get(item_id, 0))
                if available <= 0:
                    continue
                # Just enough of THIS drink to reach the target (the last one may
                # slightly overshoot - drinks are indivisible and the extra TP is
                # banked by the game, not lost).
                need = ((tp_target - tp_recovered) + restore - 1) // restore
                use = min(need, available)
                try:
                    result = self.call(
                        "item/use_recovery_item",
                        {"item_id": item_id, "client_own_num": available, "item_num": use},
                    )
                    data = result.get("data", {})
                    if data.get("tp_info"):
                        self.tp_info = data["tp_info"]
                    self.item_map[item_id] = available - use
                    tp_recovered += use * restore
                    items_used += use
                except Exception as e:
                    log.warning(f"Failed to use TP drink (id {item_id}): {e}")

        if tp_recovered < tp_target and source in ("jewels", "auto"):
            remaining = ((tp_target - tp_recovered) + 29) // 30
            total_jewels = self.coin_info.get("fcoin", 0) + self.coin_info.get("coin", 0)
            result = self.call(
                "user/recovery_trainer_point",
                {"count": remaining, "client_own_num": total_jewels},
            )
            data = result.get("data", {})
            if data.get("tp_info"):
                self.tp_info = data["tp_info"]
            if data.get("coin_info"):
                self.coin_info = data["coin_info"]
            tp_recovered += remaining * 30
            carat_chunks = remaining

        return self.tp_info, {
            "tp_recovered": tp_recovered,
            "items_used": items_used,
            "carat_chunks": carat_chunks,
            "shortfall": max(0, tp_target - tp_recovered),
        }

    #: Endpoints that move currency or consume a limited item. A write here
    #: must never be replayed after a network error, and a 208 must never be
    #: retried - both risk charging twice.
    CURRENCY_ENDPOINTS = frozenset({
        "user/recovery_trainer_point",
        "item/use_recovery_item",
    })

    #: Fallback when the caller passes nothing. The real table is read from
    #: master.mdb (item_category 20) by aic.racedata.tp_restore_items - see the
    #: note on aic.config.TP_RESTORE for why the category, not effect_type, is
    #: the discriminator.
    TP_RESTORE_FALLBACK = {32: 30, 174: 30}

    def recovery_tp(self, count=1, use_jewels=False, items=None):
        tp_needed = count * 30
        tp_recovered = 0

        restore = dict(items or self.TP_RESTORE_FALLBACK)
        stop_items = False
        for item_id in sorted(restore, key=lambda i: -restore[i]):
            if stop_items or tp_recovered >= tp_needed:
                break

            available = self.item_map.get(item_id, 0)
            restore_amount = restore[item_id]
            # One item per call: item_num > 1 came back result_code 208 when
            # measured, and a +30 item needs at most a couple of calls anyway.
            while tp_recovered < tp_needed and available > 0:
                try:
                    result = self.call(
                        "item/use_recovery_item",
                        {
                            "item_id": item_id,
                            "client_own_num": available,
                            "item_num": 1,
                        },
                    )
                except Exception as e:
                    log.warning(f"Failed to use TP item (ID: {item_id}): {e}")
                    break
                data = result.get("data", {}) or {}
                code = (result.get("data_headers") or {}).get("result_code")
                tp = data.get("tp_info")
                if tp:
                    self.tp_info = tp
                    available -= 1
                    self.item_map[item_id] = available
                    tp_recovered += restore_amount
                    continue
                if code != 1:
                    # call() RETURNS a raw 208 for this endpoint instead of
                    # raising, precisely so a maybe-applied spend is not
                    # retried - and a 208 body carries no tp_info. Reading that
                    # as "not a TP item" condemned a working Toughness 30 and
                    # fell through to buying carats. The spend may or may not
                    # have landed, so stop and let the caller re-read TP.
                    raise Exception(
                        f"item/use_recovery_item on {item_id} returned "
                        f"result_code {code} with no tp_info; the spend is "
                        f"indeterminate, so recovery stops here."
                    )
                # result_code 1 and still no tp_info: the item genuinely does
                # not restore TP (an RP item). The table said it did, so the
                # table is wrong - stop the item phase entirely rather than
                # proving the same point once per held id.
                log.warning(
                    f"Item {item_id} was consumed and restored no TP, but the "
                    f"TP table lists it. Trusting the server: stopping item "
                    f"recovery for this account."
                )
                self.item_map[item_id] = max(0, available - 1)
                stop_items = True
                break

        if tp_recovered >= tp_needed:
            return self.tp_info

        if not use_jewels:
            raise Exception(
                "Not enough TP potions available and jewel recovery is disabled."
            )

        remaining_chunks = ((tp_needed - tp_recovered) + 29) // 30
        total_jewels = self.coin_info.get("fcoin", 0) + self.coin_info.get("coin", 0)
        result = self.call(
            "user/recovery_trainer_point",
            {
                "count": remaining_chunks,
                "client_own_num": total_jewels,
            },
        )
        data = result.get("data", {})
        tp = data.get("tp_info", {})
        if tp:
            self.tp_info = tp
        coin = data.get("coin_info", {})
        if coin:
            self.coin_info = coin
        return tp

    def read_info(self):
        try:
            return self.call(
                "read_info/index",
                {
                    "add_home_story_data_array": [],
                    "add_short_episode_data_array": [],
                    "add_home_poster_data_array": [],
                    "add_tutorial_guide_data_array": [],
                    "add_released_episode_data_array": [],
                    "add_talk_gallery_data_array": [],
                    "add_home_banner_data_array": [],
                },
            )
        except Exception as e:
            if error_code(e) == 102:
                log.info("Ignored Error 102 on read_info/index (Cosmetic endpoint).")
                return {}
            raise

    def factor_select(self, current_turn=0, skill_id_array=None):
        return self.call(
            f"{self.api_prefix}/factor_select",
            {"current_turn": current_turn, "skill_id_array": skill_id_array},
        )

    def finish_career(self, current_turn=0, is_force_delete=False, factor_lottery_id=1):
        return self.call(
            f"{self.api_prefix}/finish",
            {
                "is_force_delete": is_force_delete,
                "current_turn": current_turn,
                "factor_lottery_id": factor_lottery_id,
            },
        )

    def load_career(self):
        return self.call(f"{self.api_prefix}/load", {})

    def minigame_end(
        self, current_turn, result_state=1, result_value=0, result_detail_array=None
    ):
        return self.call(
            f"{self.api_prefix}/minigame_end",
            {
                "result": {
                    "result_state": result_state,
                    "result_value": result_value,
                    "result_detail_array": result_detail_array,
                },
                "current_turn": current_turn,
            },
        )

    def save_config(self, cfg_path=None):
        global LAST_SAVED_CONFIG
        LAST_SAVED_CONFIG = {
            "viewer_id": self.viewer_id,
            "udid": self.udid_str,
            "auth_key": self.auth_key_hex,
            "steam_id": self.steam_id,
            "steam_session_ticket": self.steam_ticket,
            "device_id": self.device_id,
            "device_name": self.device_name,
            "graphics_device_name": self.graphics_device,
            "ip_address": self.ip_address,
            "platform_os_version": self.platform_os,
        }

    def pre_single_mode(self, exclude_viewer_ids=None):
        payload = {}
        if exclude_viewer_ids:
            payload["exclude_viewer_id_array"] = exclude_viewer_ids
        return self.call("pre_single_mode/index", payload)

    def start_career(
        self,
        card_id,
        support_card_ids,
        friend_viewer_id,
        friend_card_id,
        parent_id_1,
        parent_id_2,
        scenario_id=4,
        deck_id=1,
        use_tp=30,
        tp_info=None,
        current_money=0,
        succession_rank_point=0,
        rental_viewer_id=0,
        rental_trained_chara_id=0,
        difficulty_id=0,
        difficulty=0,
        is_boost=0,
        boost_story_event_id=0,
        succession_point_hint=0,
        running_style=1,
        build_only=False,
        force_running_style=False,
    ):
        """build_only=True returns the assembled payload instead of calling.

        The game's native Auto Training (`idle_single_mode/start`) wraps this
        EXACT object as `single_mode_start_request_common` — confirmed from a
        capture — so it is built here once rather than duplicated.
        """
        self.set_scenario(scenario_id)
        if not tp_info:
            tp_info = {"current_tp": 100, "max_tp": 100, "max_recovery_time": 0}

        start_chara = {
            "card_id": card_id,
            "support_card_ids": support_card_ids,
            "friend_support_card_info": {
                "viewer_id": friend_viewer_id,
                "support_card_id": friend_card_id,
            },
            "succession_trained_chara_id_1": parent_id_1,
            "succession_trained_chara_id_2": parent_id_2,
            "rental_succession_trained_chara": {
                "viewer_id": 0,
                "trained_chara_id": 0,
                "is_circle_member": False,
                "is_event_rental": False,
            },
            "scenario_id": scenario_id,
            "selected_difficulty_info": {
                "difficulty_id": difficulty_id,
                "difficulty": difficulty,
                "is_boost": is_boost,
            },
            "select_deck_id": deck_id,
            "boost_story_event_id": boost_story_event_id,
            "boost_factor_research_event_id": 0,
            "is_play_training_challenge": False,
            "training_challenge_mode": 0,
        }
        if int(scenario_id or 0) == 3 or force_running_style:
            # Grand Live (scenario 3): confirmed on Global, all 3 captured starts
            # send running_style as the LAST start_chara key. Not sent for other
            # scenarios in a NORMAL start - never observed there, and per-race
            # change_running_style already covers them.
            #
            # force_running_style is for Independent Training, which needs it in
            # EVERY scenario: the game plays the races itself, so there is no
            # per-race style choice to make later. Omitting it made the server
            # answer a bare 205 on a MANT (scenario 4) idle start, while the
            # captured scenario-3 idle start - which included it - was accepted.
            start_chara["running_style"] = int(running_style or 1)

        if rental_viewer_id and rental_trained_chara_id:
            # The borrowed (guest) parent fills whichever OWNED succession slot is
            # empty. Only clear a slot if it actually holds the rental id (it normally
            # won't — owned and guest parents are routed to disjoint request fields).
            # NEVER drop a non-matching owned parent: doing that produced a server 205
            # (data mismatch) whenever a career was started with 1 owned + 1 guest.
            if start_chara["succession_trained_chara_id_1"] == rental_trained_chara_id:
                start_chara["succession_trained_chara_id_1"] = 0
            elif start_chara["succession_trained_chara_id_2"] == rental_trained_chara_id:
                start_chara["succession_trained_chara_id_2"] = 0

            rental_obj = {
                "viewer_id": rental_viewer_id,
                "trained_chara_id": rental_trained_chara_id,
                "is_circle_member": False,
                "is_event_rental": False,
            }
            start_chara["rental_succession_trained_chara"] = rental_obj

        start_payload = {
            "start_chara": start_chara,
            "tp_info": {
                "current_tp": tp_info.get("current_tp", 100),
                "max_tp": tp_info.get("max_tp", 100),
                "max_recovery_time": tp_info.get("max_recovery_time", 0),
            },
            "current_money": current_money,
            "use_tp": use_tp,
            "current_succession_rank_point": succession_rank_point,
        }

        if build_only:
            return start_payload

        try:
            return self.call(f"{self.api_prefix}/start", start_payload)
        except Exception as e:
            # A BORROWED parent is the single most common non-points cause of a
            # start-102 (the daily borrow limit), and main.py already handles it
            # by retrying with the user's own/backup parents - but only once this
            # call raises. Sweeping first meant the user sat through up to 160
            # paced /start calls (~6 minutes of red 102s in the logs) before the
            # fallback that actually works got a chance. Let it propagate.
            if (
                error_code(e) == 102
                and int(rental_trained_chara_id or 0) > 0
            ):
                log.warning(
                    "Career start refused (102) with a borrowed parent selected — "
                    "not searching for a succession-point correction; the borrow "
                    "is the likelier cause and is retried without it."
                )
                raise e
            if error_code(e) == 102 and succession_rank_point > 0:
                # The client-side point formula can be systematically off for
                # some parent pairs (seen in the wild: 63 short on EVERY start,
                # rediscovered by ~125 blind calls per career). Try the computed
                # value first (it is usually right), then the smart candidates,
                # then sweep - positive offsets first, since rank points only
                # accumulate. NOTE: do NOT reorder to send the item-75 hint first
                # and bail - some accounts need the computed value, and sending
                # only the hint would false-fail those starts.
                cache_key = (int(self.viewer_id or 0), int(parent_id_1 or 0), int(parent_id_2 or 0))
                vkey = int(self.viewer_id or 0)
                UmaClient._load_succession_cache()
                cache = UmaClient._succession_point_cache
                voff = UmaClient._succession_offset_by_viewer
                seen = {succession_rank_point}

                def _try_point(point):
                    """Retry /start with a candidate succession point. Returns the
                    response on success, None on a 102 (keep trying), and re-raises
                    any non-102 error. Caches the winning offset both for this exact
                    parent pair and for the account. NOTE: capture replay showed the
                    gap is mostly PER-PAIR (it is the trainee-parent affinity the old
                    formula omitted; now computed directly) - the account-level cache
                    only helps when a small data-staleness residual repeats."""
                    point = int(point)
                    if point < 0 or point in seen:
                        return None
                    seen.add(point)
                    start_payload["current_succession_rank_point"] = point
                    try:
                        res = self.call(f"{self.api_prefix}/start", start_payload)
                        offset = point - succession_rank_point
                        cache[cache_key] = offset
                        voff[vkey] = offset
                        if offset:
                            UmaClient._save_succession_cache()
                        log.info(
                            f"Start point corrected: {point} (computed {succession_rank_point}, "
                            f"offset {offset:+d} cached for next careers)"
                        )
                        return res
                    except Exception as err:
                        if error_code(err) != 102:
                            raise err
                        return None

                # 1) A cached offset learned from a prior career with this parent pair.
                cached_delta = cache.get(cache_key)
                if cached_delta:
                    res = _try_point(succession_rank_point + cached_delta)
                    if res is not None:
                        return res

                # 1b) The account's last known offset from any prior sweep. With
                #     the trainee links now in the computed formula this is only a
                #     small data-staleness residual, which does tend to repeat.
                vdelta = voff.get(vkey)
                if vdelta and vdelta != cached_delta:
                    res = _try_point(succession_rank_point + vdelta)
                    if res is not None:
                        return res

                # 2) The server's own item-75 balance (from load/index) as a quick
                #    candidate - it's sometimes the value, but a wrong hint does NOT
                #    mean no point works, so we still fall through to the sweep.
                if succession_point_hint:
                    hint = int(succession_point_hint)
                    if hint != succession_rank_point:
                        res = _try_point(hint)
                        if res is not None:
                            return res

                # 3) Bounded +/-80 sweep to rediscover a systematic formula offset.
                #    This is what makes otherwise-valid parents startable when the
                #    computed point is a fixed amount off (seen in the wild: ~63
                #    short on every start). It runs even when the item-75 hint was
                #    present - a 4.3 change bailed here instead and false-failed
                #    most of some users' valid, finished parents with a 102.
                log.warning(
                    f"Career start needs a succession-point correction (computed "
                    f"{succession_rank_point} was rejected). Searching for the right value..."
                )
                # Positives first, deliberately: rank points only accumulate, so
                # the computed value is short far more often than it is over
                # (the documented real case was ~63 SHORT on every start).
                # Do not "optimise" this into an interleaved +1,-1,+2,-2 order -
                # it halves the worst case for negative offsets but doubles the
                # cost of the common positive one.
                sweep = list(range(1, 81)) + [-i for i in range(1, 81)]
                for i, offset in enumerate(sweep):
                    res = _try_point(succession_rank_point + offset)
                    if res is not None:
                        return res
                    # One progress line per 40 attempts, so the log shows a
                    # search in progress rather than a wall of identical errors.
                    if i and i % 40 == 0:
                        log.warning(
                            "Still searching for the succession point "
                            f"({i}/{len(sweep)} candidates tried)..."
                        )
                log.error(
                    "Couldn't find a working succession point for these parents - "
                    "this is likely a genuine state mismatch (an unfinished career "
                    "still active, or the scenario isn't startable), not the points."
                )
            raise e

    def exec_command(
        self,
        command_type,
        command_id,
        current_turn,
        current_vital,
        command_group_id=0,
        select_id=0,
    ):
        return self.call(
            f"{self.api_prefix}/exec_command",
            {
                "command_type": command_type,
                "command_id": command_id,
                "command_group_id": command_group_id,
                "select_id": select_id,
                "current_turn": current_turn,
                "current_vital": current_vital,
            },
        )

    def check_event(self, event_id, chara_id=0, choice_number=0, current_turn=0):
        payload = {
            "event_id": event_id,
            "chara_id": chara_id or 0,
            "choice_number": choice_number if choice_number is not None else 0,
            "current_turn": current_turn,
        }
        return self.call(f"{self.api_prefix}/check_event", payload)

    def get_choice_reward(self, event_id):
        return self.call(f"{self.api_prefix}/get_choice_reward", {"event_id": event_id})

    def use_items(self, use_item_info_array, current_turn):
        return self.call(
            f"{self.api_prefix}/multi_item_use",
            {"use_item_info_array": use_item_info_array, "current_turn": current_turn},
        )

    def exchange_items(self, exchange_item_info_array, current_turn):
        return self.call(
            f"{self.api_prefix}/multi_item_exchange",
            {
                "exchange_item_info_array": exchange_item_info_array,
                "current_turn": current_turn,
            },
        )

    def gain_skills(self, gain_skill_info_array, current_turn):
        payload_array = []
        for item in gain_skill_info_array or []:
            obj = {
                "skill_id": item.get("skill_id"),
                "level": item.get("level", item.get("skill_level", 1)),
            }
            payload_array.append(obj)

        names = [
            item.get("name", str(item.get("skill_id")))
            for item in gain_skill_info_array or []
        ]
        if names:
            log.info(f"--> BOT DECISION: Gaining Skills [ {', '.join(names)} ]")

        return self.call(
            f"{self.api_prefix}/gain_skills",
            {
                "gain_skill_info_array": payload_array,
                "current_turn": current_turn,
            },
        )

    def race_entry(self, program_id, current_turn, running_style=None):
        # Capture-verified: the real client sends ONLY {program_id, current_turn}
        # here. running_style is not part of this endpoint's schema (the server
        # ignores it, and sending it is a client-fingerprint deviation) - style
        # changes go through change_running_style in the entry->start window.
        # The kwarg is accepted-but-ignored so stale call sites can't crash.
        return self.call(
            f"{self.api_prefix}/race_entry",
            {"program_id": program_id, "current_turn": current_turn},
        )

    def change_running_style(self, program_id, running_style, current_turn):
        # Only valid between race_entry and race_start (102s outside that
        # window). The change persists server-side for the rest of the career.
        return self.call(
            f"{self.api_prefix}/change_running_style",
            {
                "program_id": program_id,
                "running_style": int(running_style),
                "current_turn": current_turn,
            },
        )

    def race_start(self, is_short, current_turn, running_style=None):
        # Same as race_entry: the genuine client never sends running_style here
        # (daily_race/daily_legend_race race_start DO take it - those are
        # separate, capture-verified endpoints).
        return self.call(
            f"{self.api_prefix}/race_start",
            {"is_short": is_short, "current_turn": current_turn},
        )

    def race_end(self, current_turn):
        return self.call(f"{self.api_prefix}/race_end", {"current_turn": current_turn})

    def race_reward(self, current_turn):
        return self.call(f"{self.api_prefix}/race_reward", {"current_turn": current_turn})

    def race_out(self, current_turn):
        return self.call(f"{self.api_prefix}/race_out", {"current_turn": current_turn})

    def race_continue(self, current_turn, continue_type):
        # A 205 here is the server saying no continue is offered for this result
        # (only an actual lost/failed race can be retried with a clock) - the
        # payload can't change, so retrying it just hammers the server.
        return self.call(
            f"{self.api_prefix}/continue",
            {"current_turn": current_turn, "continue_type": continue_type},
            retry_205=0,
        )

    def reserve_race(self, current_turn, add_race_array=None, cancel_race_array=None):
        return self.call(
            f"{self.api_prefix}/reserve_race",
            {
                "current_turn": current_turn,
                "add_race_array": add_race_array or [],
                "cancel_race_array": cancel_race_array or [],
            },
        )

    # ------------------------------------------------------------------
    # Unity Cup (scenario_id 2, api_prefix "single_mode_team") team races.
    # Endpoints + request keys confirmed from the captured session; body
    # keys marked "inferred" mirror the standard race-flow convention and
    # can be trimmed if the server rejects them.
    # ------------------------------------------------------------------
    def opponent_list(self, current_turn=0):
        return self.call(
            f"{self.api_prefix}/opponent_list",
            {"current_turn": current_turn},
        )

    def team_edit(self, team_data_array, current_turn=0):
        return self.call(
            f"{self.api_prefix}/team_edit",
            {"team_data_array": team_data_array, "current_turn": current_turn},
        )

    def team_race_analyze(self, race_set_id, current_turn=0):
        return self.call(
            f"{self.api_prefix}/team_race_analyze",
            {"race_set_id": race_set_id, "current_turn": current_turn},
        )

    def team_race_start(self, team_race_set_id, current_turn=0):
        return self.call(
            f"{self.api_prefix}/team_race_start",
            {"team_race_set_id": team_race_set_id, "current_turn": current_turn},
        )

    def team_race_out(self, current_turn):
        return self.call(
            f"{self.api_prefix}/team_race_out",
            {"current_turn": current_turn},
        )

    def team_race_end(self, current_turn):
        return self.call(
            f"{self.api_prefix}/team_race_end",
            {"current_turn": current_turn},
        )

    # ------------------------------------------------------------------
    # Grand Live (scenario_id 3, api_prefix "single_mode_live") square board.
    # Payload shapes confirmed on Global (two complete captures, 20260722):
    #   master_square  {square_id, current_turn}  -> take a square (technique/
    #     skill-hint/body-care AND song squares; 103/103 requests are exactly
    #     these two keys). Mastering a 40xxx song square is what QUEUES its song
    #     into next_live_id_array and it spends performance tokens.
    #   reserve_square {square_id, current_turn}  -> sets the cosmetic
    #     reserve_square_id ONLY (queues nothing, auto-cleared by the next
    #     master_square); the bot does not use it.
    #   live_start     {current_turn}             -> perform the live (all 5).
    # Flat bodies (this client sends no single_mode_*_request_common wrapper).
    # See career_bot/scenarios/grand_live.py for the queue-mechanic note.
    # ------------------------------------------------------------------
    def reserve_square(self, square_id, current_turn=0):
        return self.call(
            f"{self.api_prefix}/reserve_square",
            {"square_id": square_id, "current_turn": current_turn},
        )

    def master_square(self, square_id, current_turn=0):
        return self.call(
            f"{self.api_prefix}/master_square",
            {"square_id": square_id, "current_turn": current_turn},
        )

    def live_start(self, current_turn=0):
        return self.call(
            f"{self.api_prefix}/live_start",
            {"current_turn": current_turn},
        )

    # --- Veteran (trained chara) management -------------------------------
    # Account-level endpoints (no api_prefix). Protocol confirmed from the
    # 2026-07-07 IcarusRecorder capture: load takes only the base payload;
    # remove takes trained_chara_id_array and supports batch deletion.

    # ---- Native Auto Training ("idle" single mode) -------------------------
    # The game's own unattended-training feature: a career that runs itself for
    # a fixed 50 minutes and then waits to be collected. Confirmed from a live
    # capture (Navigator 20260726-145849):
    #
    #   idle_single_mode/pre_start  {"scenario_id": N}  ->  reserved_race_info,
    #                                                       last_idle_single_mode_start_info
    #   idle_single_mode/start      {"single_mode_start_request_common": {...},
    #                                "start_info": {...}}  ->  progress_info
    #   idle_single_mode/status     {}                    ->  progress_info
    #
    # progress_info carries start_time/end_time as "YYYY-MM-DD HH:MM:SS" strings
    # exactly 50 minutes apart, so completion is a clock comparison, not a poll
    # for a status flag. load/index also carries `idle_single_mode_load_info`
    # (null when nothing is running), which is how an in-progress session is
    # detected without any extra call.
    #
    # NOT YET CAPTURED: the call that COLLECTS a finished session. See
    # career_bot/auto_training.py.

    IDLE_PREFIX = "idle_single_mode"

    def idle_pre_start(self, scenario_id=0):
        """Pre-start info: the reserved-race decks, plus the previous session's
        settings (`last_idle_single_mode_start_info`, null if there is none) so a
        repeat run can reuse them."""
        return self.call(f"{self.IDLE_PREFIX}/pre_start", {"scenario_id": int(scenario_id or 0)})

    def _idle_start_once(self, start_common, start_info):
        return self.call(
            f"{self.IDLE_PREFIX}/start",
            {
                "single_mode_start_request_common": start_common,
                "start_info": start_info,
            },
        )

    def has_succession_offset(self, p1, p2):
        """True when a start has already succeeded with the parents in THIS order.

        The learned offset is keyed by the ordered pair, so this is also how the
        caller knows which way round to send the parents — without it, a start
        that only works reversed re-runs the whole sweep on the stored order
        every single time, which makes looping unusable.
        """
        UmaClient._load_succession_cache()
        key = (int(self.viewer_id or 0), int(p1 or 0), int(p2 or 0))
        return key in UmaClient._succession_point_cache

    def idle_start_corrected(self, start_common, start_info, hint=0, budget=None,
                             rearm=None):
        """`idle_start` with the same succession-point correction a normal career
        start does — because Icarus's point formula is routinely wrong and the
        two endpoints report it differently.

        `<prefix>/start` answers a wrong `current_succession_rank_point` with
        **102**, which start_career already recovers from (cached pair offset ->
        account offset -> item-75 hint -> bounded sweep). `idle_single_mode/start`
        answers the same mistake with a bare **205**, so nothing recovered and the
        session simply never began. Measured on one account: Icarus computed 221
        and 921 for a pair the game valued at 174.

        Negative offsets are tried first here. A normal start sweeps positives
        first (rank points only accumulate), but every idle sample so far has the
        computed value too HIGH, so this direction finds it far sooner.
        """
        computed = int(start_common.get("current_succession_rank_point") or 0)
        p1 = int(start_common.get("start_chara", {}).get("succession_trained_chara_id_1") or 0)
        p2 = int(start_common.get("start_chara", {}).get("succession_trained_chara_id_2") or 0)
        cache_key = (int(self.viewer_id or 0), p1, p2)
        vkey = int(self.viewer_id or 0)
        UmaClient._load_succession_cache()
        cache = UmaClient._succession_point_cache
        voff = UmaClient._succession_offset_by_viewer

        seen = set()
        first_error = None
        # Shared across Agenda variants by the caller, so a start that has to try
        # several Agenda lengths can't multiply this search by three.
        if budget is None:
            budget = [170]

        def attempt(point):
            nonlocal first_error
            point = int(point)
            if point < 0 or point in seen:
                return None
            if budget[0] <= 0:
                return None
            budget[0] -= 1
            seen.add(point)
            if len(seen) % 40 == 0:
                log.info(
                    "Independent Training still searching for the start point "
                    "(%d tried, %d left in budget)…", len(seen), budget[0],
                )
            body = dict(start_common)
            body["current_succession_rank_point"] = point
            try:
                # Re-arm before EVERY attempt. The real client calls
                # pre_single_mode/index + idle_single_mode/pre_start immediately
                # before each start — three times in one capture, once per start.
                # Firing repeated starts off a single pre_start is not something
                # the game ever does, and it is why a sweep that DID send the
                # exact accepted point still got 205: only the first attempt off
                # a given pre_start is armed, and that one carries the computed
                # (wrong) point.
                if rearm is not None:
                    # The re-arm also hands back whatever the refresh revealed.
                    # This is not optional bookkeeping: `pre_single_mode/index`
                    # RE-ROLLS the borrowable friend roster on every call, so the
                    # card picked before the refresh is no longer on offer after
                    # it. TP and money are the same story — a snapshot the server
                    # checks against its own. A 205 is literally "your data
                    # disagrees with mine", so every attempt has to carry values
                    # read from the refresh that immediately preceded it.
                    updates = dict(rearm() or {})
                    chara = updates.pop("start_chara", None)
                    body.update(updates)
                    if chara:
                        body["start_chara"] = dict(
                            body.get("start_chara") or {}, **chara
                        )
                res = self._idle_start_once(body, start_info)
            except Exception as err:
                if first_error is None:
                    first_error = err
                if error_code(err) not in (102, 205):
                    raise
                return None
            offset = point - computed
            cache[cache_key] = offset
            voff[vkey] = offset
            if offset:
                UmaClient._save_succession_cache()
                log.info(
                    "Independent Training start point corrected: %s "
                    "(computed %s, offset %+d cached for next time)",
                    point, computed, offset,
                )
            return res

        res = attempt(computed)
        if res is not None:
            return res

        for candidate in (
            computed + (cache.get(cache_key) or 0),
            computed + (voff.get(vkey) or 0),
            # The caller's hint is the account's succession-point *currency*
            # balance, which is only coincidentally a plausible rank point — on
            # the measured account it was 921 against a real value of 174. Try
            # it only when it lands near the computed value; otherwise it is a
            # guaranteed-wrong candidate and a wasted call.
            int(hint or 0) if abs(int(hint or 0) - computed) <= 200 else 0,
        ):
            if candidate and candidate != computed:
                res = attempt(candidate)
                if res is not None:
                    return res

        if budget[0] <= 0:
            # No budget left to sweep with. Saying "Searching…" here is a lie
            # that repeats once per shape the caller tries and reads like a hang.
            raise first_error if first_error else RuntimeError(
                "idle_single_mode/start refused every succession-point candidate"
            )
        log.warning(
            "Independent Training needs a succession-point correction "
            "(computed %s was rejected). Searching…", computed,
        )
        # Strictly negatives first, not interleaved. Every idle sample observed
        # has the computed value too HIGH, and interleaving doubles the distance
        # to the answer: the measured -47 sits at candidate 93 interleaved but
        # candidate 47 here.
        sweep = [-step for step in range(1, 81)]
        sweep += [step for step in range(1, 81)]
        for offset in sweep:
            res = attempt(computed + offset)
            if res is not None:
                return res
        raise first_error if first_error else RuntimeError(
            "idle_single_mode/start refused every succession-point candidate"
        )

    def idle_start(self, start_common, start_info):
        """Begin an Auto Training session.

        `start_common` is the same object a normal career start sends (build it
        with `start_career(..., build_only=True)`); `start_info` carries the
        auto-training policy: training_policy_ground_type,
        training_policy_param_rate_set_id, priority_skill_array, race_array.
        """
        return self.call(
            f"{self.IDLE_PREFIX}/start",
            {
                "single_mode_start_request_common": start_common,
                "start_info": start_info,
            },
        )

    def idle_status(self):
        """Current session's progress_info (chara_info + start_time/end_time)."""
        return self.call(f"{self.IDLE_PREFIX}/status", {})

    def idle_end(self):
        """Close a finished session and collect it.

        Returns `end_info` (the full career state, plus `reward_summary_info`,
        `win_saddle_id_array`, `resume_factor_select`, `unchecked_event_array`)
        and `progress_log_info` (per-support stat gains for the results screen).

        After this the career is a NORMAL finished career: the real client goes
        on to <prefix>/gain_skills -> <prefix>/factor_select -> <prefix>/finish,
        exactly as it does for a hand-played one.
        """
        return self.call(f"{self.IDLE_PREFIX}/end", {})

    def idle_check_progress_log(self):
        """Acknowledge the results screen. The real client sends this straight
        after `end`; it returns a bare ack."""
        return self.call(f"{self.IDLE_PREFIX}/check_progress_log", {})

    def change_support_card_deck(self, deck_array):
        """Save support-card decks. ``deck_array`` is all 10 slots.

        The game's real deck-save endpoint. Its response echoes back ONLY the
        decks whose contents actually differed from stored state, and ``[]``
        when nothing changed - so the echo is a diff, not a confirmation of the
        whole array. Verified against captures on 2026-09-05.
        """
        return self.call("support_card_deck/change_party",
                         {"support_card_deck_array": deck_array})

    def trained_chara_load(self):
        return self.call("trained_chara/load")

    def trained_chara_remove(self, trained_chara_id_array):
        return self.call(
            "trained_chara/remove",
            {"trained_chara_id_array": [int(i) for i in trained_chara_id_array]},
        )

    # --- Dailies: Team Trials (team_stadium) -----------------------------
    # Account-level (no api_prefix). Protocol from the 2026-07-07 capture:
    # index -> opponent_list -> decide_frame_order(full opponent echo) ->
    # start -> replay_check(round=5 skips all 5) -> all_race_end.

    def team_stadium_index(self):
        return self.call("team_stadium/index")

    def team_stadium_opponent_list(self):
        return self.call("team_stadium/opponent_list")

    def team_stadium_decide_frame_order(self, opponent_info):
        return self.call(
            "team_stadium/decide_frame_order", {"opponent_info": opponent_info}
        )

    def team_stadium_start(self, item_id_array=None):
        return self.call("team_stadium/start", {"item_id_array": item_id_array or []})

    def team_stadium_replay_check(self, round=5):
        # round == number of races skips the whole slate (we always skip all 5).
        return self.call("team_stadium/replay_check", {"round": int(round)})

    def team_stadium_all_race_end(self):
        return self.call("team_stadium/all_race_end")

    # --- Dailies: Daily Race & Legend Race -------------------------------
    def daily_race_index(self):
        return self.call("daily_race/index")

    def daily_race_race_entry(self, daily_race_id, trained_chara_id):
        return self.call(
            "daily_race/race_entry",
            {
                "daily_race_id": int(daily_race_id),
                "trained_chara_id": int(trained_chara_id),
            },
        )

    def daily_race_reflect_item_effect(self, item_id_array=None):
        return self.call(
            "daily_race/reflect_item_effect", {"item_id_array": item_id_array or []}
        )

    def daily_race_race_start(self, running_style, is_short=0):
        return self.call(
            "daily_race/race_start",
            {"running_style": int(running_style), "is_short": int(is_short)},
        )

    def daily_race_replay_check(self, race_result_array):
        return self.call(
            "daily_race/replay_check", {"race_result_array": race_result_array}
        )

    def daily_legend_race_index(self):
        return self.call("daily_legend_race/index")

    def daily_legend_race_race_entry(self, daily_legend_race_id, trained_chara_id):
        return self.call(
            "daily_legend_race/race_entry",
            {
                "daily_legend_race_id": int(daily_legend_race_id),
                "trained_chara_id": int(trained_chara_id),
            },
        )

    def daily_legend_race_reflect_item_effect(self, item_id_array=None):
        return self.call(
            "daily_legend_race/reflect_item_effect",
            {"item_id_array": item_id_array or []},
        )

    def daily_legend_race_race_start(self, running_style, is_short=0):
        return self.call(
            "daily_legend_race/race_start",
            {"running_style": int(running_style), "is_short": int(is_short)},
        )

    def daily_legend_race_replay_check(self):
        # Legend replay_check takes no race_result_array (confirmed from capture).
        return self.call("daily_legend_race/replay_check", {})

    # --- Dailies: Item exchange shop -------------------------------------
    def item_show_exchange(self):
        return self.call("item/show_exchange")

    def item_exchange_multi(self, exchange_item_info_array, use_item_info_array, get_list_time):
        # Buy multiple shop items at once. use_item_info_array carries the
        # CURRENT balance of each pay currency (server deducts the real cost);
        # get_list_time echoes the show_exchange server time (UTC).
        return self.call(
            "item/exchange_multi",
            {
                "exchange_item_info_array": exchange_item_info_array,
                "use_item_info_array": use_item_info_array,
                "get_list_time": get_list_time,
            },
        )

    # --- Claimables: present box (gifts) + missions --------------------------
    # Payload shapes verified from a live capture (2026-07-10).
    def present_index(self, offset=0, limit=100):
        return self.call(
            "present/index",
            {
                "time_filter_type": 0,
                "category_filter_type": [0],
                "offset": int(offset),
                "limit": int(limit),
                "is_asc": True,
            },
        )

    def present_receive_all(self, current_rest_present_num):
        return self.call(
            "present/receive_all",
            {
                "time_filter_type": 0,
                "category_filter_type": [0],
                "is_asc": True,
                "current_rest_present_num": int(current_rest_present_num),
            },
        )

    def mission_index(self):
        return self.call("mission/index", {})

    def mission_receive(self, mission_id_array):
        return self.call(
            "mission/receive", {"mission_id_array": list(mission_id_array)}
        )
