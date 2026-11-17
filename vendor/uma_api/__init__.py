"""Vendored copy of Icarus' ``uma_api`` wire client.

Kept here so ``aic`` is self-contained: no separate Icarus checkout, no
``AIC_ICARUS_ROOT``, and no import-ordering rules to get wrong. ``client.py`` is
byte-for-byte Icarus' file - it was already written to import standalone (its
``career_bot`` imports are guarded with fallbacks), so it needed no edits.

Update it by re-copying that one file. ``aic.bootstrap`` prefers a real Icarus
checkout when one is configured, so a newer client can still be used without
touching this copy.
"""
