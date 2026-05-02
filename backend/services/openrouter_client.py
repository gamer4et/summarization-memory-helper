"""
Async HTTP client for the OpenRouter API.

Two functions are exposed:

``transcribe_audio(audio_path, language)``
    Reads a WAV file, encodes it as base64, and sends it to the
    OpenRouter chat/completions endpoint using a multimodal LLM
    (google/gemini-2.0-flash-001).  Returns a dict with keys
    ``full_transcription`` and ``chapters``.

``summarize_text(text)``
    Sends a chapter text to the OpenRouter chat/completions endpoint.
    Returns the LLM-generated summary string.

Both functions raise :class:`httpx.HTTPStatusError` on 4xx/5xx responses and
:class:`OpenRouterError` for unexpected response shapes.
"""

import base64
import json
import logging
from pathlib import Path

import httpx

from backend.core.config import settings

logger = logging.getLogger(__name__)

_BASE_URL = "https://openrouter.ai/api/v1"

# Default timeout for all requests (seconds).
_TIMEOUT = httpx.Timeout(connect=10.0, read=180.0, write=30.0, pool=5.0)

# ---------------------------------------------------------------------------
# System prompt for transcription + chapter detection
# ---------------------------------------------------------------------------

TRANSCRIPTION_SYSTEM_PROMPT = """You are a transcription and chapter detection assistant.
Your task is to:
1. Transcribe the spoken audio accurately in the language provided
2. Detect chapter markers - whenever the speaker says something like "chapter one", "chapter two", "new chapter", "chapter [number/word]", "глава один", "глава два", "новая глава", "следующая глава", etc.
3. Split the transcription into chapters based on these markers

Return a JSON object with this exact structure:
{
    "full_transcription": "complete transcription of all speech",
    "chapters": [
        {
            "chapter_number": 1,
            "title": "Chapter 1",
            "transcription": "text of this chapter"
        }
    ]
}

If no chapter markers are detected, return a single chapter with chapter_number 1.
The "title" field should use the exact chapter name the speaker said, or "Chapter N" if only a number was mentioned.
Do not include the chapter marker words themselves in the chapter transcription content."""


class OpenRouterError(RuntimeError):
    """Raised when the OpenRouter API returns an unexpected response shape."""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _auth_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {settings.openrouter.api_key}",
        "HTTP-Referer": "https://github.com/summarization-memory-helper",
        "X-Title": "Book Summarizer",
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def transcribe_audio(audio_path: str | Path, language: str = "ru") -> dict:
    """
    Transcribe a WAV audio file via a multimodal LLM on OpenRouter.

    The audio is base64-encoded and passed in the messages array alongside a
    system prompt that instructs the model to transcribe and detect chapters.

    Parameters
    ----------
    audio_path:
        Path to a WAV file on disk.
    language:
        BCP-47 language code (e.g. ``"ru"``, ``"en"``) or ``"auto"`` for
        automatic detection.

    Returns
    -------
    dict
        ``{"full_transcription": str, "chapters": [{"chapter_number": int,
        "title": str, "transcription": str}]}``

    Raises
    ------
    FileNotFoundError
        If ``audio_path`` does not exist.
    httpx.HTTPStatusError
        On 4xx / 5xx responses from OpenRouter.
    OpenRouterError
        When the JSON response does not contain the expected structure or the
        LLM returns malformed JSON.
    """
    path = Path(audio_path)
    if not path.exists():
        raise FileNotFoundError(f"Audio file not found: {path}")

    audio_bytes = path.read_bytes()
    audio_size = len(audio_bytes)

    logger.info(
        "Transcribing %s — size=%d bytes, model=%s, language=%s",
        path.name,
        audio_size,
        settings.openrouter.transcription.model,
        language,
    )

    # Guard: a WAV file with only the 44-byte header contains no audio frames.
    # Sending it to the API results in INVALID_ARGUMENT from the provider.
    _WAV_HEADER_SIZE = 44
    if audio_size <= _WAV_HEADER_SIZE:
        raise ValueError(
            f"Audio file '{path.name}' contains no audio frames "
            f"(file size {audio_size} bytes ≤ WAV header {_WAV_HEADER_SIZE} bytes). "
            "Ensure the recording captured actual speech before calling transcription."
        )

    # Encode the WAV file as base64.
    audio_b64 = base64.b64encode(audio_bytes).decode("utf-8")

    messages = [
        {
            "role": "system",
            "content": TRANSCRIPTION_SYSTEM_PROMPT,
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "input_audio",
                    "input_audio": {
                        "data": audio_b64,
                        "format": "wav",
                    },
                },
                {
                    "type": "text",
                    "text": (
                        f"Transcribe this audio. The language spoken is: {language}. "
                        "Return JSON as specified."
                    ),
                },
            ],
        },
    ]

    payload: dict = {
        "model": settings.openrouter.transcription.model,
        "messages": messages,
        "response_format": {"type": "json_object"},
    }

    headers = {**_auth_headers(), "Content-Type": "application/json"}

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        response = await client.post(
            f"{_BASE_URL}/chat/completions",
            headers=headers,
            json=payload,
        )

    _log_response(response, "transcription")
    response.raise_for_status()

    body = response.json()
    try:
        raw_content: str = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise OpenRouterError(
            f"Transcription response has unexpected structure: {body!r}"
        ) from exc

    try:
        result: dict = json.loads(raw_content)
    except json.JSONDecodeError as exc:
        raise OpenRouterError(
            f"Transcription LLM returned malformed JSON: {raw_content!r}"
        ) from exc

    # Validate required keys.
    if "full_transcription" not in result or "chapters" not in result:
        raise OpenRouterError(
            f"Transcription JSON missing required keys "
            f"('full_transcription', 'chapters'): {result!r}"
        )

    logger.info(
        "Transcription complete: %d chars, %d chapter(s)",
        len(result["full_transcription"]),
        len(result["chapters"]),
    )
    return result


async def summarize_text(text: str) -> str:
    """
    Summarize a chapter transcription via OpenRouter chat completions.

    Parameters
    ----------
    text:
        The chapter transcription text to summarize.

    Returns
    -------
    str
        The LLM-generated summary.

    Raises
    ------
    httpx.HTTPStatusError
        On 4xx / 5xx responses from OpenRouter.
    OpenRouterError
        When the JSON response does not contain the expected structure.
    """
    s = settings.openrouter.summarization
    logger.info("Summarizing %d chars using model=%s", len(text), s.model)

    payload: dict = {
        "model": s.model,
        "messages": [
            {"role": "system", "content": s.system_prompt},
            {"role": "user", "content": text},
        ],
        "max_tokens": s.max_tokens,
        "temperature": s.temperature,
    }

    headers = {**_auth_headers(), "Content-Type": "application/json"}

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        response = await client.post(
            f"{_BASE_URL}/chat/completions",
            headers=headers,
            json=payload,
        )

    _log_response(response, "summarization")
    response.raise_for_status()

    body = response.json()
    try:
        summary: str = body["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, TypeError) as exc:
        raise OpenRouterError(
            f"Summarization response has unexpected structure: {body!r}"
        ) from exc

    logger.info("Summarization complete: %d chars", len(summary))
    return summary


# ---------------------------------------------------------------------------
# Logging helper
# ---------------------------------------------------------------------------


def _log_response(response: httpx.Response, label: str) -> None:
    logger.debug(
        "%s response: status=%d url=%s",
        label,
        response.status_code,
        str(response.url),
    )
    if response.status_code >= 400:
        try:
            body_text = response.text
        except Exception:
            body_text = "<unreadable>"
        logger.error(
            "%s ERROR response body: %s",
            label,
            body_text,
        )
