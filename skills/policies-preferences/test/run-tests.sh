#!/usr/bin/env bash
# Unit tests for pp. Needs PyYAML + jsonschema (install.sh, or set PP_PYTHON).
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="${XDG_DATA_HOME:-$HOME/.local/share}/policies-preferences/venv"
PY="${PP_PYTHON:-}"
[[ -z "$PY" && -x "$VENV/bin/python" ]] && PY="$VENV/bin/python"
PY="${PY:-python3}"
cd "$HERE"
exec "$PY" -m unittest -v test_pp "$@"
