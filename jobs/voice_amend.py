#!/usr/bin/env python3
"""JOB D — voice_amend. Sunday 10:00 ET.

Three things, in this order:

  1. Apply the amendments the human ticked Accepted since last week. This is
     the only path by which anything reaches the voice card.
  2. Read the week's corrections and propose new rules into VoiceAmendments,
     unticked. The model does not get to edit its own instructions.
  3. Report the health metric, and say so plainly if the pipeline is costing
     more editing time than it saves.

    python jobs/voice_amend.py
    python jobs/voice_amend.py --dry-run
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import ulid

from lnp import log, runner, voice
from lnp.alerts import job_guard
from lnp.config import load_config
from lnp.models import health_stats
from lnp.store import AmendmentRecord, FeedbackRecord
from lnp.util import iso

logger = log.get("voice_amend")
JOB = "voice_amend"


def apply_accepted(store, dry_run: bool) -> list[str]:
    """Write ticked amendments into the card. Idempotent by rule text."""
    pending = store.pending_amendments()
    if not pending:
        return []
    rules = [r.rule for r in pending if r.rule.strip()]
    if dry_run:
        print(f"--dry-run: would apply {len(rules)} accepted rule(s):")
        for rule in rules:
            print(f"  - {rule}")
        return rules

    written = voice.amend_card_in_store(store, rules)
    stamp = iso()
    for record in pending:
        store.mark_amendment_applied(
            record,
            stamp if record.rule in written else "duplicate of an existing rule",
        )
    logger.info("applied accepted amendments", extra={"count": len(written)})
    return written


def record_feedback(store, items, dry_run: bool) -> list:
    """Record this week's corrections, without duplicating what is already there."""
    seen = {
        (r.row_id, r.signal, (r.instruction or "")[:80])
        for r in store.feedback_records()
    }
    fresh = [
        item for item in items
        if (item.row_id, item.signal, item.instruction[:80]) not in seen
    ]
    if fresh and not dry_run:
        stamp = iso()
        store.append_feedback([
            FeedbackRecord(
                id=ulid.new().str,
                created_at=stamp,
                row_id=item.row_id,
                signal=item.signal,
                instruction=item.instruction,
                draft_text=item.draft_text,
                final_text=item.final_text,
            )
            for item in fresh
        ])
    logger.info(
        "feedback recorded",
        extra={"new": len(fresh), "already_known": len(items) - len(fresh)},
    )
    return fresh


def amend(run: runner.Run, args) -> int:
    """One account's weekly voice pass."""
    config, store = run.config, run.store
    applied = apply_accepted(store, args.dry_run)
    if applied:
        print(f"Applied {len(applied)} rule(s) to the voice card:")
        for rule in applied:
            print(f"  + {rule}")

    rows = store.pipeline_rows()
    items = voice.collect_feedback(rows)
    record_feedback(store, items, args.dry_run)

    threshold = int(config.get("voice.recurrence_threshold", 3))
    recurring = voice.find_recurring(items, threshold)
    if recurring:
        run.alert(
            f"{len(recurring)} instruction(s) have come up {threshold}+ times",
            "The same correction repeating across different posts is the "
            "clearest sign the system is not learning. These are at the top "
            "of the VoiceAmendments queue; accepting them should stop the "
            "repetition. If one keeps recurring after being accepted, the "
            "rule as written is not specific enough.\n\n"
            + "\n".join(
                f"- \"{instruction}\" ({len(group)} posts)"
                for instruction, group in recurring.items()
            ),
            severity="warn",
            job=JOB,
        )

    proposals = []
    if items:
        card = store.load_voice_card()
        proposals = voice.propose_rules(config, items, card)
        existing_rules = voice.existing_amendments(card)
        proposals = [
            p for p in proposals
            if not any(voice.same_rule(p.rule, seen) for seen in existing_rules)
        ]

    if proposals and not args.dry_run:
        stamp = iso()
        # accepted=False, every time. The model does not get to tick its
        # own proposals, and there is no code path that sets this True.
        store.append_amendments([
            AmendmentRecord(
                id=ulid.new().str,
                created_at=stamp,
                rule=p.rule,
                rationale=p.rationale,
                signal=p.signal,
                occurrences=p.occurrences,
                recurring=p.recurring,
                accepted=False,
                source_row_ids=list(p.source_row_ids),
            )
            for p in proposals
        ])

    for proposal in proposals:
        marker = "RECURRING " if proposal.recurring else ""
        print(f"{marker}[{proposal.signal}] {proposal.rule}\n    {proposal.rationale}")

    stats = health_stats(
        rows,
        window=int(config.get("health.window", 10)),
        floor=float(config.get("health.clean_publish_floor", 0.5)),
        evaluate_after_days=int(config.get("health.evaluate_after_days", 30)),
    )
    logger.info("health", extra=vars(stats))
    print(
        f"\nHealth: {stats.published} published, "
        f"{stats.clean} clean ({stats.clean_rate:.0%}). "
        f"Mean edit distance first {config.get('health.window', 10)}: "
        f"{stats.mean_edit_distance_first}, last: {stats.mean_edit_distance_last}.\n"
        f"{stats.verdict}"
    )
    if "shut it off" in stats.verdict:
        run.alert(
            "This pipeline is not earning its keep",
            f"{stats.verdict}\n\n"
            f"Published: {stats.published}\nClean publishes: {stats.clean}\n"
            f"Mean edit distance first/last: {stats.mean_edit_distance_first} / "
            f"{stats.mean_edit_distance_last}",
            severity="warn",
            job=JOB,
        )

    if proposals:
        print(
            f"\n{len(proposals)} proposal(s) written. "
            "Tick Accepted on the ones you want; nothing reaches the voice "
            "card until you do."
        )
    return len(proposals)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="propose and print, write nothing")
    parser.add_argument("--tenant", help="only this account (hosted deployments)")
    args = parser.parse_args()

    config = load_config()
    with job_guard(JOB, config):
        for run in runner.runs(JOB, config, args.tenant):
            with runner.isolated(run, JOB):
                amend(run, args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
