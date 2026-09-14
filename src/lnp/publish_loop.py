"""The one process left that runs on its own clock.

Every other job in this pipeline moved on-demand: a person is looking at the
screen when a batch is fetched, a row is drafted, or a rule is accepted.
Publishing a *scheduled* post is the one exception - the whole point of
"schedule for later" is that it still happens after the person closes the
tab - so it is the one thing still watched on a timer. It runs here, as a
thread inside the web process, instead of as a separate Railway cron
service: there is nothing left to schedule but this, so there is no reason
to keep a second deployable around for it.

A plain Python thread, not an asyncio task: the rest of this codebase
(SQLAlchemy, requests) blocks, and an async task built on blocking calls
would starve FastAPI's event loop exactly when the checker had the most
work to do. The actual publishing logic is unchanged - it is the same
`jobs.publish` module the old cron service ran - only the thing calling it
on a schedule has moved.
"""

from __future__ import annotations

import threading
from typing import Optional

from . import log, runner
from .config import Config

logger = log.get("publish_loop")

DEFAULT_INTERVAL_SECONDS = 60
JOB = "publish"


class _Args:
    """jobs.publish.publish() only reads .limit off its args namespace."""

    limit: Optional[int] = None


def _tick(config: Config, lock: threading.Lock) -> None:
    from jobs import publish as publish_job

    for run in runner.runs(JOB, config):
        with runner.isolated(run, JOB):
            with lock:
                publish_job.publish(run, _Args(), config.dry_run)


def start(
    config: Config,
    lock: threading.Lock,
    *,
    interval: Optional[float] = None,
    stop_event: Optional[threading.Event] = None,
) -> threading.Thread:
    """Start the checker in a daemon thread and return it, already running.

    `lock` is shared with the "Post now" route so a manual publish and the
    checker's own sweep can never land on the same tenant at once. `stop_event`
    lets a test (or a graceful shutdown, one day) end the loop without waiting
    out a full interval.
    """
    interval = interval or float(
        config.get("publish_checker.interval_seconds", DEFAULT_INTERVAL_SECONDS)
    )
    stop_event = stop_event or threading.Event()

    def loop() -> None:
        logger.info("publish checker started", extra={"interval_seconds": interval})
        while not stop_event.is_set():
            try:
                _tick(config, lock)
            except Exception:  # noqa: BLE001 - a tick failing must not end the checker
                logger.exception("publish checker tick failed")
            stop_event.wait(interval)

    thread = threading.Thread(target=loop, name="publish-checker", daemon=True)
    thread.start()
    return thread
