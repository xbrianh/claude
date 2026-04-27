import pathlib
import sys

SKILLS_ROOT = pathlib.Path(__file__).resolve().parent.parent
for sub in ("gremlins", "handoff"):
    p = str(SKILLS_ROOT / sub)
    if p not in sys.path:
        sys.path.insert(0, p)
