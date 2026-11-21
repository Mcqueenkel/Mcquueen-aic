"""Proxy relay for the game API, for a server whose address the game refuses.

Some providers' addresses (Tencent Cloud, VPNs, Cloudflare WARP, ...) get no
answer or a 403 from the game's CDN. Turn this on with
``sudo bash scripts/proxy_setup.sh``: it runs this relay as aic-proxy.service and
points aic.service's HTTPS at it (https_proxy=http://127.0.0.1:8899, see
deploy/vps/aic-proxy-dropin.conf). README "Proxy relay" has the full story.

Only CONNECTs to the game API go through an upstream proxy; everything else
(Steam, Discord) goes out directly. TLS runs end to end through the tunnel and
the farm checks the game's certificate, so an upstream sees only encrypted bytes.

Where upstreams come from (``AIC_PROXY_LIST`` in /etc/umabot.env):
  * empty (default): the proxifly free public list, fetched at run time.
    Free proxies die often; SOCKS4 and the ports hijacked routers usually
    expose are skipped.
  * a file path: your own proxies, one per line - ``http://user:pass@host:port``,
    ``socks5://user:pass@host:port`` or bare ``host:port`` (= http); ``#`` comments.
    Keep that file outside the repository (chmod 600): it holds passwords.

A candidate is kept only if the game answers through it quickly (404 from the
API root, not the CDN's 403 or a reset). The whole pool is re-tested every
minute; the farm sticks to one upstream until it fails, then moves to the one
that has survived the most re-tests, because the game drops a session (217) when
its address changes mid-visit. A game request with no answer in STALL seconds
drops its upstream and cuts the tunnel, so the farm retries at once instead of
waiting out its own 30 s timeout. The working pool of a free list is saved to
state/<profile>/proxy_pool.json (git-ignored), so a restart is ready in seconds.
Proxy passwords never reach the log or the process list.

    python scripts/proxy_pool.py        # serve on 127.0.0.1:8899
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import random
import time
import urllib.request
from pathlib import Path
from urllib.parse import unquote, urlsplit

LISTEN = ("127.0.0.1", int(os.environ.get("AIC_PROXY_PORT", "8899")))
GAME_HOSTS = {"api.games.umamusume.com"}
FREE_LIST_URL = "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/all/data.txt"
OWN_LIST = os.environ.get("AIC_PROXY_LIST", "").strip()
STATE = (Path(__file__).resolve().parent.parent / "state"
         / os.environ.get("AIC_PROFILE", "aic") / "proxy_pool.json")
CHECK_URL = "https://api.games.umamusume.com/"
SKIP_PORTS = {"4145", "5678"}
WANT = 25            # stop testing candidates once this many work
MIN_POOL = 10        # refill below this
REFRESH = 30 * 60    # and at least this often
RECHECK = 60         # re-test the whole pool this often, so dead ones go before the farm hits them
MAX_LATENCY = 2.5    # slower candidates are mostly overloaded and die soon
TRIES = 2            # upstreams tried per CONNECT (the farm's own timeout is 30s)
TIMEOUT = 6          # seconds per connect / handshake step
STALL = 15           # seconds a game request may go unanswered before its upstream counts as dead
REQUEST_BYTES = 200  # smaller client writes (HTTP/2 window updates, pings, TLS alerts) get no
                     # answer by design, so they never start the stall clock

pool: list[str] = []          # working upstreams; pool[0] is the one in use
passes: dict[str, int] = {}   # re-tests survived in a row
latency: dict[str, float] = {}
_refill_lock = asyncio.Lock()


def log(msg: str) -> None:
    print(time.strftime("%Y-%m-%d %H:%M:%S"), msg, flush=True)


def show(up: str | None) -> str:
    """``up`` without its user:password, for the log."""
    if not up:
        return "NONE"
    u = urlsplit(up)
    return f"{u.scheme}://{u.hostname}:{u.port}"


def normalize(line: str) -> str | None:
    """One proxy URL (http:// or socks5://) from a list line, or None."""
    line = line.split("#", 1)[0].strip()
    if not line:
        return None
    if "://" not in line:
        line = "http://" + line
    if line.startswith("socks5h://"):
        line = "socks5://" + line[len("socks5h://"):]
    try:
        u = urlsplit(line)
        ok = u.scheme in ("http", "socks5") and u.hostname and u.port
    except ValueError:
        return None
    return line if ok else None


def candidates(text: str, own: bool = False) -> list[str]:
    """Usable proxies from a list. The free list is shuffled and filtered; your
    own list is kept as written, in order."""
    out = []
    for line in text.splitlines() if own else text.split():
        up = normalize(line)
        if up and (own or (urlsplit(up).port and str(urlsplit(up).port) not in SKIP_PORTS)):
            out.append(up)
    if not own:
        random.shuffle(out)
    return list(dict.fromkeys(out))


def load_list() -> str:
    if OWN_LIST:
        return Path(OWN_LIST).read_text(encoding="utf-8")
    return urllib.request.urlopen(FREE_LIST_URL, timeout=30).read().decode()


def save() -> None:
    """Remember a free list's working pool across restarts (never your own list)."""
    if OWN_LIST:
        return
    try:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE.with_suffix(".tmp")
        tmp.write_text(json.dumps(pool), encoding="utf-8")
        os.replace(tmp, STATE)
    except OSError:
        pass


def load_saved() -> list[str]:
    if OWN_LIST:
        return []
    try:
        return [u for u in json.loads(STATE.read_text(encoding="utf-8")) if normalize(u)]
    except (OSError, ValueError):
        return []


def forget(up: str) -> None:
    if up in pool:
        pool.remove(up)
    passes.pop(up, None)
    latency.pop(up, None)


def rank(alive: list[str]) -> list[str]:
    """The upstream in use first while it lives; the rest by survival, then speed."""
    head = [pool[0]] if pool and pool[0] in alive else []
    rest = sorted((u for u in dict.fromkeys(alive) if u not in head),
                  key=lambda u: (-passes.get(u, 0), latency.get(u, 99.0)))
    return head + rest


async def probe(up: str) -> float | None:
    """Seconds for a full HTTPS request to the game through ``up``, or None.
    The proxy goes to curl on stdin, so its password is not in the process list."""
    url = up.replace("socks5://", "socks5h://", 1).replace("\\", "\\\\").replace('"', '\\"')
    start = time.monotonic()
    proc = await asyncio.create_subprocess_exec(
        "curl", "-K", "-", "-s", "-o", "/dev/null", "-m", str(int(MAX_LATENCY) + 1),
        "-w", "%{http_code}", CHECK_URL,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL)
    out, _ = await proc.communicate(f'proxy = "{url}"\n'.encode())
    took = time.monotonic() - start
    return took if out == b"404" and took <= MAX_LATENCY else None


async def test(ups: list[str]) -> list[str]:
    """Probe ``ups``; returns the ones that pass and updates their records."""
    ok = []
    for up, lat in zip(ups, await asyncio.gather(*map(probe, ups))):
        if lat:
            passes[up] = passes.get(up, 0) + 1
            latency[up] = lat
            ok.append(up)
        else:
            passes.pop(up, None)
            latency.pop(up, None)
    return ok


async def refill() -> None:
    """Re-test the pool and top it up from the list (one refill at a time)."""
    async with _refill_lock:
        try:
            text = await asyncio.to_thread(load_list)
        except Exception as exc:  # noqa: BLE001 - keep what we have
            log(f"proxy list unreadable: {type(exc).__name__}: {exc}")
            text = ""
        pool[:] = rank(await test(list(pool)))
        cands = [c for c in candidates(text, own=bool(OWN_LIST)) if c not in pool]
        for i in range(0, len(cands), 200):
            if len(pool) >= WANT:
                break
            # each batch joins the pool at once, so the farm need not wait for WANT
            pool[:] = rank(list(pool) + await test(cands[i:i + 200]))
        save()
        log(f"pool: {len(pool)} working, using {show(pool[0] if pool else None)}"
            + (" (own list)" if OWN_LIST else ""))


async def open_upstream(up: str, host: str, port: int):
    u = urlsplit(up)
    user, pw = unquote(u.username or ""), unquote(u.password or "")
    r, w = await asyncio.wait_for(asyncio.open_connection(u.hostname, u.port), TIMEOUT)
    try:
        if u.scheme == "http":
            auth = ""
            if user:
                token = base64.b64encode(f"{user}:{pw}".encode()).decode()
                auth = f"Proxy-Authorization: Basic {token}\r\n"
            w.write(f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n{auth}\r\n".encode())
            head = await asyncio.wait_for(r.readuntil(b"\r\n\r\n"), TIMEOUT)
            if head.split(b" ", 2)[1:2] != [b"200"]:
                status = head.split(b"\r\n", 1)[0][:40]
                raise OSError(f"CONNECT refused: {status!r}")
        else:  # socks5; the proxy resolves the name
            method = b"\x02" if user else b"\x00"
            w.write(b"\x05\x01" + method)
            if await asyncio.wait_for(r.readexactly(2), TIMEOUT) != b"\x05" + method:
                raise OSError("socks5 greeting refused")
            if user:
                ub, pb = user.encode(), pw.encode()
                w.write(b"\x01" + bytes([len(ub)]) + ub + bytes([len(pb)]) + pb)
                if (await asyncio.wait_for(r.readexactly(2), TIMEOUT))[1] != 0:
                    raise OSError("socks5 login refused")
            name = host.encode()
            w.write(b"\x05\x01\x00\x03" + bytes([len(name)]) + name + port.to_bytes(2, "big"))
            rep = await asyncio.wait_for(r.readexactly(4), TIMEOUT)
            if rep[1] != 0:
                raise OSError(f"socks5 connect refused ({rep[1]})")
            n = {1: 4, 4: 16}.get(rep[3]) or (await r.readexactly(1))[0]
            await r.readexactly(n + 2)
        return r, w
    except BaseException:
        w.close()
        raise


async def connect(host: str, port: int):
    """(reader, writer, upstream) - upstream is None for a direct connection."""
    if host not in GAME_HOSTS:
        r, w = await asyncio.wait_for(asyncio.open_connection(host, port), 15)
        return r, w, None
    for _ in range(TRIES):
        if not pool:
            if not _refill_lock.locked():
                asyncio.create_task(refill())
            for _ in range(40):       # up to 20 s for the first working upstream
                await asyncio.sleep(0.5)
                if pool:
                    break
            if not pool:
                break
        up = pool[0]
        try:
            r, w = await open_upstream(up, host, port)
            return r, w, up
        except Exception as exc:  # noqa: BLE001 - drop it, try the next
            forget(up)
            log(f"dropped {show(up)} ({type(exc).__name__}: {exc}); next: {show(pool[0] if pool else None)}")
    raise OSError("no working upstream")


async def pipe(r: asyncio.StreamReader, w: asyncio.StreamWriter, on_data=None) -> None:
    try:
        while data := await r.read(65536):
            if on_data:
                on_data(len(data))
            w.write(data)
            await w.drain()
    except Exception:  # noqa: BLE001 - either side hung up
        pass
    finally:
        w.close()


async def tunnel(cr, cw, ur, uw, up: str | None) -> None:
    """Splice both ways; for a game tunnel, cut it when the upstream goes silent."""
    waiting = [0.0]   # when the client last sent a request with no answer since (0 = answered)

    def sent(n: int) -> None:
        if n >= REQUEST_BYTES and not waiting[0]:
            waiting[0] = time.monotonic()

    def answered(_n: int) -> None:
        waiting[0] = 0.0

    async def watch() -> None:
        while True:
            await asyncio.sleep(1)
            if waiting[0] and time.monotonic() - waiting[0] > STALL:
                forget(up)
                log(f"dropped {show(up)} (no answer in {STALL}s); next: {show(pool[0] if pool else None)}")
                uw.close()
                cw.close()
                return

    watcher = asyncio.create_task(watch()) if up else None
    await asyncio.gather(pipe(cr, uw, sent if up else None), pipe(ur, cw, answered if up else None))
    if watcher:
        watcher.cancel()


async def handle(cr: asyncio.StreamReader, cw: asyncio.StreamWriter) -> None:
    try:
        head = await asyncio.wait_for(cr.readuntil(b"\r\n\r\n"), 30)
        method, target = head.split(b" ", 2)[:2]
        if method != b"CONNECT":
            cw.write(b"HTTP/1.1 405 Method Not Allowed\r\n\r\n")
            cw.close()
            return
        host, _, port = target.decode().rpartition(":")
        ur, uw, up = await connect(host, int(port))
    except Exception:  # noqa: BLE001
        cw.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
        cw.close()
        return
    cw.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
    await tunnel(cr, cw, ur, uw, up)


_doubted = False


async def retest() -> None:
    """Re-test the pool and drop the dead. Most of it failing at once is usually this
    server (busy CPU, network blip), not the proxies: the first time, keep them."""
    global _doubted
    snap = list(pool)
    alive = set(await test(snap))
    dead = [u for u in snap if u not in alive]
    if len(snap) >= 4 and len(dead) > len(snap) // 2 and not _doubted:
        _doubted = True
        log(f"re-test: {len(dead)} of {len(snap)} failed at once - keeping them one more round")
        return
    _doubted = False
    if dead:
        for up in dead:
            forget(up)
        log(f"re-test dropped {len(dead)}; {len(pool)} left, using {show(pool[0] if pool else None)}")
    pool[:] = rank(list(pool))
    save()


async def keeper() -> None:
    last = time.monotonic()
    while True:
        await asyncio.sleep(RECHECK)
        await retest()
        if len(pool) < MIN_POOL or time.monotonic() - last > REFRESH:
            await refill()
            last = time.monotonic()


async def main() -> None:
    server = await asyncio.start_server(handle, *LISTEN)
    pool[:] = load_saved()          # last run's pool: usable at once, re-tested by refill
    log(f"listening on {LISTEN[0]}:{LISTEN[1]}, "
        + (f"own list {OWN_LIST}" if OWN_LIST else f"free list, {len(pool)} saved"))
    await refill()
    asyncio.create_task(keeper())
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
