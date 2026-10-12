"""Scheduler and watchdog share one engine but have different scopes.

scheduler: starts slots on time (first attempt, at most `grace_minutes` late).
watchdog : everything else (retries with backoff, catch-up of missed slots,
           crash recovery, alerts). It runs as a separate process so a broken
           scheduler cannot hide its own failure.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from .lock import FileLock, LockBusy
from .models import Config, Job
from .plan import Action, decide
from .publishers import REGISTRY, Publisher
from .store import Store

LOOKBACK_HOURS = 36


class Engine:
    def __init__(self, config: Config, store: Store, lock_dir: str | Path, notifier,
                 publishers: dict[str, Publisher] | None = None):
        self.config, self.store, self.notifier = config, store, notifier
        self.lock_dir = Path(lock_dir)
        self.publishers = publishers or REGISTRY

    def sweep(self, scope: str, now: datetime, clock: Callable[[], datetime] | None = None) -> list[str]:
        assert scope in ("scheduler", "watchdog")
        clock = clock or (lambda: now)
        log: list[str] = []
        since = self.store.watching_since(now)
        for job in self.config.jobs:
            self.store.expire_stale(now, job.stale_run_minutes, job.name)
            start = max(now - timedelta(hours=LOOKBACK_HOURS), since - timedelta(minutes=job.max_lateness_minutes))
            for slot, slot_time in job.slots_between(start, now):
                state = self.store.state(job.name, slot)
                action = decide(job, slot_time, state, now)
                on_time = state.attempts == 0 and now - slot_time <= timedelta(minutes=job.grace_minutes)
                if action is Action.RUN and (on_time if scope == "scheduler" else not on_time):
                    log.append(self._run(job, slot, clock))
                elif scope == "watchdog" and action in (Action.MISSED, Action.GAVE_UP):
                    if self.store.alert_once(job.name, slot, action.value, now):
                        self.notifier.send(f"{job.name} {slot}: {action.value} after {state.attempts} attempt(s)")
                        log.append(f"alert {job.name} {slot} {action.value}")
        return log

    def _run(self, job: Job, slot: str, clock: Callable[[], datetime]) -> str:
        key = f"{job.name}:{slot}"
        try:
            with FileLock(self.lock_dir / f"{job.name}.lock"):
                run_id = self.store.start(job.name, slot, clock())
                if run_id is None:
                    return f"skip {key} (already claimed)"
                try:
                    ext = self.publishers[job.publisher].publish(job, slot, key)
                except Exception as exc:  # any failure becomes a recorded attempt
                    self.store.finish(run_id, clock(), error=f"{type(exc).__name__}: {exc}")
                    return f"failed {key}: {exc}"
                self.store.finish(run_id, clock(), external_id=ext)
                return f"ok {key}"
        except LockBusy:
            return f"skip {key} (lock busy)"
