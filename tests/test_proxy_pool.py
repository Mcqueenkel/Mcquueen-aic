"""Offline checks for scripts/proxy_pool.py. No network: a local echo server
plays the game, local fake proxies play the upstreams.

    python tests/test_proxy_pool.py

The rules these guard: game traffic goes through the upstream in use and stays
on it; a dead upstream is dropped and the next one used in the same CONNECT;
a game request with no answer cuts the tunnel and drops its upstream, but a
small unanswered write (an HTTP/2 window update) does not; the next upstream
is the one that survived the most re-tests; proxy logins work for http and
socks5 and never reach the log; your own list keeps its order and format;
other hosts go direct; the free list skips SOCKS4 and router ports.
"""

from __future__ import annotations

import asyncio
import base64
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import proxy_pool as pp  # noqa: E402

FAILURES: list[str] = []
USED: list[str] = []
AUTH: list[str] = []
REQUEST = b"ping" * 75          # 300 bytes: request-sized, starts the stall clock
CRLF2 = b"\r\n\r\n"


def check(label: str, condition: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if condition else 'FAIL'} {label} {'' if condition else detail}")
    if not condition:
        FAILURES.append(label)


async def echo(r, w):
    while data := await r.read(1024):
        w.write(data)
        await w.drain()
    w.close()


async def splice_to(r, w, port: int, reply: bytes) -> None:
    ur, uw = await asyncio.open_connection("127.0.0.1", port)
    w.write(reply)
    await asyncio.gather(pp.pipe(r, uw), pp.pipe(ur, w))


async def fake_http_proxy(r, w):
    head = await r.readuntil(CRLF2)
    USED.append("http")
    port = int(head.split(b" ")[1].decode().rpartition(":")[2])
    await splice_to(r, w, port, b"HTTP/1.1 200 Connection established" + CRLF2)


async def fake_http_auth(r, w):
    """An http proxy that wants user u / password p@ss."""
    head = await r.readuntil(CRLF2)
    if b"Proxy-Authorization: Basic " + base64.b64encode(b"u:p@ss") not in head:
        w.write(b"HTTP/1.1 407 Proxy Authentication Required" + CRLF2)
        w.close()
        return
    AUTH.append("http")
    port = int(head.split(b" ")[1].decode().rpartition(":")[2])
    await splice_to(r, w, port, b"HTTP/1.1 200 Connection established" + CRLF2)


async def socks5_connect(r, w) -> None:
    _ver, _cmd, _rsv, _atyp, n = await r.readexactly(5)
    await r.readexactly(n)
    port = int.from_bytes(await r.readexactly(2), "big")
    await splice_to(r, w, port, b"\x05\x00\x00\x01" + bytes(4) + b"\x00\x00")


async def fake_socks5(r, w):
    await r.readexactly(3)
    w.write(b"\x05\x00")
    USED.append("socks5")
    await socks5_connect(r, w)


async def fake_socks5_auth(r, w):
    """A socks5 proxy that wants user u / password p@ss (RFC 1929)."""
    _ver, n = await r.readexactly(2)
    if b"\x02" not in await r.readexactly(n):
        w.write(b"\x05\xff")
        w.close()
        return
    w.write(b"\x05\x02")
    _v, ulen = await r.readexactly(2)
    user = await r.readexactly(ulen)
    pw = await r.readexactly((await r.readexactly(1))[0])
    ok = (user, pw) == (b"u", b"p@ss")
    w.write(b"\x01" + (b"\x00" if ok else b"\x01"))
    if not ok:
        w.close()
        return
    AUTH.append("socks5")
    await socks5_connect(r, w)


async def black_hole(r, w):
    """Accepts the tunnel, then never forwards a byte - a proxy that died mid-way."""
    await r.readuntil(CRLF2)
    w.write(b"HTTP/1.1 200 Connection established" + CRLF2)
    try:
        while await r.read(1024):
            pass
    except ConnectionError:
        pass


async def roundtrip(relay_port: int, host: str, port: int, payload: bytes = REQUEST,
                    wait: float = 5) -> bytes:
    r, w = await asyncio.open_connection("127.0.0.1", relay_port)
    w.write(f"CONNECT {host}:{port} HTTP/1.1".encode() + CRLF2)
    head = await r.readuntil(CRLF2)
    if b" 200 " not in head:
        w.close()
        return head
    w.write(payload)
    try:
        got = await asyncio.wait_for(r.readexactly(len(payload)), wait)
    except (asyncio.IncompleteReadError, asyncio.TimeoutError, ConnectionError):
        got = b""
    w.close()
    return got


async def main() -> None:
    servers = [await asyncio.start_server(f, "127.0.0.1", 0)
               for f in (echo, fake_http_proxy, fake_socks5, black_hole, fake_http_auth,
                         fake_socks5_auth, pp.handle)]
    game, http_up, socks_up, hole_up, hauth_up, sauth_up, relay = (
        s.sockets[0].getsockname()[1] for s in servers)
    pp.GAME_HOSTS = {"localhost"}          # the "game"; 127.0.0.1 counts as another host
    pp.OWN_LIST = "test"                   # keeps save() off the real state file
    dead = "http://127.0.0.1:1"            # nothing listens there
    good = f"http://127.0.0.1:{http_up}"
    hole = f"http://127.0.0.1:{hole_up}"

    pp.pool[:] = [dead, good, f"socks5://127.0.0.1:{socks_up}"]
    got = await roundtrip(relay, "localhost", game)
    check("dead upstream skipped, tunnel works through the next", got == REQUEST, repr(got[:20]))
    check("dead upstream dropped from the pool", dead not in pp.pool, str(pp.pool))
    check("went through the http upstream", USED == ["http"], str(USED))

    await roundtrip(relay, "localhost", game)
    check("sticks to the same upstream", USED == ["http", "http"], str(USED))

    pp.pool[:] = [f"socks5://127.0.0.1:{socks_up}"]
    got = await roundtrip(relay, "localhost", game)
    check("socks5 upstream tunnels", got == REQUEST and USED[-1] == "socks5", f"{got[:20]!r} {USED}")

    USED.clear()
    got = await roundtrip(relay, "127.0.0.1", game)
    check("non-game host goes direct", got == REQUEST and not USED, f"{got[:20]!r} {USED}")

    pp.STALL = 1
    pp.pool[:] = [hole, good]
    await roundtrip(relay, "localhost", game, payload=b"k" * 40, wait=2.5)
    check("small unanswered write (h2 window update) keeps the upstream", hole in pp.pool, str(pp.pool))
    got = await roundtrip(relay, "localhost", game, wait=4)
    check("silent upstream: tunnel cut and upstream dropped", got == b"" and hole not in pp.pool,
          f"{got[:20]!r} {pp.pool}")
    got = await roundtrip(relay, "localhost", game)
    check("next request goes through the good upstream", got == REQUEST, repr(got[:20]))
    pp.STALL = 15

    wrong = f"http://u:nope@127.0.0.1:{hauth_up}"
    pp.pool[:] = [wrong, f"http://u:p%40ss@127.0.0.1:{hauth_up}"]
    got = await roundtrip(relay, "localhost", game)
    check("http proxy login: wrong password dropped, right one tunnels",
          got == REQUEST and wrong not in pp.pool and AUTH == ["http"], f"{got[:20]!r} {AUTH}")
    pp.pool[:] = [f"socks5://u:p%40ss@127.0.0.1:{sauth_up}"]
    got = await roundtrip(relay, "localhost", game)
    check("socks5 proxy login tunnels", got == REQUEST and AUTH[-1:] == ["socks5"], f"{got[:20]!r} {AUTH}")
    shown = pp.show("http://u:secret@1.2.3.4:80")
    check("log shows a proxy without its password", shown == "http://1.2.3.4:80", shown)

    own = pp.candidates("# my proxies\n1.2.3.4:8080\nsocks5h://u:p@5.6.7.8:1080  # paid\n\n"
                        "http://9.9.9.9:4145\nnot a proxy\nftp://1.1.1.1:21\n", own=True)
    check("own list: comments, bare host:port, socks5h, kept in order",
          own == ["http://1.2.3.4:8080", "socks5://u:p@5.6.7.8:1080", "http://9.9.9.9:4145"], str(own))
    free = pp.candidates("http://1.2.3.4:80 socks4://1.2.3.4:1080 socks5://1.2.3.4:1080 "
                         "socks5://1.2.3.4:4145 http://1.2.3.4:5678 https://1.2.3.4:443")
    check("free list: only http/socks5, no router ports",
          sorted(free) == ["http://1.2.3.4:80", "socks5://1.2.3.4:1080"], str(free))

    a, b, c = "http://a:1", "http://b:1", "http://c:1"
    pp.pool[:] = [a, b, c]
    pp.passes.update({a: 1, b: 1, c: 9})
    pp.latency.update({a: 2.0, b: 0.5, c: 2.0})
    order = pp.rank([a, b, c])
    check("in use stays first, then most re-tests survived, then fastest", order == [a, c, b], str(order))
    order = pp.rank([b, c])
    check("in use died: the longest survivor takes over", order == [c, b], str(order))

    ups = [f"http://p{i}:1" for i in range(6)]
    pp.pool[:] = list(ups)
    alive_now = ups[:1]
    real_test = pp.test

    async def fake_test(snap):
        return [u for u in snap if u in alive_now]

    pp.test = fake_test
    await pp.retest()
    check("most of the pool failing at once is doubted: pool kept", pp.pool == ups, str(pp.pool))
    await pp.retest()
    check("failing again: dropped", pp.pool == ups[:1], str(pp.pool))
    pp.pool[:] = list(ups)
    alive_now = ups[:4]
    await pp.retest()
    check("a few failing: dropped at once", pp.pool == ups[:4], str(pp.pool))
    pp.test = real_test

    for s in servers:
        s.close()


asyncio.run(main())
print("FAILED:" if FAILURES else "all ok", *FAILURES)
sys.exit(1 if FAILURES else 0)
