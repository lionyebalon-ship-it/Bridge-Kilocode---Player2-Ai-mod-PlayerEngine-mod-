"""Tests for p2k_bridge.normalize (LLM output -> Player2 NPC contract)."""

import json
import sys
import unittest

sys.path.insert(0, ".")
sys.path.insert(0, "..")

from p2k_bridge.normalize import (  # noqa: E402
    _first_json_object,
    _sanitize_command,
    _strip_code_fences,
    normalize_llm_content,
    set_model,
    to_openai_envelope,
)


class StripFencesTests(unittest.TestCase):
    def test_no_fence_unchanged(self):
        self.assertEqual(_strip_code_fences("plain text"), "plain text")

    def test_fence_marker_removed(self):
        self.assertEqual(_strip_code_fences("```json\n{}\n```").strip(), "{}")

    def test_bare_fence_becomes_space(self):
        self.assertEqual(_strip_code_fences("```"), " ")

    def test_empty(self):
        self.assertEqual(_strip_code_fences(""), "")


class FirstJsonObjectTests(unittest.TestCase):
    def test_direct_dict(self):
        self.assertEqual(_first_json_object('{"a": 1}'), {"a": 1})

    def test_embedded_in_prose(self):
        self.assertEqual(_first_json_object('sure: {"a": 1} done'), {"a": 1})

    def test_nested_objects(self):
        self.assertEqual(_first_json_object('{"a": {"b": 2}}'), {"a": {"b": 2}})

    def test_braces_inside_strings_are_not_structure(self):
        self.assertEqual(_first_json_object('{"m": "} {"}'), {"m": "} {"})

    def test_escaped_quote_inside_string(self):
        self.assertEqual(
            _first_json_object('{"m": "say \\"hi\\""}'), {"m": 'say "hi"'}
        )

    def test_first_valid_dict_wins(self):
        self.assertEqual(_first_json_object('{"first": 1} {"second": 2}'), {"first": 1})

    def test_unbalanced_leading_brace_skipped(self):
        self.assertEqual(_first_json_object('{ nope {"good": 1}'), {"good": 1})

    def test_unclosed_object_is_none(self):
        self.assertIsNone(_first_json_object('{"a": 1'))

    def test_text_without_braces_is_none(self):
        self.assertIsNone(_first_json_object("just text, no braces at all"))

    def test_non_dict_candidates_skipped(self):
        self.assertIsNone(_first_json_object('"a string" {bad json}'))


class SanitizeCommandTests(unittest.TestCase):
    def test_leading_at_stripped(self):
        self.assertEqual(_sanitize_command("@stop"), "stop")

    def test_whitespace_and_at_stripped(self):
        self.assertEqual(_sanitize_command("  @  stop  "), "stop")

    def test_interior_at_kept(self):
        self.assertEqual(_sanitize_command("@goto @  home"), "goto @  home")

    def test_non_strings_rejected(self):
        self.assertEqual(_sanitize_command(None), "")
        self.assertEqual(_sanitize_command(5), "")
        self.assertEqual(_sanitize_command(["stop"]), "")

    def test_truncated_at_300(self):
        self.assertEqual(len(_sanitize_command("c" * 400)), 300)


class NormalizeLlMContentTests(unittest.TestCase):
    def test_plain_text_becomes_message(self):
        self.assertEqual(
            normalize_llm_content("hello world"),
            {"reason": "", "command": "", "message": "hello world"},
        )

    def test_none_and_non_string_content(self):
        self.assertEqual(
            normalize_llm_content(None), {"reason": "", "command": "", "message": ""}
        )
        self.assertEqual(
            normalize_llm_content(12345), {"reason": "", "command": "", "message": ""}
        )

    def test_exact_json_object(self):
        self.assertEqual(
            normalize_llm_content('{"reason":"r","command":"stop","message":"hi"}'),
            {"reason": "r", "command": "stop", "message": "hi"},
        )

    def test_fenced_json_object(self):
        self.assertEqual(
            normalize_llm_content('```json\n{"reason":"r","command":"stop","message":"hi"}\n```'),
            {"reason": "r", "command": "stop", "message": "hi"},
        )

    def test_json_embedded_in_prose(self):
        self.assertEqual(
            normalize_llm_content('Here you go: {"reason":"r","command":"stop","message":"hi"} thanks'),
            {"reason": "r", "command": "stop", "message": "hi"},
        )

    def test_extra_keys_dropped(self):
        obj = normalize_llm_content('{"reason":"r","command":"stop","message":"hi","extra":[1,2]}')
        self.assertEqual(obj, {"reason": "r", "command": "stop", "message": "hi"})

    def test_non_string_values_become_empty(self):
        self.assertEqual(
            normalize_llm_content('{"reason":5,"command":null,"message":{}}'),
            {"reason": "", "command": "", "message": ""},
        )

    def test_missing_keys_become_empty(self):
        self.assertEqual(
            normalize_llm_content("{}"), {"reason": "", "command": "", "message": ""}
        )
        self.assertEqual(
            normalize_llm_content('{"other": 1}'), {"reason": "", "command": "", "message": ""}
        )

    def test_command_sanitized(self):
        obj = normalize_llm_content('{"command":"  @goto @  home  "}')
        self.assertEqual(obj["command"], "goto @  home")

    def test_message_stripped_and_truncated_at_250(self):
        obj = normalize_llm_content('{"message":"  padded  "}')
        self.assertEqual(obj["message"], "padded")
        obj = normalize_llm_content('{"message":"' + "x" * 300 + '"}')
        self.assertEqual(obj["message"], "x" * 250)

    def test_unparseable_falls_back_to_truncated_text(self):
        obj = normalize_llm_content("y" * 400)
        self.assertEqual(obj, {"reason": "", "command": "", "message": "y" * 250})

    def test_json_array_is_not_a_dict(self):
        obj = normalize_llm_content("[1, 2]")
        self.assertEqual(obj, {"reason": "", "command": "", "message": "[1, 2]"})

    def test_json_number_is_not_a_dict(self):
        self.assertEqual(normalize_llm_content("42")["message"], "42")

    def test_empty_and_whitespace_only(self):
        self.assertEqual(
            normalize_llm_content(""), {"reason": "", "command": "", "message": ""}
        )
        self.assertEqual(
            normalize_llm_content("   \n  "), {"reason": "", "command": "", "message": ""}
        )


class ToOpenAIEnvelopeTests(unittest.TestCase):
    def setUp(self):
        # set_model mutates a module global shared with server.py; restore it
        self.addCleanup(set_model, "p2k-bridge")

    def test_envelope_shape_and_model(self):
        set_model("test-model")
        obj = {"reason": "r", "command": "stop", "message": "hi"}
        env = to_openai_envelope(obj)
        self.assertEqual(env["id"], "chatcmpl-p2k")
        self.assertEqual(env["object"], "chat.completion")
        self.assertEqual(env["model"], "test-model")
        self.assertIsInstance(env["created"], int)
        self.assertEqual(len(env["choices"]), 1)
        choice = env["choices"][0]
        self.assertEqual(choice["index"], 0)
        self.assertEqual(choice["finish_reason"], "stop")
        message = choice["message"]
        self.assertEqual(message["role"], "assistant")
        self.assertEqual(json.loads(message["content"]), obj)

    def test_set_model_coerces_to_str(self):
        set_model(5)
        self.assertEqual(to_openai_envelope({})["model"], "5")


if __name__ == "__main__":
    unittest.main()
