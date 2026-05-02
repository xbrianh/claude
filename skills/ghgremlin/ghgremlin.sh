#!/usr/bin/env bash
# Thin shim: delegates to gremlins.orchestrators.gh via gremlins.cli.
# All logic lives in gremlins/orchestrators/gh.py.
set -euo pipefail
. "$(dirname "$0")/../_lib/python.sh"
PYTHONPATH="$HOME/.claude${PYTHONPATH:+:$PYTHONPATH}" exec "$CLAUDE_PY" -m gremlins.cli gh "$@"
