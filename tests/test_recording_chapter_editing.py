from datetime import datetime

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.api.recordings import update_chapter, update_chapter_order
from backend.core.database import Base
from backend.models.orm import Book, Chapter, Recording, Summary, Transcription
from backend.schemas.api import ChapterOrderRequest, ChapterUpdateRequest


@pytest.fixture()
def db_session(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'chapter_editing.db'}",
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


def _seed_recording(db, status="completed"):
    book = Book(title="Editable Book", author="Author")
    db.add(book)
    db.flush()
    recording = Recording(
        book_id=book.id,
        audio_file_path="data/vad_audio/1.wav",
        status=status,
        duration_seconds=120,
        processed_at=datetime.utcnow() if status == "completed" else None,
    )
    db.add(recording)
    db.flush()

    first = Chapter(recording_id=recording.id, chapter_number=1, title="First", start_offset_ms=0, end_offset_ms=0)
    second = Chapter(recording_id=recording.id, chapter_number=2, title="Second", start_offset_ms=0, end_offset_ms=0)
    db.add_all([first, second])
    db.flush()

    db.add_all([
        Transcription(chapter_id=first.id, raw_text="First transcript"),
        Summary(chapter_id=first.id, summary_text="First summary", model_used="test-model"),
        Transcription(chapter_id=second.id, raw_text="Second transcript"),
        Summary(chapter_id=second.id, summary_text="Second summary", model_used="test-model"),
    ])
    db.commit()
    return recording.id, first.id, second.id


def test_update_chapter_persists_title_transcription_and_summary(db_session):
    recording_id, first_id, _ = _seed_recording(db_session)

    detail = update_chapter(
        recording_id,
        first_id,
        ChapterUpdateRequest(
            title="Updated title",
            transcription="Updated transcript",
            summary="Updated summary",
        ),
        db_session,
    )

    chapter = db_session.get(Chapter, first_id)
    assert chapter.title == "Updated title"
    assert chapter.transcription.raw_text == "Updated transcript"
    assert chapter.summary.summary_text == "Updated summary"
    assert detail.audio_url == f"/media/audio/{recording_id}.wav"
    assert detail.chapters[0].title == "Updated title"


def test_update_chapter_rejects_non_completed_recording(db_session):
    recording_id, first_id, _ = _seed_recording(db_session, status="ready")

    with pytest.raises(HTTPException) as exc_info:
        update_chapter(
            recording_id,
            first_id,
            ChapterUpdateRequest(title="Should fail"),
            db_session,
        )

    assert exc_info.value.status_code == 400
    assert "completed" in exc_info.value.detail


def test_update_chapter_rejects_empty_transcription(db_session):
    recording_id, first_id, _ = _seed_recording(db_session)

    with pytest.raises(HTTPException) as exc_info:
        update_chapter(
            recording_id,
            first_id,
            ChapterUpdateRequest(transcription="   "),
            db_session,
        )

    assert exc_info.value.status_code == 400
    assert "transcription" in exc_info.value.detail.lower()


def test_update_chapter_order_persists_new_chapter_numbers(db_session):
    recording_id, first_id, second_id = _seed_recording(db_session)

    detail = update_chapter_order(
        recording_id,
        ChapterOrderRequest(chapter_ids=[second_id, first_id]),
        db_session,
    )

    first = db_session.get(Chapter, first_id)
    second = db_session.get(Chapter, second_id)
    assert first.chapter_number == 2
    assert second.chapter_number == 1
    assert [chapter.id for chapter in detail.chapters] == [second_id, first_id]
    assert [chapter.chapter_number for chapter in detail.chapters] == [1, 2]


def test_update_chapter_order_requires_all_recording_chapters(db_session):
    recording_id, first_id, _ = _seed_recording(db_session)

    with pytest.raises(HTTPException) as exc_info:
        update_chapter_order(
            recording_id,
            ChapterOrderRequest(chapter_ids=[first_id]),
            db_session,
        )

    assert exc_info.value.status_code == 400
    assert "every chapter" in exc_info.value.detail
