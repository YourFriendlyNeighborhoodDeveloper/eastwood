"""Sliding one-minute rate windows, per key (None = global)."""
from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta


class SlidingWindowRate:
    """Events in the last 60 s, per key. In-process: after a restart the window
    is empty, which permits at most one minute's quota."""

    def __init__(self) -> None:
        self._events: dict = {}

    def _window(self, key, now: datetime):
        q = self._events.setdefault(key, deque())
        cutoff = now - timedelta(seconds=60)
        while q and q[0] < cutoff:
            q.popleft()
        return q

    def allow(self, key, per_minute: int, now: datetime) -> bool:
        return len(self._window(key, now)) < max(per_minute, 0)

    def record(self, key, now: datetime) -> None:
        self._window(key, now).append(now)
