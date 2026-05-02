"""
Pydantic request and response schemas for all API endpoints.
"""

from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field


# ---------------------------------------------------------------------------
# Shared config mixin – enables ORM mode (from_attributes) for all Out models
# ---------------------------------------------------------------------------


class _ORMBase(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ---------------------------------------------------------------------------
# Books
# ---------------------------------------------------------------------------


class BookCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=512)
    author: Optional[str] = Field(None, max_length=512)


class BookUpdate(BaseModel):
    title: Optional[str] = Field(None, min_length=1, max_length=512)
    author: Optional[str] = Field(None, max_length=512)


class BookOut(_ORMBase):
    id: int
    title: str
    author: Optional[str]
    created_at: datetime
    updated_at: datetime


class BookWithRecordingsOut(BookOut):
    recordings: List["RecordingOut"] = []


# ---------------------------------------------------------------------------
# Recordings
# ---------------------------------------------------------------------------


class RecordingCreate(BaseModel):
    book_id: int


class RecordingOut(_ORMBase):
    id: int
    book_id: int
    status: str
    duration_seconds: Optional[float]
    created_at: datetime
    processed_at: Optional[datetime]


class RecordingProcessRequest(BaseModel):
    """Request body for POST /api/recordings/{id}/process."""

    language: str = "ru"


# ---------------------------------------------------------------------------
# Chapters
# ---------------------------------------------------------------------------


class ChapterOut(_ORMBase):
    id: int
    recording_id: int
    chapter_number: int
    title: Optional[str]
    start_offset_ms: int
    end_offset_ms: int


# ---------------------------------------------------------------------------
# Transcriptions
# ---------------------------------------------------------------------------


class TranscriptionOut(_ORMBase):
    id: int
    chapter_id: int
    raw_text: str
    created_at: datetime


# ---------------------------------------------------------------------------
# Summaries
# ---------------------------------------------------------------------------


class SummaryOut(_ORMBase):
    id: int
    chapter_id: int
    summary_text: str
    model_used: str
    created_at: datetime


# ---------------------------------------------------------------------------
# Composite / detail views
# ---------------------------------------------------------------------------


class FullChapterOut(ChapterOut):
    """Chapter with its embedded transcription and summary."""

    transcription: Optional[TranscriptionOut] = None
    summary: Optional[SummaryOut] = None


class RecordingDetailOut(RecordingOut):
    """Recording with full chapter tree including a browser-playable audio URL."""

    chapters: List[FullChapterOut] = []
    # Populated by the API layer; None while the recording is still being captured.
    audio_url: Optional[str] = None


# ---------------------------------------------------------------------------
# WebSocket messages
# ---------------------------------------------------------------------------


class WSActionMessage(BaseModel):
    """
    JSON message sent from the browser to signal control actions.

    Example::

        {"action": "stop"}
    """

    action: str


class WSStatusMessage(BaseModel):
    """Server-to-client status update over the audio WebSocket."""

    status: str
    recording_id: Optional[int] = None
    ms: Optional[int] = None
    detail: Optional[str] = None


# Resolve forward references
BookWithRecordingsOut.model_rebuild()
RecordingDetailOut.model_rebuild()
