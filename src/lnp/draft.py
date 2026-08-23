"""Job B's work: draft what was selected, regenerate what was sent back.

Shared by the hourly cron job (`jobs/draft.py`) and the on-demand "redraft
now" the Review screen offers on a row that was sent back with a note, so a
manual redraft and the scheduled one are provably the same code path.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import List, Optional, Set
from zoneinfo import ZoneInfo

from . import drafting, log, runner, voice
from .models import Row, Status
from .util import iso, parse_dt, utcnow

logger = log.get("draft")
JOB = "draft"

DAYS = {"Mon": 0, "Tue": 1, "Wed": 2, "Thu": 3, "Fri": 4, "Sat": 5, "Sun": 6}


def next_slot(config, taken: Set[str], now: Optional[datetime] = None) -> str:
    """The next configured posting slot that no other row already holds.

    Slots are local-time weekday times from config; they come back as UTC ISO
    strings because every timestamp in this system is UTC.
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


def draft_all(run: "runner.Run", *, row: Optional[str] = None, print_only: bool = False) -> int:
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
    if row:
        pending_draft = [r for r in pending_draft if r.ID == row]
        pending_revise = [r for r in pending_revise if r.ID == row]

    # A ticked row with no angle is the human halfway through their ten
    # minutes. It is not an error, and it must not be drafted from nothing.
    waiting = [r for r in rows if r.status == Status.NEW and r.is_selected and not r.angle]
    for waiting_row in waiting:
        logger.info("selected but no angle yet", extra={"row_id": waiting_row.ID})

    if not pending_draft and not pending_revise:
        logger.info("nothing to draft", extra={"selected_without_angle": len(waiting)})
        return 0

    post_count = store.post_count()
    published = store.published_rows()
    card = run.voice_card()
    variants = post_count < int(config.get("drafting.variants_until_post", 20))
    max_revisions = config.max_revisions
    taken = {r.ScheduledFor for r in rows if r.ScheduledFor}

    for candidate_row in pending_draft:
        context = voice.build_voice_context(
            config, angle=candidate_row.angle, published=published,
            post_count=post_count, card=card,
        )
        text = drafting.draft(config, candidate_row, context, variants=variants)
        problems = drafting.check_constraints(config, text)
        slot = next_slot(config, taken)
        taken.add(slot)

        logger.info(
            "drafted",
            extra={"row_id": candidate_row.ID, "chars": drafting.char_count(text),
                   "variants": variants, "problems": problems, "scheduled_for": slot},
        )
        if print_only:
            print(f"\n=== {candidate_row.ID} — {candidate_row.SourceTitle[:70]}\n"
                  f"angle: {candidate_row.angle}\n\n{text}\n")
            continue

        store.transition(
            candidate_row,
            Status.DRAFTED,
            {
                "DraftText": text,
                "CharCount": str(drafting.char_count(text)),
                "ScheduledFor": slot,
                "Error": "; ".join(problems),
            },
        )

    for revise_row in pending_revise:
        if not revise_row.revision_note:
            logger.warning("REVISE row has no note", extra={"row_id": revise_row.ID})
            continue

        if revise_row.revision_count >= max_revisions:
            # Three instructions that did not land is a signal about the
            # angle, not the prose. Stop burning the human's attention.
            if not print_only:
                store.transition(
                    revise_row,
                    Status.SKIPPED,
                    {"Error": f"skipped after {revise_row.revision_count} revisions"},
                )
            run.alert(
                f"Row {revise_row.ID} skipped after {max_revisions} revisions",
                "Three failed instructions usually means the angle is the "
                "problem, not the prose. Rewrite the Angle on a fresh row "
                "rather than revising this one again.\n\n"
                f"Source: {revise_row.SourceTitle}\nAngle: {revise_row.angle}\n"
                f"Last note: {revise_row.revision_note}",
                severity="warn",
                job=JOB,
            )
            continue

        context = voice.build_voice_context(
            config, angle=revise_row.angle, published=published,
            post_count=post_count, card=card,
        )
        text = drafting.revise(config, revise_row, context)
        problems = drafting.check_constraints(config, text)

        logger.info(
            "revised",
            extra={"row_id": revise_row.ID, "revision": revise_row.revision_count + 1,
                   "chars": drafting.char_count(text), "problems": problems},
        )
        if print_only:
            print(f"\n=== {revise_row.ID} (revision {revise_row.revision_count + 1})\n"
                  f"note: {revise_row.revision_note}\n\n{text}\n")
            continue

        # DraftText is overwritten here on purpose: a revision is new model
        # output, and the diff that matters is against what gets published.
        # RevisionNote is cleared through the one sanctioned exception, so
        # the human can see their instruction was consumed.
        store.transition(
            revise_row,
            Status.DRAFTED,
            {
                "DraftText": text,
                "CharCount": str(drafting.char_count(text)),
                "RevisionCount": str(revise_row.revision_count + 1),
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
