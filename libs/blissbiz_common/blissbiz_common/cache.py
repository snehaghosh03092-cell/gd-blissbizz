"""Tiny per-process TTL cache (single worker per service for now, docs/02 section 12)."""

import threading
import time
from collections.abc import Callable
from typing import Any


class TTLCache:
    def __init__(
        self, ttl_seconds: float, clock: Callable[[], float] = time.monotonic, *, max_entries: int | None = None
    ):
        self.ttl, self.clock, self._data, self._lock = ttl_seconds, clock, {}, threading.Lock()
        self.max_entries = max_entries

    def get(self, key: str) -> Any | None:
        with self._lock:
            hit = self._data.get(key)
            if hit is None or hit[0] <= self.clock():  # ttl 0 = never cached
                self._data.pop(key, None)
                return None
            return hit[1]

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self._data.pop(key, None)
            if self.max_entries and len(self._data) >= self.max_entries:
                self._data.pop(next(iter(self._data)))
            self._data[key] = (self.clock() + self.ttl, value)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

    def pop(self, key: str) -> None:
        with self._lock:
            self._data.pop(key, None)
