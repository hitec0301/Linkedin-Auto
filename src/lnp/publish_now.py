"""Publish exactly one already-approved row, on request, right now.

Separate from lnp.publish's batch pass rather than a shared code path with
it: the batch job decides *which* rows are due and works through a list, this
decides nothing - the human already decided by pressing the button - and
publishes the one row it was given. Both go through the same LinkedIn client
and the same POSTING-before/POSTED-after sequence, so the two ways a post can
leave this system behave identically once a row is in flight.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import log, runner, tokens as token_mod
from .drafting import contains_both_variants
from .linkedin import LinkedIn, LinkedInError, PostNotConfirmed
from .models import Row, Status
from .util import iso

logger = log.get("publish_now")


@dataclass
class PublishResult:
    published: bool
    detail: str


def publish_one(run: "runner.Run", row: Row, *, dry_run: bool) -> PublishResult:
    """Attempt to publish `row` immediately. Never raises for an ordinary refusal.

    PAUSED, empty text, both variants still present, no LinkedIn connection, or
    LinkedIn itself refusing the post all come back as `PublishResult(False,
    reason)` rather than an exception - the approval that got the customer here
    still stands, and an unpublished-but-approved row is picked up by the next
    scheduled run exactly as if this button had never been pressed.
    """
    store = run.store

    # Every early return below is a soft refusal, not a failure: the approval
    # stands, and the row - still APPROVED, still due - is exactly what the
    # next scheduled run picks up. The reason is written to Error, the same
    # column jobs/publish.py's own flag() uses for the same purpose, so it
    # shows up on the card without a second response shape to keep in sync.
    def refuse(message: str) -> PublishResult:
        store.write(row, {"Error": message})
        return PublishResult(False, message)

    if store.is_paused():
        return refuse("publishing is paused for this account; turn it back on to post")

    text = row.effective_text
    if not text.strip():
        return refuse("there is nothing to publish in this row yet")
    if contains_both_variants(text):
        return refuse("both variants are still present; keep one before publishing")

    backend = run.token_backend()
    try:
        token_set = token_mod.load_fresh(
            run.config,
            backend=backend,
            alerter=lambda title, body: run.alert(title, body, severity="warn", job="publish"),
            app=run.linkedin_app(),
        )
        api = LinkedIn(run.config, token_set)
        author = api.person_urn()
    except token_mod.TokenError as exc:
        return refuse(str(exc))

    token_mod.cache_person_urn(run.config, token_set, author, backend=backend)
    store.set_config_value("PERSON_URN", author)
    payload = api.build_payload(author, text)

    if dry_run:
        logger.info(
            "dry run: not posting",
            extra={"row_id": row.ID, "chars": len(text), "payload": payload},
        )
        message = "dry run is on for this deployment: logged the payload, posted nothing"
        store.write(row, {"Error": message})
        return PublishResult(True, message)

    # POSTING before the call, POSTED after - the same sequence the scheduled
    # job uses, so a crash here leaves the row in the same recoverable state
    # jobs/publish.py's stuck-row check already knows how to resolve.
    store.transition(row, Status.POSTING, {"Error": ""})
    try:
        urn = api.create_post(payload)
    except PostNotConfirmed as exc:
        run.alert(
            f"Row {row.ID}: post may have been created but was not confirmed",
            f"{exc}\n\nThe row is left in POSTING. Check the profile and "
            "resolve it by hand; the next scheduled run will not retry it blindly.",
            job="publish",
        )
        return PublishResult(
            False,
            "could not confirm the post went out - check your LinkedIn profile "
            "before trying again",
        )
    except LinkedInError as exc:
        store.transition(row, Status.FAILED, {"Error": str(exc)[:900]})
        return PublishResult(False, f"LinkedIn refused the post: {exc}")

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
    logger.info("published on demand", extra={"row_id": row.ID, "urn": urn})
    return PublishResult(True, "published")
