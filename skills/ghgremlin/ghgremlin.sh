#!/usr/bin/env bash
# Thin shim: delegates to gremlins.orchestrators.gh via gremlins.cli.
# All logic lives in gremlins/orchestrators/gh.py; this file exists only so
# launch.sh can still resolve skills/ghgremlin/ghgremlin.sh as a fallback
# while the USE_PIPELINE=1 fast-path is active.
set -euo pipefail
PYTHONPATH="$HOME/.claude${PYTHONPATH:+:$PYTHONPATH}" \
exec python3 -m gremlins.cli gh "$@"
