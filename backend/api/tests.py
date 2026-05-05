"""Book-level endpoints for generated chapter tests and quiz sessions."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from backend.core.database import get_db
from backend.core.exceptions import bad_request, not_found
from backend.models.orm import Book
from backend.schemas.api import (
    BookTestAvailabilityOut,
    TestGenerationRequest,
    TestGenerationResultOut,
    TestSampleOut,
    TestSampleRequest,
    TestSubmitRequest,
    TestSubmissionQuestionResultOut,
    TestSubmissionResultOut,
)
from backend.services.test_generator import (
    TestGenerationError,
    build_availability,
    generate_tests_for_book,
    sample_questions,
    score_answers,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/books", tags=["tests"])


@router.get(
    "/{book_id}/tests/availability",
    response_model=BookTestAvailabilityOut,
    summary="List generated test availability for a book",
)
def get_test_availability(book_id: int, db: Session = Depends(get_db)) -> dict:
    if db.get(Book, book_id) is None:
        raise not_found("Book", book_id)
    return build_availability(db, book_id)


@router.post(
    "/{book_id}/tests/generate",
    response_model=TestGenerationResultOut,
    summary="Generate multiple-choice tests from chapter transcriptions",
)
async def generate_book_tests(
    book_id: int,
    request: TestGenerationRequest,
    db: Session = Depends(get_db),
) -> dict:
    if db.get(Book, book_id) is None:
        raise not_found("Book", book_id)
    try:
        generated = await generate_tests_for_book(
            db,
            book_id,
            chapter_id=request.chapter_id,
            target_count=request.target_count,
            replace_existing=request.replace_existing,
        )
    except TestGenerationError as exc:
        raise bad_request(str(exc)) from exc

    availability = build_availability(db, book_id)
    return {
        "book_id": book_id,
        "generated_questions": generated,
        "total_questions": availability["total_questions"],
        "chapters": availability["chapters"],
        "generation_state": availability["generation_state"],
    }


@router.post(
    "/{book_id}/tests/sample",
    response_model=TestSampleOut,
    summary="Sample generated questions for a quiz session",
)
def sample_book_tests(
    book_id: int,
    request: TestSampleRequest,
    db: Session = Depends(get_db),
) -> dict:
    if db.get(Book, book_id) is None:
        raise not_found("Book", book_id)
    try:
        questions = sample_questions(
            db,
            book_id,
            chapter_id=request.chapter_id,
            sample_size=request.sample_size,
        )
    except TestGenerationError as exc:
        raise bad_request(str(exc)) from exc

    return {
        "book_id": book_id,
        "requested_size": request.sample_size,
        "returned_size": len(questions),
        "chapter_id": request.chapter_id,
        "questions": questions,
    }


@router.post(
    "/{book_id}/tests/submit",
    response_model=TestSubmissionResultOut,
    summary="Score submitted quiz answers",
)
def submit_book_tests(
    book_id: int,
    request: TestSubmitRequest,
    db: Session = Depends(get_db),
) -> TestSubmissionResultOut:
    if db.get(Book, book_id) is None:
        raise not_found("Book", book_id)
    answers = {answer.question_id: answer.option_id for answer in request.answers}
    try:
        scores = score_answers(db, answers, book_id=book_id)
    except TestGenerationError as exc:
        raise bad_request(str(exc)) from exc

    correct = sum(1 for score in scores if score.is_correct)
    total = len(scores)
    results = [
        TestSubmissionQuestionResultOut(
            question_id=score.question.id,
            selected_option_id=score.selected_option_id,
            correct_option_id=score.correct_option.id,
            is_correct=score.is_correct,
            question_text=score.question.question_text,
            explanation=score.question.explanation,
            wrong_explanation=score.wrong_explanation,
        )
        for score in scores
    ]
    return TestSubmissionResultOut(
        total=total,
        correct=correct,
        score_percent=round((correct / total) * 100, 1) if total else 0.0,
        results=results,
    )
