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


if __name__ == "__main__":
    unittest.main()
