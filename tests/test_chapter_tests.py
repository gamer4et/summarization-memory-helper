from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.core.database import Base
from backend.api.tests import submit_book_tests
from backend.models.orm import Book, Chapter, Recording, Summary, Transcription
from backend.schemas.api import TestAnswerIn, TestSubmitRequest
from backend.services import test_generator


@pytest.fixture()
def db_session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'tests.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)


def _seed_completed_chapter(db):
    book = Book(title="The Book", author="Author")
    db.add(book)
    db.flush()
    recording = Recording(
        book_id=book.id,
        audio_file_path="data/vad_audio/1.wav",
        status="completed",
        duration_seconds=60,
        processed_at=datetime.utcnow(),
    )
    db.add(recording)
    db.flush()
    chapter = Chapter(
        recording_id=recording.id,
        chapter_number=1,
        title="Core chapter",
        start_offset_ms=0,
        end_offset_ms=0,
    )
    db.add(chapter)
    db.flush()
    db.add(Transcription(chapter_id=chapter.id, raw_text="The key idea is customer development."))
    db.add(Summary(chapter_id=chapter.id, summary_text="Summary", model_used="test-model"))
    db.commit()
    return book.id, chapter.id


@pytest.mark.asyncio
async def test_generate_tests_for_book_persists_questions_and_options(monkeypatch, db_session):
    book_id, chapter_id = _seed_completed_chapter(db_session)

    async def fake_generate_chapter_tests(text, chapter_title="", target_count=10):
        assert "customer development" in text
        assert chapter_title == "Core chapter"
        assert target_count == 2
        return {
            "questions": [
                {
                    "question": "What is the chapter mainly about?",
                    "options": ["Customer development", "Accounting", "Hiring", "Pricing trivia"],
                    "correct_option_index": 0,
                    "explanation": "The transcription centers on customer development.",
                    "option_explanations": [
                        "",
                        "Accounting is not the chapter's central idea.",
                        "Hiring is not discussed as the core idea.",
                        "Pricing trivia is not the focus of the transcription.",
                    ],
                    "difficulty": "medium",
                    "concept_tags": ["customers", "startup"],
                }
            ]
        }

    monkeypatch.setattr(test_generator, "generate_chapter_tests", fake_generate_chapter_tests)

    generated = await test_generator.generate_tests_for_book(
        db_session,
        book_id,
        chapter_id=chapter_id,
        target_count=2,
    )

    assert generated == 1
    availability = test_generator.build_availability(db_session, book_id)
    assert availability["total_questions"] == 1
    question = test_generator.sample_questions(db_session, book_id, sample_size=1)[0]
    assert question.question_text == "What is the chapter mainly about?"
    assert len(question.options) == 4
    assert [option.is_correct for option in question.options] == [True, False, False, False]
    assert question.options[0].wrong_explanation is None
    assert question.options[1].wrong_explanation == "Accounting is not the chapter's central idea."


@pytest.mark.asyncio
async def test_generate_tests_replaces_existing_questions(monkeypatch, db_session):
    book_id, chapter_id = _seed_completed_chapter(db_session)
    calls = 0

    async def fake_generate_chapter_tests(text, chapter_title="", target_count=10):
        nonlocal calls
        calls += 1
        if calls == 2:
            assert test_generator.build_availability(db_session, book_id)["total_questions"] == 0
        return {
            "questions": [
                {
                    "question": f"Question {calls}?",
                    "options": ["A", "B", "C", "D"],
                    "correct_option_index": 1,
                    "explanation": "Because B is correct.",
                    "option_explanations": ["A is wrong.", "", "C is wrong.", "D is wrong."],
                    "difficulty": "easy",
                    "concept_tags": [],
                }
            ]
        }

    monkeypatch.setattr(test_generator, "generate_chapter_tests", fake_generate_chapter_tests)

    await test_generator.generate_tests_for_book(db_session, book_id, chapter_id=chapter_id)
    await test_generator.generate_tests_for_book(db_session, book_id, chapter_id=chapter_id)

    availability = test_generator.build_availability(db_session, book_id)
    assert availability["total_questions"] == 1
    question = test_generator.sample_questions(db_session, book_id, sample_size=1)[0]
    assert question.question_text == "Question 2?"


@pytest.mark.asyncio
async def test_sample_questions_and_score_answers(monkeypatch, db_session):
    book_id, chapter_id = _seed_completed_chapter(db_session)

    async def fake_generate_chapter_tests(text, chapter_title="", target_count=10):
        return {
            "questions": [
                {
                    "question": "Which option matches the core idea?",
                    "options": ["Wrong", "Right", "Also wrong", "Trivia"],
                    "correct_option_index": 1,
                    "explanation": "The right option matches the core idea.",
                    "option_explanations": [
                        "Wrong contradicts the chapter's core idea.",
                        "",
                        "Also wrong does not match the core idea.",
                        "Trivia focuses on non-essential detail.",
                    ],
                    "difficulty": "medium",
                    "concept_tags": ["core idea"],
                }
            ]
        }

    monkeypatch.setattr(test_generator, "generate_chapter_tests", fake_generate_chapter_tests)
    await test_generator.generate_tests_for_book(db_session, book_id, chapter_id=chapter_id)

    question = test_generator.sample_questions(db_session, book_id, chapter_id=chapter_id, sample_size=10)[0]
    correct_option = next(option for option in question.options if option.is_correct)
    wrong_option = next(option for option in question.options if not option.is_correct)

    correct_score = test_generator.score_answers(db_session, {question.id: correct_option.id})[0]
    wrong_score = test_generator.score_answers(db_session, {question.id: wrong_option.id})[0]

    assert correct_score.is_correct is True
    assert correct_score.wrong_explanation is None
    assert wrong_score.is_correct is False
    assert wrong_score.correct_option.id == correct_option.id
    assert wrong_score.selected_option.id == wrong_option.id
    assert wrong_score.wrong_explanation == wrong_option.wrong_explanation


@pytest.mark.asyncio
async def test_submit_book_tests_returns_selected_wrong_explanation(monkeypatch, db_session):
    book_id, chapter_id = _seed_completed_chapter(db_session)

    async def fake_generate_chapter_tests(text, chapter_title="", target_count=10):
        return {
            "questions": [
                {
                    "question": "Which option matches the core idea?",
                    "options": ["Wrong", "Right", "Also wrong", "Trivia"],
                    "correct_option_index": 1,
                    "explanation": "The right option matches the core idea.",
                    "option_explanations": [
                        "Wrong contradicts the chapter's core idea.",
                        "",
                        "Also wrong does not match the core idea.",
                        "Trivia focuses on non-essential detail.",
                    ],
                    "difficulty": "medium",
                    "concept_tags": ["core idea"],
                }
            ]
        }

    monkeypatch.setattr(test_generator, "generate_chapter_tests", fake_generate_chapter_tests)
    await test_generator.generate_tests_for_book(db_session, book_id, chapter_id=chapter_id)

    question = test_generator.sample_questions(db_session, book_id, chapter_id=chapter_id, sample_size=1)[0]
    wrong_option = next(option for option in question.options if option.option_text == "Also wrong")

    result = submit_book_tests(
        book_id,
        TestSubmitRequest(answers=[TestAnswerIn(question_id=question.id, option_id=wrong_option.id)]),
        db_session,
    )

    assert result.correct == 0
    assert result.results[0].is_correct is False
    assert result.results[0].wrong_explanation == "Also wrong does not match the core idea."
