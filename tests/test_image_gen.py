"""Tests for lnp.image_gen: the visual-brief step, the Gemini call, and the
one function (`generate_image_now`) that writes a row.

No network. `llm.complete` and the HTTP session are faked at the seams the
route itself uses, the same way the LinkedIn client's calls are faked
elsewhere in this suite.
"""

from __future__ import annotations

import base64
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lnp import image_gen, llm
from lnp.config import Config
from lnp.models import Row


def test_build_prompt_asks_claude_for_a_visual_brief(monkeypatch):
    seen = {}

    def fake_complete(config, *, system, user, **kw):
        seen["system"] = system
        seen["user"] = user
        return "A quiet training room, empty chairs, morning light."

    monkeypatch.setattr(llm, "complete", fake_complete)
    row = Row(DraftText="Districts are buying AI seats faster than staff.",
              WhyItMatters="Procurement is outrunning support.")
    prompt = image_gen.build_prompt(Config({}), row)
    assert prompt == "A quiet training room, empty chairs, morning light."
    assert "Districts are buying AI seats" in seen["user"]
    assert "no text of any kind" in seen["system"].lower() or "text of any kind" in seen["system"]


def test_build_prompt_refuses_a_row_with_no_text():
    with pytest.raises(image_gen.ImageGenError):
        image_gen.build_prompt(Config({}), Row())


def test_build_prompt_refuses_an_empty_completion(monkeypatch):
    monkeypatch.setattr(llm, "complete", lambda *a, **kw: "   ")
    with pytest.raises(image_gen.ImageGenError):
        image_gen.build_prompt(Config({}), Row(DraftText="something"))


class FakeImageResponse:
    def __init__(self, status_code=200, json_body=None, text=""):
        self.status_code = status_code
        self._json = json_body or {}
        self.text = text

    def json(self):
        return self._json


class FakeSession:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def post(self, url, **kw):
        self.calls.append((url, kw))
        return self.response


def _ok_body(data="aGVsbG8="):  # "hello"
    return {
        "candidates": [
            {
                "finishReason": "STOP",
                "content": {"parts": [{"inlineData": {"mimeType": "image/png", "data": data}}]},
            }
        ]
    }


def test_generate_returns_decoded_image_bytes(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    session = FakeSession(FakeImageResponse(200, _ok_body()))
    image_bytes = image_gen.generate(Config({}), "a prompt", session=session)
    assert image_bytes == base64.b64decode("aGVsbG8=")
    url, kwargs = session.calls[0]
    assert "generateContent" in url
    assert kwargs["params"]["key"] == "test-key"


def test_generate_requires_the_api_key(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    session = FakeSession(FakeImageResponse(200, _ok_body()))
    with pytest.raises(Exception):
        image_gen.generate(Config({}), "a prompt", session=session)


def test_generate_raises_on_an_http_error(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    session = FakeSession(FakeImageResponse(500, {}, text="server exploded"))
    with pytest.raises(image_gen.ImageGenError, match="500"):
        image_gen.generate(Config({}), "a prompt", session=session)


def test_generate_raises_when_no_candidates_come_back(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    session = FakeSession(FakeImageResponse(200, {"promptFeedback": {"blockReason": "SAFETY"}}))
    with pytest.raises(image_gen.ImageGenError, match="SAFETY"):
        image_gen.generate(Config({}), "a prompt", session=session)


def test_generate_raises_when_a_candidate_has_no_image(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    body = {"candidates": [{"finishReason": "IMAGE_SAFETY", "content": {"parts": [{"text": "no image, sorry"}]}}]}
    session = FakeSession(FakeImageResponse(200, body))
    with pytest.raises(image_gen.ImageGenError, match="IMAGE_SAFETY"):
        image_gen.generate(Config({}), "a prompt", session=session)
