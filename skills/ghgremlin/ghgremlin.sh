#!/usr/bin/env bash
# Thin shim: delegates to gremlins.orchestrators.gh via gremlins.cli.
# All logic lives in gremlins/orchestrators/gh.py.
set -euo pipefail
PYTHONPATH="$HOME/.claude${PYTHONPATH:+:$PYTHONPATH}" \
exec python -m gremlins.cli gh "$@"
