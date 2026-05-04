"""
Processing orchestrator.

``process_recording(db, recording_id, language)``
    1. Validates the recording is in ``ready`` status, or ``error`` status for
       a retry against an already-finalized audio file.
    2. Validates that the WAV audio file is non-empty (more than the 44-byte
       WAV header) before calling the transcription API.
    3. Calls the multimodal transcription service on each stored WAV chunk and
       concatenates the chunk transcriptions into one full transcript.
    4. Runs chapter analysis on the combined transcript, so raw audio chunks do
       not become chapters.
    5. Normalises the chapters via :func:`~backend.services.chapter_parser.parse_llm_chapters`.
    6. For each analyzed chapter, calls the summarization service and persists
        Chapter / Transcription / Summary rows.
    7. Marks the recording as ``completed`` (or ``error`` on failure).

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
from backend.services.openrouter_client import (
    analyze_transcription_chapters,
    summarize_text,
    transcribe_audio,
)

# A WAV file with a 44-byte header and zero audio frames is considered empty.
# The configured minimum protects Gemini/OpenRouter from tiny VAD outputs.
_MIN_AUDIO_BYTES = settings.audio.min_transcription_audio_bytes

logger = logging.getLogger(__name__)


def _chunk_dir_for_audio(audio_path: Path) -> Path:
    return audio_path.with_name(f"{audio_path.stem}_chunks")


def _transcription_chunk_paths(audio_path: Path) -> list[Path]:
    """Return VAD chunk WAVs if present; otherwise fall back to the assembled WAV."""
    chunk_dir = _chunk_dir_for_audio(audio_path)
    if not chunk_dir.exists() or not chunk_dir.is_dir():
        return [audio_path]

    paths = sorted(chunk_dir.glob("*.wav"))
    return paths or [audio_path]

async def _transcribe_recording_audio(audio_path: Path, language: str) -> dict:
    """Transcribe all VAD chunks sequentially and concatenate plain text."""
    chunk_paths = _transcription_chunk_paths(audio_path)
    logger.info(
        "Transcribing recording audio via %d chunk(s): %s",
        len(chunk_paths),
        ", ".join(path.name for path in chunk_paths),
    )

    full_parts: list[str] = []

    for index, chunk_path in enumerate(chunk_paths, start=1):
        chunk_size = chunk_path.stat().st_size if chunk_path.exists() else 0
        if chunk_size < _MIN_AUDIO_BYTES:
            logger.warning(
                "Skipping too-short VAD chunk %s (%d bytes; minimum %d bytes)",
                chunk_path,
                chunk_size,
                _MIN_AUDIO_BYTES,
            )
            continue

        logger.info(
            "Transcribing VAD chunk %d/%d for %s (%d bytes)",
            index,
            len(chunk_paths),
            audio_path.name,
            chunk_size,
        )
        result = await transcribe_audio(chunk_path, language=language)
        chunk_full = str(result.get("full_transcription") or "").strip()
        if chunk_full:
            full_parts.append(chunk_full)

    if not full_parts:
        raise ProcessingError(
            f"No usable transcription chunks found for audio file '{audio_path}'."
        )

    return {
        "full_transcription": "\n\n".join(full_parts),
    }


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
        If the recording is not found or is not in ``ready``/``error``/``completed`` status.
    Exception
        Re-raises any error after marking the recording as ``error``.
    """
    recording: Recording | None = db.get(Recording, recording_id)

    if recording is None:
        raise ProcessingError(f"Recording {recording_id} not found.")

    if recording.status not in {"ready", "error", "completed"}:
        raise ProcessingError(
            f"Recording {recording_id} has status '{recording.status}'; "
            "expected 'ready', 'error', or 'completed'. Upload and finalize VAD-filtered "
            "audio first."
        )

    # Remove stale results before every attempt so a retry cannot append to or
    # display partial data from a previous failed run.
    stale_chapters = list(recording.chapters)
    if stale_chapters:
        logger.info(
            "Clearing %d stale chapter(s) before processing recording %d.",
            len(stale_chapters),
            recording_id,
        )
        for chapter in stale_chapters:
            db.delete(chapter)
        db.flush()

    recording.processed_at = None
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
        # Step 1 – Plain transcription (one LLM call per VAD chunk)
        # ------------------------------------------------------------------ #
        transcription_result: dict = await _transcribe_recording_audio(
            audio_path, language=language
        )
        full_transcription: str = transcription_result["full_transcription"]
        logger.info(
            "Transcription done for recording %d: %d chars", recording_id, len(full_transcription)
        )

        # ------------------------------------------------------------------ #
        # Step 2 – Global chapter analysis over the combined transcription
        # ------------------------------------------------------------------ #
        chapter_analysis_result = await analyze_transcription_chapters(
            full_transcription, language=language
        )
        parsed_chapters = parse_llm_chapters(chapter_analysis_result["chapters"])
        if not parsed_chapters:
            parsed_chapters = [
                {
                    "chapter_number": 1,
                    "title": "Full recording",
                    "transcription": full_transcription,
                }
            ]
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
