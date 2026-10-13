"""Thread-safe token bucket rate limiting."""

import threading
import time


class TokenBucket:
    """Continuously refilling token bucket keyed to a requests-per-hour rate."""

    def __init__(self, rate_per_hour: int):
        rate = rate_per_hour if isinstance(rate_per_hour, (int, float)) else 0
        self._rate_per_hour = float(rate) if rate > 0 else 0.0
        self._capacity = max(1.0, self._rate_per_hour / 60.0)
        self._tokens = self._capacity
        self._last_refill = time.monotonic()
        self._lock = threading.Lock()

    def _refill(self, now: float) -> None:
        elapsed = now - self._last_refill
        if elapsed <= 0:
            return
        self._tokens = min(self._capacity, self._tokens + elapsed * (self._rate_per_hour / 3600.0))
        self._last_refill = now

    def try_acquire(self) -> bool:
        """Consume one token if available; never raises."""
        try:
            with self._lock:
                self._refill(time.monotonic())
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return True
                return False
        except Exception:
            return False

    def available(self) -> float:
        """Approximate number of tokens currently in the bucket; never raises."""
        try:
            with self._lock:
                self._refill(time.monotonic())
                return self._tokens
        except Exception:
            return 0.0
