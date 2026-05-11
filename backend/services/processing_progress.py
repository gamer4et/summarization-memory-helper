"""Helpers for persisting recording processing progress."""

from datetime import datetime

from sqlalchemy.orm import Session

from backend.models.orm import Recording


def clamp_percent(value: int | float | None) -> int:
    """Normalize a progress percentage to the inclusive 0–100 range."""
    if value is None:
        return 0
    return max(0, min(100, int(round(value))))


def clamp_completed_to_total(completed: int | None, total: int | None) -> int:
    """Normalize a completed counter and clamp it to total when total is known."""
    normalized_completed = max(0, int(completed or 0))
    normalized_total = max(0, int(total or 0))
    if normalized_total <= 0:
        return normalized_completed
    return min(normalized_completed, normalized_total)


def apply_recording_progress(
    recording: Recording,
    *,
    stage: str | None = None,
    message: str | None = None,
    percent: int | float | None = None,
    transcription_chunks_completed: int | None = None,
    transcription_chunks_total: int | None = None,
    summary_chapters_completed: int | None = None,
    summary_chapters_total: int | None = None,
    summary_sections_completed: int | None = None,
    summary_sections_total: int | None = None,
    error_message: str | None = None,
    clear_error: bool = False,
    mark_started: bool = False,
) -> None:
    """Apply progress fields to a recording without committing the session."""
    now = datetime.utcnow()

    if stage is not None:
        recording.progress_stage = stage
    if message is not None:
        recording.progress_message = message
    if percent is not None:
        recording.progress_percent = clamp_percent(percent)
    if transcription_chunks_completed is not None:
        effective_total = transcription_chunks_total if transcription_chunks_total is not None else recording.transcription_chunks_total
        recording.transcription_chunks_completed = clamp_completed_to_total(transcription_chunks_completed, effective_total)
    if transcription_chunks_total is not None:
        recording.transcription_chunks_total = max(0, int(transcription_chunks_total))
        recording.transcription_chunks_completed = clamp_completed_to_total(
            recording.transcription_chunks_completed,
            recording.transcription_chunks_total,
        )
    if summary_chapters_completed is not None:
        effective_total = summary_chapters_total if summary_chapters_total is not None else recording.summary_chapters_total
        recording.summary_chapters_completed = clamp_completed_to_total(summary_chapters_completed, effective_total)
    if summary_chapters_total is not None:
        recording.summary_chapters_total = max(0, int(summary_chapters_total))
        recording.summary_chapters_completed = clamp_completed_to_total(
            recording.summary_chapters_completed,
            recording.summary_chapters_total,
        )
    if summary_sections_completed is not None:
        effective_total = summary_sections_total if summary_sections_total is not None else recording.summary_sections_total
        recording.summary_sections_completed = clamp_completed_to_total(summary_sections_completed, effective_total)
    if summary_sections_total is not None:
        recording.summary_sections_total = max(0, int(summary_sections_total))
        recording.summary_sections_completed = clamp_completed_to_total(
            recording.summary_sections_completed,
            recording.summary_sections_total,
        )
    if error_message is not None:
        recording.progress_error = error_message
    elif clear_error:
        recording.progress_error = None
    if mark_started:
        recording.progress_started_at = now

    recording.progress_updated_at = now


def save_recording_progress(
    db: Session,
    recording: Recording,
    *,
    commit: bool = True,
    **kwargs,
) -> None:
    """Apply recording progress fields and optionally commit immediately."""
    apply_recording_progress(recording, **kwargs)
    if commit:
        db.commit()
        db.refresh(recording)


def reset_recording_progress(
    recording: Recording,
    *,
    stage: str = "idle",
    message: str = "",
    percent: int | float = 0,
    clear_started_at: bool = True,
) -> None:
    """Reset all processing counters for a fresh processing attempt."""
    now = datetime.utcnow()
    recording.progress_stage = stage
    recording.progress_message = message
    recording.progress_percent = clamp_percent(percent)
    recording.transcription_chunks_completed = 0
    recording.transcription_chunks_total = 0
    recording.summary_chapters_completed = 0
    recording.summary_chapters_total = 0
    recording.summary_sections_completed = 0
    recording.summary_sections_total = 0
    recording.progress_error = None
    if clear_started_at:
        recording.progress_started_at = None
    recording.progress_updated_at = now
