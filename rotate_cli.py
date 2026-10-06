#!/usr/bin/env python
"""Entry point for the account rotation. See rotate.bat."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from aic.rotate_cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
