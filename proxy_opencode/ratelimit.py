"""In-memory per-key fixed-window rate limiter."""

from __future__ import annotations

import time
from collections import defaultdict, deque


class RateLimiter:
    def __init__(self, requests_per_minute: int = 60) -> None:
        self.limit = requests_per_minute
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        window_start = now - 60.0
        q = self._hits[key]
        while q and q[0] < window_start:
            q.popleft()
        if len(q) >= self.limit:
            return False
        q.append(now)
        return True
