"""Pre-flight diagnostic for a p2k-bridge deployment.

Run it from the repository root to verify the whole chain on your own machine:

    python3 bridge/doctor.py            # one line per check + summary
    python3 bridge/doctor.py --json      # the same checks as a JSON array
    python3 bridge/doctor.py --quiet     # only FAIL lines + summary

The bridge never prints the KILO_API_KEY value: key checks report a boolean and
its length only. Exit status is 0 when there is no FAIL (PASS/WARN/SKIP/NOTE)
and 1 when any check FAILs, so it drops straight into CI or a boot script.

Each check is a small function returning a ``Check`` (number, name, status,
detail, fix). The network/environment-facing pieces (urllib calls, DNS, keytool,
openssl, the .env path) are injectable so the whole tool can be tested without a
live bridge or the public Kilo gateway.
"""

import argparse
import json
import os
import re
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NamedTuple, Optional
from urllib.parse import urlsplit

# sys.path shim, exactly like bridge/run.py and the tests use.
BRIDGE_DIR = Path(__file__).resolve().parent
REPO_ROOT = BRIDGE_DIR.parent
if str(BRIDGE_DIR) not in sys.path:
    sys.path.insert(0, str(BRIDGE_DIR))

from p2k_bridge.config import _parse_env_file, load_config  # noqa: E402

TARGET_HOSTNAME = "api.player2.game"
LOOPBACK_IP = "127.0.0.1"
JAVA_CERT_ALIAS = "p2k-bridge-ca"
JAVA_STOREPASS = "changeit"
ENV_NAME = "KILO_API_KEY"
ENV_PLACEHOLDER = "paste-your-kilocode-api-key-here"
USER_AGENT = "p2k-bridge-doctor/1.0"

HTTP_TIMEOUT = 5.0
GATEWAY_TIMEOUT = 15.0
CHAT_TIMEOUT = 30.0
KEYTOOL_TIMEOUT = 15

DEFAULT_CONFIG = BRIDGE_DIR / "config.json"

STATUS_PASS = "PASS"
STATUS_WARN = "WARN"
STATUS_FAIL = "FAIL"
STATUS_SKIP = "SKIP"
STATUS_NOTE = "NOTE"
_STATUS_ORDER = (STATUS_PASS, STATUS_WARN, STATUS_FAIL, STATUS_SKIP, STATUS_NOTE)

_CN_RE = re.compile(r"CN\s*=\s*([^,/]+)")


class Check(NamedTuple):
    number: int
    name: str
    status: str
    detail: str
    fix: str = ""


class HttpResult(NamedTuple):
    """Normalized result of one HTTP attempt; never raises to the caller.

    ``kind`` is one of None (got a status), 'refused', 'timeout', 'unreachable',
    'tls_verify' or 'http' (a non-2xx that still reached the server).
    """

    status: Optional[int]
    data: Any
    text: str
    error: Optional[str]
    kind: Optional[str]


# --------------------------------------------------------------------------
# low-level, dependency-injectable helpers
# --------------------------------------------------------------------------
def _maybe_json(text: str) -> Any:
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return None


def _classify_urlerror(reason: Any) -> str:
    if isinstance(reason, ConnectionRefusedError):
        return "refused"
    if isinstance(reason, (TimeoutError, socket.timeout)):
        return "timeout"
    text = str(reason).lower()
    if "timed out" in text or "timeout" in text:
        return "timeout"
    if "refused" in text:
        return "refused"
    return "unreachable"


def _classify_oserror(exc: BaseException) -> str:
    if isinstance(exc, ConnectionRefusedError):
        return "refused"
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return "timeout"
    return "other"


def _do_http(
    method: str,
    url: str,
    body: Any,
    timeout: float,
    headers: Optional[dict],
    context: Optional[ssl.SSLContext],
) -> HttpResult:
    data = None
    hdrs = dict(headers or {})
    if body is not None:
        data = body if isinstance(body, (bytes, bytearray)) else json.dumps(body).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")
    request = urllib.request.Request(url, data=data, method=method, headers=hdrs)
    kwargs: dict[str, Any] = {"timeout": timeout}
    if context is not None:
        kwargs["context"] = context
    try:
        with urllib.request.urlopen(request, **kwargs) as response:
            raw = response.read().decode("utf-8", "replace")
            return HttpResult(response.status, _maybe_json(raw), raw, None, None)
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read().decode("utf-8", "replace")
        except Exception:
            raw = ""
        return HttpResult(exc.code, _maybe_json(raw), raw, None, "http")
    except ssl.SSLCertVerificationError as exc:
        return HttpResult(None, None, "", f"{type(exc).__name__}: {exc}", "tls_verify")
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        return HttpResult(None, None, "", f"{type(reason).__name__}: {reason}", _classify_urlerror(reason))
    except (ConnectionError, TimeoutError, OSError) as exc:
        return HttpResult(None, None, "", f"{type(exc).__name__}: {exc}", _classify_oserror(exc))


def http_get(url: str, *, timeout: float, headers: Optional[dict] = None,
             context: Optional[ssl.SSLContext] = None) -> HttpResult:
    return _do_http("GET", url, None, timeout, headers, context)


def http_post(url: str, *, body: Any, timeout: float, headers: Optional[dict] = None,
              context: Optional[ssl.SSLContext] = None) -> HttpResult:
    return _do_http("POST", url, body, timeout, headers, context)


def fetch_peer_cert(addr, *, timeout: float = 10) -> str:
    """Return the PEM text of the certificate served at ``addr``."""
    return ssl.get_server_certificate(addr)


def _parse_cert_time(text: Any) -> Optional[datetime]:
    text = (text or "").strip()
    if not text:
        return None
    for fmt in ("%b %d %H:%M:%S %Y %Z", "%b %d %H:%M:%S %Y"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _cn_from_subject(subject: str) -> str:
    match = _CN_RE.search(subject or "")
    return match.group(1).strip() if match else ""


def _parse_openssl_x509(text: str) -> dict:
    """Parse the output of ``openssl x509 -noout -enddate -subject -ext SAN``."""
    not_before: Optional[datetime] = None
    not_after: Optional[datetime] = None
    subject_cn = ""
    names: list[str] = []
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.startswith("notAfter="):
            not_after = _parse_cert_time(line.split("=", 1)[1].strip())
        elif line.startswith("notBefore="):
            not_before = _parse_cert_time(line.split("=", 1)[1].strip())
        elif line.startswith("subject="):
            subject_cn = _cn_from_subject(line.split("=", 1)[1].strip())
        elif "Subject Alternative Name" in line:
            rest = line.split(":", 1)[1].strip() if ":" in line else ""
            if rest.lower() in ("", "critical") and index + 1 < len(lines):
                rest = lines[index + 1].strip()
                index += 1
            for token in rest.split(","):
                token = token.strip()
                if ":" in token:
                    names.append(token.split(":", 1)[1].strip())
        index += 1
    if subject_cn and subject_cn not in names:
        names.append(subject_cn)
    return {
        "not_before": not_before,
        "not_after": not_after,
        "subject_cn": subject_cn,
        "names": sorted(set(names)),
    }


def _run_openssl_x509(path: str) -> Optional[dict]:
    try:
        proc = subprocess.run(
            ["openssl", "x509", "-in", path, "-noout", "-enddate", "-subject", "-ext", "subjectAltName"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return _parse_openssl_x509(proc.stdout)


def _decode_via_ssl(path: str) -> Optional[dict]:
    decode = getattr(getattr(ssl, "_ssl", object()), "_test_decode_cert", None)
    if decode is None:
        return None
    try:
        decoded = decode(path)
    except Exception:
        return None
    subject_cn = ""
    for rdn in decoded.get("subject", ()) or ():
        for key, value in rdn:
            if key == "commonName":
                subject_cn = value
    names = [value for _kind, value in decoded.get("subjectAltName", ()) or ()]
    if subject_cn and subject_cn not in names:
        names.append(subject_cn)
    return {
        "not_before": _parse_cert_time(decoded.get("notBefore")),
        "not_after": _parse_cert_time(decoded.get("notAfter")),
        "subject_cn": subject_cn,
        "names": sorted(set(names)),
    }


def decode_cert_file(path: Any, *, prefer: str = "ssl") -> Optional[dict]:
    """Decode a cert file into {not_before, not_after, subject_cn, names}.

    ``prefer`` picks the first parser: ``\"openssl\"`` shells out to openssl
    (used for the on-disk certs), ``\"ssl\"`` tries the stdlib
    ``ssl._ssl._test_decode_cert`` first and falls back to openssl (used for the
    certificate served on the TLS port). Returns None when nothing can parse it.
    """
    try:
        path = str(path)
    except Exception:
        return None
    if prefer == "openssl" and shutil.which("openssl"):
        shaped = _run_openssl_x509(path)
        if shaped is not None:
            return shaped
    shaped = _decode_via_ssl(path)
    if shaped is not None:
        return shaped
    if prefer != "openssl" and shutil.which("openssl"):
        return _run_openssl_x509(path)
    return None


def decode_cert_pem(pem: Any, *, prefer: str = "ssl") -> Optional[dict]:
    """Decode a certificate given as PEM text (writes a temp file first)."""
    if not isinstance(pem, str):
        return None
    fd, tmp = tempfile.mkstemp(suffix=".pem")
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(pem)
        return decode_cert_file(tmp, prefer=prefer)
    except OSError:
        return None
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _dotenv_lookup(path: Path, name: str) -> str:
    try:
        values = _parse_env_file(Path(path))
    except Exception:
        return ""
    return (values.get(name) or "").strip()


def _is_placeholder(value: str) -> bool:
    text = (value or "").strip()
    if not text:
        return True
    if text == ENV_PLACEHOLDER:
        return True
    lowered = text.lower()
    return lowered.startswith("paste-your") or "your-kilocode" in lowered


def _keytool_list_argv(keytool: str, cacerts: Any, alias: str, storepass: str) -> list[str]:
    return [keytool, "-list", "-keystore", str(cacerts), "-storepass", storepass, "-alias", alias]


def _keytool_has_alias(keytool: str, cacerts: Any, storepass: str, alias: str, *, runner) -> Optional[bool]:
    argv = _keytool_list_argv(keytool, cacerts, alias, storepass)
    try:
        proc = runner(argv, capture_output=True, text=True, timeout=KEYTOOL_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return None
    if getattr(proc, "returncode", 1) == 0:
        return True
    stderr = (getattr(proc, "stderr", "") or "").lower()
    if "does not exist" in stderr or "alias" in stderr:
        return False
    return None


def _validate_envelope(data: Any) -> tuple[bool, str]:
    """Check an OpenAI-style chat.completion body decodes to the NPC contract."""
    if not isinstance(data, dict):
        return False, f"body is {type(data).__name__}, not an object"
    try:
        content = data["choices"][0]["message"]["content"]
        parsed = json.loads(content)
    except Exception as exc:
        return False, f"envelope parse error: {exc!r}"
    if not (
        isinstance(parsed, dict)
        and set(parsed) >= {"reason", "command", "message"}
        and all(isinstance(parsed.get(key), str) for key in ("reason", "command", "message"))
    ):
        return False, f"content missing reason/command/message: {content[:160]}"
    message = " ".join(str(parsed.get("message", "")).split())
    return True, (message[:200] or "<empty message>")


def summarize(results: list[Check]) -> dict:
    counts = {status: 0 for status in _STATUS_ORDER}
    for result in results:
        counts[result.status] = counts.get(result.status, 0) + 1
    return {
        "counts": counts,
        "total": len(results),
        "exit_code": 1 if counts[STATUS_FAIL] else 0,
    }


# --------------------------------------------------------------------------
# individual checks (each: injectable deps, returns a Check or list[Check])
# --------------------------------------------------------------------------
def check_python_version(version_info: Any = None) -> Check:
    info = sys.version_info if version_info is None else version_info
    text = ".".join(str(part) for part in tuple(info)[:3])
    if (tuple(info)[0], tuple(info)[1]) >= (3, 10):
        return Check(1, "python version", STATUS_PASS, f"Python {text} (>= 3.10 required)")
    return Check(1, "python version", STATUS_FAIL, f"Python {text} is older than 3.10",
                 "install Python 3.10 or newer and re-run the doctor")


def check_env(env_path: Any, *, environ: Optional[dict] = None) -> Check:
    environ = os.environ if environ is None else environ
    env_path = Path(env_path)
    file_exists = env_path.is_file()
    file_value = _dotenv_lookup(env_path, ENV_NAME) if file_exists else ""
    env_value = (environ.get(ENV_NAME) or "").strip()
    if env_value:
        effective, source = env_value, f"{ENV_NAME} environment variable"
    elif file_value:
        effective, source = file_value, str(env_path.name)
    else:
        effective, source = "", ""

    if effective and not _is_placeholder(effective):
        return Check(2, "env / api key", STATUS_PASS,
                     f"KILO_API_KEY configured via {source} (length {len(effective)}; value never printed)")
    if effective:  # non-empty but still a placeholder value
        where = (env_path.name if (file_value and not env_value) else f"{ENV_NAME} in the environment")
        detail = f"{where} is still the placeholder (free tier only)"
    elif file_exists:
        detail = f"{env_path.name} exists but KILO_API_KEY is not set"
    else:
        detail = f"no {env_path} file and {ENV_NAME} not in the environment"
    fix = ("the free tier works without a key, but a real key means higher rate limits and fewer "
           f"throttling surprises. Fix: cp .env.example .env then set {ENV_NAME}, or: export {ENV_NAME}=...")
    return Check(2, "env / api key", STATUS_WARN, detail, fix)


def check_certs(certs_dir: Any, *, now: Optional[datetime] = None) -> Check:
    now = now or datetime.now(timezone.utc)
    certs_dir = Path(certs_dir)
    ca = certs_dir / "ca.crt"
    server = certs_dir / "server.crt"
    missing = [name for path, name in ((ca, "ca.crt"), (server, "server.crt")) if not path.is_file()]
    if missing:
        return Check(3, "tls certs", STATUS_FAIL,
                     f"missing cert file(s): {', '.join(missing)} in {certs_dir}",
                     "generate them: bash tls/gen_certs.sh (or restore everything: bash bootstrap.sh)")
    decoded = decode_cert_file(server, prefer="openssl")
    if decoded is None:
        return Check(3, "tls certs", STATUS_WARN,
                     f"{ca.name} and {server.name} exist but could not be parsed",
                     "install openssl to enable cert date + SAN validation")
    not_after, not_before = decoded["not_after"], decoded["not_before"]
    if not_after is not None and now > not_after:
        return Check(3, "tls certs", STATUS_FAIL,
                     f"server.crt EXPIRED (notAfter {not_after:%Y-%m-%d %H:%M UTC})",
                     "regenerate: bash tls/gen_certs.sh")
    if not_before is not None and now < not_before:
        return Check(3, "tls certs", STATUS_FAIL,
                     f"server.crt not yet valid (notBefore {not_before:%Y-%m-%d %H:%M UTC})",
                     "check the system clock; regenerate with bash tls/gen_certs.sh if it persists")
    names = decoded.get("names", [])
    if TARGET_HOSTNAME not in names:
        return Check(3, "tls certs", STATUS_FAIL,
                     f"server.crt SAN [{', '.join(names)}] does not cover {TARGET_HOSTNAME}",
                     "regenerate the leaf cert for api.player2.game: bash tls/gen_certs.sh")
    validity = "present"
    if not_after is not None:
        validity = f"valid, expires in {(not_after - now).days}d ({not_after:%Y-%m-%d})"
    return Check(3, "tls certs", STATUS_PASS,
                 f"{ca.name} + {server.name} present; server.crt {validity}; "
                 f"SAN covers {TARGET_HOSTNAME} [{', '.join(names)}]")


def _hosts_verdict(resolved: Optional[str], expected: str = LOOPBACK_IP) -> tuple[str, str, str]:
    if resolved is None:
        return (STATUS_WARN, f"{TARGET_HOSTNAME} does not resolve",
                f"add this line to /etc/hosts: {expected} {TARGET_HOSTNAME}")
    if resolved == expected:
        return (STATUS_PASS, f"{TARGET_HOSTNAME} -> {expected}", "")
    return (STATUS_FAIL, f"{TARGET_HOSTNAME} -> {resolved} (expected {expected})",
            f"add this line to /etc/hosts: {expected} {TARGET_HOSTNAME}")


def check_hosts(hostname: str = TARGET_HOSTNAME, *, resolver=None, expected: str = LOOPBACK_IP) -> Check:
    resolver = resolver or socket.gethostbyname
    try:
        resolved = resolver(hostname)
        reason = ""
    except Exception as exc:
        resolved = None
        reason = f"{type(exc).__name__}: {exc}"
    status, detail, fix = _hosts_verdict(resolved, expected)
    if reason:
        detail = f"{detail} ({reason})"
    return Check(4, "hosts", status, detail, fix)


def check_bridge_health(url: str, *, getter=None, timeout: float = HTTP_TIMEOUT) -> Check:
    getter = getter if getter is not None else http_get
    result = getter(url, timeout=timeout)
    if result.kind == "refused":
        return Check(5, "bridge liveness", STATUS_FAIL,
                     f"bridge not reachable on {url} (connection refused)",
                     "is the bridge running? start it with: python3 bridge/run.py")
    if result.status != 200 or not isinstance(result.data, dict) or result.data.get("ok") is not True:
        return Check(5, "bridge liveness", STATUS_FAIL,
                     f"unexpected response (status={result.status} kind={result.kind} err={result.error}) "
                     f"body={result.text[:120]}",
                     "is the bridge running? start it with: python3 bridge/run.py")
    uptime = result.data.get("uptime_seconds")
    model = result.data.get("model")
    return Check(5, "bridge liveness", STATUS_PASS,
                 f"bridge up on {urlsplit(url).netloc} — ok=true, model={model}, uptime={uptime}s")


def check_login_shim(url: str, *, poster=None, timeout: float = HTTP_TIMEOUT) -> Check:
    poster = poster if poster is not None else http_post
    result = poster(url, body={}, timeout=timeout)
    if result.kind == "refused":
        return Check(6, "login shim", STATUS_FAIL,
                     f"login shim not reachable on {url} (connection refused)",
                     "is the bridge running? start it with: python3 bridge/run.py")
    if result.status == 200 and isinstance(result.data, dict) and result.data.get("p2Key"):
        return Check(6, "login shim", STATUS_PASS,
                     f"POST {url} -> p2Key issued; plain-HTTP login interception works")
    return Check(6, "login shim", STATUS_FAIL,
                 f"no p2Key (status={result.status} kind={result.kind} err={result.error}) body={result.text[:120]}",
                 "is the login shim running? start the bridge: python3 bridge/run.py")


def check_tls(url: str, ca_file: Any, peer_addr, *, getter=None, peer=None,
              decode_pem=None, timeout: float = HTTP_TIMEOUT) -> Check:
    getter = getter if getter is not None else http_get
    peer = peer if peer is not None else fetch_peer_cert
    decode_pem = decode_pem if decode_pem is not None else decode_cert_pem
    ca_file = Path(ca_file)
    if not ca_file.is_file():
        return Check(7, "tls listener", STATUS_FAIL,
                     f"CA file {ca_file} not found; cannot verify the TLS listener",
                     "generate certs: bash tls/gen_certs.sh (or bash bootstrap.sh)")
    context = ssl.create_default_context(cafile=str(ca_file))
    context.check_hostname = False  # connect by IP; the served name is checked separately
    result = getter(url, timeout=timeout, context=context)
    if result.kind == "refused":
        return Check(7, "tls listener", STATUS_FAIL,
                     f"TLS listener not reachable on {url} (connection refused)",
                     "is the bridge running? start it with: python3 bridge/run.py")
    if result.kind == "tls_verify":
        return Check(7, "tls listener", STATUS_FAIL,
                     f"CERTIFICATE_VERIFY_FAILED: the served cert does not chain to {ca_file.name} ({result.error})",
                     "the on-disk CA is out of sync with the served cert — run bash bootstrap.sh, then "
                     "re-trust: bash tls/trust_jvm.sh <java-home>")
    peer_error = ""
    try:
        pem = peer(peer_addr)
        decoded = decode_pem(pem)
    except Exception as exc:
        decoded = None
        peer_error = repr(exc)
    names = set((decoded or {}).get("names", []) or [])
    if TARGET_HOSTNAME not in names:
        shown = ", ".join(sorted(names)) if names else (peer_error or "unreadable")
        return Check(7, "tls listener", STATUS_FAIL,
                     f"served cert does not cover {TARGET_HOSTNAME} (SAN=[{shown}])",
                     "regenerate the leaf cert: bash tls/gen_certs.sh")
    data = result.data if isinstance(result.data, dict) else {}
    if not (result.status == 200 and (data.get("status") == "ok" or data.get("client_version"))):
        return Check(7, "tls listener", STATUS_FAIL,
                     f"https health not ok (status={result.status} kind={result.kind} err={result.error}) "
                     f"body={result.text[:120]}",
                     "is the bridge running? start it with: python3 bridge/run.py")
    return Check(7, "tls listener", STATUS_PASS,
                 f"{url} ok (status 200); served cert covers {TARGET_HOSTNAME} and chains to {ca_file.name}")


def _discover_java_homes(which_java, realpath, home_dir: Path, environ: dict) -> list[tuple[str, Optional[Path], str]]:
    """Return (label, home-or-None, skip-reason) tuples for every java candidate."""
    found: list[tuple[str, Optional[Path], str]] = []
    seen: set[str] = set()

    def add(label: str, path: Path) -> None:
        key = str(path)
        if key in seen:
            return
        seen.add(key)
        found.append((label, Path(path), ""))

    java = which_java("java") if which_java else None
    if java:
        try:
            home = Path(realpath(str(java))).resolve().parent.parent
            add("server (PATH java)", home)
        except OSError:
            found.append(("server (PATH java)", None, "could not resolve PATH 'java' to a home"))
    else:
        found.append(("server (PATH java)", None, "no 'java' on PATH (dedicated-server JVM not discoverable)"))

    launcher_specs = [
        ("linux launcher", Path(home_dir) / ".minecraft" / "runtimes"),
        ("macOS launcher", Path(home_dir) / "Library" / "Application Support" / "minecraft" / "runtimes"),
    ]
    appdata = environ.get("APPDATA")
    if appdata:
        launcher_specs.append(("windows launcher", Path(appdata) / ".minecraft" / "runtimes"))
    for label, runtimes in launcher_specs:
        if not runtimes.is_dir():
            continue
        matches = sorted(entry for entry in runtimes.glob("java-runtime-*") if entry.is_dir())
        for match in matches:
            add(label, match)
        if not matches:
            found.append((label, None, f"{runtimes} exists but contains no java-runtime-* directories"))
    return found


def check_jvm(*, which_java=None, which_keytool=None, run_keytool=None, realpath=None,
              home_dir=None, environ=None) -> list[Check]:
    which_java = which_java or shutil.which
    which_keytool = which_keytool or shutil.which
    run_keytool = run_keytool or subprocess.run
    realpath = realpath or os.path.realpath
    home_dir = Path(home_dir) if home_dir is not None else Path.home()
    environ = os.environ if environ is None else environ

    keytool = which_keytool("keytool")
    candidates = _discover_java_homes(which_java, realpath, home_dir, environ)
    results: list[Check] = []
    for label, home, skip_reason in candidates:
        if home is None:
            results.append(Check(8, f"jvm trust: {label}", STATUS_SKIP, skip_reason, ""))
            continue
        cacerts = home / "lib" / "security" / "cacerts"
        if keytool is None:
            results.append(Check(8, f"jvm trust: {label}", STATUS_SKIP,
                                 f"{home} found, but keytool is not on PATH",
                                 "install a JDK so the CA can be trusted/verified (e.g. apt install openjdk-21-jdk)"))
            continue
        if not cacerts.is_file():
            results.append(Check(8, f"jvm trust: {label}", STATUS_SKIP,
                                 f"no cacerts at {cacerts} (JRE-only or jlink'd runtime)", ""))
            continue
        present = _keytool_has_alias(keytool, cacerts, JAVA_STOREPASS, JAVA_CERT_ALIAS, runner=run_keytool)
        if present is None:
            results.append(Check(8, f"jvm trust: {label}", STATUS_SKIP,
                                 f"keytool could not read {cacerts}",
                                 "if the storepass is not 'changeit', set P2K_STOREPASS"))
        elif present:
            results.append(Check(8, f"jvm trust: {label}", STATUS_PASS,
                                 f"{cacerts} trusts alias {JAVA_CERT_ALIAS}", ""))
        else:
            results.append(Check(8, f"jvm trust: {label}", STATUS_FAIL,
                                 f"{cacerts} is MISSING {JAVA_CERT_ALIAS} — TLS handshakes will fail in this JVM",
                                 f"run: bash tls/trust_jvm.sh {home}"))

    real_homes = sum(1 for (_label, home, _reason) in candidates if home is not None)
    if real_homes:
        labels = ", ".join(label for label, home, _r in candidates if home is not None)
        if real_homes >= 2:
            note = (f"found {real_homes} distinct JVMs ({labels}); the server JVM (PATH java) and each "
                    "Minecraft launcher runtime are usually DIFFERENT JVMs — trust the CA in every one "
                    "that makes a TLS call")
        else:
            note = ("the server JVM (PATH java) and any Minecraft launcher runtime are usually DIFFERENT JVMs; "
                    "trust the CA in every JVM that makes a TLS call (the server AND each client machine)")
        results.append(Check(8, "jvm trust: note", STATUS_NOTE, note, ""))
    return results


def check_gateway(url: str, model: str, *, api_key: str = "", getter=None,
                  timeout: float = GATEWAY_TIMEOUT) -> Check:
    getter = getter if getter is not None else http_get
    headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"  # sent, never printed/logged
    result = getter(url, timeout=timeout, headers=headers)
    if result.status is None:
        return Check(9, "kilo gateway", STATUS_FAIL,
                     f"cannot reach the Kilo gateway ({result.kind or 'error'}): {result.error}",
                     "check internet egress; on a fresh host a TLS-inspecting proxy may need its root CA "
                     "trusted (see SETUP.md)")
    if result.status != 200 or not isinstance(result.data, dict):
        return Check(9, "kilo gateway", STATUS_FAIL,
                     f"gateway returned HTTP {result.status}: {result.text[:160]}",
                     "verify KILO_API_KEY and network egress")
    raw = result.data.get("data", [])
    ids = [entry.get("id") for entry in raw
           if isinstance(entry, dict) and isinstance(entry.get("id"), str)] if isinstance(raw, list) else []
    auth = "with KILO_API_KEY" if api_key else "anonymous (free tier)"
    if model in ids:
        return Check(9, "kilo gateway", STATUS_PASS,
                     f"gateway reachable {auth}; {len(ids)} models listed; pinned model '{model}' is PRESENT")
    return Check(9, "kilo gateway", STATUS_WARN,
                 f"gateway reachable {auth}; {len(ids)} models listed but pinned model '{model}' is MISSING "
                 "(catalog churn)",
                 f"pin a listed model: set P2K_MODEL in .env and \"model\" in bridge/config.json, then: "
                 "python3 bridge/run.py --check")


def check_roster_models(
    url: str,
    model_ids: list[str],
    *,
    api_key: str = "",
    getter=None,
    timeout: float = GATEWAY_TIMEOUT,
) -> Check:
    """Every model named by a character must exist in the gateway catalog.

    A character pinned to a model that has left the catalog does not fail, but
    that NPC silently answers with the global model instead of the one it was
    configured for — which is exactly the kind of drift a pre-flight should
    surface before someone tries to debug it in-game.
    """
    if not model_ids:
        return Check(12, "roster models", STATUS_SKIP,
                     "no character overrides its model", "")
    getter = getter if getter is not None else http_get
    headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    result = getter(url, timeout=timeout, headers=headers)
    if result.status != 200 or not isinstance(result.data, dict):
        return Check(12, "roster models", STATUS_SKIP,
                     f"catalog unavailable (status={result.status} kind={result.kind}); "
                     "roster models not verified", "")
    raw = result.data.get("data", [])
    ids = {entry.get("id") for entry in raw
           if isinstance(entry, dict) and isinstance(entry.get("id"), str)} if isinstance(raw, list) else set()
    missing = [model_id for model_id in model_ids if model_id not in ids]
    if not missing:
        return Check(12, "roster models", STATUS_PASS,
                     f"all {len(model_ids)} roster model(s) present in the catalog")
    return Check(12, "roster models", STATUS_WARN,
                 f"roster model(s) MISSING from the catalog: " + ", ".join(missing),
                 "those characters fall back to the global model; update bridge/config.json "
                 "or run: python3 bridge/run.py --check")


def check_chat(url: str, model: str, *, poster=None, timeout: float = CHAT_TIMEOUT) -> Check:
    poster = poster if poster is not None else http_post
    payload = {
        "model": model,
        "messages": [
            {"role": "system",
             "content": "You are a terse Minecraft companion NPC. Always answer with a JSON object "
                        "containing keys reason, command and message."},
            {"role": "user",
             "content": json.dumps({"userMessage": "p2k-bridge doctor preflight: greet me in five words or fewer.",
                                    "worldStatus": "time=day, biome=plains",
                                    "agentStatus": "pos=0,64,0 health=20"})},
        ],
    }
    started = time.monotonic()
    result = poster(url, body=payload, timeout=timeout)
    latency_ms = int((time.monotonic() - started) * 1000)
    if result.status != 200 or not isinstance(result.data, dict):
        return Check(10, "end-to-end chat", STATUS_FAIL,
                     f"bridge chat POST status={result.status} kind={result.kind} err={result.error} "
                     f"body={result.text[:160]}",
                     "check the bridge stdout logs; the bridge should always answer 200 (degraded idle "
                     "envelope on upstream error)")
    ok, detail = _validate_envelope(result.data)
    if not ok:
        return Check(10, "end-to-end chat", STATUS_FAIL, detail,
                     "the mod expects choices[0].message.content to be JSON with reason/command/message")
    return Check(10, "end-to-end chat", STATUS_PASS,
                 f"one real LLM call through the local bridge succeeded in {latency_ms} ms — reply: {detail}")


def check_quota(url: str, *, getter=None, timeout: float = HTTP_TIMEOUT) -> Check:
    getter = getter if getter is not None else http_get
    result = getter(url, timeout=timeout)
    if result.status != 200 or not isinstance(result.data, dict):
        return Check(11, "request quota", STATUS_WARN,
                     f"could not read /bridge/stats (status={result.status} kind={result.kind})", "")
    block = result.data.get("requests")
    if not isinstance(block, dict) or "available" not in block:
        return Check(11, "request quota", STATUS_WARN, "/bridge/stats has no requests block", "")
    try:
        available = int(block.get("available"))
    except (TypeError, ValueError):
        available = -1
    limit = block.get("limit_per_hour")
    if available > 0:
        return Check(11, "request quota", STATUS_PASS, f"{available}/{limit} requests available this hour")
    return Check(11, "request quota", STATUS_WARN,
                 f"{available}/{limit} requests available (bucket exhausted)",
                 "refills 1 token per 20s at the default 180/hr cap; wait, or raise requests_per_hour "
                 "in bridge/config.json")


# --------------------------------------------------------------------------
# orchestration / CLI
# --------------------------------------------------------------------------
def run_all(cfg, *, environ: Optional[dict] = None, now: Optional[datetime] = None) -> list[Check]:
    environ = os.environ if environ is None else environ
    now = now or datetime.now(timezone.utc)
    certs_dir = REPO_ROOT / "tls" / "certs"
    http_base = f"http://{LOOPBACK_IP}:{cfg.http_port}"
    tls_url = f"https://{LOOPBACK_IP}:{cfg.tls_port}/v1/health"
    shim_url = f"http://{LOOPBACK_IP}:{cfg.local_login_port}/v1/login/web/doctor-probe"
    api_key = (getattr(cfg, "kilo_api_key", "") or "").strip()
    model = getattr(cfg, "model", "")
    gateway_url = (getattr(cfg, "kilo_base_url", "") or "").rstrip("/") + "/models"
    roster_models = _roster_models(cfg)

    results: list[Check] = []
    results.append(check_python_version(sys.version_info))
    results.append(check_env(REPO_ROOT / ".env", environ=environ))
    results.append(check_certs(certs_dir, now=now))
    results.append(check_hosts(TARGET_HOSTNAME))
    results.append(check_bridge_health(f"{http_base}/bridge/healthz"))
    results.append(check_login_shim(shim_url))
    results.append(check_tls(tls_url, certs_dir / "p2k_ca.crt", (LOOPBACK_IP, int(cfg.tls_port))))
    results.extend(check_jvm())
    results.append(check_gateway(gateway_url, model, api_key=api_key))
    results.append(check_roster_models(gateway_url, roster_models, api_key=api_key))
    results.append(check_chat(f"{http_base}/v1/chat/completions", model))
    results.append(check_quota(f"{http_base}/bridge/stats"))
    return results


def _roster_models(cfg) -> list[str]:
    """Distinct models referenced by the roster, the global model first."""
    ordered: list[str] = []
    for model_id in [getattr(cfg, "model", "") or ""] + [
        character.get("model") for character in getattr(cfg, "characters", []) or []
        if isinstance(character, dict)
    ]:
        if isinstance(model_id, str) and model_id.strip() and model_id.strip() not in ordered:
            ordered.append(model_id.strip())
    return ordered


def format_line(check: Check) -> str:
    line = f"[{check.status}] #{check.number} {check.name}: {check.detail}"
    if check.fix and check.status in (STATUS_WARN, STATUS_FAIL, STATUS_SKIP):
        line += f"  (fix: {check.fix})"
    return line


def format_summary(results: list[Check]) -> str:
    summary = summarize(results)
    counts = summary["counts"]
    parts = [f"{counts[status]} {status}" for status in _STATUS_ORDER if counts.get(status)]
    verdict = "FAIL" if summary["exit_code"] else "OK"
    return f"Summary: {', '.join(parts)} ({summary['total']} checks) -> {verdict} (exit {summary['exit_code']})"


def checks_to_json(results: list[Check]) -> str:
    return json.dumps(
        [
            {"n": check.number, "check": check.name, "status": check.status,
             "detail": check.detail, "fix": check.fix}
            for check in results
        ],
        indent=2,
    )


def parse_args(argv: Optional[list] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="p2k-bridge-doctor",
        description="Pre-flight diagnostic for a p2k-bridge deployment.",
    )
    parser.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG),
        help="path to the bridge config.json (default: bridge/config.json)",
    )
    parser.add_argument("--json", action="store_true", help="emit the checks as a JSON array")
    parser.add_argument("--quiet", action="store_true", help="print only FAIL lines plus the summary")
    return parser.parse_args(argv)


def main(argv: Optional[list] = None) -> int:
    args = parse_args(argv)
    cfg = load_config(args.config)
    results = run_all(cfg)
    if args.json:
        print(checks_to_json(results))
    else:
        for check in results:
            if args.quiet and check.status != STATUS_FAIL:
                continue
            print(format_line(check))
        print(format_summary(results))
    return summarize(results)["exit_code"]


if __name__ == "__main__":
    sys.exit(main())
