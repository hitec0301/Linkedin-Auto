"""Tests for the in-process publish checker - the one thing still on a clock.

Everything else moved on-demand; publishing a *scheduled* post is the one
job that still has to run when nobody is looking at the screen, so it is
the one job that still runs on a timer - now a thread inside the web
process instead of a separate Railway cron service. These tests cover the
loop's own behaviour (it ticks, it survives a failing tick, it serialises
against the same lock a manual "Post now" click uses); `jobs.publish`'s own
logic - due rows, stuck-row recovery, the dry-run path - is already covered
in tests/test_pipeline.py and is not re-tested here.
"""

from __future__ import annotations

import threading
import time
from contextlib import nullcontext

from lnp import publish_loop
from lnp.config import Config


def wait_for(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_start_ticks_immediately_and_on_each_interval(monkeypatch):
    ticks = []
    monkeypatch.setattr(publish_loop, "_tick", lambda config, lock: ticks.append(1))

    stop_event = threading.Event()
    thread = publish_loop.start(
        Config({}), threading.Lock(), interval=0.02, stop_event=stop_event
    )
    try:
        assert wait_for(lambda: len(ticks) >= 3)
    finally:
        stop_event.set()
        thread.join(timeout=1)
    assert not thread.is_alive()


def test_a_failing_tick_does_not_end_the_checker(monkeypatch):
    calls = {"n": 0}

    def flaky(config, lock):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")

    monkeypatch.setattr(publish_loop, "_tick", flaky)

    stop_event = threading.Event()
    thread = publish_loop.start(
        Config({}), threading.Lock(), interval=0.02, stop_event=stop_event
    )
    try:
        assert wait_for(lambda: calls["n"] >= 2)
    finally:
        stop_event.set()
        thread.join(timeout=1)


def test_stop_event_ends_the_thread_promptly(monkeypatch):
    monkeypatch.setattr(publish_loop, "_tick", lambda config, lock: None)
    stop_event = threading.Event()
    thread = publish_loop.start(
        Config({}), threading.Lock(), interval=10, stop_event=stop_event
    )
    stop_event.set()
    thread.join(timeout=1)
    assert not thread.is_alive()


def test_tick_holds_the_shared_lock_while_publishing_each_tenant(monkeypatch):
    """The same lock a "Post now" click takes must be held for the batch too."""
    from jobs import publish as publish_job

    seen_locked = []
    lock = threading.Lock()

    def fake_publish(run, args, dry_run):
        seen_locked.append(lock.locked())

    monkeypatch.setattr(publish_job, "publish", fake_publish)
    monkeypatch.setattr(publish_loop.runner, "runs", lambda job, config: iter([object()]))
    monkeypatch.setattr(publish_loop.runner, "isolated", lambda run, job: nullcontext())

    publish_loop._tick(Config({}), lock)

    assert seen_locked == [True]
    assert not lock.locked()  # released once the tick is done


class _FakeRun:
    def __init__(self, name: str):
        self.name = name
        self.label = name
        self.alerts: list = []

    def alert(self, title, message="", **kw):
        self.alerts.append(title)


def test_tick_isolates_one_tenants_failure_from_the_next(monkeypatch):
    from jobs import publish as publish_job

    processed = []
    broken, fine = _FakeRun("broken"), _FakeRun("fine")

    def fake_publish(run, args, dry_run):
        if run is broken:
            raise RuntimeError("this tenant's publish blew up")
        processed.append(run.name)

    monkeypatch.setattr(publish_job, "publish", fake_publish)
    monkeypatch.setattr(
        publish_loop.runner, "runs", lambda job, config: iter([broken, fine])
    )
    # The real runner.isolated() swallows exactly this: one tenant's failure
    # must not stop the loop from reaching the next.
    from lnp import runner as runner_mod

    monkeypatch.setattr(publish_loop.runner, "isolated", runner_mod.isolated)

    publish_loop._tick(Config({}), threading.Lock())

    assert processed == ["fine"]
    assert broken.alerts  # the failure was reported, not silently dropped
