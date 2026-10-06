"""AutoIndependentCareer - a headless CLI driver for Umamusume Independent Careers.

The wire protocol (AES/msgpack framing, the rolling sid, Steam ticket minting)
is NOT reimplemented here; it is imported from the data-root checkout so
protocol fixes there land here for free. See ``aic.bootstrap``.
"""

__version__ = "0.1.0"
