"""Tests for the bridge/characters.py roster CLI.

Run from the bridge/ directory:

    python3 -m unittest discover -s tests -v

The CLI is imported as a standalone module and every case works against a
throwaway characters.json in a temp dir via --path, so the seeded
bridge/characters.json is never touched.
"""

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

_BRIDGE_DIR = Path(__file__).resolve().parents[1]
if str(_BRIDGE_DIR) not in sys.path:
    sys.path.insert(0, str(_BRIDGE_DIR))

import characters  # noqa: E402  (bridge dir added to sys.path above)

_CONTRACT_KEYS = {"id", "name", "short_name", "greeting", "description", "voice_ids", "meta"}


class CharactersCliTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = str(Path(self._tmp.name) / "characters.json")

    def _add(self, name="Bob", short="BOB", greeting="Hey, I'm Bob.", description="A gruff miner NPC.", *extra):
        with contextlib.redirect_stdout(io.StringIO()):
            return characters.main(
                [
                    "add",
                    "--name", name,
                    "--short", short,
                    "--greeting", greeting,
                    "--description", description,
                    "--path", self.path,
                    *extra,
                ]
            )

    def _read(self):
        return json.loads(Path(self.path).read_text(encoding="utf-8"))

    def test_add_creates_contract_shaped_character(self):
        self.assertEqual(self._add(), 0)
        data = self._read()
        self.assertIn("characters", data)
        self.assertEqual(len(data["characters"]), 1)
        character = data["characters"][0]
        self.assertEqual(set(character), _CONTRACT_KEYS)
        self.assertEqual(character["name"], "Bob")
        self.assertEqual(character["short_name"], "BOB")
        self.assertEqual(character["greeting"], "Hey, I'm Bob.")
        self.assertEqual(character["description"], "A gruff miner NPC.")
        self.assertEqual(character["voice_ids"], [])
        self.assertEqual(character["meta"], {"skin_url": ""})

    def test_add_preserves_other_top_level_keys(self):
        Path(self.path).write_text(json.dumps({"characters": [], "note": "keep me"}), encoding="utf-8")
        self.assertEqual(self._add(), 0)
        data = self._read()
        self.assertEqual(data["note"], "keep me")
        self.assertEqual(len(data["characters"]), 1)

    def test_written_file_is_pretty_printed(self):
        self.assertEqual(self._add(), 0)
        text = Path(self.path).read_text(encoding="utf-8")
        self.assertIn('\n  "characters"', text)

    def test_add_with_voice_ids_and_skin_url(self):
        with contextlib.redirect_stdout(io.StringIO()):
            rc = characters.main(
                [
                    "add",
                    "--name", "Vex",
                    "--short", "VX",
                    "--greeting", "Careful now.",
                    "--description", "A cautious scout.",
                    "--voice-ids", "scout_a, scout_b",
                    "--skin-url", "https://example.com/skins/vex.png",
                    "--path", self.path,
                ]
            )
        self.assertEqual(rc, 0)
        character = self._read()["characters"][0]
        self.assertEqual(character["voice_ids"], ["scout_a", "scout_b"])
        self.assertEqual(character["meta"], {"skin_url": "https://example.com/skins/vex.png"})

    def test_add_refuses_duplicate_name(self):
        self.assertEqual(self._add(), 0)
        self.assertEqual(self._add(name="Bob", short="OTHER"), 1)
        self.assertEqual(len(self._read()["characters"]), 1)

    def test_add_refuses_duplicate_short(self):
        self.assertEqual(self._add(), 0)
        self.assertEqual(self._add(name="Other", short="BOB"), 1)
        self.assertEqual(len(self._read()["characters"]), 1)

    def test_add_requires_mandatory_fields(self):
        self.assertEqual(
            characters.main(
                ["add", "--name", "", "--short", "X", "--greeting", "g", "--description", "d", "--path", self.path]
            ),
            1,
        )
        self.assertEqual(
            characters.main(
                ["add", "--name", "N", "--greeting", "g", "--description", "d", "--path", self.path]
            ),
            1,
        )
        self.assertFalse(Path(self.path).exists())

    def test_force_allows_duplicate_short_and_appends(self):
        self.assertEqual(self._add(name="Bob", short="BOB"), 0)
        self.assertEqual(
            self._add("Robert", "BOB", "Hey, I'm Bob.", "A gruff miner NPC.", "--force"), 0
        )
        characters_ = self._read()["characters"]
        self.assertEqual(len(characters_), 2)
        self.assertEqual([c["short_name"] for c in characters_], ["BOB", "BOB"])

    def test_list_shows_character(self):
        self.assertEqual(self._add(), 0)
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            rc = characters.main(["list", "--path", self.path])
        output = buffer.getvalue()
        self.assertEqual(rc, 0)
        self.assertIn("Bob", output)
        self.assertIn("BOB", output)
        self.assertIn("1 character(s)", output)

    def test_list_on_missing_and_empty(self):
        for _ in (0, 1):
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                rc = characters.main(["list", "--path", self.path])
            self.assertEqual(rc, 0)
            self.assertIn("no characters configured", buffer.getvalue())
            Path(self.path).write_text('{"characters": []}', encoding="utf-8")

    def test_remove_deletes_then_errors(self):
        self.assertEqual(self._add(), 0)
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            rc = characters.main(["remove", "--name", "Bob", "--path", self.path])
        self.assertEqual(rc, 0)
        self.assertEqual(self._read()["characters"], [])
        self.assertEqual(characters.main(["remove", "--name", "Bob", "--path", self.path]), 1)

    def test_remove_matches_short_name(self):
        self.assertEqual(self._add(), 0)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(characters.main(["remove", "--name", "BOB", "--path", self.path]), 0)
        self.assertEqual(self._read()["characters"], [])

    def test_show_prints_full_json(self):
        self.assertEqual(self._add(), 0)
        for query in ("Bob", "BOB"):
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                rc = characters.main(["show", "--name", query, "--path", self.path])
            self.assertEqual(rc, 0)
            character = json.loads(buffer.getvalue())
            self.assertEqual(set(character), _CONTRACT_KEYS)
            self.assertEqual(character["name"], "Bob")

    def test_show_missing_exits_1(self):
        self.assertEqual(characters.main(["show", "--name", "nope", "--path", self.path]), 1)

    def test_add_on_missing_file_creates_it(self):
        self.assertFalse(Path(self.path).exists())
        self.assertEqual(self._add(), 0)
        self.assertTrue(Path(self.path).exists())
        data = self._read()
        self.assertEqual(list(data), ["characters"])
        self.assertEqual(len(data["characters"]), 1)

    def test_add_generates_id_from_short_name(self):
        self.assertEqual(self._add(), 0)
        self.assertEqual(self._read()["characters"][0]["id"], "bob")
        self.assertEqual(
            self._add(name="Weird Name!", short="W@#$E IRD", greeting="g", description="d"), 0
        )
        self.assertEqual(self._read()["characters"][1]["id"], "w-e-ird")

    def test_add_generates_unique_id_when_slug_taken(self):
        self.assertEqual(self._add("First", "BOB"), 0)
        self.assertEqual(self._add("Second", "BOB", "Hi.", "Another.", "--force"), 0)
        characters_ = self._read()["characters"]
        self.assertEqual([c["id"] for c in characters_], ["bob", "char-bob"])

    def test_add_respects_explicit_id(self):
        self.assertEqual(self._add("Bob", "BOB", "Hey, I'm Bob.", "A gruff miner NPC.", "--id", "bob_the_miner"), 0)
        self.assertEqual(self._read()["characters"][0]["id"], "bob_the_miner")

    def test_add_rejects_invalid_id(self):
        for bad in ("has space", "no/slashes", "tabs\there", "x" * 65, "", "   "):
            with contextlib.redirect_stderr(io.StringIO()):
                rc = self._add("Bob", "BOB", "Hey.", "A miner.", "--id", bad)
            self.assertEqual(rc, 1, msg=f"--id {bad!r} should be rejected")
            self.assertFalse(Path(self.path).exists())

    def test_add_rejects_id_collision_unless_force(self):
        self.assertEqual(self._add(), 0)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(self._add("Bobbie", "BOBBY", "Hi.", "Another.", "--id", "BOB"), 1)
        self.assertEqual(len(self._read()["characters"]), 1)
        self.assertEqual(
            self._add("Bobbie", "BOBBY", "Hi.", "Another.", "--id", "BOB", "--force"), 0
        )
        self.assertEqual([c["id"] for c in self._read()["characters"]], ["bob", "BOB"])

    def test_list_shows_id_column(self):
        self.assertEqual(self._add(), 0)
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            rc = characters.main(["list", "--path", self.path])
        output = buffer.getvalue()
        self.assertEqual(rc, 0)
        header, _, first_row = output.partition("\n")
        self.assertIn("ID", header)
        self.assertTrue(first_row.startswith("bob"), msg=output)
        self.assertIn("1 character(s)", output)

    def test_add_leaves_legacy_entries_without_ids_alone(self):
        Path(self.path).write_text(
            json.dumps(
                {
                    "characters": [
                        {
                            "name": "Legacy",
                            "short_name": "LEGACY",
                            "greeting": "old",
                            "description": "no id here",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        self.assertEqual(self._add(), 0)
        characters_ = self._read()["characters"]
        self.assertEqual("id" in characters_[0], False)
        self.assertEqual(characters_[1]["id"], "bob")


if __name__ == "__main__":
    unittest.main()
