#!/usr/bin/env python3
"""JOB B — draft. Hourly.

Drafts every row the human selected and gave an angle to, and regenerates every
row they marked REVISE.

Hourly, not weekly, on purpose: a regenerate the human waits a day for is one
they will not use — they will rewrite it themselves and the pipeline will have
cost them time instead of saving it.

    python jobs/draft.py
    python jobs/draft.py --row 01HZY...     # one row
    python jobs/draft.py --print            # draft, print, write nothing
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional, Set
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lnp import drafting, log, runner, voice
from lnp.alerts import job_guard
from lnp.config import load_config
from lnp.models import Row, Status
from lnp.util import iso, parse_dt, utcnow

logger = log.get("draft")
JOB = "draft"

DAYS = {"Mon": 0, "Tue": 1, "Wed": 2, "Thu": 3, "Fri": 4, "Sat": 5, "Sun": 6}


def next_slot(config, taken: Set[str], now: Optional[datetime] = None) -> str:
    """The next configured posting slot that no other row already holds.

    Slots are local-time weekday times from config; they come back as UTC ISO
    strings because everything in the Sheet is UTC.
    """
    now = now or utcnow()
    tz = ZoneInfo(config.get("schedule.timezone", "America/New_York"))
    slots = config.get("schedule.slots") or [{"day": "Tue", "time": "08:30"}]
    taken_times = {parse_dt(t) for t in taken if t}
    earliest = now + timedelta(hours=1)  # never schedule inside the current hour

    local_now = now.astimezone(tz)
    for offset in range(0, 28):
        day = (local_now + timedelta(days=offset)).date()
        for slot in slots:
            weekday = DAYS.get(str(slot.get("day", "Tue")).title()[:3])
            if weekday is None or day.weekday() != weekday:
                continue
            hour, _, minute = str(slot.get("time", "08:30")).partition(":")
            candidate = datetime(
                day.year, day.month, day.day, int(hour), int(minute or 0), tzinfo=tz
            ).astimezone(utcnow().tzinfo)
            if candidate < earliest:
                continue
            if any(t and abs((t - candidate).total_seconds()) < 3600 for t in taken_times):
                continue
            return iso(candidate)
    return iso(earliest)


def rows_to_draft(rows: List[Row]) -> List[Row]:
    """Selected rows with an angle that have never been drafted."""
    return [
        r for r in rows
        if r.status == Status.NEW and r.is_selected and r.angle
    ]


def rows_to_revise(rows: List[Row]) -> List[Row]:
    return [r for r in rows if r.status == Status.REVISE]


def draft_all(run: runner.Run, args) -> int:
    """One account's drafting pass."""
    config, store = run.config, run.store
    rows = store.pipeline_rows()

    expired = store.expire_stale(rows, config.staleness_hours)
    if expired:
        run.alert(
            f"{len(expired)} row(s) expired unpublished",
            "\n".join(f"{r.ID}: {r.SourceTitle[:80]}" for r in expired),
            severity="warn",
            job=JOB,
        )

    pending_draft = rows_to_draft(rows)
    pending_revise = rows_to_revise(rows)
    if args.row:
        pending_draft = [r for r in pending_draft if r.ID == args.row]
        pending_revise = [r for r in pending_revise if r.ID == args.row]

    # A ticked row with no angle is the human halfway through their ten
    # minutes. It is not an error, and it must not be drafted from nothing.
    waiting = [r for r in rows if r.status == Status.NEW and r.is_selected and not r.angle]
    for row in waiting:
        logger.info("selected but no angle yet", extra={"row_id": row.ID})

    if not pending_draft and not pending_revise:
        logger.info("nothing to draft", extra={"selected_without_angle": len(waiting)})
        return 0

    post_count = store.post_count()
    published = store.published_rows()
    card = run.voice_card()
    variants = post_count < int(config.get("drafting.variants_until_post", 20))
    max_revisions = config.max_revisions
    taken = {r.ScheduledFor for r in rows if r.ScheduledFor}

    for row in pending_draft:
        context = voice.build_voice_context(
            config, angle=row.angle, published=published,
            post_count=post_count, card=card,
        )
        text = drafting.draft(config, row, context, variants=variants)
        problems = drafting.check_constraints(config, text)
        slot = next_slot(config, taken)
        taken.add(slot)

        logger.info(
            "drafted",
            extra={"row_id": row.ID, "chars": drafting.char_count(text),
                   "variants": variants, "problems": problems, "scheduled_for": slot},
        )
        if args.print_only:
            print(f"\n=== {row.ID} — {row.SourceTitle[:70]}\nangle: {row.angle}\n\n{text}\n")
            continue

        store.transition(
            row,
            Status.DRAFTED,
            {
                "DraftText": text,
                "CharCount": str(drafting.char_count(text)),
                "ScheduledFor": slot,
                "Error": "; ".join(problems),
            },
        )

    for row in pending_revise:
        if not row.revision_note:
            logger.warning("REVISE row has no note", extra={"row_id": row.ID})
            continue

        if row.revision_count >= max_revisions:
            # Three instructions that did not land is a signal about the
            # angle, not the prose. Stop burning the human's attention.
            if not args.print_only:
                store.transition(
                    row,
                    Status.SKIPPED,
                    {"Error": f"skipped after {row.revision_count} revisions"},
                )
            run.alert(
                f"Row {row.ID} skipped after {max_revisions} revisions",
                "Three failed instructions usually means the angle is the "
                "problem, not the prose. Rewrite the Angle on a fresh row "
                "rather than revising this one again.\n\n"
                f"Source: {row.SourceTitle}\nAngle: {row.angle}\n"
                f"Last note: {row.revision_note}",
                severity="warn",
                job=JOB,
            )
            continue

        context = voice.build_voice_context(
            config, angle=row.angle, published=published,
            post_count=post_count, card=card,
        )
        text = drafting.revise(config, row, context)
        problems = drafting.check_constraints(config, text)

        logger.info(
            "revised",
            extra={"row_id": row.ID, "revision": row.revision_count + 1,
                   "chars": drafting.char_count(text), "problems": problems},
        )
        if args.print_only:
            print(f"\n=== {row.ID} (revision {row.revision_count + 1})\n"
                  f"note: {row.revision_note}\n\n{text}\n")
            continue

        # DraftText is overwritten here on purpose: a revision is new model
        # output, and the diff that matters is against what gets published.
        # RevisionNote is cleared through the one sanctioned exception, so
        # the human can see their instruction was consumed.
        store.transition(
            row,
            Status.DRAFTED,
            {
                "DraftText": text,
                "CharCount": str(drafting.char_count(text)),
                "RevisionCount": str(row.revision_count + 1),
                "RevisionNote": "",
                "Error": "; ".join(problems),
            },
            allow_revision_note=True,
        )

    logger.info(
        "draft complete",
        extra={"drafted": len(pending_draft), "revised": len(pending_revise)},
    )
    print(
        f"drafted {len(pending_draft)}, revised {len(pending_revise)}. "
        "Edit FinalText or write a RevisionNote, then set Status=APPROVED."
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--row", help="only this row ID")
    parser.add_argument("--print", dest="print_only", action="store_true",
                        help="draft and print without writing anything")
    parser.add_argument("--tenant", help="only this account (hosted deployments)")
    args = parser.parse_args()

    config = load_config()
    with job_guard(JOB, config):
        for run in runner.runs(JOB, config, args.tenant):
            with runner.isolated(run, JOB):
                draft_all(run, args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
