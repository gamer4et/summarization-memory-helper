"""
SQLAlchemy ORM models for the Book Summarization application.

Tables
------
books            Books the user wants to summarize.
recordings       A single recording session attached to a book.
chapters         Detected chapters within one recording.
transcriptions   Raw transcription text for one chapter.
summaries        LLM-generated summary for one chapter.
"""

from datetime import datetime

from sqlalchemy import (
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
    model_used = Column(String(256), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    chapter = relationship("Chapter", back_populates="summary")

    def __repr__(self) -> str:
        return f"<Summary id={self.id} chapter_id={self.chapter_id} model={self.model_used!r}>"
