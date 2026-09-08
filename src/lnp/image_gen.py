"""An image for an approved post, generated on request.

Two calls, not one: the post's own text makes a poor image prompt fed
straight into an image model - it is full of jargon, numbers, and LinkedIn
post structure, none of which an image model should try to draw. So this
first asks Claude for a short visual brief describing what the post is
*about*, then hands that brief to Gemini's image model. Nothing here writes
to a row except `generate_image_now`, which mirrors `redraft.redraft_now`:
one call, in, generates, writes, done. Regenerating always overwrites
whatever image was there before - there is no history of earlier attempts,
same as a fresh draft.
"""

from __future__ import annotations

import base64
from typing import Optional

import requests

from . import llm, log, runner
from .config import Config, require_env
from .models import Row

logger = log.get("image_gen")

GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"

IMAGE_PROMPT_SYSTEM = """You write short, concrete image-generation prompts for \
LinkedIn posts about workforce learning and corporate/academic training.

Rules:
- One to three sentences, a plain description of a scene or composition.
- No embedded words, captions, numbers, logos, or text of any kind in the image.
- No named real people, companies, or brands.
- Editorial photography or clean flat-illustration style - professional, not \
stock-photo cliche (no handshakes, no lightbulbs, no puzzle pieces, no
people pointing at charts).
- Ground it in the post's actual subject, not generic "business" imagery.

Return only the prompt text, nothing else - no preamble, no quotes."""


class ImageGenError(Exception):
    pass


def build_prompt(config: Config, row: Row) -> str:
    """Ask Claude for a short visual brief describing what this post is about."""
    text = (row.effective_text or row.DraftText or "").strip()
    if not text:
        raise ImageGenError("write or draft the post text first")
    user = (
        f"Post text:\n{text}\n\n"
        f"What it's about: {row.WhyItMatters or row.SourceTitle or 'not given'}\n\n"
        "Write the image prompt."
    )
    prompt = llm.complete(
        config, system=IMAGE_PROMPT_SYSTEM, user=user,
        model=config.get("imaging.prompt_model", config.get("drafting.model", "claude-sonnet-4-6")),
        max_tokens=300,
    ).strip()
    if not prompt:
        raise ImageGenError("the model returned an empty image prompt")
    return prompt


def generate(
    config: Config, prompt: str, *, session: Optional[requests.Session] = None
) -> bytes:
    """Call Gemini's image model and return raw image bytes (PNG)."""
    session = session or requests.Session()
    model = config.get("imaging.model", "gemini-2.5-flash-image")
    api_key = require_env("GEMINI_API_KEY")
    url = f"{GEMINI_BASE}/models/{model}:generateContent"
    timeout = int(config.get("imaging.request_timeout_seconds", 60))
    try:
        response = session.post(
            url,
            params={"key": api_key},
            json={"contents": [{"parts": [{"text": prompt}]}]},
            timeout=timeout,
        )
    except requests.RequestException as exc:
        raise ImageGenError(f"could not reach the image service: {exc}") from exc

    if response.status_code >= 400:
        raise ImageGenError(
            f"image generation failed ({response.status_code}): {response.text[:800]}"
        )

    body = response.json()
    candidates = body.get("candidates") or []
    if not candidates:
        feedback = body.get("promptFeedback", {})
        raise ImageGenError(f"no image returned: {feedback or body}")

    finish_reason = candidates[0].get("finishReason", "")
    parts = (candidates[0].get("content") or {}).get("parts", [])
    for part in parts:
        inline = part.get("inlineData")
        if inline and inline.get("data"):
            return base64.b64decode(inline["data"])

    raise ImageGenError(
        f"the model did not return an image (finishReason={finish_reason or 'unknown'})"
    )


def generate_image_now(
    run: "runner.Run", row: Row, *, session: Optional[requests.Session] = None
) -> Row:
    """Generate an image for `row`'s post and store it as a fresh draft.

    Overwrites whatever ImagePrompt/ImageData this row already had - like
    "Redraft with AI", there is no history kept of earlier attempts.
    """
    config, store = run.config, run.store
    prompt = build_prompt(config, row)
    image_bytes = generate(config, prompt, session=session)
    store.write(row, {
        "ImagePrompt": prompt,
        "ImageData": base64.b64encode(image_bytes).decode("ascii"),
    })
    logger.info(
        "generated image", extra={"row_id": row.ID, "bytes": len(image_bytes)}
    )
    return row
