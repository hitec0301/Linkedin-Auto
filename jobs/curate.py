#!/usr/bin/env python3
"""JOB A — curate. Monday 07:00 ET.

Reads the feeds, drops duplicates, scores what is left, and writes ten
candidates into the Pipeline tab.

Then it stops. The human ticks Selected on three or four rows and writes a
one-line Angle for each. Nothing downstream happens without that.

The work itself lives in lnp.curate, shared with the on-demand "fetch
candidates now" the API offers a new account that has not reached its first
Monday yet - this file is just the cron entry point.

    python jobs/curate.py
    python jobs/curate.py --limit 5 --no-write
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lnp import ingest, runner  # noqa: F401 - ingest re-exported for tests to monkeypatch
from lnp.alerts import job_guard
from lnp.config import load_config
from lnp.curate import curate


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, help="how many candidates to write")
    parser.add_argument(
        "--no-write", action="store_true", help="score and print, write nothing"
    )
    parser.add_argument("--tenant", help="only this account (hosted deployments)")
    args = parser.parse_args()

    config = load_config()
    with job_guard("curate", config):
        for run in runner.runs("curate", config, args.tenant):
            with runner.isolated(run, "curate"):
                curate(run, limit=args.limit, no_write=args.no_write)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
