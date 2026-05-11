"""
SQLAlchemy ORM models for the Book Summarization application.

Tables
------
books            Books the user wants to summarize.
recordings       A single recording session attached to a book.
chapters         Detected chapters within one recording.
transcriptions   Raw transcription text for one chapter.
summaries        LLM-generated summary for one chapter.
chapter_test_questions  Generated quiz questions for one chapter.
chapter_test_options    Multiple-choice answer options for one question.
"""

from datetime import datetime

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import relationship

from backend.core.database import Base


class Book(Base):
    __tablename__ = "books"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String(512), nullable=False)
    author = Column(String(512), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )

    recordings = relationship(
        "Recording",
        back_populates="book",
        cascade="all, delete-orphan",
        lazy="select",
    )
    test_generation_state = relationship(
        "BookTestGenerationState",
        back_populates="book",
        uselist=False,
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:
        return f"<Book id={self.id} title={self.title!r}>"


class Recording(Base):
    __tablename__ = "recordings"

    id = Column(Integer, primary_key=True, index=True)
    book_id = Column(Integer, ForeignKey("books.id", ondelete="CASCADE"), nullable=False)
    # Absolute or workdir-relative path to the assembled WAV file on disk.
    audio_file_path = Column(String(1024), nullable=False)
    duration_seconds = Column(Float, nullable=True)
    # Lifecycle: recording → ready → processing → completed | error
    status = Column(String(32), default="recording", nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    processed_at = Column(DateTime, nullable=True)
    progress_stage = Column(String(64), default="idle", nullable=True)
    progress_message = Column(Text, default="", nullable=True)
    progress_percent = Column(Integer, default=0, nullable=True)
    transcription_chunks_completed = Column(Integer, default=0, nullable=True)
    transcription_chunks_total = Column(Integer, default=0, nullable=True)
    summary_chapters_completed = Column(Integer, default=0, nullable=True)
    summary_chapters_total = Column(Integer, default=0, nullable=True)
    summary_sections_completed = Column(Integer, default=0, nullable=True)
    summary_sections_total = Column(Integer, default=0, nullable=True)
    progress_error = Column(Text, nullable=True)
    progress_started_at = Column(DateTime, nullable=True)
    progress_updated_at = Column(DateTime, nullable=True)

    book = relationship("Book", back_populates="recordings")
    chapters = relationship(
        "Chapter",
        back_populates="recording",
        cascade="all, delete-orphan",
        lazy="select",
        order_by="Chapter.chapter_number",
    )

    def __repr__(self) -> str:
        return f"<Recording id={self.id} book_id={self.book_id} status={self.status!r}>"


class Chapter(Base):
    __tablename__ = "chapters"

    id = Column(Integer, primary_key=True, index=True)
    recording_id = Column(
        Integer, ForeignKey("recordings.id", ondelete="CASCADE"), nullable=False
    )
    chapter_number = Column(Integer, nullable=False)
    # Optional title extracted from spoken marker (e.g. "Chapter Three: The Fall")
    title = Column(String(512), nullable=True)
    # Audio byte-offsets – populated when audio-alignment is implemented,
    # defaulting to 0 when unknown.
    start_offset_ms = Column(Integer, nullable=False, default=0)
    end_offset_ms = Column(Integer, nullable=False, default=0)

    recording = relationship("Recording", back_populates="chapters")
    transcription = relationship(
        "Transcription",
        back_populates="chapter",
        uselist=False,
        cascade="all, delete-orphan",
    )
    summary = relationship(
        "Summary",
        back_populates="chapter",
        uselist=False,
        cascade="all, delete-orphan",
    )
    test_questions = relationship(
        "ChapterTestQuestion",
        back_populates="chapter",
        cascade="all, delete-orphan",
        lazy="select",
        order_by="ChapterTestQuestion.id",
    )

    def __repr__(self) -> str:
        return (
            f"<Chapter id={self.id} recording_id={self.recording_id}"
            f" number={self.chapter_number}>"
        )


class Transcription(Base):
    __tablename__ = "transcriptions"

    id = Column(Integer, primary_key=True, index=True)
    chapter_id = Column(
        Integer, ForeignKey("chapters.id", ondelete="CASCADE"), nullable=False
    )
    raw_text = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    chapter = relationship("Chapter", back_populates="transcription")

    def __repr__(self) -> str:
        preview = (self.raw_text or "")[:40]
        return f"<Transcription id={self.id} chapter_id={self.chapter_id} text={preview!r}>"


class Summary(Base):
    __tablename__ = "summaries"

    id = Column(Integer, primary_key=True, index=True)
    chapter_id = Column(
        Integer, ForeignKey("chapters.id", ondelete="CASCADE"), nullable=False
    )
    summary_text = Column(Text, nullable=False)
    graphs_markdown = Column(Text, nullable=True)
    definitions_markdown = Column(Text, nullable=True)
    tables_markdown = Column(Text, nullable=True)
    dense_summary_markdown = Column(Text, nullable=True)
    key_facts_markdown = Column(Text, nullable=True)
    triples_markdown = Column(Text, nullable=True)
    model_used = Column(String(256), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    chapter = relationship("Chapter", back_populates="summary")

    def __repr__(self) -> str:
        return f"<Summary id={self.id} chapter_id={self.chapter_id} model={self.model_used!r}>"


class BookTestGenerationState(Base):
    __tablename__ = "book_test_generation_states"

    id = Column(Integer, primary_key=True, index=True)
    book_id = Column(Integer, ForeignKey("books.id", ondelete="CASCADE"), nullable=False, unique=True, index=True)
    status = Column(String(32), nullable=False, default="idle")
    chapter_id = Column(Integer, ForeignKey("chapters.id", ondelete="SET NULL"), nullable=True)
    target_count = Column(Integer, nullable=False, default=10)
    replace_existing = Column(Boolean, nullable=False, default=True)
    generated_questions = Column(Integer, nullable=False, default=0)
    error_message = Column(Text, nullable=True)
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    updated_at = Column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )

    book = relationship("Book", back_populates="test_generation_state")

    def __repr__(self) -> str:
        return f"<BookTestGenerationState book_id={self.book_id} status={self.status!r}>"


class ChapterTestQuestion(Base):
    __tablename__ = "chapter_test_questions"

    id = Column(Integer, primary_key=True, index=True)
    chapter_id = Column(
        Integer, ForeignKey("chapters.id", ondelete="CASCADE"), nullable=False, index=True
    )
    question_text = Column(Text, nullable=False)
    explanation = Column(Text, nullable=False)
    difficulty = Column(String(32), nullable=False, default="medium")
    concept_tags = Column(Text, nullable=False, default="")
    model_used = Column(String(256), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    chapter = relationship("Chapter", back_populates="test_questions")
    options = relationship(
        "ChapterTestOption",
        back_populates="question",
        cascade="all, delete-orphan",
        lazy="select",
        order_by="ChapterTestOption.display_order",
    )

    def __repr__(self) -> str:
        preview = (self.question_text or "")[:40]
        return f"<ChapterTestQuestion id={self.id} chapter_id={self.chapter_id} text={preview!r}>"


class ChapterTestOption(Base):
    __tablename__ = "chapter_test_options"

    id = Column(Integer, primary_key=True, index=True)
    question_id = Column(
        Integer,
        ForeignKey("chapter_test_questions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    option_text = Column(Text, nullable=False)
    is_correct = Column(Boolean, nullable=False, default=False)
    wrong_explanation = Column(Text, nullable=True)
    display_order = Column(Integer, nullable=False, default=0)

    question = relationship("ChapterTestQuestion", back_populates="options")

    def __repr__(self) -> str:
        return (
            f"<ChapterTestOption id={self.id} question_id={self.question_id} "
            f"correct={self.is_correct}>"
        )
