"""
Processing orchestrator.

``process_recording(db, recording_id, language)``
    1. Validates the recording is in ``ready`` status.
    2. Validates that the WAV audio file is non-empty (more than the 44-byte
       WAV header) before calling the transcription API.
    3. Calls the multimodal transcription service on the stored WAV file,
       returning full transcription text + structured chapters list.
    4. Normalises the chapters via :func:`~backend.services.chapter_parser.parse_llm_chapters`.
    5. For each chapter, calls the summarization service and persists
       Chapter / Transcription / Summary rows.
    6. Marks the recording as ``completed`` (or ``error`` on failure).

This function is called from the REST endpoint
``POST /api/recordings/{id}/process``.
"""

import logging
from datetime import datetime
from pathlib import Path

from sqlalchemy.orm import Session

from backend.core.config import settings
from backend.models.orm import Chapter, Recording, Summary, Transcription
from backend.services.chapter_parser import parse_llm_chapters
from backend.services.openrouter_client import transcribe_audio, summarize_text

# A WAV file with a 44-byte header and zero audio frames is considered empty.
# The configured minimum protects Gemini/OpenRouter from tiny VAD outputs.
_MIN_AUDIO_BYTES = settings.audio.min_transcription_audio_bytes

logger = logging.getLogger(__name__)


class ProcessingError(RuntimeError):
    """Raised when processing cannot proceed due to an invalid state."""


async def process_recording(
    db: Session, recording_id: int, language: str = "ru"
) -> Recording:
    """
    Run the full transcription → chapter detection → summarization pipeline.

    Parameters
    ----------
    db:
        An active SQLAlchemy session (will be committed on success).
    recording_id:
        Primary key of the :class:`~backend.models.orm.Recording` to process.
    language:
        BCP-47 language code spoken in the recording (e.g. ``"ru"``, ``"en"``).
        Passed through to the transcription LLM.

    Returns
    -------
    Recording
        The refreshed ORM object with all chapters populated.

    Raises
    ------
    ProcessingError
        If the recording is not found or is not in ``ready`` status.
    Exception
        Re-raises any error after marking the recording as ``error``.
    """
    recording: Recording | None = db.get(Recording, recording_id)

    if recording is None:
        raise ProcessingError(f"Recording {recording_id} not found.")

    if recording.status != "ready":
        raise ProcessingError(
            f"Recording {recording_id} has status '{recording.status}'; "
            "expected 'ready'. Upload and finalize VAD-filtered audio first."
        )

    # Mark as processing to prevent duplicate runs.
    recording.status = "processing"
    db.commit()
    logger.info("Processing recording %d — file: %s", recording_id, recording.audio_file_path)

    try:
        # ------------------------------------------------------------------ #
        # Step 0 – Validate that the VAD-filtered WAV has meaningful audio content
        # ------------------------------------------------------------------ #
        audio_path = Path(recording.audio_file_path)
        audio_size = audio_path.stat().st_size if audio_path.exists() else 0
        logger.info(
            "Recording %d audio file: %s — %d bytes",
            recording_id,
            audio_path.name,
            audio_size,
        )
        if audio_size < _MIN_AUDIO_BYTES:
            raise ProcessingError(
                f"VAD-filtered audio file is empty or too short ({audio_size} bytes; "
                f"minimum {_MIN_AUDIO_BYTES} bytes). No transcription fallback to raw "
                "audio is enabled — please record again and speak clearly."
            )

        # ------------------------------------------------------------------ #
        # Step 1 – Transcription + chapter detection (single LLM call)
        # ------------------------------------------------------------------ #
        transcription_result: dict = await transcribe_audio(
            recording.audio_file_path, language=language
        )
        full_transcription: str = transcription_result["full_transcription"]
        logger.info(
            "Transcription done for recording %d: %d chars", recording_id, len(full_transcription)
        )

        # ------------------------------------------------------------------ #
        # Step 2 – Normalise the LLM chapter output
        # ------------------------------------------------------------------ #
        parsed_chapters = parse_llm_chapters(transcription_result["chapters"])
        logger.info(
            "Detected %d chapter(s) for recording %d",
            len(parsed_chapters),
            recording_id,
        )

        # ------------------------------------------------------------------ #
        # Step 3 – Persist chapters, transcriptions, and summaries
        # ------------------------------------------------------------------ #
        for idx, pc in enumerate(parsed_chapters, start=1):
            chapter_number = pc["chapter_number"] if pc["chapter_number"] else idx

            chapter = Chapter(
                recording_id=recording.id,
                chapter_number=chapter_number,
                title=pc["title"],
                # Audio alignment is not implemented yet; default to 0.
                start_offset_ms=0,
                end_offset_ms=0,
            )
            db.add(chapter)
            db.flush()  # get chapter.id

            db.add(
                Transcription(
                    chapter_id=chapter.id,
                    raw_text=pc["transcription"],
                )
            )

            # Summarize the chapter text.
            summary_text: str = await summarize_text(pc["transcription"])
            db.add(
                Summary(
                    chapter_id=chapter.id,
                    summary_text=summary_text,
                    model_used=settings.openrouter.summarization.model,
                )
            )

            logger.debug(
                "Chapter %d/%d persisted for recording %d",
                idx,
                len(parsed_chapters),
                recording_id,
            )

        # ------------------------------------------------------------------ #
        # Step 4 – Mark completed
        # ------------------------------------------------------------------ #
        recording.status = "completed"
        recording.processed_at = datetime.utcnow()
        db.commit()
        db.refresh(recording)

        logger.info("Recording %d processing complete.", recording_id)
        return recording

    except Exception as exc:
        # Roll back any partial chapter/transcription/summary inserts.
        db.rollback()
        logger.error(
            "Error processing recording %d: %s", recording_id, exc, exc_info=True
        )
        # Re-fetch in a clean state and mark as error.
        try:
            rec = db.get(Recording, recording_id)
            if rec is not None:
                rec.status = "error"
                db.commit()
        except Exception:
            db.rollback()
        raise
