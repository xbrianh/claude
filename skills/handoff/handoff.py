#!/usr/bin/env bash
# Thin shim — see gremlins/cli.py. The handoff agent lives in
# gremlins/handoff.py; this entrypoint stays at its original path so existing
# /handoff skill invocations keep working.
PYTHONPATH="$HOME/.claude${PYTHONPATH:+:$PYTHONPATH}" exec python3 -m gremlins.cli handoff "$@"
