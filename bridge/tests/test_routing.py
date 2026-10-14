"""Tests for call classification, character identification and route resolution.

The classification fixtures are the payloads actually captured from the mods
during the end-to-end rig runs, with the source cited for each. They are kept
verbatim (modulo JSON-escaping) so a change in the mod's message format breaks a
test here rather than silently mis-routing live traffic.
"""

import json
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from p2k_bridge.catalog import ModelCatalog  # noqa: E402
from p2k_bridge.config import BridgeConfig, _normalize_character, load_config  # noqa: E402
from p2k_bridge.routing import (  # noqa: E402
    COMMAND_FEEDBACK,
    CONVERSATION,
    EVENT,
    GREETING,
    MEMORY_EXTRACTION,
    NPC_DIALOGUE,
    UNKNOWN,
    character_overrides,
    classify_call,
    extract_payload_text,
    identify_character,
    resolve_route,
)

# --- captured payloads --------------------------------------------------------
# A wrapped conversation call, as the mod sends it (orders_boot.log:505 style):
# the content is a JSON string whose userMessage holds the real text.
WRAPPED_CONVERSATION = {
    "userMessage": "User Message: [P2KTestA]: can you get me 3 oak logs?",
    "reminders": "Remember to output valid JSON reponse with reason, command and message.",
    "currentMood": "neutral (intensity 3)",
    "worldStatus": '{"difficulty":"EASY"}',
    "agentStatus": '{"health":"20.00/20"}',
}

# A wrapped command-feedback call (orders_boot.log:1219).
WRAPPED_COMMAND_FEEDBACK = {
    "userMessage": (
        "Info: Command feedback: @get oak_log 3 finished running. What shall we do "
        "next? If no new action is needed to finish user's request, generate empty "
        "command `\"\"`."
    ),
    "reminders": "Remember to output valid JSON reponse with reason, command and message.",
}

# A bare-text respawn event (orders_boot.log:2119) — note: NOT JSON-wrapped.
BARE_RESPAWN = "Info: P2KTestA has respawned you. You are returning to them; do not greet as if meeting for the first time."

# The first-message greeting (orders_boot.log:317), wrapped like a conversation.
GREETING_TEXT = (
    "Hey there! I'm your companion on this server. What are we up to today? "
    "IMPORTANT: SINCE THIS IS THE FIRST MESSAGE, ONLY USE COMMAND `bodylang greeting`"
)

# The memory call is bare text too. This string never appears in the mod-side log
# — only in the bridge journal — hence no line citation here.
MEMORY_TEXT = "Extract the durable memory graph from the conversation turns"

SYSTEM_PROMPT = (
    "General Instructions:\n"
    "You are an AI-NPC. You have been spawned in by your owner, who's username is "
    '"P2KTestA", but you can also talk and interact with other users.\n'
    "You take the personality of the following character:\n"
    "Your character's name is Kilo.\n"
    "You are a helpful, concise Minecraft companion AI agent.\n"
    "User Message Format:"
)

CHARACTERS = [
    {
        "id": "kilo",
        "name": "Kilo",
        "short_name": "KILO",
        "greeting": "Hey there! I'm your companion on this server. What are we up to today?",
        "description": "You are a helpful, concise Minecraft companion AI agent.",
    }
]


def wrapped(d: dict) -> list[dict]:
    return [{"role": "user", "content": json.dumps(d, ensure_ascii=False)}]


def bare(text: str) -> list[dict]:
    return [{"role": "user", "content": text}]


def with_system(text: str) -> list[dict]:
    return [{"role": "system", "content": text}]


class ExtractPayloadTextTests(unittest.TestCase):
    def test_wrapped_payload_returns_inner_user_message(self):
        self.assertEqual(
            extract_payload_text(wrapped(WRAPPED_CONVERSATION)),
            "User Message: [P2KTestA]: can you get me 3 oak logs?",
        )

    def test_bare_payload_returns_the_text_unchanged(self):
        self.assertEqual(extract_payload_text(bare(BARE_RESPAWN)), BARE_RESPAWN)

    def test_wrapped_without_user_message_key_falls_back_to_raw(self):
        # A JSON object that is not the mod's envelope must not be discarded.
        raw = json.dumps({"something": "else"})
        self.assertEqual(extract_payload_text(bare(raw)), raw)

    def test_malformed_json_falls_back_to_raw_string(self):
        broken = '{"userMessage": "unterminated'
        self.assertEqual(extract_payload_text(bare(broken)), broken)

    def test_json_scalar_falls_back_to_raw_string(self):
        self.assertEqual(extract_payload_text(bare("42")), "42")

    def test_empty_or_missing_content_returns_empty(self):
        self.assertEqual(extract_payload_text(bare("")), "")
        self.assertEqual(extract_payload_text(bare("   ")), "")
        self.assertEqual(extract_payload_text([{"role": "user", "content": None}]), "")
        self.assertEqual(extract_payload_text([{"role": "assistant", "content": "hi"}]), "")
        self.assertEqual(extract_payload_text([]), "")
        self.assertEqual(extract_payload_text(None), "")
        self.assertEqual(extract_payload_text([{"role": "user", "content": 5}]), "")

    def test_uses_the_last_user_message(self):
        messages = [
            {"role": "user", "content": "User Message: old"},
            {"role": "assistant", "content": "x"},
            {"role": "user", "content": "User Message: new"},
        ]
        self.assertEqual(extract_payload_text(messages), "User Message: new")

    def test_never_raises_on_hostile_input(self):
        for bad in [[{"role": "user", "content": "\ud800"}], [{}], [None], "nope", 5]:
            self.assertIsInstance(extract_payload_text(bad), str)


class ClassifyCallTests(unittest.TestCase):
    def test_conversation(self):
        self.assertEqual(classify_call(wrapped(WRAPPED_CONVERSATION)), CONVERSATION)

    def test_conversation_from_bare_text(self):
        self.assertEqual(
            classify_call(bare("User Message: [P2KTestA]: hello")), CONVERSATION
        )

    def test_command_feedback(self):
        self.assertEqual(
            classify_call(wrapped(WRAPPED_COMMAND_FEEDBACK)), COMMAND_FEEDBACK
        )

    def test_command_feedback_takes_precedence_over_the_info_catchall(self):
        # Regression: "Info: Command feedback:" also starts with "Info: ".
        self.assertEqual(
            classify_call(
                wrapped({"userMessage": "Info: Command feedback: @get oak_log 1 done."})
            ),
            COMMAND_FEEDBACK,
        )

    def test_event_from_a_respawn_notification(self):
        self.assertEqual(classify_call(bare(BARE_RESPAWN)), EVENT)

    def test_event_wrapped(self):
        self.assertEqual(classify_call(wrapped({"userMessage": BARE_RESPAWN})), EVENT)

    def test_memory_extraction(self):
        self.assertEqual(classify_call(bare(MEMORY_TEXT)), MEMORY_EXTRACTION)

    def test_memory_extraction_wrapped(self):
        self.assertEqual(classify_call(wrapped({"userMessage": MEMORY_TEXT})), MEMORY_EXTRACTION)

    def test_greeting_is_not_an_event(self):
        # Regression: the first-message greeting also starts with "Info: ", so a
        # bare Info: catch-all would label the NPC's own greeting turn as an event.
        messages = wrapped({"userMessage": f"Info: {GREETING_TEXT}"})
        self.assertEqual(classify_call(messages, CHARACTERS), GREETING)
        self.assertEqual(classify_call(messages), EVENT, "without a roster the greeting is an Info: event")

    def test_greeting_matches_without_the_roster_prefix_tail(self):
        # Only the roster greeting itself is required to match, not the "IMPORTANT"
        # suffix the mod appends.
        short_greeting = CHARACTERS[0]["greeting"]
        self.assertEqual(
            classify_call(wrapped({"userMessage": f"Info: {short_greeting}"}), CHARACTERS),
            GREETING,
        )

    def test_npc_dialogue_is_a_peer_message_not_a_player_command(self):
        # This kind only appears when two companions are spawned and they talk to
        # each other; it was captured during the two-character live rig run.
        self.assertEqual(
            classify_call(
                wrapped({"userMessage": "Other AI Message: [KILO]: I'm back."}),
                CHARACTERS,
            ),
            NPC_DIALOGUE,
        )

    def test_npc_dialogue_from_bare_text(self):
        self.assertEqual(
            classify_call(bare("Other AI Message: [BLAZE]: Back already?"), CHARACTERS),
            NPC_DIALOGUE,
        )

    def test_unknown_for_anything_else(self):
        self.assertEqual(classify_call(bare("totally unrecognised")), UNKNOWN)
        self.assertEqual(classify_call(bare("")), UNKNOWN)
        self.assertEqual(classify_call([{"role": "assistant", "content": "x"}]), UNKNOWN)
        self.assertEqual(classify_call([]), UNKNOWN)
        self.assertEqual(classify_call(None), UNKNOWN)

    def test_never_raises(self):
        for bad in [None, 5, "x", [{}], [None], [{"role": "user", "content": "\ud800"}]]:
            self.assertIsInstance(classify_call(bad, CHARACTERS), str)


class IdentifyCharacterTests(unittest.TestCase):
    def test_name_match_in_system_message(self):
        self.assertEqual(
            identify_character(with_system(SYSTEM_PROMPT), CHARACTERS)["id"], "kilo"
        )

    def test_no_match_falls_back_to_the_single_character(self):
        # With one character in the roster, every call is that character's.
        self.assertEqual(
            identify_character(
                with_system("You are a helpful assistant."),
                CHARACTERS,
            )["id"],
            "kilo",
        )

    def test_ambiguous_roster_without_a_name_returns_none(self):
        two = [
            {"id": "a", "name": "Alpha", "short_name": "A"},
            {"id": "b", "name": "Beta", "short_name": "B"},
        ]
        self.assertIsNone(identify_character(with_system("no name here"), two))

    def test_no_system_message_still_uses_the_single_character(self):
        # One character in the roster means every call is its own, so the
        # fallback must apply even when nothing in the prompt matches. Otherwise
        # a character's overrides would never fire on a prompt without a name.
        self.assertEqual(identify_character(bare("hello"), CHARACTERS)["id"], "kilo")

    def test_single_character_roster_is_the_fallback(self):
        self.assertEqual(
            identify_character(
                with_system("no character name at all"), CHARACTERS
            )["id"],
            "kilo",
        )

    def test_picks_the_right_one_of_many(self):
        two = [
            {"id": "a", "name": "Alpha", "short_name": "A"},
            {"id": "b", "name": "Beta", "short_name": "B"},
        ]
        messages = with_system("Your character's name is Beta.")
        self.assertEqual(identify_character(messages, two)["id"], "b")

    def test_empty_roster_returns_none(self):
        self.assertIsNone(identify_character(with_system(SYSTEM_PROMPT), []))
        self.assertIsNone(identify_character(with_system(SYSTEM_PROMPT), None))

    def test_short_description_is_not_used_as_a_bare_match(self):
        # Too generic to tell two characters apart.
        two = [
            {"id": "a", "name": "Alpha", "description": "A friend."},
            {"id": "b", "name": "Beta", "description": "A pal."},
        ]
        self.assertIsNone(identify_character(with_system("A friend."), two))

    def test_description_is_a_usable_secondary_signal(self):
        two = [
            {"id": "a", "name": "Alpha", "description": "A terse dwarf miner who speaks in grunts."},
            {"id": "b", "name": "Beta", "description": "A chatty elf bard who tells long stories."},
        ]
        # No "Your character's name is" line at all, only the description body.
        messages = with_system("A chatty elf bard who tells long stories.")
        self.assertEqual(identify_character(messages, two)["id"], "b")

    def test_name_containing_a_period_still_matches(self):
        # A capture-group regex on the first "." would truncate "Dr. Bob".
        roster = [{"id": "drbob", "name": "Dr. Bob", "short_name": "DR"}]
        self.assertEqual(
            identify_character(with_system("Your character's name is Dr. Bob."), roster)["id"],
            "drbob",
        )

    def test_user_message_is_not_searched(self):
        # Regression: the same phrasing appears in ordinary conversation, and
        # matching there would mis-attribute the call to another character.
        two = [
            {"id": "a", "name": "Alpha", "short_name": "A"},
            {"id": "b", "name": "Beta", "short_name": "B"},
        ]
        messages = [
            {"role": "system", "content": "Your character's name is Alpha."},
            {"role": "user", "content": "Your character's name is Beta."},
        ]
        self.assertEqual(identify_character(messages, two)["id"], "a")

    def test_last_system_message_wins(self):
        two = [
            {"id": "a", "name": "Alpha", "short_name": "A"},
            {"id": "b", "name": "Beta", "short_name": "B"},
        ]
        messages = [
            {"role": "system", "content": "Your character's name is Alpha."},
            {"role": "user", "content": "..."},
            {"role": "system", "content": "Your character's name is Beta."},
        ]
        self.assertEqual(identify_character(messages, two)["id"], "b")


class CharacterOverridesTests(unittest.TestCase):
    def test_valid_overrides_pass_through(self):
        character = {"model": "m/x:free", "temperature": 0.9, "max_tokens": 4096}
        self.assertEqual(
            character_overrides(character),
            {"model": "m/x:free", "temperature": 0.9, "max_tokens": 4096},
        )

    def test_empty_character_has_no_overrides(self):
        self.assertEqual(character_overrides({}), {})
        self.assertEqual(character_overrides(None), {})

    def test_blank_model_is_inherited(self):
        self.assertNotIn("model", character_overrides({"model": "   "}))
        self.assertNotIn("model", character_overrides({"model": 5}))

    def test_temperature_out_of_range_is_dropped(self):
        for bad in [-0.5, 2.5, 99, "hot", True, None, []]:
            self.assertNotIn("temperature", character_overrides({"temperature": bad}))

    def test_temperature_bounds_are_inclusive(self):
        self.assertEqual(character_overrides({"temperature": 0.0})["temperature"], 0.0)
        self.assertEqual(character_overrides({"temperature": 2.0})["temperature"], 2.0)

    def test_non_positive_max_tokens_is_dropped(self):
        for bad in [0, -1, "4096", True, 4.5]:
            self.assertNotIn("max_tokens", character_overrides({"max_tokens": bad}))

    def test_unrelated_keys_are_ignored(self):
        self.assertEqual(
            character_overrides({"name": "Kilo", "greeting": "hi", "meta": {}}),
            {},
        )


class ResolveRouteTests(unittest.TestCase):
    def cfg(self, **kw) -> BridgeConfig:
        base = dict(model="global/model:free", temperature=0.5, max_tokens=500000)
        base.update(kw)
        return BridgeConfig(**base)

    def test_no_overrides_reproduces_globals(self):
        route = resolve_route(wrapped(WRAPPED_CONVERSATION), CHARACTERS, self.cfg())
        self.assertEqual(route.kind, CONVERSATION)
        self.assertEqual(route.character_id, "kilo")
        self.assertEqual(route.model, "global/model:free")
        self.assertEqual(route.temperature, 0.5)
        self.assertEqual(route.max_tokens, 500000)
        self.assertEqual(route.overridden_fields, ())

    def test_character_overrides_win(self):
        characters = [
            {
                "id": "kilo",
                "name": "Kilo",
                "greeting": CHARACTERS[0]["greeting"],
                "model": "other/model:free",
                "temperature": 1.4,
                "max_tokens": 2048,
            }
        ]
        route = resolve_route(
            wrapped(WRAPPED_CONVERSATION), characters, self.cfg()
        )
        self.assertEqual(route.model, "other/model:free")
        self.assertEqual(route.temperature, 1.4)
        self.assertEqual(route.max_tokens, 2048)
        self.assertEqual(route.overridden_fields, ("max_tokens", "model", "temperature"))

    def test_partial_override_leaves_the_rest_global(self):
        characters = [{"id": "k", "name": "Kilo", "temperature": 0.1}]
        route = resolve_route(wrapped(WRAPPED_CONVERSATION), characters, self.cfg())
        self.assertEqual(route.model, "global/model:free")
        self.assertEqual(route.temperature, 0.1)
        self.assertEqual(route.max_tokens, 500000)
        self.assertEqual(route.overridden_fields, ("temperature",))

    def test_global_route_when_character_is_unidentified(self):
        route = resolve_route(bare("hello"), [], self.cfg())
        self.assertEqual(route.character_id, "")
        self.assertEqual(route.model, "global/model:free")

    def test_never_raises_on_bad_input(self):
        cfg = self.cfg()
        for bad in [None, 5, "x", [{}], [None]]:
            self.assertEqual(resolve_route(bad, [], cfg).model, "global/model:free")


class CatalogLookupTests(unittest.TestCase):
    def payload(self) -> dict:
        return {
            "data": [
                {
                    "id": "big/model:free",
                    "context_length": 262144,
                    "top_provider": {"context_length": 262144, "max_completion_tokens": 235929},
                    "supported_parameters": ["max_tokens", "temperature"],
                    "isFree": True,
                },
                {
                    "id": "small/model:free",
                    "context_length": 65536,
                    "top_provider": {"max_completion_tokens": 8192},
                    "supported_parameters": ["max_tokens"],  # no temperature
                    "isFree": True,
                },
            ]
        }

    def test_from_payload_reads_limits(self):
        catalog = ModelCatalog.from_payload(self.payload())
        self.assertEqual(catalog.context_limit("big/model:free"), 262144)
        self.assertEqual(catalog.max_completion_tokens("big/model:free"), 235929)
        self.assertTrue(catalog.supports_temperature("big/model:free"))
        self.assertIn("big/model:free", catalog)
        self.assertEqual(len(catalog), 2)

    def test_small_model_completion_cap(self):
        catalog = ModelCatalog.from_payload(self.payload())
        self.assertEqual(catalog.max_completion_tokens("small/model:free"), 8192)
        self.assertFalse(catalog.supports_temperature("small/model:free"))

    def test_unknown_model_returns_defaults(self):
        catalog = ModelCatalog.from_payload(self.payload())
        self.assertEqual(catalog.context_limit("nope", 7), 7)
        self.assertEqual(catalog.max_completion_tokens("nope", 9), 9)
        self.assertNotIn("nope", catalog)
        self.assertIsNone(catalog.get("nope"))
        self.assertIsNone(catalog.is_free("nope"))

    def test_unknown_model_keeps_temperature_supported(self):
        # The bridge has always sent temperature; omitting it for a model we have
        # no data on would silently change behaviour.
        catalog = ModelCatalog.from_payload(self.payload())
        self.assertTrue(catalog.supports_temperature("never-seen-model"))

    def test_empty_catalog_is_valid(self):
        self.assertEqual(len(ModelCatalog()), 0)
        self.assertEqual(ModelCatalog.from_payload({}), ModelCatalog())
        self.assertEqual(ModelCatalog.from_payload(None), ModelCatalog())
        self.assertEqual(ModelCatalog.from_payload({"data": []}), ModelCatalog())
        self.assertEqual(ModelCatalog.from_payload({"data": [1, 2]}), ModelCatalog())
        self.assertEqual(ModelCatalog.from_payload("nope"), ModelCatalog())

    def test_route_prefers_catalog_limit_over_global(self):
        catalog = ModelCatalog.from_payload(self.payload())
        cfg = BridgeConfig(
            model="small/model:free",
            temperature=0.5,
            max_tokens=500000,
            model_context_limit=999999,
        )
        route = resolve_route(bare(WRAPPED_CONVERSATION), CHARACTERS, cfg, catalog)
        self.assertEqual(route.context_limit, 65536)
        self.assertEqual(route.max_completion_tokens, 8192)

    def test_route_falls_back_to_global_without_a_catalog(self):
        cfg = BridgeConfig(model="m", max_tokens=500, model_context_limit=1234)
        route = resolve_route(bare(WRAPPED_CONVERSATION), CHARACTERS, cfg, None)
        self.assertEqual(route.context_limit, 1234)
        self.assertEqual(route.max_completion_tokens, 500)
        self.assertTrue(route.supports_temperature)


class ConfigCharacterOverrideTests(unittest.TestCase):
    def write_config(self, tmp, characters) -> Path:
        path = Path(tmp) / "config.json"
        path.write_text(json.dumps({"characters": characters}), encoding="utf-8")
        return path

    def test_overrides_survive_loading(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = self.write_config(
                tmp,
                [
                    {
                        "id": "kilo",
                        "name": "Kilo",
                        "model": "m/x:free",
                        "temperature": 0.8,
                        "max_tokens": 1024,
                    }
                ],
            )
            cfg = load_config(str(path))
            character = cfg.characters[0]
            self.assertEqual(character["model"], "m/x:free")
            self.assertEqual(character["temperature"], 0.8)
            self.assertEqual(character["max_tokens"], 1024)

    def test_defaults_are_blank_when_unspecified(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = self.write_config(tmp, [{"id": "kilo", "name": "Kilo"}])
            character = load_config(str(path)).characters[0]
            self.assertIsNone(character.get("model"))
            self.assertIsNone(character.get("temperature"))
            self.assertIsNone(character.get("max_tokens"))

    def test_malformed_overrides_are_dropped_not_raised(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = self.write_config(
                tmp,
                [
                    {
                        "id": "kilo",
                        "name": "Kilo",
                        "model": 12,
                        "temperature": "warm",
                        "max_tokens": -3,
                    }
                ],
            )
            character = load_config(str(path)).characters[0]
            self.assertIsNone(character.get("model"))
            self.assertIsNone(character.get("temperature"))
            self.assertIsNone(character.get("max_tokens"))
            # The rest of the entry is untouched.
            self.assertEqual(character.get("name"), "Kilo")

    def test_unrelated_keys_are_preserved(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = self.write_config(
                tmp, [{"id": "kilo", "name": "Kilo", "greeting": "hi", "meta": {"a": 1}}]
            )
            character = load_config(str(path)).characters[0]
            self.assertEqual(character.get("greeting"), "hi")
            self.assertEqual(character.get("meta"), {"a": 1})

    def test_normalize_character_directly(self):
        self.assertEqual(
            _normalize_character({"model": "  m/x  "})["model"], "m/x"
        )
        self.assertNotIn("model", _normalize_character({"model": None}))
        self.assertNotIn("temperature", _normalize_character({"temperature": 9.9}))
        self.assertNotIn("max_tokens", _normalize_character({"max_tokens": 0}))


if __name__ == "__main__":
    unittest.main()
