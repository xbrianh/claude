#!/usr/bin/env bash
# Thin shim — see gremlins/cli.py. The launcher (skills/_bg/launch.sh)
# dispatches bossgremlin to `python3 -m gremlins.cli boss` directly; this
# file exists for direct invocations that bypass the launcher.
PYTHONPATH="$HOME/.claude${PYTHONPATH:+:$PYTHONPATH}" exec python3 -m gremlins.cli boss "$@"
