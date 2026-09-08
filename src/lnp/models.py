"""Row model and status state machine.

This module is the contract every job obeys. Two rules are enforced here rather
than in the jobs, because a job is exactly the place where a bug becomes a
published mistake:

  1. Only legal status transitions happen (`assert_transition`).
  2. Jobs never write human-owned columns (`assert_writable`).

Everything else in the package depends on this module.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Sequence, Set

from .util import (
    iso,
    normalized_edit_distance,
    parse_bool,
    parse_dt,
    parse_float,
    parse_int,
    utcnow,
)


class Status:
    """The row lifecycle."""

    NEW = "NEW"
    DRAFTED = "DRAFTED"
    REVISE = "REVISE"
    APPROVED = "APPROVED"
    POSTING = "POSTING"
    POSTED = "POSTED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"
    EXPIRED = "EXPIRED"


ALL_STATUSES: List[str] = [
    Status.NEW,
    Status.DRAFTED,
    Status.REVISE,
    Status.APPROVED,
    Status.POSTING,
    Status.POSTED,
    Status.FAILED,
    Status.SKIPPED,
    Status.EXPIRED,
]

# The state machine, written out in full. Anything not listed is illegal.
#
#   NEW -> DRAFTED -> (REVISE -> DRAFTED)* -> APPROVED -> POSTING -> POSTED
#                                                      \-> FAILED -> APPROVED
#   any -> SKIPPED ; DRAFTED/APPROVED -> EXPIRED ; POSTED is terminal
#
# POSTED and EXPIRED are terminal. SKIPPED is terminal too: a row the human
# retired stays retired, and reviving it is a new row, not a resurrected one.
LEGAL_TRANSITIONS: Dict[str, Set[str]] = {
    Status.NEW: {Status.DRAFTED, Status.SKIPPED},
    Status.DRAFTED: {Status.REVISE, Status.APPROVED, Status.SKIPPED, Status.EXPIRED},
    Status.REVISE: {Status.DRAFTED, Status.SKIPPED},
    Status.APPROVED: {Status.POSTING, Status.SKIPPED, Status.EXPIRED},
    Status.POSTING: {Status.POSTED, Status.FAILED},
    Status.FAILED: {Status.APPROVED, Status.SKIPPED},
    Status.POSTED: set(),
    Status.SKIPPED: set(),
    Status.EXPIRED: set(),
}

TERMINAL_STATUSES: Set[str] = {
    s for s, targets in LEGAL_TRANSITIONS.items() if not targets
}


class TransitionError(Exception):
    """Raised when a job attempts an illegal status change."""


class ColumnPermissionError(Exception):
    """Raised when a job attempts to write a column the human owns."""


def assert_transition(current: str, target: str) -> None:
    """Raise unless `current -> target` is a legal move.

    Called by every job before it writes a Status cell. A no-op self-transition
    is still illegal: it hides a logic error behind a write that looks harmless.
    """
    current = (current or "").strip().upper() or Status.NEW
    target = (target or "").strip().upper()
    if current not in LEGAL_TRANSITIONS:
        raise TransitionError(f"unknown current status {current!r}")
    if target not in LEGAL_TRANSITIONS:
        raise TransitionError(f"unknown target status {target!r}")
    if target not in LEGAL_TRANSITIONS[current]:
        if current in TERMINAL_STATUSES:
            raise TransitionError(
                f"{current} is terminal; cannot move to {target}"
            )
        raise TransitionError(f"illegal transition {current} -> {target}")


# Column order is load-bearing: it is the physical layout of the Pipeline tab.
# Adding a column means a migration and a mapping in db.store.FIELDS, so
# append rather than insert.
COLUMNS: List[str] = [
    "ID",
    "CreatedAt",
    "SourceURL",
    "SourceTitle",
    "Audience",
    "Theme",
    "WhyItMatters",
    "RelevanceScore",
    "Selected",
    "Angle",
    "DraftText",
    "FinalText",
    "RevisionNote",
    "RevisionCount",
    "CharCount",
    "Status",
    "ScheduledFor",
    "PostURN",
    "PostedAt",
    "EditDistance",
    "Reach",
    "Error",
    "ImagePrompt",
    "ImageData",
]

COLUMN_INDEX: Dict[str, int] = {name: i for i, name in enumerate(COLUMNS)}

# The human's columns. A job that writes one of these is overwriting a decision
# somebody made deliberately, which is the fastest way to lose their trust.
HUMAN_OWNED_COLUMNS: Set[str] = {
    "Selected",
    "Angle",
    "FinalText",
    "RevisionNote",
    "Reach",
}

# DraftText is immutable model output once written by a fresh draft. The
# revision path rewrites it on purpose (that is the point of a revision), but
# nothing may quietly fold human edits back into it: the DraftText/FinalText
# diff is the only learning signal this pipeline has.
MODEL_OWNED_COLUMNS: Set[str] = {
    "DraftText",
    "Audience",
    "Theme",
    "WhyItMatters",
    "RelevanceScore",
    "ImagePrompt",
    "ImageData",
}


def assert_writable(
    columns: Sequence[str], *,
    allow_revision_note: bool = False,
    allow_final_text_reset: bool = False,
) -> None:
    """Raise if any named column belongs to the human.

    Two narrow exceptions, both used only by redraft.py:

      `allow_revision_note`    after a successful regenerate, clears
                                RevisionNote so the human can see the
                                instruction was consumed.
      `allow_final_text_reset` after a redraft writes a fresh DraftText,
                                clears a FinalText left over from before it -
                                otherwise the old FinalText keeps winning in
                                `effective_text` and the new draft is
                                invisible until the human notices and clears
                                it themselves. Their edit was against the
                                draft that redraft just replaced; keeping it
                                around does not preserve a decision, it hides
                                the new one.
    """
    for name in columns:
        if name not in COLUMN_INDEX:
            raise ColumnPermissionError(f"unknown column {name!r}")
        if name in HUMAN_OWNED_COLUMNS:
            if name == "RevisionNote" and allow_revision_note:
                continue
            if name == "FinalText" and allow_final_text_reset:
                continue
            raise ColumnPermissionError(
                f"{name} is human-owned; jobs must not write it"
            )


# The mirror image of the rule above, for the other writer.
#
# The jobs must not write the human's columns. The interface the human uses
# must not write the model's, and the one that matters is DraftText: the
# distance between what the model wrote and what actually got published is the
# only learning signal this pipeline has, and an edit folded back into
# DraftText erases it. So the UI writes FinalText and the draft stays as it
# was, which is also why "revise" is a separate instruction rather than the
# human retyping the draft.
HUMAN_WRITABLE_COLUMNS: Set[str] = set(HUMAN_OWNED_COLUMNS)


def assert_human_writable(columns: Sequence[str]) -> None:
    """Raise if a human-facing edit touches a column the human does not own."""
    for name in columns:
        if name not in COLUMN_INDEX:
            raise ColumnPermissionError(f"unknown column {name!r}")
        if name not in HUMAN_WRITABLE_COLUMNS:
            raise ColumnPermissionError(
                f"{name} is not editable here"
                + (
                    ". Editing the draft directly would erase the difference "
                    "between what the model wrote and what you published, "
                    "which is the only thing this system learns from. Put your "
                    "version in FinalText instead."
                    if name == "DraftText"
                    else "."
                )
            )


class Audience:
    CORPORATE = "AUD_CORPORATE"
    ACADEMIC = "AUD_ACADEMIC"


class Theme:
    AI = "THM_AI"
    PLATFORM = "THM_PLATFORM"
    DELIVERY = "THM_DELIVERY"
    STRATEGY = "THM_STRATEGY"


ALL_AUDIENCES: List[str] = [Audience.CORPORATE, Audience.ACADEMIC]
ALL_THEMES: List[str] = [Theme.AI, Theme.PLATFORM, Theme.DELIVERY, Theme.STRATEGY]


@dataclass
class Row:
    """One candidate item, from ingestion through to a published post.

    Every field is a string. That is a leftover from when this pipeline stored
    its rows in a spreadsheet, where a cell had no other type; the database
    columns are properly typed and `db.store.FIELDS` is the one place the two
    meet. Retyping this is worth doing and is a change on its own.
    """

    ID: str = ""
    CreatedAt: str = ""
    SourceURL: str = ""
    SourceTitle: str = ""
    Audience: str = ""
    Theme: str = ""
    WhyItMatters: str = ""
    RelevanceScore: str = ""
    Selected: str = ""
    Angle: str = ""
    DraftText: str = ""
    FinalText: str = ""
    RevisionNote: str = ""
    RevisionCount: str = ""
    CharCount: str = ""
    Status: str = Status.NEW
    ScheduledFor: str = ""
    PostURN: str = ""
    PostedAt: str = ""
    EditDistance: str = ""
    Reach: str = ""
    Error: str = ""
    # The visual brief a model wrote for this post, and the image itself
    # (base64), from "Generate image". Both model-owned: regenerating
    # overwrites whichever attempt was there before, the same as a fresh
    # draft.
    ImagePrompt: str = ""
    ImageData: str = ""

    # ---- derived views -------------------------------------------------

    @property
    def status(self) -> str:
        return (self.Status or "").strip().upper() or Status.NEW

    @property
    def is_selected(self) -> bool:
        return parse_bool(self.Selected)

    @property
    def revision_count(self) -> int:
        return parse_int(self.RevisionCount, 0)

    @property
    def relevance_score(self) -> float:
        return parse_float(self.RelevanceScore, 0.0)

    @property
    def scheduled_for(self) -> Optional[datetime]:
        return parse_dt(self.ScheduledFor)

    @property
    def angle(self) -> str:
        return (self.Angle or "").strip()

    @property
    def revision_note(self) -> str:
        return (self.RevisionNote or "").strip()

    @property
    def effective_text(self) -> str:
        """The text that would actually be published.

        FinalText wins when the human has put something there. A cell holding
        only whitespace is not an edit — it falls through to the draft rather
        than publishing an empty post.
        """
        if (self.FinalText or "").strip():
            return self.FinalText.strip()
        return (self.DraftText or "").strip()

    @property
    def has_image(self) -> bool:
        return bool((self.ImageData or "").strip())

    @property
    def was_edited(self) -> bool:
        """True when the human's FinalText differs from the model's draft."""
        final = (self.FinalText or "").strip()
        return bool(final) and final != (self.DraftText or "").strip()

    def edit_distance(self) -> float:
        """Normalised distance between the draft and what gets published."""
        return normalized_edit_distance(
            (self.DraftText or "").strip(), self.effective_text
        )

    def is_stale(self, staleness_hours: int, now: Optional[datetime] = None) -> bool:
        """True when ScheduledFor is more than `staleness_hours` in the past.

        A blank ScheduledFor is never stale: the row simply has not been
        scheduled yet, and expiring it would delete work nobody finished.
        """
        due = self.scheduled_for
        if due is None:
            return False
        now = now or utcnow()
        return now - due > timedelta(hours=staleness_hours)

    def is_due(self, now: Optional[datetime] = None) -> bool:
        """True when ScheduledFor has arrived. A blank schedule is due now."""
        due = self.scheduled_for
        if due is None:
            return True
        return (now or utcnow()) >= due

    def touch_created(self) -> "Row":
        if not self.CreatedAt:
            self.CreatedAt = iso()
        return self


# --------------------------------------------------------------------------
# Health metric
# --------------------------------------------------------------------------
#
# The question this answers is whether the pipeline is worth keeping. If the
# human rewrites most drafts, it is costing them more editing time than writing
# from scratch would, and the honest recommendation is to switch it off. These
# functions are written to make that finding visible, not to soften it.


@dataclass
class HealthStats:
    published: int = 0
    clean: int = 0  # published with zero revisions and zero edits
    clean_rate: float = 0.0
    mean_edit_distance: float = 0.0
    mean_edit_distance_first: float = 0.0
    mean_edit_distance_last: float = 0.0
    days_running: float = 0.0
    verdict: str = ""

    @property
    def improving(self) -> Optional[bool]:
        """True when recent posts need less editing than the earliest ones."""
        if not (self.mean_edit_distance_first and self.mean_edit_distance_last):
            return None
        return self.mean_edit_distance_last < self.mean_edit_distance_first


def health_stats(
    rows: Sequence[Row], window: int = 10, floor: float = 0.5, evaluate_after_days: int = 30
) -> HealthStats:
    """Clean-publish rate and edit distance, first `window` posts vs. last."""
    published = sorted(
        (r for r in rows if r.status == Status.POSTED and r.PostedAt),
        key=lambda r: r.PostedAt,
    )
    stats = HealthStats(published=len(published))
    if not published:
        stats.verdict = "no posts published yet"
        return stats

    distances = [r.edit_distance() for r in published]
    stats.clean = sum(
        1 for r in published if r.revision_count == 0 and r.edit_distance() == 0.0
    )
    stats.clean_rate = round(stats.clean / len(published), 4)
    stats.mean_edit_distance = round(sum(distances) / len(distances), 4)
    stats.mean_edit_distance_first = round(
        sum(distances[:window]) / len(distances[:window]), 4
    )
    tail = distances[-window:]
    stats.mean_edit_distance_last = round(sum(tail) / len(tail), 4)

    first_posted = parse_dt(published[0].PostedAt)
    stats.days_running = (
        round((utcnow() - first_posted).total_seconds() / 86400, 1) if first_posted else 0.0
    )

    if stats.days_running >= evaluate_after_days and stats.clean_rate < floor:
        stats.verdict = (
            f"clean-publish rate is {stats.clean_rate:.0%} after "
            f"{stats.days_running:.0f} days, below the {floor:.0%} floor. This "
            f"pipeline is costing more editing time than it saves. Either fix "
            f"the voice card until drafts land, or shut it off."
        )
    elif stats.days_running < evaluate_after_days:
        stats.verdict = (
            f"{stats.days_running:.0f} days in; too early to judge "
            f"(evaluating at {evaluate_after_days})"
        )
    else:
        stats.verdict = f"clean-publish rate {stats.clean_rate:.0%}, above the floor"
    return stats
