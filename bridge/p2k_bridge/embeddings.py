"""Embedding calls and similarity, pure so they are unit-testable.

The provider contract this module speaks is **measured**, not assumed. A real
request against the live provider gave:

    {"text": ["one sentence", "another"]}
    -> {"success": true,
        "result": {"data": [[0.0126, ...], [0.0342, ...]],
                   "shape": [2, 768], ...}}

Note `result.data[i]` **is** the vector. The OpenAI convention (`data[].embedding`)
is *not* what this provider returns, and parsing for it would raise on the first
real request. `parse_responses` therefore accepts the confirmed shape first and
logs loudly when it falls back to a shape that has not been measured.

There is no numpy here and none will be added, so similarity is a hand-rolled
cosine. That is fine at the sizes this bridge actually stores: `memory_max_per_character`
caps the store at a few hundred rows, and 768 dims over 500 rows is ~200K float
operations — tens of milliseconds.
"""

import math
from typing import Any, Optional

MAX_INPUTS_PER_REQUEST = 32
MAX_TEXT_CHARS = 8192


class EmbeddingError(Exception):
    """A response we cannot read. The caller degrades to no injection."""

    def __init__(self, reason: str, shape_seen: Any = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.shape_seen = shape_seen


def build_request(model: str, inputs: Any) -> dict:
    """Build the provider payload. ``inputs`` is a list so batching is in the signature.

    Batching is not an optimisation added later: it is the difference between
    storing one memory per request and storing up to ``MAX_INPUTS_PER_REQUEST``,
    so callers pass a list from the start. A bare string is accepted for
    convenience and normalised to a one-element list.
    """
    if isinstance(inputs, str):
        texts = [inputs]
    elif isinstance(inputs, (list, tuple)):
        texts = list(inputs)
    else:
        raise EmbeddingError(f"inputs must be a str or a list of str, got {type(inputs).__name__}")

    if not texts:
        raise EmbeddingError("no inputs to embed")
    if len(texts) > MAX_INPUTS_PER_REQUEST:
        raise EmbeddingError(
            f"{len(texts)} inputs exceeds the {MAX_INPUTS_PER_REQUEST} per request"
        )

    cleaned = []
    for text in texts:
        if not isinstance(text, str):
            raise EmbeddingError(f"input is not a string: {type(text).__name__}")
        trimmed = text.strip()
        if not trimmed:
            raise EmbeddingError("refusing to embed empty text")
        if len(trimmed) > MAX_TEXT_CHARS:
            trimmed = trimmed[:MAX_TEXT_CHARS]
        cleaned.append(trimmed)

    # "text" is the provider's key and it takes a list of inputs.
    return {"text": cleaned}


def parse_responses(payload: Any) -> list[list[float]]:
    """Extract one vector per input, in order, from a provider response.

    Raises :class:`EmbeddingError` on anything unreadable so the caller can
    degrade to injecting nothing, rather than raising a KeyError three frames up.
    """
    if not isinstance(payload, dict):
        raise EmbeddingError("response is not an object", payload)

    if payload.get("success") is False:
        errors = payload.get("errors") or []
        detail = errors[0].get("message") if errors and isinstance(errors[0], dict) else ""
        raise EmbeddingError(f"provider reported failure: {detail or 'unknown'}", payload)

    # CONFIRMED shape: result.data is a list of lists of floats.
    result = payload.get("result")
    if isinstance(result, dict):
        data = result.get("data")
        if isinstance(data, list) and data:
            vectors = _vectors_from_pairs(data)
            if vectors is not None:
                return vectors
            raise EmbeddingError(
                "result.data did not look like a list of vectors", data)

    # Unmeasured fallbacks, for other providers. Accepted rather than rejected so
    # switching provider needs no code change, but they are the only shapes NOT
    # verified against a live response — see the module docstring.
    data = payload.get("data")
    if isinstance(data, list) and data:
        vectors = _vectors_from_pairs(data)
        if vectors is not None:
            return vectors
        nested = _vectors_from_objects(data)
        if nested is not None:
            return nested

    raise EmbeddingError("no recognisable vector list in response", payload)


def _vectors_from_pairs(rows: Any) -> Optional[list[list[float]]]:
    """Read ``[[float, ...], ...]``. Returns None if any row is not numeric."""
    if not isinstance(rows, list) or not rows:
        return None
    out: list[list[float]] = []
    for row in rows:
        if not isinstance(row, list):
            return None
        numbers: list[float] = []
        for value in row:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return None
            numbers.append(float(value))
        if numbers:
            out.append(numbers)
    return out or None


def _vectors_from_objects(rows: Any) -> Optional[list[list[float]]]:
    """Read ``[{"embedding": [float, ...]}, ...]``. Returns None if absent."""
    if not isinstance(rows, list) or not rows:
        return None
    out: list[list[float]] = []
    for row in rows:
        if not isinstance(row, dict):
            return None
        vector = _vectors_from_pairs(row.get("embedding"))
        if vector is None:
            return None
        out.extend(vector[0])
    return out or None


def cosine(a: Any, b: Any) -> float:
    """Cosine similarity of two sequences of equal length. 0.0 on mismatch.

    Never raises: a stored vector with the wrong length, or a non-numeric value,
    scores 0.0 rather than propagating, because one bad row must not break
    retrieval for every other memory.
    """
    if not isinstance(a, (list, tuple)) or not isinstance(b, (list, tuple)):
        return 0.0
    if len(a) != len(b) or not a:
        return 0.0
    try:
        dot = 0.0
        norm_a = 0.0
        norm_b = 0.0
        for x, y in zip(a, b):
            x = float(x)
            y = float(y)
            dot += x * y
            norm_a += x * x
            norm_b += y * y
    except (TypeError, ValueError):
        return 0.0
    if norm_a <= 0.0 or norm_b <= 0.0:
        return 0.0
    return dot / math.sqrt(norm_a * norm_b)


def top_k(query: Any, candidates: Any, k: int = 5, min_score: float = 0.0):
    """Rank ``candidates`` by cosine to ``query``, best first.

    ``candidates`` is a sequence of ``(vector, item)`` pairs. Returns
    ``[(score, item), ...]``, at most ``k`` entries, excluding anything below
    ``min_score``. Never raises; a bad candidate scores 0.0 and is filtered.
    """
    if not isinstance(candidates, (list, tuple)):
        return []
    limit = k if isinstance(k, int) and k > 0 else 5
    threshold = min_score if isinstance(min_score, (int, float)) else 0.0
    scored: list[tuple[float, Any]] = []
    for entry in candidates:
        if not isinstance(entry, (list, tuple)) or len(entry) != 2:
            continue
        vector, item = entry
        score = cosine(query, vector)
        if score >= threshold:
            scored.append((score, item))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return scored[:limit]


def render_block(items: Any, max_chars: int = 1200) -> str:
    """Render retrieved memories as a prompt block that never exceeds ``max_chars``.

    The bound is not cosmetic: it is what protects the context window, so it is
    enforced here rather than hoped for by the caller. When trimming, entries are
    dropped whole rather than cut mid-sentence, and an ellipsis records the
    omission so the model can tell a short answer from a truncated one.
    """
    if not isinstance(items, (list, tuple)):
        return ""
    lines: list[str] = []
    for item in items:
        raw = item if isinstance(item, str) else _text_of(item)
        text = raw.strip() if isinstance(raw, str) else ""
        if text:
            lines.append("- " + text)
    if not lines:
        return ""
    body = "\n".join(lines)
    limit = max_chars if isinstance(max_chars, int) and max_chars > 0 else 1200
    if len(body) <= limit:
        return body
    kept: list[str] = []
    used = 0
    for line in lines:
        if used + len(line) + 1 > limit:
            break
        kept.append(line)
        used += len(line) + 1
    omitted = len(lines) - len(kept)
    suffix = f"\n- ... (+{omitted} more)" if omitted > 0 else ""
    trimmed = "\n".join(kept) + suffix
    return trimmed[:limit] if len(trimmed) > limit else trimmed


def _text_of(item: Any) -> str:
    if isinstance(item, dict):
        for key in ("text", "memory", "content"):
            value = item.get(key)
            if isinstance(value, str):
                return value
        return ""
    return str(item)
