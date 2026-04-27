"""Top-level dispatch for ``python -m pipeline.cli``.

The first positional argument selects the subcommand:

- ``local``   — full plan → implement → review-code → address-code chain
- ``review``  — review-code stage only (was ``localreview.py``)
- ``address`` — address-code stage only (was ``localaddress.py``)
- ``gh``      — full gh-issue-driven pipeline (Phase 3)
- ``boss``    — not yet implemented (Phase 4)

Remaining argv is forwarded to the chosen orchestrator entry point with
its own argparse contract preserved byte-stable from the old skill scripts.
"""

from __future__ import annotations

import sys
from typing import List, Optional


def main(argv: Optional[List[str]] = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    if not argv:
        sys.stderr.write(
            "usage: python -m pipeline.cli {local|review|address|gh|boss} [args...]\n"
        )
        return 1
    sub = argv[0]
    rest = argv[1:]
    if sub == "local":
        from .orchestrators.local import local_main
        return local_main(rest)
    if sub == "review":
        from .orchestrators.local import review_main
        return review_main(rest)
    if sub == "address":
        from .orchestrators.local import address_main
        return address_main(rest)
    if sub == "gh":
        from .orchestrators.gh import gh_main
        return gh_main(rest)
    if sub == "boss":
        from .orchestrators.boss import boss_main
        return boss_main(rest)
    sys.stderr.write(f"unknown subcommand: {sub}\n")
    return 1


if __name__ == "__main__":
    sys.exit(main())
