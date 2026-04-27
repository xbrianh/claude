#!/usr/bin/env bash
# Thin shim: delegates to pipeline.orchestrators.gh via pipeline.cli.
# All logic lives in pipeline/orchestrators/gh.py; this file exists only so
# launch.sh can still resolve skills/ghgremlin/ghgremlin.sh as a fallback
# while the USE_PIPELINE=1 fast-path is active.
set -euo pipefail
PYTHONPATH="$HOME/.claude${PYTHONPATH:+:$PYTHONPATH}" \
exec python3 -m pipeline.cli gh "$@"
