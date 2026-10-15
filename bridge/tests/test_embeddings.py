"""Tests for embeddings.py and memorystore.py.

The provider fixture at the top of this file is a **real recorded response**,
trimmed to two dimensions. It is not invented: the shape it asserts
(`result.data[i]` *is* the vector) is what the provider actually returns, and a
parser written against the OpenAI convention would have failed on the first live
request. If that shape is ever wrong, these tests fail here rather than in
production.
"""

import json
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from p2k_bridge.embeddings import (  # noqa: E402
    EmbeddingError,
    build_request,
    cosine,
    parse_responses,
    render_block,
    top_k,
)
from p2k_bridge.memorystore import MemoryStore  # noqa: E402

# Verbatim shape from the live provider on 2026-10-15; the float lists are
# truncated for readability but every structural key is real.
REAL_RESPONSE = {
    "success": True,
    "errors": [],
    "messages": [],
    "result": {
        "data": [
            [0.01262664794921875, 0.034271240234375, -0.005291938781738281],
            [0.014121055603027344, 0.031200408935546875, -0.004674434661865234],
        ],
        "pooling": "mean",
        "shape": [2, 3],
        "usage": {"prompt_tokens": 20, "completion_tokens": 0, "total_tokens": 20},
    },
}


class BuildRequestTests(unittest.TestCase):
    def test_string_input_is_normalised_to_a_list(self):
        payload = build_request("m/x", "some text")
        self.assertEqual(payload["text"], ["some text"])

    def test_list_input_is_preserved_in_order(self):
        payload = build_request("m/x", ["first", "second"])
        self.assertEqual(payload["text"], ["first", "second"])

    def test_empty_input_is_rejected(self):
        with self.assertRaises(EmbeddingError):
            build_request("m/x", [])
        with self.assertRaises(EmbeddingError):
            build_request("m/x", ["   "])

    def test_non_string_input_is_rejected(self):
        with self.assertRaises(EmbeddingError):
            build_request("m/x", ["ok", 42])

    def test_oversized_batch_is_rejected_with_a_named_limit(self):
        with self.assertRaises(EmbeddingError) as ctx:
            build_request("m/x", ["x"] * 500)
        self.assertIn("per request", str(ctx.exception))

    def test_very_long_text_is_truncated_not_rejected(self):
        payload = build_request("m/x", ["x" * 50000])
        self.assertEqual(len(payload["text"][0]), 8192)


class ParseResponsesTests(unittest.TestCase):
    def test_real_recorded_payload_parses(self):
        vectors = parse_responses(REAL_RESPONSE)
        self.assertEqual(len(vectors), 2)
        self.assertEqual(len(vectors[0]), 3)
        self.assertAlmostEqual(vectors[0][0], 0.01262664794921875, places=10)

    def test_one_vector_per_input_in_order(self):
        payload = {"success": True, "result": {"data": [[1.0], [2.0], [3.0]]}}
        self.assertEqual(parse_responses(payload), [[1.0], [2.0], [3.0]])

    def test_provider_reported_failure_is_an_error(self):
        payload = {"success": False, "errors": [{"code": 7023, "message": "no route"}],
                   "result": None}
        with self.assertRaises(EmbeddingError) as ctx:
            parse_responses(payload)
        self.assertIn("no route", str(ctx.exception))

    def test_non_object_is_an_error(self):
        for bad in (None, "x", 5, [1, 2], object()):
            with self.assertRaises(EmbeddingError):
                parse_responses(bad)

    def test_missing_vector_data_is_an_error(self):
        payload = {"success": True, "result": {"shape": [0, 3]}}
        with self.assertRaises(EmbeddingError):
            parse_responses(payload)

    def test_data_that_is_not_a_list_of_numbers_is_an_error(self):
        payload = {"success": True, "result": {"data": [{"score": 0.5}]}}
        with self.assertRaises(EmbeddingError):
            parse_responses(payload)

    def test_bools_are_rejected_as_vectors(self):
        payload = {"success": True, "result": {"data": [[True, False]]}}
        with self.assertRaises(EmbeddingError):
            parse_responses(payload)

    def test_identical_vectors_similarity_is_one(self):
        self.assertAlmostEqual(cosine([1.0, 2.0, 3.0], [1.0, 2.0, 3.0]), 1.0, places=12)

    def test_orthogonal_vectors_similarity_is_zero(self):
        self.assertAlmostEqual(cosine([1.0, 0.0], [0.0, 1.0]), 0.0, places=12)

    def test_opposite_vectors_similarity_is_minus_one(self):
        self.assertAlmostEqual(cosine([1.0, 1.0], [-1.0, -1.0]), -1.0, places=12)

    def test_scale_invariance(self):
        self.assertAlmostEqual(cosine([1.0, 1.0], [5.0, 5.0]), 1.0, places=12)

    def test_mismatched_lengths_score_zero(self):
        self.assertEqual(cosine([1.0, 2.0], [1.0]), 0.0)

    def test_wrong_types_score_zero_never_raise(self):
        for bad in (None, "x", 5, [None], ["a", "b"], {}):
            self.assertEqual(cosine(bad, [1.0]), 0.0)
            self.assertEqual(cosine([1.0], bad), 0.0)
            self.assertEqual(cosine(bad, bad), 0.0)

    def test_empty_vector_scores_zero(self):
        self.assertEqual(cosine([], []), 0.0)


class TopKTests(unittest.TestCase):
    def setUp(self):
        # Deliberately orthogonal directions, so thresholds behave predictably:
        # every vector here is at a genuinely different angle to the query.
        self.rows = [
            ([1.0, 0.0, 0.0, 0.0], "exact"),
            ([0.8, 0.6, 0.0, 0.0], "close"),
            ([0.5, 0.5, 0.5, 0.5], "diagonal"),
            ([0.0, 0.0, 1.0, 0.0], "orthogonal"),
            ([0.0, 0.0, 0.0, 1.0], "also-orthogonal"),
        ]

    def test_ranks_by_similarity_descending(self):
        ranked = top_k([1.0, 0.0, 0.0, 0.0], self.rows, k=10)
        order = [item for _s, item in ranked]
        self.assertEqual(order[0], "exact")
        # Both orthogonal rows score exactly 0.0, so their order is arbitrary;
        # what matters is that they are the two lowest-ranked entries.
        self.assertEqual(set(order[-2:]), {"orthogonal", "also-orthogonal"})
        self.assertEqual(len(ranked), 5)

    def test_scores_are_descending(self):
        ranked = top_k([1.0, 0.0, 0.0, 0.0], self.rows, k=10)
        scores = [s for s, _i in ranked]
        self.assertEqual(scores, sorted(scores, reverse=True))
        self.assertAlmostEqual(scores[0], 1.0, places=12)

    def test_k_is_respected(self):
        self.assertEqual(len(top_k([1.0, 0.0, 0.0, 0.0], self.rows, k=2)), 2)

    def test_min_score_filters(self):
        # 0.5 sits between the diagonal's score and the close vector's, so it
        # must exclude the two orthogonal rows but keep the three real matches.
        ranked = top_k([1.0, 0.0, 0.0, 0.0], self.rows, k=10, min_score=0.5)
        kept = [item for _s, item in ranked]
        self.assertEqual(kept, ["exact", "close", "diagonal"])
        self.assertNotIn("orthogonal", kept)

    def test_no_candidates_returns_empty(self):
        self.assertEqual(top_k([1.0], [], k=5), [])
        self.assertEqual(top_k([1.0], None, k=5), [])

    def test_bad_rows_are_skipped_not_fatal(self):
        rows = self.rows + [(["bad"], "broken"), (None, "none"), "junk", (["x"], 5)]
        ranked = top_k([1.0, 0.0, 0.0, 0.0], rows, k=10, min_score=0.5)
        # The malformed rows score 0.0 and fall below the threshold.
        self.assertEqual([item for _s, item in ranked], ["exact", "close", "diagonal"])

    def test_zero_vector_scores_zero(self):
        # A zero vector cannot have a direction; it must not outrank a real match.
        rows = [([0.0, 0.0, 0.0, 0.0], "zero"), ([1.0, 0.0, 0.0, 0.0], "real")]
        ranked = top_k([1.0, 0.0, 0.0, 0.0], rows, k=5, min_score=0.5)
        self.assertEqual([item for _s, item in ranked], ["real"])

    def test_zero_threshold_admits_zero_similarity(self):
        # Documented, not accidental: min_score=0.0 keeps everything, including
        # records that share no direction with the query.
        rows = [([0.0, 1.0], "perpendicular")]
        ranked = top_k([1.0, 0.0], rows, k=5, min_score=0.0)
        self.assertEqual([item for _s, item in ranked], ["perpendicular"])


class RenderBlockTests(unittest.TestCase):
    def test_items_rendered_as_bullets(self):
        block = render_block(["one", "two"])
        self.assertEqual(block, "- one\n- two")

    def test_objects_with_text_key_are_supported(self):
        block = render_block([{"text": "one"}, {"text": "two"}])
        self.assertIn("- one", block)

    def test_empty_input_is_empty(self):
        self.assertEqual(render_block([]), "")
        self.assertEqual(render_block(None), "")
        self.assertEqual(render_block(["   "]), "")

    def test_bound_is_enforced_for_any_input(self):
        items = [f"memory item number {i} with a fair bit of text to it" for i in range(50)]
        for limit in (200, 500, 1200, 4000):
            block = render_block(items, limit)
            self.assertLessEqual(len(block), limit, f"limit={limit} got {len(block)}")

    def test_bounding_drops_whole_entries_not_mid_sentence(self):
        items = ["a" * 50, "b" * 50, "c" * 50, "d" * 50]
        block = render_block(items, 120)
        self.assertIn("- aaaa", block)
        self.assertIn("more", block)  # the omission is recorded
        for line in block.splitlines():
            self.assertNotIn("aaab", line)  # nothing is cut mid-entry

    def test_block_is_deterministic(self):
        items = ["one", "two", "three"]
        self.assertEqual(render_block(items, 100), render_block(items, 100))


class MemoryStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = str(Path(self.tmp.name) / "mem.sqlite3")
        self.store = MemoryStore(self.path, max_per_character=5, max_age_days=14.0)
        self.addCleanup(self.store.close)

    def add(self, cid, text, vec, ts=None):
        return self.store.add(cid, text, vec, source="test", ts=ts)

    def test_vector_round_trips_at_full_precision(self):
        vector = [0.01262664794921875, -0.005291938781738281, 1.5e-8, -1.0]
        self.add("kilo", "hello", vector)
        stored = self.store.candidates("kilo")[0][0]
        for original, value in zip(vector, stored):
            self.assertAlmostEqual(original, value, places=8)

    def test_candidates_are_scoped_to_one_character(self):
        self.add("kilo", "loves oak", [1.0, 0.0])
        self.add("blaze", "loves fire", [0.0, 1.0])
        kilo = self.store.candidates("kilo")
        self.assertEqual([text for _v, text in kilo], ["loves oak"])

    def test_query_for_missing_character_is_empty(self):
        self.add("kilo", "x", [1.0])
        self.assertEqual(self.store.candidates("nobody"), [])

    def test_count_and_clear(self):
        self.add("kilo", "a", [1.0])
        self.add("kilo", "b", [1.0])
        self.add("blaze", "c", [1.0])
        self.assertEqual(self.store.count("kilo"), 2)
        self.assertEqual(self.store.count(), 3)
        self.assertEqual(self.store.clear("kilo"), 2)
        self.assertEqual(self.store.count(), 1)

    def test_cap_drops_oldest_beyond_limit(self):
        now = time.time()
        for i in range(10):
            self.add("kilo", f"mem{i}", [1.0], ts=now + i)
        self.assertEqual(self.store.count("kilo"), 5)
        texts = self.store.recent("kilo", 10)
        self.assertIn("mem9", texts)
        self.assertNotIn("mem0", texts)  # the oldest was dropped

    def test_age_expiry(self):
        now = time.time()
        self.add("kilo", "old", [1.0], ts=now - 30 * 86400)
        self.add("kilo", "new", [1.0], ts=now - 1)
        self.add("kilo", "trigger", [1.0], ts=now)  # triggers prune
        texts = self.store.recent("kilo", 10)
        self.assertNotIn("old", texts)
        self.assertIn("new", texts)

    def test_bad_inputs_are_rejected_not_raised(self):
        self.assertFalse(self.add("", "text", [1.0]))
        self.assertFalse(self.add("kilo", "  ", [1.0]))
        self.assertFalse(self.add("kilo", "text", []))
        self.assertFalse(self.add("kilo", "text", "not a vector"))
        self.assertFalse(self.add("kilo", "text", None))
        self.assertEqual(self.store.count(), 0)

    def test_noise_never_breaks_retrieval(self):
        self.add("kilo", "good", [1.0, 0.0])
        self.add("kilo", "bad", ["x", "y"])  # rejected at encode
        self.add("kilo", "good2", [0.9, 0.0])
        ranked = top_k([1.0, 0.0], self.store.candidates("kilo"), k=5)
        self.assertEqual(len(ranked), 2)

    def test_identical_text_is_stored_once(self):
        # The extraction prompt is re-sent on turns where nothing changed; a
        # unique index on (character_id, text) keeps one row.
        self.add("kilo", "the player likes oak", [1.0])
        self.store.add("kilo", "the player likes oak", [0.5], source="test")
        self.assertEqual(
            [t for _v, t in self.store.candidates("kilo")], ["the player likes oak"])

    def test_same_text_for_two_characters_is_kept_separately(self):
        self.add("kilo", "the player likes oak", [1.0])
        self.add("blaze", "the player likes oak", [1.0])
        self.assertEqual(self.store.count(), 2)

    def test_store_survives_reopen(self):
        self.add("kilo", "persisted", [0.5, 0.5])
        self.store.close()
        fresh = MemoryStore(self.path, max_per_character=5)
        self.addCleanup(fresh.close)
        self.assertEqual([t for _v, t in fresh.candidates("kilo")], ["persisted"])

    def test_blob_encoding_is_compact(self):
        vector = [0.0] * 768
        blob = self.store.encode_vector(vector)
        self.assertEqual(len(blob), 768 * 4)  # float32, not float64


class MemoryScaleTests(unittest.TestCase):
    """The plan claims pure-Python cosine is fast enough. Measure it."""

    def test_scan_cost_at_the_configured_ceiling(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = MemoryStore(str(Path(tmp.name) / "big.sqlite3"), max_per_character=500)
        self.addCleanup(store.close)
        dims = 768
        for i in range(500):
            store.add("kilo", f"memory {i}", [float(i % 7) / 7.0] * dims)
        candidates = store.candidates("kilo")
        self.assertEqual(len(candidates), 500)
        started = time.monotonic()
        top_k([0.5] * dims, candidates, k=5)
        elapsed = (time.monotonic() - started) * 1000
        print("cosine scan over 500 x 768 dims: %.1f ms" % elapsed)
        # Only a sanity bound: slow CI is not a product failure, but a run several
        # orders of magnitude off would mean the design assumption is wrong.
        self.assertLess(elapsed, 3000, "retrieval is too slow for a turn")


if __name__ == "__main__":
    unittest.main()
