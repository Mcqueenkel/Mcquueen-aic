"""A roster trainee name must resolve to a card THIS account owns. No network.

    python tests/test_trainee_label.py

accounts.txt carries a written trainee per account ("Oguri_alt"), not a card id -
a card id is per-account and unmemorable. The rule that matters: when the label
cannot be resolved unambiguously, resolve to 0 and let the caller fall back. A
wrong trainee silently spends a whole 50-minute career on the wrong build.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aic.selection import resolve_trainee_label  # noqa: E402

FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label} {detail}")
        FAILURES.append(label)


class FakeGameData:
    def __init__(self, names):
        self.names = {int(k): v for k, v in names.items()}

    def chara_name(self, card_id):
        return self.names.get(int(card_id), f"Chara {card_id}")


REAL = {                       # the real owned list off account 319865323652
    100602: "Oguri Cap (Alt)", 100701: "Gold Ship", 100702: "Gold Ship (Alt)",
    100801: "Vodka", 101401: "El Condor Pasa", 103201: "Agnes Tachyon",
    105601: "Matikanefukukitaru", 106201: "Matikanetannhauser",
}


def test_the_labels_actually_in_accounts_txt() -> None:
    print("the labels from accounts.txt")
    g = FakeGameData(REAL)
    owned = list(REAL)
    cid, _ = resolve_trainee_label("Oguri_alt", owned, g)
    check("Oguri_alt -> Oguri Cap (Alt)", cid == 100602, str(cid))
    cid, _ = resolve_trainee_label("El_condor_pasa", owned, g)
    check("El_condor_pasa -> El Condor Pasa", cid == 101401, str(cid))


def test_alt_disambiguation() -> None:
    print("Oguri vs Oguri (Alt)")
    g = FakeGameData({**REAL, 100301: "Oguri Cap"})
    owned = list(g.names)
    cid, note = resolve_trainee_label("Oguri", owned, g)
    check("a bare name prefers the NON-alt card", cid == 100301, f"{cid} {note}")
    cid, _ = resolve_trainee_label("Oguri_alt", owned, g)
    check("and the alt label still takes the alt", cid == 100602, str(cid))
    cid, _ = resolve_trainee_label("Gold Ship", owned, g)
    check("same rule for another pair", cid == 100701, str(cid))


def test_unresolvable_returns_zero() -> None:
    print("never guess")
    g = FakeGameData(REAL)
    owned = list(REAL)
    cid, note = resolve_trainee_label("Symboli Rudolf", owned, g)
    check("a trainee the account does NOT own resolves to 0", cid == 0, str(cid))
    check("and says why", "no owned trainee matches" in note, note)

    # Two cards, neither distinguished by an 'alt' - genuinely ambiguous.
    g2 = FakeGameData({1: "Mejiro Ryan (Summer)", 2: "Mejiro Ryan (Wedding)"})
    cid, note = resolve_trainee_label("Mejiro Ryan", [1, 2], g2)
    check("a genuinely ambiguous label resolves to 0", cid == 0, str(cid))
    check("and lists the candidates", "ambiguous" in note, note)

    check("an empty label is a no-op", resolve_trainee_label("", owned, g)[0] == 0)
    check("so is None", resolve_trainee_label(None, owned, g)[0] == 0)


def test_separators_and_case() -> None:
    print("however it is written")
    g = FakeGameData(REAL)
    owned = list(REAL)
    for written in ("el_condor_pasa", "El Condor Pasa", "EL-CONDOR-PASA",
                    "el.condor.pasa"):
        cid, _ = resolve_trainee_label(written, owned, g)
        check(f"{written!r} resolves", cid == 101401, str(cid))


def test_empty_account() -> None:
    print("an account owning nothing")
    check("resolves to 0 rather than raising",
          resolve_trainee_label("Oguri_alt", [], FakeGameData({}))[0] == 0)


def main() -> int:
    for fn in (test_the_labels_actually_in_accounts_txt, test_alt_disambiguation,
               test_unresolvable_returns_zero, test_separators_and_case,
               test_empty_account):
        fn()
        print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
        return 1
    print("all trainee-label checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
