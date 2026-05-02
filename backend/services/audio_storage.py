"""
WAV file writer for accumulated speech segments.

Usage
-----
    writer = AudioWriter(recording_id=42)
    await writer.append(speech_segment)   # called for each VAD-approved segment
    await writer.finalize()               # flush & close; call once when stream ends
    path = writer.path                    # pathlib.Path to the written WAV file
"""

import asyncio
import logging
import os
import wave
from pathlib import Path
from typing import Optional

from backend.core.config import settings
from backend.services.vad_service import SpeechSegment

logger = logging.getLogger(__name__)


class AudioWriter:
    """
    Writes PCM s16le speech segments to a WAV file on disk.

    The file is opened immediately in the constructor.  Call :meth:`finalize`
    (or use as an async context manager) to close it safely.

    Files are stored at::

        {settings.audio.storage_dir}/{recording_id}.wav
    """

    def __init__(self, recording_id: int) -> None:
        self.recording_id = recording_id
        self._dir: Path = Path(settings.audio.storage_dir)
        self._dir.mkdir(parents=True, exist_ok=True)

        self.path: Path = self._dir / f"{recording_id}.wav"
        self._wav: Optional[wave.Wave_write] = None
        self._total_frames: int = 0  # PCM sample frames written so far
        self._lock = asyncio.Lock()

        self._open_wav()
        logger.info(
            "AudioWriter: opened %s (recording_id=%d)", self.path, recording_id
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def append(self, segment: SpeechSegment) -> None:
        """Append a completed speech segment to the WAV file."""
        async with self._lock:
            if self._wav is None:
                logger.warning(
                    "AudioWriter.append called after finalize (recording_id=%d)",
                    self.recording_id,
                )
                return
            self._wav.writeframes(segment.pcm_bytes)
            frames_written = len(segment.pcm_bytes) // 2  # 16-bit → 2 bytes/sample
            self._total_frames += frames_written
            logger.debug(
                "AudioWriter: appended %d ms / %d frames (recording_id=%d)",
                segment.duration_ms,
                frames_written,
                self.recording_id,
            )

    async def finalize(self) -> float:
        """
        Close the WAV file and return the total duration in seconds.

        Safe to call more than once (subsequent calls are no-ops).
        """
        async with self._lock:
            if self._wav is not None:
                self._wav.close()
                self._wav = None
                logger.info(
                    "AudioWriter: finalized %s — %.2f s (recording_id=%d)",
                    self.path,
                    self.duration_seconds,
                    self.recording_id,
                )
            return self.duration_seconds

    async def close(self) -> None:
        """Alias for :meth:`finalize`; provided for symmetry."""
        await self.finalize()

    # ------------------------------------------------------------------
    # Async context manager
    # ------------------------------------------------------------------

    async def __aenter__(self) -> "AudioWriter":
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.finalize()

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def duration_seconds(self) -> float:
        """Duration of audio written so far, in seconds."""
        if settings.audio.sample_rate == 0:
            return 0.0
        return self._total_frames / settings.audio.sample_rate

    @property
    def file_exists(self) -> bool:
        return self.path.exists() and self.path.stat().st_size > 44  # > WAV header

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _open_wav(self) -> None:
        """Open (or overwrite) the WAV file with the configured PCM parameters."""
        wav = wave.open(str(self.path), "wb")
        wav.setnchannels(1)
        wav.setsampwidth(2)  # 16-bit PCM
        wav.setframerate(settings.audio.sample_rate)
        self._wav = wav
