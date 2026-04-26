import pathlib
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# TODO (Phase 2): add actual tests using FakeClaudeClient. Planned cases:
#   - run_plan_stage raises when the plan file isn't produced by the client
#   - run_implement_stage raises on empty diff
#   - run_triple_review raises when a worker errors or produces no output
#   - local_main resume preconditions (--resume-from implement without plan.md, etc.)
