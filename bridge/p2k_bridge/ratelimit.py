"""Thread-safe token bucket rate limiting.

One bucket governs the total request rate, because that is what the gateway
caps. But a *single* bucket shared by everything means the companions'
conversation with each other can drain the burst and shed a player's command --
observed live: a two-companion roster spent its 3-token burst on a greeting, two
NPC-to-NPC exchanges and one player turn, and then the player's next message and
its command feedback were both degraded to the idle envelope. The player sees
"the NPC ignored me".

So admission is priority-aware. Player-driven calls (``conversation``,
``command_feedback``) may use any remaining token. Everything the companions do
on their own (NPC-to-NPC chatter, spawn greetings, background memory extraction)
must leave a reserve behind, so it can no longer starve the player.

The total rate is unchanged: the bucket still admits at most ``rate_per_hour``
requests per hour. Only *who gets shed when it is tight* changes.
"""

import threading
import time
from typing import Any

# Call kinds that represent a player asking for something. These win the bucket.
PRIORITY_KINDS = ("conversation", "command_feedback")

# The complement, as an explicit allow-list: companion-initiated traffic that is
# shed first when the quota is tight. An allow-list (rather than "not a priority
# kind") keeps `unknown` and unresolvable routes behaving exactly as before, which
# is the invariant the classifier documents.
BACKGROUND_KINDS = ("npc_dialogue", "greeting", "event", "memory_extraction")

DEFAULT_RESERVE = 2


class TokenBucket:
    """Continuously refilling token bucket keyed to a requests-per-hour rate."""

    def __init__(self, rate_per_hour: int, reserve_tokens: int = DEFAULT_RESERVE,
                 burst_capacity: int = 0):
        rate = rate_per_hour if isinstance(rate_per_hour, (int, float)) else 0
        self._rate_per_hour = float(rate) if rate > 0 else 0.0
        self._capacity = self._compute_capacity(burst_capacity)
        self._tokens = self._capacity
        self._last_refill = time.monotonic()
        self._lock = threading.Lock()
        # Never reserve so much that background calls can never run: with a
        # capacity of 1 (a rate under 60/hr) the reserve is 0, which reproduces
        # the old behaviour exactly.
        self._reserve = self._compute_reserve(reserve_tokens)

    def _compute_capacity(self, burst_capacity: Any) -> float:
        """Resolve the burst capacity.

        ``0``/unset derives it from the rate as ``rate / 60``, which is what the
        bridge has always done, so a clean checkout is unchanged. An explicit
        value decouples the burst from the rate, so an NPC turn's 3-5 calls can
        run without stalling halfway through.

        The burst is clamped to ``[1, rate]``: a burst larger than the hourly
        budget would spend next year's quota today and then stall for hours, and
        a burst below 1 would deadlock the bridge. Booleans are rejected first --
        ``True`` is an ``int`` in Python and would silently mean burst 1.
        """
        if isinstance(burst_capacity, bool):
            requested = 0
        elif isinstance(burst_capacity, int):
            requested = burst_capacity
        else:
            requested = 0

        if requested <= 0:
            return max(1.0, self._rate_per_hour / 60.0)

        ceiling = max(1, int(self._rate_per_hour))
        return float(min(requested, ceiling))

    def _compute_reserve(self, reserve_tokens: Any) -> float:
        try:
            requested = int(reserve_tokens)
        except (TypeError, ValueError):
            requested = DEFAULT_RESERVE
        if requested <= 0:
            return 0.0
        # Keep at least one token reachable by background calls.
        return float(min(requested, max(0, int(self._capacity) - 1)))

    @property
    def reserve(self) -> float:
        return self._reserve

    @property
    def capacity(self) -> float:
        """Resolved burst capacity, so it is observable in tests and diagnostics."""
        return self._capacity

    def _refill(self, now: float) -> None:
        elapsed = now - self._last_refill
        if elapsed <= 0:
            return
        self._tokens = min(self._capacity, self._tokens + elapsed * (self._rate_per_hour / 3600.0))
        self._last_refill = now

    def try_acquire(self, background: bool = False) -> bool:
        """Consume one token if available; never raises.

        ``background=True`` marks a call the companions make on their own. Such a
        call is admitted only if at least the reserve will *remain afterwards*,
        so it cannot eat into the tokens a player's next command needs.

        The comparison is ``>= reserve + 1`` rather than ``> reserve`` on purpose:
        the bucket refills continuously, so after any real elapsed time the token
        count is never exactly the reserve -- it is always a hair above it, and a
        bare ``> reserve`` would therefore let every background call through.
        """
        try:
            with self._lock:
                self._refill(time.monotonic())
                if background:
                    admitted = self._tokens >= self._reserve + 1.0
                else:
                    admitted = self._tokens >= 1.0
                if not admitted:
                    return False
                self._tokens -= 1.0
                return True
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
