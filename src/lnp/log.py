"""Structured JSON logging to stdout.

Every line is one JSON object. GitHub Actions keeps stdout for 90 days, which
makes these logs the pipeline's only durable audit trail — so log the decision,
not just the action: why a row was skipped matters more than that it was.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from typing import Any

_CONFIGURED = False

# Attributes LogRecord always carries; anything else was passed by us as extra.
_RESERVED = set(
    logging.LogRecord("", 0, "", 0, "", (), None).__dict__
) | {"message", "asctime", "taskName"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%SZ"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = _safe(value)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def _safe(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool, type(None))):
        return value
    if isinstance(value, (list, tuple)):
        return [_safe(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _safe(v) for k, v in value.items()}
    return str(value)


def setup(level: str | None = None) -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel((level or os.environ.get("LOG_LEVEL") or "INFO").upper())
    # These libraries narrate every HTTP call at INFO; we log what matters.
    for noisy in ("urllib3", "google", "googleapiclient", "httpx", "anthropic"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    _CONFIGURED = True


def get(name: str) -> logging.Logger:
    setup()
    return logging.getLogger(name)
