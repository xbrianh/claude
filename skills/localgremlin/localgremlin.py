#!/usr/bin/env bash
# Thin shim — see pipeline/cli.py. The launcher (skills/_bg/launch.sh)
# dispatches localgremlin to `python3 -m pipeline.cli local` directly; this
# file exists for direct invocations that bypass the launcher.
PYTHONPATH="$HOME/.claude${PYTHONPATH:+:$PYTHONPATH}" exec python3 -m pipeline.cli local "$@"
