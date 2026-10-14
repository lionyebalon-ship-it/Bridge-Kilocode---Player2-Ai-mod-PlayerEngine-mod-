"""Coverage-gap tests: server.py routes and helpers not exercised by selftest.

Everything runs against in-process bridge instances on ephemeral ports with a
localhost Kilo stub. No real gateway calls, no real API keys.
"""

import http.client
import http.server
import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, ".")
sys.path.insert(0, "..")

import server  # noqa: E402
from p2k_bridge.catalog import ModelCatalog  # noqa: E402
from p2k_bridge.config import BridgeConfig  # noqa: E402
from p2k_bridge.routing import CONVERSATION, Route  # noqa: E402
from p2k_bridge.state import BridgeState  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
CERTS = REPO_ROOT / "tls" / "certs"

NP_MESSAGES = [
    {"role": "system", "content": "You are a Minecraft companion AI. Valid commands: get, follow, stop."},
    {"role": "user", "content": '{"userMessage":"hello","worldStatus":"day","agentStatus":"health=20"}'},
]

STUB_OK = {
    "choices": [
        {"index": 0, "message": {"role": "assistant", "content": '{"reason":"r","command":"stop","message":"hi"}'},
         "finish_reason": "stop"}
    ]
}


def _stub_handler_factory(behavior, recorder):
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _go(self):
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            recorder.setdefault("requests", []).append({"path": self.path, "body": body.decode("utf-8", "replace")})
            if behavior.get("hang"):
                time.sleep(behavior["hang"])
            status = behavior.get("status", 200)
            payload = behavior.get("body", STUB_OK)
            data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        do_GET = _go
        do_POST = _go

    return Handler


class KiloStub:
    def __init__(self, behavior=None):
        behavior = behavior or {"status": 200, "body": STUB_OK}
        self.recorder = {}
        self.server = http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0), _stub_handler_factory(behavior, self.recorder)
        )
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def start(self):
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


def make_cfg(**overrides):
    stub = overrides.pop("kilo_stub", None)
    cfg = BridgeConfig(
        tls_enabled=False,
        requests_per_hour=100000,
        token_budget_per_window=0,
        request_timeout_seconds=5.0,
        characters=[{"id": "kilo", "name": "Kilo", "short_name": "KILO",
                     "greeting": "hi", "description": "d", "voice_ids": [], "meta": {"skin_url": ""}}],
    )
    if stub is not None:
        cfg.kilo_base_url = stub.base_url
    if overrides.get("http_port") == 0:
        overrides["http_port"] = 0
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


class BridgeHarness(unittest.TestCase):
    """Boots an in-process bridge on an ephemeral port with helpers."""

    def start_bridge(self, **overrides):
        self.stub = KiloStub(overrides.pop("kilo_behavior", None))
        self.stub.start()
        self.addCleanup(self.stub.stop)
        cfg = make_cfg(kilo_stub=self.stub, **overrides)
        server.init(cfg)
        self.srv = server.build_server(cfg, "127.0.0.1", 0)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.shutdown)
        self.addCleanup(self.srv.server_close)
        return self.port

    def request(self, method, path, body=None, timeout=30):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        data = None
        headers = {}
        if body is not None:
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        try:
            conn.request(method, path, body=data, headers=headers)
            resp = conn.getresponse()
            raw = resp.read()
            return resp.status, raw
        finally:
            conn.close()

    def post_chat(self, payload=None, timeout=60):
        return self.request("POST", "/v1/chat/completions", payload if payload is not None else {"messages": NP_MESSAGES}, timeout=timeout)

    def stats(self):
        status, raw = self.request("GET", "/bridge/stats")
        return json.loads(raw)


class RoutingWiringTests(BridgeHarness):
    """Routing must be observable and must never break the chat path."""

    def _messages(self, user_text, system_text=None):
        messages = []
        if system_text:
            messages.append({"role": "system", "content": system_text})
        messages.append({"role": "user", "content": user_text})
        return messages

    def test_no_overrides_sends_the_global_model_and_temperature(self):
        self.start_bridge()
        self.post_chat({"messages": self._messages('{"userMessage":"User Message: [a]: hi"}')})
        body = json.loads(self.stub.recorder["requests"][-1]["body"])
        self.assertEqual(body["model"], "nvidia/nemotron-3-super-120b-a12b:free")
        self.assertIn("temperature", body)

    def _write_roster(self, characters):
        """Create a temp config dir holding characters.json and return its path.

        characters.json is the served roster and takes precedence over the
        ``characters`` array in config.json, so this is the path a real override
        actually travels. The temp dir must be passed as ``config_path`` to
        start_bridge, otherwise the loader falls back to bridge/characters.json.
        """
        import tempfile

        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        path = Path(self._tmpdir.name) / "characters.json"
        path.write_text(json.dumps({"characters": characters}), encoding="utf-8")
        # characters.json is looked up in the *parent* of the config path, so
        # return a config.json path in this directory (the file need not exist).
        return str(Path(self._tmpdir.name) / "config.json")

    def test_character_model_override_reaches_the_gateway(self):
        config_path = self._write_roster(
            [
                {
                    "id": "kilo",
                    "name": "Kilo",
                    "short_name": "KILO",
                    "greeting": "hi",
                    "description": "d",
                    "model": "nvidia/nemotron-3.5-lightning:free",
                    "temperature": 1.1,
                    "max_tokens": 4096,
                }
            ]
        )
        self.start_bridge(config_path=config_path)
        self.post_chat(
            {"messages": self._messages(
                '{"userMessage":"User Message: [a]: hi"}',
                "Your character's name is Kilo.",
            )}
        )
        body = json.loads(self.stub.recorder["requests"][-1]["body"])
        self.assertEqual(body["model"], "nvidia/nemotron-3.5-lightning:free")
        self.assertEqual(body["temperature"], 1.1)

    def test_missing_character_model_degrades_to_global(self):
        # A character pinned to a model that has left the catalog must fall back
        # to the global model rather than fail the request.
        config_path = self._write_roster(
            [{"id": "kilo", "name": "Kilo", "short_name": "KILO",
              "model": "not/a-real-model:free"}]
        )
        self.start_bridge(config_path=config_path, model="global/fallback:free")
        # A catalog that knows the global model but not the character's.
        server.CATALOG = ModelCatalog.from_payload(
            {"data": [{"id": "global/fallback:free", "context_length": 1000,
                       "supported_parameters": ["max_tokens", "temperature"]}]}
        )
        status, raw = self.post_chat(
            {"messages": self._messages(
                '{"userMessage":"User Message: [a]: hi"}',
                "Your character's name is Kilo.",
            )}
        )
        self.assertEqual(status, 200)
        self.assertTrue(raw)
        body = json.loads(self.stub.recorder["requests"][-1]["body"])
        self.assertEqual(body["model"], "global/fallback:free")

    def test_config_characters_are_shadowed_by_characters_json(self):
        # Regression for the silent-precedence footgun: config.json overrides
        # are ignored once characters.json exists, and that must be visible.
        config_path = self._write_roster(
            [{"id": "kilo", "name": "Kilo", "short_name": "KILO",
              "model": "file/model:free"}]
        )
        with self.assertLogs("p2k.bridge", level="WARNING") as captured:
            self.start_bridge(
                config_path=config_path,
                characters=[{"id": "kilo", "name": "Kilo", "short_name": "KILO",
                             "model": "config/model:free"}],
            )
        warnings = "\n".join(captured.output)
        self.assertIn("are ignored", warnings)
        self.assertIn("config/model:free", warnings)

    def test_journal_records_kind_character_and_model(self):
        config_path = self._write_roster(
            [{"id": "kilo", "name": "Kilo", "short_name": "KILO",
              "model": "nvidia/nemotron-3.5-lightning:free"}]
        )
        self.start_bridge(config_path=config_path)
        self.post_chat(
            {"messages": self._messages(
                '{"userMessage":"User Message: [a]: hi"}',
                "Your character's name is Kilo.",
            )}
        )
        entry = json.loads(self.request("GET", "/bridge/stats")[1])["recent_requests"][0]
        self.assertEqual(entry["kind"], "conversation")
        self.assertEqual(entry["character_id"], "kilo")
        self.assertEqual(entry["model"], "nvidia/nemotron-3.5-lightning:free")

    def test_rate_limited_reply_still_carries_route(self):
        # The degraded envelope must not lose the routing metadata, or the logs
        # show an unattributed request.
        self.start_bridge(requests_per_hour=1)
        self.post_chat({"messages": self._messages("hello")})
        self.post_chat({"messages": self._messages("hello")})
        entries = json.loads(self.request("GET", "/bridge/stats")[1])["recent_requests"]
        self.assertEqual(entries[0]["status"], "rate_limited")
        self.assertEqual(entries[0]["kind"], "unknown")
        self.assertEqual(entries[1]["status"], "ok")

    def test_kilo_error_reply_still_carries_route(self):
        self.start_bridge(kilo_behavior={"status": 500, "body": b"upstream boom"})
        status, raw = self.post_chat({"messages": self._messages("hello")})
        self.assertEqual(status, 200)
        entry = json.loads(self.request("GET", "/bridge/stats")[1])["recent_requests"][0]
        self.assertEqual(entry["status"], "kilo_error")
        self.assertEqual(entry["kind"], "unknown")

    def test_chat_completion_without_route_is_unchanged(self):
        # Backwards compatibility for callers that do not route.
        from p2k_bridge.config import BridgeConfig
        from p2k_bridge.kilo import KiloClient

        self.start_bridge()
        cfg = BridgeConfig(model="global/model:free", temperature=0.25, max_tokens=500000)
        client = KiloClient(cfg)
        client._base_url = self.stub.base_url
        client.chat_completion(NP_MESSAGES)
        body = json.loads(self.stub.recorder["requests"][-1]["body"])
        self.assertEqual(body["model"], "global/model:free")
        self.assertEqual(body["temperature"], 0.25)

    def test_chat_completion_with_route_overrides_everything(self):
        from p2k_bridge.config import BridgeConfig
        from p2k_bridge.kilo import KiloClient

        self.start_bridge()
        cfg = BridgeConfig(model="global/model:free", temperature=0.25, max_tokens=500000)
        client = KiloClient(cfg)
        client._base_url = self.stub.base_url
        route = Route(
            kind=CONVERSATION,
            character_id="kilo",
            model="character/model:free",
            temperature=1.5,
            max_tokens=4096,
            context_limit=2000,
            max_completion_tokens=500,
        )
        client.chat_completion(NP_MESSAGES, route)
        body = json.loads(self.stub.recorder["requests"][-1]["body"])
        self.assertEqual(body["model"], "character/model:free")
        self.assertEqual(body["temperature"], 1.5)
        # max_tokens must be clamped by the per-model context window (2000) and
        # the provider's completion cap (500), not by the global 500000.
        self.assertLessEqual(body["max_tokens"], 500)

    def test_temperature_omitted_for_models_that_do_not_support_it(self):
        from p2k_bridge.config import BridgeConfig
        from p2k_bridge.kilo import KiloClient

        self.start_bridge()
        client = KiloClient(BridgeConfig(model="m", temperature=0.5))
        client._base_url = self.stub.base_url
        route = Route(kind=CONVERSATION, model="m", supports_temperature=False)
        client.chat_completion(NP_MESSAGES, route)
        body = json.loads(self.stub.recorder["requests"][-1]["body"])
        self.assertNotIn("temperature", body)
        self.assertIn("model", body)


class RouteTests(BridgeHarness):
    def test_get_root_fingerprint(self):
        self.start_bridge()
        status, raw = self.request("GET", "/")
        self.assertEqual(status, 200)
        body = json.loads(raw)
        self.assertEqual(body["service"], "p2k-bridge")

    def test_health_and_selected_characters(self):
        self.start_bridge()
        status, raw = self.request("GET", "/bridge/healthz")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(raw)["ok"])
        status, raw = self.request("GET", "/v1/selected_characters")
        self.assertEqual(status, 200)
        chars = json.loads(raw)["characters"]
        self.assertTrue(any(c["name"] == "Kilo" for c in chars))

    def test_schematics_stub(self):
        self.start_bridge()
        status, raw = self.request("POST", "/v1/minecraft/schematics/search", {"query": "castle", "max_results": 10})
        self.assertEqual(status, 200)
        body = json.loads(raw)
        self.assertEqual(body["results"], [])
        self.assertEqual(body["schematicInfos"], [])

    def test_unknown_paths(self):
        self.start_bridge()
        self.assertEqual(self.request("GET", "/nope")[0], 404)
        self.assertEqual(self.request("POST", "/nope", {"x": 1})[0], 404)

    def test_joules_endpoint(self):
        # PlayerEngine's JoulesCache GETs /v1/joules on the llm worker thread;
        # the bridge owns the real limits (token budget + 180 req/hr bucket),
        # so it must answer with a usable balance rather than 404.
        self.start_bridge()
        status, raw = self.request("GET", "/v1/joules")
        self.assertEqual(status, 200)
        body = json.loads(raw)
        self.assertIsInstance(body["joules"], int)
        self.assertGreater(body["joules"], 0)
        self.assertIsInstance(body.get("patron_tier"), str)

    def test_joules_is_not_regressed_to_404(self):
        self.start_bridge()
        self.assertNotEqual(self.request("GET", "/v1/joules")[0], 404)

    def test_device_flow_and_replay(self):
        self.start_bridge()
        status, raw = self.request("POST", "/v1/login/device/new", {"client_id": "player2-ai-npc-minecraft"})
        self.assertEqual(status, 200)
        code = json.loads(raw)["deviceCode"]
        self.assertEqual(json.loads(raw)["interval"], 5)
        status, raw = self.request("POST", "/v1/login/device/token", {"device_code": code, "client_id": "x"})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["p2Key"], "p2k-local")
        status, raw = self.request("POST", "/v1/login/device/token", {"device_code": code, "client_id": "x"})
        self.assertEqual(status, 400)
        self.assertIn("authorization_pending", raw.decode())

    def test_device_routes_invalid_json(self):
        self.start_bridge()
        status, _ = self.request("POST", "/v1/login/device/new", b"not json")
        self.assertEqual(status, 400)
        status, _ = self.request("POST", "/v1/login/device/token", b"not json")
        self.assertEqual(status, 400)

    def test_login_web_shim(self):
        self.start_bridge()
        status, raw = self.request("POST", "/v1/login/web/player2-ai-npc-minecraft", {})
        self.assertEqual(status, 200)
        self.assertIn("p2Key", json.loads(raw))

    def test_stt_tts_stubs(self):
        self.start_bridge()
        self.assertEqual(self.request("POST", "/v1/stt/start", {})[0], 200)
        status, raw = self.request("POST", "/v1/stt/stop", {})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["text"], "")
        self.assertEqual(self.request("POST", "/v1/tts/speak", {})[0], 200)

    def test_413_oversized_body(self):
        self.start_bridge()
        big = b'{"messages":' + b'["' + b"x" * (1_100_000) + b'"]}'
        status, _ = self.request("POST", "/v1/chat/completions", big)
        self.assertEqual(status, 413)

    def test_chat_bad_requests(self):
        self.start_bridge()
        self.assertEqual(self.request("POST", "/v1/chat/completions", b"not json")[0], 400)
        self.assertEqual(self.request("POST", "/v1/chat/completions", {"messages": []})[0], 400)
        self.assertEqual(self.request("POST", "/v1/chat/completions", {"foo": 1})[0], 400)
        self.assertEqual(
            self.request("POST", "/v1/chat/completions", {"messages": ["not", "dicts"]})[0], 400
        )
        self.assertEqual(
            self.request("POST", "/v1/chat/completions", {"messages": [{"role": "user"}]})[0], 400
        )


class ChatFlowTests(BridgeHarness):
    def test_happy_path_with_usage(self):
        self.start_bridge(kilo_behavior={"status": 200, "body": {**STUB_OK, "usage": {"prompt_tokens": 70, "completion_tokens": 12, "total_tokens": 82}}})
        before = self.stats()
        status, raw = self.post_chat()
        self.assertEqual(status, 200)
        content = json.loads(raw)["choices"][0]["message"]["content"]
        parsed = json.loads(content)
        self.assertEqual(set(parsed.keys()), {"reason", "command", "message"})
        after = self.stats()
        self.assertEqual(after["chat_requests"], before["chat_requests"] + 1)
        self.assertEqual(after["chat_ok"], before["chat_ok"] + 1)
        self.assertEqual(after["token_budget"]["lifetime_total"], before["token_budget"]["lifetime_total"] + 82)
        recent = after["recent_requests"][0]
        self.assertEqual(recent["status"], "ok")
        self.assertEqual(recent["total_tokens"], 82)
        self.assertEqual(recent["prompt_tokens"], 70)
        self.assertGreaterEqual(recent["duration_ms"], 0)
        self.assertTrue(recent["preview"])

    def test_happy_path_without_usage(self):
        self.start_bridge()
        status, _ = self.post_chat()
        self.assertEqual(status, 200)
        recent = self.stats()["recent_requests"][0]
        self.assertEqual(recent["status"], "ok")
        self.assertEqual(recent["total_tokens"], 0)

    def test_rate_limited_degraded(self):
        self.start_bridge(requests_per_hour=1)
        status, raw = self.post_chat()
        self.assertEqual(status, 200)  # first consumes the single token
        status, raw = self.post_chat()
        self.assertEqual(status, 200)
        content = json.loads(json.loads(raw)["choices"][0]["message"]["content"])
        self.assertEqual(content["message"], "")
        self.assertGreaterEqual(self.stats()["rate_limited"], 1)
        self.assertEqual(self.stats()["recent_requests"][0]["status"], "rate_limited")

    def test_kilo_error_degraded_and_refund(self):
        self.start_bridge(kilo_behavior={"status": 500, "body": "upstream boom"})
        status, raw = self.post_chat()
        self.assertEqual(status, 200)
        content = json.loads(json.loads(raw)["choices"][0]["message"]["content"])
        self.assertEqual(content["reason"], "kilo unavailable")
        stats = self.stats()
        self.assertGreaterEqual(stats["chat_failed"], 1)
        self.assertGreaterEqual(stats["kilo_errors"], 1)
        self.assertEqual(stats["recent_requests"][0]["status"], "kilo_error")

    def test_unexpected_exception_degraded(self):
        # a payload the stub cannot produce a valid structure for + a monkeypatched
        # client raising an unexpected error type
        self.start_bridge()
        original = server.KILO.chat_completion

        def boom(messages):
            raise RuntimeError("totally unexpected")

        server.KILO.chat_completion = boom
        try:
            status, raw = self.post_chat()
        finally:
            server.KILO.chat_completion = original
        self.assertEqual(status, 200)
        content = json.loads(json.loads(raw)["choices"][0]["message"]["content"])
        self.assertEqual(content["reason"], "kilo unavailable")

    def test_kilo_none_degraded(self):
        self.start_bridge()
        original = server.KILO
        server.KILO = None
        try:
            status, raw = self.post_chat()
        finally:
            server.KILO = original
        self.assertEqual(status, 200)
        content = json.loads(json.loads(raw)["choices"][0]["message"]["content"])
        self.assertEqual(content["reason"], "kilo unavailable")

    def test_journal_preview_is_last_user_message(self):
        self.start_bridge()
        self.post_chat({"messages": [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": '{"userMessage":"distinct needle text here"}'},
        ]})
        preview = self.stats()["recent_requests"][0]["preview"]
        self.assertIn("distinct needle text here", preview)
        self.assertLessEqual(len(preview), 60)


class HelperTests(unittest.TestCase):
    def setUp(self):
        server._reset_characters_cache()

    def test_resolve_rel_path_branches(self):
        self.assertIsNone(server.resolve_rel_path(None))
        self.assertEqual(server.resolve_rel_path(""), "")
        abs_path = os.path.abspath("/tmp/x.crt")
        self.assertEqual(server.resolve_rel_path(abs_path), abs_path)
        # repo-root relative fallback: "bridge/server.py" exists only under the
        # repo root, never relative to an unrelated cwd
        orig_cwd = os.getcwd()
        os.chdir(tempfile.gettempdir())
        self.addCleanup(os.chdir, orig_cwd)
        resolved = server.resolve_rel_path("bridge/server.py")
        self.assertTrue(resolved and os.path.isabs(resolved) and os.path.exists(resolved))
        # config-relative fallback: last resort, resolved against the config dir
        with tempfile.TemporaryDirectory() as tmp:
            cfg_file = Path(tmp) / "config.json"
            cfg_file.write_text("{}", encoding="utf-8")
            (Path(tmp) / "extra.crt").write_bytes(b"")
            cfg = BridgeConfig(tls_enabled=False)
            cfg.config_path = str(cfg_file)
            got = server.resolve_rel_path("extra.crt", cfg)
            self.assertEqual(got, str(Path(tmp) / "extra.crt"))
            # nothing anywhere -> the input is returned unchanged
            self.assertEqual(server.resolve_rel_path("nowhere.crt", cfg), "nowhere.crt")

    def test_effective_config_path(self):
        self.assertIsInstance(server._effective_config_path(None), (str, type(None)))

    def test_characters_file_candidates(self):
        cands = server.characters_file_candidates("/tmp/cfgdir/config.json")
        self.assertTrue(any("cfgdir" in str(c) for c in cands))
        cands = server.characters_file_candidates(None)
        self.assertTrue(any("bridge" in str(c) for c in cands))

    def test_valid_roster(self):
        self.assertIsNone(server._valid_roster({"characters": "nope"}))
        self.assertIsNone(server._valid_roster({"nope": 1}))
        self.assertIsNone(server._valid_roster("junk"))
        # every entry needs string name AND short_name
        self.assertIsNone(server._valid_roster({"characters": [{"name": "a"}]}))
        self.assertIsNone(server._valid_roster({"characters": [{"short_name": "A"}]}))
        self.assertIsNone(server._valid_roster({"characters": ["not a dict"]}))
        roster = server._valid_roster({"characters": [{"name": "a", "short_name": "A"}]})
        self.assertEqual(roster, [{"name": "a", "short_name": "A"}])

    def test_slugify_sanitize_disambiguate(self):
        self.assertEqual(server._slugify("Bob the Miner!"), "bob-the-miner")
        self.assertEqual(server._slugify("  MiXeD Case  "), "mixed-case")
        self.assertEqual(server._slugify(None), "none")  # str(None) -> "None"
        self.assertEqual(server._slugify("!!!"), "")
        self.assertEqual(server._sanitize_id("A b*c"), "A-b-c")  # case kept, runs -> "-"
        self.assertEqual(server._sanitize_id(""), "")
        self.assertEqual(len(server._sanitize_id("x" * 100)), 64)  # capped at 64
        self.assertEqual(server._disambiguate_id("bob", {"bob"}), "bob-2")
        self.assertEqual(server._disambiguate_id("bob", {"bob", "bob-2"}), "bob-3")

    def test_backfill_character_ids(self):
        # rosters reach this helper pre-validated (only dicts), see _valid_roster
        roster = [
            {"name": "a", "short_name": "A B"},
            {"name": "b", "short_name": "A B"},
            {"id": "  ", "name": "c", "short_name": "C"},
            {"id": "good", "name": "d"},
        ]
        out = server._backfill_character_ids(roster)
        ids = [c.get("id") for c in out]
        self.assertEqual(ids, ["a-b", "a-b-2", "c", "good"])
        # callers' dicts are shallow-copied, never mutated
        self.assertNotIn("id", roster[0])
        self.assertEqual(out[0]["name"], "a")

    def test_load_characters_variants(self):
        cfg = BridgeConfig(characters=[{"id": "cfgkilo", "name": "Kilo", "short_name": "KILO"}])
        server._reset_characters_cache()
        with tempfile.TemporaryDirectory() as tmp:
            temp = Path(tmp)
            # no characters.json anywhere -> falls back to cfg characters
            orig = server.characters_file_candidates
            server.characters_file_candidates = lambda config_path=None: [temp / "missing.json"]
            try:
                chars = server.load_characters(cfg)
                self.assertEqual([c["name"] for c in chars], ["Kilo"])
            finally:
                server.characters_file_candidates = orig
            server._reset_characters_cache()
            # invalid-shape json -> keeps previous roster, no raise
            (temp / "bad.json").write_text("{ not json", encoding="utf-8")
            server.characters_file_candidates = lambda config_path=None: [temp / "bad.json"]
            try:
                chars = server.load_characters(cfg)
                self.assertEqual([c["name"] for c in chars], ["Kilo"])
            finally:
                server.characters_file_candidates = orig
            server._reset_characters_cache()
            # valid file wins + ids backfilled
            (temp / "good.json").write_text(
                json.dumps({"characters": [{"name": "Bob", "short_name": "BOB"}]}), encoding="utf-8"
            )
            server.characters_file_candidates = lambda config_path=None: [temp / "good.json"]
            try:
                chars = server.load_characters(cfg)
                self.assertEqual([c["name"] for c in chars], ["Bob"])
                self.assertEqual(chars[0]["id"], "bob")
            finally:
                server.characters_file_candidates = orig
            server._reset_characters_cache()

    def test_chat_preview(self):
        self.assertEqual(server._chat_preview([{"role": "user", "content": "a\n\nb"}]), "a b")
        self.assertLessEqual(len(server._chat_preview([{"role": "user", "content": "x" * 500}])), 60)
        self.assertEqual(server._chat_preview("junk"), "")
        self.assertEqual(server._chat_preview([{"role": "user"}]), "")

    def test_record_chat_bad_tokens(self):
        # Bad token values must clamp to 0 rather than raise. Drive the real
        # normalizer directly (record_request) so the assertion does not depend
        # on _record_chat's exact arity.
        state = BridgeState()
        state.record_request("ok", -1, "x", -2, -3, "p")
        entry = state.recent_requests(1)[0]
        self.assertEqual(entry["duration_ms"], 0)
        self.assertEqual(entry["total_tokens"], 0)

    def test_record_chat_records_route_fields(self):
        state = BridgeState()
        route = Route(kind="conversation", character_id="kilo", model="m/x:free")
        with mock.patch.object(server, "STATE", state):
            server._record_chat("ok", 0.0, 10, 5, 15, "p", route=route)
        entry = state.recent_requests(1)[0]
        self.assertEqual(entry["kind"], "conversation")
        self.assertEqual(entry["character_id"], "kilo")
        self.assertEqual(entry["model"], "m/x:free")

    def test_record_chat_without_route_leaves_fields_blank(self):
        state = BridgeState()
        with mock.patch.object(server, "STATE", state):
            server._record_chat("ok", 0.0, 10, 5, 15, "p")
        entry = state.recent_requests(1)[0]
        self.assertEqual(entry["kind"], "")
        self.assertEqual(entry["character_id"], "")
        self.assertEqual(entry["model"], "")

    def test_init_backfills_characters(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            (tmp_path / "characters.json").write_text(
                json.dumps({"characters": [{"name": "X", "short_name": "X"}]}), encoding="utf-8"
            )
            # isolate from the repo's bridge/characters.json
            orig = server.characters_file_candidates
            server.characters_file_candidates = lambda config_path=None: [tmp_path / "characters.json"]
            try:
                server.init(BridgeConfig(tls_enabled=False), str(tmp_path / "config.json"))
            finally:
                server.characters_file_candidates = orig
            names = [c["name"] for c in server.STATE.characters]
            self.assertIn("X", names)
            for c in server.STATE.characters:
                self.assertTrue(c.get("id"))


class StartAllTests(unittest.TestCase):
    def test_main_listener_bind_failure_raises(self):
        # occupy the main listener port so the bind fails (works unprivileged
        # and as root, unlike a privileged port)
        squatter = socket.socket()
        squatter.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        squatter.bind(("127.0.0.1", 0))
        squatter.listen(1)
        port = squatter.getsockname()[1]
        cfg = make_cfg(http_port=port)
        try:
            with self.assertRaises(OSError):
                server.start_all(cfg)
        finally:
            squatter.close()

    def test_shim_port_busy_continues(self):
        # occupy the shim port, then start_all must continue with the main listener
        squatter = socket.socket()
        squatter.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        squatter.bind(("127.0.0.1", 0))
        squatter.listen(1)
        port = squatter.getsockname()[1]
        cfg = make_cfg(http_port=0, local_login_port=port)
        try:
            servers = server.start_all(cfg)
            labels = [label for label, _ in servers]
            self.assertIn("main-http", labels)
            self.assertNotIn("login-shim", labels)
        finally:
            squatter.close()

    def test_tls_missing_certs_warns(self):
        cfg = make_cfg(http_port=0, tls_enabled=True, tls_port=0, tls_cert="/nonexistent/x.crt", tls_key="/nonexistent/x.key")
        servers = server.start_all(cfg)
        labels = [label for label, _ in servers]
        self.assertIn("main-http", labels)
        self.assertNotIn("tls", labels)

    def test_tls_listener_on_ephemeral_port(self):
        if not (CERTS / "server.crt").exists():
            self.skipTest("tls/certs not generated")
        cfg = make_cfg(
            http_port=0,
            tls_enabled=True,
            tls_port=0,
            tls_cert=str(CERTS / "server.crt"),
            tls_key=str(CERTS / "server.key"),
        )
        servers = server.start_all(cfg)
        try:
            labels = [label for label, _ in servers]
            # start_all labels the TLS listener "tls" (lowercase)
            self.assertIn("tls", labels, f"labels={labels}")
        finally:
            for _label, srv in servers:
                srv.shutdown()
                srv.server_close()


class ServerBindHostnameTests(unittest.TestCase):
    """Regression: the bridge must not die when the hostname is not IDNA-safe.

    A host whose first FQDN label is longer than 63 octets (Docker's default
    64-char container-id hostname, long CI hostnames, ...) makes
    ``socket.getfqdn`` raise ``UnicodeEncodeError``, which is a ``ValueError``
    and therefore NOT caught by getfqdn's ``except error``. Because
    ``http.server`` resolves the bind address to an FQDN, this used to escape
    from ``server_bind`` and kill the whole bridge at startup — no listener
    at all, including the plain HTTP one.
    """

    def test_getfqdn_raising_does_not_kill_listener(self):
        import socketserver as _socketserver

        class Exploding:
            def __call__(self, *a, **kw):
                raise UnicodeEncodeError("idna", "x" * 64, 0, 64, "label too long")

        real_getfqdn = socket.getfqdn
        socket.getfqdn = Exploding()
        try:
            # 0.0.0.0 is where http.server substitutes gethostname(), i.e. the
            # path that used to raise.
            srv = server.build_server(make_cfg(), "0.0.0.0", 0)
            try:
                self.assertEqual(srv.server_name, "0.0.0.0")
                # it must also actually accept connections
                port = srv.server_address[1]
                threading.Thread(target=srv.serve_forever, daemon=True).start()
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                try:
                    conn.request("GET", "/v1/health")
                    resp = conn.getresponse()
                    self.assertEqual(resp.status, 200)
                finally:
                    conn.close()
            finally:
                srv.shutdown()
                srv.server_close()
            # the patched getfqdn must have been bypassed entirely
            self.assertNotIsInstance(_socketserver.TCPServer.server_bind, Exploding)
        finally:
            socket.getfqdn = real_getfqdn

    def test_bound_loopback_server_name_matches_address(self):
        srv = server.build_server(make_cfg(), "127.0.0.1", 0)
        try:
            self.assertEqual(srv.server_name, "127.0.0.1")
        finally:
            srv.server_close()


if __name__ == "__main__":
    unittest.main()
