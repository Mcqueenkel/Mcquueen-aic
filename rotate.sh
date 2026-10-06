#!/usr/bin/env bash
# Linux launcher (counterpart of rotate.bat). See ./aic.sh for details.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="${AIC_PY:-$HOME/.venvs/aic/bin/python}"
if [ ! -x "$PY" ]; then
    echo "[!] venv python not found at $PY; falling back to python3" >&2
    PY=python3
fi
export PATH="$HOME/.local/bin:$PATH"
exec "$PY" "$HERE/rotate_cli.py" "$@"
