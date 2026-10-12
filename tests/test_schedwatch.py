import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from schedwatch.engine import Engine
from schedwatch.lock import FileLock, LockBusy
from schedwatch.models import Config, Job
from schedwatch.notify import ConsoleNotifier
from schedwatch.plan import Action, SlotState, decide
from schedwatch.publishers import FlakyPublisher
from schedwatch.store import Store

UTC = timezone.utc
JOB = Job(name="j", times=("09:00",), timezone="UTC", publisher="flaky",
          grace_minutes=5, max_lateness_minutes=60, max_attempts=3, backoff_minutes=(5, 15))
SLOT = datetime(2026, 10, 12, 9, 0, tzinfo=UTC)


class Recorder:
    def __init__(self):
        self.sent = []

    def send(self, text):
        self.sent.append(text)


def make_engine(failures=0, job=JOB):
    tmp = Path(tempfile.mkdtemp())
    pub = FlakyPublisher(failures=failures)
    notifier = Recorder()
    eng = Engine(Config([job]), Store(tmp / "t.db"), tmp, notifier, {"flaky": pub})
    return eng, pub, notifier


class DecideTests(unittest.TestCase):
    def test_before_slot_waits(self):
        self.assertIs(decide(JOB, SLOT, SlotState(), SLOT - timedelta(minutes=1)), Action.WAIT)

    def test_first_attempt_runs(self):
        self.assertIs(decide(JOB, SLOT, SlotState(), SLOT), Action.RUN)

    def test_already_ok_is_done(self):
        self.assertIs(decide(JOB, SLOT, SlotState(ok=True), SLOT + timedelta(hours=5)), Action.DONE)

    def test_never_attempted_and_too_late_is_missed(self):
        self.assertIs(decide(JOB, SLOT, SlotState(), SLOT + timedelta(minutes=61)), Action.MISSED)

    def test_retry_respects_backoff(self):
        fin = SLOT + timedelta(minutes=1)
        st = SlotState(attempts=1, last_finished=fin)
        self.assertIs(decide(JOB, SLOT, st, fin + timedelta(minutes=4)), Action.WAIT)
        self.assertIs(decide(JOB, SLOT, st, fin + timedelta(minutes=5)), Action.RUN)

    def test_second_backoff_is_longer(self):
        fin = SLOT + timedelta(minutes=10)
        st = SlotState(attempts=2, last_finished=fin)
        self.assertIs(decide(JOB, SLOT, st, fin + timedelta(minutes=14)), Action.WAIT)
        self.assertIs(decide(JOB, SLOT, st, fin + timedelta(minutes=15)), Action.RUN)

    def test_gives_up_after_max_attempts(self):
        st = SlotState(attempts=3, last_finished=SLOT)
        self.assertIs(decide(JOB, SLOT, st, SLOT + timedelta(minutes=30)), Action.GAVE_UP)

    def test_running_slot_waits(self):
        st = SlotState(attempts=1, running_since=SLOT)
        self.assertIs(decide(JOB, SLOT, st, SLOT + timedelta(minutes=1)), Action.WAIT)


class LockTests(unittest.TestCase):
    def test_second_holder_is_rejected(self):
        p = Path(tempfile.mkdtemp()) / "a.lock"
        with FileLock(p):
            with self.assertRaises(LockBusy):
                FileLock(p).acquire()

    def test_dead_pid_lock_is_stolen(self):
        p = Path(tempfile.mkdtemp()) / "a.lock"
        p.write_text(f"999999:{time.time()}")
        with FileLock(p):
            self.assertTrue(p.exists())

    def test_old_lock_is_stolen(self):
        p = Path(tempfile.mkdtemp()) / "a.lock"
        p.write_text(f"{os.getpid()}:{time.time() - 7200}")
        with FileLock(p, stale_after_s=60):
            pass


class EngineTests(unittest.TestCase):
    def test_on_time_publish_happens_once(self):
        eng, pub, _ = make_engine()
        eng.sweep("scheduler", SLOT)
        eng.sweep("scheduler", SLOT + timedelta(minutes=1))
        eng.sweep("watchdog", SLOT + timedelta(minutes=2))
        self.assertEqual(pub.published, ["j:2026-10-12T09:00"])

    def test_watchdog_does_not_steal_on_time_work(self):
        eng, pub, _ = make_engine()
        eng.sweep("watchdog", SLOT + timedelta(minutes=1))
        self.assertEqual(pub.published, [])

    def test_failures_are_retried_with_backoff_then_succeed(self):
        eng, pub, notifier = make_engine(failures=2)
        eng.sweep("scheduler", SLOT)                                  # attempt 1 fails
        eng.sweep("watchdog", SLOT + timedelta(minutes=2))            # backoff 5 min: wait
        eng.sweep("watchdog", SLOT + timedelta(minutes=6))            # attempt 2 fails
        eng.sweep("watchdog", SLOT + timedelta(minutes=10))           # backoff 15 min: wait
        eng.sweep("watchdog", SLOT + timedelta(minutes=22))           # attempt 3 succeeds
        self.assertEqual(pub.published, ["j:2026-10-12T09:00"])
        self.assertEqual(pub.calls["j:2026-10-12T09:00"], 3)
        self.assertEqual(notifier.sent, [])

    def test_gives_up_and_alerts_once(self):
        eng, pub, notifier = make_engine(failures=99)
        eng.sweep("scheduler", SLOT)
        for m in (6, 22, 40, 41, 42):
            eng.sweep("watchdog", SLOT + timedelta(minutes=m))
        self.assertEqual(pub.published, [])
        self.assertEqual(len(notifier.sent), 1)
        self.assertIn("gave_up", notifier.sent[0])

    def test_missed_slot_is_caught_up_within_window(self):
        eng, pub, _ = make_engine()
        eng.sweep("watchdog", SLOT + timedelta(minutes=30))  # machine was asleep
        self.assertEqual(pub.published, ["j:2026-10-12T09:00"])

    def test_missed_slot_outside_window_alerts_not_publishes(self):
        eng, pub, notifier = make_engine()
        eng.sweep("watchdog", SLOT - timedelta(minutes=1))   # watchdog was already running before the slot
        eng.sweep("watchdog", SLOT + timedelta(minutes=120))
        eng.sweep("watchdog", SLOT + timedelta(minutes=121))
        self.assertEqual(pub.published, [])
        self.assertEqual(len(notifier.sent), 1)
        self.assertIn("missed", notifier.sent[0])

    def test_crashed_run_is_recovered(self):
        eng, pub, _ = make_engine()
        run_id = eng.store.start("j", "2026-10-12T09:00", SLOT)       # process died after claiming
        self.assertIsNotNone(run_id)
        eng.sweep("watchdog", SLOT + timedelta(minutes=10))           # still "running": wait
        self.assertEqual(pub.published, [])
        eng.sweep("watchdog", SLOT + timedelta(minutes=40))           # stale: recorded as failed, backoff starts
        self.assertEqual(pub.published, [])
        eng.sweep("watchdog", SLOT + timedelta(minutes=46))           # backoff over: retried
        self.assertEqual(pub.published, ["j:2026-10-12T09:00"])

    def test_fresh_database_does_not_judge_old_history(self):
        eng, pub, notifier = make_engine()
        eng.sweep("watchdog", SLOT + timedelta(hours=30))
        self.assertEqual(notifier.sent, [])

    def test_store_blocks_double_claim(self):
        store = Store(":memory:")
        self.assertIsNotNone(store.start("j", "s", SLOT))
        self.assertIsNone(store.start("j", "s", SLOT))


if __name__ == "__main__":
    unittest.main()
