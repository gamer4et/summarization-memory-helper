import tempfile
import unittest
from pathlib import Path
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import patch

from backend.services import processor
from backend.services.chapter_parser import parse_llm_chapters
from backend.services.openrouter_client import (
    OpenRouterError,
    TRANSCRIPTION_RESPONSE_FORMAT,
    _parse_strict_llm_json_object,
)


class TranscriptionChunkProcessingTests(IsolatedAsyncioTestCase):
    async def test_transcription_chunks_are_concatenated_not_returned_as_chapters(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            audio_path = Path(tmpdir) / "recording.wav"
            audio_path.write_bytes(b"0" * processor._MIN_AUDIO_BYTES)

            chunk_dir = Path(tmpdir) / "recording_chunks"
            chunk_dir.mkdir()
            for name in ("0001.wav", "0002.wav"):
                (chunk_dir / name).write_bytes(b"1" * processor._MIN_AUDIO_BYTES)

            async def fake_transcribe_audio(chunk_path, language="ru"):
                chunk_index = chunk_path.stem.lstrip("0") or "0"
                return {
                    "full_transcription": f"Текст чанка {chunk_index}",
                    # Simulate an old/buggy transcription response shape. The
                    # processor must ignore per-audio-chunk chapters entirely.
                    "chapters": [
                        {
                            "chapter_number": int(chunk_index),
                            "title": f"Глава {chunk_index}",
                            "transcription": f"Не должен стать главой {chunk_index}",
                        }
                    ],
                }

            with patch.object(processor, "transcribe_audio", fake_transcribe_audio):
                result = await processor._transcribe_recording_audio(audio_path, language="ru")

        self.assertEqual(
            result,
            {"full_transcription": "Текст чанка 1\n\nТекст чанка 2"},
        )
        self.assertNotIn("chapters", result)


class ChapterParserTests(TestCase):
    def test_parse_llm_chapters_preserves_global_analysis_chapters(self):
        chapters = parse_llm_chapters(
            [
                {
                    "chapter_number": 1,
                    "title": "Глава первая",
                    "transcription": "Первый глобально найденный раздел.",
                },
                {
                    "chapter_number": 2,
                    "title": "Глава вторая",
                    "transcription": "Второй глобально найденный раздел.",
                },
            ]
        )

        self.assertEqual(len(chapters), 2)
        self.assertEqual(chapters[0]["title"], "Глава первая")
        self.assertEqual(chapters[1]["chapter_number"], 2)

    def test_parse_llm_chapters_filters_empty_analysis_sections(self):
        chapters = parse_llm_chapters(
            [
                {"chapter_number": 1, "title": "Пустой раздел", "transcription": "  "},
                {
                    "chapter_number": 2,
                    "title": "Full recording",
                    "transcription": "Полная склеенная транскрибация.",
                },
            ]
        )

        self.assertEqual(
            chapters,
            [
                {
                    "chapter_number": 2,
                    "title": "Full recording",
                    "transcription": "Полная склеенная транскрибация.",
                }
            ],
        )


class OpenRouterStructuredOutputTests(TestCase):
    def test_transcription_response_format_uses_strict_json_schema(self):
        self.assertEqual(TRANSCRIPTION_RESPONSE_FORMAT["type"], "json_schema")
        json_schema = TRANSCRIPTION_RESPONSE_FORMAT["json_schema"]

        self.assertEqual(json_schema["name"], "audio_transcription")
        self.assertTrue(json_schema["strict"])
        self.assertEqual(json_schema["schema"]["required"], ["full_transcription"])
        self.assertFalse(json_schema["schema"]["additionalProperties"])

    def test_strict_llm_json_parser_rejects_repeated_trailing_braces(self):
        raw_content = '{"full_transcription": "Глава вторая"}\n}\n}\n}'

        with self.assertRaises(OpenRouterError):
            _parse_strict_llm_json_object(raw_content, "Transcription")


if __name__ == "__main__":
    unittest.main()
