import asyncio
from datetime import datetime

import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker

from backend.api.recordings import build_recording_detail
from backend.core.database import Base
from backend.models.orm import Book, Chapter, Recording, Summary, Transcription
from backend.services import openrouter_client, processor


@pytest.fixture()
def db_session(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'multi_section_summary.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)


def _seed_ready_recording(db, audio_path: str) -> int:
    book = Book(title="Book", author="Author")
    db.add(book)
    db.flush()
    recording = Recording(
        book_id=book.id,
        audio_file_path=audio_path,
        status="ready",
        duration_seconds=1,
    )
    db.add(recording)
    db.commit()
    return recording.id


def test_assemble_summary_markdown_uses_fixed_section_order():
    assembled = openrouter_client.assemble_summary_markdown(
        {
            "triples": "## Триплеты\n\n| Субъект | Отношение | Объект | Источник |",
            "graphs": "## Графы\n\n```mermaid\nflowchart LR\n```",
            "key_facts": "## Ключевые факты\n\n1. Факт",
            "definitions": "## Определения, таблицы и тезисы\n\n### Определения\n- X",
            "dense_summary": "## Плотная сводка\n\nСводка",
        }
    )

    assert assembled.index("## Графы") < assembled.index("## Определения")
    assert assembled.index("## Определения") < assembled.index("## Плотная сводка")
    assert assembled.index("## Плотная сводка") < assembled.index("## Ключевые факты")
    assert assembled.index("## Ключевые факты") < assembled.index("## Триплеты")


def test_empty_summary_section_markdown_returns_valid_section():
    assert openrouter_client._empty_summary_section_markdown("graphs").startswith("## Графы")
    triples = openrouter_client._empty_summary_section_markdown("triples")
    assert triples.startswith("## Триплеты")
    assert "| Субъект | Отношение | Объект | Источник |" in triples
    assert "модель вернула пустой ответ" in triples


@pytest.mark.asyncio
async def test_summarize_chapter_sections_runs_stage_calls_in_parallel(monkeypatch):
    started: list[str] = []
    release = asyncio.Event()

    async def fake_summarize_section(text: str, section_key: str) -> str:
        assert text == "chapter text"
        started.append(section_key)
        if len(started) == len(openrouter_client.SUMMARY_SECTION_ORDER):
            release.set()
        await release.wait()
        return f"{openrouter_client.SUMMARY_SECTION_HEADINGS[section_key]}\n\n{section_key}"

    monkeypatch.setattr(openrouter_client, "_summarize_chapter_section", fake_summarize_section)

    result = await openrouter_client.summarize_chapter_sections("chapter text")

    assert started == list(openrouter_client.SUMMARY_SECTION_ORDER)
    assert set(result["sections"]) == set(openrouter_client.SUMMARY_SECTION_ORDER)
    assert "## Графы" in result["summary_text"]
    assert "## Триплеты" in result["summary_text"]


def test_summary_out_exposes_summary_sections(db_session):
    book = Book(title="Book", author="Author")
    db_session.add(book)
    db_session.flush()
    recording = Recording(
        book_id=book.id,
        audio_file_path="data/vad_audio/1.wav",
        status="completed",
        duration_seconds=1,
        processed_at=datetime.utcnow(),
    )
    db_session.add(recording)
    db_session.flush()
    chapter = Chapter(
        recording_id=recording.id,
        chapter_number=1,
        title="Chapter",
        start_offset_ms=0,
        end_offset_ms=0,
    )
    db_session.add(chapter)
    db_session.flush()
    db_session.add(Transcription(chapter_id=chapter.id, raw_text="Transcript"))
    db_session.add(
        Summary(
            chapter_id=chapter.id,
            summary_text="assembled",
            graphs_markdown="graphs",
            definitions_markdown="definitions",
            dense_summary_markdown="dense",
            key_facts_markdown="facts",
            triples_markdown="triples",
            model_used="test-model",
        )
    )
    db_session.commit()
    db_session.refresh(recording)

    detail = build_recording_detail(recording)

    assert detail.chapters[0].summary.summary_text == "assembled"
    assert detail.chapters[0].summary.summary_sections == {
        "graphs": "graphs",
        "definitions": "definitions",
        "dense_summary": "dense",
        "key_facts": "facts",
        "triples": "triples",
    }


@pytest.mark.asyncio
async def test_process_recording_persists_multi_section_summary(monkeypatch, tmp_path, db_session):
    audio_path = tmp_path / "audio.wav"
    audio_path.write_bytes(b"0" * 100)
    recording_id = _seed_ready_recording(db_session, str(audio_path))

    monkeypatch.setattr(processor, "_MIN_AUDIO_BYTES", 1)

    async def fake_transcribe_audio(path, language="ru", previous_context=""):
        return {"full_transcription": "Chapter text about concepts."}

    async def fake_analyze_chapters(text, language="ru"):
        return {
            "chapters": [
                {
                    "chapter_number": 1,
                    "title": "Full recording",
                    "transcription": text,
                }
            ]
        }

    async def fake_summarize_sections(text):
        assert text == "Chapter text about concepts."
        sections = {
            "graphs": "## Графы\n\ngraphs",
            "definitions": "## Определения, таблицы и тезисы\n\ndefinitions",
            "dense_summary": "## Плотная сводка\n\ndense",
            "key_facts": "## Ключевые факты\n\nfacts",
            "triples": "## Триплеты\n\ntriples",
        }
        return {
            "summary_text": openrouter_client.assemble_summary_markdown(sections),
            "sections": sections,
        }

    monkeypatch.setattr(processor, "transcribe_audio", fake_transcribe_audio)
    monkeypatch.setattr(processor, "analyze_transcription_chapters", fake_analyze_chapters)
    monkeypatch.setattr(processor, "summarize_chapter_sections", fake_summarize_sections)

    recording = await processor.process_recording(db_session, recording_id, language="ru")

    assert recording.status == "completed"
    chapter = recording.chapters[0]
    assert chapter.summary.graphs_markdown == "## Графы\n\ngraphs"
    assert chapter.summary.definitions_markdown == "## Определения, таблицы и тезисы\n\ndefinitions"
    assert chapter.summary.dense_summary_markdown == "## Плотная сводка\n\ndense"
    assert chapter.summary.key_facts_markdown == "## Ключевые факты\n\nfacts"
    assert chapter.summary.triples_markdown == "## Триплеты\n\ntriples"
    assert "## Графы" in chapter.summary.summary_text
    assert "## Триплеты" in chapter.summary.summary_text


@pytest.mark.asyncio
async def test_process_recording_generates_summaries_before_chapter_flush(monkeypatch, tmp_path, db_session):
    audio_path = tmp_path / "audio.wav"
    audio_path.write_bytes(b"0" * 100)
    recording_id = _seed_ready_recording(db_session, str(audio_path))

    monkeypatch.setattr(processor, "_MIN_AUDIO_BYTES", 1)

    async def fake_transcribe_audio(path, language="ru", previous_context=""):
        return {"full_transcription": "Chapter one text. Chapter two text."}

    async def fake_analyze_chapters(text, language="ru"):
        return {
            "chapters": [
                {"chapter_number": 1, "title": "One", "transcription": "Chapter one text."},
                {"chapter_number": 2, "title": "Two", "transcription": "Chapter two text."},
            ]
        }

    async def fake_summarize_sections(text):
        assert db_session.query(Chapter).count() == 0
        sections = {
            "graphs": f"## Графы\n\n{text}",
            "definitions": "## Определения, таблицы и тезисы\n\ndefinitions",
            "dense_summary": "## Плотная сводка\n\ndense",
            "key_facts": "## Ключевые факты\n\nfacts",
            "triples": "## Триплеты\n\ntriples",
        }
        return {
            "summary_text": openrouter_client.assemble_summary_markdown(sections),
            "sections": sections,
        }

    monkeypatch.setattr(processor, "transcribe_audio", fake_transcribe_audio)
    monkeypatch.setattr(processor, "analyze_transcription_chapters", fake_analyze_chapters)
    monkeypatch.setattr(processor, "summarize_chapter_sections", fake_summarize_sections)

    recording = await processor.process_recording(db_session, recording_id, language="ru")

    assert recording.status == "completed"
    assert len(recording.chapters) == 2


def test_summary_model_contains_section_columns():
    columns = {column["name"] for column in inspect(Summary).columns}

    assert {
        "graphs_markdown",
        "definitions_markdown",
        "dense_summary_markdown",
        "key_facts_markdown",
        "triples_markdown",
    }.issubset(columns)
