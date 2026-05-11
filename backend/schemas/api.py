"""
Pydantic request and response schemas for all API endpoints.
"""

from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field, computed_field


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


class RecordingProgressOut(BaseModel):
    """Persisted processing progress for a recording."""

    recording_id: int
    status: str
    stage: str = "idle"
    message: str = ""
    percent: int = Field(0, ge=0, le=100)
    transcription_chunks_completed: int = 0
    transcription_chunks_total: int = 0
    summary_chapters_completed: int = 0
    summary_chapters_total: int = 0
    summary_sections_completed: int = 0
    summary_sections_total: int = 0
    error_message: Optional[str] = None
    started_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    @computed_field
    @property
    def transcription_percent(self) -> int:
        if self.transcription_chunks_total <= 0:
            return 0
        return max(
            0,
            min(100, round(self.transcription_chunks_completed / self.transcription_chunks_total * 100)),
        )

    @computed_field
    @property
    def summary_percent(self) -> int:
        if self.summary_chapters_total <= 0:
            return 0
        return max(
            0,
            min(100, round(self.summary_chapters_completed / self.summary_chapters_total * 100)),
        )

    @computed_field
    @property
    def summary_section_percent(self) -> int:
        if self.summary_sections_total <= 0:
            return 0
        return max(
            0,
            min(100, round(self.summary_sections_completed / self.summary_sections_total * 100)),
        )

    @computed_field
    @property
    def is_active(self) -> bool:
        return self.status == "processing" and self.stage not in {"completed", "error"}


class RecordingOut(_ORMBase):
    id: int
    book_id: int
    status: str
    duration_seconds: Optional[float]
    created_at: datetime
    processed_at: Optional[datetime]
    progress_stage: Optional[str] = Field(None, exclude=True)
    progress_message: Optional[str] = Field(None, exclude=True)
    progress_percent: Optional[int] = Field(None, exclude=True)
    transcription_chunks_completed: Optional[int] = Field(None, exclude=True)
    transcription_chunks_total: Optional[int] = Field(None, exclude=True)
    summary_chapters_completed: Optional[int] = Field(None, exclude=True)
    summary_chapters_total: Optional[int] = Field(None, exclude=True)
    summary_sections_completed: Optional[int] = Field(None, exclude=True)
    summary_sections_total: Optional[int] = Field(None, exclude=True)
    progress_error: Optional[str] = Field(None, exclude=True)
    progress_started_at: Optional[datetime] = Field(None, exclude=True)
    progress_updated_at: Optional[datetime] = Field(None, exclude=True)

    @computed_field
    @property
    def progress(self) -> RecordingProgressOut:
        return RecordingProgressOut(
            recording_id=self.id,
            status=self.status,
            stage=self.progress_stage or "idle",
            message=self.progress_message or "",
            percent=self.progress_percent or 0,
            transcription_chunks_completed=self.transcription_chunks_completed or 0,
            transcription_chunks_total=self.transcription_chunks_total or 0,
            summary_chapters_completed=self.summary_chapters_completed or 0,
            summary_chapters_total=self.summary_chapters_total or 0,
            summary_sections_completed=self.summary_sections_completed or 0,
            summary_sections_total=self.summary_sections_total or 0,
            error_message=self.progress_error,
            started_at=self.progress_started_at,
            updated_at=self.progress_updated_at,
        )


class RecordingProcessRequest(BaseModel):
    """Request body for POST /api/recordings/{id}/process."""

    language: str = "ru"


class ChapterUpdateRequest(BaseModel):
    """Manually update chapter metadata and generated/stored text fields."""

    title: Optional[str] = Field(None, max_length=512)
    transcription: Optional[str] = None
    summary: Optional[str] = None


class ChapterOrderRequest(BaseModel):
    """Persist a complete manual chapter ordering for one recording."""

    chapter_ids: List[int] = Field(..., min_length=1)


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
    graphs_markdown: Optional[str] = None
    definitions_markdown: Optional[str] = None
    tables_markdown: Optional[str] = None
    dense_summary_markdown: Optional[str] = None
    key_facts_markdown: Optional[str] = None
    triples_markdown: Optional[str] = None
    model_used: str
    created_at: datetime

    @computed_field
    @property
    def summary_sections(self) -> dict[str, str]:
        return {
            "graphs": self.graphs_markdown or "",
            "definitions": self.definitions_markdown or "",
            "tables": self.tables_markdown or "",
            "dense_summary": self.dense_summary_markdown or "",
            "key_facts": self.key_facts_markdown or "",
            "triples": self.triples_markdown or "",
        }


# ---------------------------------------------------------------------------
# Composite / detail views
# ---------------------------------------------------------------------------


class FullChapterOut(ChapterOut):
    """Chapter with its embedded transcription and summary."""

    transcription: Optional[TranscriptionOut] = None
    summary: Optional[SummaryOut] = None


# ---------------------------------------------------------------------------
# Chapter tests / quizzes
# ---------------------------------------------------------------------------


class TestGenerationRequest(BaseModel):
    """Generate multiple-choice tests from stored chapter transcriptions."""

    chapter_id: Optional[int] = None
    target_count: int = Field(10, ge=1, le=30)
    replace_existing: bool = True


class TestSampleRequest(BaseModel):
    """Create a quiz sample from generated tests."""

    chapter_id: Optional[int] = None
    sample_size: int = Field(10, ge=1, le=100)


class TestAnswerIn(BaseModel):
    question_id: int
    option_id: int


class TestSubmitRequest(BaseModel):
    answers: List[TestAnswerIn]


class TestOptionOut(_ORMBase):
    id: int
    question_id: int
    option_text: str
    display_order: int


class TestQuestionOut(_ORMBase):
    id: int
    chapter_id: int
    question_text: str
    difficulty: str
    concept_tags: str
    created_at: datetime
    options: List[TestOptionOut] = []


class ChapterTestAvailabilityOut(BaseModel):
    chapter_id: int
    recording_id: int
    chapter_number: int
    chapter_title: Optional[str]
    recording_status: str
    has_transcription: bool
    question_count: int


class TestGenerationStateOut(BaseModel):
    status: str
    chapter_id: Optional[int]
    target_count: int
    replace_existing: bool
    generated_questions: int
    error_message: Optional[str]
    started_at: Optional[datetime]
    completed_at: Optional[datetime]
    updated_at: datetime


class BookTestAvailabilityOut(BaseModel):
    book_id: int
    total_questions: int
    chapters: List[ChapterTestAvailabilityOut]
    generation_state: Optional[TestGenerationStateOut] = None


class TestGenerationResultOut(BaseModel):
    book_id: int
    generated_questions: int
    total_questions: int
    chapters: List[ChapterTestAvailabilityOut]
    generation_state: Optional[TestGenerationStateOut] = None


class TestSampleOut(BaseModel):
    book_id: int
    requested_size: int
    returned_size: int
    chapter_id: Optional[int]
    questions: List[TestQuestionOut]


class TestSubmissionQuestionResultOut(BaseModel):
    question_id: int
    selected_option_id: Optional[int]
    correct_option_id: int
    is_correct: bool
    question_text: str
    explanation: str
    wrong_explanation: Optional[str] = None


class TestSubmissionResultOut(BaseModel):
    total: int
    correct: int
    score_percent: float
    results: List[TestSubmissionQuestionResultOut]


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
TestQuestionOut.model_rebuild()
