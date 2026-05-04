"""
Async HTTP client for the OpenRouter API.

Three functions are exposed:

``transcribe_audio(audio_path, language, previous_context)``
    Reads a WAV file, encodes it as base64, and sends it to the
    OpenRouter chat/completions endpoint using a multimodal LLM.
    Returns a dict with the key ``full_transcription`` only.

``analyze_transcription_chapters(text, language)``
    Sends the concatenated full transcript to a text LLM and detects real
    chapter boundaries globally. Raw audio/VAD chunks are intentionally not
    treated as chapters.

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
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from backend.core.config import settings

logger = logging.getLogger(__name__)

_BASE_URL = "https://openrouter.ai/api/v1"

# Default timeout for non-audio requests (seconds).
_DEFAULT_TIMEOUT = httpx.Timeout(connect=15.0, read=300.0, write=120.0, pool=10.0)

_WAV_HEADER_SIZE = 44


def _is_retriable_error(exception: Exception) -> bool:
    """
    Check if exception should trigger a retry.

    Retries only on transient server/network issues.
    Does not retry on 4xx client errors.
    """
    if isinstance(exception, httpx.HTTPStatusError):
        # Retry only on 5xx server errors
        return 500 <= exception.response.status_code < 600
    return isinstance(
        exception,
        (
            httpx.TimeoutException,
            httpx.ConnectError,
            httpx.RemoteProtocolError,
            httpx.NetworkError,
        ),
    )


def _calculate_transcription_timeout(audio_size_bytes: int) -> httpx.Timeout:
    """Calculate a generous timeout for a WAV transcription request."""
    audio_data_bytes = max(0, audio_size_bytes - _WAV_HEADER_SIZE)
    estimated_duration_sec = audio_data_bytes / (settings.audio.sample_rate * 2)
    estimated_duration_min = estimated_duration_sec / 60

    # LLM audio processing time is non-linear. Keep a high floor, scale with
    # duration, and cap at 90 minutes so a stuck request cannot hang forever.
    read_timeout = min(max(300.0, 180.0 + estimated_duration_min * 240.0), 5400.0)

    # Large base64 JSON bodies can hit write timeout on slow Docker/network
    # links. Scale write timeout with payload size, cap at 20 minutes.
    size_mb = audio_size_bytes / 1024 / 1024
    write_timeout = min(max(300.0, 120.0 + size_mb * 30.0), 1200.0)

    logger.info(
        "Transcription request timeout limits: audio_size=%d bytes duration≈%.1f min read_limit=%.0fs write_limit=%.0fs",
        audio_size_bytes,
        estimated_duration_min,
        read_timeout,
        write_timeout,
    )

    return httpx.Timeout(
        connect=60.0,
        read=read_timeout,
        write=write_timeout,
        pool=30.0,
    )

# ---------------------------------------------------------------------------
# System prompts for transcription and global chapter detection
# ---------------------------------------------------------------------------

TRANSCRIPTION_SYSTEM_PROMPT = """You are a precise audio transcription and light cleanup assistant.
Your task is to transcribe the primary speaker's speech accurately in the language provided.

Do not summarize.
Do not analyze topics.
Do not detect chapters.
Do not split the result into sections.

Speaker and noise rules:
1. Transcribe only the main/nearest speaker who is giving the book notes.
2. Ignore background voices, side conversations, accidental interruptions, playback echo, TV/radio, and other secondary speakers unless the main speaker clearly repeats or responds to them as part of the note.

Context rules:
1. You may receive previous transcript context from earlier audio fragments.
2. Use previous context only to resolve unclear words, names, terms, grammar, and continuity.
3. Do not copy previous context into the answer. Return only speech contained in the current audio fragment.

Light cleanup rules:
1. Remove obvious ASR/speech glitches that are not meaningful content: duplicated filler fragments, accidental syllables, repeated conjunctions, false starts, and nonsense insertions caused by stutter or recognition artifacts.
2. Example: if the intended phrase is "гипотезы и догадки", do not output artifacts like "гипотезы и га и догадки".
3. Keep the speaker's meaning, terminology, order of ideas, and wording as close to the audio as possible.
4. Do not rewrite into a polished summary. Do not add facts. Do not silently replace uncertain terms with unrelated guesses.
5. Preserve explicit spoken chapter marker words as normal text if they are present.

Return a JSON object with this exact structure:
{
    "full_transcription": "complete transcription of all speech in this audio fragment"
}
"""


TRANSCRIPTION_RESPONSE_FORMAT: dict = {
    "type": "json_schema",
    "json_schema": {
        "name": "audio_transcription",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "full_transcription": {
                    "type": "string",
                    "description": "Complete transcription of all speech in the audio fragment.",
                },
            },
            "required": ["full_transcription"],
            "additionalProperties": False,
        },
    },
}


CHAPTER_ANALYSIS_SYSTEM_PROMPT = """You are a transcript structure analysis assistant.
Your task is to split one complete concatenated transcript into real chapters/sections.

Rules:
1. Use only explicit spoken chapter markers, such as "chapter one", "chapter two", "new chapter", "next chapter", "chapter [number/word]", "глава один", "глава два", "новая глава", "следующая глава", etc.
2. Never split by raw audio chunks, pauses, topic shifts, paragraph boundaries, or estimated time.
3. If the transcript contains no explicit spoken chapter markers, return exactly one chapter containing the full transcript.
4. If explicit markers exist, split only at those markers and keep all surrounding text in the correct chapter.
5. Prefer the title/name spoken in the marker. If no useful title is spoken, use "Chapter N".

Return a JSON object with this exact structure:
{
    "chapters": [
        {
            "chapter_number": 1,
            "title": "Full recording",
            "transcription": "text of this chapter"
        }
    ]
}

Do not invent extra chapters. A long transcript without explicit chapter markers is still one chapter."""


CHAPTER_ANALYSIS_RESPONSE_FORMAT: dict = {
    "type": "json_schema",
    "json_schema": {
        "name": "chapter_analysis",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "chapters": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "chapter_number": {"type": "integer"},
                            "title": {"type": "string"},
                            "transcription": {"type": "string"},
                        },
                        "required": ["chapter_number", "title", "transcription"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["chapters"],
            "additionalProperties": False,
        },
    },
}


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


def _parse_strict_llm_json_object(raw_content: str, label: str) -> dict:
    """
    Parse LLM message content as one strict JSON object.

    The request uses OpenRouter/OpenAI-compatible JSON Schema structured
    outputs, so extra braces, markdown fences, comments, or trailing text should
    be treated as provider/model output defects instead of being silently healed.
    """
    try:
        result = json.loads(str(raw_content or ""))
    except json.JSONDecodeError as exc:
        raise OpenRouterError(
            f"{label} LLM returned malformed JSON: {raw_content!r}"
        ) from exc

    if not isinstance(result, dict):
        raise OpenRouterError(
            f"{label} LLM returned JSON but not an object: {raw_content!r}"
        )
    return result


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


@retry(
    retry=retry_if_exception(_is_retriable_error),
    stop=stop_after_attempt(3),  # 1 original + 2 retries = 3 total attempts
    wait=wait_exponential(multiplier=2, min=5, max=30),
    reraise=True,
)
async def transcribe_audio(
    audio_path: str | Path,
    language: str = "ru",
    previous_context: str = "",
) -> dict:
    """
    Transcribe a WAV audio file via a multimodal LLM on OpenRouter.

    The audio is base64-encoded and passed in the messages array alongside a
    system prompt that instructs the model to transcribe only, lightly removing
    obvious speech/ASR glitches. Chapter analysis is performed later on the
    concatenated transcript, so raw audio chunks do not become chapters.

    Automatically retries up to 2 times on 5xx server errors and transient
    network/timeouts, including write timeouts while uploading large payloads.
    Does NOT retry on 4xx client errors (bad request, auth failure, etc).
    Uses a generous per-file timeout because LLM processing time is non-linear.

    Parameters
    ----------
    audio_path:
        Path to a WAV file on disk.
    language:
        BCP-47 language code (e.g. ``"ru"``, ``"en"``) or ``"auto"`` for
        automatic detection.
    previous_context:
        Previous transcript text from earlier chunks. Used only as context for
        resolving unclear words in the current audio fragment.

    Returns
    -------
    dict
        ``{"full_transcription": str}``

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
    if audio_size <= _WAV_HEADER_SIZE:
        raise ValueError(
            f"Audio file '{path.name}' contains no audio frames "
            f"(file size {audio_size} bytes ≤ WAV header {_WAV_HEADER_SIZE} bytes). "
            "Ensure the recording captured actual speech before calling transcription."
        )

    # Encode the WAV file as base64.
    audio_b64 = base64.b64encode(audio_bytes).decode("utf-8")

    context_text = str(previous_context or "").strip()
    text_instruction = (
        f"Transcribe this audio fragment. The language spoken is: {language}. "
        "Return JSON as specified. Do not detect chapters or summarize. "
        "Use the previous transcript context, if provided, only to resolve unclear words. "
        "Return only speech from the current audio fragment."
    )
    if context_text:
        text_instruction += (
            "\n\nPrevious transcript context from earlier fragments "
            "(do not include it in your answer):\n"
            f"```\n{context_text}\n```"
        )

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
                    "text": text_instruction,
                },
            ],
        },
    ]

    payload: dict = {
        "model": settings.openrouter.transcription.model,
        "messages": messages,
        "response_format": TRANSCRIPTION_RESPONSE_FORMAT,
    }

    headers = {**_auth_headers(), "Content-Type": "application/json"}

    timeout = _calculate_transcription_timeout(audio_size)
    async with httpx.AsyncClient(timeout=timeout) as client:
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

    result = _parse_strict_llm_json_object(raw_content, "Transcription")

    # Validate required keys.
    if "full_transcription" not in result:
        raise OpenRouterError(
            f"Transcription JSON missing required keys "
            f"('full_transcription'): {result!r}"
        )

    result = {"full_transcription": str(result.get("full_transcription") or "").strip()}
    logger.info(
        "Transcription complete: %d chars",
        len(result["full_transcription"]),
    )
    return result


@retry(
    retry=retry_if_exception(_is_retriable_error),
    stop=stop_after_attempt(3),  # 1 original + 2 retries = 3 total attempts
    wait=wait_exponential(multiplier=2, min=5, max=30),
    reraise=True,
)
async def analyze_transcription_chapters(text: str, language: str = "ru") -> dict:
    """
    Detect real chapter boundaries in a complete concatenated transcript.

    This function intentionally runs after all audio chunks have been
    transcribed and concatenated. It must not see or infer raw VAD/transcription
    chunk boundaries; only explicit spoken chapter markers can create multiple
    chapters.
    """
    transcript = str(text or "").strip()
    if not transcript:
        raise ValueError("Cannot analyze chapters for an empty transcription.")

    s = settings.openrouter.summarization
    logger.info(
        "Analyzing chapters from combined transcription: %d chars, model=%s, language=%s",
        len(transcript),
        s.model,
        language,
    )

    payload: dict = {
        "model": s.model,
        "messages": [
            {"role": "system", "content": CHAPTER_ANALYSIS_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    f"Language: {language}\n\n"
                    "Analyze this complete concatenated transcript and return chapters JSON.\n\n"
                    f"{transcript}"
                ),
            },
        ],
        "response_format": CHAPTER_ANALYSIS_RESPONSE_FORMAT,
        "max_tokens": s.max_tokens,
        "temperature": 0.0,
    }

    headers = {**_auth_headers(), "Content-Type": "application/json"}

    async with httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT) as client:
        response = await client.post(
            f"{_BASE_URL}/chat/completions",
            headers=headers,
            json=payload,
        )

    _log_response(response, "chapter analysis")
    response.raise_for_status()

    body = response.json()
    try:
        raw_content: str = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise OpenRouterError(
            f"Chapter analysis response has unexpected structure: {body!r}"
        ) from exc

    result = _parse_strict_llm_json_object(raw_content, "Chapter analysis")

    if "chapters" not in result or not isinstance(result["chapters"], list):
        raise OpenRouterError(
            f"Chapter analysis JSON missing required 'chapters' list: {result!r}"
        )

    logger.info("Chapter analysis complete: %d chapter(s)", len(result["chapters"]))
    return result


@retry(
    retry=retry_if_exception(_is_retriable_error),
    stop=stop_after_attempt(3),  # 1 original + 2 retries = 3 total attempts
    wait=wait_exponential(multiplier=2, min=5, max=30),
    reraise=True,
)
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
    user_prompt = (
        "Текст для анализа:\n"
        "```\n"
        f"{text}\n"
        "```\n\n"
        f"Выбранные режимы: `{s.default_modes}`\n"
        f"Язык вывода: `{s.language}`\n"
        f"Итераций для плотной сводки: `{s.density_iterations}`\n\n"
        "Сгенерируй ответ."
    )

    payload: dict = {
        "model": s.model,
        "messages": [
            {"role": "system", "content": s.system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "max_tokens": s.max_tokens,
        "temperature": s.temperature,
    }

    headers = {**_auth_headers(), "Content-Type": "application/json"}

    async with httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT) as client:
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
