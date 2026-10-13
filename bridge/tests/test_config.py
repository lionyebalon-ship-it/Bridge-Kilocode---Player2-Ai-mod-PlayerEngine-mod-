"""Tests for p2k_bridge.config: defaults, coercion, .env parsing and API-key precedence.

Hermetic and secret-safe by design:
  * Every config file and .env file lives in a tempfile.TemporaryDirectory; the
    real repository .env (e.g. /workspace/app/.env, which may carry a live
    KILO_API_KEY) is never read.  Tests that only exercise JSON behaviour stub
    the .env fallback out entirely, and tests that walk the full resolution
    chain redirect the repo-root candidate to a temp path, so the real
    repo-root .env is never parsed.
  * API-key resolution always runs through the bespoke env-var name
    P2K_GAP_KEY, which occurs in no real .env, so the shipped KILO_API_KEY can
    never interfere or leak into a test.
  * os.environ is always patched with unittest.mock.patch.dict(..., clear=True)
    so ambient variables (P2K_CONFIG, KILO_API_KEY, ...) cannot leak in.
  * Key values are only ever obvious throwaway literals such as
    'FAKE_KEY_FROM_ENV'; no real secret is read or printed anywhere.
"""

import contextlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import p2k_bridge.config as cfgmod
from p2k_bridge.config import (
    BridgeConfig,
    _coerce_bool,
    _env_file_candidates,
    _parse_env_file,
    load_config,
)

# Bespoke env-var name used for every API-key test: it cannot occur in any
# real .env, so the shipped KILO_API_KEY can never be picked up by accident.
GAP_KEY = "P2K_GAP_KEY"
REPO_ROOT_ENV = Path(cfgmod.__file__).resolve().parents[2] / ".env"


def _write_json(path: Path, data) -> Path:
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


class _HermeticDotenvCase(unittest.TestCase):
    """Base for load_config tests that must not consult any .env file.

    The environment is emptied and the .env fallback is stubbed out so the
    real repo-root .env is never parsed while pure JSON behaviour is under
    test.  The full resolution chain is covered deterministically in
    ApiKeyPrecedenceTests.
    """

    def setUp(self):
        env_patcher = mock.patch.dict(os.environ, {}, clear=True)
        env_patcher.start()
        self.addCleanup(env_patcher.stop)
        dotenv_patcher = mock.patch.object(cfgmod, "_dotenv_value", lambda *_a, **_k: "")
        dotenv_patcher.start()
        self.addCleanup(dotenv_patcher.stop)

    def _write_config(self, tmp, data):
        return _write_json(Path(tmp) / "config.json", data)


class ReprTests(unittest.TestCase):
    def test_repr_masks_api_key_value(self):
        secret = "sk-NEVER-PRINT-abcdef123456"
        text = repr(BridgeConfig(kilo_api_key=secret))
        self.assertIn("kilo_api_key='***'", text)
        self.assertNotIn(secret, text)

    def test_repr_empty_key_renders_empty_string_not_mask(self):
        self.assertIn("kilo_api_key=''", repr(BridgeConfig(kilo_api_key="")))

    def test_repr_lists_every_field(self):
        text = repr(BridgeConfig())
        self.assertTrue(text.startswith("BridgeConfig("))
        for name in ("listen_host", "http_port", "tls_enabled", "model", "characters"):
            self.assertIn(f"{name}=", text)


class LoadConfigFileTests(_HermeticDotenvCase):
    def test_missing_file_falls_back_to_all_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = load_config(str(Path(tmp) / "does-not-exist.json"))
        default = BridgeConfig()
        self.assertEqual(cfg.http_port, default.http_port)
        self.assertEqual(cfg.requests_per_hour, default.requests_per_hour)
        self.assertEqual(cfg.model, default.model)
        self.assertEqual(cfg.kilo_api_key, "")

    def test_malformed_json_falls_back_to_all_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text("{ this is not json", encoding="utf-8")
            cfg = load_config(str(path))
        self.assertEqual(cfg.http_port, 9800)
        self.assertEqual(cfg.model, BridgeConfig().model)
        self.assertEqual(cfg.kilo_api_key, "")

    def test_non_dict_json_falls_back_to_all_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_json(Path(tmp) / "config.json", ["not", "a", "mapping"])
            cfg = load_config(str(path))
        self.assertEqual(cfg.requests_per_hour, 180)
        self.assertEqual(cfg.model, BridgeConfig().model)

    def test_partial_config_overrides_only_supplied_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_config(tmp, {"http_port": 1234, "model": "gap/model-x"})
            cfg = load_config(str(path))
        self.assertEqual(cfg.http_port, 1234)
        self.assertEqual(cfg.model, "gap/model-x")
        self.assertEqual(cfg.requests_per_hour, 180)  # untouched -> default
        self.assertEqual(cfg.temperature, 0.5)

    def test_none_and_unknown_fields_are_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_config(
                tmp, {"model": None, "totally_unknown": 5, "max_tokens": 321}
            )
            cfg = load_config(str(path))
        self.assertEqual(cfg.model, BridgeConfig().model)  # None -> default
        self.assertEqual(cfg.max_tokens, 321)
        self.assertFalse(hasattr(cfg, "totally_unknown"))  # extra key ignored

    def test_missing_argument_reads_config_json_from_cwd(self):
        # No argument and no P2K_CONFIG env var -> ./config.json relative to cwd.
        with tempfile.TemporaryDirectory() as tmp:
            old = os.getcwd()
            self.addCleanup(os.chdir, old)
            os.chdir(tmp)
            _write_json(Path("config.json"), {"model": "cwd/model"})
            cfg = load_config()
        self.assertEqual(cfg.model, "cwd/model")


class CoerceBoolTests(unittest.TestCase):
    def test_true_false_string_sets(self):
        for truthy in ("1", "true", "YES", "On", "y", "t", "  TrUe "):
            self.assertIs(_coerce_bool(truthy), True, truthy)
        for falsy in ("0", "false", "No", "OFF", "n", "f"):
            self.assertIs(_coerce_bool(falsy), False, falsy)

    def test_passthrough_and_unrecognized_values(self):
        self.assertIs(_coerce_bool(True), True)
        self.assertIs(_coerce_bool(False), False)
        self.assertIs(_coerce_bool(0), False)
        self.assertIs(_coerce_bool(2.5), True)
        self.assertIsNone(_coerce_bool("maybe"))
        self.assertIsNone(_coerce_bool([1]))


class IntCoercionTests(_HermeticDotenvCase):
    def test_int_fields_from_numeric_strings(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_config(
                tmp,
                {"http_port": "1234", "max_tokens": "500000", "requests_per_hour": "180"},
            )
            cfg = load_config(str(path))
        self.assertEqual(cfg.http_port, 1234)
        self.assertEqual(cfg.max_tokens, 500000)
        self.assertEqual(cfg.requests_per_hour, 180)

    def test_int_field_from_float_string_truncates(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_config(tmp, {"requests_per_hour": "18.9"})
            cfg = load_config(str(path))
        self.assertEqual(cfg.requests_per_hour, 18)  # int(float("18.9"))

    def test_unparseable_int_left_verbatim(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_config(tmp, {"requests_per_hour": "not-a-number"})
            cfg = load_config(str(path))
        self.assertEqual(cfg.requests_per_hour, "not-a-number")  # kept as-is


class FloatCoercionTests(_HermeticDotenvCase):
    def test_float_fields_from_numeric_strings(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_config(
                tmp,
                {
                    "window_seconds": "3600.5",
                    "temperature": "0.7",
                    "request_timeout_seconds": "90",
                },
            )
            cfg = load_config(str(path))
        self.assertEqual(cfg.window_seconds, 3600.5)
        self.assertEqual(cfg.temperature, 0.7)
        self.assertEqual(cfg.request_timeout_seconds, 90.0)
        self.assertIsInstance(cfg.request_timeout_seconds, float)

    def test_unparseable_float_left_verbatim(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_config(tmp, {"temperature": "hot"})
            cfg = load_config(str(path))
        self.assertEqual(cfg.temperature, "hot")  # kept as-is


class BoolCoercionTests(_HermeticDotenvCase):
    def test_bool_field_from_string_vocabulary(self):
        with tempfile.TemporaryDirectory() as tmp:
            for text, expected in (
                ("true", True),
                ("yes", True),
                ("1", True),
                ("false", False),
                ("no", False),
                ("0", False),
            ):
                path = self._write_config(tmp, {"tls_enabled": text})
                self.assertIs(load_config(str(path)).tls_enabled, expected, text)

    def test_unparseable_bool_left_verbatim(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_config(tmp, {"tls_enabled": "maybe"})
            cfg = load_config(str(path))
        self.assertEqual(cfg.tls_enabled, "maybe")  # kept as-is, not False


class StrCoercionTests(_HermeticDotenvCase):
    def test_number_for_str_field_is_stringified(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_config(tmp, {"model": 123})
            cfg = load_config(str(path))
        self.assertEqual(cfg.model, "123")


class CharactersCoercionTests(_HermeticDotenvCase):
    def test_characters_list_of_dicts_filters_non_dicts(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_config(tmp, {"characters": [{"name": "a"}, "junk", 2, {"name": "b"}]})
            cfg = load_config(str(path))
        self.assertEqual(cfg.characters, [{"name": "a"}, {"name": "b"}])

    def test_single_dict_characters_wrapped_in_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_config(tmp, {"characters": {"name": "Solo", "short_name": "SO"}})
            cfg = load_config(str(path))
        self.assertEqual(cfg.characters, [{"name": "Solo", "short_name": "SO"}])

    def test_uncoercible_characters_kept_verbatim(self):
        # Source behaviour: an un-coercible characters value (e.g. a plain
        # string) is stored as-is; only *missing* characters fall back to the
        # default roster.
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_config(tmp, {"characters": "junk"})
            cfg = load_config(str(path))
        self.assertEqual(cfg.characters, "junk")

    def test_none_or_missing_characters_use_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_config(tmp, {"characters": None, "http_port": 9999})
            cfg = load_config(str(path))
        self.assertEqual(cfg.characters, cfgmod.DEFAULT_CHARACTERS)
        self.assertEqual(cfg.characters, BridgeConfig().characters)
        self.assertEqual(cfg.http_port, 9999)


class ParseEnvFileTests(unittest.TestCase):
    def test_missing_file_yields_empty_dict(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(_parse_env_file(Path(tmp) / ".env"), {})

    def test_comments_blanks_and_equals_less_lines_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text(
                "# a comment\n"
                "\n"
                "line-without-equals\n"
                "URL=http://host/path?a=b\n",
                encoding="utf-8",
            )
            parsed = _parse_env_file(path)
        self.assertEqual(parsed, {"URL": "http://host/path?a=b"})  # split on first =

    def test_export_prefix_quotes_and_padding_stripped(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text(
                'export  TOKEN="quoted value"\n'
                "SINGLE='single quoted'\n"
                "  PADDED  =  spaced value  \n",
                encoding="utf-8",
            )
            parsed = _parse_env_file(path)
        self.assertEqual(parsed["TOKEN"], "quoted value")
        self.assertEqual(parsed["SINGLE"], "single quoted")
        self.assertEqual(parsed["PADDED"], "spaced value")
        self.assertNotIn("export  TOKEN", parsed)


class EnvCandidatesTests(unittest.TestCase):
    def test_candidate_order_config_dir_cwd_then_repo_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            candidates = _env_file_candidates(Path(tmp) / "config.json")
        self.assertEqual(candidates[0], Path(tmp) / ".env")
        self.assertEqual(candidates[1], Path.cwd() / ".env")
        self.assertEqual(candidates[-1], REPO_ROOT_ENV)
        self.assertEqual(len(candidates), 3)

    def test_candidates_deduplicated_when_config_dir_equals_cwd(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp).resolve()
            old = os.getcwd()
            self.addCleanup(os.chdir, old)
            os.chdir(target)
            candidates = _env_file_candidates(target / "config.json")
        resolved = [c.resolve() for c in candidates]
        self.assertEqual(len(resolved), len(set(resolved)))  # no duplicates
        self.assertEqual(len(candidates), 2)  # config-dir == cwd collapses
        self.assertEqual(candidates[-1], REPO_ROOT_ENV)  # repo root stays last


class ApiKeyPrecedenceTests(unittest.TestCase):
    """Documented order: config value > env var > config-dir .env > cwd .env > repo-root .env."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.config_dir = self.root / "config-dir"
        self.config_dir.mkdir()
        self.work_dir = self.root / "workdir"
        self.work_dir.mkdir()

    def _write_config(self, data):
        return _write_json(self.config_dir / "config.json", data)

    def _write_dotenv(self, directory, text):
        (Path(directory) / ".env").write_text(text, encoding="utf-8")

    def _key_config(self):
        return {"kilo_api_key": "", "kilo_api_key_env": GAP_KEY}

    def _clear_env(self, **values):
        patcher = mock.patch.dict(os.environ, values, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _chdir_workdir(self):
        old = os.getcwd()
        self.addCleanup(os.chdir, old)
        os.chdir(str(self.work_dir))

    @contextlib.contextmanager
    def _redirect_repo_root(self, dotenv_text=None):
        """Point the repo-root .env candidate at a temp file.

        The coded candidate order is untouched; only the last path is
        redirected so the real repository .env -- which may carry a live
        KILO_API_KEY -- is never parsed.
        """
        fake_repo = self.root / "fake-repo-root"
        fake_repo.mkdir(exist_ok=True)
        if dotenv_text is not None:
            self._write_dotenv(fake_repo, dotenv_text)
        target = fake_repo / ".env"
        real = cfgmod._env_file_candidates

        def patched(config_path):
            return [
                target if candidate.resolve() == REPO_ROOT_ENV else candidate
                for candidate in real(config_path)
            ]

        with mock.patch.object(cfgmod, "_env_file_candidates", patched):
            yield

    def test_config_file_value_beats_env_var_and_dotenv(self):
        path = self._write_config(
            {"kilo_api_key": "FAKE_KEY_FROM_CONFIG", "kilo_api_key_env": GAP_KEY}
        )
        self._write_dotenv(self.config_dir, f"{GAP_KEY}=FAKE_KEY_FROM_CONFIG_DIR\n")
        self._clear_env(**{GAP_KEY: "FAKE_KEY_FROM_ENV"})
        cfg = load_config(str(path))
        self.assertEqual(cfg.kilo_api_key, "FAKE_KEY_FROM_CONFIG")

    def test_env_var_beats_config_dir_dotenv(self):
        path = self._write_config(self._key_config())
        self._write_dotenv(self.config_dir, f"{GAP_KEY}=FAKE_KEY_FROM_CONFIG_DIR\n")
        self._clear_env(**{GAP_KEY: "FAKE_KEY_FROM_ENV"})
        cfg = load_config(str(path))
        self.assertEqual(cfg.kilo_api_key, "FAKE_KEY_FROM_ENV")

    def test_config_dir_dotenv_beats_cwd_and_repo_root(self):
        path = self._write_config(self._key_config())
        self._write_dotenv(self.config_dir, f"{GAP_KEY}=FAKE_KEY_FROM_CONFIG_DIR\n")
        self._write_dotenv(self.work_dir, f"{GAP_KEY}=FAKE_KEY_FROM_CWD\n")
        self._chdir_workdir()
        self._clear_env()
        cfg = load_config(str(path))
        self.assertEqual(cfg.kilo_api_key, "FAKE_KEY_FROM_CONFIG_DIR")

    def test_cwd_dotenv_used_when_config_dir_lacks_key(self):
        path = self._write_config(self._key_config())  # no .env in the config dir
        self._write_dotenv(self.work_dir, f'{GAP_KEY}="FAKE_KEY_FROM_CWD"\n')
        self._chdir_workdir()
        self._clear_env()
        cfg = load_config(str(path))
        self.assertEqual(cfg.kilo_api_key, "FAKE_KEY_FROM_CWD")

    def test_repo_root_dotenv_used_as_last_resort(self):
        # No .env in the config dir or cwd: the chain must fall through to the
        # repo-root candidate (redirected to a temp path here).
        path = self._write_config(self._key_config())
        self._chdir_workdir()
        self._clear_env()
        with self._redirect_repo_root(f"{GAP_KEY}=FAKE_KEY_FROM_REPO_ROOT\n"):
            cfg = load_config(str(path))
        self.assertEqual(cfg.kilo_api_key, "FAKE_KEY_FROM_REPO_ROOT")

    def test_no_key_anywhere_leaves_key_empty(self):
        path = self._write_config(self._key_config())
        self._chdir_workdir()
        self._clear_env()
        with self._redirect_repo_root():  # redirected, empty repo-root .env
            cfg = load_config(str(path))
        self.assertEqual(cfg.kilo_api_key, "")

    def test_blank_env_name_falls_back_to_kilo_api_key(self):
        # A blank kilo_api_key_env means the KILO_API_KEY name is used; the
        # config-dir .env (candidate #0) wins over any repo-root .env.
        path = self._write_config({"kilo_api_key": "", "kilo_api_key_env": ""})
        self._write_dotenv(self.config_dir, "KILO_API_KEY=FAKE_KEY_FROM_CONFIG_DIR\n")
        self._clear_env()
        cfg = load_config(str(path))
        self.assertEqual(cfg.kilo_api_key, "FAKE_KEY_FROM_CONFIG_DIR")

    def test_whitespace_only_key_falls_through_to_stripped_env_value(self):
        path = self._write_config({"kilo_api_key": "   ", "kilo_api_key_env": GAP_KEY})
        self._clear_env(**{GAP_KEY: "  FAKE_KEY_FROM_ENV  "})
        cfg = load_config(str(path))
        self.assertEqual(cfg.kilo_api_key, "FAKE_KEY_FROM_ENV")  # env value stripped


if __name__ == "__main__":
    unittest.main()
