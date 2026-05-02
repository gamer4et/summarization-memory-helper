"""
Book CRUD endpoints.

Routes
------
GET    /api/books          — list all books
POST   /api/books          — create a book
GET    /api/books/{id}     — get a book with its recordings
DELETE /api/books/{id}     — delete a book (cascades to recordings/chapters)
PATCH  /api/books/{id}     — update title / author
"""

import logging
from typing import List

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy.orm import Session

from backend.core.database import get_db
from backend.core.exceptions import not_found
from backend.models.orm import Book
from backend.schemas.api import BookCreate, BookOut, BookUpdate, BookWithRecordingsOut

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/books", tags=["books"])


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


@router.get("", response_model=List[BookOut], summary="List all books")
def list_books(db: Session = Depends(get_db)) -> List[Book]:
    """Return every book stored in the database, ordered by creation date."""
    return db.query(Book).order_by(Book.created_at.desc()).all()


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


@router.post(
    "",
    response_model=BookOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new book",
)
def create_book(payload: BookCreate, db: Session = Depends(get_db)) -> Book:
    """Create a new book entry."""
    book = Book(title=payload.title, author=payload.author)
    db.add(book)
    db.commit()
    db.refresh(book)
    logger.info("Created book id=%d title=%r", book.id, book.title)
    return book


# ---------------------------------------------------------------------------
# Read (with recordings)
# ---------------------------------------------------------------------------


@router.get(
    "/{book_id}",
    response_model=BookWithRecordingsOut,
    summary="Get a book with its recordings",
)
def get_book(book_id: int, db: Session = Depends(get_db)) -> Book:
    """Retrieve a single book and its associated recordings."""
    book = db.get(Book, book_id)
    if book is None:
        raise not_found("Book", book_id)
    return book


# ---------------------------------------------------------------------------
# Update
# ---------------------------------------------------------------------------


@router.patch(
    "/{book_id}",
    response_model=BookOut,
    summary="Update a book's title or author",
)
def update_book(
    book_id: int, payload: BookUpdate, db: Session = Depends(get_db)
) -> Book:
    """Partially update a book (title and/or author)."""
    book = db.get(Book, book_id)
    if book is None:
        raise not_found("Book", book_id)

    if payload.title is not None:
        book.title = payload.title
    if payload.author is not None:
        book.author = payload.author

    db.commit()
    db.refresh(book)
    logger.info("Updated book id=%d", book.id)
    return book


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------


@router.delete(
    "/{book_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a book and all its data",
)
def delete_book(book_id: int, db: Session = Depends(get_db)) -> Response:
    """
    Delete a book.  All recordings, chapters, transcriptions, and summaries
    cascade-delete automatically via SQLAlchemy relationship settings.
    """
    book = db.get(Book, book_id)
    if book is None:
        raise not_found("Book", book_id)

    db.delete(book)
    db.commit()
    logger.info("Deleted book id=%d", book_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
