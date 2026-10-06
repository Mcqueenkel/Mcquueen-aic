#!/usr/bin/env python
"""Entry point. Run with the Icarus virtualenv interpreter:

    ..\\Icarus-Dev-Build-Private-main\\.venv\\Scripts\\python.exe aic_cli.py run
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from aic.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
