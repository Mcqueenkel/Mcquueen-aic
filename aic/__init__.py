"""AutoIndependentCareer - a headless CLI driver for Umamusume Independent Careers.

The wire protocol (AES/msgpack framing, the rolling sid, Steam ticket minting)
is NOT reimplemented here; it comes from the vendored ``uma_api`` client in
``vendor/``. See ``aic.bootstrap``.
"""

__version__ = "0.1.0"
