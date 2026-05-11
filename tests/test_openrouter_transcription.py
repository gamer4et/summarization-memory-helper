import json
import logging
import wave
from types import SimpleNamespace

import pytest

from backend.core.config import OpenRouterTranscriptionSettings
from backend.services import openrouter_client


def _write_wav(path) -> None:
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16_000)
        wav.writeframes(b"\x00\x00" * 160)


class _FakeStream:
    def __init__(self, chunks):
        self.chunks = chunks

    def __aiter__(self):
        return self._iterate()

    async def _iterate(self):
        for chunk in self.chunks:
            yield chunk


class _FakeChatCompletions:
    def __init__(self):
        self.requests: list[dict] = []
        self.stream_chunks_queue: list[list[object]] = []
        self.stream_chunks = [
            SimpleNamespace(choices=[]),
            SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        delta=SimpleNamespace(
                            content=json.dumps(
                                {"full_transcription": "Тестовая транскрибация."}
                            )
                        )
                    )
                ]
            ),
        ]
        self.completion_content = json.dumps(
            {"full_transcription": "Тестовая транскрибация."}
        )

    async def create(self, **kwargs):
        self.requests.append(kwargs)
        if kwargs.get("stream"):
            if self.stream_chunks_queue:
                return _FakeStream(self.stream_chunks_queue.pop(0))
            return _FakeStream(self.stream_chunks)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=self.completion_content),
                    finish_reason="stop",
                )
            ]
        )


class _FakeAsyncOpenAI:
    instances: list["_FakeAsyncOpenAI"] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.chat_completions = _FakeChatCompletions()
        self.chat = SimpleNamespace(completions=self.chat_completions)
        self.instances.append(self)


@pytest.fixture(autouse=True)
def fake_openrouter_client(monkeypatch):
    _FakeAsyncOpenAI.instances = []
    monkeypatch.setattr(openrouter_client, "AsyncOpenAI", _FakeAsyncOpenAI)
    monkeypatch.setattr(openrouter_client.settings.openrouter, "api_key", "test-key")
    transcription_settings = openrouter_client.settings.openrouter.transcription
    monkeypatch.setattr(transcription_settings, "model", "google/gemini-3.1-pro-preview")
    monkeypatch.setattr(transcription_settings, "provider_order", [])
    monkeypatch.setattr(transcription_settings, "provider_allow_fallbacks", None)
    monkeypatch.setattr(transcription_settings, "stream", True)
    monkeypatch.setattr(transcription_settings, "max_tokens", None)
    monkeypatch.setattr(transcription_settings, "temperature", None)
    monkeypatch.setattr(transcription_settings, "top_p", None)
    monkeypatch.setattr(transcription_settings, "thinking_effort", None)


def _last_request() -> dict:
    return _FakeAsyncOpenAI.instances[-1].chat_completions.requests[-1]


def test_transcription_settings_normalizes_provider_order() -> None:
    settings = OpenRouterTranscriptionSettings(
        provider_order="google-vertex/global, openai",
        provider_allow_fallbacks="",
    )

    assert settings.provider_order == ["google-vertex/global", "openai"]
    assert settings.provider_allow_fallbacks is None


def test_structured_response_schema_is_openrouter_strict() -> None:
    schema = openrouter_client._chapter_analysis_response_format()["json_schema"]["schema"]

    assert "$defs" not in schema
    assert "title" not in schema
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["chapters"]
    chapter_item_schema = schema["properties"]["chapters"]["items"]
    assert "$ref" not in chapter_item_schema
    assert chapter_item_schema["additionalProperties"] is False
    assert chapter_item_schema["required"] == ["chapter_number", "title", "transcription"]
    assert chapter_item_schema["properties"]["transcription"]["description"] == (
        "Transcript text that belongs to this chapter."
    )


def test_chapter_analysis_accepts_missing_title_from_provider() -> None:
    parsed = openrouter_client.ChapterAnalysisResponse.model_validate(
        {
            "chapters": [
                {
                    "chapter_number": 1,
                    "transcription": "Chapter text without a generated title.",
                }
            ]
        }
    )

    assert parsed.chapters[0].title == ""
    assert parsed.chapters[0].transcription == "Chapter text without a generated title."


@pytest.mark.asyncio
async def test_transcribe_audio_uses_openai_client_streaming(caplog, tmp_path):
    audio_path = tmp_path / "audio.wav"
    _write_wav(audio_path)
    caplog.set_level(logging.INFO, logger=openrouter_client.__name__)

    result = await openrouter_client.transcribe_audio(audio_path)

    assert result == {"full_transcription": "Тестовая транскрибация."}
    client_kwargs = _FakeAsyncOpenAI.instances[0].kwargs
    assert str(client_kwargs["base_url"]) == "https://openrouter.ai/api/v1"
    assert client_kwargs["api_key"] == "test-key"
    assert client_kwargs["default_headers"]["HTTP-Referer"]
    request = _last_request()
    assert request["stream"] is True
    assert request["model"] == "google/gemini-3.1-pro-preview"
    assert request["response_format"]["type"] == "json_schema"
    assert request["response_format"]["json_schema"]["name"] == "audio_transcription"
    assert request["messages"][1]["content"][0]["type"] == "input_audio"
    assert request["messages"][1]["content"][0]["input_audio"]["format"] == "wav"
    assert "Prepared transcription payload" in caplog.text
    assert "wav_bytes=" in caplog.text
    assert "base64_bytes=" in caplog.text


@pytest.mark.asyncio
async def test_transcribe_audio_sends_configured_provider_routing(
    monkeypatch,
    tmp_path,
):
    audio_path = tmp_path / "audio.wav"
    _write_wav(audio_path)
    transcription_settings = openrouter_client.settings.openrouter.transcription
    monkeypatch.setattr(transcription_settings, "provider_order", ["google-vertex/global"])
    monkeypatch.setattr(transcription_settings, "provider_allow_fallbacks", False)

    await openrouter_client.transcribe_audio(audio_path)

    request = _last_request()
    assert request["extra_body"]["provider"] == {
        "order": ["google-vertex/global"],
        "allow_fallbacks": False,
    }


@pytest.mark.asyncio
async def test_transcribe_audio_merges_provider_routing_and_reasoning(
    monkeypatch,
    tmp_path,
):
    audio_path = tmp_path / "audio.wav"
    _write_wav(audio_path)
    transcription_settings = openrouter_client.settings.openrouter.transcription
    monkeypatch.setattr(transcription_settings, "provider_order", ["google-vertex/global"])
    monkeypatch.setattr(transcription_settings, "provider_allow_fallbacks", False)
    monkeypatch.setattr(transcription_settings, "thinking_effort", "low")

    await openrouter_client.transcribe_audio(audio_path)

    request = _last_request()
    assert request["extra_body"] == {
        "provider": {
            "order": ["google-vertex/global"],
            "allow_fallbacks": False,
        },
        "reasoning": {"effort": "low"},
    }


@pytest.mark.asyncio
async def test_transcribe_audio_maps_thinking_effort_to_reasoning(monkeypatch, tmp_path):
    audio_path = tmp_path / "audio.wav"
    _write_wav(audio_path)
    monkeypatch.setattr(
        openrouter_client.settings.openrouter.transcription,
        "thinking_effort",
        "low",
    )

    await openrouter_client.transcribe_audio(audio_path)

    request = _last_request()
    assert request["extra_body"]["reasoning"] == {"effort": "low"}


@pytest.mark.asyncio
async def test_transcribe_audio_sends_configured_generation_settings(monkeypatch, tmp_path):
    audio_path = tmp_path / "audio.wav"
    _write_wav(audio_path)
    transcription_settings = openrouter_client.settings.openrouter.transcription
    monkeypatch.setattr(transcription_settings, "max_tokens", 4096)
    monkeypatch.setattr(transcription_settings, "temperature", 0.2)
    monkeypatch.setattr(transcription_settings, "top_p", 0.9)

    await openrouter_client.transcribe_audio(audio_path)

    request = _last_request()
    assert request["max_tokens"] == 4096
    assert request["temperature"] == 0.2
    assert request["top_p"] == 0.9


@pytest.mark.asyncio
async def test_transcribe_audio_omits_unset_generation_settings(tmp_path):
    audio_path = tmp_path / "audio.wav"
    _write_wav(audio_path)

    await openrouter_client.transcribe_audio(audio_path)

    request = _last_request()
    assert "max_tokens" not in request
    assert "temperature" not in request
    assert "top_p" not in request
    assert "extra_body" not in request


@pytest.mark.asyncio
async def test_transcribe_audio_can_use_non_streaming_openai_client(monkeypatch, tmp_path):
    audio_path = tmp_path / "audio.wav"
    _write_wav(audio_path)
    monkeypatch.setattr(openrouter_client.settings.openrouter.transcription, "stream", False)

    await openrouter_client.transcribe_audio(audio_path)

    request = _last_request()
    assert request["stream"] is False
    assert request["response_format"]["type"] == "json_schema"


@pytest.mark.asyncio
async def test_transcribe_audio_pushes_reasoning_only_stream_to_continue(tmp_path):
    audio_path = tmp_path / "audio.wav"
    _write_wav(audio_path)
    reasoning_details = [
        {
            "type": "reasoning.encrypted",
            "data": "opaque-google-thought-signature",
            "format": "google-gemini-v1",
            "index": 0,
        }
    ]

    original_init = _FakeAsyncOpenAI.__init__

    def init_with_reasoning_only_then_answer(self, **kwargs):
        original_init(self, **kwargs)
        self.chat_completions.stream_chunks_queue = [
            [
                SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            delta=SimpleNamespace(reasoning_details=reasoning_details),
                            finish_reason="stop",
                        )
                    ]
                )
            ],
            [
                SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            delta=SimpleNamespace(
                                content=json.dumps(
                                    {"full_transcription": "Ответ после продолжения."}
                                )
                            ),
                            finish_reason="stop",
                        )
                    ]
                )
            ],
        ]

    _FakeAsyncOpenAI.__init__ = init_with_reasoning_only_then_answer
    try:
        result = await openrouter_client.transcribe_audio(audio_path)
    finally:
        _FakeAsyncOpenAI.__init__ = original_init

    assert result == {"full_transcription": "Ответ после продолжения."}
    requests = _FakeAsyncOpenAI.instances[-1].chat_completions.requests
    assert len(requests) == 2
    continuation_messages = requests[1]["messages"]
    assert continuation_messages[-2] == {
        "role": "assistant",
        "content": "",
        "reasoning_details": reasoning_details,
    }
    assert "Continue from your preserved reasoning" in continuation_messages[-1]["content"]
    assert continuation_messages[-1]["role"] == "user"


@pytest.mark.asyncio
async def test_transcribe_audio_keeps_empty_stream_retriable(tmp_path):
    audio_path = tmp_path / "audio.wav"
    _write_wav(audio_path)

    original_init = _FakeAsyncOpenAI.__init__

    def init_with_empty_stream(self, **kwargs):
        original_init(self, **kwargs)
        self.chat_completions.stream_chunks = [SimpleNamespace(choices=[])]

    _FakeAsyncOpenAI.__init__ = init_with_empty_stream
    try:
        with pytest.raises(openrouter_client.OpenRouterError, match="no content"):
            await openrouter_client.transcribe_audio(audio_path)
    finally:
        _FakeAsyncOpenAI.__init__ = original_init

    assert len(_FakeAsyncOpenAI.instances) == 3
    assert all(
        len(instance.chat_completions.requests) == 1
        for instance in _FakeAsyncOpenAI.instances
    )


@pytest.mark.asyncio
async def test_streaming_provider_error_is_retriable():
    stream = _FakeStream(
        [
            SimpleNamespace(
                error={"code": "server_error", "message": "Provider disconnected"},
                choices=[],
            )
        ]
    )

    with pytest.raises(openrouter_client.OpenRouterProviderError) as exc_info:
        async for chunk in stream:
            error = openrouter_client._stream_chunk_error(chunk)
            if error:
                openrouter_client._raise_provider_error("Transcription", error)

    assert openrouter_client._is_retriable_error(exc_info.value)


@pytest.mark.asyncio
async def test_transcribe_audio_rejects_streamed_json_with_missing_required_key(
    tmp_path,
):
    audio_path = tmp_path / "audio.wav"
    _write_wav(audio_path)
    original_init = _FakeAsyncOpenAI.__init__

    def init_with_bad_json(self, **kwargs):
        original_init(self, **kwargs)
        self.chat_completions.stream_chunks = [
            SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        delta=SimpleNamespace(content=json.dumps({"unexpected": "value"}))
                    )
                ]
            )
        ]

    _FakeAsyncOpenAI.__init__ = init_with_bad_json
    try:
        with pytest.raises(openrouter_client.OpenRouterError, match="schema validation"):
            await openrouter_client.transcribe_audio(audio_path)
    finally:
        _FakeAsyncOpenAI.__init__ = original_init
