"""Classify a chat request and resolve which model should answer it.

Two questions are answered here, both pure functions of the request:

1. **What kind of call is this?** The mod issues five distinct kinds of chat
   request during a normal turn. They are all ``POST /v1/chat/completions`` with a
   conversation-shaped ``messages`` array, so the only signal is the payload text:

   ========================  =========================================
   kind                     signal in the last user message
   ========================  =========================================
   ``conversation``         ``User Message: [player]: text``
   ``command_feedback``     ``Info: Command feedback: @cmd n done.``
   ``npc_dialogue``         ``Other AI Message: [npc]: text``
   ``greeting``             ``Info: <roster greeting>`` (first message)
   ``event``                ``Info: ...`` (e.g. respawn)
   ``memory_extraction``    ``Extract the durable memory graph ...``
   ``unknown``              none of the above
   ========================  =========================================

   Every rule and its evidence is documented inline; the classifications were
   derived from payloads actually captured from the mods, not guessed.
   ``npc_dialogue`` is the newest of them: it only appeared once a second
   character was spawned and the two companions started talking to each other,
   which is a good reminder that the taxonomy grows with the roster.

2. **Which character is asking?** The mod writes the character's roster entry
   into the system prompt:

       You take the personality of the following character:
       Your character's name is Kilo.
       You are a helpful, concise Minecraft companion AI agent.

   so the character is identified by looking for its roster ``name`` in the
   system messages. Only ``role == "system"`` messages are searched: the same
   phrasing can appear inside a user message and would mis-identify the speaker.

Nothing here performs I/O or logs, so it is fully unit-testable.
"""

import json
from dataclasses import dataclass, field
from typing import Any, Optional

from .catalog import ModelCatalog

# --- call kinds ---------------------------------------------------------------

CONVERSATION = "conversation"
COMMAND_FEEDBACK = "command_feedback"
GREETING = "greeting"
EVENT = "event"
NPC_DIALOGUE = "npc_dialogue"
MEMORY_EXTRACTION = "memory_extraction"
UNKNOWN = "unknown"

ALL_KINDS = (
    CONVERSATION,
    COMMAND_FEEDBACK,
    GREETING,
    EVENT,
    NPC_DIALOGUE,
    MEMORY_EXTRACTION,
    UNKNOWN,
)

# The mod's markers, verified against captured traffic. ``Info: `` is the mod's
# prefix for every system-generated message, which is why the more specific tests
# must run before the bare ``Info: `` catch-all.
_MEMORY_MARKER = "Extract the durable memory graph from the conversation turns"
_COMMAND_FEEDBACK_PREFIX = "Info: Command feedback:"
_NPC_DIALOGUE_PREFIX = "Other AI Message: "
_INFO_PREFIX = "Info: "
_USER_MESSAGE_PREFIX = "User Message: "
_NAME_TEMPLATE = "Your character's name is "

# A description must be at least this long before it is used as a bare match:
# shorter ones are too generic to distinguish two characters.
_MIN_DESCRIPTION_LENGTH = 20


# --- per-character override keys ---------------------------------------------

# These are the keys honoured on a roster entry. Anything else in a character
# dict is the roster's own business (greeting, description, voice_ids, meta).
OVERRIDE_KEYS = ("model", "stt_model", "temperature", "max_tokens")


@dataclass
class Route:
    """Everything the gateway call needs, resolved for one request."""

    kind: str = UNKNOWN
    character_id: str = ""
    model: str = ""
    stt_model: str = ""
    temperature: float = 0.0
    max_tokens: int = 0
    context_limit: int = 0
    max_completion_tokens: int = 0
    supports_temperature: bool = True
    overridden_fields: tuple = ()

    @property
    def label(self) -> str:
        return self.kind


# --- payload extraction -------------------------------------------------------


def _last_user_content(messages: Any) -> str:
    if not isinstance(messages, list):
        return ""
    for message in reversed(messages):
        if isinstance(message, dict) and message.get("role") == "user":
            content = message.get("content")
            return content if isinstance(content, str) else ""
    return ""


def extract_payload_text(messages: Any) -> str:
    """Reduce the last user message to the plain text the mod actually wrote.

    The mod uses two content shapes and they do **not** correlate with the call
    kind — bare text (an ``event``/memory call) and a JSON object wrapping the
    same fields (``userMessage``, ``worldStatus``, ``agentStatus``, ...) for
    conversational calls. Both must land in one plain-text space before any
    prefix test, so callers never assume "wrapped means conversational".
    """
    content = _last_user_content(messages)
    if not content or not content.strip():
        return ""
    text = content.strip()
    parsed = _try_json(text)
    if isinstance(parsed, dict):
        inner = parsed.get("userMessage")
        if isinstance(inner, str) and inner.strip():
            return inner.strip()
    return text


def _try_json(text: str) -> Any:
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return None


# --- classification -----------------------------------------------------------


def classify_call(messages: Any, characters: Any = None) -> str:
    """Return the call kind for ``messages``; never raises.

    ``unknown`` (the safe answer) is returned for anything unparseable, which
    maps to the global route exactly as every request did before this module.
    """
    text = extract_payload_text(messages)
    if not text:
        return UNKNOWN
    if _MEMORY_MARKER in text:
        return MEMORY_EXTRACTION
    if text.startswith(_COMMAND_FEEDBACK_PREFIX):
        return COMMAND_FEEDBACK
    if text.startswith(_NPC_DIALOGUE_PREFIX):
        # NPC-to-NPC chatter: one companion quoting another's message, seen in the
        # live two-character rig run (logged as "Other AI Message: [KILO]: ...").
        return NPC_DIALOGUE
    if text.startswith(_INFO_PREFIX):
        # ``Info: `` covers the first-message greeting, respawn and other system
        # events. The greeting is the text of the roster entry it came from, so
        # match against that rather than mod-internal wording.
        remainder = text[len(_INFO_PREFIX):]
        for character in characters or []:
            greeting = _text_field(character, "greeting")
            if greeting and remainder.startswith(greeting):
                return GREETING
        return EVENT
    if text.startswith(_USER_MESSAGE_PREFIX):
        return CONVERSATION
    return UNKNOWN


# --- character identification ------------------------------------------------


def _text_field(entry: Any, key: str) -> str:
    if not isinstance(entry, dict):
        return ""
    value = entry.get(key)
    if not isinstance(value, str):
        return ""
    return value.strip()


def identify_character(messages: Any, characters: Any) -> Optional[dict]:
    """Find the roster entry that wrote this request's system prompt.

    Resolution order is by decreasing signal strength, and the fallback is
    deliberately last: an explicit ``name`` match, then ``short_name``, then the
    ``description`` (a useful secondary if the mod ever drops the name line),
    then the trivial single-character roster. ``None`` means "use the global
    route".

    Only ``role == "system"`` messages are searched — the character name also
    appears in ordinary conversation, and matching there would mis-attribute the
    call. Later system messages win over earlier ones.
    """
    if not isinstance(characters, list):
        return None
    roster = [c for c in characters if isinstance(c, dict)]
    if not roster:
        return None

    for system_text in reversed(_system_texts(messages)):
        for character in roster:
            if _matches_templated(character, "name", system_text) or _matches_templated(
                character, "short_name", system_text
            ):
                return character
        for character in roster:
            # The description follows the name line in the mod's template, so a
            # bare match is only a secondary signal — and only for descriptions
            # long enough to be distinctive, to avoid matching a generic one.
            description = _text_field(character, "description")
            if len(description) >= _MIN_DESCRIPTION_LENGTH and description in system_text:
                return character

    # With one character in the roster every call is that character's, so apply
    # its overrides rather than silently using the global settings.
    if len(roster) == 1:
        return roster[0]
    return None


def _matches_templated(character: dict, key: str, system_text: str) -> bool:
    value = _text_field(character, key)
    return bool(value) and f"Your character's name is {value}." in system_text


def _system_texts(messages: Any) -> list[str]:
    if not isinstance(messages, list):
        return []
    parts = []
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "system":
            continue
        content = message.get("content")
        if isinstance(content, str):
            parts.append(content)
    return parts


# --- route resolution --------------------------------------------------------


def _clean_temperature(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        if 0.0 <= number <= 2.0:
            return number
    return None


def _clean_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int) and value > 0:
        return value
    return 0


def character_overrides(character: Optional[dict]) -> dict:
    """Validated per-character overrides, with anything unusable dropped.

    Deliberately forgiving: a mistyped ``temperature`` never breaks the bridge,
    it just means that character answers with the global temperature.
    """
    if not isinstance(character, dict):
        return {}
    overrides: dict[str, Any] = {}

    model = _text_field(character, "model")
    if model:
        overrides["model"] = model

    stt_model = _text_field(character, "stt_model")
    if stt_model:
        overrides["stt_model"] = stt_model

    temperature = _clean_temperature(character.get("temperature"))
    if temperature is not None:
        overrides["temperature"] = temperature

    max_tokens = _clean_int(character.get("max_tokens"))
    if max_tokens:
        overrides["max_tokens"] = max_tokens

    return overrides


def resolve_route(
    messages: Any,
    characters: Any,
    cfg: Any,
    catalog: Optional[ModelCatalog] = None,
) -> Route:
    """Resolve model/temperature/max_tokens for one chat request.

    Precedence is **per-character override → global config**. The context window
    comes from the catalog for the resolved model (a global limit cannot be right
    once more than one model is in play), falling back to the configured value
    when the catalog has no entry.
    """
    kind = classify_call(messages, characters if isinstance(characters, list) else None)
    character = identify_character(messages, characters if isinstance(characters, list) else None)
    overrides = character_overrides(character)

    character_id = _text_field(character, "id") if character else ""
    model = overrides.get("model") or getattr(cfg, "model", "") or ""
    stt_model = overrides.get("stt_model") or getattr(cfg, "stt_model", "") or ""
    temperature = overrides.get("temperature")
    if temperature is None:
        temperature = _clean_temperature(getattr(cfg, "temperature", None))
    if temperature is None:
        temperature = 0.0
    max_tokens = overrides.get("max_tokens") or _clean_int(getattr(cfg, "max_tokens", 0))

    context_limit = cfg.model_context_limit if catalog is None else catalog.context_limit(
        model, cfg.model_context_limit
    )
    max_completion_tokens = cfg.max_tokens if catalog is None else catalog.max_completion_tokens(
        model, cfg.max_tokens
    )
    supports_temperature = (
        True
        if catalog is None
        else catalog.supports_temperature(model)
    )

    return Route(
        kind=kind,
        character_id=character_id,
        model=model,
        stt_model=stt_model,
        temperature=float(temperature),
        max_tokens=int(max_tokens),
        context_limit=int(context_limit),
        max_completion_tokens=int(max_completion_tokens),
        supports_temperature=supports_temperature,
        overridden_fields=tuple(sorted(overrides.keys())),
    )
