"""
Recording lifecycle endpoints.

Routes
------
POST   /api/recordings              — start a new recording for a book
GET    /api/recordings/{id}         — get recording detail with chapters
DELETE /api/recordings/{id}         — delete recording + audio file
POST   /api/recordings/{id}/process — trigger transcription + summarization pipeline
"""

import logging
import shutil
from uuid import uuid4
from pathlib import Path

from fastapi import APIRouter, Depends, File, Response, UploadFile, status
from sqlalchemy.orm import Session

from backend.core.database import get_db
from backend.core.config import settings
from backend.core.exceptions import bad_request, not_found
from backend.models.orm import Book, Recording
from backend.schemas.api import RecordingCreate, RecordingDetailOut, RecordingOut, RecordingProcessRequest
from backend.services.audio_pipeline import AudioPipelineError, append_raw_recording, finalize_raw_recording
from backend.services.processor import process_recording, ProcessingError
from backend.services.raw_audio_storage import save_raw_audio_upload

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/recordings", tags=["recordings"])


# ---------------------------------------------------------------------------
# Create (start a new recording session)
# ---------------------------------------------------------------------------


@router.post(
    "",
    response_model=RecordingOut,
    status_code=status.HTTP_201_CREATED,
    summary="Start a new recording session",
)
def create_recording(
    payload: RecordingCreate, db: Session = Depends(get_db)
) -> Recording:
    """
    Create a Recording row and allocate the future VAD-filtered WAV path.

    The browser later uploads one complete raw audio blob via
    ``POST /api/recordings/{recording_id}/audio``. Offline decode and VAD then
    write the transcription-ready WAV to ``settings.audio.vad_storage_dir``.
    """
    book = db.get(Book, payload.book_id)
    if book is None:
        raise not_found("Book", payload.book_id)

    # Determine the future VAD-filtered WAV path; the file is created after
    # complete raw upload + offline VAD.
    audio_dir = Path(settings.audio.vad_storage_dir)
    audio_dir.mkdir(parents=True, exist_ok=True)

    # Temporary placeholder — the real path is patched once the recording id
    # is known (after the first commit).
    recording = Recording(
        book_id=payload.book_id,
        audio_file_path="",  # filled below
        status="recording",
    )
    db.add(recording)
    db.commit()
    db.refresh(recording)

    # Now set the deterministic path based on the auto-incremented id.
    recording.audio_file_path = str(audio_dir / f"{recording.id}.wav")
    db.commit()
    db.refresh(recording)

    logger.info(
        "Created recording id=%d for book_id=%d path=%s",
        recording.id,
        payload.book_id,
        recording.audio_file_path,
    )
    return recording


# ---------------------------------------------------------------------------
# Upload complete browser audio and finalize it into VAD-filtered WAV
# ---------------------------------------------------------------------------


@router.post(
    "/{recording_id}/audio",
    response_model=RecordingOut,
    summary="Upload a complete raw recording and run offline VAD",
)
async def upload_recording_audio(
    recording_id: int,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
) -> Recording:
    """
    Save the complete browser-recorded audio blob, decode it offline, run VAD,
    and store the VAD-filtered WAV used for transcription.

    This endpoint intentionally does not fall back to raw audio when VAD output
    is empty.
    """
    recording = db.get(Recording, recording_id)
    if recording is None:
        raise not_found("Recording", recording_id)

    if recording.status in {"processing", "completed"}:
        raise bad_request(
            f"Recording {recording_id} cannot accept audio while status='{recording.status}'."
        )

    try:
        raw_path = await save_raw_audio_upload(recording_id, file)
        finalized = await finalize_raw_recording(raw_path, recording_id)
    except AudioPipelineError as exc:
        recording.status = "error"
        db.commit()
        raise bad_request(str(exc)) from exc
    except Exception as exc:
        recording.status = "error"
        db.commit()
        logger.error(
            "Unexpected audio finalization error for recording %d: %s",
            recording_id,
            exc,
            exc_info=True,
        )
        raise

    recording.audio_file_path = str(finalized.vad_path)
    recording.duration_seconds = finalized.vad_duration_seconds
    recording.status = "ready"
    db.commit()
    db.refresh(recording)

    logger.info(
        "Recording %d audio finalized: vad=%s duration=%.2fs segments=%d",
        recording_id,
        finalized.vad_path,
        finalized.vad_duration_seconds,
        finalized.speech_segments,
    )
    return recording


@router.post(
    "/{recording_id}/audio/append",
    response_model=RecordingOut,
    summary="Append browser audio to an existing recording and rerun offline VAD",
)
async def append_recording_audio(
    recording_id: int,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
) -> Recording:
    """
    Append a complete browser-recorded audio blob to an existing recording.

    The endpoint rewrites the recording's decoded/VAD audio as one combined
    chronological file and marks the recording ``ready``. The frontend then
    immediately calls the existing processing endpoint to refresh chapters,
    transcriptions, and summaries for the full combined recording.
    """
    recording = db.get(Recording, recording_id)
    if recording is None:
        raise not_found("Recording", recording_id)

    if recording.status in {"recording", "processing"}:
        raise bad_request(
            f"Recording {recording_id} cannot be continued while status='{recording.status}'."
        )

    try:
        raw_path = await save_raw_audio_upload(recording_id, file, suffix=f"-append-{uuid4().hex}")
        finalized = await append_raw_recording(raw_path, recording_id)
    except AudioPipelineError as exc:
        recording.status = "error"
        recording.processed_at = None
        db.commit()
        raise bad_request(str(exc)) from exc
    except Exception as exc:
        recording.status = "error"
        recording.processed_at = None
        db.commit()
        logger.error(
            "Unexpected audio append finalization error for recording %d: %s",
            recording_id,
            exc,
            exc_info=True,
        )
        raise

    recording.audio_file_path = str(finalized.vad_path)
    recording.duration_seconds = finalized.vad_duration_seconds
    recording.status = "ready"
    recording.processed_at = None
    db.commit()
    db.refresh(recording)

    logger.info(
        "Recording %d audio appended: vad=%s duration=%.2fs segments=%d",
        recording_id,
        finalized.vad_path,
        finalized.vad_duration_seconds,
        finalized.speech_segments,
    )
    return recording


# ---------------------------------------------------------------------------
# Read (with full chapter tree)
# ---------------------------------------------------------------------------


@router.get(
    "/{recording_id}",
    response_model=RecordingDetailOut,
    summary="Get a recording with its chapters",
)
def get_recording(recording_id: int, db: Session = Depends(get_db)) -> RecordingDetailOut:
    recording = db.get(Recording, recording_id)
    if recording is None:
        raise not_found("Recording", recording_id)

    detail = RecordingDetailOut.model_validate(recording)
    # Set the browser-playable URL for the VAD-filtered WAV file.
    # The file is always named {recording_id}.wav under data/vad_audio/.
    detail.audio_url = f"/media/audio/{recording.id}.wav"
    return detail


# ---------------------------------------------------------------------------
# Process (transcription + summarization pipeline)
# ---------------------------------------------------------------------------


@router.post(
    "/{recording_id}/process",
    response_model=RecordingDetailOut,
    summary="Transcribe and summarize a completed recording",
)
async def trigger_processing(
    recording_id: int,
    request: RecordingProcessRequest,
    db: Session = Depends(get_db),
) -> Recording:
    """
    Run the full pipeline on a ``ready`` recording:

    1. Transcribe the VAD-filtered WAV chunks via a multimodal LLM.
    2. Concatenate chunk transcriptions into one complete transcript.
    3. Analyze chapter boundaries from the complete transcript only.
    4. Summarize each analyzed chapter via OpenRouter LLM.
    5. Persist chapters / transcriptions / summaries.

    The recording must be in ``ready`` status for the first processing attempt,
    ``error`` status for a retry, or ``completed`` status for reprocessing
    against the already-finalized audio file.
    On error, the recording is marked ``error`` and a clear API error is
    returned.

    Request body
    ------------
    ``language`` (str, default ``"ru"``): BCP-47 language code for transcription.
    """
    recording = db.get(Recording, recording_id)
    if recording is None:
        raise not_found("Recording", recording_id)

    logger.info(
        "Processing request received: recording_id=%d status=%s language=%s",
        recording_id,
        recording.status,
        request.language,
    )

    if recording.status == "processing":
        logger.warning("Processing request rejected: recording_id=%d already processing", recording_id)
        raise bad_request(
            f"Recording {recording_id} is already being processed."
        )
    if recording.status not in {"ready", "error", "completed"}:
        logger.warning(
            "Processing request rejected: recording_id=%d unexpected status=%s",
            recording_id,
            recording.status,
        )
        raise bad_request(
            f"Recording {recording_id} is not ready for processing retry "
            f"(status='{recording.status}'). Upload and finalize audio first."
        )

    try:
        result = await process_recording(db, recording_id, language=request.language)
    except ProcessingError as exc:
        logger.warning("Processing request failed: recording_id=%d detail=%s", recording_id, exc)
        raise bad_request(str(exc)) from exc

    logger.info("Processing request completed: recording_id=%d status=%s", recording_id, result.status)

    return result


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------


@router.delete(
    "/{recording_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a recording and its audio file",
)
def delete_recording(recording_id: int, db: Session = Depends(get_db)) -> Response:
    """
    Delete a recording row (cascades to chapters, transcriptions, summaries)
    and removes the associated WAV file from disk.
    """
    recording = db.get(Recording, recording_id)
    if recording is None:
        raise not_found("Recording", recording_id)

    audio_path = Path(recording.audio_file_path)
    chunk_dir = audio_path.with_name(f"{audio_path.stem}_chunks")

    db.delete(recording)
    db.commit()
    logger.info("Deleted recording id=%d", recording_id)

    # Best-effort file removal — don't fail if the file is missing.
    if audio_path.exists():
        try:
            audio_path.unlink()
            logger.info("Removed audio file %s", audio_path)
        except OSError as exc:
            logger.warning("Could not remove audio file %s: %s", audio_path, exc)

    if chunk_dir.exists() and chunk_dir.is_dir():
        try:
            shutil.rmtree(chunk_dir)
            logger.info("Removed VAD chunk directory %s", chunk_dir)
        except OSError as exc:
            logger.warning("Could not remove VAD chunk directory %s: %s", chunk_dir, exc)

    return Response(status_code=status.HTTP_204_NO_CONTENT)
