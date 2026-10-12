"""Publisher plug-ins. Real ones call an API and must honour the idempotency key."""
from __future__ import annotations

from typing import Protocol

from .models import Job


class Publisher(Protocol):
    def publish(self, job: Job, slot: str, idempotency_key: str) -> str:
        """Return an external id. Raise on failure."""


class DryRunPublisher:
    def publish(self, job: Job, slot: str, idempotency_key: str) -> str:
        print(f"[dry-run] published {job.name} slot={slot} key={idempotency_key}")
        return f"dry-{idempotency_key}"


class FlakyPublisher:
    """Test double: fails the first `failures` calls for each slot, then succeeds."""

    def __init__(self, failures: int = 2):
        self.failures = failures
        self.calls: dict[str, int] = {}
        self.published: list[str] = []

    def publish(self, job: Job, slot: str, idempotency_key: str) -> str:
        n = self.calls.get(idempotency_key, 0) + 1
        self.calls[idempotency_key] = n
        if n <= self.failures:
            raise RuntimeError(f"upstream API error (call {n})")
        self.published.append(idempotency_key)
        return f"ext-{idempotency_key}"


REGISTRY: dict[str, Publisher] = {"dry-run": DryRunPublisher()}
