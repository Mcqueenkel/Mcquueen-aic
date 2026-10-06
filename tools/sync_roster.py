"""Rebuild reroll_accounts.json from accounts.txt.

accounts.txt is the hand-maintained source: ``viewer_id link_password trainee``
per line. Existing names are kept so log history stays readable; new accounts
get ``<Trainee>_<last 4 of id>``.

That name is only a FALLBACK label. Once an account has been taken over once,
the log and the Discord report call it by its in-game trainer name, recorded in
state/accounts.json (see ``aic.rotate.apply_ingame_names``) - so what you write
here is what a brand-new roster entry shows until its first visit, and little
after that.
"""
import json
import pathlib
import sys

TXT = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/aic/accounts.txt")
OUT = pathlib.Path("/opt/aic/reroll_accounts.json")

rows = []
for n, line in enumerate(TXT.read_text(encoding="utf-8-sig").splitlines(), 1):
    parts = line.strip().split()
    if not parts:
        continue
    if len(parts) < 3:
        sys.exit(f"{TXT}:{n}: need 'viewer_id password trainee', got {line!r}")
    rows.append((parts[0], parts[1], parts[2]))

old = {}
if OUT.is_file():
    raw = json.loads(OUT.read_text(encoding="utf-8-sig"))
    for r in (raw.get("accounts") if isinstance(raw, dict) else raw) or []:
        old[str(r.get("trainer_id") or r.get("viewer_id"))] = r

accounts = []
for vid, password, trainee in rows:
    prev = old.get(vid, {})
    accounts.append({
        **prev,                      # keeps per-account settings (ace_once, club)
        "trainer_id": vid,
        "password": password,
        # A name already in use stays put; the log and the careerclock store
        # are easier to read across a roster change if labels do not churn.
        "name": prev.get("name") or f"{trainee}_{vid[-4:]}",
        "trainee": trainee,
    })

gone = sorted(set(old) - {v for v, _, _ in rows})
new = sorted({v for v, _, _ in rows} - set(old))

OUT.write_text(json.dumps(accounts, indent=2) + "\n", encoding="utf-8")
print(f"  wrote {len(accounts)} accounts to {OUT}")
print(f"  added   : {', '.join(new) or 'none'}")
print(f"  removed : {', '.join(gone) or 'none'}")
for a in accounts:
    print(f"    {a['trainer_id']}  {a['name']:<18} trainee={a['trainee']}")
