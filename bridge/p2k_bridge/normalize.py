"""Normalize LLM output into the Player2 NPC response contract."""

import json
import re
import time
from typing import Optional

_FENCE_RE = re.compile(r"```[ \t]*[A-Za-z0-9_+.\-]*")
_MODEL_NAME = "p2k-bridge"
_MAX_MESSAGE = 250
_MAX_COMMAND = 300


def set_model(name: str) -> None:
    """Inject the configured model name used in OpenAI-style envelopes."""
    global _MODEL_NAME
    _MODEL_NAME = str(name)


def _strip_code_fences(text: str) -> str:
    if "```" not in text:
        return text
    return _FENCE_RE.sub(" ", text)


def _iter_balanced_objects(text: str):
    index = 0
    length = len(text)
    while True:
        start = text.find("{", index)
        if start == -1:
            return
        depth = 0
        in_string = False
        escaped = False
        end = -1
        for position in range(start, length):
            char = text[position]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    end = position
                    break
        if end == -1:
            index = start + 1
            continue
        yield text[start : end + 1]
        index = end + 1


def _first_json_object(text: str) -> dict | None:
    for candidate in _iter_balanced_objects(text):
        try:
            parsed = json.loads(candidate)
        except (ValueError, TypeError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _as_text(value) -> str:
    return value if isinstance(value, str) else ""


def _sanitize_command(value) -> str:
    if not isinstance(value, str):
        return ""
    command = value.strip().lstrip("@").strip()
    return command[:_MAX_COMMAND]


def normalize_llm_content(content: str) -> dict:
    """Coerce any model output into {"reason": str, "command": str, "message": str}."""
    text = _strip_code_fences(content if isinstance(content, str) else "").strip()
    obj = None
    if text:
        try:
            parsed = json.loads(text)
        except (ValueError, TypeError):
            parsed = None
        if isinstance(parsed, dict):
            obj = parsed
        else:
            obj = _first_json_object(text)
    if obj is None:
        return {"reason": "", "command": "", "message": text[:_MAX_MESSAGE]}
    return {
        "reason": _as_text(obj.get("reason")),
        "command": _sanitize_command(obj.get("command")),
        "message": _as_text(obj.get("message")).strip()[:_MAX_MESSAGE],
    }


def to_openai_envelope(obj: dict, model: Optional[str] = None) -> dict:
    """Wrap a normalized response object in an OpenAI chat.completion envelope.

    ``model`` reports the model that actually answered. It defaults to the
    configured global model, so per-character routing can report the truth
    without every caller having to pass it.
    """
    return {
        "id": "chatcmpl-p2k",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": (model or _MODEL_NAME),
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": json.dumps(obj, ensure_ascii=False),
                },
                "finish_reason": "stop",
            }
        ],
    }
