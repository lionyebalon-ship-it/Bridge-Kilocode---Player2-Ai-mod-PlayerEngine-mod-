"""Tests for bridge/doctor.py — the offline pre-flight diagnostic.

Hermetic and secret-safe by design (mirrors tests/test_config.py):
  * No real network: every HTTP/DNS/TLS call goes through the injectable
    ``getter`` / ``poster`` / ``resolver`` / ``peer`` / ``decode_pem`` /
    ``run_keytool`` hooks doctor.py exposes, returning hand-built
    ``HttpResult`` values.  Nothing here opens a socket to the internet.
  * The real repository ``.env`` is NEVER read: check_env tests always pass an
    explicit temp ``env_path`` plus an injected ``environ`` dict, and the CLI
    tests monkeypatch both ``load_config`` and ``run_all`` so no ``.env`` /
    disk / network dependency is reached.
  * No key value is ever printed: the fake key ``FAKE_CHECK_KEY`` is asserted
    ABSENT from every rendered detail/fix string, while its length is present.
  * TLS cert checks use the repo's real tls/certs (they exist and decode via
    openssl) and, for the wrong-SAN case, a throwaway self-signed cert minted
    into a temp dir with ``openssl`` (skipped cleanly if openssl is missing).

Only the current source behaviour is asserted; no source file is modified.
"""

import contextlib
import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

_BRIDGE_DIR = Path(__file__).resolve().parents[1]
if str(_BRIDGE_DIR) not in sys.path:
    sys.path.insert(0, str(_BRIDGE_DIR))

import doctor  # noqa: E402  (bridge dir added to sys.path above)

from p2k_bridge.config import BridgeConfig  # noqa: E402

# A bespoke, obviously-fake key value used everywhere an API key is needed.
# It occurs in no real .env, so the shipped KILO_API_KEY can never interfere,
# and its literal must never appear in any rendered output.
FAKE_KEY = "FAKE_CHECK_KEY_DO_NOT_PRINT_abcdef0123456789"
FAKE_KEY_LEN = len(FAKE_KEY)

REPO_CERTS = _BRIDGE_DIR.parent / "tls" / "certs"
TARGET = doctor.TARGET_HOSTNAME


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _proc(returncode, stderr="", stdout=""):
    """A minimal stand-in for the object subprocess.run() would return."""
    return SimpleNamespace(returncode=returncode, stderr=stderr, stdout=stdout)


def _ok_getter(data, status=200, kind=None, error=None):
    """Build a getter closure returning a fixed HttpResult."""
    text = "" if not isinstance(data, dict) else json.dumps(data)
    return lambda url, timeout=None, headers=None, context=None: doctor.HttpResult(
        status, data if isinstance(data, dict) else None, text, error, kind
    )


def _ok_poster(data, status=200, kind=None, error=None):
    text = "" if not isinstance(data, dict) else json.dumps(data)
    return lambda url, body=None, timeout=None, headers=None, context=None: doctor.HttpResult(
        status, data if isinstance(data, dict) else None, text, error, kind
    )


def _refused_getter():
    return lambda url, timeout=None, headers=None, context=None: doctor.HttpResult(
        None, None, "", "ConnectionRefusedError: refused", "refused"
    )


def _refused_poster():
    return lambda url, body=None, timeout=None, headers=None, context=None: doctor.HttpResult(
        None, None, "", "ConnectionRefusedError: refused", "refused"
    )


def _envelope_content(message="hello there friend", reason="cordial", command="wave"):
    return json.dumps({"reason": reason, "command": command, "message": message})


def _assert_key_hidden(test, *texts):
    blob = "".join(t for t in texts if t)
    test.assertNotIn(FAKE_KEY, blob, "API key value leaked into output")


def _has_openssl():
    return shutil.which("openssl") is not None


def _make_fake_home(root):
    """Create a tmp 'HOME' with a Minecraft launcher java runtime + cacerts."""
    home = Path(root)
    cacerts = home / ".minecraft" / "runtimes" / "java-runtime-alpha" / "lib" / "security" / "cacerts"
    cacerts.parent.mkdir(parents=True, exist_ok=True)
    cacerts.write_text("fake-jvm-truststore", encoding="utf-8")
    return home, cacerts.parent.parent  # (home, java-runtime-alpha/)


# --------------------------------------------------------------------------- #
# 1. python version
# --------------------------------------------------------------------------- #
class CheckPythonVersionTests(unittest.TestCase):
    def test_modern_version_passes(self):
        chk = doctor.check_python_version((3, 12, 1))
        self.assertEqual(chk.status, doctor.STATUS_PASS)
        self.assertEqual(chk.number, 1)
        self.assertIn("3.12.1", chk.detail)
        self.assertEqual(chk.fix, "")

    def test_old_version_fails_with_fix(self):
        chk = doctor.check_python_version((3, 9, 7))
        self.assertEqual(chk.status, doctor.STATUS_FAIL)
        self.assertIn("3.9.7", chk.detail)
        self.assertIn("3.10", chk.fix)

    def test_default_uses_running_interpreter(self):
        # No injection -> reflects sys.version_info (>=3.10 test runner).
        chk = doctor.check_python_version()
        self.assertEqual(chk.status, doctor.STATUS_PASS)
        self.assertEqual(chk.number, 1)


# --------------------------------------------------------------------------- #
# 2. .env / api key
# --------------------------------------------------------------------------- #
class CheckEnvTests(unittest.TestCase):
    def _write_dotenv(self, directory, value):
        path = Path(directory) / ".env"
        path.write_text(f"{doctor.ENV_NAME}={value}\n", encoding="utf-8")
        return path

    def test_configured_key_passes_from_var_or_file_and_var_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_file_value = "FAKE_CHECK_KEY_from_file_zzz"
            # (a) via injected environ
            chk = doctor.check_env(Path(tmp) / ".env", environ={doctor.ENV_NAME: FAKE_KEY})
            self.assertEqual(chk.status, doctor.STATUS_PASS)
            _assert_key_hidden(self, chk.detail, chk.fix)
            self.assertIn(f"length {FAKE_KEY_LEN}", chk.detail)
            self.assertIn("environment variable", chk.detail)

            # (b) via a real .env file on disk
            path = self._write_dotenv(tmp, env_file_value)
            chk2 = doctor.check_env(path, environ={})
            self.assertEqual(chk2.status, doctor.STATUS_PASS)
            _assert_key_hidden(self, chk2.detail, chk2.fix)
            self.assertIn(path.name, chk2.detail)  # source is the file name
            self.assertNotIn(env_file_value, chk2.detail)

            # (c) environ beats the file, and never shows either value
            chk3 = doctor.check_env(path, environ={doctor.ENV_NAME: FAKE_KEY})
            self.assertEqual(chk3.status, doctor.STATUS_PASS)
            self.assertIn("environment variable", chk3.detail)
            _assert_key_hidden(self, chk3.detail, chk3.fix)
            self.assertNotIn(env_file_value, chk3.detail)

    def test_missing_key_warns_for_absent_and_present_but_empty_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            # (a) no file at all
            chk = doctor.check_env(Path(tmp) / "nope.env", environ={})
            self.assertEqual(chk.status, doctor.STATUS_WARN)
            self.assertIn("not in the environment", chk.detail)
            self.assertTrue(chk.fix)  # free-tier advice present

            # (b) file exists but has no KILO_API_KEY
            path = Path(tmp) / ".env"
            path.write_text("# nothing here\nSOME_OTHER=x\n", encoding="utf-8")
            chk2 = doctor.check_env(path, environ={})
            self.assertEqual(chk2.status, doctor.STATUS_WARN)
            self.assertIn("exists but KILO_API_KEY is not set", chk2.detail)

    def test_placeholder_values_warn_and_hide_value(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_dotenv(tmp, doctor.ENV_PLACEHOLDER)
            chk = doctor.check_env(path, environ={})
            self.assertEqual(chk.status, doctor.STATUS_WARN)
            self.assertIn("still the placeholder", chk.detail)

            # a 'paste-your-*' variant via the environment is also a placeholder
            chk2 = doctor.check_env(Path(tmp) / "x.env", environ={doctor.ENV_NAME: "paste-your-key"})
            self.assertEqual(chk2.status, doctor.STATUS_WARN)
            self.assertIn("placeholder", chk2.detail)
            _assert_key_hidden(self, chk2.detail, chk2.fix)


# --------------------------------------------------------------------------- #
# 3. hosts resolution
# --------------------------------------------------------------------------- #
class CheckHostsTests(unittest.TestCase):
    def test_loopback_resolves_pass(self):
        chk = doctor.check_hosts(resolver=lambda host: doctor.LOOPBACK_IP)
        self.assertEqual(chk.status, doctor.STATUS_PASS)
        self.assertEqual(chk.number, 4)
        self.assertEqual(chk.fix, "")
        self.assertIn(doctor.LOOPBACK_IP, chk.detail)

    def test_foreign_ip_fails_with_hosts_fix(self):
        chk = doctor.check_hosts(resolver=lambda host: "203.0.113.9")
        self.assertEqual(chk.status, doctor.STATUS_FAIL)
        self.assertIn("203.0.113.9", chk.detail)
        self.assertIn(f"/etc/hosts: {doctor.LOOPBACK_IP} {doctor.TARGET_HOSTNAME}", chk.fix)

    def test_unresolvable_warns_and_appends_reason(self):
        def boom(_host):
            raise ValueError("dns exploded")

        chk = doctor.check_hosts(resolver=boom)
        self.assertEqual(chk.status, doctor.STATUS_WARN)
        self.assertIn("does not resolve", chk.detail)
        self.assertIn("ValueError: dns exploded", chk.detail)
        self.assertIn("/etc/hosts", chk.fix)


# --------------------------------------------------------------------------- #
# 4. tls certs
# --------------------------------------------------------------------------- #
class CheckCertsTests(unittest.TestCase):
    def test_real_repo_certs_pass(self):
        if not (REPO_CERTS / "ca.crt").is_file() or not (REPO_CERTS / "server.crt").is_file():
            self.skipTest("repo tls certs missing")
        chk = doctor.check_certs(REPO_CERTS)
        self.assertEqual(chk.status, doctor.STATUS_PASS)
        self.assertIn(TARGET, chk.detail)
        self.assertEqual(chk.number, 3)

    def test_missing_cert_files_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            chk = doctor.check_certs(Path(tmp))
        self.assertEqual(chk.status, doctor.STATUS_FAIL)
        self.assertIn("ca.crt", chk.detail)
        self.assertIn("server.crt", chk.detail)
        self.assertIn("gen_certs.sh", chk.fix)

    def test_expired_cert_fails(self):
        if not (REPO_CERTS / "server.crt").is_file():
            self.skipTest("repo tls certs missing")
        far_future = datetime(2045, 6, 1, tzinfo=timezone.utc)
        chk = doctor.check_certs(REPO_CERTS, now=far_future)
        self.assertEqual(chk.status, doctor.STATUS_FAIL)
        self.assertIn("EXPIRED", chk.detail)

    def test_unparseable_cert_warns(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "ca.crt").write_text("not a real ca\n", encoding="utf-8")
            (Path(tmp) / "server.crt").write_text("garbage leaf\n", encoding="utf-8")
            chk = doctor.check_certs(Path(tmp))
        self.assertEqual(chk.status, doctor.STATUS_WARN)
        self.assertIn("could not be parsed", chk.detail)
        self.assertIn("openssl", chk.fix)

    def test_wrong_san_fails(self):
        if not _has_openssl():
            self.skipTest("openssl unavailable")
        with tempfile.TemporaryDirectory() as tmp:
            key = Path(tmp) / "server.key"
            crt = Path(tmp) / "server.crt"
            proc = subprocess.run(
                ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                 "-keyout", str(key), "-out", str(crt), "-days", "825",
                 "-subj", "/O=p2k-bridge/CN=not-target.example",
                 "-addext", "subjectAltName=DNS:not-target.example,IP:10.9.8.7"],
                capture_output=True, text=True, timeout=30,
            )
            if proc.returncode != 0:
                self.skipTest(f"openssl cert generation failed: {proc.stderr[:120]!r}")
            # check_certs needs both ca.crt and server.crt present
            (Path(tmp) / "ca.crt").write_text(crt.read_text(), encoding="utf-8")
            chk = doctor.check_certs(Path(tmp))
        self.assertEqual(chk.status, doctor.STATUS_FAIL)
        self.assertIn("does not cover", chk.detail)
        self.assertNotIn(TARGET, chk.detail.split("[", 1)[1].split("]", 1)[0])  # target absent from SAN list


# --------------------------------------------------------------------------- #
# 5. keytool argv builder + alias probing
# --------------------------------------------------------------------------- #
class KeytoolArgvTests(unittest.TestCase):
    def test_list_argv_shape_and_custom_storepass(self):
        argv = doctor._keytool_list_argv("keytool", "/opt/jdk/lib/security/cacerts",
                                         doctor.JAVA_CERT_ALIAS, doctor.JAVA_STOREPASS)
        self.assertEqual(argv, [
            "keytool", "-list", "-keystore", "/opt/jdk/lib/security/cacerts",
            "-storepass", doctor.JAVA_STOREPASS, "-alias", doctor.JAVA_CERT_ALIAS,
        ])

        # The builder emits whatever storepass it is handed, so a caller-level
        # P2K_STOREPASS override simply passes a different value through.
        override = "CUSTOM_STOREPASS_secret"
        argv2 = doctor._keytool_list_argv("keytool", "/cacerts", doctor.JAVA_CERT_ALIAS, override)
        self.assertIn(override, argv2)
        self.assertNotIn(doctor.JAVA_STOREPASS, argv2)


class KeytoolHasAliasTests(unittest.TestCase):
    def _run(self, runner, expected):
        result = doctor._keytool_has_alias(
            "keytool", "/cacerts", doctor.JAVA_STOREPASS, doctor.JAVA_CERT_ALIAS, runner=runner
        )
        self.assertIs(result, expected)

    def test_success_returncode_yields_true(self):
        self._run(lambda argv, **kw: _proc(0), True)

    def test_missing_alias_stderr_yields_false(self):
        self._run(lambda argv, **kw: _proc(1, stderr="alias <p2k-bridge-ca> does not exist"), False)

    def test_other_failure_yields_none(self):
        self._run(lambda argv, **kw: _proc(1, stderr="keystore was tampered with or password incorrect"), None)

    def test_runner_raising_yields_none(self):
        def boom(argv, **kw):
            raise OSError("keytool not executable")
        self._run(boom, None)
        # subprocess.SubprocessError is also caught
        def boom2(argv, **kw):
            raise subprocess.TimeoutExpired(cmd="keytool", timeout=15)
        self._run(boom2, None)


# --------------------------------------------------------------------------- #
# 6. bridge liveness / login shim / tls listener
# --------------------------------------------------------------------------- #
class CheckBridgeHealthTests(unittest.TestCase):
    URL = "http://127.0.0.1:9800/bridge/healthz"

    def test_ok_pass(self):
        getter = _ok_getter({"ok": True, "uptime_seconds": 12, "model": "m"})
        chk = doctor.check_bridge_health(self.URL, getter=getter)
        self.assertEqual(chk.status, doctor.STATUS_PASS)
        self.assertEqual(chk.number, 5)
        self.assertIn("ok=true", chk.detail)
        self.assertIn("model=m", chk.detail)

    def test_refused_and_bad_body_fail(self):
        refused = doctor.check_bridge_health(self.URL, getter=_refused_getter())
        self.assertEqual(refused.status, doctor.STATUS_FAIL)
        self.assertIn("connection refused", refused.detail)

        not_ok = doctor.check_bridge_health(self.URL, getter=_ok_getter({"ok": False}))
        self.assertEqual(not_ok.status, doctor.STATUS_FAIL)
        self.assertIn("unexpected response", not_ok.detail)


class CheckLoginShimTests(unittest.TestCase):
    URL = "http://127.0.0.1:4315/v1/login/web/doctor-probe"

    def test_p2key_present_pass(self):
        chk = doctor.check_login_shim(self.URL, poster=_ok_poster({"p2Key": "abc123"}))
        self.assertEqual(chk.status, doctor.STATUS_PASS)
        self.assertEqual(chk.number, 6)
        self.assertIn("p2Key issued", chk.detail)

    def test_absent_and_refused_fail(self):
        absent = doctor.check_login_shim(self.URL, poster=_ok_poster({}))
        self.assertEqual(absent.status, doctor.STATUS_FAIL)
        self.assertIn("no p2Key", absent.detail)

        refused = doctor.check_login_shim(self.URL, poster=_refused_poster())
        self.assertEqual(refused.status, doctor.STATUS_FAIL)
        self.assertIn("not reachable", refused.detail)


class CheckTLSListenerTests(unittest.TestCase):
    URL = "https://127.0.0.1:443/v1/health"
    ADDR = ("127.0.0.1", 443)

    def _ca(self):
        if not (REPO_CERTS / "p2k_ca.crt").is_file():
            self.skipTest("repo p2k_ca.crt missing")
        return REPO_CERTS / "p2k_ca.crt"

    def test_ok_pass(self):
        getter = _ok_getter({"status": "ok"})
        decode = lambda pem, **kw: {"names": [TARGET, "*.player2.game"]}
        chk = doctor.check_tls(self.URL, self._ca(), self.ADDR,
                               getter=getter, peer=lambda a: "PEM", decode_pem=decode)
        self.assertEqual(chk.status, doctor.STATUS_PASS)
        self.assertEqual(chk.number, 7)
        self.assertIn("chains to", chk.detail)

    def test_refused_and_cert_verify_fail(self):
        refused = doctor.check_tls(self.URL, self._ca(), self.ADDR,
                                   getter=_refused_getter(), peer=lambda a: "PEM", decode_pem=lambda p, **k: {})
        self.assertEqual(refused.status, doctor.STATUS_FAIL)
        self.assertIn("not reachable", refused.detail)

        verify = doctor.check_tls(
            self.URL, self._ca(), self.ADDR,
            getter=lambda u, **k: doctor.HttpResult(None, None, "", "SSLCertVerificationError: untrusted", "tls_verify"),
            peer=lambda a: "PEM", decode_pem=lambda p, **k: {"names": [TARGET]},
        )
        self.assertEqual(verify.status, doctor.STATUS_FAIL)
        self.assertIn("CERTIFICATE_VERIFY_FAILED", verify.detail)

    def test_name_mismatch_fails(self):
        decode = lambda pem, **kw: {"names": ["evil.example"]}
        chk = doctor.check_tls(self.URL, self._ca(), self.ADDR,
                               getter=_ok_getter({"status": "ok"}), peer=lambda a: "PEM", decode_pem=decode)
        self.assertEqual(chk.status, doctor.STATUS_FAIL)
        self.assertIn("does not cover", chk.detail)
        self.assertIn("evil.example", chk.detail)

    def test_ca_missing_and_health_not_ok_fail(self):
        ca_missing = doctor.check_tls(
            self.URL, Path("/nonexistent/p2k_ca.crt"), self.ADDR,
            getter=_ok_getter({"status": "ok"}), peer=lambda a: "PEM", decode_pem=lambda p, **k: {},
        )
        self.assertEqual(ca_missing.status, doctor.STATUS_FAIL)
        self.assertIn("not found", ca_missing.detail)

        # cert covers target, but the https health payload has neither status=ok nor client_version
        decode = lambda pem, **kw: {"names": [TARGET]}
        bad_health = doctor.check_tls(self.URL, self._ca(), self.ADDR,
                                      getter=_ok_getter({"unexpected": 1}), peer=lambda a: "PEM", decode_pem=decode)
        self.assertEqual(bad_health.status, doctor.STATUS_FAIL)
        self.assertIn("https health not ok", bad_health.detail)


# --------------------------------------------------------------------------- #
# 7. jvm trust discovery
# --------------------------------------------------------------------------- #
class CheckJvmTests(unittest.TestCase):
    def _run_keytool_ok(self, argv, **kw):
        return _proc(0)

    def _run_keytool_missing(self, argv, **kw):
        return _proc(1, stderr="alias <p2k-bridge-ca> does not exist")

    def test_launcher_runtime_discovery_passes_and_notes(self):
        with tempfile.TemporaryDirectory() as tmp:
            home, _ = _make_fake_home(tmp)
            results = doctor.check_jvm(
                which_java=lambda n: None, which_keytool=lambda n: "/usr/bin/keytool",
                run_keytool=self._run_keytool_ok, home_dir=home, environ={},
            )
        by_status = {(c.name, c.status) for c in results}
        passes = [c for c in results if c.status == doctor.STATUS_PASS]
        self.assertTrue(any("linux launcher" in c.name for c in passes))
        self.assertEqual(passes[0].number, 8)
        self.assertIn(doctor.JAVA_CERT_ALIAS, passes[0].detail)
        # the PATH-java candidate is a SKIP, plus a single-home NOTE is emitted
        self.assertIn(("jvm trust: server (PATH java)", doctor.STATUS_SKIP), by_status)
        notes = [c for c in results if c.status == doctor.STATUS_NOTE]
        self.assertEqual(len(notes), 1)
        self.assertIn("DIFFERENT JVMs", notes[0].detail)

    def test_missing_alias_fails_with_exact_trust_fix(self):
        with tempfile.TemporaryDirectory() as tmp:
            home, _ = _make_fake_home(tmp)
            expected_home = home / ".minecraft" / "runtimes" / "java-runtime-alpha"
            results = doctor.check_jvm(
                which_java=lambda n: None, which_keytool=lambda n: "/usr/bin/keytool",
                run_keytool=self._run_keytool_missing, home_dir=home, environ={},
            )
            fails = [c for c in results if c.status == doctor.STATUS_FAIL]
        self.assertEqual(len(fails), 1)
        self.assertEqual(fails[0].fix, f"run: bash tls/trust_jvm.sh {expected_home}")

    def test_keytool_absent_skips(self):
        with tempfile.TemporaryDirectory() as tmp:
            home, _ = _make_fake_home(tmp)
            results = doctor.check_jvm(
                which_java=lambda n: None, which_keytool=lambda n: None,
                run_keytool=self._run_keytool_ok, home_dir=home, environ={},
            )
        skips = [c for c in results if c.status == doctor.STATUS_SKIP]
        self.assertTrue(any("keytool is not on PATH" in c.detail for c in skips))

    def test_path_java_discovery_yields_two_home_note(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            java_bin = home / "jdk" / "bin" / "java"
            java_bin.parent.mkdir(parents=True)
            cacerts = home / "jdk" / "lib" / "security" / "cacerts"
            cacerts.parent.mkdir(parents=True)
            cacerts.write_text("x", encoding="utf-8")
            # add a launcher runtime too -> 2 distinct real homes
            _make_fake_home(tmp)
            results = doctor.check_jvm(
                which_java=lambda n: str(java_bin), which_keytool=lambda n: "/usr/bin/keytool",
                run_keytool=self._run_keytool_ok, realpath=lambda p: str(java_bin),
                home_dir=home, environ={},
            )
        passes = [c for c in results if c.status == doctor.STATUS_PASS]
        labels = {c.name for c in passes}
        self.assertIn("jvm trust: server (PATH java)", labels)
        self.assertIn("jvm trust: linux launcher", labels)
        note = [c for c in results if c.status == doctor.STATUS_NOTE][0]
        self.assertIn("2 distinct JVMs", note.detail)


# --------------------------------------------------------------------------- #
# 8. kilo gateway
# --------------------------------------------------------------------------- #
class CheckGatewayTests(unittest.TestCase):
    URL = "https://api.kilo.ai/api/gateway/models"
    MODEL = "pinned-model-1"

    def test_present_with_key_passes_and_hides_key(self):
        getter = _ok_getter({"data": [{"id": self.MODEL}, {"id": "other"}]})
        chk = doctor.check_gateway(self.URL, self.MODEL, api_key=FAKE_KEY, getter=getter)
        self.assertEqual(chk.status, doctor.STATUS_PASS)
        self.assertIn("with KILO_API_KEY", chk.detail)
        self.assertIn("PRESENT", chk.detail)
        _assert_key_hidden(self, chk.detail, chk.fix)

    def test_missing_model_warns_as_anonymous(self):
        getter = _ok_getter({"data": [{"id": "a"}, {"id": "b"}]})
        chk = doctor.check_gateway(self.URL, "ghost-model", api_key="", getter=getter)
        self.assertEqual(chk.status, doctor.STATUS_WARN)
        self.assertIn("anonymous (free tier)", chk.detail)
        self.assertIn("MISSING", chk.detail)
        self.assertIn("churn", chk.detail)
        self.assertTrue(chk.fix)

    def test_network_error_fails_and_hides_key(self):
        getter = lambda u, **k: doctor.HttpResult(None, None, "", "URLError: no route", "unreachable")
        chk = doctor.check_gateway(self.URL, self.MODEL, api_key=FAKE_KEY, getter=getter)
        self.assertEqual(chk.status, doctor.STATUS_FAIL)
        self.assertIn("cannot reach the Kilo gateway", chk.detail)
        _assert_key_hidden(self, chk.detail, chk.fix)


# --------------------------------------------------------------------------- #
# 12. roster models — must look at the SERVED roster, not cfg.characters
# --------------------------------------------------------------------------- #
class CheckRosterModelsTests(unittest.TestCase):
    URL = "https://api.kilo.ai/api/gateway/models"

    def test_character_models_are_validated(self):
        # Per-character models live in characters.json, which shadows the
        # "characters" array in config.json. Validating cfg.characters would
        # check a roster that is not in use.
        getter = _ok_getter({"data": [{"id": "pinned-model-1"}, {"id": "blaze-model"}]})
        chars = [
            {"id": "kilo", "name": "Kilo", "short_name": "KILO"},
            {"id": "blaze", "name": "Blaze", "short_name": "BLAZE",
             "model": "blaze-model"},
        ]
        chk = doctor.check_roster_models(
            self.URL, doctor._roster_models(chars), getter=getter
        )
        self.assertEqual(chk.status, doctor.STATUS_PASS)
        self.assertIn("all 1 roster model(s)", chk.detail)

    def test_missing_character_model_warns_and_names_it(self):
        getter = _ok_getter({"data": [{"id": "pinned-model-1"}]})
        chars = [{"id": "blaze", "name": "Blaze", "model": "gone/model:free"}]
        chk = doctor.check_roster_models(
            self.URL, doctor._roster_models(chars), getter=getter
        )
        self.assertEqual(chk.status, doctor.STATUS_WARN)
        self.assertIn("gone/model:free", chk.detail)
        self.assertTrue(chk.fix)

    def test_catalog_unavailable_skips_rather_than_fails(self):
        getter = lambda u, **k: doctor.HttpResult(None, None, "", "URLError", "unreachable")
        chk = doctor.check_roster_models(self.URL, ["some-model"], getter=getter)
        self.assertEqual(chk.status, doctor.STATUS_SKIP)

    def test_no_character_overrides_skips(self):
        # The global model is already covered by check #9.
        chars = [{"id": "kilo", "name": "Kilo"}]
        chk = doctor.check_roster_models(self.URL, doctor._roster_models(chars))
        self.assertEqual(chk.status, doctor.STATUS_SKIP)

    def test_the_injected_getter_is_called_not_the_network(self):
        called = []

        def getter(url, timeout=None, headers=None):
            called.append(url)
            return doctor.HttpResult(200, {"data": []}, "{}", None, None)

        doctor.check_roster_models(self.URL, ["some-model"], getter=getter)
        self.assertEqual(called, [self.URL])


# --------------------------------------------------------------------------- #
# 9. end-to-end chat + envelope validation
# --------------------------------------------------------------------------- #
class CheckChatTests(unittest.TestCase):
    URL = "http://127.0.0.1:9800/v1/chat/completions"
    MODEL = "m"

    def test_ok_passes_with_reply_and_latency(self):
        poster = _ok_poster({"choices": [{"message": {"content": _envelope_content()}}]})
        chk = doctor.check_chat(self.URL, self.MODEL, poster=poster)
        self.assertEqual(chk.status, doctor.STATUS_PASS)
        self.assertEqual(chk.number, 10)
        self.assertIn("hello there friend", chk.detail)  # the reply
        self.assertRegex(chk.detail, r"\d+ ms")           # latency reported

    def test_malformed_and_non_200_fail(self):
        malformed = doctor.check_chat(self.URL, self.MODEL,
                                      poster=_ok_poster({"choices": [{"message": {"content": "not json"}}]}))
        self.assertEqual(malformed.status, doctor.STATUS_FAIL)
        self.assertIn("envelope parse error", malformed.detail)

        non200 = doctor.check_chat(self.URL, self.MODEL, poster=_ok_poster({}, status=500))
        self.assertEqual(non200.status, doctor.STATUS_FAIL)
        self.assertIn("status=500", non200.detail)


class ValidateEnvelopeTests(unittest.TestCase):
    def test_variants(self):
        # valid object carrying the NPC contract
        good = {"choices": [{"message": {"content": _envelope_content(message="greetings!")}}]}
        ok, detail = doctor._validate_envelope(good)
        self.assertTrue(ok)
        self.assertEqual(detail, "greetings!")

        # missing keys -> False with the offending content echoed
        bad_keys = {"choices": [{"message": {"content": json.dumps({"reason": "r"})}}]}
        ok2, _ = doctor._validate_envelope(bad_keys)
        self.assertFalse(ok2)

        # content that is not JSON -> False
        ok3, _ = doctor._validate_envelope({"choices": [{"message": {"content": "plain text"}}]})
        self.assertFalse(ok3)

        # body is not an object -> False
        ok4, _ = doctor._validate_envelope([1, 2, 3])
        self.assertFalse(ok4)

        # blank message -> the "<empty message>" sentinel
        blank = {"choices": [{"message": {"content": _envelope_content(message="")}}]}
        ok5, detail5 = doctor._validate_envelope(blank)
        self.assertTrue(ok5)
        self.assertEqual(detail5, "<empty message>")


# --------------------------------------------------------------------------- #
# 10. request quota
# --------------------------------------------------------------------------- #
class CheckQuotaTests(unittest.TestCase):
    URL = "http://127.0.0.1:9800/bridge/stats"

    def test_available_passes(self):
        getter = _ok_getter({"requests": {"available": 42, "limit_per_hour": 180}})
        chk = doctor.check_quota(self.URL, getter=getter)
        self.assertEqual(chk.status, doctor.STATUS_PASS)
        self.assertEqual(chk.number, 11)
        self.assertIn("42/180 requests available this hour", chk.detail)

    def test_warns_when_exhausted_no_block_or_unreachable(self):
        exhausted = doctor.check_quota(
            self.URL, getter=_ok_getter({"requests": {"available": 0, "limit_per_hour": 180}}))
        self.assertEqual(exhausted.status, doctor.STATUS_WARN)
        self.assertIn("bucket exhausted", exhausted.detail)

        no_block = doctor.check_quota(
            self.URL, getter=_ok_getter({"requests": None}))
        self.assertEqual(no_block.status, doctor.STATUS_WARN)
        self.assertIn("no requests block", no_block.detail)

        unreachable = doctor.check_quota(
            self.URL, getter=lambda u, **k: doctor.HttpResult(None, None, "", "err", "refused"))
        self.assertEqual(unreachable.status, doctor.STATUS_WARN)
        self.assertIn("could not read /bridge/stats", unreachable.detail)


# --------------------------------------------------------------------------- #
# 11. aggregation / output formatting
# --------------------------------------------------------------------------- #
class SummarizeTests(unittest.TestCase):
    def test_exit_codes(self):
        all_pass = [doctor.Check(1, "a", doctor.STATUS_PASS, "d")]
        self.assertEqual(doctor.summarize(all_pass)["exit_code"], 0)

        with_fail = [doctor.Check(1, "a", doctor.STATUS_PASS, "d"),
                     doctor.Check(2, "b", doctor.STATUS_FAIL, "d")]
        self.assertEqual(doctor.summarize(with_fail)["exit_code"], 1)

        # WARN/SKIP/NOTE never fail the run
        mixed = [doctor.Check(1, "a", doctor.STATUS_PASS, "d"),
                 doctor.Check(2, "b", doctor.STATUS_WARN, "d"),
                 doctor.Check(3, "c", doctor.STATUS_SKIP, "d"),
                 doctor.Check(4, "d", doctor.STATUS_NOTE, "d")]
        summary = doctor.summarize(mixed)
        self.assertEqual(summary["exit_code"], 0)
        self.assertEqual(summary["total"], 4)
        self.assertEqual(summary["counts"][doctor.STATUS_WARN], 1)

    def test_summary_line_and_json(self):
        results = [doctor.Check(1, "python version", doctor.STATUS_PASS, "d"),
                   doctor.Check(2, "env / api key", doctor.STATUS_FAIL, "d", "fix")]
        line = doctor.format_summary(results)
        self.assertIn("1 PASS", line)
        self.assertIn("1 FAIL", line)
        self.assertIn("(2 checks)", line)
        self.assertIn("-> FAIL (exit 1)", line)

        arr = json.loads(doctor.checks_to_json(results))
        self.assertIsInstance(arr, list)
        self.assertEqual(len(arr), 2)
        self.assertEqual(set(arr[0]), {"n", "check", "status", "detail", "fix"})
        self.assertEqual(arr[1]["status"], doctor.STATUS_FAIL)


# --------------------------------------------------------------------------- #
# 12. main(argv) / CLI flags (run_all + load_config fully injected)
# --------------------------------------------------------------------------- #
class MainCliTests(unittest.TestCase):
    def setUp(self):
        # never touch a real config/.env: both load_config and run_all are stubbed.
        self._mixed = [
            doctor.Check(1, "python version", doctor.STATUS_PASS, "py ok"),
            doctor.Check(2, "env / api key", doctor.STATUS_WARN, "warn here", "the fix"),
            doctor.Check(5, "bridge liveness", doctor.STATUS_FAIL, "bridge down", "start it"),
        ]
        self._all_pass = [doctor.Check(1, "python version", doctor.STATUS_PASS, "py ok")]

    def _run_main(self, argv, results):
        cfg_patch = mock.patch.object(doctor, "load_config", lambda *a, **k: BridgeConfig())
        run_patch = mock.patch.object(doctor, "run_all", lambda *a, **k: results)
        buf = io.StringIO()
        with cfg_patch, run_patch, contextlib.redirect_stdout(buf):
            rc = doctor.main(argv)
        return rc, buf.getvalue()

    def test_parse_args_defaults(self):
        args = doctor.parse_args([])
        self.assertEqual(args.config, str(doctor.DEFAULT_CONFIG))
        self.assertFalse(args.json)
        self.assertFalse(args.quiet)

    def test_all_pass_exit_zero(self):
        rc, out = self._run_main(["--config", "x"], self._all_pass)
        self.assertEqual(rc, 0)
        self.assertIn("[PASS] #1 python version: py ok", out)
        self.assertIn("-> OK (exit 0)", out)
        self.assertIn("Summary:", out)

    def test_with_fail_exit_one(self):
        rc, out = self._run_main(["--config", "x"], self._mixed)
        self.assertEqual(rc, 1)
        self.assertIn("[FAIL] #5 bridge liveness", out)
        self.assertIn("fix: start it", out)
        self.assertIn("-> FAIL (exit 1)", out)

    def test_json_flag_emits_matching_array(self):
        rc, out = self._run_main(["--json", "--config", "x"], self._mixed)
        self.assertEqual(rc, 1)
        arr = json.loads(out)  # must be valid JSON with nothing else printed
        self.assertEqual(len(arr), 3)
        self.assertEqual({c["n"] for c in arr}, {1, 2, 5})
        self.assertNotIn("Summary:", out)  # json mode prints only the array

    def test_quiet_prints_only_failures_plus_summary(self):
        rc, out = self._run_main(["--quiet", "--config", "x"], self._mixed)
        self.assertEqual(rc, 1)
        self.assertIn("[FAIL] #5 bridge liveness", out)
        self.assertNotIn("[PASS]", out)
        self.assertNotIn("[WARN]", out)
        self.assertIn("Summary:", out)


if __name__ == "__main__":
    unittest.main()
