"""Scoring candidates and enforcing the mix.

The model scores relevance and tags audience and theme; the tier weight then
pushes against the news cycle, and the quota pass decides what actually reaches
the human. Without the quota pass, an AI-heavy news week produces ten AI
candidates and the human posts about AI four times — which is how a feed of
sources becomes a monoculture.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence

from . import log
from .config import Config
from .ingest import Candidate
from .llm import complete_json
from .models import ALL_AUDIENCES, ALL_THEMES, Audience, Theme

logger = log.get(__name__)

SCORING_SYSTEM = """\
You are triaging source material for an L&D leader in edtech who publishes on \
LinkedIn to two audiences at once: corporate L&D practitioners (heads of \
learning, enablement leads, HR technology buyers) and academic or institutional \
educators (instructional designers, faculty developers, university leadership).

For each item you receive, return:

- score: 1-10. How much does this item give that person something worth saying? \
Score for the strength of the take it enables, not the importance of the news. \
A minor procurement detail that reveals how budgets are actually moving beats a \
major funding announcement that invites nothing but a summary. Vendor press \
releases and funding rounds rarely clear 5.
- audience: AUD_CORPORATE or AUD_ACADEMIC — whichever would find it more useful. \
Pick one, never both.
- theme: exactly one of
    THM_AI        AI capability, adoption, evidence, or its limits in learning
    THM_PLATFORM  systems, tooling, LMS/LXP, procurement, infrastructure
    THM_DELIVERY  how learning is actually delivered: pedagogy, formats, practice
    THM_STRATEGY  budgets, org design, policy, business models, workforce strategy
- why: ONE sentence stating the implication for that audience. State what \
follows from this, not what it says. "Districts are buying tutoring seats faster \
than they can staff them" is an implication; "A report on AI tutoring adoption" \
is a summary and is wrong.

Return a JSON array with one object per item: \
{"i": <index>, "score": <int>, "audience": "...", "theme": "...", "why": "..."}

Return only the JSON array. Score every item you are given, in index order."""


@dataclass
class ScoredCandidate:
    candidate: Candidate
    score: float
    audience: str
    theme: str
    why: str

    @property
    def weighted_score(self) -> float:
        return round(self.score * self.candidate.weight, 3)


def _batches(items: Sequence, size: int) -> List[Sequence]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def _coerce(value: str, allowed: Sequence[str], default: str) -> str:
    text = str(value or "").strip().upper()
    return text if text in allowed else default


def score_candidates(
    config: Config,
    candidates: Sequence[Candidate],
    completer=None,
) -> List[ScoredCandidate]:
    """Score every candidate in batched LLM calls.

    A batch that fails to parse is logged and dropped rather than taking the run
    down: nine good candidates beat a crashed Monday morning.
    """
    if not candidates:
        return []
    # Resolved here rather than as a default argument so callers and tests can
    # substitute the completion function.
    completer = completer or complete_json
    batch_size = int(config.get("scoring.batch_size", 40))
    model = config.get("scoring.model", "claude-sonnet-4-6")
    max_tokens = int(config.get("scoring.max_tokens", 8000))

    scored: List[ScoredCandidate] = []
    for batch in _batches(list(candidates), batch_size):
        payload = [
            {
                "i": i,
                "title": c.title,
                "source": c.source_name,
                "tier": c.tier,
                "summary": (c.summary or "")[:600],
            }
            for i, c in enumerate(batch)
        ]
        user = "Items to triage:\n" + _dump(payload)
        try:
            raw = completer(
                config,
                system=SCORING_SYSTEM,
                user=user,
                model=model,
                max_tokens=max_tokens,
            )
        except Exception as exc:  # noqa: BLE001 - logged, batch skipped
            logger.error("scoring batch failed", extra={"error": str(exc), "size": len(batch)})
            continue

        by_index: Dict[int, dict] = {}
        for entry in raw if isinstance(raw, list) else []:
            try:
                by_index[int(entry["i"])] = entry
            except (KeyError, TypeError, ValueError):
                continue
        for i, candidate in enumerate(batch):
            entry = by_index.get(i)
            if not entry:
                logger.warning("item not scored", extra={"title": candidate.title[:120]})
                continue
            try:
                score = float(entry.get("score", 0))
            except (TypeError, ValueError):
                score = 0.0
            scored.append(
                ScoredCandidate(
                    candidate=candidate,
                    score=max(0.0, min(10.0, score)),
                    audience=_coerce(entry.get("audience"), ALL_AUDIENCES, Audience.CORPORATE),
                    theme=_coerce(entry.get("theme"), ALL_THEMES, Theme.STRATEGY),
                    why=str(entry.get("why", "")).strip(),
                )
            )
    logger.info("scored candidates", extra={"count": len(scored), "of": len(candidates)})
    return scored


def _dump(payload) -> str:
    import json

    return json.dumps(payload, ensure_ascii=False, indent=1)


def enforce_mix(
    config: Config,
    scored: Sequence[ScoredCandidate],
    limit: Optional[int] = None,
) -> List[ScoredCandidate]:
    """Select `limit` candidates that satisfy the audience and theme quotas.

    Quotas are filled first, best-weighted-score first within each bucket; the
    remaining slots are topped up by weighted score regardless of tag. A quota
    that cannot be filled (nothing in the feed matched that theme this week) is
    left short rather than padded with something irrelevant.
    """
    limit = limit or int(config.get("scoring.candidates", 10))
    pool = sorted(scored, key=lambda s: s.weighted_score, reverse=True)
    if len(pool) <= limit:
        return pool

    audience_quota = config.get("scoring.quotas.audience") or {}
    theme_quota = config.get("scoring.quotas.theme") or {}

    selected: List[ScoredCandidate] = []
    chosen = set()

    def take(predicate, count: int) -> None:
        for item in pool:
            if count <= 0:
                return
            if id(item) in chosen:
                continue
            if predicate(item):
                chosen.add(id(item))
                selected.append(item)
                count -= 1

    # Themes first: they are the narrower constraint, and an unfilled theme is
    # more visible in a week of posts than an unfilled audience split.
    for theme, share in sorted(theme_quota.items(), key=lambda kv: -kv[1]):
        take(lambda s, t=theme: s.theme == t, int(round(float(share) * limit)))
    for audience, share in sorted(audience_quota.items(), key=lambda kv: -kv[1]):
        have = sum(1 for s in selected if s.audience == audience)
        take(lambda s, a=audience: s.audience == a, int(round(float(share) * limit)) - have)

    take(lambda s: True, limit - len(selected))
    selected = selected[:limit]
    selected.sort(key=lambda s: s.weighted_score, reverse=True)

    logger.info(
        "mix enforced",
        extra={
            "selected": len(selected),
            "themes": _counts(selected, "theme"),
            "audiences": _counts(selected, "audience"),
        },
    )
    return selected


def _counts(items: Sequence[ScoredCandidate], attr: str) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for item in items:
        key = getattr(item, attr)
        out[key] = out.get(key, 0) + 1
    return out
