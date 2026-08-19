"""Feed ingestion and dedupe.

Two things matter here. First, a dead feed must be loud: a feed returning zero
entries looks exactly like a quiet news week, and a pipeline that quietly
narrows to three sources is worse than one that stops. Second, dedupe has to
catch the same story told by four outlets, which is the normal case in this
space, not the exception — hence a fuzzy title pass on top of URL matching.
"""

from __future__ import annotations

import email
import imaplib
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import feedparser
import requests
from rapidfuzz import fuzz

from . import log
from .config import Config, env
from .util import normalize_title, normalize_url, parse_dt, utcnow

logger = log.get(__name__)


class IngestError(Exception):
    pass


@dataclass
class Candidate:
    """One item from a feed, before any LLM has seen it."""

    url: str
    title: str
    summary: str = ""
    published: Optional[datetime] = None
    source_name: str = ""
    tier: int = 2
    weight: float = 1.0
    origin: str = "rss"

    @property
    def normalized_url(self) -> str:
        return normalize_url(self.url)

    @property
    def normalized_title(self) -> str:
        return normalize_title(self.title)

    def as_dict(self) -> Dict[str, object]:
        return {
            "url": self.url,
            "title": self.title,
            "summary": self.summary[:600],
            "source": self.source_name,
            "tier": self.tier,
            "published": self.published.isoformat() if self.published else "",
        }


@dataclass
class FeedResult:
    name: str
    url: str
    tier: int
    ok: bool
    count: int
    error: str = ""
    candidates: List[Candidate] = field(default_factory=list)


# --------------------------------------------------------------------------
# Dedupe
# --------------------------------------------------------------------------


class Deduper:
    """Two-stage dedupe: normalized URL, then fuzzy title.

    Both stages are required. URL matching alone misses the same wire story on
    four domains; title matching alone misses a syndicated repost that changed
    its headline capitalisation and picked up a utm tag.
    """

    def __init__(
        self,
        threshold: int = 85,
        history_urls: Optional[Iterable[str]] = None,
        history_titles: Optional[Iterable[str]] = None,
    ):
        self.threshold = threshold
        self.seen_urls = {normalize_url(u) for u in (history_urls or []) if u}
        self.seen_titles = [
            normalize_title(t) for t in (history_titles or []) if t and t.strip()
        ]
        self.dropped_url = 0
        self.dropped_title = 0

    def is_duplicate(self, candidate: Candidate) -> Tuple[bool, str]:
        url = candidate.normalized_url
        if url and url in self.seen_urls:
            self.dropped_url += 1
            return True, "url"
        title = candidate.normalized_title
        if title:
            for seen in self.seen_titles:
                if fuzz.token_set_ratio(title, seen) >= self.threshold:
                    self.dropped_title += 1
                    return True, "title"
        return False, ""

    def add(self, candidate: Candidate) -> None:
        if candidate.normalized_url:
            self.seen_urls.add(candidate.normalized_url)
        if candidate.normalized_title:
            self.seen_titles.append(candidate.normalized_title)

    def keep(self, candidates: Iterable[Candidate]) -> List[Candidate]:
        """Filter a stream, remembering everything kept so this run self-dedupes."""
        kept: List[Candidate] = []
        for candidate in candidates:
            duplicate, reason = self.is_duplicate(candidate)
            if duplicate:
                logger.info(
                    "dropped duplicate",
                    extra={"reason": reason, "title": candidate.title[:120], "url": candidate.url},
                )
                continue
            self.add(candidate)
            kept.append(candidate)
        return kept


# --------------------------------------------------------------------------
# Feed fetching
# --------------------------------------------------------------------------


def _entry_datetime(entry) -> Optional[datetime]:
    for key in ("published_parsed", "updated_parsed"):
        value = getattr(entry, key, None) or entry.get(key) if hasattr(entry, "get") else None
        if value:
            try:
                return datetime(*value[:6], tzinfo=timezone.utc)
            except (TypeError, ValueError):
                pass
    for key in ("published", "updated", "created"):
        parsed = parse_dt(entry.get(key) if hasattr(entry, "get") else None)
        if parsed:
            return parsed
    return None


def _entry_summary(entry) -> str:
    for key in ("summary", "description"):
        value = entry.get(key) if hasattr(entry, "get") else None
        if value:
            return re.sub(r"<[^>]+>", " ", str(value)).strip()
    content = entry.get("content") if hasattr(entry, "get") else None
    if content:
        try:
            return re.sub(r"<[^>]+>", " ", str(content[0].get("value", ""))).strip()
        except (AttributeError, IndexError, KeyError):
            pass
    return ""


def fetch_feed(feed: Dict, timeout: int = 20, user_agent: str = "lnp-pipeline/1.0") -> FeedResult:
    """Fetch and parse one RSS/Atom feed.

    Network and parse failures come back as `ok=False` rather than raising, so
    one dead feed cannot take down a whole curate run — but the caller is
    expected to alert on them, not swallow them.
    """
    name = feed.get("name", feed.get("url", "?"))
    url = feed.get("url", "")
    tier = int(feed.get("tier", 2))
    weight = float(feed.get("weight", 1.0))
    if not url:
        return FeedResult(name, url, tier, False, 0, "no url configured")

    try:
        response = requests.get(
            url,
            timeout=timeout,
            headers={"User-Agent": user_agent, "Accept": "application/rss+xml, application/xml, text/xml, */*"},
        )
        response.raise_for_status()
        parsed = feedparser.parse(response.content)
    except requests.RequestException as exc:
        return FeedResult(name, url, tier, False, 0, f"{type(exc).__name__}: {exc}")

    entries = list(parsed.entries or [])
    if not entries:
        reason = "feed returned no entries"
        if getattr(parsed, "bozo", 0):
            reason += f" ({getattr(parsed, 'bozo_exception', '')})"
        return FeedResult(name, url, tier, False, 0, reason)

    candidates = []
    for entry in entries:
        link = (entry.get("link") or "").strip()
        title = (entry.get("title") or "").strip()
        if not link or not title:
            continue
        candidates.append(
            Candidate(
                url=link,
                title=title,
                summary=_entry_summary(entry),
                published=_entry_datetime(entry),
                source_name=name,
                tier=tier,
                weight=weight,
                origin="rss",
            )
        )
    return FeedResult(name, url, tier, True, len(candidates), "", candidates)


def matches_education_terms(candidate: Candidate, terms: Sequence[str]) -> bool:
    """arXiv is mostly not about education. Keep only what is."""
    haystack = f"{candidate.title} {candidate.summary}".lower()
    return any(term.lower() in haystack for term in terms)


def within_lookback(candidate: Candidate, days: int, now: Optional[datetime] = None) -> bool:
    """Undated items are kept: several tier-2 blogs omit dates entirely."""
    if candidate.published is None:
        return True
    return candidate.published >= (now or utcnow()) - timedelta(days=days)


# --------------------------------------------------------------------------
# Optional sources, both off by default
# --------------------------------------------------------------------------


def fetch_gmail_label(config: Config) -> List[Candidate]:
    """Read a Gmail label over IMAP for newsletter-only sources.

    Off by default. Needs GMAIL_USER and GMAIL_APP_PASSWORD (an app password,
    not the account password) and `ingest.sources.gmail_label: true`.
    """
    user = env("GMAIL_USER")
    password = env("GMAIL_APP_PASSWORD")
    if not user or not password:
        raise IngestError(
            "gmail ingestion is enabled but GMAIL_USER / GMAIL_APP_PASSWORD are unset"
        )
    label = config.get("ingest.gmail.label", "LNP-Sources")
    limit = int(config.get("ingest.gmail.max_messages", 50))
    # Resolved here rather than as a default argument so callers and tests can
    # substitute the fetcher.
    fetcher = fetcher or fetch_feed
    lookback = int(config.get("ingest.lookback_days", 7))
    since = (utcnow() - timedelta(days=lookback)).strftime("%d-%b-%Y")

    candidates: List[Candidate] = []
    imap = imaplib.IMAP4_SSL("imap.gmail.com")
    try:
        imap.login(user, password)
        imap.select(f'"{label}"', readonly=True)
        _, data = imap.search(None, f'(SINCE "{since}")')
        ids = (data[0].split() if data and data[0] else [])[-limit:]
        for msg_id in ids:
            _, payload = imap.fetch(msg_id, "(RFC822)")
            if not payload or not payload[0]:
                continue
            message = email.message_from_bytes(payload[0][1])
            subject = str(email.header.make_header(email.header.decode_header(message.get("Subject", ""))))
            body = _email_body(message)
            link = _first_link(body)
            if not subject:
                continue
            candidates.append(
                Candidate(
                    url=link or f"mailto:{message.get('From', 'unknown')}#{msg_id.decode()}",
                    title=subject.strip(),
                    summary=re.sub(r"\s+", " ", body)[:1500],
                    published=parse_dt(message.get("Date")),
                    source_name=f"gmail:{label}",
                    tier=2,
                    weight=1.0,
                    origin="gmail",
                )
            )
    finally:
        try:
            imap.logout()
        except Exception:  # pragma: no cover - best effort
            pass
    logger.info("gmail ingestion", extra={"label": label, "count": len(candidates)})
    return candidates


def _email_body(message) -> str:
    if message.is_multipart():
        for part in message.walk():
            if part.get_content_type() == "text/plain":
                try:
                    return part.get_payload(decode=True).decode(errors="replace")
                except (AttributeError, UnicodeDecodeError):
                    continue
        for part in message.walk():
            if part.get_content_type() == "text/html":
                try:
                    html = part.get_payload(decode=True).decode(errors="replace")
                    return re.sub(r"<[^>]+>", " ", html)
                except (AttributeError, UnicodeDecodeError):
                    continue
        return ""
    try:
        return message.get_payload(decode=True).decode(errors="replace")
    except (AttributeError, UnicodeDecodeError):
        return str(message.get_payload())


def _first_link(text: str) -> str:
    match = re.search(r"https?://[^\s>\)\]\"']+", text or "")
    return match.group(0) if match else ""


def fetch_web_search(config: Config) -> List[Candidate]:
    """Web search via the Anthropic server-side search tool. Off by default."""
    from .llm import web_search  # imported lazily; search is rarely enabled

    queries = config.get("ingest.web_search.queries") or []
    if not queries:
        raise IngestError("web search is enabled but ingest.web_search.queries is empty")
    results: List[Candidate] = []
    for query in queries:
        for hit in web_search(config, query):
            results.append(
                Candidate(
                    url=hit.get("url", ""),
                    title=hit.get("title", ""),
                    summary=hit.get("snippet", ""),
                    published=parse_dt(hit.get("published")),
                    source_name=f"search:{query}",
                    tier=int(config.get("ingest.web_search.tier", 2)),
                    weight=float(config.get("ingest.web_search.weight", 1.0)),
                    origin="search",
                )
            )
    return [c for c in results if c.url and c.title]


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------


def interleave(groups: Sequence[Sequence[Candidate]], limit: int) -> List[Candidate]:
    """Round-robin across feeds up to `limit`.

    Taking the first N in feed order would let one prolific publisher fill the
    cap before a weekly analyst post is ever considered.
    """
    out: List[Candidate] = []
    index = 0
    while len(out) < limit:
        added = False
        for group in groups:
            if index < len(group):
                out.append(group[index])
                added = True
                if len(out) >= limit:
                    return out
        if not added:
            break
        index += 1
    return out


def collect(
    config: Config,
    feeds: Sequence[Dict],
    history_urls: Optional[Iterable[str]] = None,
    history_titles: Optional[Iterable[str]] = None,
    fetcher=None,
) -> Tuple[List[Candidate], List[FeedResult]]:
    """Fetch every enabled feed, filter, dedupe, and cap.

    Returns the surviving candidates and the per-feed results, including
    failures — the caller alerts on those.
    """
    # Resolved here rather than as a default argument so callers and tests can
    # substitute the fetcher.
    fetcher = fetcher or fetch_feed
    lookback = int(config.get("ingest.lookback_days", 7))
    per_feed = int(config.get("ingest.max_items_per_feed", 40))
    max_items = int(config.get("ingest.max_items", 200))
    threshold = int(config.get("ingest.fuzzy_threshold", 85))
    terms = config.get("ingest.education_terms") or []
    timeout = int(config.get("ingest.request_timeout_seconds", 20))
    user_agent = config.get("drafting.user_agent", "lnp-pipeline/1.0")
    now = utcnow()

    results: List[FeedResult] = []
    groups: List[List[Candidate]] = []

    if config.get("ingest.sources.rss", True):
        for feed in feeds:
            if not feed.get("enabled", True) or feed.get("ingest", "rss") != "rss":
                continue
            result = fetcher(feed, timeout, user_agent)
            results.append(result)
            if not result.ok:
                logger.error(
                    "feed failed",
                    extra={"feed": result.name, "url": result.url, "error": result.error},
                )
                continue
            items = [c for c in result.candidates if within_lookback(c, lookback, now)]
            if feed.get("education_filter"):
                before = len(items)
                items = [c for c in items if matches_education_terms(c, terms)]
                logger.info(
                    "education filter applied",
                    extra={"feed": result.name, "before": before, "after": len(items)},
                )
            items.sort(key=lambda c: c.published or now, reverse=True)
            groups.append(items[:per_feed])
            logger.info(
                "feed ingested",
                extra={"feed": result.name, "tier": result.tier, "entries": result.count, "kept": len(items[:per_feed])},
            )

    if config.get("ingest.sources.gmail_label", False):
        try:
            items = [c for c in fetch_gmail_label(config) if within_lookback(c, lookback, now)]
            groups.append(items[:per_feed])
            results.append(FeedResult("gmail", "imap", 2, True, len(items)))
        except Exception as exc:  # noqa: BLE001 - reported, never silent
            logger.error("gmail ingestion failed", extra={"error": str(exc)})
            results.append(FeedResult("gmail", "imap", 2, False, 0, str(exc)))

    if config.get("ingest.sources.web_search", False):
        try:
            items = [c for c in fetch_web_search(config) if within_lookback(c, lookback, now)]
            groups.append(items[:per_feed])
            results.append(FeedResult("web_search", "anthropic", 2, True, len(items)))
        except Exception as exc:  # noqa: BLE001 - reported, never silent
            logger.error("web search failed", extra={"error": str(exc)})
            results.append(FeedResult("web_search", "anthropic", 2, False, 0, str(exc)))

    deduper = Deduper(threshold, history_urls, history_titles)
    # Dedupe before the cap so 200 items means 200 distinct stories.
    ordered = interleave(groups, max_items * 3)
    kept = deduper.keep(ordered)[:max_items]

    logger.info(
        "ingestion complete",
        extra={
            "feeds_ok": sum(1 for r in results if r.ok),
            "feeds_failed": sum(1 for r in results if not r.ok),
            "candidates": len(kept),
            "dropped_url": deduper.dropped_url,
            "dropped_title": deduper.dropped_title,
        },
    )
    return kept, results
