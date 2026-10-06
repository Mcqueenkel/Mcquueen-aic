#!/usr/bin/env python
"""Entry point for account switching. See account.bat."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from aic.account_cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
