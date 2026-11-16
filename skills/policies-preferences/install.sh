#!/usr/bin/env bash
# Idempotent: create a private venv with PyYAML + jsonschema for `pp`. --uninstall removes it.
set -euo pipefail
VENV="${XDG_DATA_HOME:-$HOME/.local/share}/policies-preferences/venv"
if [[ "${1:-}" == "--uninstall" ]]; then
  rm -rf "$(dirname "$VENV")"; echo "removed $VENV"; exit 0
fi
if [[ -x "$VENV/bin/python" ]] && "$VENV/bin/python" -c 'import yaml, jsonschema' 2>/dev/null; then
  echo "ok: $VENV"; exit 0
fi
python3 -m venv "$VENV"
"$VENV/bin/pip" install -q --upgrade pip >/dev/null
"$VENV/bin/pip" install -q 'pyyaml>=6' 'jsonschema>=4.18'
echo "installed: $VENV"
