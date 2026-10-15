"""Provider client for embeddings. The only module here that does I/O.

Separate from ``embeddings.py`` so the parsing, similarity and rendering logic
stays pure and unit-testable, and so a provider failure has exactly one place to
be handled.

The request shape is the **measured** one from Cloudflare Workers AI:

    POST {base_url}/run/{model}      (base_url ends at .../ai)
    {"text": ["one sentence", "another"]}

which answers ``{"success": true, "result": {"data": [[float, ...], ...]}}``.
Both are recorded in ``embeddings.parse_responses``.
"""

import json
import logging
import urllib.error
import urllib.request
from typing import Any, Optional

from .embeddings import MAX_INPUTS_PER_REQUEST, EmbeddingError, build_request, parse_responses

_LOGGER = logging.getLogger(__name__)


class EmbeddingClient:
    """A thin REST client for whatever provider ``base_url`` points at."""

    def __init__(self, base_url: str, api_key: str, model: str, timeout: float = 30.0):
        self._model = model if isinstance(model, str) and model.strip() else ""
        url = (base_url or "").strip()
        self._url = url.rstrip("/") if url else ""
        self._key = api_key if isinstance(api_key, str) else ""
        try:
            self._timeout = float(timeout) if timeout and timeout > 0 else 30.0
        except (TypeError, ValueError):
            self._timeout = 30.0

    @property
    def configured(self) -> bool:
        return bool(self._url and self._model)

    def _headers(self, content_length: int) -> dict:
        headers = {"Content-Type": "application/json", "Accept": "application/json",
                   "Content-Length": str(content_length)}
        # The token is deliberately not logged anywhere.
        if self._key:
            headers["Authorization"] = f"Bearer {self._key}"
        return headers

    def embed(self, inputs: Any) -> list[list[float]]:
        """Embed one or many strings. Raises :class:`EmbeddingError` on failure.

        Batches internally up to the provider's safe limit, so a caller may hand
        over more texts than one request should carry.
        """
        if not self.configured:
            raise EmbeddingError("embedding client is not configured")

        texts = [inputs] if isinstance(inputs, str) else list(inputs or [])
        if not texts:
            raise EmbeddingError("no inputs to embed")

        out: list[list[float]] = []
        for start in range(0, len(texts), MAX_INPUTS_PER_REQUEST):
            out.extend(self._embed_batch(texts[start:start + MAX_INPUTS_PER_REQUEST]))
        return out

    def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        payload = build_request(self._model, texts)
        url = f"{self._url}/run/{self._model}"
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(url, data=body, method="POST",
                                         headers=self._headers(len(body)))
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                raw = response.read().decode("utf-8", "replace")
                status = getattr(response, "status", 200)
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:200]
            except Exception:
                pass
            raise EmbeddingError(f"embedding HTTP {exc.code}: {detail}") from exc
        except Exception as exc:  # URLError, timeout, socket
            raise EmbeddingError(f"embedding request failed: {type(exc).__name__} {exc}") from exc

        if status != 200:
            raise EmbeddingError(f"embedding HTTP {status}")
        try:
            parsed = json.loads(raw)
        except ValueError as exc:
            raise EmbeddingError("embedding response was not JSON") from exc

        vectors = parse_responses(parsed)
        if len(vectors) != len(texts):
            raise EmbeddingError(
                f"embedding returned {len(vectors)} vectors for {len(texts)} inputs")
        return vectors

    def embed_safe(self, inputs: Any) -> tuple[list[list[float]], Optional[str]]:
        """``embed`` that never raises: returns (vectors, reason-if-empty)."""
        try:
            return self.embed(inputs), None
        except EmbeddingError as exc:
            return [], str(exc)
        except Exception as exc:  # pragma: no cover - defensive
            return [], f"{type(exc).__name__}: {exc}"
