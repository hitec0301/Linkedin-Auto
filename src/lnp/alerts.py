"""Alerting: Slack webhook first, SMTP fallback, always a structured log line.

A silent pipeline is worse than no pipeline, because it looks like it is
working. Every job wraps itself in `job_guard`, so a crash reaches a human even
when the crash is in the alerting path's neighbour.
"""

from __future__ import annotations

import smtplib
import traceback
from contextlib import contextmanager
from email.message import EmailMessage
from typing import Optional

import requests

from . import log
from .config import Config, env

logger = log.get(__name__)

SEVERITY_ICON = {"info": "•", "warn": "!", "error": "✕"}


def alert(
    config: Optional[Config],
    title: str,
    message: str = "",
    *,
    severity: str = "error",
    job: str = "",
) -> None:
    """Send an alert on every configured channel. Never raises.

    An exception escaping the alerter would mask the failure it was reporting,
    so delivery failures are logged and swallowed here — and only here.
    """
    logger.log(
        {"info": 20, "warn": 30, "error": 40}.get(severity, 40),
        title,
        extra={"alert": True, "severity": severity, "job": job, "detail": message},
    )

    text = f"{SEVERITY_ICON.get(severity, '•')} *{title}*"
    if job:
        text += f"  _(job: {job})_"
    if message:
        text += f"\n```{message[:2800]}```"

    delivered = False
    if config is None or config.get("alerts.slack", True):
        delivered = _slack(text) or delivered
    if config is not None and config.get("alerts.email", False):
        delivered = _email(config, title, message, job) or delivered
    if not delivered:
        logger.warning(
            "alert not delivered to any channel; log line is the only record",
            extra={"title": title},
        )


def _slack(text: str) -> bool:
    url = env("SLACK_WEBHOOK_URL")
    if not url:
        return False
    try:
        response = requests.post(url, json={"text": text}, timeout=15)
        response.raise_for_status()
        return True
    except Exception as exc:  # noqa: BLE001 - alerting must not raise
        logger.error("slack alert failed", extra={"error": str(exc)})
        return False


def _email(config: Config, title: str, body: str, job: str) -> bool:
    host = config.get("alerts.smtp.host") or env("SMTP_HOST")
    to_addr = config.get("alerts.smtp.to_addr") or env("SMTP_TO")
    from_addr = config.get("alerts.smtp.from_addr") or env("SMTP_FROM") or to_addr
    if not host or not to_addr:
        return False
    message = EmailMessage()
    message["Subject"] = f"[lnp] {title}"
    message["From"] = from_addr
    message["To"] = to_addr
    message.set_content(f"{title}\n\njob: {job or 'n/a'}\n\n{body}")
    try:
        port = int(config.get("alerts.smtp.port", 587))
        with smtplib.SMTP(host, port, timeout=20) as server:
            if config.get("alerts.smtp.use_tls", True):
                server.starttls()
            user, password = env("SMTP_USER"), env("SMTP_PASSWORD")
            if user and password:
                server.login(user, password)
            server.send_message(message)
        return True
    except Exception as exc:  # noqa: BLE001 - alerting must not raise
        logger.error("smtp alert failed", extra={"error": str(exc)})
        return False


@contextmanager
def job_guard(job_name: str, config: Optional[Config] = None):
    """Wrap a job so any crash alerts a human and still exits non-zero."""
    logger.info("job started", extra={"job": job_name})
    try:
        yield
    except Exception as exc:  # noqa: BLE001 - re-raised after alerting
        alert(
            config,
            f"{job_name} crashed: {type(exc).__name__}: {exc}",
            traceback.format_exc(),
            severity="error",
            job=job_name,
        )
        raise
    logger.info("job finished", extra={"job": job_name})
