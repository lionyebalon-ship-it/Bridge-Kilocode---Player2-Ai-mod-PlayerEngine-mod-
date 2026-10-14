"""HTTP server layer for the Player2 -> Kilo bridge."""

import json
import logging
import os
import re
import socketserver
import ssl
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional
from urllib.parse import unquote

from p2k_bridge.budget import TokenBudget
from p2k_bridge.config import BridgeConfig
from p2k_bridge.kilo import KiloClient, KiloError
from p2k_bridge.normalize import normalize_llm_content, set_model, to_openai_envelope
from p2k_bridge.ratelimit import TokenBucket
from p2k_bridge.state import STATE

SERVICE_NAME = "p2k-bridge"
SERVICE_VERSION = "1.0.0"
MAX_BODY_BYTES = 1024 * 1024
_P2K_KEY = "p2k-local"

ACCESS_LOG = logging.getLogger("p2k.access")
LOG = logging.getLogger("p2k.bridge")

DEFAULT_WINDOW_SECONDS = 3600.0

CFG: Optional[BridgeConfig] = None
BUCKET: Optional[TokenBucket] = None
BUDGET: Optional[TokenBudget] = None
BUDGET_ENABLED: bool = False
KILO: Optional[KiloClient] = None
CONFIG_PATH: Optional[str] = None

CHARACTERS_FILENAME = "characters.json"
_CHARACTERS_LOCK = threading.Lock()
_CHARACTERS_CACHE: dict[str, Any] = {"path": None, "mtime": None, "chars": None}

_ID_INVALID_CHARS = re.compile(r"[^A-Za-z0-9_-]+")
_ID_MAX_LENGTH = 64
_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")


def init(cfg: BridgeConfig, config_path: Optional[str] = None) -> BridgeConfig:
    global CFG, BUCKET, BUDGET, BUDGET_ENABLED, KILO, CONFIG_PATH
    CFG = cfg
    BUCKET = TokenBucket(cfg.requests_per_hour)
    BUDGET_ENABLED = getattr(cfg, "token_budget_per_window", 0) > 0
    # A budget built with tokens_per_window=0 is tracking-only: try_reserve is
    # never called on it, commit() still records window/lifetime usage.
    BUDGET = TokenBudget(
        cfg.token_budget_per_window if BUDGET_ENABLED else 0,
        getattr(cfg, "window_seconds", DEFAULT_WINDOW_SECONDS),
    )
    KILO = KiloClient(cfg)
    CONFIG_PATH = (
        config_path or getattr(cfg, "config_path", None) or os.environ.get("P2K_CONFIG") or None
    )
    STATE.set_characters(load_characters(cfg))
    set_model(cfg.model)
    return cfg


def _valid_messages(messages: Any) -> bool:
    if not isinstance(messages, list) or not messages:
        return False
    for message in messages:
        if not isinstance(message, dict):
            return False
        if not isinstance(message.get("role"), str) or not isinstance(message.get("content"), str):
            return False
    return True


def resolve_rel_path(p: Optional[str], cfg: Optional[BridgeConfig] = None) -> Optional[str]:
    if p is None:
        return None
    text = str(p)
    if not text:
        return text
    candidate = Path(text)
    if candidate.is_absolute():
        return text
    if candidate.exists():
        return text
    repo_root = Path(__file__).resolve().parent.parent
    probe = repo_root / text
    if probe.exists():
        return str(probe)
    config_path = getattr(cfg, "config_path", None) or CONFIG_PATH or os.environ.get("P2K_CONFIG")
    if config_path:
        cfg_probe = Path(config_path).resolve().parent / text
        if cfg_probe.exists():
            return str(cfg_probe)
    return text


def _effective_config_path(cfg: Optional[BridgeConfig] = None) -> Optional[str]:
    if cfg is not None:
        cfg_path = getattr(cfg, "config_path", None)
        if cfg_path:
            return str(cfg_path)
    return CONFIG_PATH or os.environ.get("P2K_CONFIG") or None


def characters_file_candidates(config_path: Optional[str] = None) -> list[Path]:
    """Ordered candidate locations for the character roster file.

    First the directory of the given config path, then the directory holding
    this server module (bridge/), then the current working directory.
    """
    candidates: list[Path] = []
    if config_path:
        candidates.append(Path(config_path).expanduser().resolve().parent / CHARACTERS_FILENAME)
    candidates.append(Path(__file__).resolve().parent / CHARACTERS_FILENAME)
    candidates.append(Path.cwd() / CHARACTERS_FILENAME)
    ordered: list[Path] = []
    for candidate in candidates:
        if candidate not in ordered:
            ordered.append(candidate)
    return ordered


def resolve_characters_path(config_path: Optional[str] = None) -> Optional[Path]:
    """First existing characters.json candidate, or None when there is none."""
    for candidate in characters_file_candidates(config_path):
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            continue
    return None


def _valid_roster(parsed: Any) -> Optional[list[dict]]:
    """Validate a parsed roster document; None when malformed."""
    if not isinstance(parsed, dict):
        return None
    characters = parsed.get("characters")
    if not isinstance(characters, list):
        return None
    roster: list[dict] = []
    for entry in characters:
        if not isinstance(entry, dict):
            return None
        if not isinstance(entry.get("name"), str) or not isinstance(entry.get("short_name"), str):
            return None
        roster.append(entry)
    return roster


def _cfg_characters(cfg: Optional[BridgeConfig]) -> list[dict]:
    return [c for c in (getattr(cfg, "characters", None) or []) if isinstance(c, dict)]


def _slugify(text: Any) -> str:
    """Lowercase slug: non-alphanumeric runs collapse to '-', edges stripped."""
    return "-".join(part for part in re.split(r"[^a-zA-Z0-9]+", str(text).lower()) if part)


def _sanitize_id(value: Any) -> str:
    """Coerce a value onto ^[A-Za-z0-9_-]+$ (invalid chars -> '-', capped at 64)."""
    return _ID_INVALID_CHARS.sub("-", str(value))[:_ID_MAX_LENGTH]


def _disambiguate_id(candidate: str, taken: set[str]) -> str:
    """Append -2, -3, ... to candidate until it no longer collides."""
    unique = candidate
    suffix = 2
    while unique in taken:
        unique = f"{candidate}-{suffix}"
        suffix += 1
    return unique


def _backfill_character_ids(roster: list[dict]) -> list[dict]:
    """Return a copy of roster in which every entry has a unique, non-empty id.

    Player2NPC's AutomatoneEntity aborts the companion spawn when a served
    character has no id, so the bridge guarantees one here no matter where the
    roster came from (characters.json, the config.json fallback, or the CLI).
    Entries are shallow-copied: the on-disk file and the caller's dicts are
    never mutated. Ids are generated from short_name, then name, then a random
    ``char-`` token, then made unique within the list.
    """
    served: list[dict] = []
    taken: set[str] = set()
    for entry in roster:
        character = dict(entry)
        raw_id = character.get("id")
        if not isinstance(raw_id, str) or not raw_id.strip():
            slug = _slugify(character.get("short_name", "")) or _slugify(character.get("name", ""))
            character["id"] = _sanitize_id(slug or f"char-{uuid.uuid4().hex[:8]}")
            LOG.debug("backfilled character id %r for %r", character["id"], character.get("name"))
        unique_id = _disambiguate_id(character["id"], taken)
        if unique_id != character["id"]:
            LOG.debug(
                "character id %r collided; serving %r for %r",
                character["id"],
                unique_id,
                character.get("name"),
            )
            character["id"] = unique_id
        taken.add(unique_id)
        served.append(character)
    return served


def _reset_characters_cache() -> None:
    _CHARACTERS_CACHE.update(path=None, mtime=None, chars=None)


def load_characters(cfg: Optional[BridgeConfig]) -> list[dict]:
    """Return the character roster, hot-reloading characters.json on change.

    Prefers the first existing roster file (see resolve_characters_path) and
    falls back to cfg.characters when absent. Results are cached by
    (path, mtime): an unchanged file is served from cache, a changed mtime is
    reloaded (and logs "reloaded"), and any parse/validation failure keeps the
    previous roster after logging a warning. Every served entry is guaranteed
    a unique, non-empty id (see _backfill_character_ids); nothing on disk is
    rewritten.
    """
    fallback = _backfill_character_ids(_cfg_characters(cfg))
    path = resolve_characters_path(_effective_config_path(cfg))
    if path is None:
        with _CHARACTERS_LOCK:
            _reset_characters_cache()
        return fallback
    try:
        mtime = os.stat(path).st_mtime_ns
    except OSError:
        with _CHARACTERS_LOCK:
            _reset_characters_cache()
        return fallback
    with _CHARACTERS_LOCK:
        cached_path = _CHARACTERS_CACHE["path"]
        cached_mtime = _CHARACTERS_CACHE["mtime"]
        cached_chars = _CHARACTERS_CACHE["chars"]
    if cached_path == path and cached_mtime == mtime and cached_chars is not None:
        return cached_chars
    try:
        parsed = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        LOG.warning("characters roster %s unreadable (%s); keeping previous roster", path, exc)
        if cached_chars is not None and cached_path == path:
            return cached_chars
        with _CHARACTERS_LOCK:
            _reset_characters_cache()
        return fallback
    roster = _valid_roster(parsed)
    if roster is None:
        LOG.warning("characters roster %s invalid; keeping previous roster", path)
        if cached_chars is not None and cached_path == path:
            return cached_chars
        with _CHARACTERS_LOCK:
            _reset_characters_cache()
        return fallback
    roster = _backfill_character_ids(roster)
    with _CHARACTERS_LOCK:
        _CHARACTERS_CACHE.update(path=path, mtime=mtime, chars=roster)
    reloaded = cached_path == path and cached_mtime is not None and cached_mtime != mtime
    LOG.info(
        "characters %s from %s (n=%d)",
        "reloaded" if reloaded else "loaded",
        path,
        len(roster),
    )
    return roster


def _chat_preview(messages: Any) -> str:
    """Last user message content, whitespace-collapsed and truncated to 60 chars."""
    if isinstance(messages, list):
        for message in reversed(messages):
            if isinstance(message, dict) and message.get("role") == "user":
                content = message.get("content")
                text = "" if content is None else str(content)
                return " ".join(text.split())[:60]
    return ""


def _record_chat(
    status: str,
    t0: float,
    prompt_tokens: int,
    completion_tokens: int,
    total_tokens: int,
    preview: str,
) -> None:
    """Journal one chat request and emit the single INFO summary line."""
    duration_ms = int((time.monotonic() - t0) * 1000)
    try:
        prompt_tokens = max(0, int(prompt_tokens or 0))
        completion_tokens = max(0, int(completion_tokens or 0))
        total_tokens = max(0, int(total_tokens or 0))
    except (TypeError, ValueError):
        prompt_tokens = completion_tokens = total_tokens = 0
    STATE.record_request(
        status=status,
        duration_ms=duration_ms,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
        preview=preview,
    )
    LOG.info(
        'chat status=%s %dtok (%din/%dout) %dms :: "%s"',
        status,
        total_tokens,
        prompt_tokens,
        completion_tokens,
        duration_ms,
        preview,
    )


class BridgeHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = f"{SERVICE_NAME}/{SERVICE_VERSION}"
    sys_version = ""

    def _send_json(self, obj: Any, status: int = 200) -> None:
        body = json.dumps(obj).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError):
            self.close_connection = True

    def _read_body(self) -> Optional[bytes]:
        raw_length = self.headers.get("Content-Length")
        try:
            length = int(str(raw_length).strip())
        except (TypeError, ValueError):
            length = 0
        if length <= 0:
            return b""
        if length > MAX_BODY_BYTES:
            self.close_connection = True
            return None
        chunks: list[bytes] = []
        remaining = length
        while remaining > 0:
            chunk = self.rfile.read(min(remaining, 65536))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        body = b"".join(chunks)
        if len(body) != length:
            self.close_connection = True
        return body

    @staticmethod
    def _parse_json_object(body: bytes) -> Optional[dict]:
        try:
            parsed = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None
        return parsed if isinstance(parsed, dict) else None

    def _healthz(self) -> dict:
        started_at = STATE.stats.get("started_at", time.time())
        try:
            uptime = round(time.time() - float(started_at), 1)
        except (TypeError, ValueError):
            uptime = 0.0
        return {
            "ok": True,
            "service": SERVICE_NAME,
            "model": CFG.model if CFG is not None else "",
            "uptime_seconds": uptime,
        }

    def _handle_chat(self, body: bytes) -> None:
        data = self._parse_json_object(body)
        if data is None:
            self._send_json({"error": "invalid json"}, 400)
            return
        messages = data.get("messages")
        if not _valid_messages(messages):
            self._send_json({"error": "messages must be a non-empty list of {role,content}"}, 400)
            return
        t0 = time.monotonic()
        preview = _chat_preview(messages)
        STATE.increment("chat_requests")
        STATE.increment("bytes_up", len(body))
        if BUCKET is None or not BUCKET.try_acquire():
            STATE.increment("rate_limited")
            cap = CFG.requests_per_hour if CFG is not None else 0
            LOG.warning("rate limited (%s req/hour quota exhausted; degraded reply)", cap)
            _record_chat("rate_limited", t0, 0, 0, 0, preview)
            self._send_json(to_openai_envelope({"reason": "", "command": "", "message": ""}))
            return
        reserved = TokenBudget.estimate_tokens(messages) + (CFG.max_tokens if CFG is not None else 0)
        if BUDGET_ENABLED and (BUDGET is None or not BUDGET.try_reserve(reserved)):
            STATE.increment("rate_limited")
            remaining = BUDGET.remaining() if BUDGET is not None else 0
            LOG.info("token budget exhausted (remaining=%d)", remaining)
            _record_chat("budget_exhausted", t0, 0, 0, 0, preview)
            self._send_json(to_openai_envelope({"reason": "", "command": "", "message": ""}))
            return
        try:
            if KILO is None:
                raise KiloError("kilo client not initialized")
            resp = KILO.chat_completion(messages)
            content = resp["choices"][0]["message"]["content"]
            obj = normalize_llm_content(content)
            usage = resp.get("usage") if isinstance(resp, dict) else None
            usage_total = usage.get("total_tokens") if usage else None
            if usage_total is not None:
                actual_total = usage_total
                prompt_tokens = usage.get("prompt_tokens", 0)
                completion_tokens = usage.get("completion_tokens", 0)
            else:
                # Nothing to reserve against when the budget is disabled; the
                # committed amount is still tracked (never enforced).
                actual_total = reserved if BUDGET_ENABLED else 0
                prompt_tokens = 0
                completion_tokens = 0
            if BUDGET is not None:
                BUDGET.commit(
                    actual_total=actual_total,
                    reserved=reserved if BUDGET_ENABLED else 0,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                )
            STATE.increment("chat_ok")
            STATE.increment("bytes_down", 0)
            _record_chat("ok", t0, prompt_tokens, completion_tokens, usage_total or 0, preview)
            self._send_json(to_openai_envelope(obj))
        except KiloError as exc:
            if BUDGET_ENABLED and BUDGET is not None:
                BUDGET.refund(reserved)
            STATE.increment("chat_failed")
            STATE.increment("kilo_errors")
            LOG.error("%s", exc)
            _record_chat("kilo_error", t0, 0, 0, 0, preview)
            self._send_json(to_openai_envelope({"reason": "kilo unavailable", "command": "", "message": ""}))
        except Exception as exc:
            if BUDGET_ENABLED and BUDGET is not None:
                BUDGET.refund(reserved)
            STATE.increment("chat_failed")
            STATE.increment("kilo_errors")
            LOG.error("unexpected chat failure: %s", exc)
            _record_chat("kilo_error", t0, 0, 0, 0, preview)
            self._send_json(to_openai_envelope({"reason": "kilo unavailable", "command": "", "message": ""}))

    def _route_get(self, path: str) -> None:
        if path == "/bridge/healthz":
            self._send_json(self._healthz())
        elif path == "/bridge/stats":
            stats = STATE.snapshot()
            if BUDGET is not None:
                stats["token_budget"] = BUDGET.snapshot()
            if CFG is not None and BUCKET is not None:
                stats["requests"] = {
                    "limit_per_hour": int(CFG.requests_per_hour),
                    "available": int(BUCKET.available()),
                    "refill_per_sec": round(CFG.requests_per_hour / 3600.0, 4),
                }
            self._send_json(stats)
        elif path == "/v1/health":
            self._send_json({"client_version": f"{SERVICE_NAME}/{SERVICE_VERSION}", "status": "ok"})
        elif path == "/v1/selected_characters":
            chars = load_characters(CFG) if CFG is not None else list(STATE.characters)
            STATE.set_characters(chars)
            self._send_json({"characters": chars})
        elif path == "/v1/joules":
            # PlayerEngine's JoulesCache polls this (GET) roughly once per
            # JoulesRefreshInterval to feed its BudgetTracker soft/hard limits.
            # The bridge is the billing authority here — the token budget and
            # the 180 req/hr bucket are the real limits — so report an
            # effectively unlimited balance and let those systems decide.
            # Keys taken from JoulesCache.refresh's bytecode: joules (long),
            # patron_tier (string), user_id (string).
            self._send_json(
                {
                    "joules": sys.maxsize,
                    "patron_tier": "",
                    "user_id": "",
                }
            )
        elif path == "/":
            self._send_json({"service": SERVICE_NAME, "docs": "docs/SETUP.md"})
        else:
            self._send_json({"error": "not found"}, 404)

    def _route_post(self, path: str, body: bytes) -> None:
        if path == "/v1/chat/completions":
            self._handle_chat(body)
            return
        if path == "/v1/login/device/new":
            data = self._parse_json_object(body)
            if data is None:
                self._send_json({"error": "invalid json"}, 400)
                return
            code, uri, _interval = STATE.issue_device_code(data.get("client_id"))
            self._send_json(
                {
                    "deviceCode": code,
                    "userCode": code,
                    "verificationUri": uri,
                    "verificationUriComplete": uri,
                    "interval": 5,
                    "expiresIn": 600,
                }
            )
            return
        if path == "/v1/login/device/token":
            data = self._parse_json_object(body)
            if data is None:
                self._send_json({"error": "invalid json"}, 400)
                return
            code = data.get("device_code")
            if STATE.consume_device_code(code if isinstance(code, str) else "") is not None:
                self._send_json({"p2Key": _P2K_KEY, "token_type": "Bearer"})
            else:
                self._send_json({"error": "authorization_pending"}, 400)
            return
        if path.startswith("/v1/login/web"):
            game_key = unquote(path[len("/v1/login/web"):]).strip("/")
            LOG.info("POST /v1/login/web/%s -> issued p2Key (instant)", game_key)
            self._send_json({"p2Key": _P2K_KEY})
            return
        if path == "/v1/minecraft/schematics/search":
            data = self._parse_json_object(body) or {}
            query = data.get("query")
            LOG.warning(
                "POST /v1/minecraft/schematics/search -> schematics disabled (stub); query=%r max_results=%r",
                query,
                data.get("max_results"),
            )
            self._send_json({"results": [], "schematicInfos": []})
            return
        if path == "/v1/stt/start":
            self._send_json({})
            return
        if path == "/v1/stt/stop":
            self._send_json({"text": ""})
            return
        if path == "/v1/tts/speak":
            self._send_json({})
            return
        self._send_json({"error": "not found"}, 404)

    def _route(self, method: str) -> None:
        body = self._read_body()
        if body is None:
            self._send_json({"error": "payload too large"}, 413)
            return
        path = self.path.split("?", 1)[0]
        if method == "GET":
            self._route_get(path)
        else:
            self._route_post(path, body)

    def do_GET(self) -> None:
        self._route("GET")

    def do_POST(self) -> None:
        self._route("POST")

    def log_message(self, fmt: str, *args) -> None:
        ACCESS_LOG.debug("%s %s", self.address_string(), fmt % args)

    def handle_one_request(self) -> None:
        try:
            super().handle_one_request()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError):
            self.close_connection = True


def _handle_server_error(request: Any, client_address: Any) -> None:
    LOG.debug("client handler error from %s", client_address)


class _BridgeServer(ThreadingHTTPServer):
    """ThreadingHTTPServer that never resolves the bind address to an FQDN.

    ``http.server.HTTPServer.server_bind`` calls ``socket.getfqdn(host)``.
    ``socket.getfqdn`` only catches ``socket.error`` (an ``OSError``), but the
    reverse lookup it performs can raise ``UnicodeEncodeError`` (a
    ``ValueError``) when the local hostname's first label exceeds the 63-octet
    IDNA limit — e.g. Docker's default 64-character container-id hostname.
    That escapes uncaught and kills the bridge at startup before any listener
    is up. The bridge never uses ``server_name`` (no redirect generation, no
    ``Server:``-header WebDAV trickery), so deriving it from the bind address
    is both safe and sufficient.
    """

    daemon_threads = True
    allow_reuse_address = True

    def server_bind(self) -> None:
        # Re-implementation of socketserver.TCPServer.server_bind +
        # http.server.HTTPServer.server_bind, minus the getfqdn call.
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = str(host)
        self.server_port = port


def build_server(
    cfg: BridgeConfig,
    host: str,
    port: int,
    tls: bool = False,
    tls_cert: Optional[str] = None,
    tls_key: Optional[str] = None,
) -> ThreadingHTTPServer:
    srv = _BridgeServer((host, int(port)), BridgeHandler)
    srv.handle_error = _handle_server_error
    if tls:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(tls_cert, tls_key)
        srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    srv.p2k_tls = bool(tls)
    return srv


def _log_startup_summary(cfg: BridgeConfig, servers: list) -> None:
    key_configured = bool((getattr(cfg, "kilo_api_key", "") or "").strip())
    LOG.info("%s %s ready", SERVICE_NAME, SERVICE_VERSION)
    for label, srv in servers:
        address = srv.server_address
        host = address[0] if address else "?"
        port = address[1] if address and len(address) > 1 else "?"
        scheme = "https" if getattr(srv, "p2k_tls", False) else "http"
        note = " (TLS, servername api.player2.game)" if getattr(srv, "p2k_tls", False) else ""
        LOG.info("  %-11s %s://%s:%s%s", label, scheme, host, port, note)
    LOG.info(
        "  model=%s quota=%s req/hour kilo_api_key=%s",
        cfg.model,
        cfg.requests_per_hour,
        "configured" if key_configured else "not configured",
    )
    if BUDGET_ENABLED:
        LOG.info(
            "  token budget: %d tokens / %gs window (enforced)",
            getattr(cfg, "token_budget_per_window", 0),
            getattr(cfg, "window_seconds", DEFAULT_WINDOW_SECONDS),
        )
    else:
        LOG.info(
            "  token budget: disabled (usage tracked only); max_tokens=%d "
            "(clamped to context %d)",
            getattr(cfg, "max_tokens", 0),
            getattr(cfg, "model_context_limit", 0),
        )


def start_all(cfg: BridgeConfig) -> list:
    servers: list = []

    try:
        main = build_server(cfg, cfg.listen_host, cfg.http_port)
    except OSError as exc:
        LOG.error("main listener %s:%s failed: %s", cfg.listen_host, cfg.http_port, exc)
        raise
    servers.append(("main-http", main))

    try:
        shim = build_server(cfg, "127.0.0.1", cfg.local_login_port)
    except OSError as exc:
        LOG.error("login shim 127.0.0.1:%s failed to start: %s", cfg.local_login_port, exc)
    else:
        servers.append(("login-shim", shim))

    if cfg.tls_enabled:
        cert = resolve_rel_path(cfg.tls_cert, cfg)
        key = resolve_rel_path(cfg.tls_key, cfg)
        if cert and key and Path(cert).is_file() and Path(key).is_file():
            try:
                tls_server = build_server(
                    cfg,
                    "0.0.0.0",
                    cfg.tls_port,
                    tls=True,
                    tls_cert=cert,
                    tls_key=key,
                )
            except OSError as exc:
                LOG.warning(
                    "TLS listener 0.0.0.0:%s not started (%s). Binding 443 needs root or "
                    "CAP_NET_BIND_SERVICE; unprivileged alternative: set \"tls_port\": 8443 in "
                    "bridge/config.json and run: sudo iptables -t nat -A OUTPUT -p tcp -d 127.0.0.1 "
                    "--dport 443 -j REDIRECT --to-port 8443",
                    cfg.tls_port,
                    exc,
                )
            else:
                servers.append(("tls", tls_server))
        else:
            LOG.warning("TLS enabled but cert files missing; run bash tls/gen_certs.sh")

    for _label, srv in servers:
        threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.5}, daemon=True).start()

    _log_startup_summary(cfg, servers)
    return servers
