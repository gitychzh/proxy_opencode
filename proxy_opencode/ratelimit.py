"""Per-key fixed-window rate limiter."""

from __future__ import annotations

import collections
import time


class RateLimiter:
    def __init__(self, requests_per_minute: int) -> None:
        self.rpm = max(1, requests_per_minute)
        self._hits: dict[str, collections.deque] = collections.defaultdict(
            collections.deque
        )

    def allow(self, key: str) -> bool:
        now = time.time()
        window_start = now - 60.0
        hits = self._hits[key]
        while hits and hits[0] < window_start:
            hits.popleft()
        if len(hits) >= self.rpm:
            return False
        hits.append(now)
        return True
