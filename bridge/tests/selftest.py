#!/usr/bin/env python3
"""Offline/online self test for the p2k-bridge HTTP layer.

Usage (from /workspace/app/bridge):
    python3 tests/selftest.py
    python3 tests/selftest.py --live
"""

import argparse
import http.client as http_client
import json
import logging
import os
import re
import socket
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

BRIDGE_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = BRIDGE_DIR.parent
if str(BRIDGE_DIR) not in sys.path:
    sys.path.insert(0, str(BRIDGE_DIR))

import server  # noqa: E402
from p2k_bridge import state as state_mod  # noqa: E402
from p2k_bridge.config import BridgeConfig  # noqa: E402
from p2k_bridge.kilo import KiloError  # noqa: E402

TARGET_MODEL = "nvidia/nemotron-3-super-120b-a12b:free"

CHAT_PAYLOAD = {
    "model": TARGET_MODEL,
    "messages": [
        {
            "role": "system",
            "content": (
                "You are an AI friend in Minecraft. You take the personality of: Kilo. "
                "Respond with JSON containing message, command and reason.\n"
                "Valid Commands:\n"
                "get: mine and collect items\n"
                "follow: follow the user\n"
                "stop: stop current task"
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "userMessage": "hey can you get me 10 oak logs?",
                    "worldStatus": "time=day, biome=plains",
                    "agentStatus": "pos=100,64,200, health=20, inventory=[torch x4]",
                }
            ),
        },
    ],
}

PORT = 0
CFG = None
SRV = None

BUDGET_PAYLOAD = {
    "model": TARGET_MODEL,
    "messages": [
        {"role": "user", "content": "hey kilo, get me 10 oak logs please"},
    ],
}
BUDGET_TOKENS_PER_WINDOW = 10

SMALL_PAYLOAD = {
    "model": TARGET_MODEL,
    "messages": [
        {"role": "system", "content": "You are a terse Minecraft companion."},
        {"role": "user", "content": "hello"},
    ],
}

TOTAL = 0
FAILED = 0
SKIPPED = 0


def check(name, ok, detail=""):
    global TOTAL, FAILED
    TOTAL += 1
    if ok:
        print(f"PASS {TOTAL:02d} {name}")
    else:
        FAILED += 1
        suffix = f" :: {detail}" if detail else ""
        print(f"FAIL {TOTAL:02d} {name}{suffix}")


def skip(message):
    global SKIPPED
    SKIPPED += 1
    print(f"SKIP    {message}")


def _parse_envelope(data, text):
    """Return (ok, detail) for a JSON content that decodes to a valid envelope."""
    if not isinstance(data, dict):
        return False, f"body={text[:200]}"
    try:
        content = data["choices"][0]["message"]["content"]
        parsed = json.loads(content)
    except Exception as exc:
        return False, f"envelope parse error: {exc!r}; body={text[:200]}"
    if not (
        set(parsed) >= {"reason", "command", "message"}
        and all(isinstance(parsed.get(key), str) for key in ("reason", "command", "message"))
    ):
        return False, f"content={content[:160]}"
    return True, f"content={content[:160]}"


def _token_budget(port=None):
    _status, stats, _text = http("GET", "/bridge/stats", port=port)
    if not isinstance(stats, dict):
        return None
    token_budget = stats.get("token_budget")
    return token_budget if isinstance(token_budget, dict) else None


def _maybe_json(text):
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return None


def http(method, path, body=None, port=None, timeout=60):
    if port is None:
        port = PORT
    url = f"http://127.0.0.1:{port}{path}"
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = body if isinstance(body, (bytes, bytearray)) else json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", "replace")
            return response.status, _maybe_json(raw), raw
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        return exc.code, _maybe_json(raw), raw


def _start(cfg, host="127.0.0.1", tls=False, tls_cert=None, tls_key=None):
    srv = server.build_server(cfg, host, 0, tls=tls, tls_cert=tls_cert, tls_key=tls_key)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.5}, daemon=True).start()
    return srv, port


def _character_names(data):
    if not isinstance(data, dict) or not isinstance(data.get("characters"), list):
        return None
    return [c.get("name") for c in data["characters"] if isinstance(c, dict)]


def _resolve_roster_path():
    """Path of the characters.json the server will resolve (like the reload check)."""
    effective_cfg_path = getattr(CFG, "config_path", None) or os.environ.get("P2K_CONFIG")
    resolver = getattr(server, "resolve_characters_path", None)
    roster_path = None
    if callable(resolver):
        resolved = resolver(effective_cfg_path)
        if resolved is not None:
            roster_path = Path(resolved)
    if roster_path is None:
        roster_path = BRIDGE_DIR / "characters.json"
    return roster_path


def _read_roster_text(roster_path):
    try:
        return roster_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        print(f"note: could not read existing roster {roster_path}: {exc!r}")
        return None


def _restore_roster_file(roster_path, original_text):
    """Put the roster file back exactly as it was found."""
    if original_text is not None:
        try:
            roster_path.write_text(original_text, encoding="utf-8")
        except OSError as exc:
            print(f"note: could not restore roster {roster_path}: {exc!r}")
    else:
        try:
            roster_path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            print(f"note: could not remove temp roster {roster_path}: {exc!r}")


def run_characters_reload_check():
    """Hot reload on the main selftest instance.

    Writes a temp characters.json into the directory the server resolves (via
    server.resolve_characters_path when importable, else bridge/), checks that
    GET /v1/selected_characters serves it, then restores/removes the file and
    checks that the roster reverts (to cfg.characters when there was no roster
    file, or to the original contents when there was one).
    """
    roster_path = _resolve_roster_path()

    original_text = _read_roster_text(roster_path) if roster_path.is_file() else None

    temp_roster = {
        "characters": [
            {
                "name": "Selftest Temp",
                "short_name": "TEMP",
                "greeting": "hot reload probe",
                "description": "temporary roster written by the selftest",
                "voice_ids": [],
                "meta": {"skin_url": ""},
            }
        ]
    }

    try:
        roster_path.write_text(json.dumps(temp_roster, indent=2), encoding="utf-8")
        status, data, text = http("GET", "/v1/selected_characters")
        names = _character_names(data)
        check(
            "characters hot-reload: temp characters.json is served",
            status == 200 and names is not None and "Selftest Temp" in names,
            f"status={status} names={names} path={roster_path}",
        )
    except OSError as exc:
        check(
            "characters hot-reload: temp characters.json is served",
            False,
            f"write/read failed: {exc!r} path={roster_path}",
        )
    finally:
        _restore_roster_file(roster_path, original_text)

    status, data, text = http("GET", "/v1/selected_characters")
    names = _character_names(data)
    if original_text is None and not roster_path.is_file():
        expected = [c.get("name") for c in (CFG.characters or []) if isinstance(c, dict)]
        check(
            "characters hot-reload: temp removed -> fallback to cfg.characters",
            status == 200 and names == expected,
            f"status={status} names={names} expected={expected}",
        )
    else:
        check(
            "characters hot-reload: original roster restored (temp character gone)",
            status == 200 and names is not None and "Selftest Temp" not in names,
            f"status={status} names={names}",
        )


def run_characters_id_backfill_check():
    """A characters.json entry without an id must still be served WITH one.

    Player2NPC aborts the companion spawn when a character has no id, so the
    bridge backfills missing ids at serve time (never rewriting the file).
    Writes a legacy-style roster (no id) into the directory the server
    resolves, checks that GET /v1/selected_characters answers with a non-empty
    id matching ^[A-Za-z0-9_-]+$, confirms the file on disk is untouched, then
    restores the original file exactly as found.
    """
    roster_path = _resolve_roster_path()
    original_text = _read_roster_text(roster_path) if roster_path.is_file() else None

    legacy_roster = {
        "characters": [
            {
                "name": "Selftest Legacy",
                "short_name": "LEGACY",
                "greeting": "hand-edited entry with no id",
                "description": "written without an id to exercise the backfill",
            }
        ]
    }
    written_text = json.dumps(legacy_roster, indent=2)

    try:
        roster_path.write_text(written_text, encoding="utf-8")
        status, data, text = http("GET", "/v1/selected_characters")
        characters = data.get("characters") if isinstance(data, dict) else None
        character = characters[0] if isinstance(characters, list) and characters else None
        served_id = character.get("id") if isinstance(character, dict) else None
        check(
            "characters backfill: entry without id is served with a valid id",
            status == 200
            and isinstance(served_id, str)
            and re.fullmatch(r"[A-Za-z0-9_-]+", served_id) is not None,
            f"status={status} served_id={served_id!r} path={roster_path}",
        )
        disk_text = _read_roster_text(roster_path)
        check(
            "characters backfill: on-disk characters.json is not rewritten",
            disk_text == written_text,
            f"on_disk={str(disk_text)[:120]!r} path={roster_path}",
        )
    except OSError as exc:
        check(
            "characters backfill: entry without id is served with a valid id",
            False,
            f"write/read failed: {exc!r} path={roster_path}",
        )
    finally:
        _restore_roster_file(roster_path, original_text)


def run_core_checks():
    status, data, text = http("GET", "/bridge/healthz")
    check(
        "GET /bridge/healthz",
        status == 200 and isinstance(data, dict) and data.get("ok") is True and data.get("model") == CFG.model,
        f"status={status} body={text[:200]}",
    )

    status, data, text = http("GET", "/bridge/stats")
    check(
        "GET /bridge/stats",
        status == 200 and isinstance(data, dict) and "chat_requests" in data,
        f"status={status} body={text[:200]}",
    )

    status, data, text = http("GET", "/v1/health")
    check(
        "GET /v1/health",
        status == 200 and isinstance(data.get("client_version"), str) and data.get("status") == "ok",
        f"status={status} body={text[:200]}",
    )

    status, data, text = http("GET", "/v1/selected_characters")
    characters = data.get("characters") if isinstance(data, dict) else None
    ok = status == 200 and isinstance(characters, list) and len(characters) > 0
    if ok:
        first = characters[0]
        ok = isinstance(first, dict) and "name" in first and "short_name" in first and "meta" in first
    check("GET /v1/selected_characters", ok, f"status={status} body={text[:200]}")

    status, data, text = http("POST", "/v1/login/device/new", {"client_id": "player2-ai-npc-minecraft"})
    ok = (
        status == 200
        and isinstance(data.get("deviceCode"), str)
        and bool(data.get("deviceCode"))
        and isinstance(data.get("verificationUriComplete"), str)
        and bool(data.get("verificationUriComplete"))
        and data.get("interval") == 5
    )
    check("POST /v1/login/device/new", ok, f"status={status} body={text[:200]}")

    status, data, text = http("POST", "/v1/login/device/new", {"client_id": "player2-ai-npc-minecraft"})
    fresh_code = data.get("deviceCode") if isinstance(data, dict) else None
    status, data, text = http(
        "POST",
        "/v1/login/device/token",
        {"device_code": fresh_code, "client_id": "player2-ai-npc-minecraft"},
    )
    check(
        "POST /v1/login/device/token (fresh code)",
        status == 200 and isinstance(data, dict) and bool(data.get("p2Key")),
        f"status={status} body={text[:200]}",
    )

    status, data, text = http(
        "POST",
        "/v1/login/device/token",
        {"device_code": fresh_code, "client_id": "player2-ai-npc-minecraft"},
    )
    check(
        "POST /v1/login/device/token (replay -> 400)",
        status == 400 and "authorization_pending" in text,
        f"status={status} body={text[:200]}",
    )

    status, data, text = http("POST", "/v1/login/web/player2-ai-npc-minecraft", {})
    check(
        "POST /v1/login/web/<gameKey>",
        status == 200 and isinstance(data, dict) and bool(data.get("p2Key")),
        f"status={status} body={text[:200]}",
    )

    status, data, text = http("POST", "/v1/stt/start", {})
    check("POST /v1/stt/start", status == 200 and isinstance(data, dict), f"status={status} body={text[:200]}")

    status, data, text = http("POST", "/v1/stt/stop", {})
    check("POST /v1/stt/stop", status == 200 and data.get("text") == "", f"status={status} body={text[:200]}")

    status, data, text = http("POST", "/v1/tts/speak", {})
    check("POST /v1/tts/speak", status == 200 and isinstance(data, dict), f"status={status} body={text[:200]}")

    status, stats_before, _ = http("GET", "/bridge/stats")
    before = stats_before.get("chat_requests", 0) if isinstance(stats_before, dict) else 0
    status, data, text = http("POST", "/v1/chat/completions", CHAT_PAYLOAD)
    ok = status == 200
    detail = f"status={status} body={text[:200]}"
    if ok and isinstance(data, dict):
        try:
            content = data["choices"][0]["message"]["content"]
            parsed = json.loads(content)
            ok = set(parsed) >= {"reason", "command", "message"} and all(
                isinstance(parsed.get(key), str) for key in ("reason", "command", "message")
            )
            detail = f"content={content[:160]}"
        except Exception as exc:
            ok = False
            detail = f"envelope parse error: {exc!r}; body={text[:200]}"
    else:
        ok = False
    check("POST /v1/chat/completions valid envelope", ok, detail)

    status, stats_after, _ = http("GET", "/bridge/stats")
    after = stats_after.get("chat_requests", 0) if isinstance(stats_after, dict) else 0
    check("chat_requests counter incremented", after > before, f"before={before} after={after}")

    status, stats_data, stats_text = http("GET", "/bridge/stats")
    stats_data = stats_data if isinstance(stats_data, dict) else {}

    requests_info = stats_data.get("requests")
    ok = (
        status == 200
        and isinstance(requests_info, dict)
        and set(requests_info) >= {"limit_per_hour", "available", "refill_per_sec"}
        and isinstance(requests_info.get("limit_per_hour"), int)
        and isinstance(requests_info.get("available"), int)
        and requests_info["available"] <= requests_info["limit_per_hour"]
    )
    check(
        "GET /bridge/stats requests block",
        ok,
        f"status={status} requests={requests_info}",
    )

    recent = stats_data.get("recent_requests")
    ok = isinstance(recent, list)
    detail = f"recent_requests is {type(recent).__name__}"
    if ok:
        newest = recent[0] if recent else None
        ok = isinstance(newest, dict) and set(newest) >= {
            "ts",
            "status",
            "duration_ms",
            "prompt_tokens",
            "completion_tokens",
            "total_tokens",
            "preview",
        }
        ok = ok and isinstance(newest.get("preview"), str) and len(newest["preview"]) <= 60
        detail = f"newest={newest}"
    check(
        "GET /bridge/stats recent_requests journal",
        ok,
        detail,
    )

    check(
        "GET /bridge/stats token_budget still present",
        isinstance(stats_data.get("token_budget"), dict),
        f"token_budget={stats_data.get('token_budget')}",
    )

    run_characters_reload_check()
    run_characters_id_backfill_check()

    status, data, text = http("GET", "/nope")
    check(
        "GET /nope -> 404",
        status == 404 and isinstance(data, dict) and data.get("error") == "not found",
        f"status={status} body={text[:200]}",
    )

    status, data, text = http("POST", "/v1/chat/completions", b"not json")
    check(
        "POST /v1/chat/completions invalid json -> 400",
        status == 400 and isinstance(data, dict) and data.get("error") == "invalid json",
        f"status={status} body={text[:200]}",
    )

    status, data, text = http("POST", "/v1/chat/completions", {"messages": []})
    check(
        "POST /v1/chat/completions empty messages -> 400",
        status == 400
        and isinstance(data, dict)
        and data.get("error") == "messages must be a non-empty list of {role,content}",
        f"status={status} body={text[:200]}",
    )

    status, data, text = http("POST", "/v1/chat/completions", {"foo": 1})
    check(
        "POST /v1/chat/completions missing messages -> 400",
        status == 400
        and isinstance(data, dict)
        and data.get("error") == "messages must be a non-empty list of {role,content}",
        f"status={status} body={text[:200]}",
    )


class _LogCapture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.messages = []

    def emit(self, record):
        try:
            self.messages.append(record.getMessage())
        except Exception:
            pass


def _dotenv_key():
    seen = set()
    for candidate in (REPO_ROOT / ".env", Path.cwd().parent / ".env", Path.cwd() / ".env"):
        try:
            resolved = str(candidate.resolve())
        except OSError:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        try:
            text = candidate.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            if key.lower().startswith("export "):
                key = key[7:].strip()
            if key == "KILO_API_KEY":
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                    value = value[1:-1]
                if value:
                    return value
    return ""


def run_live_checks():
    key = (os.environ.get("KILO_API_KEY") or "").strip() or _dotenv_key().strip()
    if not key:
        skip("--live (no KILO_API_KEY)")
        return

    live_cfg = BridgeConfig(
        http_port=0,
        local_login_port=0,
        tls_enabled=False,
        requests_per_hour=10000,
        kilo_api_key=key,
    )
    server.init(live_cfg)
    live_srv, live_port = _start(live_cfg)

    capture = _LogCapture()
    p2k_logger = logging.getLogger("p2k")
    p2k_logger.addHandler(capture)
    before = dict(state_mod.STATE.snapshot())
    budget_before = _token_budget(live_port) or {}
    started = time.time()
    status, data, text = http("POST", "/v1/chat/completions", CHAT_PAYLOAD, port=live_port, timeout=120)
    latency_ms = (time.time() - started) * 1000.0
    after = dict(state_mod.STATE.snapshot())
    budget_after = _token_budget(live_port) or {}
    p2k_logger.removeHandler(capture)

    ok_delta = after.get("chat_ok", 0) - before.get("chat_ok", 0)
    err_delta = after.get("kilo_errors", 0) - before.get("kilo_errors", 0)

    if status == 200 and ok_delta > 0:
        content = json.loads(data["choices"][0]["message"]["content"])
        check("--live chat_ok incremented", True)
        print(f"live model reply ({latency_ms:.0f} ms): {str(content.get('message', ''))[:200]}")
        print(f"live command: {content.get('command', '')!r}")
        print(f"live reason : {content.get('reason', '')!r}")
        # Budget disabled by default: usage is still TRACKED, never denied.
        check(
            "--live token budget tracks real usage (lifetime_total > 0)",
            isinstance(budget_after.get("lifetime_total"), int) and budget_after["lifetime_total"] > 0,
            f"before={budget_before.get('lifetime_total')} after={budget_after.get('lifetime_total')}",
        )
        check(
            "--live token budget window_used > 0",
            isinstance(budget_after.get("window_used"), int) and budget_after["window_used"] > 0,
            f"before={budget_before.get('window_used')} after={budget_after.get('window_used')}",
        )
        check(
            "--live token budget recorded no denials (budget disabled)",
            budget_after.get("lifetime_denials") == 0 and budget_after.get("tokens_per_window") == 0,
            f"lifetime_denials={budget_after.get('lifetime_denials')} "
            f"tokens_per_window={budget_after.get('tokens_per_window')}",
        )
    elif err_delta > 0 or status != 200:
        error = next((message for message in reversed(capture.messages) if message), None)
        detail = error or (text[:200] if text else "no upstream reply")
        skip(f"--live (kilo unreachable: {detail})")
    else:
        skip(f"--live (no chat_ok increment; status={status} body={text[:200]})")

    live_srv.shutdown()


def run_tls_check():
    cert = REPO_ROOT / "tls" / "certs" / "server.crt"
    key = REPO_ROOT / "tls" / "certs" / "server.key"
    if not (cert.is_file() and key.is_file()):
        skip("TLS (no certs; run bash tls/gen_certs.sh)")
        return

    tls_cfg = BridgeConfig(http_port=0, local_login_port=0, tls_enabled=True, requests_per_hour=10000)
    tsrv, tls_port = _start(tls_cfg, "127.0.0.1", tls=True, tls_cert=str(cert), tls_key=str(key))
    try:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        raw = socket.create_connection(("127.0.0.1", tls_port), timeout=15)
        try:
            tls_sock = ctx.wrap_socket(raw, server_hostname="api.player2.game")
        except Exception:
            raw.close()
            raise
        conn = http_client.HTTPConnection("127.0.0.1", tls_port, timeout=15)
        conn.sock = tls_sock
        conn.request("GET", "/v1/health")
        response = conn.getresponse()
        body = response.read().decode("utf-8", "replace")
        status = response.status
        conn.close()
        data = _maybe_json(body)
        if status == 200 and isinstance(data, dict) and data.get("client_version"):
            check("TLS handshake + roundtrip", True)
            print("TLS handshake + roundtrip: PASS")
        else:
            check("TLS handshake + roundtrip", False, f"status={status} body={body[:200]}")
    except Exception as exc:
        check("TLS handshake + roundtrip", False, repr(exc))
    finally:
        tsrv.shutdown()


def run_budget_disabled_default_check():
    """Default config must ship with the per-window token budget DISABLED.

    The budget is tracking-only by default: nothing is reserved, so a chat POST
    can never be denied on tokens and /bridge/stats must report
    tokens_per_window == 0 with lifetime_denials == 0.
    """
    cfg = BridgeConfig()  # library defaults
    server.init(cfg)
    check(
        "budget disabled by default: server.BUDGET_ENABLED is False",
        server.BUDGET_ENABLED is False,
        f"BUDGET_ENABLED={server.BUDGET_ENABLED}",
    )
    budget_srv, budget_port = _start(cfg)
    try:
        replies = []
        for _ in range(2):
            status, data, text = http("POST", "/v1/chat/completions", SMALL_PAYLOAD, port=budget_port)
            replies.append((status, data, text))

        ok = all(status == 200 for status, _data, _text in replies)
        detail = f"statuses={[status for status, _d, _t in replies]}"
        for _status, data, text in replies:
            if not ok:
                break
            ok, detail = _parse_envelope(data, text)
        check(
            "budget disabled: both POSTs return 200 with parseable envelopes",
            ok,
            detail,
        )

        token_budget = _token_budget(budget_port)
        check(
            "budget disabled: stats lifetime_denials == 0 and tokens_per_window == 0",
            isinstance(token_budget, dict)
            and token_budget.get("lifetime_denials") == 0
            and token_budget.get("tokens_per_window") == 0,
            f"token_budget={token_budget}",
        )
    finally:
        budget_srv.shutdown()


def run_tracking_check():
    """Tracking-only: with the budget disabled a failed upstream call still
    answers a valid envelope, records no denial, and still moves the stats."""
    cfg = BridgeConfig()  # defaults: budget disabled, no API key (offline-safe)
    server.init(cfg)
    tracking_srv, tracking_port = _start(cfg)
    try:
        stats_before = state_mod.STATE.snapshot()
        budget_before = _token_budget(tracking_port) or {}
        status, data, text = http("POST", "/v1/chat/completions", BUDGET_PAYLOAD, port=tracking_port)
        stats_after = state_mod.STATE.snapshot()
        budget_after = _token_budget(tracking_port) or {}

        ok, detail = _parse_envelope(data, text)
        check(
            "tracking: failed/offline chat still returns a valid envelope (HTTP 200)",
            status == 200 and ok,
            f"status={status} {detail}",
        )

        denials = budget_after.get("lifetime_denials")
        check(
            "tracking: no token-budget denial recorded (budget disabled)",
            isinstance(denials, int) and denials == 0 and budget_before.get("lifetime_denials", 0) == 0,
            f"lifetime_denials={denials} (before={budget_before.get('lifetime_denials')})",
        )

        counters_moved = any(
            stats_after.get(key, 0) > stats_before.get(key, 0)
            for key in ("chat_requests", "chat_ok", "chat_failed", "kilo_errors")
        )
        check(
            "tracking: stats counters incremented for the chat call",
            counters_moved,
            "no chat counter moved",
        )

        total_before = budget_before.get("lifetime_total", 0)
        total_after = budget_after.get("lifetime_total")
        check(
            "tracking: lifetime_total only grows via real gateway usage",
            isinstance(total_after, int)
            and (
                total_after > total_before
                or stats_after.get("kilo_errors", 0) > stats_before.get("kilo_errors", 0)
            ),
            f"lifetime_total before={total_before} after={total_after}",
        )
    finally:
        tracking_srv.shutdown()


def run_budget_enabled_check():
    """Regression: with token_budget_per_window > 0 the budget is enforced again."""
    budget_cfg = BridgeConfig(
        http_port=0,
        tls_enabled=False,
        token_budget_per_window=BUDGET_TOKENS_PER_WINDOW,
        requests_per_hour=10000,
    )
    server.init(budget_cfg)
    check(
        "budget enabled when configured: server.BUDGET_ENABLED is True",
        server.BUDGET_ENABLED is True,
        f"BUDGET_ENABLED={server.BUDGET_ENABLED}",
    )
    budget_srv, budget_port = _start(budget_cfg)
    try:
        status, data, text = http("POST", "/v1/chat/completions", BUDGET_PAYLOAD, port=budget_port)
        ok = status == 200
        detail = f"status={status}"
        if ok:
            ok, detail = _parse_envelope(data, text)
        check("budget enabled: POST returns 200 with a parseable envelope", ok, detail)

        idle = False
        if isinstance(data, dict):
            try:
                parsed = json.loads(data["choices"][0]["message"]["content"])
                idle = parsed == {"reason": "", "command": "", "message": ""}
            except Exception:
                idle = False
        check(
            "budget enabled: POST below the window returns the idle envelope",
            idle,
            f"body={text[:200]}",
        )

        token_budget = _token_budget(budget_port)
        check(
            "budget enabled: stats tokens_per_window == 10 and lifetime_denials >= 1",
            isinstance(token_budget, dict)
            and token_budget.get("tokens_per_window") == BUDGET_TOKENS_PER_WINDOW
            and isinstance(token_budget.get("lifetime_denials"), int)
            and token_budget.get("lifetime_denials") >= 1,
            f"token_budget={token_budget}",
        )
        remaining = token_budget.get("remaining") if isinstance(token_budget, dict) else None
        check(
            "budget enabled: remaining <= window capacity",
            isinstance(remaining, int) and remaining <= BUDGET_TOKENS_PER_WINDOW,
            f"remaining={remaining}",
        )
    finally:
        budget_srv.shutdown()


def parse_args(argv=None):
    parser = argparse.ArgumentParser(prog="p2k-selftest", description="p2k-bridge HTTP layer self test")
    parser.add_argument("--live", action="store_true", help="also exercise the real Kilo Gateway")
    return parser.parse_args(argv)


def main(argv=None):
    global CFG, PORT, SRV
    args = parse_args(argv)

    CFG = BridgeConfig(http_port=0, local_login_port=0, tls_enabled=False, requests_per_hour=10000)
    server.init(CFG)
    SRV, PORT = _start(CFG)

    run_core_checks()

    if args.live:
        SRV.shutdown()
        run_live_checks()

    run_tls_check()

    run_budget_disabled_default_check()
    run_tracking_check()
    run_budget_enabled_check()

    if FAILED:
        print(f"FAIL: {FAILED}/{TOTAL} checks failed ({SKIPPED} skipped)")
    else:
        summary = f"PASS: {TOTAL}/{TOTAL}"
        if SKIPPED:
            summary += f" ({SKIPPED} skipped)"
        print(summary)

    SRV.shutdown()
    sys.exit(0 if FAILED == 0 else 1)


if __name__ == "__main__":
    main()
