#!/usr/bin/env bash
# Thin shim — see pipeline/cli.py.
PYTHONPATH="$HOME/.claude${PYTHONPATH:+:$PYTHONPATH}" exec python3 -m pipeline.cli address "$@"
