"""Services for persistent chapter-level multiple-choice tests."""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session

from backend.core.config import settings
from backend.models.orm import (
    Book,
    BookTestGenerationState,
    Chapter,
    ChapterTestOption,
    ChapterTestQuestion,
    Recording,
)
from backend.services.openrouter_client import generate_chapter_tests

logger = logging.getLogger(__name__)


class TestGenerationError(RuntimeError):
    """Raised when quiz generation or sampling cannot proceed."""


@dataclass(frozen=True)
class QuestionScore:
    question: ChapterTestQuestion
    selected_option_id: int | None
    selected_option: ChapterTestOption | None
    correct_option: ChapterTestOption
    is_correct: bool
    wrong_explanation: str | None


def get_book_test_chapters(db: Session, book_id: int) -> list[Chapter]:
    """Return completed chapters for a book, including those with generated tests."""
    book = db.get(Book, book_id)
    if book is None:
        raise TestGenerationError(f"Book {book_id} not found.")

    return (
        db.query(Chapter)
        .join(Recording)
        .filter(Recording.book_id == book_id)
        .order_by(Recording.created_at.desc(), Chapter.chapter_number.asc(), Chapter.id.asc())
        .all()
    )


def ensure_chapter_belongs_to_book(db: Session, book_id: int, chapter_id: int) -> Chapter:
    chapter = (
        db.query(Chapter)
        .join(Recording)
        .filter(Recording.book_id == book_id, Chapter.id == chapter_id)
        .first()
    )
    if chapter is None:
        raise TestGenerationError(f"Chapter {chapter_id} was not found in book {book_id}.")
    return chapter


def build_availability(db: Session, book_id: int) -> dict:
    chapters = get_book_test_chapters(db, book_id)
    chapter_rows = []
    total = 0
    for chapter in chapters:
        count = len(chapter.test_questions or [])
        total += count
        chapter_rows.append(
            {
                "chapter_id": chapter.id,
                "recording_id": chapter.recording_id,
                "chapter_number": chapter.chapter_number,
                "chapter_title": chapter.title,
                "recording_status": chapter.recording.status,
                "has_transcription": bool(chapter.transcription and chapter.transcription.raw_text.strip()),
                "question_count": count,
            }
        )
    state = _get_generation_state(db, book_id, create=False)
    return {
        "book_id": book_id,
        "total_questions": total,
        "chapters": chapter_rows,
        "generation_state": _generation_state_dict(state) if state else None,
    }


async def generate_tests_for_book(
    db: Session,
    book_id: int,
    chapter_id: int | None = None,
    target_count: int = 10,
    replace_existing: bool = True,
) -> int:
    """Generate and persist multiple-choice tests for one chapter or all book chapters."""
    state = _get_generation_state(db, book_id, create=True)
    state.status = "generating"
    state.chapter_id = chapter_id
    state.target_count = target_count
    state.replace_existing = replace_existing
    state.generated_questions = 0
    state.error_message = None
    state.started_at = datetime.utcnow()
    state.completed_at = None
    db.commit()

    if chapter_id is not None:
        chapters = [ensure_chapter_belongs_to_book(db, book_id, chapter_id)]
    else:
        chapters = get_book_test_chapters(db, book_id)

    eligible = [
        chapter for chapter in chapters
        if chapter.recording.status == "completed"
        and chapter.transcription
        and chapter.transcription.raw_text.strip()
    ]
    if not eligible:
        state.status = "error"
        state.error_message = "No completed chapters with transcriptions are available for test generation."
        state.completed_at = datetime.utcnow()
        db.commit()
        raise TestGenerationError(
            "No completed chapters with transcriptions are available for test generation."
        )

    total_generated = 0
    try:
        if replace_existing:
            deleted_count = 0
            for chapter in eligible:
                for question in list(chapter.test_questions):
                    db.delete(question)
                    deleted_count += 1
            if deleted_count:
                db.flush()
                db.commit()
                logger.info(
                    "Deleted %d existing test question(s) before regenerating book %d.",
                    deleted_count,
                    book_id,
                )

        for chapter in eligible:
            if not replace_existing and chapter.test_questions:
                logger.info("Skipping chapter %d because tests already exist.", chapter.id)
                continue

            result = await generate_chapter_tests(
                chapter.transcription.raw_text,
                chapter_title=chapter.title or f"Chapter {chapter.chapter_number}",
                target_count=target_count,
            )
            questions = _normalize_generated_questions(result)
            for generated in questions:
                question = ChapterTestQuestion(
                    chapter_id=chapter.id,
                    question_text=generated["question"],
                    explanation=generated["explanation"],
                    difficulty=generated["difficulty"],
                    concept_tags=", ".join(generated["concept_tags"]),
                    model_used=settings.openrouter.summarization.model,
                )
                db.add(question)
                db.flush()
                for display_order, option_text in enumerate(generated["options"]):
                    db.add(
                        ChapterTestOption(
                            question_id=question.id,
                            option_text=option_text,
                            is_correct=display_order == generated["correct_option_index"],
                            wrong_explanation=generated["option_explanations"][display_order] or None,
                            display_order=display_order,
                        )
                    )
                total_generated += 1

        state.status = "completed"
        state.generated_questions = total_generated
        state.error_message = None
        state.completed_at = datetime.utcnow()
        db.commit()
    except Exception as exc:
        db.rollback()
        state = _get_generation_state(db, book_id, create=True)
        state.status = "error"
        state.generated_questions = total_generated
        state.error_message = str(exc)
        state.completed_at = datetime.utcnow()
        db.commit()
        raise

    return total_generated


def sample_questions(
    db: Session,
    book_id: int,
    chapter_id: int | None = None,
    sample_size: int = 10,
) -> list[ChapterTestQuestion]:
    query = db.query(ChapterTestQuestion).join(Chapter).join(Recording).filter(Recording.book_id == book_id)
    if chapter_id is not None:
        ensure_chapter_belongs_to_book(db, book_id, chapter_id)
        query = query.filter(Chapter.id == chapter_id)

    questions = query.all()
    if not questions:
        raise TestGenerationError("No generated tests found for the selected scope.")

    size = max(1, min(int(sample_size or 10), len(questions)))
    sampled = random.sample(questions, size) if len(questions) > size else questions
    random.shuffle(sampled)
    return sampled


def score_answers(
    db: Session,
    answers: dict[int, int | None],
    book_id: int | None = None,
) -> list[QuestionScore]:
    if not answers:
        raise TestGenerationError("No answers were submitted.")

    query = db.query(ChapterTestQuestion).filter(ChapterTestQuestion.id.in_(answers.keys()))
    if book_id is not None:
        query = query.join(Chapter).join(Recording).filter(Recording.book_id == book_id)

    questions = query.all()
    found_ids = {question.id for question in questions}
    missing = sorted(set(answers.keys()) - found_ids)
    if missing:
        raise TestGenerationError(f"Question(s) not found: {missing}")

    scores: list[QuestionScore] = []
    for question in questions:
        correct = next((option for option in question.options if option.is_correct), None)
        if correct is None:
            raise TestGenerationError(f"Question {question.id} has no correct option configured.")
        selected_id = answers.get(question.id)
        selected = next((option for option in question.options if option.id == selected_id), None)
        is_correct = selected_id == correct.id
        scores.append(
            QuestionScore(
                question=question,
                selected_option_id=selected_id,
                selected_option=selected,
                correct_option=correct,
                is_correct=is_correct,
                wrong_explanation=(selected.wrong_explanation if selected and not is_correct else None),
            )
        )
    return scores


def _normalize_generated_questions(result: dict) -> list[dict]:
    normalized: list[dict] = []
    for raw in result.get("questions", []):
        question = str(raw.get("question") or "").strip()
        options = [str(option or "").strip() for option in raw.get("options", [])]
        explanation = str(raw.get("explanation") or "").strip()
        option_explanations = [
            str(option_explanation or "").strip()
            for option_explanation in raw.get("option_explanations", [])
        ]
        difficulty = str(raw.get("difficulty") or "medium").strip().lower()
        concept_tags = [str(tag or "").strip() for tag in raw.get("concept_tags", [])]
        concept_tags = [tag for tag in concept_tags if tag]

        try:
            correct_index = int(raw.get("correct_option_index"))
        except (TypeError, ValueError):
            continue

        if not question or not explanation:
            continue
        if len(options) != 4 or any(not option for option in options):
            continue
        if len(option_explanations) != 4:
            continue
        if correct_index < 0 or correct_index >= len(options):
            continue
        if any(
            index != correct_index and not option_explanation
            for index, option_explanation in enumerate(option_explanations)
        ):
            continue
        option_explanations[correct_index] = ""
        if difficulty not in {"easy", "medium", "hard"}:
            difficulty = "medium"

        normalized.append(
            {
                "question": question,
                "options": options,
                "correct_option_index": correct_index,
                "explanation": explanation,
                "option_explanations": option_explanations,
                "difficulty": difficulty,
                "concept_tags": concept_tags,
            }
        )

    if not normalized:
        raise TestGenerationError("The LLM did not return any valid multiple-choice questions.")
    return normalized


def _get_generation_state(
    db: Session,
    book_id: int,
    create: bool,
) -> BookTestGenerationState | None:
    state = (
        db.query(BookTestGenerationState)
        .filter(BookTestGenerationState.book_id == book_id)
        .first()
    )
    if state is None and create:
        state = BookTestGenerationState(book_id=book_id)
        db.add(state)
        db.flush()
    return state


def _generation_state_dict(state: BookTestGenerationState) -> dict:
    return {
        "status": state.status,
        "chapter_id": state.chapter_id,
        "target_count": state.target_count,
        "replace_existing": state.replace_existing,
        "generated_questions": state.generated_questions,
        "error_message": state.error_message,
        "started_at": state.started_at,
        "completed_at": state.completed_at,
        "updated_at": state.updated_at,
    }
