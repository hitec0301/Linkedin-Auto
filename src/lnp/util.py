"""Small dependency-free helpers.

This module must not import any other module in the package: models.py depends
on it, so anything imported here becomes a dependency of the whole pipeline.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from dateutil import parser as date_parser

# Query parameters that identify a campaign rather than a document. Two links
# that differ only in these point at the same article.
TRACKING_PARAMS = {
    "fbclid",
    "gclid",
    "igshid",
    "mc_cid",
    "mc_eid",
    "msclkid",
    "ref",
    "ref_src",
    "s",
    "src",
    "spm",
}
TRACKING_PREFIXES = ("utm_",)


def utcnow() -> datetime:
    """Timezone-aware current time. Never use naive datetimes in this codebase."""
    return datetime.now(timezone.utc)


def iso(dt: Optional[datetime] = None) -> str:
    """Render a datetime as a UTC ISO-8601 string with a trailing Z."""
    dt = dt or utcnow()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_dt(value: Optional[str]) -> Optional[datetime]:
    """Parse a stored timestamp. Returns None for blank or unparseable.

    Naive values are assumed UTC. Some of these values were typed by a person,
    so unparseable input is expected and must not raise.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        dt = date_parser.parse(text)
    except (ValueError, OverflowError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def normalize_url(url: str) -> str:
    """Canonical form of a URL for dedupe.

    Lowercases the host, drops the leading www., strips a trailing slash, drops
    tracking parameters, and discards the fragment. The path keeps its case:
    many CMSes serve case-sensitive slugs.
    """
    if not url:
        return ""
    parts = urlsplit(url.strip())
    scheme = (parts.scheme or "https").lower()
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if parts.port and parts.port not in (80, 443):
        host = f"{host}:{parts.port}"
    path = parts.path.rstrip("/")
    kept = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k.lower() not in TRACKING_PARAMS
        and not any(k.lower().startswith(p) for p in TRACKING_PREFIXES)
    ]
    query = urlencode(sorted(kept))
    return urlunsplit((scheme, host, path, query, ""))


def normalize_title(title: str) -> str:
    """Lowercased, punctuation-free title for fuzzy comparison."""
    text = (title or "").lower()
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def levenshtein(a: str, b: str) -> int:
    """Edit distance between two strings, iterative two-row implementation."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        current = [i]
        for j, cb in enumerate(b, start=1):
            current.append(
                min(
                    previous[j] + 1,  # deletion
                    current[j - 1] + 1,  # insertion
                    previous[j - 1] + (ca != cb),  # substitution
                )
            )
        previous = current
    return previous[-1]


def normalized_edit_distance(a: str, b: str) -> float:
    """Levenshtein distance scaled to 0.0-1.0 by the longer string.

    0.0 means the human published the draft untouched; values near 1.0 mean
    they rewrote it. This is the health metric for the whole pipeline.
    """
    a = a or ""
    b = b or ""
    if not a and not b:
        return 0.0
    return round(levenshtein(a, b) / max(len(a), len(b)), 4)


def parse_bool(value) -> bool:
    """Read a checkbox or a hand-typed truthy string."""
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"true", "yes", "y", "1", "x", "✓"}


def parse_int(value, default: int = 0) -> int:
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default


def parse_float(value, default: float = 0.0) -> float:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


def truncate(text: str, limit: int) -> str:
    text = text or ""
    return text if len(text) <= limit else text[:limit]
