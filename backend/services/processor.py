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

from openai import APIStatusError
from sqlalchemy.orm import Session

from backend.core.config import settings
from backend.models.orm import Chapter, Recording, Summary, Transcription
from backend.services.chapter_parser import parse_llm_chapters
from backend.services.openrouter_client import (
    SUMMARY_SECTION_ORDER,
    analyze_transcription_chapters,
    summarize_chapter_sections,
    transcribe_audio,
)
from backend.services.processing_progress import save_recording_progress

# A WAV file with a 44-byte header and zero audio frames is considered empty.
# The configured minimum protects Gemini/OpenRouter from tiny VAD outputs.
_MIN_AUDIO_BYTES = settings.audio.min_transcription_audio_bytes

logger = logging.getLogger(__name__)

_TRANSCRIPTION_CONTEXT_CHARS = 4_000


class ProcessingError(RuntimeError):
    """Raised when processing cannot proceed due to an invalid state."""


def _chunk_dir_for_audio(audio_path: Path) -> Path:
    return audio_path.with_name(f"{audio_path.stem}_chunks")


def _transcription_chunk_paths(audio_path: Path) -> list[Path]:
    """Return VAD chunk WAVs if present; otherwise fall back to the assembled WAV."""
    chunk_dir = _chunk_dir_for_audio(audio_path)
    if not chunk_dir.exists() or not chunk_dir.is_dir():
        return [audio_path]

    paths = sorted(chunk_dir.glob("*.wav"))
    return paths or [audio_path]


def _processing_error_for_api_status(exc: APIStatusError, chunk_path: Path) -> ProcessingError:
    """Convert known OpenRouter client-size failures to actionable processing errors."""
    if exc.status_code == 413:
        return ProcessingError(
            "OpenRouter rejected transcription audio as too large (HTTP 413 Payload Too Large): "
            f"chunk='{chunk_path}', size={chunk_path.stat().st_size if chunk_path.exists() else 0} bytes. "
            "Regenerate VAD chunks or reduce transcription chunk duration so each "
            "base64 JSON request stays below the provider gateway limit."
        )
    return ProcessingError(f"OpenRouter transcription failed for chunk '{chunk_path}': {exc}")


async def _transcribe_recording_audio(audio_path: Path, language: str, progress_callback=None) -> dict:
    """Transcribe all VAD chunks sequentially and concatenate plain text."""
    chunk_paths = _transcription_chunk_paths(audio_path)
    logger.info(
        "Transcribing recording audio via %d chunk(s): %s",
        len(chunk_paths),
        ", ".join(path.name for path in chunk_paths),
    )

    usable_chunk_paths = []
    for chunk_path in chunk_paths:
        chunk_size = chunk_path.stat().st_size if chunk_path.exists() else 0
        if chunk_size < _MIN_AUDIO_BYTES:
            logger.warning(
                "Skipping too-short VAD chunk %s (%d bytes; minimum %d bytes)",
                chunk_path,
                chunk_size,
                _MIN_AUDIO_BYTES,
            )
            continue
        usable_chunk_paths.append(chunk_path)

    if progress_callback is not None:
        progress_callback(
            0,
            len(usable_chunk_paths),
            f"Preparing to transcribe {len(usable_chunk_paths)} audio chunk(s).",
        )

    if not usable_chunk_paths:
        raise ProcessingError(
            f"No usable transcription chunks found for audio file '{audio_path}'."
        )

    full_parts: list[str] = []

    for index, chunk_path in enumerate(usable_chunk_paths, start=1):
        chunk_size = chunk_path.stat().st_size if chunk_path.exists() else 0
        logger.info(
            "Transcribing VAD chunk %d/%d for %s (%d bytes)",
            index,
            len(usable_chunk_paths),
            audio_path.name,
            chunk_size,
        )
        if progress_callback is not None:
            progress_callback(
                index - 1,
                len(usable_chunk_paths),
                f"Transcribing audio chunk {index}/{len(usable_chunk_paths)}…",
            )
        previous_context = "\n\n".join(full_parts)[-_TRANSCRIPTION_CONTEXT_CHARS:]
        try:
            result = await transcribe_audio(
                chunk_path,
                language=language,
                previous_context=previous_context,
            )
        except APIStatusError as exc:
            raise _processing_error_for_api_status(exc, chunk_path) from exc
        chunk_full = str(result.get("full_transcription") or "").strip()
        if chunk_full:
            full_parts.append(chunk_full)
        if progress_callback is not None:
            progress_callback(
                index,
                len(usable_chunk_paths),
                f"Transcribed audio chunk {index}/{len(usable_chunk_paths)}.",
            )

    if not full_parts:
        raise ProcessingError(
            f"No usable transcription chunks found for audio file '{audio_path}'."
        )

    return {
        "full_transcription": "\n\n".join(full_parts),
    }


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
        save_recording_progress(
            db,
            recording,
            stage="validating",
            message="Validating VAD-filtered audio before transcription…",
            percent=5,
            transcription_chunks_completed=0,
            transcription_chunks_total=0,
            summary_chapters_completed=0,
            summary_chapters_total=0,
            summary_sections_completed=0,
            summary_sections_total=0,
            clear_error=True,
            mark_started=True,
        )
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
        def update_transcription_progress(completed: int, total: int, message: str) -> None:
            fraction = completed / total if total else 0
            save_recording_progress(
                db,
                recording,
                stage="transcribing",
                message=message,
                percent=10 + (fraction * 35),
                transcription_chunks_completed=completed,
                transcription_chunks_total=total,
            )

        transcription_result: dict = await _transcribe_recording_audio(
            audio_path,
            language=language,
            progress_callback=update_transcription_progress,
        )
        full_transcription: str = transcription_result["full_transcription"]
        save_recording_progress(
            db,
            recording,
            stage="transcribed",
            message="Transcription complete. Preparing chapter analysis…",
            percent=50,
        )
        logger.info(
            "Transcription done for recording %d: %d chars",
            recording_id,
            len(full_transcription),
        )

        # ------------------------------------------------------------------ #
        # Step 2 – Global chapter analysis over the combined transcription
        # ------------------------------------------------------------------ #
        save_recording_progress(
            db,
            recording,
            stage="analyzing_chapters",
            message="Analyzing chapter boundaries from the full transcription…",
            percent=55,
        )
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
        # Step 3 – Generate summaries before opening a DB write transaction
        # ------------------------------------------------------------------ #
        # SQLite allows only one writer at a time. Do not hold an uncommitted
        # INSERT/flush transaction while waiting for slow external LLM calls.
        summary_sections_per_chapter = len(SUMMARY_SECTION_ORDER)
        summary_sections_total = len(parsed_chapters) * summary_sections_per_chapter
        save_recording_progress(
            db,
            recording,
            stage="summarizing",
            message=f"Generating {summary_sections_total} summary section(s) across {len(parsed_chapters)} chapter(s)…",
            percent=60,
            summary_chapters_completed=0,
            summary_chapters_total=len(parsed_chapters),
            summary_sections_completed=0,
            summary_sections_total=summary_sections_total,
        )
        for idx, pc in enumerate(parsed_chapters, start=1):
            logger.info(
                "Generating summary for chapter %d/%d before DB persistence",
                idx,
                len(parsed_chapters),
            )
            save_recording_progress(
                db,
                recording,
                stage="summarizing",
                message=f"Summarizing chapter {idx}/{len(parsed_chapters)}…",
                percent=60 + (((idx - 1) / len(parsed_chapters)) * 30),
                summary_chapters_completed=idx - 1,
                summary_chapters_total=len(parsed_chapters),
                summary_sections_completed=(idx - 1) * summary_sections_per_chapter,
                summary_sections_total=summary_sections_total,
            )
            chapter_sections_completed = 0
            completed_section_keys: set[str] = set()

            def update_summary_section_progress(section_key: str) -> None:
                nonlocal chapter_sections_completed
                if section_key in completed_section_keys:
                    logger.warning(
                        "Duplicate summary section progress callback ignored: recording=%d chapter=%d section=%s",
                        recording_id,
                        idx,
                        section_key,
                    )
                    return

                completed_section_keys.add(section_key)
                chapter_sections_completed = min(len(completed_section_keys), summary_sections_per_chapter)
                overall_sections_completed = ((idx - 1) * summary_sections_per_chapter) + chapter_sections_completed
                if overall_sections_completed > summary_sections_total:
                    logger.warning(
                        "Summary section progress overshoot clamped: recording=%d completed=%d total=%d",
                        recording_id,
                        overall_sections_completed,
                        summary_sections_total,
                    )
                    overall_sections_completed = summary_sections_total
                section_fraction = overall_sections_completed / summary_sections_total if summary_sections_total else 0
                save_recording_progress(
                    db,
                    recording,
                    stage="summarizing",
                    message=(
                        f"Summarized section {chapter_sections_completed}/{summary_sections_per_chapter} "
                        f"({section_key}) for chapter {idx}/{len(parsed_chapters)}."
                    ),
                    percent=60 + (section_fraction * 30),
                    summary_chapters_completed=idx - 1,
                    summary_chapters_total=len(parsed_chapters),
                    summary_sections_completed=overall_sections_completed,
                    summary_sections_total=summary_sections_total,
                )

            pc["summary_result"] = await summarize_chapter_sections(
                pc["transcription"],
                progress_callback=update_summary_section_progress,
            )
            save_recording_progress(
                db,
                recording,
                stage="summarizing",
                message=f"Summarized chapter {idx}/{len(parsed_chapters)}.",
                percent=60 + (((idx * summary_sections_per_chapter) / summary_sections_total) * 30),
                summary_chapters_completed=idx,
                summary_chapters_total=len(parsed_chapters),
                summary_sections_completed=idx * summary_sections_per_chapter,
                summary_sections_total=summary_sections_total,
            )

        # ------------------------------------------------------------------ #
        # Step 4 – Persist chapters, transcriptions, and summaries
        # ------------------------------------------------------------------ #
        save_recording_progress(
            db,
            recording,
            stage="persisting",
            message="Saving chapters, transcriptions, and summaries…",
            percent=95,
        )
        for idx, pc in enumerate(parsed_chapters, start=1):
            chapter_number = pc["chapter_number"] if pc["chapter_number"] else idx
            summary_result = pc["summary_result"]
            summary_sections = summary_result["sections"]

            chapter = Chapter(
                recording_id=recording.id,
                chapter_number=chapter_number,
                title=pc["title"],
                # Audio alignment is not implemented; default to 0.
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

            db.add(
                Summary(
                    chapter_id=chapter.id,
                    summary_text=str(summary_result["summary_text"]),
                    graphs_markdown=summary_sections.get("graphs") or None,
                    definitions_markdown=summary_sections.get("definitions") or None,
                    tables_markdown=summary_sections.get("tables") or None,
                    dense_summary_markdown=summary_sections.get("dense_summary") or None,
                    key_facts_markdown=summary_sections.get("key_facts") or None,
                    triples_markdown=summary_sections.get("triples") or None,
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
        # Step 5 – Mark completed
        # ------------------------------------------------------------------ #
        recording.status = "completed"
        recording.processed_at = datetime.utcnow()
        save_recording_progress(
            db,
            recording,
            stage="completed",
            message="Processing complete.",
            percent=100,
            transcription_chunks_completed=recording.transcription_chunks_total or 0,
            summary_chapters_completed=recording.summary_chapters_total or 0,
            summary_sections_completed=recording.summary_sections_total or 0,
            clear_error=True,
            commit=False,
        )
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
                save_recording_progress(
                    db,
                    rec,
                    stage="error",
                    message="Processing failed.",
                    error_message=str(exc),
                    commit=False,
                )
                db.commit()
        except Exception:
            db.rollback()
        raise
