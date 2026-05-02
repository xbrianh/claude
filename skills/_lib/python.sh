#!/usr/bin/env bash
# Source this to set CLAUDE_PY to a python interpreter suitable for the
# gremlins package. The shims must avoid the active project venv: another
# repo's venv may be on a Python version older than gremlins requires
# (>=3.11) or have site-packages that shadow the gremlins import.
#
# Usage in a shim:
#   . "$(dirname "$0")/../_lib/python.sh"
#   PYTHONPATH="$HOME/.claude${PYTHONPATH:+:$PYTHONPATH}" exec "$CLAUDE_PY" -m gremlins.cli ...

__claude_find_python() {
    local candidates=(
        "$HOME/.claude/.venv/bin/python"
        /opt/homebrew/bin/python3.14
        /opt/homebrew/bin/python3.13
        /opt/homebrew/bin/python3.12
        /opt/homebrew/bin/python3.11
        /opt/homebrew/bin/python3
        /usr/local/bin/python3
        /usr/bin/python3
    )
    local p
    for p in "${candidates[@]}"; do
        if [[ -x "$p" ]] && "$p" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
            echo "$p"
            return 0
        fi
    done
    return 1
}

CLAUDE_PY="$(__claude_find_python)" || {
    echo "skills: no python interpreter (>=3.11) found in known locations" >&2
    exit 127
}
