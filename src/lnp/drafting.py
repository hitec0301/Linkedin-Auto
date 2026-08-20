"""Drafting and revision.

The human's Angle is the thesis. The source article is evidence for it. A model
left to its own devices will summarise the article instead, which produces a
post that reads like everyone else's post about the same article — so the
system prompt says this outright, twice.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional, Tuple

import requests
from bs4 import BeautifulSoup

from . import log
from .config import Config
from .llm import complete
from .models import Row

logger = log.get(__name__)

VARIANT_A = "--- VARIANT A ---"
VARIANT_B = "--- VARIANT B ---"

STRIP_TAGS = [
    "script", "style", "nav", "footer", "header", "aside", "form",
    "noscript", "iframe", "svg", "button",
]

PREAMBLE = re.compile(
    r"^\s*(here(?:'|’)?s|here is|sure|certainly|below is|i(?:'|’)?ve (?:written|drafted))\b[^\n]*:\s*\n+",
    re.IGNORECASE,
)


@dataclass
class Extract:
    text: str
    ok: bool
    note: str = ""


# --------------------------------------------------------------------------
# Source extraction
# --------------------------------------------------------------------------


def fetch_extract(config: Config, url: str) -> Extract:
    """Fetch the source article and reduce it to readable text.

    Failure is tolerable — the angle carries the post — but the caller must
    then tell the model it has no source detail to draw on.
    """
    if not url:
        return Extract("", False, "no source URL on the row")
    timeout = int(config.get("drafting.fetch_timeout_seconds", 20))
    cap = int(config.get("drafting.extract_max_chars", 8000))
    user_agent = config.get("drafting.user_agent", "lnp-pipeline/1.0")
    try:
        response = requests.get(
            url, timeout=timeout, headers={"User-Agent": user_agent}
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        logger.warning("extraction failed", extra={"url": url, "error": str(exc)})
        return Extract("", False, f"could not fetch the article ({type(exc).__name__})")

    # lxml is faster and handles broken markup better, but it is an optional
    # dependency: it needs a C build where no wheel matches, and the stdlib
    # parser is a perfectly adequate fallback for pulling text out of a page.
    try:
        soup = BeautifulSoup(response.content, "lxml")
    except Exception:  # noqa: BLE001 - not installed, or it rejected the markup
        soup = BeautifulSoup(response.content, "html.parser")
    for tag in soup(STRIP_TAGS):
        tag.decompose()

    body = soup.find("article") or soup.find("main") or soup.body or soup
    text = re.sub(r"\n{3,}", "\n\n", body.get_text("\n", strip=True))
    text = re.sub(r"[ \t]{2,}", " ", text)
    if len(text) < 200:
        return Extract(text, False, "the page returned almost no readable text")
    return Extract(text[:cap], True)


# --------------------------------------------------------------------------
# Prompts
# --------------------------------------------------------------------------

DRAFT_SYSTEM = """\
You write LinkedIn posts for one person, from a thesis they give you.

The angle you are given is the thesis of the post. The source article is \
evidence for that thesis, not the subject of the post. If what you produce \
reads like a summary of the article, you have failed, however accurate the \
summary is — the person supplying the angle already read the article and has \
something to say about it that the article does not say.

Write in their voice as described by the voice card. The card outranks any \
convention you have learned about how LinkedIn posts are written. Where the \
card bans something, it is banned even if it would make the post perform \
better.

Use only figures, dates, names, and quantities that appear in the source \
extract you are given. Do not calculate, estimate, round, or infer a number \
that is not written there. If you want a number and the extract does not have \
one, write the sentence without it.

Return the post text and nothing else. No preamble, no title, no commentary, \
no markdown fences."""

CONSTRAINTS = """\
Constraints:
- {min_chars}-{max_chars} characters in total.
- The first two lines carry the hook and the claim, and must make sense alone. \
LinkedIn truncates near {hook_chars} characters behind a "see more" link; \
anything after that is read only by people the first two lines convinced.
- Short paragraphs, one idea each, with a blank line between them.
- Close on a statement. Never close on a question.
- No em-dashes anywhere.
- At most {max_hashtags} hashtags, at the end, or none.
- Include the source URL on its own line near the end, as plain text.
- Only numbers that appear in the source extract."""

VARIANTS_INSTRUCTION = """\
Write two different posts from the same angle. Make them genuinely different \
in approach, not the same post reworded: for example, one opening on the \
concrete case and one opening on the claim it supports.

Format exactly:

{a}
<first post>

{b}
<second post>"""

SINGLE_INSTRUCTION = "Write one post."

REVISION_SYSTEM = """\
You are revising a LinkedIn post that the writer has asked you to change.

Make the change they asked for. Leave everything else alone — sentences they \
did not object to should come through recognisably intact. A revision that \
rewrites the whole post is a failure even if the new post is better, because \
the writer cannot tell whether their instruction was understood.

Every constraint from the original brief still applies, including the voice \
card, the character range, and the ban on numbers that are not in the source \
extract.

Return the revised post text and nothing else."""


def build_draft_prompt(
    config: Config,
    *,
    voice_context: str,
    angle: str,
    title: str,
    url: str,
    why_it_matters: str,
    extract: Extract,
    variants: bool,
) -> str:
    """Assemble the drafting prompt: voice, then angle, then source, then rules."""
    parts = [
        "# Voice",
        voice_context,
        "\n---\n",
        "# The angle — this is the thesis of the post",
        angle.strip(),
        "\n---\n",
        "# Source material (evidence for the angle, not the subject)",
        f"Title: {title}",
        f"URL: {url}",
    ]
    if why_it_matters:
        parts.append(f"Triage note: {why_it_matters}")
    if extract.ok and extract.text:
        parts.append(f"\nExtract:\n{extract.text}")
    else:
        parts.append(
            f"\nExtract: unavailable ({extract.note}).\n"
            "You have no article text. Write from the angle alone. Do not state "
            "any fact, figure, quotation, or detail about the source beyond its "
            "title, and do not imply you have read it."
        )
    parts += [
        "\n---\n",
        CONSTRAINTS.format(
            min_chars=int(config.get("drafting.min_chars", 900)),
            max_chars=int(config.get("drafting.max_chars", 1300)),
            hook_chars=int(config.get("drafting.hook_chars", 210)),
            max_hashtags=int(config.get("drafting.max_hashtags", 3)),
        ),
        "",
        VARIANTS_INSTRUCTION.format(a=VARIANT_A, b=VARIANT_B)
        if variants
        else SINGLE_INSTRUCTION,
    ]
    return "\n".join(parts)


def build_revision_prompt(
    config: Config,
    *,
    voice_context: str,
    angle: str,
    title: str,
    url: str,
    extract: Extract,
    previous_draft: str,
    note: str,
) -> str:
    return "\n".join(
        [
            "# Voice",
            voice_context,
            "\n---\n",
            "# The angle — still the thesis of the post",
            angle.strip(),
            "\n---\n",
            "# Source material",
            f"Title: {title}",
            f"URL: {url}",
            (f"\nExtract:\n{extract.text}" if extract.ok and extract.text else
             f"\nExtract: unavailable ({extract.note}). Invent nothing about the source."),
            "\n---\n",
            "# The current draft",
            previous_draft.strip(),
            "\n---\n",
            "# What to change",
            note.strip(),
            "",
            CONSTRAINTS.format(
                min_chars=int(config.get("drafting.min_chars", 900)),
                max_chars=int(config.get("drafting.max_chars", 1300)),
                hook_chars=int(config.get("drafting.hook_chars", 210)),
                max_hashtags=int(config.get("drafting.max_hashtags", 3)),
            ),
            "",
            "Return one post: the current draft with that change made.",
        ]
    )


# --------------------------------------------------------------------------
# Post-processing
# --------------------------------------------------------------------------


def postprocess(text: str) -> str:
    """Strip the tells: fences, preambles, em-dashes, smart quotes.

    The em-dash rule is in the voice card, but models reach for them anyway, so
    it is enforced here as well as asked for there.
    """
    out = (text or "").strip()
    out = re.sub(r"^```[a-zA-Z]*\s*\n?", "", out)
    out = re.sub(r"\n?```\s*$", "", out).strip()
    out = PREAMBLE.sub("", out).strip()
    out = re.sub(r"(\d)\s*[—–]\s*(\d)", r"\1-\2", out)  # ranges keep a hyphen
    out = re.sub(r"\s*[—–]\s*", ", ", out)
    out = re.sub(r",\s*,", ",", out)
    out = re.sub(r"\s+,", ",", out)
    out = re.sub(r",\s*\.", ".", out)
    out = re.sub(r",[ \t]{2,}", ", ", out)
    out = re.sub(r"[ \t]+\n", "\n", out)
    out = re.sub(r"\n{3,}", "\n\n", out)
    out = re.sub(r'^["“](.*)["”]$', r"\1", out, flags=re.DOTALL)
    return out.strip()


def contains_both_variants(text: str) -> bool:
    """True when a row still holds both drafts.

    An approved row in this state is ambiguous — nobody can say which post the
    human meant — so the publish job refuses it rather than guessing.
    """
    body = text or ""
    return VARIANT_A in body and VARIANT_B in body


def split_variants(text: str) -> Tuple[str, Optional[str]]:
    """Split a two-variant draft into its parts, for display and counting."""
    if not contains_both_variants(text):
        return text, None
    _, _, rest = text.partition(VARIANT_A)
    first, _, second = rest.partition(VARIANT_B)
    return first.strip(), second.strip()


def char_count(text: str) -> int:
    """Characters the human is judging: the longest variant, not the wrapper."""
    first, second = split_variants(text)
    return max(len(first), len(second or ""))


def check_constraints(config: Config, text: str) -> list[str]:
    """Report constraint violations. Advisory: the human still decides."""
    problems = []
    min_chars = int(config.get("drafting.min_chars", 900))
    max_chars = int(config.get("drafting.max_chars", 1300))
    max_hashtags = int(config.get("drafting.max_hashtags", 3))
    for label, body in (("A", split_variants(text)[0]), ("B", split_variants(text)[1])):
        if body is None:
            continue
        if not (min_chars <= len(body) <= max_chars):
            problems.append(f"variant {label}: {len(body)} chars, want {min_chars}-{max_chars}")
        if body.count("#") > max_hashtags:
            problems.append(f"variant {label}: more than {max_hashtags} hashtags")
        if body.rstrip().endswith("?"):
            problems.append(f"variant {label}: closes on a question")
        if "—" in body:
            problems.append(f"variant {label}: contains an em-dash")
    return problems


# --------------------------------------------------------------------------
# Entry points
# --------------------------------------------------------------------------


def draft(
    config: Config,
    row: Row,
    voice_context: str,
    *,
    variants: bool,
    extract: Optional[Extract] = None,
    completer=None,
) -> str:
    completer = completer or complete
    extract = extract if extract is not None else fetch_extract(config, row.SourceURL)
    prompt = build_draft_prompt(
        config,
        voice_context=voice_context,
        angle=row.angle,
        title=row.SourceTitle,
        url=row.SourceURL,
        why_it_matters=row.WhyItMatters,
        extract=extract,
        variants=variants,
    )
    raw = completer(
        config,
        system=DRAFT_SYSTEM,
        user=prompt,
        model=config.get("drafting.model", "claude-sonnet-4-6"),
        max_tokens=int(config.get("drafting.max_tokens", 4000)),
        temperature=config.get("drafting.temperature"),
    )
    if variants and contains_both_variants(raw):
        first, second = split_variants(raw)
        return f"{VARIANT_A}\n{postprocess(first)}\n\n{VARIANT_B}\n{postprocess(second)}"
    return postprocess(raw)


def revise(
    config: Config,
    row: Row,
    voice_context: str,
    *,
    extract: Optional[Extract] = None,
    completer=None,
) -> str:
    completer = completer or complete
    extract = extract if extract is not None else fetch_extract(config, row.SourceURL)
    prompt = build_revision_prompt(
        config,
        voice_context=voice_context,
        angle=row.angle,
        title=row.SourceTitle,
        url=row.SourceURL,
        extract=extract,
        previous_draft=row.DraftText,
        note=row.revision_note,
    )
    raw = completer(
        config,
        system=REVISION_SYSTEM,
        user=prompt,
        model=config.get("drafting.model", "claude-sonnet-4-6"),
        max_tokens=int(config.get("drafting.max_tokens", 4000)),
        temperature=config.get("drafting.temperature"),
    )
    return postprocess(raw)
