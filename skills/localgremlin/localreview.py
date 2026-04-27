#!/usr/bin/env bash
# Thin shim — see gremlins/cli.py.
PYTHONPATH="$HOME/.claude${PYTHONPATH:+:$PYTHONPATH}" exec python3 -m gremlins.cli review "$@"
