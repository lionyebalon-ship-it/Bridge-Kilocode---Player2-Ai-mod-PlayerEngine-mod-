"""Tests for p2k_bridge.budget."""

import threading
import time
import unittest

from p2k_bridge.budget import _MAX_WINDOW_ENTRIES, BUDGET, TokenBudget


class TokenBudgetTests(unittest.TestCase):
    def test_reserve_within_budget_reduces_remaining(self):
        budget = TokenBudget(1000)
        self.assertTrue(budget.try_reserve(400))
        self.assertEqual(budget.window_usage(), 400)
        self.assertEqual(budget.remaining(), 600)

    def test_reserve_beyond_budget_fails_and_counts_denial(self):
        budget = TokenBudget(500)
        self.assertTrue(budget.try_reserve(500))
        self.assertFalse(budget.try_reserve(1))
        self.assertEqual(budget.window_usage(), 500)
        self.assertEqual(budget.remaining(), 0)
        self.assertEqual(budget.snapshot()["lifetime_denials"], 1)

    def test_window_rollover_after_sleep(self):
        budget = TokenBudget(1000, window_seconds=0.5)
        self.assertTrue(budget.try_reserve(300))
        self.assertEqual(budget.window_usage(), 300)
        time.sleep(0.7)
        self.assertEqual(budget.window_usage(), 0)
        self.assertEqual(budget.remaining(), 1000)

    def test_commit_replaces_reservation(self):
        budget = TokenBudget(10_000)
        self.assertTrue(budget.try_reserve(1000))
        budget.commit(200, 1000, prompt_tokens=150, completion_tokens=50)
        self.assertEqual(budget.window_usage(), 200)
        self.assertEqual(budget.remaining(), 9_800)
        snap = budget.snapshot()
        self.assertEqual(snap["lifetime_prompt"], 150)
        self.assertEqual(snap["lifetime_completion"], 50)
        self.assertEqual(snap["lifetime_total"], 200)

    def test_refund_reduces_window_usage_and_tracks_lifetime(self):
        budget = TokenBudget(1000)
        self.assertTrue(budget.try_reserve(1000))
        budget.refund(400)
        self.assertEqual(budget.window_usage(), 600)
        self.assertEqual(budget.snapshot()["lifetime_refunded"], 400)
        budget.refund(10_000)
        self.assertEqual(budget.window_usage(), 0)
        self.assertEqual(budget.snapshot()["lifetime_refunded"], 1000)

    def test_estimate_tokens_heuristic(self):
        self.assertGreaterEqual(TokenBudget.estimate_tokens([]), 1)
        self.assertEqual(TokenBudget.estimate_tokens([{"role": "user"}]), 4)
        self.assertEqual(TokenBudget.estimate_tokens([{"role": "user", "content": "a" * 400}]), 104)
        messages = [{"role": "system", "content": "abc"}, {"role": "user", "content": "a" * 8}]
        self.assertEqual(TokenBudget.estimate_tokens(messages), 4 + 4 + 2)

    def test_thread_safety_reservations_never_exceed_budget(self):
        budget = TokenBudget(1000)
        results = []
        errors = []
        guard = threading.Lock()

        def worker():
            try:
                reserved = budget.try_reserve(50)
            except Exception as exc:
                with guard:
                    errors.append(exc)
                return
            with guard:
                results.append(reserved)

        threads = [threading.Thread(target=worker) for _ in range(20)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [])
        self.assertEqual(len(results), 20)
        self.assertLessEqual(sum(results) * 50, 1000)
        self.assertEqual(budget.window_usage(), sum(results) * 50)

    def test_would_fit_does_not_reserve_or_count_denials(self):
        budget = TokenBudget(100)
        self.assertTrue(budget.would_fit(60))
        self.assertEqual(budget.window_usage(), 0)
        self.assertFalse(budget.would_fit(200))
        self.assertEqual(budget.window_usage(), 0)
        self.assertEqual(budget.snapshot()["lifetime_denials"], 0)

        self.assertTrue(budget.try_reserve(100))
        self.assertFalse(budget.would_fit(1))
        self.assertEqual(budget.window_usage(), 100)
        self.assertEqual(budget.snapshot()["lifetime_denials"], 0)
        self.assertFalse(budget.try_reserve(1))
        self.assertEqual(budget.snapshot()["lifetime_denials"], 1)

    def test_snapshot_keys_and_types(self):
        budget = TokenBudget(4321, window_seconds=1234.5)
        budget.try_reserve(21)
        snap = budget.snapshot()
        self.assertEqual(
            set(snap),
            {
                "tokens_per_window",
                "window_seconds",
                "window_used",
                "remaining",
                "lifetime_prompt",
                "lifetime_completion",
                "lifetime_total",
                "lifetime_denials",
                "lifetime_refunded",
            },
        )
        self.assertIsInstance(snap["tokens_per_window"], int)
        self.assertIsInstance(snap["window_seconds"], float)
        for key in (
            "window_used",
            "remaining",
            "lifetime_prompt",
            "lifetime_completion",
            "lifetime_total",
            "lifetime_denials",
            "lifetime_refunded",
        ):
            self.assertIsInstance(snap[key], int, key)
        self.assertEqual(snap["tokens_per_window"], 4321)
        self.assertEqual(snap["window_seconds"], 1234.5)
        self.assertEqual(snap["window_used"], 21)
        self.assertEqual(snap["remaining"], 4300)

    def test_capacity_guard_compacts_old_entries(self):
        budget = TokenBudget(1_000_000)
        now = time.monotonic()
        budget._window = [(now, 1) for _ in range(_MAX_WINDOW_ENTRIES + 1)]
        self.assertTrue(budget.try_reserve(1))
        self.assertLessEqual(len(budget._window), _MAX_WINDOW_ENTRIES)
        self.assertEqual(budget.window_usage(), _MAX_WINDOW_ENTRIES + 2)

    def test_refund_after_window_rollover_absorbs_nothing(self):
        budget = TokenBudget(1000, window_seconds=0.5)
        self.assertTrue(budget.try_reserve(1000))
        time.sleep(0.7)
        budget.refund(1000)
        self.assertEqual(budget.window_usage(), 0)
        self.assertEqual(budget.snapshot()["lifetime_refunded"], 0)

    def test_commit_counts_refunded_reservation(self):
        budget = TokenBudget(10_000)
        self.assertTrue(budget.try_reserve(1000))
        budget.commit(200, 1000, prompt_tokens=150, completion_tokens=50)
        snap = budget.snapshot()
        self.assertEqual(snap["lifetime_refunded"], 1000)
        self.assertEqual(budget.window_usage(), 200)

    def test_public_methods_never_raise_on_bad_input(self):
        budget = TokenBudget(100)
        self.assertFalse(budget.try_reserve("not-a-number"))
        self.assertFalse(budget.would_fit("not-a-number"))
        budget.refund(None)
        budget.refund(-5)
        budget.commit("bad", 0)
        budget.commit(50, 0, prompt_tokens="bad", completion_tokens=0)
        self.assertEqual(budget.window_usage(), 0)
        self.assertEqual(budget.snapshot()["lifetime_total"], 0)

    def test_module_singleton_defaults(self):
        self.assertEqual(BUDGET.tokens_per_window, 500_000)
        self.assertEqual(BUDGET.window_seconds, 3600.0)
        self.assertEqual(BUDGET.remaining(), 500_000)


if __name__ == "__main__":
    unittest.main()
