# schedwatch

[![CI](https://github.com/ediriv22/scheduler-watchdog/actions/workflows/ci.yml/badge.svg)](https://github.com/ediriv22/scheduler-watchdog/actions/workflows/ci.yml) ![python](https://img.shields.io/badge/python-3.10%2B-blue) ![deps](https://img.shields.io/badge/dependencies-none-brightgreen) ![license](https://img.shields.io/badge/license-MIT-lightgrey)

A small scheduler with an **independent watchdog** for jobs that must run at fixed times and must never run twice, such as publishing a post, sending a report or syncing data. Pure Python standard library, no dependencies.

It is a clean, generic version of a pattern I use in my own automation home lab, where a daily publisher on a laptop has to survive expired tokens, API errors, crashes and a machine that falls asleep.

## The problem

A plain cron job or `sleep` loop fails silently:

- the API returns an error once and the post is lost
- the process is killed halfway and nobody knows whether it published
- the laptop was asleep at 09:00 and the slot is simply skipped
- a retry after a timeout publishes the same post twice

## How it works

```
 launchd/cron, every 1 min            launchd/cron, every 5 min
 ┌───────────────────────┐            ┌─────────────────────────────┐
 │ scheduler             │            │ watchdog                    │
 │ first attempt, on time│            │ retries with backoff        │
 │ (<= grace_minutes)    │            │ catches up missed slots     │
 └──────────┬────────────┘            │ recovers crashed runs       │
            │                         │ alerts once, then stops     │
            ▼                         └──────────────┬──────────────┘
      ┌───────────────────────────────────────────────▼───────┐
      │ SQLite run log (one row per attempt)                  │
      │ unique index: one success and one running row per slot│
      └───────────────────────────────────────────────────────┘
```

- **Slots.** A job runs at fixed local times (`"09:00"`, `"18:30"`). Each occurrence is a slot with a stable id such as `2026-10-12T09:00`.
- **Scheduler vs watchdog.** They are two separate processes with different scopes. The scheduler only starts a slot on time. Everything else (retries, catch-up, crash recovery, alerts) belongs to the watchdog, so a broken scheduler cannot hide its own failure.
- **Idempotency.** Success is enforced in the database with a partial unique index, and every publish call receives the key `job:slot` so a real publisher can pass it to the upstream API. A slot that already succeeded is never run again.
- **Crash recovery.** A run that stays `running` longer than `stale_run_minutes` is recorded as a failed attempt and retried after the backoff.
- **Bounded effort.** Attempts are limited and a slot has a `max_lateness_minutes` window. After that the watchdog sends one alert (deduplicated in the database) and stops.
- **No alert storm.** The database remembers when it was first swept. History from before that is not judged.
- **Locking.** A file lock per job, with stale detection (dead PID or too old).

The decision logic lives in `plan.py` as a pure function, so every branch is covered by unit tests without a clock or a database.

## Try it

```bash
git clone https://github.com/ediriv22/scheduler-watchdog
cd scheduler-watchdog
PYTHONPATH=src python3 -m schedwatch demo
```

The demo simulates a morning where the upstream API fails twice:

```
09:00 scheduler -> ['failed daily-post:2026-10-12T09:00: upstream API error (call 1)']
09:01 watchdog  -> nothing to do
09:06 watchdog  -> ['failed daily-post:2026-10-12T09:00: upstream API error (call 2)']
09:22 watchdog  -> ['ok daily-post:2026-10-12T09:00']
09:23 scheduler -> nothing to do
09:30 watchdog  -> nothing to do
published exactly once: ['daily-post:2026-10-12T09:00']
```

Run the tests:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## Use it

```bash
cp config/jobs.example.json config/jobs.json
PYTHONPATH=src python3 -m schedwatch scheduler   # run every minute
PYTHONPATH=src python3 -m schedwatch watchdog    # run every 5 minutes
PYTHONPATH=src python3 -m schedwatch status      # last runs
```

On macOS, copy the two files in `deploy/` to `~/Library/LaunchAgents/`, replace `/path/to/...` and load them with `launchctl bootstrap`. With cron:

```
* * * * *   cd /path/to/repo && PYTHONPATH=src python3 -m schedwatch scheduler
*/5 * * * * cd /path/to/repo && PYTHONPATH=src python3 -m schedwatch watchdog
```

### Job options

| field | meaning |
|---|---|
| `times` | local times of day, `"HH:MM"` |
| `timezone` | IANA name, for example `Europe/Rome` |
| `publisher` | name registered in `publishers.REGISTRY` |
| `grace_minutes` | how late the scheduler may still start a first attempt |
| `max_lateness_minutes` | after this the slot is abandoned and alerted |
| `max_attempts`, `backoff_minutes` | retry policy; the last backoff repeats |
| `stale_run_minutes` | when a `running` row counts as a crash |

### Add a real publisher

```python
class MyPublisher:
    def publish(self, job, slot, idempotency_key):
        resp = requests.post(URL, json=payload, headers={"Idempotency-Key": idempotency_key})
        resp.raise_for_status()
        return resp.json()["id"]

REGISTRY["my-api"] = MyPublisher()
```

Credentials come from the environment, never from the repo. Alerts go to the console by default, or to Telegram when `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` are set.

## Layout

```
src/schedwatch/
  models.py      job config and slot calculation (timezone aware)
  plan.py        pure decision function: RUN / WAIT / DONE / MISSED / GAVE_UP
  store.py       SQLite run log and alert deduplication
  lock.py        file lock with stale detection
  engine.py      scheduler and watchdog scopes
  publishers.py  publisher interface, dry-run and a flaky test double
  notify.py      console and Telegram alerts
  cli.py         scheduler | watchdog | status | demo
tests/           20 unit tests, standard library only
deploy/          launchd examples
```

## Limits

This is a single-machine design. Two machines sharing one database file would need a real database and distributed locking. Time zones follow the system tz database, and slots skipped by a DST change are not backfilled.

MIT license.
