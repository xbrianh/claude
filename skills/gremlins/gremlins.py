#!/usr/bin/env bash
# Thin shim — see gremlins/cli.py. The fleet-manager logic lives in
# gremlins/fleet.py; this entrypoint stays at its original path so existing
# /gremlins skill invocations and operator muscle memory keep working.
PYTHONPATH="$HOME/.claude${PYTHONPATH:+:$PYTHONPATH}" exec python3 -m gremlins.cli fleet "$@"
