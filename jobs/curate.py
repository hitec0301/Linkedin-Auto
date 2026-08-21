#!/usr/bin/env python3
"""JOB A — curate. Monday 07:00 ET.

Reads the feeds, drops duplicates, scores what is left, and writes ten
candidates into the Pipeline tab.

Then it stops. The human ticks Selected on three or four rows and writes a
one-line Angle for each. Nothing downstream happens without that.

    python jobs/curate.py
    python jobs/curate.py --limit 5 --no-write
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import ulid

from lnp import ingest, log, runner
from lnp.alerts import job_guard
from lnp.config import load_config
from lnp.models import Row, Status
from lnp.scoring import enforce_mix, score_candidates
from lnp.util import iso

logger = log.get("curate")
JOB = "curate"


def build_rows(selected) -> list[Row]:
    """Turn scored candidates into Pipeline rows.

    Selected, Angle, FinalText, RevisionNote and Reach are left empty on
    purpose: those columns belong to the human.
    """
    rows = []
    for item in selected:
        rows.append(
            Row(
                ID=ulid.new().str,
                CreatedAt=iso(),
                SourceURL=item.candidate.url,
                SourceTitle=item.candidate.title,
                Audience=item.audience,
                Theme=item.theme,
                WhyItMatters=item.why,
                RelevanceScore=f"{item.weighted_score:.2f}",
                Status=Status.NEW,
            )
        )
    return rows


def curate(run: runner.Run, args) -> int:
    """One account's curate. Returns the number of candidates written."""
    config, store = run.config, run.store
    history_days = int(config.get("ingest.history_days", 90))

    # Dedupe against what we have already surfaced, not just this run.
    history_urls, history_titles = store.recent_index(history_days)

    feeds = run.sources()
    candidates, results = ingest.collect(
        config, feeds, history_urls, history_titles
    )

    failed = [r for r in results if not r.ok]
    if failed:
        run.alert(
            f"{len(failed)} of {len(results)} feeds returned nothing",
            "\n".join(f"{r.name}: {r.error}" for r in failed),
            severity="warn" if len(failed) < len(results) else "error",
            job=JOB,
        )
    if not results:
        raise RuntimeError("no feeds were checked; is the source list empty?")
    if not candidates:
        run.alert(
            "curate found no new candidates",
            "Every feed item was a duplicate or outside the look-back window. "
            "Check scripts/validate_sources.py before assuming it was a quiet week.",
            severity="warn",
            job=JOB,
        )
        return 0

    scored = score_candidates(config, candidates)
    if not scored:
        raise RuntimeError(
            f"scored none of {len(candidates)} candidates; the scoring call failed"
        )

    selected = enforce_mix(config, scored, args.limit)
    rows = build_rows(selected)

    for row in rows:
        print(
            f"[{row.RelevanceScore:>5}] {row.Audience:<14} {row.Theme:<14} "
            f"{row.SourceTitle[:70]}\n         {row.WhyItMatters[:100]}"
        )

    if args.no_write:
        print(f"\n--no-write: {len(rows)} candidates not written")
        return 0

    store.append_rows(rows)
    archived = store.archive_old_rows(int(config.get("retention.archive_after_days", 90)))
    store.set_config_value("LAST_CURATE", iso())

    logger.info(
        "curate complete",
        extra={"written": len(rows), "archived": archived, "scored": len(scored)},
    )
    print(
        f"\n{len(rows)} candidates written. "
        "Tick Selected on 3-4 rows and write a one-line Angle for each."
    )
    return len(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, help="how many candidates to write")
    parser.add_argument(
        "--no-write", action="store_true", help="score and print, write nothing"
    )
    parser.add_argument("--tenant", help="only this account (hosted deployments)")
    args = parser.parse_args()

    config = load_config()
    with job_guard(JOB, config):
        for run in runner.runs(JOB, config, args.tenant):
            with runner.isolated(run, JOB):
                curate(run, args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
