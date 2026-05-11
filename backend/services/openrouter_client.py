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
    Sends a chapter text to six focused OpenRouter chat/completions requests.
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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeVar

import httpx
from openai import APIConnectionError, APIStatusError, APITimeoutError, AsyncOpenAI
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from backend.core.config import settings

logger = logging.getLogger(__name__)

_BASE_URL = "https://openrouter.ai/api/v1"
_OPENROUTER_REFERER = "https://github.com/summarization-memory-helper"
_OPENROUTER_TITLE = "Book Summarizer"
_OPENAI_API_KEY_PLACEHOLDER = "missing-openrouter-api-key"

# Default timeout for non-audio requests (seconds).
_DEFAULT_TIMEOUT = httpx.Timeout(connect=15.0, read=300.0, write=120.0, pool=10.0)

_WAV_HEADER_SIZE = 44

TStructuredModel = TypeVar("TStructuredModel", bound=BaseModel)


def _is_retriable_error(exception: Exception) -> bool:
    """
    Check if exception should trigger a retry.

    Retries only on transient server/network issues.
    Does not retry on 4xx client errors.
    """
    # Transient provider errors embedded in HTTP 200 envelopes (e.g. 502 from
    # Google Vertex routed through OpenRouter) are always retriable.
    if isinstance(exception, OpenRouterProviderError):
        return True
    if isinstance(exception, httpx.HTTPStatusError):
        # Retry only on 5xx server errors
        return 500 <= exception.response.status_code < 600
    if isinstance(exception, APIStatusError):
        return 500 <= exception.status_code < 600
    return isinstance(
        exception,
        (
            httpx.TimeoutException,
            httpx.ConnectError,
            httpx.RemoteProtocolError,
            httpx.NetworkError,
            APITimeoutError,
            APIConnectionError,
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


class TranscriptionResponse(BaseModel):
    """Structured response returned by the transcription model."""

    model_config = ConfigDict(extra="forbid")

    full_transcription: str

    @field_validator("full_transcription", mode="before")
    @classmethod
    def normalize_full_transcription(cls, value: Any) -> str:
        return str(value or "").strip()


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


class ChapterAnalysisItem(BaseModel):
    """One chapter identified in the concatenated transcript."""

    model_config = ConfigDict(extra="forbid")

    chapter_number: int
    title: str = ""
    transcription: str

    @field_validator("title", mode="before")
    @classmethod
    def normalize_title(cls, value: Any) -> str:
        return str(value or "").strip()


class ChapterAnalysisResponse(BaseModel):
    """Structured response returned by chapter analysis."""

    model_config = ConfigDict(extra="forbid")

    chapters: list[ChapterAnalysisItem]


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


class ChapterTestQuestion(BaseModel):
    """One generated multiple-choice question."""

    model_config = ConfigDict(extra="forbid")

    question: str
    options: list[str]
    correct_option_index: int
    explanation: str
    option_explanations: list[str]
    difficulty: str
    concept_tags: list[str]


class ChapterTestsResponse(BaseModel):
    """Structured response returned by chapter-test generation."""

    model_config = ConfigDict(extra="forbid")

    questions: list[ChapterTestQuestion]


SUMMARY_SECTION_ORDER = (
    "graphs",
    "definitions",
    "tables",
    "dense_summary",
    "key_facts",
    "triples",
)


SUMMARY_SECTION_HEADINGS: dict[str, str] = {
    "graphs": "## Графы",
    "definitions": "## Определения и тезисы",
    "tables": "## Таблицы",
    "dense_summary": "## Плотная сводка",
    "key_facts": "## Ключевые факты",
    "triples": "## Триплеты",
}


SUMMARY_SECTION_EMPTY_MESSAGES: dict[str, str] = {
    "graphs": "— Явных графовых структур в главе нет или модель вернула пустой ответ.",
    "definitions": "— Определения и тезисы не извлечены: модель вернула пустой ответ.",
    "tables": "— Таблицы не извлечены: модель вернула пустой ответ.",
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
    "definitions": """You extract definitions, theses, distinctions, rules, exceptions, and caveats from a chapter transcript.
Return only Markdown in the requested output language. Do not return JSON.

Rules:
1. Use only the transcript. Do not invent or import external knowledge.
2. Extract absolutely all definitions, explicit terms, distinctions, rules, claims, theses, exceptions, caveats, and classifications present in the chapter.
3. Do not extract or render table-like structures here. They belong only to the separate `## Таблицы` section.
4. Treat the transcript as imperfect speech: ignore obvious filler and ASR glitches, but never change meaning.
5. Include short source anchors as verbatim quotes where possible.
6. If a subsection has no items, keep the subsection and write `—` plus a short reason.

Strict format:
## Определения и тезисы

### Определения
- **<term>** — <definition>. *Источник:* «<short quote>»

### Тезисы
- <thesis>. *Источник:* «<short quote>»

### Исключения и оговорки
- <exception/caveat>. *Источник:* «<short quote>»

### Классификации и различия
- <classification/distinction/rule>. *Источник:* «<short quote>»

Output no text outside this section.""",
    "tables": """You extract every explicit table-like structure from a chapter transcript.
Return only Markdown in the requested output language. Do not return JSON.

Rules:
1. Use only the transcript. Do not invent rows, columns, categories, examples, or source anchors.
2. Extract all explicit tables, matrices, lists of attributes/columns, comparison tables, frameworks, enumerated structures, classifications, scoring sheets, checklists, and parameter sets that are clearer as tables.
3. Each independent table-like structure must be rendered as its own subsection with its own Markdown table. Never merge unrelated tables into one large table.
4. Preserve the source structure and column names when the transcript gives them. If the transcript lists only items without explicit columns, use a compact table with columns such as `Элемент`, `Описание/роль`, and `Источник`.
5. Include a short verbatim source anchor in the `Источник` column for every row where possible.
6. If the chapter contains no table-like structures, return the heading and `— Таблицы не обнаружены.`

Strict format:
## Таблицы

### <table title 1>
| <column 1> | <column 2> | Источник |
|---|---|---|
| ... | ... | «<short quote>» |

### <table title 2>
| <column 1> | <column 2> | Источник |
|---|---|---|
| ... | ... | «<short quote>» |

Repeat table subsections as needed. Output no text outside this section.""",
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


class OpenRouterProviderError(OpenRouterError):
    """Raised when the upstream provider returns a transient error inside a 200 body.

    OpenRouter sometimes wraps provider failures (e.g. 502 "Network connection
    lost") inside an HTTP 200 response with ``choices[0].error``.  Unlike the
    base :class:`OpenRouterError`, this exception is considered retriable by
    :func:`_is_retriable_error` so tenacity will attempt the call again.
    """


@dataclass
class _StreamCompletionParts:
    """Content and reasoning metadata collected from a streamed completion."""

    content_parts: list[str] = field(default_factory=list)
    reasoning_details: list[Any] = field(default_factory=list)
    reasoning_parts: list[str] = field(default_factory=list)
    finish_reasons: list[str] = field(default_factory=list)
    chunk_count: int = 0

    @property
    def raw_content(self) -> str:
        return "".join(self.content_parts).strip()

    @property
    def reasoning(self) -> str:
        return "".join(self.reasoning_parts).strip()

    @property
    def has_reasoning(self) -> bool:
        return bool(self.reasoning_details or self.reasoning)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _openrouter_extra_headers() -> dict[str, str]:
    return {
        "HTTP-Referer": _OPENROUTER_REFERER,
        "X-Title": _OPENROUTER_TITLE,
    }


def _openrouter_client(timeout: httpx.Timeout | None = None) -> AsyncOpenAI:
    return AsyncOpenAI(
        api_key=settings.openrouter.api_key or _OPENAI_API_KEY_PLACEHOLDER,
        base_url=_BASE_URL,
        default_headers=_openrouter_extra_headers(),
        timeout=timeout or _DEFAULT_TIMEOUT,
    )


def _schema_descriptions(configured: dict[str, str] | None) -> dict[str, str]:
    return dict(configured or {})


def _pydantic_json_schema(
    model: type[BaseModel],
    *,
    name: str,
    descriptions: dict[str, str] | None = None,
) -> dict:
    raw_schema = model.model_json_schema()
    definitions = raw_schema.get("$defs") if isinstance(raw_schema.get("$defs"), dict) else {}

    def resolve_ref(ref: str) -> dict[str, Any]:
        prefix = "#/$defs/"
        if not ref.startswith(prefix):
            raise OpenRouterError(f"Unsupported JSON schema reference generated for {model.__name__}: {ref}")
        definition_name = ref[len(prefix):]
        definition = definitions.get(definition_name)
        if not isinstance(definition, dict):
            raise OpenRouterError(
                f"JSON schema reference {ref!r} for {model.__name__} points to a missing definition."
            )
        return definition

    def strict_schema(node: Any) -> Any:
        if not isinstance(node, dict):
            if isinstance(node, list):
                return [strict_schema(item) for item in node]
            return node

        if "$ref" in node:
            resolved = dict(resolve_ref(str(node["$ref"])))
            for key, value in node.items():
                if key != "$ref":
                    resolved[key] = value
            return strict_schema(resolved)

        strict_node: dict[str, Any] = {}
        for key, value in node.items():
            if key in {"$defs", "title", "default"}:
                continue
            strict_node[key] = strict_schema(value)

        properties = strict_node.get("properties")
        if isinstance(properties, dict):
            strict_node["additionalProperties"] = False
            strict_node["required"] = list(properties.keys())

        return strict_node

    schema = strict_schema(raw_schema)
    configured_descriptions = _schema_descriptions(descriptions)

    def apply_description(node: dict[str, Any]) -> None:
        properties = node.get("properties")
        if isinstance(properties, dict):
            for property_name, property_schema in properties.items():
                if (
                    property_name in configured_descriptions
                    and isinstance(property_schema, dict)
                ):
                    property_schema["description"] = configured_descriptions[property_name]
                if isinstance(property_schema, dict):
                    apply_description(property_schema)

        items = node.get("items")
        if isinstance(items, dict):
            apply_description(items)

        definitions = node.get("$defs")
        if isinstance(definitions, dict):
            for definition in definitions.values():
                if isinstance(definition, dict):
                    apply_description(definition)

    apply_description(schema)
    return {
        "type": "json_schema",
        "json_schema": {
            "name": name,
            "strict": True,
            "schema": schema,
        },
    }


def _transcription_response_format() -> dict:
    s = settings.openrouter.transcription
    return _pydantic_json_schema(
        TranscriptionResponse,
        name=s.response_schema_name,
        descriptions=s.schema_descriptions,
    )


def _chapter_analysis_response_format() -> dict:
    s = settings.openrouter.summarization
    return _pydantic_json_schema(
        ChapterAnalysisResponse,
        name=s.chapter_analysis_schema_name,
        descriptions=s.chapter_analysis_schema_descriptions,
    )


def _chapter_tests_response_format() -> dict:
    s = settings.openrouter.summarization
    return _pydantic_json_schema(
        ChapterTestsResponse,
        name=s.chapter_tests_schema_name,
        descriptions=s.chapter_tests_schema_descriptions,
    )


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


def _normalize_provider_error(error: Any) -> dict[str, Any] | None:
    if error is None:
        return None
    if isinstance(error, dict):
        return error
    if isinstance(error, BaseModel):
        return error.model_dump()
    code = getattr(error, "code", None)
    message = getattr(error, "message", None)
    metadata = getattr(error, "metadata", None)
    if code is None and message is None and metadata is None:
        return {"message": str(error)}
    return {"code": code, "message": message or "", "metadata": metadata or {}}


def _raise_provider_error(label: str, error: Any) -> None:
    provider_error = _normalize_provider_error(error) or {}
    err_code = provider_error.get("code")
    err_msg = provider_error.get("message", "")
    metadata = provider_error.get("metadata") or {}
    err_type = metadata.get("error_type") if isinstance(metadata, dict) else ""
    logger.error(
        "%s response contains provider error (code=%r, type=%r, message=%r). Marking as retriable.",
        label,
        err_code,
        err_type,
        err_msg,
    )
    raise OpenRouterProviderError(
        f"Upstream provider returned an error in {label} "
        f"(code={err_code!r}, type={err_type!r}): {err_msg}"
    )


def _model_dump(model: BaseModel) -> dict:
    return model.model_dump(mode="json")


def _request_extra_body(
    *,
    reasoning_effort: str | None = None,
    extra_body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    merged_extra_body = dict(extra_body or {})
    if reasoning_effort:
        merged_extra_body["reasoning"] = {"effort": reasoning_effort}
    return merged_extra_body


def _transcription_extra_body() -> dict[str, Any]:
    transcription_settings = settings.openrouter.transcription
    provider: dict[str, Any] = {}
    if transcription_settings.provider_order:
        provider["order"] = list(transcription_settings.provider_order)
    if transcription_settings.provider_allow_fallbacks is not None:
        provider["allow_fallbacks"] = transcription_settings.provider_allow_fallbacks
    if not provider:
        return {}
    return {"provider": provider}


def _validate_structured_response(
    model: type[TStructuredModel],
    raw_content: str,
    label: str,
) -> TStructuredModel:
    result = _parse_strict_llm_json_object(raw_content, label)
    try:
        return model.model_validate(result)
    except ValidationError as exc:
        raise OpenRouterError(
            f"{label} JSON failed schema validation: {exc}. Raw result: {result!r}"
        ) from exc


def _completion_text(completion: Any, label: str) -> str:
    try:
        choice = completion.choices[0]
        provider_error = getattr(choice, "error", None)
        if provider_error:
            _raise_provider_error(label, provider_error)
        content = choice.message.content
    except OpenRouterProviderError:
        raise
    except (AttributeError, IndexError, TypeError) as exc:
        raise OpenRouterError(
            f"{label} response has unexpected structure: {completion!r}"
        ) from exc

    if content is None:
        finish_reason = getattr(choice, "finish_reason", None)
        raise OpenRouterError(
            f"{label} model returned null content (finish_reason={finish_reason!r})."
        )
    return str(content)


async def _create_chat_completion(
    *,
    label: str,
    model: str,
    messages: list[dict],
    timeout: httpx.Timeout | None = None,
    response_format: dict | None = None,
    max_tokens: int | None = None,
    temperature: float | None = None,
    top_p: float | None = None,
    reasoning_effort: str | None = None,
    extra_body: dict[str, Any] | None = None,
) -> Any:
    request: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": False,
    }
    if response_format is not None:
        request["response_format"] = response_format
    if max_tokens is not None:
        request["max_tokens"] = max_tokens
    if temperature is not None:
        request["temperature"] = temperature
    if top_p is not None:
        request["top_p"] = top_p

    merged_extra_body = _request_extra_body(
        reasoning_effort=reasoning_effort,
        extra_body=extra_body,
    )
    if merged_extra_body:
        request["extra_body"] = merged_extra_body

    logger.debug("%s request via OpenAI client: keys=%s", label, sorted(request.keys()))
    client = _openrouter_client(timeout=timeout)
    return await client.chat.completions.create(**request)


async def _complete_plain_text(
    *,
    label: str,
    model: str,
    messages: list[dict],
    max_tokens: int | None,
    temperature: float | None,
    timeout: httpx.Timeout | None = None,
) -> str:
    completion = await _create_chat_completion(
        label=label,
        model=model,
        messages=messages,
        max_tokens=max_tokens,
        temperature=temperature,
        timeout=timeout,
    )
    return _completion_text(completion, label).strip()


async def _complete_structured(
    *,
    label: str,
    model_name: str,
    messages: list[dict],
    response_model: type[TStructuredModel],
    response_format: dict,
    max_tokens: int | None,
    temperature: float | None,
    top_p: float | None = None,
    timeout: httpx.Timeout | None = None,
    reasoning_effort: str | None = None,
    extra_body: dict[str, Any] | None = None,
) -> TStructuredModel:
    completion = await _create_chat_completion(
        label=label,
        model=model_name,
        messages=messages,
        response_format=response_format,
        max_tokens=max_tokens,
        temperature=temperature,
        top_p=top_p,
        timeout=timeout,
        reasoning_effort=reasoning_effort,
        extra_body=extra_body,
    )
    raw_content = _completion_text(completion, label)
    return _validate_structured_response(response_model, raw_content, label)


def _stream_chunk_error(chunk: Any) -> Any:
    error = getattr(chunk, "error", None)
    if error:
        return error
    if isinstance(chunk, dict):
        return chunk.get("error")
    return None


def _stream_chunk_delta_content(chunk: Any) -> str:
    try:
        choices = chunk.choices
        if not choices:
            return ""
        delta = choices[0].delta
        content = getattr(delta, "content", None)
    except (AttributeError, IndexError, TypeError):
        if not isinstance(chunk, dict):
            return ""
        choices = chunk.get("choices") or []
        if not choices:
            return ""
        delta = choices[0].get("delta") or {}
        content = delta.get("content")
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                parts.append(str(item.get("text") or ""))
            else:
                parts.append(str(getattr(item, "text", None) or item))
        return "".join(parts)
    return str(content or "")


def _stream_chunk_choice(chunk: Any) -> Any:
    try:
        choices = chunk.choices
    except AttributeError:
        if not isinstance(chunk, dict):
            return None
        choices = chunk.get("choices") or []
    if not choices:
        return None
    try:
        return choices[0]
    except (IndexError, TypeError):
        return None


def _stream_choice_delta(choice: Any) -> Any:
    if choice is None:
        return None
    if isinstance(choice, dict):
        return choice.get("delta") or {}
    return getattr(choice, "delta", None)


def _stream_choice_finish_reason(choice: Any) -> str:
    if choice is None:
        return ""
    if isinstance(choice, dict):
        return str(choice.get("finish_reason") or "")
    return str(getattr(choice, "finish_reason", None) or "")


def _stream_delta_value(delta: Any, key: str) -> Any:
    if delta is None:
        return None
    if isinstance(delta, dict):
        return delta.get(key)
    return getattr(delta, key, None)


def _stream_chunk_delta_reasoning_details(chunk: Any) -> list[Any]:
    choice = _stream_chunk_choice(chunk)
    delta = _stream_choice_delta(choice)
    details = _stream_delta_value(delta, "reasoning_details")
    if details is None:
        return []
    if isinstance(details, list):
        return details
    return [details]


def _stream_chunk_delta_reasoning(chunk: Any) -> str:
    choice = _stream_chunk_choice(chunk)
    delta = _stream_choice_delta(choice)
    reasoning = _stream_delta_value(delta, "reasoning")
    if reasoning is None:
        reasoning = _stream_delta_value(delta, "reasoning_content")
    if isinstance(reasoning, list):
        parts: list[str] = []
        for item in reasoning:
            if isinstance(item, dict):
                parts.append(str(item.get("text") or item.get("summary") or ""))
            else:
                parts.append(str(getattr(item, "text", None) or item))
        return "".join(parts)
    return str(reasoning or "")


async def _collect_structured_stream(
    stream: Any,
    *,
    label: str,
) -> _StreamCompletionParts:
    parts = _StreamCompletionParts()
    async for chunk in stream:
        parts.chunk_count += 1
        error = _stream_chunk_error(chunk)
        if error:
            _raise_provider_error(label, error)

        choice = _stream_chunk_choice(chunk)
        finish_reason = _stream_choice_finish_reason(choice)
        if finish_reason:
            parts.finish_reasons.append(finish_reason)

        content = _stream_chunk_delta_content(chunk)
        if content:
            parts.content_parts.append(content)

        reasoning_details = _stream_chunk_delta_reasoning_details(chunk)
        if reasoning_details:
            parts.reasoning_details.extend(reasoning_details)

        reasoning = _stream_chunk_delta_reasoning(chunk)
        if reasoning:
            parts.reasoning_parts.append(reasoning)
    return parts


def _continuation_messages_from_stream_reasoning(
    messages: list[dict],
    parts: _StreamCompletionParts,
) -> list[dict]:
    assistant_message: dict[str, Any] = {"role": "assistant", "content": ""}
    if parts.reasoning_details:
        assistant_message["reasoning_details"] = parts.reasoning_details
    elif parts.reasoning:
        assistant_message["reasoning"] = parts.reasoning

    return [
        *messages,
        assistant_message,
        {
            "role": "user",
            "content": (
                "Continue from your preserved reasoning and produce the final answer now. "
                "Return only the JSON object that matches the requested response schema. "
                "Do not add markdown, commentary, or analysis."
            ),
        },
    ]


def _streaming_request(
    *,
    model_name: str,
    messages: list[dict],
    response_format: dict,
    max_tokens: int | None = None,
    temperature: float | None = None,
    top_p: float | None = None,
    reasoning_effort: str | None = None,
    extra_body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    request: dict[str, Any] = {
        "model": model_name,
        "messages": messages,
        "response_format": response_format,
        "stream": True,
    }
    if max_tokens is not None:
        request["max_tokens"] = max_tokens
    if temperature is not None:
        request["temperature"] = temperature
    if top_p is not None:
        request["top_p"] = top_p
    merged_extra_body = _request_extra_body(
        reasoning_effort=reasoning_effort,
        extra_body=extra_body,
    )
    if merged_extra_body:
        request["extra_body"] = merged_extra_body
    return request


async def _complete_structured_streaming(
    *,
    label: str,
    model_name: str,
    messages: list[dict],
    response_model: type[TStructuredModel],
    response_format: dict,
    timeout: httpx.Timeout,
    max_tokens: int | None = None,
    temperature: float | None = None,
    top_p: float | None = None,
    reasoning_effort: str | None = None,
    extra_body: dict[str, Any] | None = None,
) -> TStructuredModel:
    request = _streaming_request(
        model_name=model_name,
        messages=messages,
        response_format=response_format,
        max_tokens=max_tokens,
        temperature=temperature,
        top_p=top_p,
        reasoning_effort=reasoning_effort,
        extra_body=extra_body,
    )

    client = _openrouter_client(timeout=timeout)
    stream = await client.chat.completions.create(**request)
    streamed = await _collect_structured_stream(stream, label=label)

    if streamed.raw_content:
        return _validate_structured_response(response_model, streamed.raw_content, label)

    if streamed.has_reasoning:
        logger.warning(
            "%s streaming response produced reasoning without final content "
            "(chunks=%d, reasoning_details=%d, reasoning_chars=%d, finish_reasons=%s). "
            "Issuing one push-to-continue request with preserved reasoning metadata.",
            label,
            streamed.chunk_count,
            len(streamed.reasoning_details),
            len(streamed.reasoning),
            streamed.finish_reasons,
        )
        continuation_request = _streaming_request(
            model_name=model_name,
            messages=_continuation_messages_from_stream_reasoning(messages, streamed),
            response_format=response_format,
            max_tokens=max_tokens,
            temperature=temperature,
            top_p=top_p,
            reasoning_effort=reasoning_effort,
            extra_body=extra_body,
        )
        continuation_stream = await client.chat.completions.create(**continuation_request)
        continuation = await _collect_structured_stream(
            continuation_stream,
            label=f"{label} continuation",
        )
        if continuation.raw_content:
            return _validate_structured_response(
                response_model,
                continuation.raw_content,
                label,
            )
        raise OpenRouterProviderError(
            f"{label} continuation after reasoning-only stream produced no content "
            f"(chunks={continuation.chunk_count}, reasoning_details={len(continuation.reasoning_details)}, "
            f"reasoning_chars={len(continuation.reasoning)}, finish_reasons={continuation.finish_reasons})."
        )

    if not streamed.raw_content:
        raise OpenRouterProviderError(f"{label} streaming response produced no content.")
    return _validate_structured_response(response_model, streamed.raw_content, label)


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
    estimated_json_audio_bytes = len(audio_b64.encode("utf-8"))
    logger.info(
        "Prepared transcription payload for %s: wav_bytes=%d base64_bytes=%d model=%s stream=%s",
        path.name,
        audio_size,
        estimated_json_audio_bytes,
        settings.openrouter.transcription.model,
        settings.openrouter.transcription.stream,
    )

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

    transcription_settings = settings.openrouter.transcription
    extra_body = _transcription_extra_body()
    timeout = _calculate_transcription_timeout(audio_size)
    if transcription_settings.stream:
        parsed = await _complete_structured_streaming(
            label="Transcription",
            model_name=transcription_settings.model,
            messages=messages,
            response_model=TranscriptionResponse,
            response_format=_transcription_response_format(),
            timeout=timeout,
            max_tokens=transcription_settings.max_tokens,
            temperature=transcription_settings.temperature,
            top_p=transcription_settings.top_p,
            reasoning_effort=transcription_settings.thinking_effort,
            extra_body=extra_body,
        )
    else:
        parsed = await _complete_structured(
            label="Transcription",
            model_name=transcription_settings.model,
            messages=messages,
            response_model=TranscriptionResponse,
            response_format=_transcription_response_format(),
            timeout=timeout,
            max_tokens=transcription_settings.max_tokens,
            temperature=transcription_settings.temperature,
            top_p=transcription_settings.top_p,
            reasoning_effort=transcription_settings.thinking_effort,
            extra_body=extra_body,
        )

    result = _model_dump(parsed)
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

    parsed = await _complete_structured(
        label="Chapter analysis",
        model_name=s.model,
        messages=[
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
        response_model=ChapterAnalysisResponse,
        response_format=_chapter_analysis_response_format(),
        max_tokens=s.max_tokens,
        temperature=0.0,
        timeout=_DEFAULT_TIMEOUT,
    )
    result = _model_dump(parsed)

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
    openai.APIStatusError
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

    summary = await _complete_plain_text(
        label="Summarization",
        model=s.model,
        messages=[
            {"role": "system", "content": s.system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        max_tokens=s.max_tokens,
        temperature=s.temperature,
        timeout=_DEFAULT_TIMEOUT,
    )

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

    section_markdown = await _complete_plain_text(
        label=f"Summary section {section_key}",
        model=s.model,
        messages=[
            {"role": "system", "content": SUMMARY_SECTION_SYSTEM_PROMPTS[section_key]},
            {"role": "user", "content": user_prompt},
        ],
        max_tokens=s.max_tokens,
        temperature=s.temperature,
        timeout=_DEFAULT_TIMEOUT,
    )
    if not section_markdown:
        logger.warning(
            "Summary section '%s' response content is empty/null; using fallback Markdown section.",
            section_key,
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
async def summarize_chapter_sections(text: str, progress_callback=None) -> dict:
    """Generate all chapter summary sections as strict Markdown.

    The six calls are launched in parallel because they read the same chapter
    transcript and have no dependencies on each other. Any failed section fails
    the whole attempt, keeping the existing processing error semantics.
    """
    transcript = str(text or "").strip()
    if not transcript:
        raise ValueError("Cannot summarize an empty chapter transcription.")

    async def run_section(section_key: str) -> str:
        value = await _summarize_chapter_section(transcript, section_key)
        if progress_callback is not None:
            progress_callback(section_key)
        return value

    section_values = await asyncio.gather(
        *(run_section(section_key) for section_key in SUMMARY_SECTION_ORDER)
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

    parsed = await _complete_structured(
        label="Chapter tests",
        model_name=s.model,
        messages=[
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
        response_model=ChapterTestsResponse,
        response_format=_chapter_tests_response_format(),
        max_tokens=s.max_tokens,
        temperature=0.2,
        timeout=_DEFAULT_TIMEOUT,
    )
    result = _model_dump(parsed)

    logger.info("Chapter test generation complete: %d question(s)", len(result["questions"]))
    return result
