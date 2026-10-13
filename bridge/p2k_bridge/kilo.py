"""Client for the Kilo Code AI Gateway (OpenAI-compatible chat completions)."""

import json
import logging
import socket
import urllib.error
import urllib.request
from typing import Any, Optional

from .budget import TokenBudget
from .config import BridgeConfig

_LOGGER = logging.getLogger(__name__)

_USER_AGENT = "p2k-bridge/1.0"
_FALLBACK_BASE_URL = "https://api.kilo.ai/api/gateway"

# Response headroom kept below the model context limit so the gateway never
# rejects a request for exceeding the window, and a hard floor so tiny/degenerate
# contexts still ask for a usable amount of output.
_OUTPUT_HEADROOM = 1024
_MIN_OUTPUT_TOKENS = 256


def compute_max_tokens(cfg: BridgeConfig, messages: list[dict]) -> int:
    """Effective max_tokens for this request.

    The user wants a large "free reign" cap (default 500k), not the model's
    per-window token budget. So the ceiling is cfg.max_tokens when no model
    context limit is configured (model_context_limit <= 0); otherwise it is the
    smaller of cfg.max_tokens and (context window - estimated input - headroom),
    which keeps input+output within the model context and avoids gateway errors.

    Floored at _MIN_OUTPUT_TOKENS and never returned above cfg.max_tokens, so the
    value stays sane even for degenerate contexts while never exceeding the cap.
    """
    max_tokens = int(getattr(cfg, "max_tokens", 0))
    context_limit = int(getattr(cfg, "model_context_limit", 0))
    if context_limit > 0:
        available = context_limit - TokenBudget.estimate_tokens(messages) - _OUTPUT_HEADROOM
        ceiling = min(max_tokens, available)
    else:
        ceiling = max_tokens
    return max(_MIN_OUTPUT_TOKENS, min(ceiling, max_tokens))


class KiloError(Exception):
    """Error raised for any failed Kilo gateway call; never contains the api key."""

    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.message = message
        self.status = status


class KiloClient:
    def __init__(self, cfg: BridgeConfig):
        self._cfg = cfg
        base = (getattr(cfg, "kilo_base_url", "") or "").strip().rstrip("/")
        self._base_url = base or _FALLBACK_BASE_URL

    def _headers(self, with_content_type: bool) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "User-Agent": _USER_AGENT,
        }
        if with_content_type:
            headers["Content-Type"] = "application/json"
        api_key = (getattr(self._cfg, "kilo_api_key", "") or "").strip()
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        return headers

    def _redact(self, text: str) -> str:
        api_key = (getattr(self._cfg, "kilo_api_key", "") or "").strip()
        if api_key and api_key in text:
            text = text.replace(api_key, "[REDACTED]")
        return text

    @staticmethod
    def _read_body(response) -> str:
        try:
            raw = response.read()
        except Exception:
            return ""
        if isinstance(raw, bytes):
            return raw.decode("utf-8", "replace")
        return str(raw)

    def _request(self, method: str, url: str, payload: Optional[dict] = None) -> Any:
        data = None
        if payload is not None:
            try:
                data = json.dumps(payload).encode("utf-8")
            except (TypeError, ValueError) as exc:
                raise KiloError(f"invalid request payload: {exc}") from None
        request = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers=self._headers(data is not None),
        )
        try:
            with urllib.request.urlopen(request, timeout=self._cfg.request_timeout_seconds) as response:
                status = getattr(response, "status", None)
                if status is None:
                    try:
                        status = response.getcode()
                    except Exception:
                        status = None
                body = self._read_body(response)
        except urllib.error.HTTPError as exc:
            body = self._read_body(exc)[:500]
            code = getattr(exc, "code", None)
            raise KiloError(self._redact(f"HTTP {code}: {body}"), status=code) from None
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
            raise KiloError(self._redact(f"network error: {exc}")) from None

        if status is not None and not (200 <= int(status) < 300):
            raise KiloError(self._redact(f"HTTP {status}: {body[:500]}"), status=status)
        try:
            return json.loads(body)
        except ValueError:
            raise KiloError("malformed response", status=200) from None

    def chat_completion(self, messages: list[dict]) -> dict:
        """POST a chat completion request and return the parsed gateway response."""
        max_tokens = compute_max_tokens(self._cfg, messages)
        _LOGGER.debug("effective max_tokens=%d", max_tokens)
        payload = {
            "model": self._cfg.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": self._cfg.temperature,
        }
        data = self._request("POST", f"{self._base_url}/chat/completions", payload)
        if not isinstance(data, dict):
            raise KiloError("malformed response", status=200)
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise KiloError("malformed response", status=200)
        message = choices[0].get("message")
        if not isinstance(message, dict) or not isinstance(message.get("content"), str):
            raise KiloError("malformed response", status=200)
        return data

    def models(self) -> dict:
        """GET the gateway model list (diagnostics)."""
        return self._request("GET", f"{self._base_url}/models")
