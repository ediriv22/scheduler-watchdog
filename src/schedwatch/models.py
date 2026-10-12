from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class Job:
    name: str
    times: tuple[str, ...]
    timezone: str = "UTC"
    publisher: str = "dry-run"
    grace_minutes: int = 5            # scheduler only starts a slot if it is at most this late
    max_lateness_minutes: int = 180   # after this the watchdog stops trying and alerts
    max_attempts: int = 3
    backoff_minutes: tuple[int, ...] = (5, 15)
    stale_run_minutes: int = 20       # a "running" row older than this is treated as a crash

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def slots_between(self, start: datetime, end: datetime) -> list[tuple[str, datetime]]:
        """All (slot_id, slot_time) with start <= slot_time <= end. Slot ids are local, e.g. 2026-10-12T09:00."""
        out: list[tuple[str, datetime]] = []
        day: date = start.astimezone(self.tz).date()
        last: date = end.astimezone(self.tz).date()
        while day <= last:
            for hhmm in self.times:
                hh, mm = (int(p) for p in hhmm.split(":"))
                local = datetime.combine(day, time(hh, mm), tzinfo=self.tz)
                if start <= local <= end:
                    out.append((f"{day.isoformat()}T{hhmm}", local))
            day += timedelta(days=1)
        return out


@dataclass
class Config:
    jobs: list[Job] = field(default_factory=list)

    @classmethod
    def load(cls, path: str | Path) -> "Config":
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        jobs = []
        for j in raw["jobs"]:
            j = dict(j)
            j["times"] = tuple(j["times"])
            if "backoff_minutes" in j:
                j["backoff_minutes"] = tuple(j["backoff_minutes"])
            jobs.append(Job(**j))
        names = [j.name for j in jobs]
        if len(set(names)) != len(names):
            raise ValueError("duplicate job names in config")
        return cls(jobs)
