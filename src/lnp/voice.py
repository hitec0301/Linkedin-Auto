"""Voice acquisition: the card, the feedback signals, and rule proposals.

The voice card is hand-editable markdown. Not an embedding, not a fine-tune —
a text the customer can open and fix in thirty seconds, which is the property
that matters when a draft comes out wrong on a Tuesday morning.

Two feedback signals feed it, weighted differently:

  NOTE  a RevisionNote. The human already abstracted the problem into an
        instruction, so the rule is nearly written. Primary signal.
  DIFF  a direct FinalText edit. Richer, but the rule has to be inferred from
        what changed, and inference is lossy. Secondary.

Nothing the model proposes reaches the card without a human ticking Accepted.
The model does not get to edit its own instructions.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

from rapidfuzz import fuzz

from . import log
from .config import Config
from .llm import complete_json
from .models import Row, Status
from .util import normalized_edit_distance

logger = log.get(__name__)

AMENDMENTS_BEGIN = "<!-- AMENDMENTS-BEGIN -->"
AMENDMENTS_END = "<!-- AMENDMENTS-END -->"

SIGNAL_NOTE = "NOTE"
SIGNAL_DIFF = "DIFF"

# Below this, a FinalText edit is a typo fix rather than a voice correction.
DIFF_SIGNAL_FLOOR = 0.02


@dataclass
class FeedbackItem:
    row_id: str
    signal: str
    instruction: str
    draft_text: str = ""
    final_text: str = ""


@dataclass
class Proposal:
    rule: str
    rationale: str
    signal: str
    occurrences: int = 1
    recurring: bool = False
    source_row_ids: List[str] = field(default_factory=list)


# --------------------------------------------------------------------------
# The card
# --------------------------------------------------------------------------


def existing_amendments(card: str) -> List[str]:
    """The rules currently inside the amendment markers."""
    match = re.search(
        re.escape(AMENDMENTS_BEGIN) + r"(.*?)" + re.escape(AMENDMENTS_END),
        card,
        re.DOTALL,
    )
    if not match:
        return []
    body = re.sub(r"<!--.*?-->", "", match.group(1), flags=re.DOTALL)
    return [
        line.lstrip("-* ").strip()
        for line in body.splitlines()
        if line.strip().startswith(("-", "*"))
    ]


def amend_card_text(card: str, rules: Sequence[str], where: str = "the voice card") -> tuple:
    """Insert accepted rules between the markers. Returns (card, written).

    Pure, so it works the same whether the card is a file on the owner's laptop
    or a row in a customer's account. Idempotent by rule text: re-running with
    the same rule is a no-op, so a failed run can be repeated without
    duplicating the card.
    """
    if AMENDMENTS_BEGIN not in card or AMENDMENTS_END not in card:
        raise ValueError(
            f"{where} is missing the amendment markers; add "
            f"{AMENDMENTS_BEGIN} / {AMENDMENTS_END} back before running the voice job"
        )
    present = existing_amendments(card)
    written: List[str] = []
    for rule in rules:
        rule = (rule or "").strip()
        if not rule or any(same_rule(rule, seen) for seen in present + written):
            continue
        written.append(rule)
    if not written:
        return card, []
    insertion = "".join(f"- {rule}\n" for rule in written)
    return card.replace(AMENDMENTS_END, insertion + AMENDMENTS_END), written


def amend_card_in_store(store, rules: Sequence[str]) -> List[str]:
    """Amend the card wherever this deployment keeps it."""
    card, written = amend_card_text(store.load_voice_card(), rules)
    if written:
        store.save_voice_card(card)
        logger.info("voice card amended", extra={"rules": written})
    return written


def same_rule(a: str, b: str) -> bool:
    """Rules that differ only in wording are the same rule."""
    return a.strip().lower() == b.strip().lower() or fuzz.token_set_ratio(a, b) >= 92


# --------------------------------------------------------------------------
# Prompt context
# --------------------------------------------------------------------------

NO_CORPUS_NOTE = """\
There is no corpus of this person's published posts yet, so you have no examples \
to imitate. Do not fall back on generic LinkedIn conventions to fill the gap — \
generic LinkedIn conventions are exactly what the voice card above rules out. \
Follow the card literally. Where the card is silent, write plainly and stop."""


def retrieve_examples(
    angle: str, published: Sequence[Row], limit: int = 8
) -> List[Row]:
    """The published posts most similar to this angle, as few-shot examples.

    Similarity is lexical (rapidfuzz over the angle and the published text)
    rather than embedding-based. At the corpus size this pipeline reaches —
    tens of posts, not thousands — that is enough to surface the right
    neighbours, and it avoids taking on an embedding provider, a vector store,
    and their two failure modes for a list that fits in memory.
    """
    if not angle or not published:
        return []
    ranked = sorted(
        published,
        key=lambda r: fuzz.token_set_ratio(angle, f"{r.Angle} {r.effective_text}"),
        reverse=True,
    )
    return ranked[:limit]


def build_voice_context(
    config: Config,
    *,
    angle: str = "",
    published: Optional[Sequence[Row]] = None,
    post_count: int = 0,
    card: str = "",
) -> str:
    """The voice block that opens every drafting prompt.

    The card is passed in because it belongs to a tenant, not to this checkout.
    """
    parts = [card.strip()]
    threshold = int(config.get("voice.retrieval_min_posts", 20))
    published = [r for r in (published or []) if r.status == Status.POSTED]

    if post_count < threshold or not published:
        parts.append(f"## No corpus yet\n\n{NO_CORPUS_NOTE}")
        return "\n\n---\n\n".join(parts)

    examples = retrieve_examples(
        angle, published, int(config.get("voice.retrieval_examples", 8))
    )
    rendered = "\n\n".join(
        f"### Example {i}\nAngle: {row.angle or '(not recorded)'}\n\n{row.effective_text}"
        for i, row in enumerate(examples, start=1)
    )
    parts.append(
        "## Published examples\n\n"
        "These are posts this person actually published, closest first by "
        "subject. Match their rhythm and their level of directness. Do not "
        "reuse their sentences.\n\n" + rendered
    )
    return "\n\n---\n\n".join(parts)


# --------------------------------------------------------------------------
# Feedback collection
# --------------------------------------------------------------------------


def collect_feedback(rows: Sequence[Row]) -> List[FeedbackItem]:
    """Read both signals off the Pipeline rows.

    A row can produce both: the human wrote a note and then edited the result
    anyway, which usually means the note was followed literally and still
    landed wrong.
    """
    items: List[FeedbackItem] = []
    for row in rows:
        if row.revision_note:
            items.append(
                FeedbackItem(
                    row_id=row.ID,
                    signal=SIGNAL_NOTE,
                    instruction=row.revision_note,
                    draft_text=row.DraftText,
                    final_text=row.FinalText,
                )
            )
        if row.status == Status.POSTED and row.was_edited:
            distance = normalized_edit_distance(row.DraftText, row.effective_text)
            if distance >= DIFF_SIGNAL_FLOOR:
                items.append(
                    FeedbackItem(
                        row_id=row.ID,
                        signal=SIGNAL_DIFF,
                        instruction="",
                        draft_text=row.DraftText,
                        final_text=row.effective_text,
                    )
                )
    return items


def find_recurring(
    items: Sequence[FeedbackItem], threshold: int = 3
) -> Dict[str, List[FeedbackItem]]:
    """Group near-identical instructions across different posts.

    The same instruction three times is the clearest possible signal that the
    system is not learning, and it is worth interrupting the human for.
    """
    groups: List[List[FeedbackItem]] = []
    for item in items:
        if item.signal != SIGNAL_NOTE or not item.instruction.strip():
            continue
        for group in groups:
            if fuzz.token_set_ratio(item.instruction, group[0].instruction) >= 80:
                group.append(item)
                break
        else:
            groups.append([item])
    return {
        group[0].instruction: group
        for group in groups
        if len({i.row_id for i in group}) >= threshold
    }


# --------------------------------------------------------------------------
# Rule proposals
# --------------------------------------------------------------------------

PROPOSAL_SYSTEM = """\
You maintain a voice card: a list of concrete writing rules for one person's \
LinkedIn posts. You are given corrections they made to drafts written for them.

Turn the corrections into rules. A rule is usable only if a writer could follow \
it without judgement and a reader could tell whether it was followed.

  Usable:   "Delete the closing question. End on the strongest declarative \
sentence in the post."
  Usable:   "Name the institution or company in the first two lines rather than \
saying 'a large university'."
  Unusable: "Write more authentically."
  Unusable: "Improve the flow."

Two kinds of input:

  NOTE  the person wrote the instruction themselves. Preserve their intent \
closely; your job is mostly to generalise it from this one post to all posts.
  DIFF  you get the draft and their edited version. Infer what they changed and \
why. Propose a rule only where the change is a pattern, not a one-off fact fix. \
Be conservative here: a wrong rule costs more than a missing one, because it \
will be applied to every future post.

Do not propose a rule that merely restates something already in the card.

Return a JSON array, at most {max_rules} entries, each: \
{{"rule": "...", "rationale": "one sentence on what evidence supports it", \
"signal": "NOTE" or "DIFF"}}

Return only the JSON array."""


def propose_rules(
    config: Config,
    items: Sequence[FeedbackItem],
    card: str,
    completer=None,
) -> List[Proposal]:
    """Ask the model for candidate rules. Nothing here touches the card."""
    if not items:
        return []
    completer = completer or complete_json
    max_rules = int(config.get("voice.max_proposals_per_run", 5))
    recurring = find_recurring(
        items, int(config.get("voice.recurrence_threshold", 3))
    )

    rendered = []
    for item in items:
        if item.signal == SIGNAL_NOTE:
            rendered.append(
                f"[NOTE on row {item.row_id}] instruction: {item.instruction}"
            )
        else:
            rendered.append(
                f"[DIFF on row {item.row_id}]\n--- draft ---\n{item.draft_text}\n"
                f"--- their version ---\n{item.final_text}"
            )

    user = (
        "Current voice card:\n\n"
        + card
        + "\n\n---\n\nCorrections since the last review:\n\n"
        + "\n\n".join(rendered)
    )
    raw = completer(
        config,
        system=PROPOSAL_SYSTEM.format(max_rules=max_rules),
        user=user,
        model=config.get("voice.model", "claude-sonnet-4-6"),
        max_tokens=int(config.get("voice.max_tokens", 4000)),
    )

    proposals: List[Proposal] = []
    for entry in raw if isinstance(raw, list) else []:
        rule = str(entry.get("rule", "")).strip() if isinstance(entry, dict) else ""
        if not rule:
            continue
        signal = str(entry.get("signal", SIGNAL_NOTE)).strip().upper()
        matched = [
            group
            for instruction, group in recurring.items()
            if fuzz.token_set_ratio(rule, instruction) >= 70
        ]
        sources = [i.row_id for i in (matched[0] if matched else [])]
        proposals.append(
            Proposal(
                rule=rule,
                rationale=str(entry.get("rationale", "")).strip(),
                signal=signal if signal in {SIGNAL_NOTE, SIGNAL_DIFF} else SIGNAL_NOTE,
                occurrences=len(sources) or 1,
                recurring=bool(matched),
                source_row_ids=sources,
            )
        )

    # Recurring first: a rule the human keeps repeating is the one to read.
    proposals.sort(key=lambda p: (not p.recurring, -p.occurrences))
    return proposals[:max_rules]
