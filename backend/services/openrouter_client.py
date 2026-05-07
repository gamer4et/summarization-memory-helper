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

``summarize_chapter_sections(text)``
    Sends a chapter text to five focused OpenRouter chat/completions requests.
    Each request returns one strict Markdown section; the sections are assembled
    into one Markdown summary for the existing UI.

``generate_chapter_tests(text, chapter_title, target_count)``
    Sends a chapter transcription to a text LLM and returns stored multiple-choice
    questions focused on essential understanding rather than minor details.

Both functions raise :class:`httpx.HTTPStatusError` on 4xx/5xx responses and
:class:`OpenRouterError` for unexpected response shapes.
"""

import base64
import asyncio
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


CHAPTER_TESTS_SYSTEM_PROMPT = """You are an educational test designer for book learning and spaced repetition.
Create multiple-choice questions from one chapter transcription.

Question quality rules:
1. Test essential understanding: central ideas, causal links, definitions, trade-offs, frameworks, examples that reveal a concept, and practical implications.
2. Do not test tiny details, dates, names, wording trivia, or accidental transcription artifacts unless they are central to the chapter's argument.
3. Each question must be answerable from the supplied transcription only.
4. Each question must have exactly four options and exactly one correct option.
5. Distractors must be plausible but clearly wrong if the chapter is understood.
6. Explanations must briefly explain why the correct answer follows from the chapter.
7. For every incorrect option, generate a separate short explanation of why that specific option is wrong.
8. Use an empty string for the correct option's option_explanations item.
9. Use the same language as the chapter unless the user explicitly says otherwise.

Return a JSON object with this exact structure:
{
  "questions": [
    {
      "question": "question text",
      "options": ["answer A", "answer B", "answer C", "answer D"],
      "correct_option_index": 0,
      "explanation": "short explanation",
      "option_explanations": ["", "why answer B is wrong", "why answer C is wrong", "why answer D is wrong"],
      "difficulty": "easy|medium|hard",
      "concept_tags": ["tag one", "tag two"]
    }
  ]
}
"""


CHAPTER_TESTS_RESPONSE_FORMAT: dict = {
    "type": "json_schema",
    "json_schema": {
        "name": "chapter_tests",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "questions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "question": {"type": "string"},
                            "options": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "correct_option_index": {"type": "integer"},
                            "explanation": {"type": "string"},
                            "option_explanations": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "difficulty": {"type": "string"},
                            "concept_tags": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                        },
                        "required": [
                            "question",
                            "options",
                            "correct_option_index",
                            "explanation",
                            "option_explanations",
                            "difficulty",
                            "concept_tags",
                        ],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["questions"],
            "additionalProperties": False,
        },
    },
}


SUMMARY_SECTION_ORDER = (
    "graphs",
    "definitions",
    "dense_summary",
    "key_facts",
    "triples",
)


SUMMARY_SECTION_HEADINGS: dict[str, str] = {
    "graphs": "## Графы",
    "definitions": "## Определения, таблицы и тезисы",
    "dense_summary": "## Плотная сводка",
    "key_facts": "## Ключевые факты",
    "triples": "## Триплеты",
}


SUMMARY_SECTION_EMPTY_MESSAGES: dict[str, str] = {
    "graphs": "— Явных графовых структур в главе нет или модель вернула пустой ответ.",
    "definitions": "— Определения, таблицы и тезисы не извлечены: модель вернула пустой ответ.",
    "dense_summary": "— Плотная сводка недоступна: модель вернула пустой ответ.",
    "key_facts": "— Ключевые факты не извлечены: модель вернула пустой ответ.",
    "triples": "— Триплеты не извлечены: модель вернула пустой ответ.",
}


SUMMARY_SECTION_SYSTEM_PROMPTS: dict[str, str] = {
    "graphs": """You extract every meaningful graph-like structure from a chapter transcript.
Return only Markdown in the requested output language. Do not return JSON.

Rules:
1. Use only the transcript. Do not invent entities, links, processes, or hierarchy.
2. Extract all explicit relationship graphs and all structures defined in the chapter: causal maps, hierarchies, classifications, processes, dependencies, contrasts, entity relationships, frameworks, and table-like structures that are clearer as graphs.
3. If the chapter contains multiple independent graphs, output multiple separate Mermaid code blocks.
4. Each Mermaid block must use `flowchart LR` only.
5. Node IDs must be Latin letters/digits/underscores without spaces. Display names must be quoted in square brackets, for example `A["Name"]`.
6. Edge labels must be concise and factual.
7. If there are no graph-worthy relationships, return the heading and `— Явных графовых структур в главе нет.`

Strict format:
## Графы

### <graph title>
```mermaid
flowchart LR
  A["..."] -->|...| B["..."]
```

Repeat graph subsections as needed. Output no text outside this section.""",
    "definitions": """You extract definitions, tables, and theses from a chapter transcript.
Return only Markdown in the requested output language. Do not return JSON.

Rules:
1. Use only the transcript. Do not invent or import external knowledge.
2. Extract absolutely all definitions, explicit terms, distinctions, rules, claims, theses, exceptions, caveats, classifications, and table-like structures present in the chapter.
3. Treat the transcript as imperfect speech: ignore obvious filler and ASR glitches, but never change meaning.
4. Include short source anchors as verbatim quotes where possible.
5. If a subsection has no items, keep the subsection and write `—` plus a short reason.

Strict format:
## Определения, таблицы и тезисы

### Определения
- **<term>** — <definition>. *Источник:* «<short quote>»

### Таблицы и структуры
| Название | Элементы/колонки | Смысл | Источник |
|---|---|---|---|
| ... | ... | ... | «...» |

### Тезисы
- <thesis>. *Источник:* «<short quote>»

### Исключения и оговорки
- <exception/caveat>. *Источник:* «<short quote>»

Output no text outside this section.""",
    "dense_summary": """You write a very dense chapter summary.
Return only Markdown in the requested output language. Do not return JSON.

Rules:
1. Use only the transcript. Do not invent facts.
2. Silently ignore speech filler and obvious ASR glitches without changing meaning.
3. Internally perform chain-of-density compression: start broad, add missing important entities, compress wording, and keep all central concepts.
4. The final paragraph must be compact but information-rich.

Strict format:
## Плотная сводка

<one dense paragraph, usually 120–220 words, depending on chapter complexity>

**Ключевые сущности:** <10–25 comma-separated entities/concepts>

Output no text outside this section.""",
    "key_facts": """You extract key facts from a chapter transcript.
Return only Markdown in the requested output language. Do not return JSON.

Rules:
1. Use only the transcript. Do not invent facts.
2. Extract all key atomic facts, not just a small sample. A fact must be self-contained and understandable without surrounding context.
3. Prefer conceptual, causal, definitional, practical, and argumentative facts over minor wording trivia.
4. Include a short verbatim source anchor for every fact where possible.
5. If there are no factual claims, return the heading and `— Ключевые факты не обнаружены.`

Strict format:
## Ключевые факты

1. <atomic fact>. *Источник:* «<short quote>»
2. ...

Output no text outside this section.""",
    "triples": """You extract knowledge graph triples from a chapter transcript.
Return only Markdown in the requested output language. Do not return JSON.

Rules:
1. Use only the transcript. Do not invent subjects, relations, or objects.
2. Extract all meaningful subject–relation–object triples: definitions, causality, hierarchy, membership, opposition, constraints, examples, properties, consequences, and process steps.
3. Normalize entity names consistently.
4. Include source anchors as short verbatim quotes where possible.
5. If there are no triples, return the heading and an empty table with one row containing `—`.

Strict format:
## Триплеты

| Субъект | Отношение | Объект | Источник |
|---|---|---|---|
| <subject> | <relation> | <object> | «<short quote>» |

Output no text outside this section.""",
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


def assemble_summary_markdown(sections: dict[str, str]) -> str:
    """Assemble focused Markdown sections into one summary for the UI."""
    parts: list[str] = []
    for section_key in SUMMARY_SECTION_ORDER:
        section_text = str(sections.get(section_key) or "").strip()
        if section_text:
            parts.append(section_text)
    return "\n\n".join(parts).strip()


def _empty_summary_section_markdown(section_key: str) -> str:
    """Return a valid Markdown section when a provider returns no content."""
    heading = SUMMARY_SECTION_HEADINGS[section_key]
    message = SUMMARY_SECTION_EMPTY_MESSAGES[section_key]
    if section_key == "triples":
        return (
            f"{heading}\n\n"
            "| Субъект | Отношение | Объект | Источник |\n"
            "|---|---|---|---|\n"
            f"| — | — | — | {message} |"
        )
    return f"{heading}\n\n{message}"


async def _summarize_chapter_section(text: str, section_key: str) -> str:
    """Generate one strict Markdown summary section for a chapter."""
    transcript = str(text or "").strip()
    if not transcript:
        raise ValueError("Cannot summarize an empty chapter transcription.")
    if section_key not in SUMMARY_SECTION_SYSTEM_PROMPTS:
        raise ValueError(f"Unknown summary section key: {section_key}")

    s = settings.openrouter.summarization
    heading = SUMMARY_SECTION_HEADINGS[section_key]
    logger.info(
        "Generating summary section '%s': %d chars using model=%s",
        section_key,
        len(transcript),
        s.model,
    )
    user_prompt = (
        f"Язык вывода: `{s.language}`\n"
        f"Обязательный заголовок секции: `{heading}`\n\n"
        "Транскрипт главы для анализа:\n"
        "```\n"
        f"{transcript}\n"
        "```\n\n"
        "Верни только требуемую Markdown-секцию. Не возвращай JSON. "
        "Не добавляй вступление, заключение или комментарии вне секции."
    )

    payload: dict = {
        "model": s.model,
        "messages": [
            {"role": "system", "content": SUMMARY_SECTION_SYSTEM_PROMPTS[section_key]},
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

    _log_response(response, f"summary section {section_key}")
    response.raise_for_status()

    body = response.json()
    try:
        raw_content = body["choices"][0]["message"].get("content")
    except (KeyError, IndexError, TypeError) as exc:
        raise OpenRouterError(
            f"Summary section '{section_key}' response has unexpected structure: {body!r}"
        ) from exc

    section_markdown = str(raw_content or "").strip()
    if not section_markdown:
        logger.warning(
            "Summary section '%s' response content is empty/null; using fallback Markdown section. Body: %r",
            section_key,
            body,
        )
        section_markdown = _empty_summary_section_markdown(section_key)


    logger.info(
        "Summary section '%s' complete: %d chars",
        section_key,
        len(section_markdown),
    )
    return section_markdown


@retry(
    retry=retry_if_exception(_is_retriable_error),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=5, max=30),
    reraise=True,
)
async def summarize_chapter_sections(text: str) -> dict:
    """Generate all chapter summary sections as strict Markdown.

    The five calls are launched in parallel because they read the same chapter
    transcript and have no dependencies on each other. Any failed section fails
    the whole attempt, keeping the existing processing error semantics.
    """
    transcript = str(text or "").strip()
    if not transcript:
        raise ValueError("Cannot summarize an empty chapter transcription.")

    section_values = await asyncio.gather(
        *(
            _summarize_chapter_section(transcript, section_key)
            for section_key in SUMMARY_SECTION_ORDER
        )
    )
    sections = dict(zip(SUMMARY_SECTION_ORDER, section_values, strict=True))
    summary_text = assemble_summary_markdown(sections)
    logger.info(
        "Multi-section summarization complete: %d section(s), %d assembled chars",
        len(sections),
        len(summary_text),
    )
    return {
        "summary_text": summary_text,
        "sections": sections,
    }


@retry(
    retry=retry_if_exception(_is_retriable_error),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=5, max=30),
    reraise=True,
)
async def generate_chapter_tests(
    text: str,
    chapter_title: str = "",
    target_count: int = 10,
) -> dict:
    """Generate essence-focused multiple-choice tests for one chapter."""
    transcript = str(text or "").strip()
    if not transcript:
        raise ValueError("Cannot generate tests for an empty chapter transcription.")

    count = max(1, min(int(target_count or 10), 30))
    s = settings.openrouter.summarization
    logger.info(
        "Generating %d chapter test question(s): %d chars, model=%s",
        count,
        len(transcript),
        s.model,
    )

    payload: dict = {
        "model": s.model,
        "messages": [
            {"role": "system", "content": CHAPTER_TESTS_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    f"Chapter title: {chapter_title or 'Untitled chapter'}\n"
                    f"Target number of questions: {count}\n\n"
                    "Generate multiple-choice questions from this transcription. "
                    "Prioritize conceptual understanding and useful repetition, not minor details.\n\n"
                    f"{transcript}"
                ),
            },
        ],
        "response_format": CHAPTER_TESTS_RESPONSE_FORMAT,
        "max_tokens": s.max_tokens,
        "temperature": 0.2,
    }

    headers = {**_auth_headers(), "Content-Type": "application/json"}

    async with httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT) as client:
        response = await client.post(
            f"{_BASE_URL}/chat/completions",
            headers=headers,
            json=payload,
        )

    _log_response(response, "chapter tests")
    response.raise_for_status()

    body = response.json()
    try:
        raw_content: str = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise OpenRouterError(
            f"Chapter tests response has unexpected structure: {body!r}"
        ) from exc

    result = _parse_strict_llm_json_object(raw_content, "Chapter tests")
    if "questions" not in result or not isinstance(result["questions"], list):
        raise OpenRouterError(
            f"Chapter tests JSON missing required 'questions' list: {result!r}"
        )

    logger.info("Chapter test generation complete: %d question(s)", len(result["questions"]))
    return result


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
