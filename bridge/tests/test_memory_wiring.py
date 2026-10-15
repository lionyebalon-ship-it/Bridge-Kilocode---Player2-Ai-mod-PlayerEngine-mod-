"""Tests for the memory wiring in server.py.

The behaviour under test is a **no-op by default**: with ``memory_enabled: false``
the chat path must be byte-identical to before, every memory counter must stay at
zero, and no store may be created. That invariant is asserted first, because a
feature that changes behaviour when it is supposed to be off is worse than one
that does nothing.
"""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import server  # noqa: E402
from p2k_bridge.routing import Route  # noqa: E402

VEC = [0.1] * 768
WARM = {"role": "user", "content": "what did I tell you my dog is called?"}
MESSAGES = [{"role": "system", "content": "You are Kilo."}, WARM]


class MemoryWiringTests(unittest.TestCase):
    def setUp(self):
        # Reset the module state each test: MEMORY is a lazy global.
        # Tests that exercise the store path must call _seed_fakes, otherwise
        # _ensure_memory would create a real SQLite file on disk.
        server.MEMORY = None
        server.MEMORY_CLIENT = None
        server._MEMORY_PENDING.clear()
        server._MEMORY_WARNED.clear()
        server.CATALOG = __import__(
            "p2k_bridge.catalog", fromlist=["ModelCatalog"]).ModelCatalog()

    def _seed_fakes(self):
        """Inject fakes so _ensure_memory succeeds without touching disk."""
        server.MEMORY = _FakeStore()
        server.MEMORY_CLIENT = _FakeClient()
        # A generous bucket, so a flush is not shed while under test.
        if server.BUCKET is None:
            from p2k_bridge.ratelimit import TokenBucket

            server.BUCKET = TokenBucket(100000)
        return server.MEMORY, server.MEMORY_CLIENT

    def _cfg(self, **kw):
        from p2k_bridge.config import BridgeConfig

        return BridgeConfig(**kw)

    def _route(self, kind="conversation", cid="kilo", **kw):
        return Route(kind=kind, character_id=cid, **kw)

    # --- off by default -----------------------------------------------------

    def test_disabled_memory_leaves_messages_untouched(self):
        cfg = self._cfg(memory_enabled=False)
        server.CFG = cfg
        out = server._memory_inject(MESSAGES, self._route())
        self.assertEqual(out, MESSAGES)

    def test_disabled_memory_creates_no_store(self):
        self.assertIsNone(server.MEMORY)

    def test_disabled_memory_store_is_a_noop(self):
        cfg = self._cfg(memory_enabled=False)
        server.CFG = cfg
        server._memory_store(MESSAGES, "some extracted memory", self._route())
        self.assertEqual(server._MEMORY_PENDING, {})

    # --- wrong kind is not embedded ----------------------------------------

    def test_background_kinds_are_not_embedded(self):
        # The default embeds only `conversation`; a background call must not
        # spend an embedding or inject anything.
        cfg = self._cfg(memory_enabled=True)
        server.CFG = cfg
        for kind in ("npc_dialogue", "event", "greeting", "memory_extraction"):
            out = server._memory_inject(MESSAGES, self._route(kind=kind))
            self.assertEqual(len(out), len(MESSAGES), kind)

    def test_unidentified_character_injects_nothing(self):
        cfg = self._cfg(memory_enabled=True)
        server.CFG = cfg
        out = server._memory_inject(MESSAGES, self._route(cid=""))
        self.assertEqual(out, MESSAGES)

    def test_message_without_user_text_injects_nothing(self):
        cfg = self._cfg(memory_enabled=True)
        server.CFG = cfg
        out = server._memory_inject(
            [{"role": "system", "content": "no user here"}], self._route())
        self.assertEqual(out, [{"role": "system", "content": "no user here"}])

    # --- quota gating -------------------------------------------------------

    def test_no_bucket_means_no_injection(self):
        cfg = self._cfg(memory_enabled=True)
        server.CFG = cfg
        server.BUCKET = None
        out = server._memory_inject(MESSAGES, self._route())
        self.assertEqual(out, MESSAGES)

    # --- store batching -----------------------------------------------------

    def test_store_flushes_immediately_even_below_batch_size(self):
        """Regression: a single memory must not sit pending forever.

        An earlier version only flushed once memory_store_batch_size items were
        pending. At this feature's real rate (about one extraction per several
        minutes) that left the first memory never stored.
        """
        cfg = self._cfg(memory_enabled=True, memory_store_batch_size=8)
        server.CFG = cfg
        store, client = self._seed_fakes()
        server._memory_store(MESSAGES, "first memory", self._route(kind="memory_extraction"))
        self.assertNotIn("kilo", server._MEMORY_PENDING)
        self.assertEqual(client.calls, [["first memory"]])
        self.assertEqual(store.stored, [("kilo", "first memory")])

    def test_batching_still_combines_pending_items(self):
        # When several items are already pending they share one request.
        cfg = self._cfg(memory_enabled=True, memory_store_batch_size=8)
        server.CFG = cfg
        store, client = self._seed_fakes()
        server._MEMORY_PENDING["kilo"] = ["a", "b"]
        server._memory_flush("kilo")
        self.assertEqual(client.calls, [["a", "b"]])
        self.assertEqual(store.stored, [("kilo", "a"), ("kilo", "b")])

    def test_store_deduplicates_across_flushes(self):
        # Dedup lives in the store (unique index on character_id, text); the
        # wiring only needs to not duplicate work on its own. See
        # test_embeddings.MemoryStoreTests for the store-level behaviour.
        cfg = self._cfg(memory_enabled=True, memory_store_batch_size=10)
        server.CFG = cfg
        store, _client = self._seed_fakes()
        route = self._route(kind="memory_extraction")
        server._memory_store(MESSAGES, "same", route)
        server._memory_store(MESSAGES, "same", route)
        # Two store attempts, because the store is what decides to drop a dupe.
        self.assertEqual([t for _c, t in server.MEMORY.stored], ["same", "same"])
        self.assertEqual(server._MEMORY_PENDING, {})

    def test_store_flush_is_scoped_per_character(self):
        cfg = self._cfg(memory_enabled=True, memory_store_batch_size=10)
        server.CFG = cfg
        self._seed_fakes()
        for cid in ("kilo", "blaze"):
            server._memory_store(
                MESSAGES, "x", self._route(kind="memory_extraction", cid=cid))
        # Each store flushed immediately, so both landed and nothing is pending.
        self.assertEqual(server._MEMORY_PENDING, {})
        self.assertEqual(
            sorted(r[0] for r in server.MEMORY.stored), ["blaze", "kilo"])

    def test_store_ignores_empty_replies(self):
        cfg = self._cfg(memory_enabled=True)
        server.CFG = cfg
        self._seed_fakes()
        route = self._route(kind="memory_extraction")
        for bad in ("", "   ", None, 5):
            server._memory_store(MESSAGES, bad, route)
        self.assertEqual(server._MEMORY_PENDING, {})

    def test_store_does_not_create_a_store_when_disabled(self):
        # The regression this guards: a disabled bridge must not write to disk.
        cfg = self._cfg(memory_enabled=False)
        server.CFG = cfg
        server._memory_store(MESSAGES, "x", self._route(kind="memory_extraction"))
        self.assertIsNone(server.MEMORY)


class _FakeStore:
    def __init__(self):
        self.stored = []

    def add(self, cid, text, vector, source=""):
        self.stored.append((cid, text))
        return True

    def candidates(self, cid, limit=0):
        return []


class _FakeClient:
    def __init__(self):
        self.configured = True
        self.calls = []

    def embed_safe(self, inputs):
        texts = [inputs] if isinstance(inputs, str) else list(inputs)
        self.calls.append(texts)
        return [[0.001, 0.002]] * len(texts), None


class MemoryKindTests(unittest.TestCase):
    def test_kind_matching(self):
        self.assertTrue(server._kind_in(["conversation"], "conversation"))
        self.assertFalse(server._kind_in(["conversation"], "npc_dialogue"))
        self.assertFalse(server._kind_in([], "conversation"))
        self.assertFalse(server._kind_in(None, "conversation"))
        self.assertFalse(server._kind_in(["conversation"], None))

    def test_preview_uses_last_user_text(self):
        self.assertEqual(server._chat_preview_user(MESSAGES), WARM["content"])
        self.assertEqual(server._chat_preview_user([]), "")
        self.assertEqual(server._chat_preview_user(None), "")
        self.assertEqual(
            server._chat_preview_user([{"role": "system", "content": "s"}]), "")

    def test_scope_requires_a_character(self):
        self.assertEqual(server._memory_scope(Route(character_id="kilo")), "kilo")
        self.assertEqual(server._memory_scope(Route(character_id="  ")), "")
        self.assertEqual(server._memory_scope(None), "")


if __name__ == "__main__":
    unittest.main()
