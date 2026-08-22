"""Job A's work: feeds in, ten scored candidates out.

Shared by the Monday cron job (`jobs/curate.py`) and the one-time on-demand
run a new account gets from the Setup screen, so a manual run and the
scheduled one are provably the same code path rather than two copies that
drift.
"""

from __future__ import annotations

import ulid

from . import ingest, log, runner
from .models import Row, Status
from .scoring import enforce_mix, score_candidates
from .util import iso

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


def curate(run: "runner.Run", *, limit: int = None, no_write: bool = False) -> int:
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
            "Every feed item was a duplicate or outside the look-back window.",
            severity="warn",
            job=JOB,
        )
        return 0

    scored = score_candidates(config, candidates)
    if not scored:
        raise RuntimeError(
            f"scored none of {len(candidates)} candidates; the scoring call failed"
        )

    selected = enforce_mix(config, scored, limit)
    rows = build_rows(selected)

    for row in rows:
        print(
            f"[{row.RelevanceScore:>5}] {row.Audience:<14} {row.Theme:<14} "
            f"{row.SourceTitle[:70]}\n         {row.WhyItMatters[:100]}"
        )

    if no_write:
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
