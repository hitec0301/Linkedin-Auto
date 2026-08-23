#!/usr/bin/env python3
"""JOB B — draft. Hourly.

Drafts every row the human selected and gave an angle to, and regenerates every
row they marked REVISE.

Hourly, not weekly, on purpose: a regenerate the human waits a day for is one
they will not use — they will rewrite it themselves and the pipeline will have
cost them time instead of saving it.

The work itself lives in lnp.draft, shared with the on-demand "redraft now"
the Review screen offers on a row that was sent back - this file is just the
cron entry point.

    python jobs/draft.py
    python jobs/draft.py --row 01HZY...     # one row
    python jobs/draft.py --print            # draft, print, write nothing
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lnp import drafting, runner, voice  # noqa: F401 - re-exported for tests to monkeypatch
from lnp.alerts import job_guard
from lnp.config import load_config
from lnp.draft import draft_all, next_slot  # noqa: F401 - next_slot re-exported for tests


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--row", help="only this row ID")
    parser.add_argument("--print", dest="print_only", action="store_true",
                        help="draft and print without writing anything")
    parser.add_argument("--tenant", help="only this account (hosted deployments)")
    args = parser.parse_args()

    config = load_config()
    with job_guard("draft", config):
        for run in runner.runs("draft", config, args.tenant):
            with runner.isolated(run, "draft"):
                draft_all(run, row=args.row, print_only=args.print_only)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
