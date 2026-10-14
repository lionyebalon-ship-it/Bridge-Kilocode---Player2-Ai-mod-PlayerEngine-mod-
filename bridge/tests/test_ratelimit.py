"""Tests for p2k_bridge.ratelimit (TokenBucket)."""

import sys
import threading
import time
import unittest

sys.path.insert(0, ".")
sys.path.insert(0, "..")

from p2k_bridge.ratelimit import TokenBucket  # noqa: E402


class CapacityTests(unittest.TestCase):
    def test_hourly_burst_capacity(self):
        bucket = TokenBucket(3600)  # capacity = max(1, 3600/60) = 60
        self.assertAlmostEqual(bucket.available(), 60.0, places=5)
        for _ in range(60):
            self.assertTrue(bucket.try_acquire())
        self.assertFalse(bucket.try_acquire())  # 61st denied
        # the bucket refills continuously, so it is never exactly empty
        self.assertAlmostEqual(bucket.available(), 0.0, delta=0.01)

    def test_available_decrements_exactly(self):
        bucket = TokenBucket(3600)
        bucket.try_acquire()
        self.assertAlmostEqual(bucket.available(), 59.0, delta=0.01)
        bucket.try_acquire()
        self.assertAlmostEqual(bucket.available(), 58.0, delta=0.01)

    def test_default_config_rate(self):
        # the bridge's production rate: 180/hr -> capacity 3, one token / 20s
        bucket = TokenBucket(180)
        self.assertAlmostEqual(bucket.available(), 3.0, places=5)
        for _ in range(3):
            self.assertTrue(bucket.try_acquire())
        self.assertFalse(bucket.try_acquire())


class RefillTests(unittest.TestCase):
    def test_slow_rate_needs_a_full_minute(self):
        bucket = TokenBucket(60)  # capacity 1, refill 1/60 per second
        self.assertTrue(bucket.try_acquire())
        self.assertFalse(bucket.try_acquire())
        time.sleep(1.1)
        self.assertFalse(bucket.try_acquire())  # only ~0.018 tokens back
        # backdate the clock instead of sleeping 60s
        bucket._last_refill -= 61.0
        self.assertTrue(bucket.try_acquire())

    def test_one_token_per_second_rate(self):
        bucket = TokenBucket(3600)
        for _ in range(60):
            bucket.try_acquire()
        self.assertFalse(bucket.try_acquire())
        bucket._last_refill -= 2.0  # 2 seconds -> 2 tokens
        self.assertTrue(bucket.try_acquire())
        self.assertTrue(bucket.try_acquire())
        self.assertFalse(bucket.try_acquire())

    def test_refill_caps_at_capacity(self):
        bucket = TokenBucket(3600)
        bucket._last_refill -= 10000.0  # ages far past capacity
        self.assertAlmostEqual(bucket.available(), 60.0, places=5)


class DegenerateRateTests(unittest.TestCase):
    def test_zero_rate_single_token_never_refills(self):
        bucket = TokenBucket(0)
        self.assertAlmostEqual(bucket.available(), 1.0, places=5)
        self.assertTrue(bucket.try_acquire())
        self.assertFalse(bucket.try_acquire())
        bucket._last_refill -= 100000.0  # even "an hour later"
        self.assertFalse(bucket.try_acquire())

    def test_negative_and_nonnumeric_rates(self):
        for bogus in (-1, "3600", None, [], {}, float("nan")):
            bucket = TokenBucket(bogus)
            self.assertAlmostEqual(bucket.available(), 1.0, places=5)
            self.assertTrue(bucket.try_acquire())
            self.assertFalse(bucket.try_acquire())

    def test_huge_rate(self):
        bucket = TokenBucket(10**9)
        self.assertGreater(bucket.available(), 1000.0)
        self.assertTrue(bucket.try_acquire())


class RobustnessTests(unittest.TestCase):
    def test_never_raises(self):
        for rate in (0, -1, 180, 3600, "x", None):
            bucket = TokenBucket(rate)
            try:
                bucket.try_acquire()
                bucket.available()
            except Exception as exc:  # pragma: no cover
                self.fail(f"TokenBucket({rate!r}) raised: {exc}")

    def test_thread_safety(self):
        bucket = TokenBucket(3600)
        successes = []
        lock = threading.Lock()

        def worker():
            ok = bucket.try_acquire()
            with lock:
                successes.append(ok)

        threads = [threading.Thread(target=worker) for _ in range(50)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertLessEqual(sum(1 for s in successes if s), 60)
        self.assertGreaterEqual(sum(1 for s in successes if s), 1)


class PriorityAdmissionTests(unittest.TestCase):
    """Companion chatter must not be able to starve a player's command.

    Regression for the observed two-companion failure: a spawn greeting, two
    NPC-to-NPC exchanges and one player turn drained the 3-token burst, and the
    player's next message plus its command feedback were both degraded to the
    idle envelope -- indistinguishable from the NPC ignoring them.
    """

    def test_background_calls_leave_the_reserve(self):
        bucket = TokenBucket(180)  # capacity 3, reserve 2 by default
        self.assertEqual(bucket.reserve, 2.0)
        # Background (companion-initiated) needs MORE than the reserve.
        self.assertTrue(bucket.try_acquire(background=True))   # 3 -> 2
        self.assertFalse(bucket.try_acquire(background=True))  # at the reserve

    def test_player_calls_still_get_the_reserved_tokens(self):
        bucket = TokenBucket(180)
        bucket.try_acquire(background=True)  # 3 -> 2, now at the reserve
        # Both remaining tokens are still available to a player.
        self.assertTrue(bucket.try_acquire(background=False))  # 2 -> 1
        self.assertTrue(bucket.try_acquire(background=False))  # 1 -> 0
        self.assertFalse(bucket.try_acquire(background=False))  # genuinely empty

    def test_player_calls_work_when_the_bucket_is_full(self):
        bucket = TokenBucket(180)
        self.assertTrue(bucket.try_acquire(background=False))
        self.assertTrue(bucket.try_acquire(background=False))
        self.assertTrue(bucket.try_acquire(background=False))

    def test_the_reserve_never_locks_background_out_entirely(self):
        # A capacity of 1 (a rate under 60/hr) must not make background calls
        # impossible, or a single-companion server would stop working.
        bucket = TokenBucket(1)
        self.assertEqual(bucket.reserve, 0.0)
        self.assertTrue(bucket.try_acquire(background=True))

    def test_zero_reserve_reproduces_the_old_behaviour(self):
        bucket = TokenBucket(180, reserve_tokens=0)
        self.assertEqual(bucket.reserve, 0.0)
        self.assertTrue(bucket.try_acquire(background=True))
        self.assertTrue(bucket.try_acquire(background=True))
        self.assertTrue(bucket.try_acquire(background=True))
        self.assertFalse(bucket.try_acquire(background=True))

    def test_reserve_is_capped_below_capacity(self):
        # Asking for more reserve than the bucket can hold must not deadlock
        # background traffic; the reserve is clamped to capacity - 1.
        bucket = TokenBucket(180, reserve_tokens=99)
        self.assertEqual(bucket.reserve, 2.0)
        self.assertTrue(bucket.try_acquire(background=True))

    def test_bad_reserve_values_fall_back_to_the_default(self):
        for bogus in ("2", None, [], 2.5):
            bucket = TokenBucket(180, reserve_tokens=bogus)
            self.assertEqual(bucket.reserve, 2.0, bogus)

    def test_total_rate_is_unchanged_by_the_reserve(self):
        # Priority reorders WHO is shed; it must not increase the total admitted.
        bucket = TokenBucket(180)
        admitted = 0
        for _ in range(20):
            if bucket.try_acquire(background=False) or bucket.try_acquire(background=True):
                admitted += 1
        self.assertLessEqual(admitted, 3)

    def test_default_positional_call_is_priority(self):
        bucket = TokenBucket(180)
        self.assertTrue(bucket.try_acquire())
        self.assertTrue(bucket.try_acquire())
        self.assertTrue(bucket.try_acquire())
        self.assertFalse(bucket.try_acquire())


if __name__ == "__main__":
    unittest.main()
