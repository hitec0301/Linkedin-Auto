#!/usr/bin/env python3
"""JOB C — publish. Every 30 minutes.

Posts rows the human approved, once their slot has arrived. This is the only
code in the repo that writes to LinkedIn, and it is built to be boring:

  * only APPROVED rows are touched. Every other status is a no-op.
  * PAUSED in the Config tab stops the run before anything else happens.
  * POSTING is written before the HTTP call and POSTED after, so a crashed run
    cannot double-publish.
  * a row already stuck in POSTING is never blindly retried. The Posts API is
    asked whether the post exists; if that cannot be answered, a human is.
  * rows too far past their slot expire instead of publishing late.

    python jobs/publish.py --dry-run     # logs the payload, posts nothing
    python jobs/publish.py
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lnp import log, runner, tokens as token_mod
from lnp.alerts import job_guard
from lnp.config import load_config
from lnp.drafting import contains_both_variants
from lnp.linkedin import LinkedIn, LinkedInError, PostNotConfirmed
from lnp.models import Row, Status, health_stats
from lnp.util import iso, utcnow

logger = log.get("publish")
JOB = "publish"


def resolve_stuck_rows(run: runner.Run, rows, api: LinkedIn, author: str) -> None:
    """Deal with rows left in POSTING by an earlier crashed run.

    Never republish on a maybe. Either the Posts API confirms the post exists
    (mark it POSTED and move on) or it confirms it does not (mark it FAILED so
    the human can re-approve) or it cannot answer, in which case the row is
    left exactly as it is and a human is told.
    """
    store = run.store
    for row in [r for r in rows if r.status == Status.POSTING]:
        text = row.effective_text
        try:
            confirmed, urn = api.find_recent_post(author, text)
        except PostNotConfirmed as exc:
            run.alert(
                f"Row {row.ID} is stuck in POSTING and could not be verified",
                f"{exc}\n\nThe row has been left untouched. Check the LinkedIn "
                f"profile by hand:\n- if the post is there, set Status=POSTED "
                f"and paste the URN into PostURN.\n- if it is not, set "
                f"Status=FAILED, then Status=APPROVED to republish.\n\n"
                f"Source: {row.SourceTitle}",
                job=JOB,
            )
            continue

        if confirmed:
            store.transition(
                row,
                Status.POSTED,
                {
                    "PostURN": urn or "",
                    "PostedAt": iso(),
                    "EditDistance": str(row.edit_distance()),
                    "Error": "recovered: post was already live",
                },
            )
            logger.info("recovered stuck row as posted", extra={"row_id": row.ID, "urn": urn})
        else:
            store.transition(
                row,
                Status.FAILED,
                {"Error": "crashed mid-publish; the post was not created"},
            )
            run.alert(
                f"Row {row.ID} failed mid-publish and was not posted",
                "The Posts API shows no matching post. The row is now FAILED. "
                "Set it back to APPROVED to try again.",
                severity="warn",
                job=JOB,
            )


def flag(run: runner.Run, row: Row, marker: str, title: str, body: str) -> None:
    """Refuse to publish a row, leaving it APPROVED with the reason in Error.

    The row is not moved to FAILED: nothing was attempted, and the problem is
    one the human fixes in a cell. Leaving it APPROVED means their fix
    publishes on the next run with no further ceremony. The alert fires only
    when the flag is new, so a row waiting on a human does not alert every
    thirty minutes until they get to it.
    """
    already_flagged = row.Error.strip() == marker
    if not already_flagged:
        run.store.write(row, {"Error": marker})
    logger.warning("refused to publish", extra={"row_id": row.ID, "reason": marker})
    if not already_flagged:
        run.alert(title, body, severity="warn", job=JOB)


def due_rows(rows, config, now=None) -> list[Row]:
    """APPROVED rows whose slot has arrived and which are not stale.

    Only APPROVED. This is invariant 1 and there is no flag that changes it.
    """
    now = now or utcnow()
    ready = [
        r
        for r in rows
        if r.status == Status.APPROVED
        and r.is_due(now)
        and not r.is_stale(config.staleness_hours, now)
    ]
    ready.sort(key=lambda r: r.ScheduledFor or "")
    return ready


def publish(run: runner.Run, args, dry_run: bool) -> int:
    """One account's publish pass. Returns how many posts went out."""
    config, store = run.config, run.store
    # The kill switch, before anything else. It has to be reachable in five
    # seconds from a phone, so it is one setting and nothing else.
    if store.is_paused():
        logger.warning("PAUSED is set; exiting without publishing")
        print("Publishing is paused. Nothing was published.")
        return 0

    rows = store.pipeline_rows()

    expired = store.expire_stale(rows, config.staleness_hours)
    if expired:
        run.alert(
            f"{len(expired)} approved row(s) expired before publishing",
            "\n".join(
                f"{r.ID}: scheduled {r.ScheduledFor} — {r.SourceTitle[:70]}"
                for r in expired
            ),
            severity="warn",
            job=JOB,
        )

    ready = due_rows(rows, config)
    stuck = [r for r in rows if r.status == Status.POSTING]
    if not ready and not stuck:
        logger.info("nothing due", extra={"rows": len(rows)})
        return 0

    backend = run.token_backend()
    tokens = token_mod.load_fresh(
        config,
        backend=backend,
        alerter=lambda title, body: run.alert(title, body, severity="warn", job=JOB),
        app=run.linkedin_app(),
    )
    api = LinkedIn(config, tokens)
    author = api.person_urn()
    token_mod.cache_person_urn(config, tokens, author, backend=backend)
    store.set_config_value("PERSON_URN", author)

    if stuck:
        resolve_stuck_rows(run, rows, api, author)
        # Re-read: a recovered row may now be POSTED, and a failed one must
        # not be picked up in the same run.
        rows = store.pipeline_rows()
        ready = due_rows(rows, config)

    limit = args.limit or int(config.get("publish.max_posts_per_run", 1))
    published = 0

    for row in ready[:limit]:
        text = row.effective_text

        if not text.strip():
            flag(
                run, row,
                "approved with no text to publish",
                f"Row {row.ID} is APPROVED but has no text",
                "Nothing was posted. Put the post text in FinalText, or "
                "re-draft the row.",
            )
            continue

        # Two variants still in the row means nobody has said which post
        # this is. Refuse rather than guess.
        if contains_both_variants(text):
            flag(
                run, row,
                "ambiguous: both variants are still present",
                f"Row {row.ID} was approved with both variants still in it",
                "Nothing was posted. Copy the variant you want into "
                "FinalText, or delete the other one from DraftText. The row "
                "stays APPROVED, so it will publish on the next run once "
                f"there is only one post in it.\n\nSource: {row.SourceTitle}",
            )
            continue

        payload = api.build_payload(author, text)

        if dry_run:
            logger.info(
                "dry run: not posting",
                extra={"row_id": row.ID, "chars": len(text), "payload": payload},
            )
            print(
                f"\n--- DRY RUN — row {row.ID} ({len(text)} chars), "
                f"scheduled {row.ScheduledFor or 'now'}\n"
                f"POST {api.base}/rest/posts\n"
                f"LinkedIn-Version: {api.version}\n"
                f"X-Restli-Protocol-Version: 2.0.0\n"
                f"Authorization: Bearer <redacted>\n\n"
                f"{json.dumps(payload, indent=2, ensure_ascii=False)}\n"
            )
            published += 1
            continue

        # POSTING before the call, POSTED after. If the process dies in
        # between, the row stays POSTING and the recovery path above — not
        # a blind retry — decides what happened.
        store.transition(row, Status.POSTING, {"Error": ""})
        try:
            urn = api.create_post(payload)
        except PostNotConfirmed as exc:
            run.alert(
                f"Row {row.ID}: post may have been created but was not confirmed",
                f"{exc}\n\nThe row is left in POSTING. Check the profile and "
                "resolve it by hand; the next run will not retry it blindly.",
                job=JOB,
            )
            continue
        except LinkedInError as exc:
            store.transition(row, Status.FAILED, {"Error": str(exc)[:900]})
            run.alert(
                f"Row {row.ID} failed to publish",
                f"{exc}\n\nSet Status back to APPROVED to retry.",
                job=JOB,
            )
            continue

        store.transition(
            row,
            Status.POSTED,
            {
                "PostURN": urn,
                "PostedAt": iso(),
                "EditDistance": str(row.edit_distance()),
                "Error": "",
            },
        )
        store.bump_post_count()
        published += 1
        logger.info(
            "published",
            extra={
                "row_id": row.ID,
                "urn": urn,
                "edit_distance": row.edit_distance(),
                "revisions": row.revision_count,
            },
        )

    if published and not dry_run:
        store.set_config_value("LAST_PUBLISH", iso())
        report_health(run)

    print(
        f"{'dry run: ' if dry_run else ''}{published} post(s) "
        f"{'would be ' if dry_run else ''}published."
    )
    return published


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true",
        help="log the exact payload and skip the HTTP call (also set by config)",
    )
    parser.add_argument("--limit", type=int, help="max posts this run")
    parser.add_argument("--tenant", help="only this account (hosted deployments)")
    args = parser.parse_args()

    config = load_config()
    dry_run = args.dry_run or config.dry_run

    with job_guard(JOB, config):
        for run in runner.runs(JOB, config, args.tenant):
            with runner.isolated(run, JOB):
                publish(run, args, dry_run)
    return 0


def report_health(run: runner.Run) -> None:
    """Log the health metric, and say plainly when the pipeline is not earning
    its keep."""
    config = run.config
    stats = health_stats(
        run.store.pipeline_rows(),
        window=int(config.get("health.window", 10)),
        floor=float(config.get("health.clean_publish_floor", 0.5)),
        evaluate_after_days=int(config.get("health.evaluate_after_days", 30)),
    )
    logger.info("health", extra=vars(stats))
    if "shut it off" in stats.verdict:
        run.alert(
            "This pipeline is not earning its keep",
            f"{stats.verdict}\n\n"
            f"Published: {stats.published}\n"
            f"Clean publishes (no revisions, no edits): {stats.clean}\n"
            f"Mean edit distance, first {config.get('health.window', 10)}: "
            f"{stats.mean_edit_distance_first}\n"
            f"Mean edit distance, last {config.get('health.window', 10)}: "
            f"{stats.mean_edit_distance_last}",
            severity="warn",
            job=JOB,
        )


if __name__ == "__main__":
    raise SystemExit(main())
