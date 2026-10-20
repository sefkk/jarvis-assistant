"""Per-stage timers for a turn (`timings` table)."""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Iterator

from jarvis.utils.timeutil import monotonic_ms

log = logging.getLogger("jarvis.timing")


class TurnTimer:
    """Collects (stage, duration_ms) pairs for one turn and writes them to the database."""

    def __init__(self, turn_id: str, db=None, *, prefix: str = "") -> None:
        self.turn_id = turn_id
        self.db = db
        self.prefix = prefix  # e.g. "delegate:" so sub-agent stages stay distinguishable
        self.stages: list[tuple[str, float]] = []
        self._started = monotonic_ms()

    @contextmanager
    def stage(self, name: str) -> Iterator[None]:
        start = monotonic_ms()
        try:
            yield
        finally:
            self.record(name, monotonic_ms() - start)

    def record(self, name: str, duration_ms: float) -> None:
        name = self.prefix + name
        self.stages.append((name, duration_ms))
        if self.db is not None:
            self.db.add_timing(self.turn_id, name, duration_ms)
        log.debug("stage %s took %.1f ms", name, duration_ms)

    def elapsed_ms(self) -> float:
        return monotonic_ms() - self._started

    def summary(self) -> dict[str, float]:
        """Total milliseconds per stage name (stages can repeat, e.g. several api_call)."""
        totals: dict[str, float] = {}
        for name, ms in self.stages:
            totals[name] = totals.get(name, 0.0) + ms
        return totals
