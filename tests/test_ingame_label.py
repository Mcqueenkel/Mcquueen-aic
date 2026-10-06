"""Roster entries are labelled with their IN-GAME trainer name. No network.

    python tests/test_ingame_label.py

The log is how this farm is read, so a line should name something you can find
in the client ("McSpade"), not a roster bookkeeping nickname ("Oguri_5943").
The rules that matter:

  * the in-game name wins, but only when there IS one - an account never
    chained on this profile (a fresh roster addition) must keep its nickname
    rather than fall back to a bare trainer id;
  * two accounts sharing an in-game name must stay distinguishable;
  * ``--only`` keeps accepting whatever the operator has been typing.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aic.rotate import (  # noqa: E402
    RosterAccount, RotationError, apply_ingame_names, pick_account,
)

FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label} {detail}")
        FAILURES.append(label)


def labelled(roster: list[tuple[int, str]], recorded: list[dict]) -> list[RosterAccount]:
    """Run the real accounts.json load path over a temp state dir."""
    accounts = [RosterAccount(tid, "pw", name) for tid, name in roster]
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "accounts.json").write_text(json.dumps(recorded), encoding="utf-8")
        apply_ingame_names(accounts, SimpleNamespace(state_dir=root / "state"))
    return accounts


# The live roster as of 2026-09-03, trimmed: nine accounts whose in-game names
# were recorded on chain, plus 793593252166 which has never been chained here.
LIVE = [(996150905943, "Oguri_5943"), (364872132559, "Oguri_2559"),
        (975393504390, "Oguri_4390"), (793593252166, "Oguri_2166")]
RECORDED = [{"viewer_id": 996150905943, "name": "McSpade"},
            {"viewer_id": 364872132559, "name": "HiImAstonMartin"},
            {"viewer_id": 975393504390, "name": "AAAAAAAAAAAAAAA"}]


def test_ingame_name_wins() -> None:
    print("the in-game name replaces the nickname")
    a = labelled(LIVE, RECORDED)
    check("Oguri_5943 -> McSpade", a[0].label == "McSpade", a[0].label)
    check("Oguri_2559 -> HiImAstonMartin",
          a[1].label == "HiImAstonMartin", a[1].label)
    check("odd names pass through unchanged",
          a[2].label == "AAAAAAAAAAAAAAA", a[2].label)
    check("nickname is still on record", a[0].name == "Oguri_5943", a[0].name)


def test_unchained_account_keeps_its_nickname() -> None:
    print("an account with no recorded name")
    a = labelled(LIVE, RECORDED)
    # THE REGRESSION TO FEAR: labelling a brand-new roster entry "793 593 252 166"
    # because accounts.json has not heard of it yet.
    check("793593252166 -> Oguri_2166", a[3].label == "Oguri_2166", a[3].label)
    check("no in-game name claimed", a[3].ingame_name == "", a[3].ingame_name)


def test_no_name_at_all_falls_back_to_the_trainer_id() -> None:
    print("neither name")
    a = labelled([(996150905943, "")], [])
    check("bare id label", a[0].label == "996 150 905 943", a[0].label)


def test_clashing_names_stay_distinguishable() -> None:
    print("two accounts, one in-game name")
    a = labelled([(996150905943, "Oguri_5943"), (364872132559, "Oguri_2559")],
                 [{"viewer_id": 996150905943, "name": "McAce"},
                  {"viewer_id": 364872132559, "name": "McAce"}])
    check("first keeps an id tail", a[0].label == "McAce 5943", a[0].label)
    check("second keeps an id tail", a[1].label == "McAce 2559", a[1].label)
    check("labels differ", a[0].label != a[1].label)


def test_blank_and_broken_records_are_ignored() -> None:
    print("junk in accounts.json")
    a = labelled([(996150905943, "Oguri_5943"), (364872132559, "Oguri_2559")],
                 [{"viewer_id": 996150905943, "name": "   "},
                  {"viewer_id": None, "name": "Nobody"},
                  {"name": "McAce"}])
    check("whitespace name ignored", a[0].label == "Oguri_5943", a[0].label)
    check("id-less row ignored", a[1].label == "Oguri_2559", a[1].label)


def test_only_still_matches_what_the_operator_types() -> None:
    print("pick_account after the relabel")
    a = labelled(LIVE, RECORDED)
    check("by in-game name", pick_account(a, "McSpade") is a[0])
    check("by in-game name, any case", pick_account(a, "mcspade") is a[0])
    check("by the old nickname", pick_account(a, "Oguri_5943") is a[0])
    check("by trainer id", pick_account(a, "996 150 905 943") is a[0])
    check("unchained account by nickname", pick_account(a, "Oguri_2166") is a[3])

    clashed = labelled([(996150905943, "Oguri_5943"), (364872132559, "Oguri_2559")],
                       [{"viewer_id": 996150905943, "name": "McAce"},
                        {"viewer_id": 364872132559, "name": "McAce"}])
    check("a clashed label is a usable --only",
          pick_account(clashed, "McAce 5943") is clashed[0])
    try:
        pick_account(clashed, "McAce")
    except RotationError:
        check("the ambiguous half is refused, not guessed", True)
    else:
        check("the ambiguous half is refused, not guessed", False, "matched one")


def test_case_variant_names_still_clash() -> None:
    print("the same name in different case")
    # pick_account matches case-insensitively, so if these two were treated as
    # unique here they would get no id tail and BOTH become unselectable.
    a = labelled([(996150905943, "Oguri_5943"), (364872132559, "Oguri_2559")],
                 [{"viewer_id": 996150905943, "name": "McAce"},
                  {"viewer_id": 364872132559, "name": "mcace"}])
    check("both get an id tail",
          a[0].label == "McAce 5943" and a[1].label == "mcace 2559",
          f"{a[0].label} / {a[1].label}")
    check("each is selectable", pick_account(a, "McAce 5943") is a[0]
          and pick_account(a, "mcace 2559") is a[1])


def test_a_relabel_mid_run_still_disambiguates() -> None:
    print("a new account turns up wearing a name already in use")
    # THE REAL CASE, 2026-09-03: 793593252166 was added to the roster, chained on
    # its first visit, and came back called "McDonald" - which 768270152387 was
    # already called. Re-applying over the whole roster is what gives them tails;
    # relabelling only the account just visited would leave both plain "McDonald".
    accounts = [RosterAccount(768270152387, "pw", "Oguri_2387"),
                RosterAccount(793593252166, "pw", "Oguri_2166")]
    recorded = [{"viewer_id": 768270152387, "name": "McDonald"}]

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        settings = SimpleNamespace(state_dir=root / "state")
        store = root / "accounts.json"

        store.write_text(json.dumps(recorded), encoding="utf-8")
        apply_ingame_names(accounts, settings)
        check("before the new account is chained", accounts[0].label == "McDonald"
              and accounts[1].label == "Oguri_2166",
              f"{accounts[0].label} / {accounts[1].label}")

        # Its first visit chains it; remember_account records the clashing name.
        recorded.append({"viewer_id": 793593252166, "name": "McDonald"})
        store.write_text(json.dumps(recorded), encoding="utf-8")
        apply_ingame_names(accounts, settings)
        check("the incumbent gains a tail too",
              accounts[0].label == "McDonald 2387", accounts[0].label)
        check("the newcomer is distinguishable",
              accounts[1].label == "McDonald 2166", accounts[1].label)
        check("and both are selectable",
              pick_account(accounts, "McDonald 2387") is accounts[0]
              and pick_account(accounts, "McDonald 2166") is accounts[1])

        # Re-applying with nothing changed must be a no-op, not a growing tail.
        apply_ingame_names(accounts, settings)
        check("re-applying is idempotent", accounts[0].label == "McDonald 2387",
              accounts[0].label)


def test_the_log_and_the_webhook_agree_on_every_name() -> None:
    print("one name per account, everywhere")
    # The log labels from the roster; the Discord report labels straight from
    # names_by_viewer. If disambiguation lived in only one of them, a clashed
    # account would be "McDonald 2166" in the log and plain "McDonald" in the
    # report - and the report would show two identical rows. MEASURED 2026-09-03.
    from aic import account as acct

    roster = [(768270152387, "Oguri_2387"), (793593252166, "Oguri_2166"),
              (996150905943, "Oguri_5943")]
    recorded = [{"viewer_id": 768270152387, "name": "McDonald"},
                {"viewer_id": 793593252166, "name": "McDonald"},
                {"viewer_id": 996150905943, "name": "McSpade"}]
    accounts = [RosterAccount(t, "pw", n) for t, n in roster]
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        settings = SimpleNamespace(state_dir=root / "state")
        (root / "accounts.json").write_text(json.dumps(recorded), encoding="utf-8")
        apply_ingame_names(accounts, settings)
        webhook = acct.names_by_viewer(settings)

    for account in accounts:
        check(f"{account.name}: log == webhook",
              account.label == webhook[account.trainer_id],
              f"log={account.label!r} webhook={webhook[account.trainer_id]!r}")
    check("the report has no duplicate rows",
          len(set(webhook.values())) == len(webhook), str(webhook))
    check("the unclashed one is left alone",
          webhook[996150905943] == "McSpade", webhook[996150905943])


def test_a_nickname_is_never_recorded_as_an_ingame_name() -> None:
    print("accounts.json must not be poisoned with roster nicknames")
    # rotate.run() pre-registers every roster password. It must NOT pass the
    # roster nickname as the in-game name: a nickname in that field is
    # indistinguishable from a real one, so the "never chained -> keep the
    # nickname" fallback would be silently dead for that account.
    import inspect

    from aic import farm, rotate
    for mod in (rotate, farm):
        src = inspect.getsource(mod)
        calls = [ln.strip() for ln in src.splitlines()
                 if "remember_account(" in ln and "def " not in ln
                 and not ln.strip().startswith("#")]
        for call in calls:
            check(f"{mod.__name__}: no name argument -> {call[:56]}",
                  call.rstrip().endswith("True)"), call)


def test_recording_a_password_never_wipes_a_known_name() -> None:
    print("remember_account preserves the in-game name")
    from aic import account as acct
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        settings = SimpleNamespace(state_dir=root / "state")
        (root / "accounts.json").write_text(json.dumps(
            [{"viewer_id": 996150905943, "name": "McSpade",
              "password": "old", "link_set": True}]), encoding="utf-8")

        # save-link / logout re-record the password with no name to offer.
        acct.remember_account(settings, 996150905943, "new", True)
        check("the name survived",
              acct.names_by_viewer(settings).get(996150905943) == "McSpade",
              str(acct.names_by_viewer(settings)))
        check("the new password was written",
              any(r["password"] == "new" for r in acct.load_accounts(settings)))

        # A real chain reports the name, and that DOES replace it.
        acct.remember_account(settings, 996150905943, "new", True, "McRenamed")
        check("a server-supplied name replaces it",
              acct.names_by_viewer(settings).get(996150905943) == "McRenamed",
              str(acct.names_by_viewer(settings)))
        check("one row per account still",
              len(acct.load_accounts(settings)) == 1,
              str(len(acct.load_accounts(settings))))


def main() -> int:
    for fn in (test_ingame_name_wins,
               test_unchained_account_keeps_its_nickname,
               test_no_name_at_all_falls_back_to_the_trainer_id,
               test_clashing_names_stay_distinguishable,
               test_blank_and_broken_records_are_ignored,
               test_only_still_matches_what_the_operator_types,
               test_case_variant_names_still_clash,
               test_a_relabel_mid_run_still_disambiguates,
               test_the_log_and_the_webhook_agree_on_every_name,
               test_a_nickname_is_never_recorded_as_an_ingame_name,
               test_recording_a_password_never_wipes_a_known_name):
        fn()
        print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
        return 1
    print("all in-game label checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
