"""Redraft with AI: one action, reachable from any row, at any stage.

No DraftText yet -> draft fresh, using the take as the angle. DraftText
already exists -> revise in place, using the take as the instruction, and
land back on DRAFTED regardless of where the row was - including APPROVED,
since redrafting something you were about to publish should always require
a fresh decision to publish it again.

A terminal row (POSTED, SKIPPED, EXPIRED) is never rewritten in place:
PostURN, PostedAt and EditDistance are the pipeline's only record of what
actually went out, and overwriting them to relabel a historical row as a
fresh draft would corrupt that record. A new row is cloned from the same
candidate and drafted instead, leaving the original exactly as it was.

A revision that lands - DraftText existed, the take was consumed as an
instruction - is also a correction, and is learned from immediately: the
same rule-proposal step the weekly voice job used to batch runs here, once,
for this one correction, the moment it happens.
"""

from __future__ import annotations

from dataclasses import dataclass

import ulid

from . import drafting, log, runner, voice
from .db.store import AmendmentRecord, FeedbackRecord
from .draft import next_slot
from .models import TERMINAL_STATUSES, Row, Status
from .util import iso, utcnow
from .voice import FeedbackItem, SIGNAL_NOTE

logger = log.get("redraft")


class RedraftRefused(Exception):
    """Raised for a row this action must never touch, e.g. mid-publish."""


@dataclass
class RedraftResult:
    row: Row
    cloned: bool = False       # a new row was created rather than this one rewritten
    correction: bool = False   # a prior draft + instruction were consumed - learned from
    proposals: int = 0         # voice-rule proposals filed from this correction


def _clone_for_redraft(row: Row, take: str) -> Row:
    return Row(
        ID=ulid.new().str,
        CreatedAt=iso(),
        SourceURL=row.SourceURL,
        SourceTitle=row.SourceTitle,
        Audience=row.Audience,
        Theme=row.Theme,
        WhyItMatters=row.WhyItMatters,
        RelevanceScore=row.RelevanceScore,
        Selected="TRUE",
        Angle=take,
        Status=Status.NEW,
    )


def _learn_from_correction(run: "runner.Run", item: FeedbackItem) -> int:
    """File a rule proposal from one correction, the moment it happens.

    Mirrors what the weekly job did in batch, for exactly this one item:
    record it, check whether the same instruction has now come up often
    enough across different posts to flag as recurring, ask the model for a
    rule, and file it unaccepted. Nothing here writes the voice card - that
    still only happens when a person ticks Accepted.
    """
    config, store = run.config, run.store
    existing = store.feedback_records()
    seen = {(r.row_id, r.signal, (r.instruction or "")[:80]) for r in existing}
    key = (item.row_id, item.signal, item.instruction[:80])
    if key not in seen:
        store.append_feedback([
            FeedbackRecord(
                id=ulid.new().str, created_at=iso(utcnow()), row_id=item.row_id,
                signal=item.signal, instruction=item.instruction,
                draft_text=item.draft_text, final_text=item.final_text,
            )
        ])

    threshold = int(config.get("voice.recurrence_threshold", 3))
    history = [
        FeedbackItem(row_id=r.row_id, signal=r.signal, instruction=r.instruction,
                     draft_text=r.draft_text, final_text=r.final_text)
        for r in store.feedback_records()
    ]
    recurring_groups = voice.find_recurring(history, threshold)
    matched = None
    if item.signal == SIGNAL_NOTE:
        for instruction, group in recurring_groups.items():
            if voice.same_rule(item.instruction, instruction):
                matched = group
                break
    if matched:
        run.alert(
            f"\"{item.instruction}\" has come up {len(matched)} times",
            "The same correction repeating across different posts is the "
            "clearest sign the system is not learning. It is at the top of "
            "the Voice screen's proposal queue; accepting it should stop "
            "the repetition.",
            severity="warn",
            job="redraft",
        )

    card = store.load_voice_card()
    proposals = voice.propose_rules(config, [item], card)
    existing_rules = voice.existing_amendments(card)
    # Not `is_pending` - that means accepted-but-not-yet-applied. What we
    # want here is anything already sitting in the queue awaiting a decision,
    # so the same correction typed again before the last proposal is decided
    # does not file a second, near-duplicate row.
    queued_rules = [
        a.rule for a in store.amendment_records() if not a.accepted and not a.applied
    ]
    proposals = [
        p for p in proposals
        if not any(voice.same_rule(p.rule, seen) for seen in existing_rules + queued_rules)
    ]
    if matched:
        for p in proposals:
            p.recurring = True
            p.occurrences = len({i.row_id for i in matched})
            p.source_row_ids = list({i.row_id for i in matched})

    if proposals:
        store.append_amendments([
            AmendmentRecord(
                id=ulid.new().str, created_at=iso(utcnow()), rule=p.rule,
                rationale=p.rationale, signal=p.signal, occurrences=p.occurrences,
                recurring=p.recurring, accepted=False, source_row_ids=p.source_row_ids,
            )
            for p in proposals
        ])
    return len(proposals)


def redraft_now(run: "runner.Run", row: Row, take: str) -> RedraftResult:
    """Draft, revise, or clone-and-draft `row`, depending on where it is."""
    config, store = run.config, run.store
    take = (take or "").strip()

    if row.status == Status.POSTING:
        raise RedraftRefused(
            "this row is being published right now; wait for it to finish"
        )

    target = row
    cloned = False
    if row.status in TERMINAL_STATUSES:
        target = _clone_for_redraft(row, take)
        store.append_rows([target])
        cloned = True
    elif not target.DraftText:
        store.write_as_human(target, {"Angle": take})
    else:
        store.write_as_human(target, {"RevisionNote": take})

    correction = bool(target.DraftText) and not cloned
    post_count = store.post_count()
    published = store.published_rows()
    card = run.voice_card()
    variants = post_count < int(config.get("drafting.variants_until_post", 20))

    context = voice.build_voice_context(
        config, angle=target.angle, published=published,
        post_count=post_count, card=card,
    )

    previous_draft = target.DraftText
    if correction:
        text = drafting.revise(config, target, context)
    else:
        text = drafting.draft(config, target, context, variants=variants)
    problems = drafting.check_constraints(config, text)

    updates = {
        "Status": Status.DRAFTED,
        "DraftText": text,
        "CharCount": str(drafting.char_count(text)),
        "Error": "; ".join(problems),
    }
    if correction:
        updates["RevisionCount"] = str(target.revision_count + 1)
        updates["RevisionNote"] = ""
        store.write(target, updates, allow_revision_note=True)
    else:
        if not target.ScheduledFor:
            taken = {r.ScheduledFor for r in store.pipeline_rows() if r.ScheduledFor}
            updates["ScheduledFor"] = next_slot(config, taken)
        store.write(target, updates)

    proposals = 0
    if correction and take:
        proposals = _learn_from_correction(
            run,
            FeedbackItem(
                row_id=target.ID, signal=SIGNAL_NOTE, instruction=take,
                draft_text=previous_draft, final_text="",
            ),
        )

    logger.info(
        "redrafted",
        extra={"row_id": target.ID, "cloned": cloned, "correction": correction,
               "proposals": proposals},
    )
    return RedraftResult(row=target, cloned=cloned, correction=correction, proposals=proposals)
