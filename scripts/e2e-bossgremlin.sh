#!/usr/bin/env bash
# E2E test for /bossgremlin --chain-kind local.
# Creates a sandbox branch, launches the boss with a real spec, polls until
# done or stalled, asserts structural pass conditions, and emits a JSON summary
# as the final stdout line.
set -euo pipefail

# ── Setup ──────────────────────────────────────────────────────────────────────

REPO_ROOT=$(git rev-parse --show-toplevel 2>/dev/null) || {
    echo "[e2e] FAIL: not in a git repo" >&2; exit 1
}

SPEC="$REPO_ROOT/scripts/e2e/bossgremlin-spec.md"
LAUNCH_SH="$HOME/.claude/skills/_bg/launch.sh"

[[ -f "$SPEC" && -s "$SPEC" ]] || {
    echo "[e2e] FAIL: spec not found or empty: $SPEC" >&2; exit 1
}
[[ -x "$LAUNCH_SH" ]] || {
    echo "[e2e] FAIL: launch.sh not executable: $LAUNCH_SH" >&2; exit 1
}

ORIG_BRANCH=$(git -C "$REPO_ROOT" symbolic-ref --short HEAD 2>/dev/null || echo "")
MAX_WAIT=${E2E_TIMEOUT:-7200}

[[ -z "$(git -C "$REPO_ROOT" status --porcelain)" ]] || {
    echo "[e2e] FAIL: working tree is not clean; commit, stash, or discard changes before running" >&2; exit 1
}

git -C "$REPO_ROOT" fetch --quiet origin || {
    echo "[e2e] FAIL: git fetch origin failed" >&2; exit 1
}

# Refresh origin/HEAD: `git fetch` does not update it, so a renamed upstream
# default branch would otherwise leave us branching off a stale local ref.
git -C "$REPO_ROOT" remote set-head origin --auto >/dev/null || {
    echo "[e2e] FAIL: git remote set-head origin --auto failed" >&2; exit 1
}

DEFAULT_BRANCH=$(git -C "$REPO_ROOT" rev-parse --abbrev-ref origin/HEAD 2>/dev/null || echo "")
DEFAULT_BRANCH=${DEFAULT_BRANCH#origin/}
[[ -n "$DEFAULT_BRANCH" && "$DEFAULT_BRANCH" != "HEAD" ]] || {
    echo "[e2e] FAIL: could not resolve default branch from origin/HEAD" >&2; exit 1
}

SANDBOX_BRANCH="bossgremlin-e2e/$(date +%Y%m%d-%H%M%S)"
git -C "$REPO_ROOT" checkout -b "$SANDBOX_BRANCH" "origin/$DEFAULT_BRANCH"
echo "[e2e] sandbox branch: $SANDBOX_BRANCH (from origin/$DEFAULT_BRANCH)"

cleanup() {
    [[ -n "${BOSS_ID:-}" && -n "${STATE_DIR:-}" ]] && {
        local _pid=""
        _pid=$(python3 -c "
import json
try:
    print(json.load(open('${STATE_DIR}/state.json')).get('pid') or '')
except Exception:
    print('')
" 2>/dev/null) || true
        [[ -n "$_pid" ]] && kill "$_pid" 2>/dev/null || true
    }
    [[ -n "$ORIG_BRANCH" ]] && git -C "$REPO_ROOT" checkout "$ORIG_BRANCH" 2>/dev/null || true
    [[ "${PASS_FAIL:-fail}" != "pass" ]] && git -C "$REPO_ROOT" branch -D "$SANDBOX_BRANCH" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

START_TS=$(date +%s)

# ── Launch ──────────────────────────────────────────────────────────────────────

BOSS_ID=$(
    "$LAUNCH_SH" \
        --print-id \
        --description "e2e: stages docs across gremlin SKILL.md files" \
        bossgremlin \
        --chain-kind local \
        --plan "$SPEC"
)
[[ -n "$BOSS_ID" ]] || {
    echo "[e2e] FAIL: launcher printed no ID" >&2; exit 1
}

STATE_ROOT="${XDG_STATE_HOME:-$HOME/.local/state}"
STATE_DIR="$STATE_ROOT/claude-gremlins/$BOSS_ID"
echo "[e2e] boss launched: $BOSS_ID"

# ── Poll ────────────────────────────────────────────────────────────────────────

BOSS_EXIT_CODE=""
STALL_COUNT=0

while true; do
    sleep 30
    ELAPSED=$(( $(date +%s) - START_TS ))

    if [[ $ELAPSED -ge $MAX_WAIT ]]; then
        echo "[e2e] +${ELAPSED}s poll timeout after ${MAX_WAIT}s — boss still running"
        BOSS_EXIT_CODE="timeout"
        break
    fi

    # Read exit_code, status, pid, stage in a single call; 'error' on failure
    STATE_LINE=$(python3 -c "
import json
try:
    with open('$STATE_DIR/state.json') as f:
        s = json.load(f)
    ec = s.get('exit_code')
    print('null' if ec is None else str(ec),
          s.get('status', ''),
          str(s.get('pid') or ''),
          s.get('stage', ''), sep='\t')
except Exception:
    print('error', '', '', '', sep='\t')
" 2>/dev/null) || STATE_LINE="error"

    IFS=$'\t' read -r EC BSTATUS PID BSTAGE <<< "$STATE_LINE"

    if [[ "${EC:-error}" == "error" ]]; then
        echo "[e2e] +${ELAPSED}s state.json unreadable, retrying..."
        continue
    fi

    if [[ "$EC" != "null" ]]; then
        BOSS_EXIT_CODE="$EC"
        echo "[e2e] +${ELAPSED}s boss stage=${BSTAGE:-?} exit_code=$EC"
        break
    fi

    # Stall: process dead for two consecutive poll cycles
    if [[ "$BSTATUS" == "running" && -n "$PID" ]] && ! kill -0 "$PID" 2>/dev/null; then
        STALL_COUNT=$(( STALL_COUNT + 1 ))
        echo "[e2e] +${ELAPSED}s boss stage=${BSTAGE:-?} (stall detected, cycle $STALL_COUNT/2)"
        if [[ "$STALL_COUNT" -ge 2 ]]; then
            echo "[e2e] boss stalled — proceeding to assertions"
            break
        fi
    else
        STALL_COUNT=0
        echo "[e2e] +${ELAPSED}s boss stage=${BSTAGE:-?} (still running)"
    fi
done

# ── Assertions ──────────────────────────────────────────────────────────────────

FAILURES=()
CHAIN_EXIT_STATE=""
CHILD_COUNT=0
BAD_CHILDREN=""

# Parse boss_state.json for: last handoff exit_state, child count, bad outcomes
if [[ -f "$STATE_DIR/boss_state.json" ]]; then
    BS_LINE=$(python3 -c "
import json
try:
    with open('$STATE_DIR/boss_state.json') as f:
        bs = json.load(f)
    recs = bs.get('handoff_records', [])
    exit_state = recs[-1]['exit_state'] if recs else ''
    children = bs.get('children', [])
    bad = ','.join(
        c['id'] + ':' + c.get('outcome', '')
        for c in children
        if c.get('outcome') not in ('landed', 'rescued-then-landed')
    )
    print(exit_state, len(children), bad if bad else 'NONE')
except Exception as e:
    print('parse_error', 0, str(e).replace(' ', '_'))
" 2>/dev/null) || BS_LINE="parse_error 0 unknown"
    IFS=' ' read -r CHAIN_EXIT_STATE CHILD_COUNT BAD_CHILDREN_OR_NONE <<< "$BS_LINE"
    if [[ "$CHAIN_EXIT_STATE" == "parse_error" ]]; then
        FAILURES+=("boss_state.json parse failed: $BAD_CHILDREN_OR_NONE")
        CHAIN_EXIT_STATE=""
        CHILD_COUNT=0
        BAD_CHILDREN_OR_NONE="NONE"
    fi
    [[ "$BAD_CHILDREN_OR_NONE" == "NONE" ]] && BAD_CHILDREN="" || BAD_CHILDREN="$BAD_CHILDREN_OR_NONE"
else
    FAILURES+=("boss_state.json not found")
    CHAIN_EXIT_STATE="missing"
fi

# A: boss exit code is 0
if [[ "$BOSS_EXIT_CODE" != "0" ]]; then
    FAILURES+=("boss exited ${BOSS_EXIT_CODE:-stalled (no exit code)}")
fi

# B: chain ended with chain-done
if [[ "$CHAIN_EXIT_STATE" != "chain-done" ]]; then
    FAILURES+=("last handoff exited '$CHAIN_EXIT_STATE' (expected chain-done)")
fi

# C: at least 3 children ran (spec has 4 tasks, one per child)
if [[ "$CHILD_COUNT" -lt 3 ]]; then
    FAILURES+=("only $CHILD_COUNT children ran (expected >= 3)")
fi

# D: all children have a passing outcome
if [[ -n "$BAD_CHILDREN" ]]; then
    FAILURES+=("children with bad outcomes: $BAD_CHILDREN")
fi

# E: clean working tree on sandbox branch
DIRTY=$(git -C "$REPO_ROOT" status --porcelain 2>/dev/null) || DIRTY=""
if [[ -n "$DIRTY" ]]; then
    FAILURES+=("dirty working tree on sandbox branch")
fi

# F: no orphaned running/stalled children from this run
ORPHAN_IDS=$(STATE_ROOT="$STATE_ROOT" python3 -c "
import json, os, glob
orphans = []
for path in glob.glob(os.path.join(os.environ['STATE_ROOT'], 'claude-gremlins/*/state.json')):
    try:
        with open(path) as f:
            s = json.load(f)
        if s.get('parent_id') == '$BOSS_ID' and s.get('exit_code') is None:
            orphans.append(s.get('id', os.path.basename(os.path.dirname(path))))
    except Exception:
        pass
print(','.join(orphans))
" 2>/dev/null) || ORPHAN_IDS=""
if [[ -n "$ORPHAN_IDS" ]]; then
    FAILURES+=("orphaned running/stalled children: $ORPHAN_IDS")
fi

# G: LLM judge — plan reduction property (skipped if fewer than 3 child plans)
JUDGE_VERDICT="skipped"
JUDGE_PAIRS_JSON="[]"
JUDGE_MODEL_USED="${JUDGE_MODEL:-claude-sonnet-4-6}"

if [[ "$CHILD_COUNT" -ge 3 && -f "$STATE_DIR/boss_state.json" ]]; then
    echo "[e2e] running LLM judge (model=${JUDGE_MODEL:-claude-sonnet-4-6}, timeout=${JUDGE_TIMEOUT:-120}s)..."
    JUDGE_TMP=$(mktemp)
    STATE_DIR="$STATE_DIR" REPO_ROOT="$REPO_ROOT" SPEC="$SPEC" \
        JUDGE_MODEL="${JUDGE_MODEL:-claude-sonnet-4-6}" \
        JUDGE_TIMEOUT="${JUDGE_TIMEOUT:-120}" \
        python3 - > "$JUDGE_TMP" 2>/dev/null <<'JUDGE_PY' || true
import json, os, re, subprocess, sys

def emit(verdict, pairs=None, error=''):
    model = os.environ.get('JUDGE_MODEL', '')
    print(json.dumps({'verdict': verdict, 'pairs': pairs or [], 'model': model, 'error': error}))

try:
    state_dir = os.environ['STATE_DIR']
    repo_root = os.environ['REPO_ROOT']
    spec_path = os.environ['SPEC']
    model = os.environ.get('JUDGE_MODEL', 'claude-sonnet-4-6')
    timeout = int(os.environ.get('JUDGE_TIMEOUT', '120'))

    with open(os.path.join(state_dir, 'boss_state.json')) as f:
        bs = json.load(f)
    chain_base_ref = bs.get('chain_base_ref', '')

    with open(spec_path) as f:
        spec_content = f.read().strip()

    # Collect child plans in order (handoff-NNN-child.md)
    child_plans = []
    i = 1
    while True:
        path = os.path.join(state_dir, f'handoff-{i:03d}-child.md')
        if not os.path.exists(path):
            break
        with open(path) as f:
            child_plans.append(f.read().strip())
        i += 1

    if len(child_plans) < 2:
        emit('skipped', error='fewer than 2 child plans')
        sys.exit(0)

    # Collect per-child diffs from squash commits on sandbox since chain_base_ref
    diffs = []
    if chain_base_ref:
        try:
            out = subprocess.check_output(
                ['git', '-C', repo_root, 'log', '--reverse', '--format=%H',
                 f'{chain_base_ref}..HEAD'],
                text=True, stderr=subprocess.DEVNULL
            ).strip()
            for sha in [c for c in out.splitlines() if c.strip()]:
                try:
                    diff = subprocess.check_output(
                        ['git', '-C', repo_root, 'show', '--stat', '--patch', sha],
                        text=True, stderr=subprocess.DEVNULL
                    )
                    diffs.append(diff[:8000])
                except Exception:
                    diffs.append('(diff unavailable)')
        except Exception:
            pass  # non-fatal; judge proceeds without diffs

    # Build prompt
    sections = [f'## Overarching Spec\n\n{spec_content}']
    for idx, plan in enumerate(child_plans):
        sections.append(f'## Child Plan {idx + 1}\n\n{plan}')
    for idx, diff in enumerate(diffs):
        sections.append(f'## Landed Diff {idx + 1}\n\n```\n{diff}\n```')

    num_pairs = len(child_plans) - 1
    pairs_list = ', '.join(f'{j}→{j+1}' for j in range(1, num_pairs + 1))
    sections.append(f"""## Task

You are a chain-coherence judge. Evaluate the reduction property of this plan chain.

For each consecutive pair ({pairs_list}), determine whether plan_{{i+1}}'s scope is a \
strict subset of the work remaining after plan_i landed:
- No plan repeats work already landed by a prior child
- No plan expands scope beyond what the spec originally committed to
- The set of remaining work shrinks monotonically across the chain

Respond with ONLY a valid JSON object — no markdown fences, no text before or after:
{{"verdict":"pass","pairs":[{{"index":"1→2","pass":true,"rationale":"one sentence"}}]}}

verdict is "pass" if ALL pairs pass; "fail" if ANY pair fails.
pairs has one entry per adjacent pair: index (e.g. "1→2"), pass (bool), rationale (one sentence).""")

    prompt = '\n\n'.join(sections)

    try:
        proc = subprocess.run(
            ['claude', '-p', '--model', model],
            input=prompt, capture_output=True, text=True, timeout=timeout
        )
        raw = proc.stdout.strip()
        if not raw:
            emit('error', error=f'claude returned empty output (exit {proc.returncode}): {proc.stderr[:200]}')
            sys.exit(0)
    except subprocess.TimeoutExpired:
        emit('error', error=f'judge timed out after {timeout}s')
        sys.exit(0)
    except FileNotFoundError:
        emit('error', error='claude CLI not found in PATH')
        sys.exit(0)

    # Extract JSON from response (model may include prose before/after)
    try:
        result = json.loads(raw)
    except Exception:
        m = re.search(r'\{.*?\}', raw, re.DOTALL)
        raw_json = m.group(0) if m else raw
        result = json.loads(raw_json)
    try:
        verdict = result.get('verdict', 'error')
        if verdict not in ('pass', 'fail'):
            verdict = 'error'
        emit(verdict, pairs=result.get('pairs', []))
    except Exception as e:
        emit('error', error=f'response not valid JSON: {e!r}; raw={raw[:300]!r}')

except Exception as e:
    emit('error', error=f'unhandled exception: {e!r}')
JUDGE_PY
    JUDGE_OUT=$(cat "$JUDGE_TMP" 2>/dev/null) || JUDGE_OUT=""
    rm -f "$JUDGE_TMP"
    [[ -n "$JUDGE_OUT" ]] || JUDGE_OUT='{"verdict":"error","pairs":[],"model":"","error":"judge produced no output"}'

    JUDGE_VERDICT=$(python3 -c "
import json, sys
try:
    print(json.loads(sys.argv[1]).get('verdict', 'error'))
except Exception:
    print('error')
" "$JUDGE_OUT" 2>/dev/null) || JUDGE_VERDICT="error"

    JUDGE_PAIRS_JSON=$(python3 -c "
import json, sys
try:
    print(json.dumps(json.loads(sys.argv[1]).get('pairs', [])))
except Exception:
    print('[]')
" "$JUDGE_OUT" 2>/dev/null) || JUDGE_PAIRS_JSON="[]"

    JUDGE_MODEL_USED=$(python3 -c "
import json, sys
try:
    print(json.loads(sys.argv[1]).get('model', ''))
except Exception:
    print('')
" "$JUDGE_OUT" 2>/dev/null) || JUDGE_MODEL_USED=""

    case "$JUDGE_VERDICT" in
        fail)
            FAILING_PAIRS=$(python3 -c "
import json, sys
try:
    pairs = json.loads(sys.argv[1])
    bad = [p.get('index', '?') for p in pairs if not p.get('pass', True)]
    print(', '.join(bad) if bad else 'unknown')
except Exception:
    print('unknown')
" "$JUDGE_PAIRS_JSON" 2>/dev/null) || FAILING_PAIRS="unknown"
            FAILURES+=("plan chain fails reduction property (failing pairs: $FAILING_PAIRS)")
            ;;
        error)
            JUDGE_ERR=$(python3 -c "
import json, sys
try:
    print(json.loads(sys.argv[1]).get('error', 'unknown error'))
except Exception:
    print('unknown error')
" "$JUDGE_OUT" 2>/dev/null) || JUDGE_ERR="unknown error"
            FAILURES+=("LLM judge error: $JUDGE_ERR")
            ;;
    esac
fi

# ── Output ──────────────────────────────────────────────────────────────────────

ELAPSED=$(( $(date +%s) - START_TS ))

if [[ ${#FAILURES[@]} -gt 0 ]]; then
    for f in "${FAILURES[@]}"; do
        echo "[e2e] FAIL: $f"
    done
    echo "[e2e] FAIL"
    PASS_FAIL="fail"
else
    echo "[e2e] PASS"
    PASS_FAIL="pass"
fi

# Serialize failure reasons array for JSON
FAILURE_REASONS_JSON=$(python3 -c "
import json, sys
print(json.dumps(sys.argv[1:]))
" "${FAILURES[@]+"${FAILURES[@]}"}")

# Emit final-line JSON summary (must be last stdout line)
export BOSS_ID STATE_DIR SANDBOX_BRANCH PASS_FAIL CHAIN_EXIT_STATE ELAPSED \
    FAILURE_REASONS_JSON JUDGE_VERDICT JUDGE_PAIRS_JSON JUDGE_MODEL_USED
python3 -c "
import json, os

state_dir = os.environ['STATE_DIR']
try:
    with open(os.path.join(state_dir, 'boss_state.json')) as f:
        bs = json.load(f)
    children = bs.get('children', [])
    child_ids = [c['id'] for c in children]
    child_outcomes = {c['id']: c.get('outcome', '') for c in children}
    recs = bs.get('handoff_records', [])
    chain_exit = recs[-1]['exit_state'] if recs else ''
except Exception:
    child_ids = []
    child_outcomes = {}
    chain_exit = os.environ.get('CHAIN_EXIT_STATE', '')

try:
    judge_pairs = json.loads(os.environ.get('JUDGE_PAIRS_JSON', '[]'))
except Exception:
    judge_pairs = []

print(json.dumps({
    'outcome': os.environ['PASS_FAIL'],
    'boss_id': os.environ['BOSS_ID'],
    'sandbox_branch': os.environ['SANDBOX_BRANCH'],
    'boss_log': os.path.join(state_dir, 'log'),
    'child_ids': child_ids,
    'child_outcomes': child_outcomes,
    'chain_exit_state': chain_exit,
    'failure_reasons': json.loads(os.environ['FAILURE_REASONS_JSON']),
    'elapsed_seconds': int(os.environ['ELAPSED']),
    'judge_verdict': os.environ.get('JUDGE_VERDICT', 'skipped'),
    'judge_pairs': judge_pairs,
    'judge_model': os.environ.get('JUDGE_MODEL_USED', ''),
}, separators=(',', ':')))
" || echo "{\"outcome\":\"$PASS_FAIL\",\"boss_id\":\"$BOSS_ID\",\"sandbox_branch\":\"$SANDBOX_BRANCH\",\"error\":\"json_construction_failed\",\"elapsed_seconds\":$ELAPSED}"

[[ "$PASS_FAIL" == "pass" ]]
