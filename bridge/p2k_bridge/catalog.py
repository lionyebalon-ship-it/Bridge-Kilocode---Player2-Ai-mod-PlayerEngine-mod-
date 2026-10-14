"""Gateway model-catalog lookup: per-model context windows and parameter support.

The Kilo Gateway's ``GET /models`` already publishes what the bridge needs to
route safely between models:

    "id": "nvidia/nemotron-3-super-120b-a12b:free",
    "context_length": 262144,
    "top_provider": {"context_length": 262144,
                     "max_completion_tokens": 235929},
    "supported_parameters": ["max_tokens", "temperature", ...],
    "isFree": true

Without this, the bridge assumed every model shares the single global
``model_context_limit``. That is fine while one model is pinned, but per-character
model overrides make it wrong in two ways:

* a model with a smaller window would be asked for more than it holds, and
* a model whose provider caps completions below its own context window (e.g.
  ``liquid/lfm-2.5-2.6b:free``: 65536 context but only 8192 completion tokens)
  would be asked for more completion tokens than it accepts.

So context limits are read from the catalog rather than hand-configured per
character. The catalog is optional at every step: an unknown or absent entry
falls back to config, and a fetch failure never blocks the bridge from serving.

This module is pure — no I/O, no logging — so it is unit-testable from fixtures.
"""

from dataclasses import dataclass
from typing import Any, Optional


@dataclass(frozen=True)
class ModelInfo:
    """What the catalog says about one model."""

    model_id: str
    context_limit: int = 0
    max_completion_tokens: int = 0
    supports_temperature: bool = True
    is_free: bool = False

    @property
    def known(self) -> bool:
        return bool(self.model_id)


class ModelCatalog:
    """``model_id -> ModelInfo`` with total, silent degradation.

    An empty catalog is a valid state (the gateway was unreachable, or the
    response shape changed). Every lookup then returns ``None`` and callers fall
    back to the configured values — behaviour identical to before this module
    existed.
    """

    def __init__(self, info: Optional[dict[str, ModelInfo]] = None) -> None:
        self._info = dict(info or {})

    @classmethod
    def from_payload(cls, payload: Any) -> "ModelCatalog":
        """Build from a parsed ``GET /models`` response.

        Accepts the gateway's shape (``{"data": [{...}]}``) and tolerates
        anything else by producing an empty catalog rather than raising.
        """
        if not isinstance(payload, dict):
            return cls()
        rows = payload.get("data")
        if not isinstance(rows, list):
            return cls()
        info: dict[str, ModelInfo] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            model_id = row.get("id")
            if not isinstance(model_id, str) or not model_id.strip():
                continue
            info[model_id.strip()] = ModelInfo(
                model_id=model_id.strip(),
                context_limit=_positive_int(row.get("context_length")),
                max_completion_tokens=_positive_int(
                    _nested(row, "top_provider", "max_completion_tokens")
                ),
                supports_temperature=_supports(row.get("supported_parameters")),
                is_free=bool(row.get("isFree")),
            )
        return cls(info)

    def get(self, model_id: Any) -> Optional[ModelInfo]:
        key = model_id if isinstance(model_id, str) else ""
        key = key.strip()
        if not key:
            return None
        return self._info.get(key)

    def context_limit(self, model_id: Any, default: int = 0) -> int:
        info = self.get(model_id)
        return info.context_limit if info and info.context_limit > 0 else int(default)

    def max_completion_tokens(self, model_id: Any, default: int = 0) -> int:
        info = self.get(model_id)
        return (
            info.max_completion_tokens
            if info and info.max_completion_tokens > 0
            else int(default)
        )

    def supports_temperature(self, model_id: Any) -> bool:
        info = self.get(model_id)
        # Unknown models are assumed to accept temperature: the bridge has always
        # sent it, and omitting it would silently change behaviour on models we
        # simply have no data for.
        return True if info is None else info.supports_temperature

    def __contains__(self, model_id: object) -> bool:
        return self.get(model_id) is not None

    def __len__(self) -> int:
        return len(self._info)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, ModelCatalog):
            return NotImplemented
        return self._info == other._info

    def __hash__(self) -> int:
        return hash(tuple(sorted(self._info.items())))

    def is_free(self, model_id: Any) -> Optional[bool]:
        info = self.get(model_id)
        return None if info is None else info.is_free


def _positive_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int) and value > 0:
        return value
    if isinstance(value, float) and value > 0:
        return int(value)
    return 0


def _nested(row: dict, *keys: str) -> Any:
    current: Any = row
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _supports(value: Any) -> bool:
    if not isinstance(value, list):
        return True
    return "temperature" in value
