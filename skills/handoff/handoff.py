#!/usr/bin/env bash
# Thin shim — see pipeline/cli.py. The handoff agent lives in
# pipeline/handoff.py; this entrypoint stays at its original path so existing
# /handoff skill invocations keep working.
PYTHONPATH="$HOME/.claude${PYTHONPATH:+:$PYTHONPATH}" exec python3 -m pipeline.cli handoff "$@"
