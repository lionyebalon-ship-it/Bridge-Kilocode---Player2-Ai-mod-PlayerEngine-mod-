"""Tests for p2k_bridge.state (journal, device codes, stats, characters)."""

import sys
import threading
import time
import unittest

sys.path.insert(0, ".")
sys.path.insert(0, "..")

from p2k_bridge.state import (  # noqa: E402
    JOURNAL_MAXLEN,
    BridgeState,
    DEFAULT_VERIFICATION_BASE,
    _clean_preview,
    _non_negative_int,
)


class NormalizeHelpersTests(unittest.TestCase):
    def test_non_negative_int_coercion(self):
        self.assertEqual(_non_negative_int(5), 5)
        self.assertEqual(_non_negative_int("42"), 42)
        self.assertEqual(_non_negative_int(3.9), 3)  # int() truncates
        self.assertEqual(_non_negative_int(-5), 0)
        self.assertEqual(_non_negative_int(0), 0)
        self.assertEqual(_non_negative_int("abc"), 0)  # default
        self.assertEqual(_non_negative_int("abc", 7), 7)
        self.assertEqual(_non_negative_int(None), 0)

    def test_clean_preview_collapses_and_truncates(self):
        self.assertEqual(_clean_preview("a\n\nb\t c"), "a b c")
        self.assertEqual(_clean_preview("  padded  "), "padded")
        self.assertEqual(len(_clean_preview("x" * 200)), 60)
        self.assertEqual(_clean_preview(None), "")
        self.assertEqual(_clean_preview(12345), "12345")


class RecordRequestTests(unittest.TestCase):
    def setUp(self):
        self.state = BridgeState()

    def test_record_and_recent_newest_first(self):
        self.state.record_request("ok", 100, 10, 20, 30, "first")
        self.state.record_request("ok", 200, 1, 2, 3, "second")
        recent = self.state.recent_requests(20)
        self.assertEqual(len(recent), 2)
        self.assertEqual(recent[0]["preview"], "second")  # newest first
        self.assertEqual(recent[1]["preview"], "first")
        entry = recent[0]
        self.assertEqual(
            sorted(entry.keys()),
            sorted(
                [
                    "ts",
                    "status",
                    "duration_ms",
                    "prompt_tokens",
                    "completion_tokens",
                    "total_tokens",
                    "preview",
                ]
            ),
        )
        self.assertEqual(entry["duration_ms"], 200)
        self.assertEqual(entry["total_tokens"], 3)

    def test_record_request_normalization(self):
        self.state.record_request("", -5, -1, -2, -3, "a\n\nb   c" + "y" * 80)
        entry = self.state.recent_requests(1)[0]
        self.assertEqual(entry["status"], "ok")  # empty -> ok
        self.assertEqual(entry["duration_ms"], 0)  # floored
        self.assertEqual(entry["prompt_tokens"], 0)
        self.assertEqual(entry["completion_tokens"], 0)
        self.assertEqual(entry["total_tokens"], 0)
        self.assertTrue(entry["preview"].startswith("a b c"))
        self.assertEqual(len(entry["preview"]), 60)

    def test_record_request_status_types(self):
        self.state.record_request(None, 1)
        self.assertEqual(self.state.recent_requests(1)[0]["status"], "ok")
        self.state.record_request("  rate_limited  ", 1)
        self.assertEqual(self.state.recent_requests(1)[0]["status"], "rate_limited")

    def test_journal_maxlen(self):
        # send JOURNAL_MAXLEN + 50 requests; the deque keeps the LAST 100
        total = JOURNAL_MAXLEN + 50
        for i in range(total):
            self.state.record_request("ok", i, preview=f"m{i}")
        recent = self.state.recent_requests(100)
        self.assertEqual(len(recent), JOURNAL_MAXLEN)
        # newest first, so the oldest survivor sits at the end
        self.assertEqual(recent[0]["preview"], "m%d" % (total - 1))
        self.assertEqual(recent[-1]["preview"], "m%d" % (total - JOURNAL_MAXLEN))

    def test_recent_requests_n_clamping(self):
        self.state.record_request("ok", 1, preview="a")
        self.state.record_request("ok", 2, preview="b")
        self.assertEqual(len(self.state.recent_requests(0)), 1)  # -> max(0,1)
        self.assertEqual(len(self.state.recent_requests(-5)), 1)
        self.assertEqual(len(self.state.recent_requests("bogus")), 2)  # -> 20 default
        self.assertEqual(len(self.state.recent_requests(1000)), 2)  # capped at 100

    def test_recent_entries_are_copies(self):
        self.state.record_request("ok", 1, preview="a")
        entry = self.state.recent_requests(1)[0]
        entry["preview"] = "mutated"
        self.assertEqual(self.state.recent_requests(1)[0]["preview"], "a")

    def test_snapshot_contents(self):
        self.state.increment("chat_requests", 3)
        self.state.record_request("ok", 5, 1, 2, 3, "x")
        snap = self.state.snapshot()
        self.assertEqual(snap["chat_requests"], 3)
        self.assertIn("started_at", snap)
        self.assertIn("uptime_seconds", snap)
        self.assertGreaterEqual(snap["uptime_seconds"], 0.0)
        self.assertEqual(len(snap["recent_requests"]), 1)
        self.assertEqual(snap["recent_requests"][0]["preview"], "x")

    def test_thread_safety(self):
        errors = []

        def worker():
            try:
                for i in range(50):
                    self.state.record_request("ok", i, preview="t")
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(self.state.recent_requests(100)), JOURNAL_MAXLEN)


class DeviceCodeTests(unittest.TestCase):
    def setUp(self):
        self.state = BridgeState()

    def test_issue_shape_and_one_shot_consume(self):
        code, uri, interval = self.state.issue_device_code("game-key")
        self.assertTrue(code)
        self.assertIn("code=" + code, uri)
        self.assertTrue(uri.startswith(DEFAULT_VERIFICATION_BASE))
        self.assertEqual(interval, 5)
        self.assertEqual(self.state.consume_device_code(code), "game-key")
        self.assertIsNone(self.state.consume_device_code(code))  # one-shot
        self.assertIsNone(self.state.consume_device_code("never-issued"))

    def test_issue_empty_client_id(self):
        code, _uri, _ = self.state.issue_device_code("")
        self.assertEqual(self.state.consume_device_code(code), "")

    def test_codes_are_unique(self):
        codes = {self.state.issue_device_code("c")[0] for _ in range(50)}
        self.assertEqual(len(codes), 50)

    def test_verification_base_override(self):
        self.state.set_verification_base("http://127.0.0.1:4315/verify")
        code, uri, _ = self.state.issue_device_code("c")
        self.assertTrue(uri.startswith("http://127.0.0.1:4315/verify"))
        self.assertIn(code, uri)


class CharactersAndCountersTests(unittest.TestCase):
    def setUp(self):
        self.state = BridgeState()

    def test_characters_property_returns_copy(self):
        self.state.set_characters([{"name": "A"}])
        got = self.state.characters
        got.append({"name": "B"})
        self.assertEqual(len(self.state.characters), 1)  # the list itself is copied
        # shallow copy: the character dicts are shared references, so in-place
        # mutation of an entry does propagate back to the state
        got[0]["name"] = "mutated"
        self.assertEqual(self.state.characters[0]["name"], "mutated")

    def test_set_characters_filters_junk(self):
        self.state.set_characters(None)
        self.assertEqual(self.state.characters, [])
        self.state.set_characters("not a list")
        self.assertEqual(self.state.characters, [])
        self.state.set_characters([{"name": "ok"}, "junk", None, 5])
        self.assertEqual(len(self.state.characters), 1)

    def test_increment_guards(self):
        self.state.increment("chat_requests", 2)
        self.assertEqual(self.state.stats["chat_requests"], 2)
        self.state.increment("chat_requests", -5)  # may go negative
        self.assertEqual(self.state.stats["chat_requests"], -3)
        self.state.increment("brand_new_counter", 4)  # unknown -> set
        self.assertEqual(self.state.stats["brand_new_counter"], 4)
        before = self.state.stats["started_at"]
        self.state.increment("started_at", 999)  # no-op by design
        self.assertEqual(self.state.stats["started_at"], before)

    def test_device_codes_property_is_copy(self):
        code, _uri, _ = self.state.issue_device_code("c")
        got = self.state.device_codes
        got.pop(code)
        self.assertIn(code, self.state.device_codes)


if __name__ == "__main__":
    unittest.main()
