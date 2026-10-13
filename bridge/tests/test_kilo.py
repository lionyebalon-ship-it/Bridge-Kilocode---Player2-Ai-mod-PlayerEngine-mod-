"""Tests for p2k_bridge.kilo (gateway client, max_tokens clamping, errors).

All network traffic goes to localhost stub servers — no real gateway calls,
no real API keys. Any key used here is an obvious fake.
"""

import http.server
import json
import sys
import threading
import unittest
import urllib.error

sys.path.insert(0, ".")
sys.path.insert(0, "..")

from p2k_bridge.budget import TokenBudget  # noqa: E402
from p2k_bridge.config import BridgeConfig  # noqa: E402
from p2k_bridge.kilo import KiloClient, KiloError, compute_max_tokens  # noqa: E402

VALID_RESPONSE = {
    "id": "chatcmpl-stub",
    "object": "chat.completion",
    "model": "stub",
    "choices": [
        {"index": 0, "message": {"role": "assistant", "content": '{"reason":"","command":"stop","message":"hi"}'},
         "finish_reason": "stop"}
    ],
}

SMALL_MESSAGES = [
    {"role": "system", "content": "You are a Minecraft companion AI."},
    {"role": "user", "content": "hello"},
]


class StubServer:
    """Configurable localhost stub for the Kilo gateway."""

    def __init__(self, handler_factory):
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler_factory)
        # client timeouts can reset the connection mid-write; keep the noise down
        self.server.handle_error = lambda request, client_address: None

    @property
    def base_url(self):
        host, port = self.server.server_address
        return f"http://{host}:{port}"

    def start(self):
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


def make_handler(behavior, recorder):
    """behavior: ('json', status, body) | ('hang', seconds) | ('drop',)"""

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _respond(self):
            recorder.setdefault("requests", []).append(
                {
                    "method": self.command,
                    "path": self.path,
                    "headers": {k.lower(): v for k, v in self.headers.items()},
                    "body": self._read_body(),
                }
            )
            kind = behavior[0]
            if kind == "hang":
                import time

                time.sleep(behavior[1])
            status = behavior[1] if kind == "json" else 200
            # bodies arrive as dicts, strings, or bytes; serialize everything
            body = behavior[2] if kind == "json" else VALID_RESPONSE
            if isinstance(body, str):
                body = body.encode()
            elif not isinstance(body, (bytes, bytearray)):
                body = json.dumps(body).encode()
            self.send_response(status)
            if kind != "drop":
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_header("Content-Length", "0")
                self.end_headers()
                try:
                    self.connection.close()
                except Exception:
                    pass

        def _read_body(self):
            length = int(self.headers.get("Content-Length") or 0)
            return self.rfile.read(length).decode("utf-8", "replace") if length else ""

        do_GET = _respond
        do_POST = _respond

    return Handler


class ComputeMaxTokensTests(unittest.TestCase):
    def test_default_free_reign_clamp(self):
        cfg = BridgeConfig()  # max_tokens=500000, model_context_limit=262144
        est = TokenBudget.estimate_tokens(SMALL_MESSAGES)
        self.assertEqual(
            compute_max_tokens(cfg, SMALL_MESSAGES), 262144 - est - 1024
        )

    def test_never_exceeds_max_tokens(self):
        cfg = BridgeConfig(max_tokens=500000, model_context_limit=10**9)
        self.assertEqual(compute_max_tokens(cfg, SMALL_MESSAGES), 500000)

    def test_floor_for_tiny_context(self):
        cfg = BridgeConfig(max_tokens=500000, model_context_limit=300)
        big = [{"role": "user", "content": "x" * 5000}]
        self.assertEqual(compute_max_tokens(cfg, big), 256)

    def test_no_clamp_when_limit_zero(self):
        cfg = BridgeConfig(max_tokens=500000, model_context_limit=0)
        big = [{"role": "user", "content": "x" * 100000}]
        self.assertEqual(compute_max_tokens(cfg, big), 500000)

    def test_empty_messages(self):
        cfg = BridgeConfig()
        value = compute_max_tokens(cfg, [])
        self.assertGreaterEqual(value, 256)
        self.assertLessEqual(value, 500000)

    def test_max_tokens_floor(self):
        cfg = BridgeConfig(max_tokens=10, model_context_limit=0)
        self.assertEqual(compute_max_tokens(cfg, SMALL_MESSAGES), 256)


class ChatCompletionStubTests(unittest.TestCase):
    def setUp(self):
        self.recorder = {}
        self.client_cfg_overrides = {}

    def _client_for(self, behavior, timeout=5.0, **cfg_overrides):
        stub = StubServer(make_handler(behavior, self.recorder)).start()
        self.addCleanup(stub.stop)
        cfg = BridgeConfig(
            kilo_base_url=stub.base_url,
            kilo_api_key="",
            request_timeout_seconds=timeout,
            **cfg_overrides,
        )
        return KiloClient(cfg)

    def test_sends_clamped_max_tokens_in_body(self):
        client = self._client_for(("json", 200, VALID_RESPONSE))
        result = client.chat_completion(SMALL_MESSAGES)
        self.assertEqual(result["choices"][0]["message"]["content"], '{"reason":"","command":"stop","message":"hi"}')
        sent = json.loads(self.recorder["requests"][0]["body"])
        est = TokenBudget.estimate_tokens(SMALL_MESSAGES)
        self.assertEqual(sent["max_tokens"], 262144 - est - 1024)
        self.assertEqual(sent["model"], "nvidia/nemotron-3-super-120b-a12b:free")
        self.assertIn("temperature", sent)

    def test_anonymous_request_has_no_authorization(self):
        client = self._client_for(("json", 200, VALID_RESPONSE))
        client.chat_completion(SMALL_MESSAGES)
        headers = self.recorder["requests"][0]["headers"]
        self.assertNotIn("authorization", headers)
        self.assertEqual(headers.get("user-agent"), "p2k-bridge/1.0")
        self.assertEqual(headers.get("accept"), "application/json")
        self.assertEqual(headers.get("content-type"), "application/json")

    def test_authorization_present_only_with_key(self):
        client = self._client_for(("json", 200, VALID_RESPONSE))
        client._cfg.kilo_api_key = "FAKE_TEST_KEY_FOR_ASSERTION"
        client.chat_completion(SMALL_MESSAGES)
        headers = self.recorder["requests"][0]["headers"]
        self.assertEqual(headers.get("authorization"), "Bearer FAKE_TEST_KEY_FOR_ASSERTION")

    def test_models_get(self):
        stub = StubServer(make_handler(("json", 200, {"data": [{"id": "a:free"}]}), self.recorder)).start()
        self.addCleanup(stub.stop)
        client = KiloClient(BridgeConfig(kilo_base_url=stub.base_url))
        data = client.models()
        self.assertEqual(data["data"][0]["id"], "a:free")
        self.assertEqual(self.recorder["requests"][0]["method"], "GET")
        self.assertTrue(self.recorder["requests"][0]["path"].endswith("/models"))

    def test_http_error_redacts_key(self):
        client = self._client_for(("json", 500, "boom FAKE_TEST_KEY_EARLY_IN_BODY reason"))
        client._cfg.kilo_api_key = "FAKE_TEST_KEY_EARLY_IN_BODY"
        with self.assertRaises(KiloError) as ctx:
            client.chat_completion(SMALL_MESSAGES)
        self.assertEqual(ctx.exception.status, 500)
        self.assertIn("500", str(ctx.exception))
        self.assertNotIn("FAKE_TEST_KEY_EARLY_IN_BODY", str(ctx.exception))
        self.assertIn("[REDACTED]", str(ctx.exception))

    def test_network_error(self):
        # point at a closed port
        probe = http.server.ThreadingHTTPServer(("127.0.0.1", 0), make_handler(("json", 200, VALID_RESPONSE), self.recorder))
        port = probe.server_address[1]
        probe.server_close()
        client = KiloClient(BridgeConfig(kilo_base_url=f"http://127.0.0.1:{port}", request_timeout_seconds=3.0))
        with self.assertRaises(KiloError) as ctx:
            client.chat_completion(SMALL_MESSAGES)
        self.assertIn("network error", str(ctx.exception))

    def test_malformed_json_body(self):
        client = self._client_for(("json", 200, "not json at all"))
        with self.assertRaises(KiloError):
            client.chat_completion(SMALL_MESSAGES)

    def test_structural_invalid_responses(self):
        for bad in (
            {"no_choices": True},
            {"choices": "not a list"},
            {"choices": []},
            {"choices": ["not a dict"]},
            {"choices": [{"index": 0, "message": {"role": "assistant", "content": 5}}]},
            {"choices": [{"index": 0, "message": "not a dict"}]},
        ):
            client = self._client_for(("json", 200, bad))
            with self.assertRaises(KiloError, msg=f"should reject {bad!r}"):
                client.chat_completion(SMALL_MESSAGES)

    def test_non_serializable_payload(self):
        client = self._client_for(("json", 200, VALID_RESPONSE))
        with self.assertRaises(KiloError):
            client.chat_completion([{"role": "user", "content": object()}])

    def test_hang_times_out(self):
        import time

        client = self._client_for(("hang", 5), timeout=2.0)
        started = time.monotonic()
        with self.assertRaises(KiloError):
            client.chat_completion(SMALL_MESSAGES)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 4.5, f"timeout did not fire early enough ({elapsed:.1f}s)")


if __name__ == "__main__":
    unittest.main()
