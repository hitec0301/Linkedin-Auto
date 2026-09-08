"""Discuss: explore a source, argue with it, before it becomes a post.

Three model calls, each doing one thing:

  summarize   read the article, write a short neutral summary to react to.
  respond     read the transcript so far and engage with what was just
              argued - push back with a real counterpoint or complication
              when there is one, agree when there genuinely isn't a
              rebuttal, ask a sharpening question when that moves things
              further than either. Never just validate.
  synthesize  read the whole transcript, write the one thesis a first draft
              would be built from - the person's position and its strongest
              reasoning, in the spirit of what they actually argued.

Nothing here writes a pipeline row. A discussion is a scratchpad - explore a
source, argue it out, and abandon it with nothing left behind - until
turn_into_post() commits it to exactly one row, the same way "New post"
does, except the take is synthesized from the conversation instead of typed
directly - and, unlike "New post", it is drafted immediately through the
same redraft_now() path "Redraft with AI" uses, so the argument lands ready
for the variant picker instead of a bare take waiting on a second click.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Dict, List, Tuple

from . import drafting, log
from .config import Config
from .llm import complete
from .models import Row, Status, TERMINAL_STATUSES
from .redraft import redraft_now

if TYPE_CHECKING:
    from . import runner
    from .db.store import DiscussionRecord

logger = log.get("discuss")

SUMMARY_SYSTEM = """\
You are briefing someone on an article so they can react to it - agree, \
disagree, or push back on some part of it. Write a short, neutral summary: \
what the article actually says, not your opinion of it. Three to six \
sentences. No preamble, no headline, no markdown."""

RESPOND_SYSTEM = """\
You are a sharp, honest conversation partner helping someone work out what \
they think about an article before they write a LinkedIn post about it.

Engage with what they just said. If there is a real counterpoint, a \
complication, or a piece of the article their argument does not account \
for, raise it - directly, not hedged. If they are right and there \
genuinely is not a good rebuttal, say so plainly rather than manufacturing \
disagreement. If a question would sharpen their position more than either, \
ask it.

Use only what is in the article extract you are given as fact about the \
article; do not invent details it does not contain. Keep it conversational \
- a few sentences, not an essay. No preamble, no markdown."""

SYNTHESIZE_SYSTEM = """\
Read this conversation - someone working out what they think about an \
article, arguing it out loud. Write the one thesis a LinkedIn post would \
be built from: their position, and its strongest reasoning, in the spirit \
of what they actually argued - not a summary of the back-and-forth, not \
your own opinion, and not softened into something more balanced than they \
were.

One paragraph. Write it as an angle, the way a person would state their \
own take, not as "the user argued that...". No preamble, no markdown."""


def _extract_block(extract: "drafting.Extract") -> str:
    if extract.ok and extract.text:
        return f"Extract:\n{extract.text}"
    return (
        f"Extract: unavailable ({extract.note}). Discuss the article by "
        "title and URL only; do not invent details about its content."
    )


def _transcript(messages: List[Dict[str, str]]) -> str:
    return "\n\n".join(f"{m['role'].upper()}: {m['content']}" for m in messages)


def summarize(config: Config, *, source_url: str, source_title: str) -> Tuple[str, "drafting.Extract"]:
    extract = drafting.fetch_extract(config, source_url)
    prompt = "\n\n".join([
        f"Title: {source_title or '(no title given)'}",
        f"URL: {source_url}",
        _extract_block(extract),
    ])
    summary = complete(config, system=SUMMARY_SYSTEM, user=prompt, max_tokens=500)
    return summary, extract


def respond(config: Config, *, source_url: str, source_title: str, messages: List[Dict[str, str]]) -> str:
    extract = drafting.fetch_extract(config, source_url)
    prompt = "\n\n".join([
        f"Title: {source_title or '(no title given)'}",
        f"URL: {source_url}",
        _extract_block(extract),
        "---",
        "Conversation so far:",
        _transcript(messages),
    ])
    return complete(config, system=RESPOND_SYSTEM, user=prompt, max_tokens=600)


def synthesize_take(config: Config, *, messages: List[Dict[str, str]]) -> str:
    return complete(config, system=SYNTHESIZE_SYSTEM, user=_transcript(messages), max_tokens=500)


def start_discussion(
    run: "runner.Run", *, source_url: str, source_title: str, started_from_row_id: str = "",
) -> "DiscussionRecord":
    summary, _extract = summarize(run.config, source_url=source_url, source_title=source_title)
    return run.store.create_discussion(
        source_url=source_url,
        source_title=source_title,
        started_from_row_id=started_from_row_id,
        messages=[{"role": "assistant", "content": summary}],
    )


def continue_discussion(
    run: "runner.Run", discussion: "DiscussionRecord", user_message: str,
) -> "DiscussionRecord":
    messages = list(discussion.messages) + [{"role": "user", "content": user_message}]
    reply = respond(
        run.config,
        source_url=discussion.source_url,
        source_title=discussion.source_title,
        messages=messages,
    )
    return run.store.append_discussion_messages(
        discussion.id,
        [{"role": "user", "content": user_message}, {"role": "assistant", "content": reply}],
    )


def turn_into_post(run: "runner.Run", discussion: "DiscussionRecord") -> Row:
    """Commit a discussion to exactly one row, drafted immediately - the row
    it started from if it has one and that row is still live, otherwise a
    fresh one, exactly like "New post" creates. Never both.

    Delegates the actual drafting to redraft_now(): the same function
    "Redraft with AI" calls, so a discussion goes straight to a draft (two
    variants, while the account is still in that window) instead of landing
    on a bare take that needs a second click to become one. Can raise
    RedraftRefused, on the one row state that can't be redrafted - the
    target is mid-publish right now.
    """
    take = synthesize_take(run.config, messages=discussion.messages)
    store = run.store

    target = None
    if discussion.started_from_row_id:
        found = next(
            (r for r in store.pipeline_rows() if r.ID == discussion.started_from_row_id), None
        )
        if found is not None and found.status not in TERMINAL_STATUSES:
            target = found

    if target is None:
        target = Row(
            SourceURL=discussion.source_url,
            SourceTitle=discussion.source_title,
            Status=Status.NEW,
        )
        store.append_rows([target])

    result = redraft_now(run, target, take)

    store.mark_discussion_committed(discussion.id, result.row.ID)
    logger.info(
        "discussion turned into a post",
        extra={"discussion_id": discussion.id, "row_id": result.row.ID},
    )
    return result.row
