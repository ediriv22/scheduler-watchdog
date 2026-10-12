"""Pure decision logic. No I/O, so every branch is easy to test."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum

from .models import Job


class Action(Enum):
    DONE = "done"          # already published, never run again
    WAIT = "wait"          # not due yet, still running, or waiting for backoff
    RUN = "run"
    MISSED = "missed"      # never attempted and too late to catch up: alert
    GAVE_UP = "gave_up"    # attempts exhausted or too late to retry: alert


@dataclass
class SlotState:
    ok: bool = False
    attempts: int = 0
    last_finished: datetime | None = None
    running_since: datetime | None = None


def decide(job: Job, slot_time: datetime, state: SlotState, now: datetime) -> Action:
    if state.ok:
        return Action.DONE
    late = now - slot_time
    if late < timedelta(0):
        return Action.WAIT
    if state.running_since is not None:
        return Action.WAIT
    max_late = timedelta(minutes=job.max_lateness_minutes)
    if state.attempts == 0:
        return Action.RUN if late <= max_late else Action.MISSED
    if state.attempts >= job.max_attempts or late > max_late:
        return Action.GAVE_UP
    idx = min(state.attempts - 1, len(job.backoff_minutes) - 1)
    wait = timedelta(minutes=job.backoff_minutes[idx]) if job.backoff_minutes else timedelta(0)
    assert state.last_finished is not None
    return Action.RUN if now >= state.last_finished + wait else Action.WAIT
