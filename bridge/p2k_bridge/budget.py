"""Thread-safe rolling-window token budget for Kilo gateway calls.

A call reserves tokens up front (pessimistic: estimated input + max output),
then commits the ACTUAL usage reported by the upstream gateway, or refunds the
reservation if the call failed. Replaces the Player2 "Joules" system with a
local 500k-token-per-hour budget.
"""

import threading
import time

_MAX_WINDOW_ENTRIES = 10_000


class TokenBudget:
    """Rolling-window token budget keyed to a tokens-per-window capacity."""

    def __init__(self, tokens_per_window: int, window_seconds: float = 3600.0):
        capacity = tokens_per_window if isinstance(tokens_per_window, (int, float)) else 0
        self.tokens_per_window = max(0, int(capacity))
        seconds = window_seconds if isinstance(window_seconds, (int, float)) else 3600.0
        self.window_seconds = float(seconds) if seconds > 0 else 0.0
        self._lock = threading.Lock()
        self._window: list[tuple[float, int]] = []
        self.lifetime_prompt = 0
        self.lifetime_completion = 0
        self.lifetime_total = 0
        self.lifetime_reserved_refunded = 0
        self.lifetime_denials = 0

    @staticmethod
    def estimate_tokens(messages: list[dict]) -> int:
        """Deterministic chars/4 approximation: 4 tokens of overhead per message."""
        total = 0
        for message in messages or []:
            if not isinstance(message, dict):
                continue
            content = message.get("content", "")
            total += 4 + len(str(content)) // 4
        return max(1, total)

    def _prune_locked(self, now: float) -> None:
        """Drop entries older than window_seconds. Caller must hold the lock."""
        horizon = now - self.window_seconds
        stale = 0
        for timestamp, _ in self._window:
            if timestamp <= horizon:
                stale += 1
            else:
                break
        if stale:
            del self._window[:stale]

    def _window_total_locked(self) -> int:
        """Sum of window entries. Caller must hold the lock."""
        return sum(tokens for _, tokens in self._window)

    def _compact_locked(self) -> None:
        """Merge the oldest entries when the window list grows too large.

        Caller must hold the lock. The merged entry keeps the newest timestamp
        of its group, so usage stays counted for the full window duration.
        """
        if len(self._window) <= _MAX_WINDOW_ENTRIES:
            return
        keep = _MAX_WINDOW_ENTRIES // 2
        merged = self._window[: len(self._window) - keep]
        merged_tokens = sum(tokens for _, tokens in merged)
        merged_until = merged[-1][0]
        self._window = [(merged_until, merged_tokens)] + self._window[len(merged) :]

    def _refund_locked(self, amount: int) -> None:
        """Absorb `amount` from the most recent window entries. Caller holds the lock."""
        remaining = amount
        while remaining > 0 and self._window:
            timestamp, tokens = self._window[-1]
            if tokens <= remaining:
                remaining -= tokens
                self.lifetime_reserved_refunded += tokens
                self._window.pop()
            else:
                self._window[-1] = (timestamp, tokens - remaining)
                self.lifetime_reserved_refunded += remaining
                remaining = 0

    def try_reserve(self, amount: int) -> bool:
        """Reserve `amount` tokens up front; False (denial counted) if it would not fit."""
        try:
            reserve = max(0, int(amount))
        except (TypeError, ValueError):
            return False
        try:
            with self._lock:
                now = time.monotonic()
                self._prune_locked(now)
                if self._window_total_locked() + reserve > self.tokens_per_window:
                    self.lifetime_denials += 1
                    return False
                self._window.append((now, reserve))
                self._compact_locked()
                return True
        except Exception:
            return False

    def refund(self, amount: int) -> None:
        """Give back reserved tokens (most recent entries first); never raises."""
        try:
            give_back = max(0, int(amount))
        except (TypeError, ValueError):
            return
        try:
            with self._lock:
                self._prune_locked(time.monotonic())
                self._refund_locked(give_back)
        except Exception:
            pass

    def commit(
        self,
        actual_total: int,
        reserved: int,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
    ) -> None:
        """Replace a reservation with the actual usage reported by the gateway.

        Refunds `reserved` and appends `max(0, actual_total)` to the window;
        the lifetime counters record the reported values verbatim.
        """
        try:
            actual = int(actual_total)
            held = max(0, int(reserved))
            prompt = int(prompt_tokens)
            completion = int(completion_tokens)
        except (TypeError, ValueError):
            return
        try:
            with self._lock:
                now = time.monotonic()
                self._prune_locked(now)
                self._refund_locked(held)
                self._window.append((now, max(0, actual)))
                self._compact_locked()
                self.lifetime_prompt += prompt
                self.lifetime_completion += completion
                self.lifetime_total += actual
        except Exception:
            pass

    def window_usage(self) -> int:
        """Tokens currently reserved/committed inside the rolling window."""
        try:
            with self._lock:
                self._prune_locked(time.monotonic())
                return self._window_total_locked()
        except Exception:
            return 0

    def remaining(self) -> int:
        """Tokens still available in the current window."""
        return max(0, self.tokens_per_window - self.window_usage())

    def would_fit(self, amount: int) -> bool:
        """Dry-run try_reserve: checks fit without reserving or counting a denial."""
        try:
            reserve = max(0, int(amount))
        except (TypeError, ValueError):
            return False
        try:
            with self._lock:
                self._prune_locked(time.monotonic())
                return self._window_total_locked() + reserve <= self.tokens_per_window
        except Exception:
            return False

    def snapshot(self) -> dict:
        """Point-in-time view of the window and lifetime counters."""
        try:
            window_used = self.window_usage()
        except Exception:
            window_used = 0
        capacity = int(self.tokens_per_window)
        return {
            "tokens_per_window": capacity,
            "window_seconds": float(self.window_seconds),
            "window_used": int(window_used),
            "remaining": max(0, capacity - int(window_used)),
            "lifetime_prompt": int(self.lifetime_prompt),
            "lifetime_completion": int(self.lifetime_completion),
            "lifetime_total": int(self.lifetime_total),
            "lifetime_denials": int(self.lifetime_denials),
            "lifetime_refunded": int(self.lifetime_reserved_refunded),
        }


BUDGET = TokenBudget(500_000, 3600.0)
