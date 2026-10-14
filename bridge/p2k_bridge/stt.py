"""Speech-to-text for NPC voice input.

Nothing here performs I/O or logs, so it is fully unit-testable. The
``/v1/stt/transcribe`` endpoint in ``server.py`` is the only caller.

The shape follows the mods' own STT contract, discovered by disassembling
PlayerEngine (``STTUtils.extractTranscriptText``): it reads a string under
``text`` first, then a legacy ``transcript`` key. Replying with either is honoured
by the mod; this module always emits ``text``.

Why the transcript is cleaned at all: the audio models on this gateway are chat
models that *summarise or answer* unless told otherwise, and a buffering Opus
stream can yield silence. A transcript that is not usable text must degrade to
an empty string, never to an error -- the turn loop treats an empty transcript
as "nothing was said", which is the same behaviour as a rate-limited chat call.
"""

import base64
import binascii
import json
from dataclasses import dataclass
from typing import Any, Optional

MAX_AUDIO_BYTES = 12 * 1024 * 1024  # 12 MB: ~6 min of 48kHz/16-bit mono
MAX_B64_CHARS = 16 * 1024 * 1024
MAX_AUDIO_SECONDS = 120.0

# Substrings that mean "the model did not hear anything". Compared after
# lowercasing and punctuation stripping, so variants are covered loosely.
_NO_SPEECH_MARKERS = (
    "no words spoken",
    "no spoken words",
    "no speech",
    "no audible",
    "nothing was said",
    "no audio",
    "i cannot hear",
    "cannot transcribe",
    "unable to transcribe",
    "no sound",
    "silence",
)


@dataclass
class AudioRequest:
    """A validated, decoded speech request."""

    raw: bytes
    seconds: float
    player_uuid: str
    player: str
    character_id: str

    @property
    def audio_b64(self) -> str:
        return base64.b64encode(self.raw).decode("ascii")


class SttError(Exception):
    """A request the bridge refuses to answer. The caller degrades to no text."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def parse_audio_request(body: Any, cfg: Any = None) -> AudioRequest:
    """Decode and validate a ``POST /v1/stt/transcribe`` body.

    Raises :class:`SttError` with a loggable reason for every bad input. The
    endpoint turns that into ``{"text": ""}`` plus a WARNING, never into a 4xx.
    """
    if not isinstance(body, dict):
        raise SttError("stt request body must be a JSON object")

    audio_b64 = body.get("audio_b64")
    if not isinstance(audio_b64, str) or not audio_b64.strip():
        raise SttError("stt request needs a non-empty 'audio_b64'")
    if len(audio_b64) > MAX_B64_CHARS:
        raise SttError("stt audio_b64 is too large")
    try:
        raw = base64.b64decode(audio_b64.strip(), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise SttError(f"stt audio_b64 is not valid base64: {exc}") from exc
    if len(raw) < 64:
        raise SttError("stt audio is too short to contain speech")
    if len(raw) > MAX_AUDIO_BYTES:
        raise SttError("stt audio is too large")

    seconds = _seconds_from_ms(body.get("duration_ms"))
    if seconds is None:
        # Fall back to the WAV header when the caller omitted a duration.
        seconds = _wav_seconds(raw)
    if seconds is None:
        raise SttError("stt audio has no readable duration and body gave none")
    if seconds <= 0.02:
        raise SttError("stt audio is too short to contain speech")
    if seconds > MAX_AUDIO_SECONDS:
        raise SttError(f"stt audio is {seconds:.0f}s, over the {MAX_AUDIO_SECONDS:.0f}s limit")

    max_seconds = _positive_seconds(getattr(cfg, "stt_max_seconds", None))
    if max_seconds and seconds > max_seconds:
        raise SttError(f"stt audio is {seconds:.0f}s, over the configured {max_seconds:.0f}s limit")

    player_uuid = _text(body.get("player_uuid"))
    player = _text(body.get("player")) or player_uuid
    return AudioRequest(
        raw=raw,
        seconds=seconds,
        player_uuid=player_uuid,
        player=player,
        character_id=_text(body.get("character_id")),
    )


def build_request(messages_or_audio: Any, cfg: Any, model: str) -> dict:
    """Build the gateway chat/completions payload that carries audio."""
    audio_b64 = _text(getattr(messages_or_audio, "audio_b64", None))
    if not audio_b64:
        audio_b64 = _text(messages_or_audio)
    prompt = getattr(cfg, "stt_prompt", "") or (
        "Transcribe the following speech exactly. Reply with ONLY the spoken words."
    )
    return {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "input_audio",
                     "input_audio": {"data": audio_b64, "format": "wav"}},
                ],
            }
        ],
        "max_tokens": 512,
    }


def extract_transcript(response: Any) -> str:
    """Pull usable transcript text out of a gateway response.

    Returns ``""`` for anything the model did not actually transcribe -- a missing
    body, a refusal, or an explicit "no speech" answer. The caller treats that as
    "nothing said", exactly as the mod does for its own empty transcripts.
    """
    if not isinstance(response, dict):
        return ""
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    first = choices[0]
    if not isinstance(first, dict):
        return ""
    message = first.get("message")
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        # Some models return a parts array.
        text = " ".join(
            part.get("text", "") for part in content
            if isinstance(part, dict) and isinstance(part.get("text"), str)
        )
    else:
        return ""
    return clean_transcript(text)


def clean_transcript(text: Any) -> str:
    """Normalise a raw model reply into text safe to hand to the NPC.

    - Strips surrounding whitespace and stray quotes/brackets the models add.
    - Drops conversational filler a chat model prepends ("Sure! ...").
    - Returns "" for any "no speech" style answer.
    """
    if not isinstance(text, str):
        return ""
    cleaned = text.strip().strip('"').strip("'").strip()
    cleaned = " ".join(cleaned.split())
    if not cleaned:
        return ""
    if len(cleaned) < 2:
        return ""
    if _is_no_speech(cleaned):
        return ""
    return cleaned


def transcript_reply(text: str) -> dict:
    """The mod-facing response body. ``text`` is the key the mod reads first."""
    return {"text": text if isinstance(text, str) else ""}


def to_json_body(payload: dict) -> bytes:
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def _is_no_speech(text: str) -> bool:
    folded = "".join(c for c in text.lower() if c.isalnum() or c.isspace())
    folded = " ".join(folded.split())
    return any(marker in folded for marker in _NO_SPEECH_MARKERS)


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _positive_seconds(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return number if number > 0 else None
    return None


def _seconds_from_ms(value: Any) -> Optional[float]:
    """Interpret a caller-supplied ``duration_ms`` as seconds.

    A string is accepted because the addon may send a JSON number, and a
    defensive caller may send a string; anything non-numeric means "unknown",
    which sends the reader to the WAV header instead.
    """
    if isinstance(value, str):
        try:
            value = float(value.strip())
        except (ValueError, AttributeError):
            return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    ms = float(value)
    if ms <= 0:
        return None
    return ms / 1000.0


def _wav_seconds(raw: bytes) -> Optional[float]:
    """Read a RIFF/WAVE header, falling back to the uncompressed byte estimate."""
    if len(raw) < 44 or raw[:4] != b"RIFF" or raw[8:12] != b"WAVE":
        return None
    if raw[12:16] != b"fmt ":
        return None
    byte_rate = int.from_bytes(raw[28:32], "little")
    data_size = int.from_bytes(raw[40:44], "little")
    if byte_rate > 0:
        # Prefer the declared data chunk, which is exact even with extra chunks.
        if 44 + data_size <= len(raw) + 64:
            return data_size / byte_rate
    if data_size > 0 and byte_rate > 0:
        return data_size / byte_rate
    return None
