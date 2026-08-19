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

from lnp import log, voice
from lnp.alerts import alert, job_guard
from lnp.config import load_config
from lnp.models import health_stats
from lnp.sheets import AMENDMENT_COLUMNS, FEEDBACK_COLUMNS, Sheets
from lnp.util import iso, parse_bool

logger = log.get("voice_amend")
JOB = "voice_amend"


def apply_accepted(sheets: Sheets, config, dry_run: bool) -> list[str]:
    """Write ticked amendments into the card. Idempotent by rule text."""
    records = sheets.read_tab(sheets.amendments_tab, AMENDMENT_COLUMNS)
    pending = [
        r for r in records
        if parse_bool(r.get("Accepted")) and not r.get("Applied", "").strip()
    ]
    if not pending:
        return []
    rules = [r["Rule"] for r in pending if r.get("Rule", "").strip()]
    if dry_run:
        print(f"--dry-run: would apply {len(rules)} accepted rule(s):")
        for rule in rules:
            print(f"  - {rule}")
        return rules

    written = voice.append_amendments(config, rules)
    stamp = iso()
    for record in pending:
        sheets.update_tab_cell(
            sheets.amendments_tab,
            int(record["_row_number"]),
            "Applied",
            stamp if record["Rule"] in written else "duplicate of an existing rule",
            AMENDMENT_COLUMNS,
        )
    logger.info("applied accepted amendments", extra={"count": len(written)})
    return written


def record_feedback(sheets: Sheets, items, dry_run: bool) -> list:
    """Append this week's corrections to the Feedback tab, without duplicates."""
    existing = sheets.read_tab(sheets.feedback_tab, FEEDBACK_COLUMNS)
    seen = {
        (r.get("RowID", ""), r.get("Signal", ""), (r.get("Instruction", "") or "")[:80])
        for r in existing
    }
    fresh = [
        item for item in items
        if (item.row_id, item.signal, item.instruction[:80]) not in seen
    ]
    if fresh and not dry_run:
        stamp = iso()
        sheets.append_generic(
            sheets.feedback_tab,
            [item.as_values(stamp, ulid.new().str) for item in fresh],
        )
    logger.info(
        "feedback recorded",
        extra={"new": len(fresh), "already_known": len(items) - len(fresh)},
    )
    return fresh


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="propose and print, write nothing")
    args = parser.parse_args()

    config = load_config()
    with job_guard(JOB, config):
        sheets = Sheets.open(config)

        applied = apply_accepted(sheets, config, args.dry_run)
        if applied:
            print(f"Applied {len(applied)} rule(s) to the voice card:")
            for rule in applied:
                print(f"  + {rule}")

        rows = sheets.pipeline_rows()
        items = voice.collect_feedback(rows)
        record_feedback(sheets, items, args.dry_run)

        threshold = int(config.get("voice.recurrence_threshold", 3))
        recurring = voice.find_recurring(items, threshold)
        if recurring:
            alert(
                config,
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
            proposals = voice.propose_rules(config, items, voice.load_card(config))
            existing_rules = voice.existing_amendments(voice.load_card(config))
            proposals = [
                p for p in proposals
                if not any(voice.same_rule(p.rule, seen) for seen in existing_rules)
            ]

        if proposals and not args.dry_run:
            stamp = iso()
            sheets.append_generic(
                sheets.amendments_tab,
                [
                    [
                        ulid.new().str, stamp, p.rule, p.rationale, p.signal,
                        str(p.occurrences), "RECURRING" if p.recurring else "",
                        "FALSE", "", ", ".join(p.source_row_ids),
                    ]
                    for p in proposals
                ],
            )

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
            alert(
                config,
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
                f"\n{len(proposals)} proposal(s) written to VoiceAmendments. "
                "Tick Accepted on the ones you want; nothing reaches the voice "
                "card until you do."
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
