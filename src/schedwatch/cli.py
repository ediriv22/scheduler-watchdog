from __future__ import annotations

import argparse
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .engine import Engine
from .models import Config
from .notify import ConsoleNotifier, from_env
from .publishers import FlakyPublisher
from .store import Store


def _demo() -> None:
    """Simulated morning: the publisher fails twice, the watchdog retries, nothing is posted twice."""
    cfg = Config.load(Path(__file__).resolve().parents[2] / "config" / "jobs.example.json")
    cfg.jobs[0] = type(cfg.jobs[0])(**{**cfg.jobs[0].__dict__, "publisher": "flaky", "times": ("09:00",)})
    flaky = FlakyPublisher(failures=2)
    tmp = Path(tempfile.mkdtemp())
    eng = Engine(cfg, Store(tmp / "demo.db"), tmp, ConsoleNotifier(), {"flaky": flaky})
    start = datetime(2026, 10, 12, 9, 0, tzinfo=cfg.jobs[0].tz)
    for minutes, scope in [(0, "scheduler"), (1, "watchdog"), (6, "watchdog"), (22, "watchdog"), (23, "scheduler"), (30, "watchdog")]:
        now = (start + timedelta(minutes=minutes)).astimezone(timezone.utc)
        out = eng.sweep(scope, now)
        print(f"09:{minutes:02d} {scope:<9} -> {out or 'nothing to do'}")
    print(f"published exactly once: {flaky.published}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="schedwatch")
    p.add_argument("command", choices=["scheduler", "watchdog", "status", "demo"])
    p.add_argument("--config", default="config/jobs.json")
    p.add_argument("--db", default="data/runs.db")
    p.add_argument("--lock-dir", default="data/locks")
    a = p.parse_args(argv)
    if a.command == "demo":
        _demo()
        return 0
    store = Store(a.db)
    if a.command == "status":
        for r in store.recent():
            print(f"{r['job']} {r['slot']} #{r['attempt']} {r['status']} {r['error'] or ''}")
        return 0
    eng = Engine(Config.load(a.config), store, a.lock_dir, from_env())
    for line in eng.sweep(a.command, datetime.now(timezone.utc)):
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
