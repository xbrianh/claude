#!/usr/bin/env bash
# Thin shim — see gremlins/cli.py.
. "$(dirname "$0")/../_lib/python.sh"
PYTHONPATH="$HOME/.claude${PYTHONPATH:+:$PYTHONPATH}" exec "$CLAUDE_PY" -m gremlins.cli address "$@"
