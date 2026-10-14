"""Tests for speech-to-text input handling (``p2k_bridge.stt``) and the
``POST /v1/stt/transcribe`` endpoint.

The contract under test is the module's whole reason for existing: **a valid
request never fails the caller**. Every bad input, quota exhaustion, gateway
error and "the model heard nothing" must come back as ``{"text": ""}``, because
the caller is a NeoForge addon running on the voice-chat packet-processing
thread, where a thrown exception would disrupt voice chat for every player.
"""

import base64
import json
import tempfile
import struct
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import server  # noqa: E402
from p2k_bridge import stt  # noqa: E402
from p2k_bridge.catalog import ModelCatalog  # noqa: E402
from p2k_bridge.config import BridgeConfig  # noqa: E402
from p2k_bridge.state import BridgeState  # noqa: E402


def wav_bytes(seconds=1.0, rate=48000, channels=1, bits=16, payload=None):
    """A minimal but valid RIFF/WAVE header + PCM payload."""
    if payload is None:
        payload = b"\x00\x00" * int(rate * seconds)
    data_size = len(payload)
    byte_rate = rate * channels * bits // 8
    header = b"RIFF" + struct.pack("<I", 36 + data_size) + b"WAVE"
    header += b"fmt " + struct.pack("<IHHIIHH", 16, 1, channels, rate,
                                     byte_rate, channels * bits // 8, bits)
    header += b"data" + struct.pack("<I", data_size)
    return header + payload


VALID_WAV = wav_bytes(2.0)
VALID_B64 = base64.b64encode(VALID_WAV).decode()


class SttRequestParsingTests(unittest.TestCase):
    def cfg(self, **kw):
        base = dict(stt_max_seconds=20.0)
        base.update(kw)
        return BridgeConfig(**base)

    def test_valid_request_parses(self):
        req = stt.parse_audio_request(
            {"audio_b64": VALID_B64, "duration_ms": 2000,
             "player": "Steve", "player_uuid": "u-1", "character_id": "kilo"},
            self.cfg())
        self.assertEqual(req.seconds, 2.0)
        self.assertEqual(req.player, "Steve")
        self.assertEqual(req.player_uuid, "u-1")
        self.assertEqual(req.character_id, "kilo")
        self.assertEqual(req.raw, VALID_WAV)

    def test_duration_falls_back_to_the_wav_header(self):
        req = stt.parse_audio_request({"audio_b64": VALID_B64}, self.cfg())
        self.assertAlmostEqual(req.seconds, 2.0, places=3)

    def test_duration_ms_overrides_the_header(self):
        req = stt.parse_audio_request(
            {"audio_b64": VALID_B64, "duration_ms": 2500}, self.cfg())
        self.assertEqual(req.seconds, 2.5)

    def test_missing_audio_is_rejected(self):
        with self.assertRaises(stt.SttError) as ctx:
            stt.parse_audio_request({}, self.cfg())
        self.assertIn("audio_b64", str(ctx.exception))

    def test_non_base64_is_rejected(self):
        for bad in ("!!!not-base64!!!", "%%%%"):
            with self.assertRaises(stt.SttError) as ctx:
                stt.parse_audio_request({"audio_b64": bad}, self.cfg())
            self.assertIn("base64", str(ctx.exception))

    def test_body_must_be_a_json_object(self):
        for bad in ([], "x", None, 5):
            with self.assertRaises(stt.SttError):
                stt.parse_audio_request(bad, self.cfg())

    def test_audio_too_short_is_rejected(self):
        tiny = base64.b64encode(b"\x00" * 8).decode()
        with self.assertRaises(stt.SttError) as ctx:
            stt.parse_audio_request(
                {"audio_b64": tiny, "duration_ms": 100}, self.cfg())
        self.assertIn("too short", str(ctx.exception))

    def test_audio_over_the_configured_limit_is_rejected(self):
        with self.assertRaises(stt.SttError) as ctx:
            stt.parse_audio_request(
                {"audio_b64": VALID_B64, "duration_ms": 31000},
                self.cfg(stt_max_seconds=30.0))
        self.assertIn("configured", str(ctx.exception))

    def test_audio_over_the_hard_limit_is_rejected(self):
        # Beyond any configured limit: the hard cap is the last line of defence.
        with self.assertRaises(stt.SttError) as ctx:
            stt.parse_audio_request(
                {"audio_b64": VALID_B64, "duration_ms": 999000},
                self.cfg(stt_max_seconds=200.0))
        self.assertIn("over the 120s limit", str(ctx.exception))

    def test_oversized_audio_is_rejected(self):
        huge_body = {"audio_b64": "A" * (stt.MAX_B64_CHARS + 10)}
        with self.assertRaises(stt.SttError):
            stt.parse_audio_request(huge_body, self.cfg())

    def test_non_wav_without_duration_is_rejected(self):
        # Opaque audio the header reader cannot measure and no duration given.
        blob = base64.b64encode(b"\xff" * 4096).decode()
        with self.assertRaises(stt.SttError):
            stt.parse_audio_request({"audio_b64": blob}, self.cfg())

    def test_no_config_means_no_configured_limit(self):
        req = stt.parse_audio_request(
            {"audio_b64": VALID_B64, "duration_ms": 10000}, None)
        self.assertEqual(req.seconds, 10.0)


class SttResponseTests(unittest.TestCase):
    def cfg(self):
        return BridgeConfig()

    def test_gateway_response_becomes_transcript(self):
        resp = {"choices": [{"message": {"content": "get me three oak logs"}}]}
        self.assertEqual(stt.extract_transcript(resp), "get me three oak logs")

    def test_content_parts_array_is_joined(self):
        resp = {"choices": [{"message": {"content": [
            {"type": "text", "text": "get me"}, {"type": "text", "text": "3 logs"}]}}]}
        self.assertEqual(stt.extract_transcript(resp), "get me 3 logs")

    def test_reply_key_is_text_matching_the_mod(self):
        self.assertEqual(stt.transcript_reply("hello"), {"text": "hello"})
        self.assertEqual(stt.transcript_reply(""), {"text": ""})
        self.assertEqual(stt.transcript_reply(None), {"text": ""})

    def test_malformed_responses_yield_empty(self):
        for bad in (None, {}, [], {"choices": None}, {"choices": []},
                    {"choices": [None]}, {"choices": ["x"]},
                    {"choices": [{"message": None}]}, {"choices": [{"message": {}}]},
                    {"choices": [{"message": {"content": None}}]},
                    {"choices": [{"message": {"content": 5}}]},
                    {"choices": [{"message": {"content": []}}]}):
            self.assertEqual(stt.extract_transcript(bad), "", bad)

    def test_stray_quotes_and_brackets_are_stripped(self):
        self.assertEqual(stt.clean_transcript('"get me logs"'), "get me logs")
        self.assertEqual(stt.clean_transcript("'get me logs'"), "get me logs")

    def test_whitespace_is_collapsed(self):
        self.assertEqual(stt.clean_transcript("  get   me \n logs  "), "get me logs")

    def test_no_speech_markers_become_empty(self):
        for text in (
            "(No words spoken)", "(No spoken words provided)", "No speech detected",
            "There is no audio in this message.", "I cannot hear any speech.",
            "Unable to transcribe the audio.", "No sound was provided.", "silence",
        ):
            self.assertEqual(stt.clean_transcript(text), "", text)

    def test_real_speech_survives_cleaning(self):
        for text in (
            "Kilo, can you get me 3 oak logs?",
            "blaze bring me two stone",
            "yes",
            "Echo online.",
        ):
            self.assertEqual(stt.clean_transcript(text), text, text)

    def test_build_request_wraps_audio_as_input_audio(self):
        req = stt.parse_audio_request(
            {"audio_b64": VALID_B64, "duration_ms": 2000}, self.cfg())
        payload = stt.build_request(req, self.cfg(), "m/audio:free")
        self.assertEqual(payload["model"], "m/audio:free")
        content = payload["messages"][0]["content"]
        self.assertEqual(content[0]["type"], "text")
        audio_part = content[1]
        self.assertEqual(audio_part["type"], "input_audio")
        self.assertEqual(audio_part["input_audio"]["format"], "wav")
        self.assertTrue(audio_part["input_audio"]["data"])


class SttRoutingTests(unittest.TestCase):
    def cfg(self, **kw):
        base = dict(stt_model="global/stt:free", stt_max_seconds=20.0)
        base.update(kw)
        return BridgeConfig(**base)

    def test_per_character_stt_model_wins(self):
        chars = [{"id": "kilo", "name": "Kilo", "stt_model": "char/stt:free"}]
        messages = [{"role": "system", "content": "Your character's name is Kilo."}]
        from p2k_bridge.routing import resolve_route
        route = resolve_route(messages, chars, self.cfg())
        self.assertEqual(route.stt_model, "char/stt:free")

    def test_global_stt_model_without_an_override(self):
        chars = [{"id": "kilo", "name": "Kilo"}]
        messages = [{"role": "system", "content": "Your character's name is Kilo."}]
        from p2k_bridge.routing import resolve_route
        self.assertEqual(
            resolve_route(messages, chars, self.cfg()).stt_model, "global/stt:free")

    def test_unidentified_character_uses_global(self):
        from p2k_bridge.routing import resolve_route
        two = [{"id": "a", "name": "A"}, {"id": "b", "name": "B"}]
        route = resolve_route([{"role": "user", "content": "hi"}], two, self.cfg())
        self.assertEqual(route.stt_model, "global/stt:free")
        self.assertEqual(route.character_id, "")


class SttEndpointViaHarnessTests(unittest.TestCase):
    """End-to-end through the real HTTP handler, with the gateway stubbed."""

    def setUp(self):
        from tests.test_coverage_gaps import BridgeHarness

        self.harness = BridgeHarness()
        self.harness.setUp()

    def tearDown(self):
        self.harness.tearDown()

    def _valid_body(self, **extra):
        payload = {"audio_b64": VALID_B64, "duration_ms": 2000, "player": "Steve"}
        payload.update(extra)
        return payload

    def _post(self, payload):
        return self.harness.request("POST", "/v1/stt/transcribe", payload)

    def _patch_gateway(self, behaviour):
        """Stub the gateway seam. ``behaviour`` is a payload or a callable."""
        original = server.KILO._request

        def fake(method, path, payload):
            return behaviour(method, path, payload) if callable(behaviour) else behaviour

        server.KILO._request = fake
        self.addCleanup(lambda: setattr(server.KILO, "_request", original))

    # --- the contract: a valid request never fails the caller -----------------

    def test_valid_utterance_returns_the_transcript(self):
        self.harness.start_bridge()
        self._patch_gateway(
            {"choices": [{"message": {"content": "get me three oak logs"}}]})
        status, raw = self._post(self._valid_body(character_id="kilo"))
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw), {"text": "get me three oak logs"})

    def test_missing_audio_returns_empty_not_4xx(self):
        self.harness.start_bridge()
        status, raw = self._post({"player": "Steve"})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw), {"text": ""})

    def test_non_wav_audio_without_duration_returns_empty(self):
        self.harness.start_bridge()
        blob = base64.b64encode(b"\xff" * 4096).decode()
        status, raw = self._post({"audio_b64": blob})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw), {"text": ""})

    def test_oversized_duration_returns_empty_not_4xx(self):
        self.harness.start_bridge()
        status, raw = self._post(self._valid_body(duration_ms=999999000))
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw), {"text": ""})

    def test_malformed_json_returns_empty_not_4xx(self):
        self.harness.start_bridge()
        status, raw = self.harness.request("POST", "/v1/stt/transcribe", b"{not json")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw), {"text": ""})

    def test_gateway_error_returns_empty_not_5xx(self):
        self.harness.start_bridge(kilo_behavior={"status": 500, "body": b"boom"})
        status, raw = self._post(self._valid_body())
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw), {"text": ""})

    def test_no_speech_reply_returns_empty(self):
        self.harness.start_bridge()
        self._patch_gateway(
            {"choices": [{"message": {"content": "(No words spoken)"}}]})
        status, raw = self._post(self._valid_body())
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw), {"text": ""})

    def test_rate_limited_returns_empty(self):
        self.harness.start_bridge(requests_per_hour=1)
        self._patch_gateway({"choices": [{"message": {"content": "first"}}]})
        first_status, first_raw = self._post(self._valid_body(duration_ms=1000))
        second_status, second_raw = self._post(self._valid_body(duration_ms=1000))
        self.assertEqual(first_status, 200)
        self.assertEqual(second_status, 200)
        self.assertEqual(json.loads(first_raw), {"text": "first"})
        self.assertEqual(json.loads(second_raw), {"text": ""})

    def test_disabled_by_config_returns_empty(self):
        self.harness.start_bridge(stt_enabled=False)
        self._patch_gateway(
            {"choices": [{"message": {"content": "should not be called"}}]})
        status, raw = self._post(self._valid_body())
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw), {"text": ""})

    # --- observability ------------------------------------------------------

    def test_stt_counters_and_journal_are_recorded(self):
        self.harness.start_bridge()
        self._patch_gateway({"choices": [{"message": {"content": "hello there"}}]})
        self._post(self._valid_body(character_id="kilo"))
        stats = json.loads(self.harness.request("GET", "/bridge/stats")[1])
        self.assertGreaterEqual(stats["stt_requests"], 1)
        self.assertGreaterEqual(stats["stt_ok"], 1)
        entry = stats["recent_requests"][0]
        self.assertEqual(entry["kind"], "stt")
        self.assertEqual(entry["status"], "ok")

    def test_rejected_request_is_journaled_as_bad_request(self):
        self.harness.start_bridge()
        self._post({"player": "Steve"})
        stats = json.loads(self.harness.request("GET", "/bridge/stats")[1])
        self.assertGreaterEqual(stats["stt_failed"], 1)
        entry = stats["recent_requests"][0]
        self.assertEqual(entry["kind"], "stt")
        self.assertEqual(entry["status"], "bad_request")

    # --- per-character routing ----------------------------------------------

    def test_character_stt_model_is_used(self):
        self.harness.start_bridge()
        roster_dir = Path(tempfile.mkdtemp())
        (roster_dir / "characters.json").write_text(
            json.dumps({"characters": [{"id": "kilo", "name": "Kilo",
                                        "short_name": "KILO",
                                        "stt_model": "char/stt:free"}]}),
            encoding="utf-8")
        server.CONFIG_PATH = str(roster_dir / "config.json")
        self.addCleanup(setattr, server, "CONFIG_PATH", None)
        captured = {}

        def fake(method, path, payload):
            captured.update(payload)
            return {"choices": [{"message": {"content": "hi"}}]}

        self._patch_gateway(fake)
        status, _ = self._post(self._valid_body(character_id="kilo"))
        self.assertEqual(status, 200)
        self.assertEqual(captured.get("model"), "char/stt:free")
        self.assertEqual(captured["messages"][0]["content"][1]["type"], "input_audio")

    def test_global_stt_model_without_overrides(self):
        self.harness.start_bridge(stt_model="global/stt:free")
        captured = {}

        def fake(method, path, payload):
            captured.update(payload)
            return {"choices": [{"message": {"content": "hi"}}]}

        self._patch_gateway(fake)
        status, _ = self._post(self._valid_body(character_id="kilo"))
        self.assertEqual(status, 200)
        self.assertEqual(captured.get("model"), "global/stt:free")


if __name__ == "__main__":
    unittest.main()
