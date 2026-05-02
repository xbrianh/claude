#!/usr/bin/env bash
# Thin shim — see gremlins/cli.py. The launcher dispatches bossgremlin to
# `gremlins.cli boss` directly; this file exists for direct invocations
# that bypass the launcher.
. "$(dirname "$0")/../_lib/python.sh"
PYTHONPATH="$HOME/.claude${PYTHONPATH:+:$PYTHONPATH}" exec "$CLAUDE_PY" -m gremlins.cli boss "$@"
