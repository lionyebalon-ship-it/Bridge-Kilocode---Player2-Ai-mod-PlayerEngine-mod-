"""Thread-safe shared state for the bridge (characters, device codes, stats)."""

import collections
import secrets
import threading
import time
from typing import Any, Optional

DEFAULT_VERIFICATION_BASE = "http://127.0.0.1:4315/verify"

_COUNTER_NAMES = (
    "chat_requests",
    "chat_ok",
    "chat_failed",
    "kilo_errors",
    "rate_limited",
    "bytes_up",
    "bytes_down",
    "stt_requests",
    "stt_ok",
    "stt_no_speech",
    "stt_failed",
    "stt_rate_limited",
    "unserved_requests",
)

JOURNAL_MAXLEN = 100
RECENT_REQUESTS_DEFAULT = 20
RECENT_REQUESTS_LIMIT = 100
PREVIEW_MAXLEN = 60

JOURNAL_ENTRY_KEYS = (
    "ts",
    "status",
    "duration_ms",
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "preview",
)


def _non_negative_int(value: Any, default: int = 0) -> int:
    """Coerce value to an int, floored at 0 (default on failure)."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return number if number > 0 else 0


def _clean_preview(value: Any, limit: int = PREVIEW_MAXLEN) -> str:
    """Coerce to str, collapse whitespace/newlines to single spaces, truncate."""
    text = "" if value is None else str(value)
    text = " ".join(text.split())
    return text[:limit]


class _Stats(dict):
    """dict of counters with thread-safe increment/snapshot helpers."""

    def __init__(self, lock, initial):
        super().__init__(initial)
        self._lock = lock

    def increment(self, name: str, n: int = 1) -> None:
        with self._lock:
            if name == "started_at":
                return
            current = self.get(name)
            if isinstance(current, (int, float)) and not isinstance(current, bool):
                self[name] = current + n
            else:
                self[name] = n

    def snapshot(self) -> dict:
        with self._lock:
            snap = dict(self)
        snap["uptime_seconds"] = max(0.0, time.time() - float(snap.get("started_at", time.time())))
        return snap


class BridgeState:
    """Thread-safe registry of characters, device codes and request statistics."""

    def __init__(
        self,
        characters: Optional[list[dict]] = None,
        verification_base: str = DEFAULT_VERIFICATION_BASE,
    ) -> None:
        self._lock = threading.RLock()
        self._characters = [c for c in (characters or []) if isinstance(c, dict)]
        self._device_codes: dict[str, dict[str, Any]] = {}
        self._request_journal: collections.deque = collections.deque(maxlen=JOURNAL_MAXLEN)
        self.stats = _Stats(self._lock, {name: 0 for name in _COUNTER_NAMES})
        self.stats["started_at"] = time.time()
        self._verification_base = verification_base

    @property
    def characters(self) -> list[dict]:
        with self._lock:
            return list(self._characters)

    def set_characters(self, characters) -> None:
        with self._lock:
            self._characters = [c for c in (characters or []) if isinstance(c, dict)]

    @property
    def device_codes(self) -> dict:
        with self._lock:
            return dict(self._device_codes)

    def set_verification_base(self, base: str) -> None:
        with self._lock:
            self._verification_base = str(base)

    def issue_device_code(self, client_id: str) -> tuple[str, str, int]:
        client = "" if client_id is None else str(client_id)
        with self._lock:
            code = secrets.token_urlsafe(24)
            while code in self._device_codes:
                code = secrets.token_urlsafe(24)
            self._device_codes[code] = {"client_id": client, "created": time.time()}
            base = self._verification_base
        verification_uri_complete = f"{base}?code={code}"
        return code, verification_uri_complete, 5

    def consume_device_code(self, code: str) -> Optional[str]:
        with self._lock:
            entry = self._device_codes.pop(code, None)
        if not isinstance(entry, dict):
            return None
        client_id = entry.get("client_id")
        return client_id if isinstance(client_id, str) else None

    def increment(self, name: str, n: int = 1) -> None:
        self.stats.increment(name, n)

    def record_request(
        self,
        status: str,
        duration_ms: int,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        total_tokens: int = 0,
        preview: str = "",
        kind: str = "",
        character_id: str = "",
        model: str = "",
    ) -> None:
        """Append one chat-call record to the journal (newest at the right)."""
        cleaned_status = "" if status is None else str(status).strip()
        entry = {
            "ts": time.time(),
            "status": cleaned_status if cleaned_status else "ok",
            "duration_ms": _non_negative_int(duration_ms),
            "prompt_tokens": _non_negative_int(prompt_tokens),
            "completion_tokens": _non_negative_int(completion_tokens),
            "total_tokens": _non_negative_int(total_tokens),
            "preview": _clean_preview(preview),
            "kind": "" if kind is None else str(kind).strip(),
            "character_id": "" if character_id is None else str(character_id).strip(),
            "model": "" if model is None else str(model).strip(),
        }
        with self._lock:
            self._request_journal.append(entry)

    def recent_requests(self, n: int = RECENT_REQUESTS_DEFAULT) -> list[dict]:
        """Return up to n journal entries as copies, newest first."""
        limit = _non_negative_int(n, RECENT_REQUESTS_DEFAULT)
        limit = min(max(limit, 1), RECENT_REQUESTS_LIMIT)
        with self._lock:
            entries = list(self._request_journal)
        return [dict(entry) for entry in entries[-limit:][::-1]]

    def snapshot(self) -> dict:
        snap = self.stats.snapshot()
        snap["recent_requests"] = self.recent_requests(RECENT_REQUESTS_DEFAULT)
        return snap


STATE = BridgeState()
