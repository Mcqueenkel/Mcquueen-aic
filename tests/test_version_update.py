"""A game client update (result code 204) is detected and fixed by the farm itself.

The version Steam ships is read by a node helper (aic.steam.VERSION_JS - proved
live on the VPS); here the helper's output is parsed, the adopt rules are checked
on a real auth_config, and rotate.run is driven through a lockout to show every
locked-out account goes again as soon as the new version is adopted.
"""

from __future__ import annotations

import sys
import tempfile
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from aic import auth, rotate, steam  # noqa: E402
from aic.config import Settings      # noqa: E402
import test_career_clock as cc       # noqa: E402

FAILURES: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if cond else 'FAIL'} {label}" + (f"  {detail}" if not cond else ""))
    if not cond:
        FAILURES.append(label)


def test_parse() -> None:
    print("the helper's output: exactly one x.y.z version, else nothing")
    ok = '{"type":"version","versions":["1.35.2"],"manifest":"8259446285230536131"}'
    check("one version", steam.parse_version_output("noise\n" + ok) == "1.35.2")
    check("an error -> None", steam.parse_version_output('{"type":"error","message":"timeout"}') is None)
    check("two versions -> None (ambiguous)",
          steam.parse_version_output('{"type":"version","versions":["1.0.1","1.35.2"]}') is None)
    check("not x.y.z -> None", steam.parse_version_output('{"type":"version","versions":["1.35"]}') is None)
    check("no output -> None", steam.parse_version_output("") is None)


def _settings(tmp: str) -> Settings:
    s = Settings(icarus_root=Path(tmp), profile="t", runtime_dir=Path(tmp) / "rt",
                 state_dir=Path(tmp) / "state")
    s.auth_config_path.parent.mkdir(parents=True, exist_ok=True)
    return s


def test_adopt() -> None:
    print("adopt only a NEWER version, at most once per VERSION_CHECK_EVERY")
    posts, asked = [], []
    real = steam.latest_game_version
    import aic.notify as notify
    real_post = notify.post
    notify.post = lambda payload, url="": posts.append(payload) or True
    try:
        with tempfile.TemporaryDirectory() as tmp:
            s = _settings(tmp)
            auth.save_auth_config(s, {"app_ver": "1.35.1", "res_ver": "10008010",
                                      "steam_refresh_token": "tok"})
            shipped = {"v": "1.35.2"}
            steam.latest_game_version = lambda node_dir, cfg: asked.append(cfg.get("steam_refresh_token")) or shipped["v"]
            t = 1_000_000.0
            auth._version_checked_at = 0.0
            check("newer on Steam -> adopted", auth.refresh_app_version(s, now=t) == "1.35.2")
            check("...written to auth_config (read by every login), the old one kept for undo",
                  auth.load_auth_config(s)["app_ver"] == "1.35.2"
                  and auth.load_auth_config(s)["app_ver_prev"] == "1.35.1")
            check("...with the real (de-obfuscated) refresh token", asked == ["tok"], str(asked))
            check("...and a Discord line", len(posts) == 1 and "1.35.1 → 1.35.2" in posts[0]["content"],
                  str(posts))
            shipped["v"] = "1.36.0"
            check("asked again within 30 min -> not even checked",
                  auth.refresh_app_version(s, now=t + 60) == "" and len(asked) == 1)
            check("after 30 min -> checked again, newer adopted",
                  auth.refresh_app_version(s, now=t + auth.VERSION_CHECK_EVERY) == "1.36.0")
            shipped["v"] = "1.36.0"
            check("same version -> nothing changes",
                  auth.refresh_app_version(s, now=t + 2 * auth.VERSION_CHECK_EVERY) == ""
                  and auth.load_auth_config(s)["app_ver"] == "1.36.0")
            shipped["v"] = "1.35.9"
            check("an OLDER version is never adopted",
                  auth.refresh_app_version(s, now=t + 3 * auth.VERSION_CHECK_EVERY) == ""
                  and auth.load_auth_config(s)["app_ver"] == "1.36.0")
            shipped["v"] = None
            check("unreadable -> nothing changes",
                  auth.refresh_app_version(s, now=t + 4 * auth.VERSION_CHECK_EVERY) == "")
            check("only the two real switches were announced", len(posts) == 2, str(posts))
            shipped["v"] = "ratelimit"
            n = len(asked)
            check("Steam rate-limits the check -> nothing changes, the mint cooldown starts",
                  auth.refresh_app_version(s, now=t + 5 * auth.VERSION_CHECK_EVERY) == ""
                  and auth._mint_block_until > 0)
            shipped["v"] = "1.37.0"
            check("...and no extra Steam login while that cooldown lasts",
                  auth.refresh_app_version(s, now=t + 6 * auth.VERSION_CHECK_EVERY) == ""
                  and len(asked) == n + 1)
            auth._mint_block_until = 0.0
            real_save = auth.save_auth_config
            auth.save_auth_config = lambda *a, **k: (_ for _ in ()).throw(OSError(28, "No space left"))
            try:
                check("a failed save (disk full) returns nothing - never raises",
                      auth.refresh_app_version(s, now=t + 7 * auth.VERSION_CHECK_EVERY) == "")
            finally:
                auth.save_auth_config = real_save
    finally:
        steam.latest_game_version = real
        notify.post = real_post
        auth._version_checked_at = 0.0


def test_run_recovers() -> None:
    print("rotate.run: a 204 lockout ends as soon as the new version is adopted")
    state = {"locked": True, "checks": 0, "switched_at": None}

    class Locked(cc.Harness):
        def _visit(self, settings, spec, target, **kw):
            if target.label == "D":              # broken for another reason entirely
                self.order.append((self.clock.now, "D FAIL"))
                raise RuntimeError("tool/signup was refused (API error 102)")
            if state["locked"]:
                self.order.append((self.clock.now, target.label + " FAIL"))
                raise RuntimeError("the transfer did not return an auth_key. Last error: "
                                   "API error 204 on account/chain_by_transition_code")
            return super()._visit(settings, spec, target, **kw)

    def fake_refresh(settings, now=None):
        state["checks"] += 1
        if state["checks"] < 3:          # Steam does not have the update yet
            return ""
        state["locked"] = False
        state["switched_at"] = h.clock.now
        return "1.35.3"

    real = auth.refresh_app_version
    auth.refresh_app_version = fake_refresh
    try:
        with tempfile.TemporaryDirectory() as tmp:
            h = Locked(tmp, {1: 3000.0, 2: 3000.0, 3: 3000.0, 4: 3000.0},
                       [(1, "A"), (2, "B"), (3, "C"), (4, "D")])
            h.run(cycles=4)
    finally:
        auth.refresh_app_version = real
    after = [(t, name) for t, name in h.order if state["switched_at"] is not None and t > state["switched_at"]]
    check("a version check ran on each lockout failure", state["checks"] >= 3, str(state))
    check("after the switch, every locked-out account is visited and none fails",
          {n for _, n in after[:3]} == {"A", "B", "C"}, str(after[:4]))
    check("...promptly (paced one MIN_INTERVAL apart, not their up-to-1h backoff)",
          after and after[2][0] - state["switched_at"] <= 3 * rotate.MIN_INTERVAL_SECONDS + 1,
          f"{[(round(t - state['switched_at']), n) for t, n in after[:3]]}")
    check("an account failing for ANOTHER reason keeps its backoff",
          "D FAIL" not in [n for _, n in after[:3]], str(after[:4]))
    check("the lockout detector matches 204 exactly",
          rotate._version_lockout(RuntimeError("x API error 204 on y"))
          and rotate._version_lockout(RuntimeError("(API error 204)"))
          and not rotate._version_lockout(RuntimeError("API error 205 on z"))
          and not rotate._version_lockout(RuntimeError("API error 2040 on z"))
          and not rotate._version_lockout(RuntimeError("API error 20401")))


def test_first_login_204_fails_the_visit() -> None:
    print("_ensure_bound: a 204 on the visit's first login fails it - no account switch")
    calls = []
    saved = (rotate.flow.connect, rotate.acct)
    rotate.flow.connect = lambda settings: (_ for _ in ()).throw(
        RuntimeError("login failed: API error 204 on tool/start_session"))
    rotate.acct = types.SimpleNamespace(login_with_link=lambda *a: calls.append("link"),
                                        find_account=lambda *a: True)
    try:
        try:
            rotate._ensure_bound(None, rotate.RosterAccount(1, "pw", "A"), rotate.log)
            check("the 204 is raised", False)
        except RuntimeError as exc:
            check("the 204 is raised, so the version check sees it", "204" in str(exc))
        check("...and no signup / transfer is attempted", calls == [], str(calls))
    finally:
        rotate.flow.connect, rotate.acct = saved


def main() -> int:
    for fn in (test_parse, test_adopt, test_run_recovers, test_first_login_204_fails_the_visit):
        fn()
        print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
        return 1
    print("all version-update checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
