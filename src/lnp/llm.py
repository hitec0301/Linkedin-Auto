"""Anthropic API access.

One place to build the client, retry sensibly, and get JSON back out of a model
that was asked for JSON. Nothing here knows about posts or feeds.
"""

from __future__ import annotations

import json
import random
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Protocol

import anthropic

from . import log
from .config import Config, require_env

logger = log.get(__name__)


class LLMError(Exception):
    pass


class UsageCapExceeded(LLMError):
    """The tenant has spent their allowance for the period.

    A hard stop rather than a warning: the operator pays for inference, and a
    cap that only warns is not a cap.
    """


@dataclass
class Usage:
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0


class Meter(Protocol):
    """Somewhere to check an allowance and record what a call cost.

    A module-level hook rather than an argument threaded through every caller:
    metering has to cover *every* model call, and an argument is something a
    new call site can be written without.
    """

    def check(self) -> None:
        """Raise UsageCapExceeded if this tenant has no allowance left."""

    def record(self, usage: Usage) -> None:
        ...


_meter: Optional[Meter] = None


def set_meter(meter: Optional[Meter]) -> None:
    """Install the meter for this run. Cleared between accounts."""
    global _meter
    _meter = meter


def current_meter() -> Optional[Meter]:
    return _meter


def client() -> anthropic.Anthropic:
    return anthropic.Anthropic(api_key=require_env("ANTHROPIC_API_KEY"))


def complete(
    config: Config,
    *,
    system: str,
    user: str,
    model: Optional[str] = None,
    max_tokens: int = 4000,
    temperature: Optional[float] = None,
    max_retries: int = 4,
    api: Optional[anthropic.Anthropic] = None,
) -> str:
    """One completion, returning concatenated text blocks.

    Retries rate limits and server errors; a 4xx that is not a rate limit is a
    bug in our request and is raised immediately rather than hammered.
    """
    api = api or client()
    model = model or config.get("drafting.model", "claude-sonnet-4-6")
    kwargs: Dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": user}],
    }
    if temperature is not None:
        kwargs["temperature"] = temperature

    if _meter is not None:
        _meter.check()

    last: Optional[Exception] = None
    for attempt in range(max_retries):
        try:
            response = api.messages.create(**kwargs)
            if _meter is not None:
                # Recorded before the response is validated: a refusal or an
                # empty completion still cost tokens, and an allowance that
                # only counts useful calls is not the one being paid for.
                _meter.record(
                    Usage(
                        model=model,
                        input_tokens=response.usage.input_tokens,
                        output_tokens=response.usage.output_tokens,
                    )
                )
            if response.stop_reason == "refusal":
                raise LLMError(
                    f"model declined the request "
                    f"({getattr(response.stop_details, 'category', 'unknown')})"
                )
            text = "".join(
                block.text for block in response.content if block.type == "text"
            ).strip()
            if not text:
                raise LLMError(f"empty completion (stop_reason={response.stop_reason})")
            logger.info(
                "llm call",
                extra={
                    "model": model,
                    "input_tokens": response.usage.input_tokens,
                    "output_tokens": response.usage.output_tokens,
                    "stop_reason": response.stop_reason,
                },
            )
            return text
        except anthropic.RateLimitError as exc:
            last = exc
        except anthropic.APIStatusError as exc:
            if exc.status_code < 500:
                raise
            last = exc
        except anthropic.APIConnectionError as exc:
            last = exc
        delay = min(2**attempt + random.uniform(0, 1), 30)
        logger.warning(
            "llm retry", extra={"attempt": attempt + 1, "delay": round(delay, 1), "error": str(last)}
        )
        time.sleep(delay)
    raise LLMError(f"completion failed after {max_retries} attempts: {last}")


_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def extract_json(text: str) -> Any:
    """Parse JSON out of a completion, tolerating fences and stray prose."""
    cleaned = _FENCE.sub("", text or "").strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    for opener, closer in (("[", "]"), ("{", "}")):
        start = cleaned.find(opener)
        end = cleaned.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(cleaned[start : end + 1])
            except json.JSONDecodeError:
                continue
    raise LLMError(f"could not parse JSON from model output: {cleaned[:400]}")


def complete_json(
    config: Config,
    *,
    system: str,
    user: str,
    model: Optional[str] = None,
    max_tokens: int = 4000,
    api: Optional[anthropic.Anthropic] = None,
) -> Any:
    text = complete(
        config, system=system, user=user, model=model, max_tokens=max_tokens, api=api
    )
    return extract_json(text)


def web_search(config: Config, query: str, api: Optional[anthropic.Anthropic] = None) -> List[Dict[str, str]]:
    """Server-side web search. Only reachable when ingest.sources.web_search is on."""
    api = api or client()
    if _meter is not None:
        _meter.check()
    response = api.messages.create(
        model=config.get("scoring.model", "claude-sonnet-4-6"),
        max_tokens=4000,
        tools=[
            {
                "type": "web_search_20260209",
                "name": "web_search",
                "max_uses": int(config.get("ingest.web_search.max_uses", 3)),
            }
        ],
        messages=[
            {
                "role": "user",
                "content": (
                    f"Search for recent items about: {query}. "
                    "Report only what the search returns."
                ),
            }
        ],
    )
    if _meter is not None:
        _meter.record(
            Usage(
                model=str(getattr(response, "model", "")),
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
            )
        )

    results: List[Dict[str, str]] = []
    for block in response.content:
        if block.type != "web_search_tool_result":
            continue
        content = block.content
        if not isinstance(content, list):  # an error object, not a result list
            logger.error("web search error", extra={"detail": str(content)})
            continue
        for hit in content:
            results.append(
                {
                    "url": getattr(hit, "url", "") or "",
                    "title": getattr(hit, "title", "") or "",
                    "snippet": (getattr(hit, "encrypted_content", "") or "")[:400],
                    "published": getattr(hit, "page_age", "") or "",
                }
            )
    return results
