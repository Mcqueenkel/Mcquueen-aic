#!/usr/bin/env bash
# Linux launcher (counterpart of aic.bat). The Windows Icarus .venv cannot be
# used here (and the exFAT drive cannot hold a venv), so this runs the
# ~/.venvs/aic interpreter, which has curl_cffi / msgpack / pycryptodome
# installed. Override with AIC_PY=/path/to/python.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="${AIC_PY:-$HOME/.venvs/aic/bin/python}"
if [ ! -x "$PY" ]; then
    echo "[!] venv python not found at $PY; falling back to python3" >&2
    PY=python3
fi
export PATH="$HOME/.local/bin:$PATH"
exec "$PY" "$HERE/aic_cli.py" "$@"
