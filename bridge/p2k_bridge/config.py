"""Configuration loading for the Player2 -> Kilo bridge."""

import copy
import json
import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Optional

DEFAULT_CHARACTER: dict[str, Any] = {
    "id": "kilo",
    "name": "Kilo",
    "short_name": "KILO",
    "greeting": "Hey there! I'm your companion on this server. What are we up to today?",
    "description": "You are a helpful, concise Minecraft companion AI agent.",
    "voice_ids": [],
    "meta": {"skin_url": ""},
    # Optional per-character model overrides. Empty/zero/None = inherit the global
    # values in bridge/config.json. There is deliberately no
    # `model_context_limit` key: the context window is read from the gateway
    # catalog (p2k_bridge.catalog), because a hand-entered value that does not
    # match the model would silently truncate replies.
    "model": "",
    "temperature": None,
    "max_tokens": 0,
    "stt_model": "",
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
    # Tokens held back from companion-initiated calls (NPC-to-NPC chatter, spawn
    # greetings, background memory extraction) so they cannot drain the burst and
    # shed a player's command. Player-driven kinds (conversation,
    # command_feedback, stt) may always use any remaining token. The total
    # requests-per-hour is unchanged; only who is shed changes. 0 disables it.
    reserve_tokens_for_players: int = 2
    characters: list[dict] = field(default_factory=lambda: [copy.deepcopy(DEFAULT_CHARACTER)])

    # Speech-to-text (voice input for NPCs). The endpoint is model-agnostic; the
    # default model is the cheapest purpose-built audio model on the gateway and
    # needs a paid Kilo key. There is no working free audio model today — the two
    # the catalog advertises either report audio_tokens=0 (audio never ingested)
    # or have no audio endpoint at all — so on the free tier every call degrades
    # to {"text": ""} and the NPC simply ignores the utterance. See docs/SETUP.md.
    stt_enabled: bool = True
    stt_model: str = "mistralai/voxtral-small-24b-2507"
    stt_temperature: float = 0.0
    stt_max_seconds: float = 20.0
    stt_prompt: str = (
        "Transcribe the following speech exactly. Reply with ONLY the words that "
        "were spoken -- no commentary, no summary, no quotation marks, and no "
        "apology if the audio is unclear."
    )
    routing_rules: list[dict] = field(default_factory=list)

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
_CHARACTER_OVERRIDE_KEYS = ("model", "stt_model", "temperature", "max_tokens")
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

    stt_model = normalized.get("stt_model")
    if not isinstance(stt_model, str) or not stt_model.strip():
        normalized.pop("stt_model", None)
    else:
        normalized["stt_model"] = stt_model.strip()

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

    cfg.kilo_api_key = _resolve_api_key(cfg, config_path)
    return cfg


_KEY_PLACEHOLDER = "paste-your-kilocode-api-key-here"


def is_placeholder_key(value: Any) -> bool:
    """True when an API key is absent or is still the shipped placeholder.

    ``bootstrap.sh`` recreates ``.env`` from ``.env.example`` after an
    environment wipe, so the placeholder is the *normal* state right after a
    restore. Treating it as a real key previously meant every chat call was sent
    with it, the gateway answered ``401 INVALID_TOKEN`` on each one, and every
    NPC silently degraded to the idle envelope — a bridge that looks healthy and
    does nothing. Ignoring the placeholder lets the bridge use the anonymous
    free tier instead.
    """
    text = value.strip() if isinstance(value, str) else ""
    if not text:
        return True
    if text == _KEY_PLACEHOLDER:
        return True
    lowered = text.lower()
    return lowered.startswith("paste-your") or "your-kilocode" in lowered


def _resolve_api_key(cfg: BridgeConfig, config_path: Optional[str]) -> str:
    """First usable key in precedence order, skipping placeholders.

    Order is: the JSON value, the environment variable named by
    ``kilo_api_key_env``, then the ``.env`` files. A placeholder at a higher
    precedence does not shadow a real key lower down, and a placeholder at every
    level resolves to "" (free tier) rather than to the placeholder itself.
    """
    env_name = (cfg.kilo_api_key_env or "KILO_API_KEY").strip() or "KILO_API_KEY"
    candidates = (
        cfg.kilo_api_key,
        os.environ.get(env_name) or "",
        _dotenv_value(config_path, env_name) or "",
    )
    for candidate in candidates:
        if not is_placeholder_key(candidate):
            return candidate.strip()
    return ""
