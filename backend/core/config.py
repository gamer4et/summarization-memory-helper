"""Application configuration.

All non-secret settings are loaded from ``config/settings.yaml``. The only
environment/.env override is ``OPENROUTER_API_KEY``.
"""

import os
from pathlib import Path
from typing import Any
from typing import Literal

import yaml

from pydantic import BaseModel, ConfigDict, Field, field_validator


_SETTINGS_FILE = Path("config/settings.yaml")
_ENV_FILE = Path(".env")


class AudioSettings(BaseModel):
    """VAD and audio pipeline configuration."""

    model_config = ConfigDict(extra="ignore")

    sample_rate: int = 16000
    frame_duration_ms: int = 30
    vad_aggressiveness: int = 2
    min_speech_duration_ms: int = 250
    max_silence_ms: int = 1000
    storage_dir: Path = Path("./data/audio")
    raw_storage_dir: Path = Path("./data/raw_audio")
    vad_storage_dir: Path = Path("./data/vad_audio")
    decoded_storage_dir: Path = Path("./data/decoded_audio")
    min_transcription_audio_bytes: int = 44 + 32_000

    @field_validator("vad_aggressiveness")
    @classmethod
    def validate_aggressiveness(cls, v: int) -> int:
        if v not in (0, 1, 2, 3):
            raise ValueError("vad_aggressiveness must be 0, 1, 2, or 3")
        return v

    @field_validator("frame_duration_ms")
    @classmethod
    def validate_frame_duration(cls, v: int) -> int:
        if v not in (10, 20, 30):
            raise ValueError("frame_duration_ms must be 10, 20, or 30")
        return v


class OpenRouterTranscriptionSettings(BaseModel):
    """Settings for the STT (speech-to-text) call to OpenRouter."""

    model_config = ConfigDict(extra="ignore")

    model: str = "google/gemini-3.1-pro-preview"
    provider_order: list[str] = Field(default_factory=list)
    provider_allow_fallbacks: bool | None = None
    default_language: str = "ru"
    stream: bool = True
    max_tokens: int | None = None
    temperature: float | None = None
    top_p: float | None = None
    thinking_effort: Literal["xhigh", "high", "medium", "low", "minimal", "none"] | None = None
    response_schema_name: str = "audio_transcription"
    schema_descriptions: dict[str, str] = Field(
        default_factory=lambda: {
            "full_transcription": "Complete transcription of all speech in the audio fragment.",
        }
    )

    @field_validator("max_tokens", "temperature", "top_p", mode="before")
    @classmethod
    def normalize_optional_generation_setting(cls, v: Any) -> Any:
        if v is None:
            return None
        if isinstance(v, str) and not v.strip():
            return None
        return v

    @field_validator("provider_order", mode="before")
    @classmethod
    def normalize_provider_order(cls, v: Any) -> list[str] | Any:
        if v is None:
            return []
        if isinstance(v, str):
            return [item.strip() for item in v.split(",") if item.strip()]
        if isinstance(v, (list, tuple)):
            return [str(item).strip() for item in v if str(item).strip()]
        return v

    @field_validator("provider_allow_fallbacks", mode="before")
    @classmethod
    def normalize_provider_allow_fallbacks(cls, v: Any) -> Any:
        if v is None:
            return None
        if isinstance(v, str) and not v.strip():
            return None
        return v

    @field_validator("max_tokens")
    @classmethod
    def validate_max_tokens(cls, v: int | None) -> int | None:
        if v is not None and v <= 0:
            raise ValueError("max_tokens must be greater than 0 when set")
        return v

    @field_validator("temperature")
    @classmethod
    def validate_temperature(cls, v: float | None) -> float | None:
        if v is not None and v < 0:
            raise ValueError("temperature must be greater than or equal to 0 when set")
        return v

    @field_validator("top_p")
    @classmethod
    def validate_top_p(cls, v: float | None) -> float | None:
        if v is not None and not 0 <= v <= 1:
            raise ValueError("top_p must be between 0 and 1 when set")
        return v

    @field_validator("thinking_effort", mode="before")
    @classmethod
    def normalize_thinking_effort(cls, v: Any) -> str | None:
        if v is None:
            return None
        value = str(v).strip().lower()
        if not value:
            return None
        return value


class OpenRouterSummarizationSettings(BaseModel):
    """Settings for the LLM summarization call to OpenRouter."""

    model_config = ConfigDict(extra="ignore")

    model: str = ""
    max_tokens: int = 0
    temperature: float = 0.0
    system_prompt: str = ""
    default_modes: str = "dense_summary, key_facts, triples, quotes, categories"
    language: str = "auto"
    density_iterations: int = 3
    chapter_analysis_schema_name: str = "chapter_analysis"
    chapter_analysis_schema_descriptions: dict[str, str] = Field(
        default_factory=lambda: {
            "chapters": "Ordered chapters detected from explicit spoken chapter markers only.",
            "chapter_number": "One-based chapter number in transcript order.",
            "title": "Chapter title inferred from the explicit spoken marker, or a safe fallback.",
            "transcription": "Transcript text that belongs to this chapter.",
        }
    )
    chapter_tests_schema_name: str = "chapter_tests"
    chapter_tests_schema_descriptions: dict[str, str] = Field(
        default_factory=lambda: {
            "questions": "Multiple-choice questions generated from the chapter transcription.",
            "question": "Question text.",
            "options": "Exactly four answer options.",
            "correct_option_index": "Zero-based index of the only correct option.",
            "explanation": "Brief explanation of why the correct answer follows from the chapter.",
            "option_explanations": "Per-option explanations; use an empty string for the correct option.",
            "difficulty": "Question difficulty label: easy, medium, or hard.",
            "concept_tags": "Short conceptual tags covered by the question.",
        }
    )


class OpenRouterSettings(BaseModel):
    """OpenRouter API credentials and sub-settings."""

    model_config = ConfigDict(extra="ignore")

    # Secret: populated only from OPENROUTER_API_KEY in environment/.env.
    api_key: str = ""
    transcription: OpenRouterTranscriptionSettings = Field(
        default_factory=OpenRouterTranscriptionSettings
    )
    summarization: OpenRouterSummarizationSettings = Field(
        default_factory=OpenRouterSummarizationSettings
    )


class DatabaseSettings(BaseModel):
    """Database connection settings."""

    model_config = ConfigDict(extra="ignore")

    url: str = "sqlite:///./data/app.db"


class Settings(BaseModel):
    """Root settings object loaded from YAML plus the OpenRouter API key."""

    model_config = ConfigDict(extra="ignore")

    app_name: str = "Book Summarizer"
    debug: bool = False

    audio: AudioSettings = Field(default_factory=AudioSettings)
    openrouter: OpenRouterSettings = Field(default_factory=OpenRouterSettings)
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)


def _load_yaml_settings() -> dict[str, Any]:
    if not _SETTINGS_FILE.exists():
        return {}
    with _SETTINGS_FILE.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Settings file {_SETTINGS_FILE} must contain a YAML mapping.")
    return data


def _read_env_file_value(key: str) -> str:
    if not _ENV_FILE.exists():
        return ""
    for raw_line in _ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        if name.strip() != key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        return value
    return ""


def _openrouter_api_key() -> str:
    return os.environ.get("OPENROUTER_API_KEY", "") or _read_env_file_value(
        "OPENROUTER_API_KEY"
    )


def _build_settings() -> Settings:
    data = _load_yaml_settings()

    app_cfg = data.get("app") or {}
    if not isinstance(app_cfg, dict):
        raise ValueError("settings.yaml field 'app' must be a mapping.")

    openrouter_cfg = dict(data.get("openrouter") or {})
    openrouter_cfg["api_key"] = _openrouter_api_key()

    return Settings(
        app_name=app_cfg.get("name", "Book Summarizer"),
        debug=app_cfg.get("debug", False),
        audio=data.get("audio") or {},
        openrouter=openrouter_cfg,
        database=data.get("database") or {},
    )


# Singleton — import this everywhere.
settings = _build_settings()
