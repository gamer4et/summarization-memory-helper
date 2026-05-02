"""
WebSocket endpoint for real-time audio streaming.

URL
---
    /ws/audio/{recording_id}

Protocol
--------
1. Client opens the WebSocket with a valid ``recording_id``.
2. Client sends binary frames of ``audio/webm;codecs=opus`` (as emitted by
   the browser's ``MediaRecorder``).
3. The server pipes each chunk through an ffmpeg subprocess that outputs raw
   PCM s16le at 16 kHz / mono.
4. PCM bytes are fed to a :class:`~backend.services.vad_service.VADProcessor`.
5. Completed speech segments are appended to a WAV file via
   :class:`~backend.services.audio_storage.AudioWriter`.
6. The client sends JSON ``{"action": "stop"}`` when the user clicks Stop.
7. The server flushes remaining speech, finalises the WAV, updates the
   Recording row to ``ready``, and closes the socket.

Error handling
--------------
Disconnection at any point triggers the same finalisation path so no data is
lost even on abrupt disconnects.

ffmpeg note
-----------
The server expects ``ffmpeg`` to be available on ``$PATH``.  A single ffmpeg
process is kept alive for the duration of the WebSocket connection.  It reads
WebM/Opus from stdin and writes raw PCM to stdout.  Audio is flushed in
non-blocking reads so that small chunks are processed incrementally.
"""

import asyncio
import json
import logging
import subprocess
from pathlib import Path

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from sqlalchemy.orm import Session

from backend.core.config import settings
from backend.core.database import SessionLocal
from backend.models.orm import Recording
from backend.services.audio_storage import AudioWriter
from backend.services.vad_service import SpeechSegment, VADProcessor

logger = logging.getLogger(__name__)

router = APIRouter(tags=["websocket"])

# ---------------------------------------------------------------------------
# ffmpeg helpers
# ---------------------------------------------------------------------------

_FFMPEG_CMD_TEMPLATE = [
    "ffmpeg",
    "-loglevel", "error",       # suppress progress spam
    "-i", "pipe:0",             # read WebM/Opus from stdin
    "-f", "s16le",              # output raw signed 16-bit little-endian PCM
    "-acodec", "pcm_s16le",
    "-ar", "{sample_rate}",     # target sample rate (e.g. 16000)
    "-ac", "1",                 # mono
    "pipe:1",                   # write PCM to stdout
]


def _build_ffmpeg_cmd() -> list[str]:
    return [
        tok.format(sample_rate=settings.audio.sample_rate)
        for tok in _FFMPEG_CMD_TEMPLATE
    ]


def _start_ffmpeg() -> subprocess.Popen:
    """Spawn a persistent ffmpeg process for WebM→PCM transcoding."""
    return subprocess.Popen(
        _build_ffmpeg_cmd(),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )


async def _read_pcm_nonblocking(
    proc: subprocess.Popen, hint_bytes: int = 4096
) -> bytes:
    """
    Read up to ``hint_bytes`` of PCM output from ffmpeg without blocking the
    event loop.
    """
    loop = asyncio.get_event_loop()
    # os.read is a true non-blocking call on the pipe fd in most POSIX
    # environments; for Windows we fall back to a thread executor.
    try:
        data = await loop.run_in_executor(
            None, proc.stdout.read1 if hasattr(proc.stdout, "read1") else proc.stdout.read, hint_bytes  # type: ignore[arg-type]
        )
    except (ValueError, OSError):
        data = b""
    return data


# ---------------------------------------------------------------------------
# WebSocket handler
# ---------------------------------------------------------------------------


@router.websocket("/ws/audio/{recording_id}")
async def audio_stream(websocket: WebSocket, recording_id: int) -> None:
    """
    Stream audio chunks from the browser, run VAD, and write speech to WAV.
    """
    await websocket.accept()
    logger.info("WS connected: recording_id=%d", recording_id)

    # Validate recording exists.
    db: Session = SessionLocal()
    try:
        recording: Recording | None = db.get(Recording, recording_id)
        if recording is None:
            await websocket.send_json(
                {"status": "error", "detail": f"Recording {recording_id} not found."}
            )
            await websocket.close(code=4004)
            return
    finally:
        db.close()

    vad = VADProcessor(recording_id=recording_id)
    writer = AudioWriter(recording_id=recording_id)
    ffmpeg_proc: subprocess.Popen | None = None

    # Accumulate every PCM chunk produced by ffmpeg so we can write raw audio
    # as a fallback if VAD never triggers (e.g. quiet mic, aggressive settings).
    raw_pcm_chunks: list[bytes] = []
    raw_pcm_total: int = 0

    try:
        ffmpeg_proc = _start_ffmpeg()
        logger.debug("ffmpeg started for recording_id=%d", recording_id)

        while True:
            message = await websocket.receive()

            # ---------------------------------------------------------------- #
            # Control message (JSON text)
            # ---------------------------------------------------------------- #
            if "text" in message:
                try:
                    data = json.loads(message["text"])
                except json.JSONDecodeError:
                    await websocket.send_json(
                        {"status": "error", "detail": "Invalid JSON text frame."}
                    )
                    continue

                if data.get("action") == "stop":
                    logger.info("Stop signal received for recording_id=%d", recording_id)
                    break

                logger.debug(
                    "Unknown WS action '%s' for recording_id=%d",
                    data.get("action"),
                    recording_id,
                )

            # ---------------------------------------------------------------- #
            # Binary audio chunk (WebM / Opus)
            # ---------------------------------------------------------------- #
            elif "bytes" in message:
                webm_chunk: bytes = message["bytes"]
                if not webm_chunk:
                    continue

                # Feed to ffmpeg stdin.
                try:
                    ffmpeg_proc.stdin.write(webm_chunk)
                    ffmpeg_proc.stdin.flush()
                except (BrokenPipeError, OSError) as exc:
                    logger.error(
                        "ffmpeg stdin error for recording_id=%d: %s", recording_id, exc
                    )
                    break

                # Read whatever PCM ffmpeg has produced so far.
                pcm_bytes = await _read_pcm_nonblocking(ffmpeg_proc)
                if not pcm_bytes:
                    continue

                # Track all raw PCM for VAD fallback.
                raw_pcm_chunks.append(pcm_bytes)
                raw_pcm_total += len(pcm_bytes)

                # VAD processing.
                segments = vad.process(pcm_bytes)
                total_ms = 0
                for seg in segments:
                    await writer.append(seg)
                    total_ms += seg.duration_ms

                if total_ms:
                    await websocket.send_json(
                        {"status": "chunk_accepted", "ms": total_ms}
                    )

    except WebSocketDisconnect:
        logger.info("WS disconnected (recording_id=%d)", recording_id)

    except Exception as exc:
        logger.error(
            "Unexpected WS error (recording_id=%d): %s", recording_id, exc, exc_info=True
        )

    finally:
        # -------------------------------------------------------------------- #
        # Clean-up: flush ffmpeg, flush VAD, finalise WAV, update DB status.
        # -------------------------------------------------------------------- #
        if ffmpeg_proc is not None:
            try:
                ffmpeg_proc.stdin.close()
            except OSError:
                pass
            # Drain remaining PCM and track it for the fallback counter.
            # Use run_in_executor so we don't block the event loop.
            try:
                loop = asyncio.get_running_loop()
                remaining_pcm = await loop.run_in_executor(
                    None, ffmpeg_proc.stdout.read
                )
                if remaining_pcm:
                    raw_pcm_chunks.append(remaining_pcm)
                    raw_pcm_total += len(remaining_pcm)
                    for seg in vad.process(remaining_pcm):
                        await writer.append(seg)
            except OSError:
                pass
            # Wait for ffmpeg to exit; kill it if it hangs.
            # TimeoutExpired MUST NOT leak out of finally or writer.finalize()
            # would be skipped and the WAV file would remain 0 bytes.
            try:
                ffmpeg_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                logger.warning(
                    "ffmpeg did not exit within 5 s for recording_id=%d — killing",
                    recording_id,
                )
                ffmpeg_proc.kill()
                ffmpeg_proc.wait()

        # Flush any trailing speech.
        trailing = vad.flush()
        if trailing:
            await writer.append(trailing)

        # -------------------------------------------------------------------- #
        # VAD fallback: if VAD never triggered but we received PCM data,
        # write all accumulated raw PCM to the WAV file so the transcription
        # API receives a non-empty file.  This handles cases where the mic is
        # quiet or vad_aggressiveness is too high.
        # -------------------------------------------------------------------- #
        if writer.duration_seconds == 0 and raw_pcm_total > 0:
            all_pcm = b"".join(raw_pcm_chunks)
            duration_ms = int(
                raw_pcm_total / 2 / settings.audio.sample_rate * 1000
            )
            fallback_seg = SpeechSegment(pcm_bytes=all_pcm, duration_ms=duration_ms)
            await writer.append(fallback_seg)
            logger.warning(
                "VAD detected no speech for recording_id=%d; "
                "writing %d bytes of raw PCM as fallback (%.1f s)",
                recording_id,
                raw_pcm_total,
                raw_pcm_total / 2 / settings.audio.sample_rate,
            )

        duration = await writer.finalize()

        # Update recording status → "ready".
        db = SessionLocal()
        try:
            rec: Recording | None = db.get(Recording, recording_id)
            if rec is not None:
                rec.status = "ready"
                rec.duration_seconds = duration
                db.commit()
                logger.info(
                    "Recording %d finalised: %.2f s, status=ready", recording_id, duration
                )
        except Exception as exc:
            db.rollback()
            logger.error(
                "Failed to update recording %d status: %s", recording_id, exc
            )
        finally:
            db.close()

        # Send final status before closing.
        try:
            await websocket.send_json(
                {
                    "status": "stopped",
                    "recording_id": recording_id,
                    "duration_seconds": duration,
                }
            )
        except Exception:
            pass  # socket may already be closed
