"""Configuration loading for the Player2 -> Kilo bridge."""

import copy
import json
import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

DEFAULT_CHARACTER: dict[str, Any] = {
    "id": "kilo",
    "name": "Kilo",
    "short_name": "KILO",
    "greeting": "Hey there! I'm your companion on this server. What are we up to today?",
    "description": "You are a helpful, concise Minecraft companion AI agent.",
    "voice_ids": [],
    "meta": {"skin_url": ""},
    # Optional per-character model overrides. Empty/zero = inherit the global
    # values in bridge/config.json. There is deliberately no
    # `model_context_limit` key: the context window is read from the gateway
    # catalog (p2k_bridge.catalog), because a hand-entered value that does not
    # match the model would silently truncate replies.
    "model": "",
    "temperature": None,
    "max_tokens": 0,
}

DEFAULT_CHARACTERS: list[dict] = [copy.deepcopy(DEFAULT_CHARACTER)]

_TRUE_STRINGS = {"1", "true", "yes", "on", "y", "t"}
_FALSE_STRINGS = {"0", "false", "no", "off", "n", "f"}


@dataclass
class BridgeConfig:
    listen_host: str = "127.0.0.1"
    http_port: int = 9800
    local_login_port: int = 4315
    tls_enabled: bool = True
    tls_port: int = 443
    tls_cert: str = "tls/certs/server.crt"
    tls_key: str = "tls/certs/server.key"
    kilo_base_url: str = "https://api.kilo.ai/api/gateway"
    kilo_api_key: str = ""
    kilo_api_key_env: str = "KILO_API_KEY"
    model: str = "nvidia/nemotron-3-super-120b-a12b:free"
    max_tokens: int = 500000
    temperature: float = 0.5
    request_timeout_seconds: float = 90.0
    requests_per_hour: int = 180
    token_budget_per_window: int = 0
    window_seconds: float = 3600.0
    model_context_limit: int = 262144
    game_key: str = "player2-ai-npc-minecraft"
    characters: list[dict] = field(default_factory=lambda: [copy.deepcopy(DEFAULT_CHARACTER)])

    def __repr__(self) -> str:
        parts = []
        for f in fields(self):
            value = getattr(self, f.name)
            if f.name == "kilo_api_key":
                value = "***" if value else ""
            parts.append(f"{f.name}={value!r}")
        return "BridgeConfig(" + ", ".join(parts) + ")"


def _coerce_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in _TRUE_STRINGS:
            return True
        if lowered in _FALSE_STRINGS:
            return False
    return None


def _coerce_characters(value: Any) -> list[dict] | None:
    if isinstance(value, dict):
        return [dict(value)]
    if isinstance(value, list):
        return [_normalize_character(item) for item in value if isinstance(item, dict)]
    return None


# Per-character keys the bridge acts on. Anything else in a roster entry
# (greeting, description, voice_ids, meta, ...) is passed through untouched — it
# belongs to the mod, not to us.
_CHARACTER_OVERRIDE_KEYS = ("model", "temperature", "max_tokens")
_MAX_TEMPERATURE = 2.0


def _normalize_character(entry: dict) -> dict:
    """Validate one roster entry's override keys in place.

    Malformed values are dropped rather than raising: a mistyped ``temperature``
    must not stop the bridge (or the rest of the roster) from loading, it just
    means that character answers with the global setting.
    """
    normalized = dict(entry)

    model = normalized.get("model")
    if not isinstance(model, str) or not model.strip():
        normalized.pop("model", None)
    else:
        normalized["model"] = model.strip()

    temperature = normalized.get("temperature")
    if isinstance(temperature, bool) or not isinstance(temperature, (int, float)):
        normalized.pop("temperature", None)
    else:
        number = float(temperature)
        if not 0.0 <= number <= _MAX_TEMPERATURE:
            normalized.pop("temperature", None)
        else:
            normalized["temperature"] = number

    max_tokens = normalized.get("max_tokens")
    if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or max_tokens <= 0:
        normalized.pop("max_tokens", None)

    return normalized


def _field_types() -> dict[str, type]:
    template = BridgeConfig()
    return {f.name: type(getattr(template, f.name)) for f in fields(BridgeConfig)}


def _coerce_value(name: str, value: Any) -> Any:
    if name == "characters":
        coerced = _coerce_characters(value)
        return coerced if coerced is not None else value
    if isinstance(value, bool):
        return value
    target = _field_types().get(name)
    if isinstance(value, str) and target is bool:
        coerced_bool = _coerce_bool(value)
        return coerced_bool if coerced_bool is not None else value
    if isinstance(value, str) and target is int:
        text = value.strip()
        try:
            return int(text)
        except ValueError:
            try:
                return int(float(text))
            except ValueError:
                return value
    if isinstance(value, str) and target is float:
        try:
            return float(value.strip())
        except ValueError:
            return value
    if isinstance(value, (int, float)) and target is str:
        return str(value)
    return value


def _parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return values
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.lower().startswith("export "):
            key = key[7:].strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def _env_file_candidates(config_path: Path) -> list[Path]:
    """Return the .env paths to try, in order, without duplicates.

    Order matches the documented precedence: the .env sitting next to the
    config file first, then the .env in the current working directory, then
    the .env at the repository root (the parent of the bridge/ directory).
    """
    candidates = [
        config_path.parent / ".env",
        Path.cwd() / ".env",
        Path(__file__).resolve().parents[2] / ".env",
    ]
    ordered: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            resolved = candidate
        if resolved in seen:
            continue
        seen.add(resolved)
        ordered.append(candidate)
    return ordered


def _dotenv_value(config_path: Path, env_name: str) -> str:
    """First non-empty value for env_name across the candidate .env files."""
    for candidate in _env_file_candidates(config_path):
        values = _parse_env_file(candidate)
        value = (values.get(env_name) or "").strip()
        if value:
            return value
    return ""


def load_config(path: str | None = None) -> BridgeConfig:
    """Load a BridgeConfig from JSON, filling missing fields with defaults."""
    raw_path = path if path not in (None, "") else (os.environ.get("P2K_CONFIG") or "config.json")
    config_path = Path(os.fspath(raw_path))

    data: dict[str, Any] = {}
    try:
        parsed = json.loads(config_path.read_text(encoding="utf-8"))
        if isinstance(parsed, dict):
            data = parsed
    except (OSError, ValueError, UnicodeDecodeError):
        data = {}

    kwargs: dict[str, Any] = {}
    for f in fields(BridgeConfig):
        if f.name not in data:
            continue
        value = data[f.name]
        if value is None:
            continue
        kwargs[f.name] = _coerce_value(f.name, value)

    cfg = BridgeConfig(**kwargs)

    if not cfg.kilo_api_key.strip():
        env_name = (cfg.kilo_api_key_env or "KILO_API_KEY").strip() or "KILO_API_KEY"
        env_value = (os.environ.get(env_name) or "").strip()
        if env_value:
            cfg.kilo_api_key = env_value
        else:
            file_value = _dotenv_value(config_path, env_name)
            if file_value:
                cfg.kilo_api_key = file_value

    return cfg
