"""
Offline audio finalization pipeline.

This module turns a complete raw browser recording into two diagnostic files:

* ``decoded_storage_dir/{recording_id}.wav`` — full decoded session, 16 kHz mono
* ``vad_storage_dir/{recording_id}.wav`` — VAD-filtered speech used for Gemini

No fallback from VAD output to raw audio is performed. If VAD finds no speech,
callers receive a clear :class:`AudioPipelineError` and transcription must not
run.
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
import wave
from dataclasses import dataclass
from pathlib import Path

from backend.core.config import settings
from backend.services.vad_service import VADProcessor

logger = logging.getLogger(__name__)


class AudioPipelineError(RuntimeError):
    """Raised when raw audio cannot be decoded or VAD output is unusable."""


@dataclass(frozen=True)
class FinalizedAudio:
    """Paths and metrics produced by the offline audio finalization pipeline."""

    raw_path: Path
    decoded_path: Path
    vad_path: Path
    decoded_bytes: int
    vad_bytes: int
    vad_duration_seconds: float
    speech_segments: int


async def finalize_raw_recording(raw_path: Path, recording_id: int) -> FinalizedAudio:
    """
    Decode a full browser recording, run offline VAD, and write VAD WAV output.
    """
    if not raw_path.exists() or raw_path.stat().st_size == 0:
        raise AudioPipelineError(
            f"Raw audio file is empty or missing: {raw_path}"
        )

    decoded_path = Path(settings.audio.decoded_storage_dir) / f"{recording_id}.wav"
    vad_path = Path(settings.audio.vad_storage_dir) / f"{recording_id}.wav"
    decoded_path.parent.mkdir(parents=True, exist_ok=True)
    vad_path.parent.mkdir(parents=True, exist_ok=True)

    await _decode_to_wav(raw_path, decoded_path)
    decoded_pcm = _read_wav_pcm(decoded_path)
    if not decoded_pcm:
        raise AudioPipelineError(
            f"Decoded audio is empty for recording {recording_id}. Raw file: {raw_path.name}"
        )

    vad_pcm, speech_segments = _run_offline_vad(decoded_pcm, recording_id)
    if not vad_pcm:
        # Ensure stale output from previous attempts cannot be transcribed.
        if vad_path.exists():
            vad_path.unlink()
        raise AudioPipelineError(
            "VAD did not detect speech in the uploaded recording. "
            "Please record again and speak clearly near the microphone."
        )

    _write_wav(vad_path, vad_pcm)
    vad_bytes = vad_path.stat().st_size
    if vad_bytes < settings.audio.min_transcription_audio_bytes:
        raise AudioPipelineError(
            f"VAD output is too short for transcription ({vad_bytes} bytes). "
            "Please record at least one second of clear speech."
        )

    vad_duration = len(vad_pcm) / 2 / settings.audio.sample_rate
    logger.info(
        "Finalized recording_id=%d raw=%s decoded=%s vad=%s decoded_pcm=%d vad_pcm=%d segments=%d duration=%.2fs",
        recording_id,
        raw_path,
        decoded_path,
        vad_path,
        len(decoded_pcm),
        len(vad_pcm),
        speech_segments,
        vad_duration,
    )

    return FinalizedAudio(
        raw_path=raw_path,
        decoded_path=decoded_path,
        vad_path=vad_path,
        decoded_bytes=len(decoded_pcm),
        vad_bytes=vad_bytes,
        vad_duration_seconds=vad_duration,
        speech_segments=speech_segments,
    )


async def _decode_to_wav(raw_path: Path, decoded_path: Path) -> None:
    """Use ffmpeg to decode browser audio into PCM WAV at the configured rate."""
    cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(raw_path),
        "-acodec",
        "pcm_s16le",
        "-ar",
        str(settings.audio.sample_rate),
        "-ac",
        "1",
        str(decoded_path),
    ]

    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(
        None,
        lambda: subprocess.run(cmd, capture_output=True, text=True, check=False),
    )
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        raise AudioPipelineError(
            f"ffmpeg failed to decode raw audio '{raw_path.name}': {stderr or 'unknown error'}"
        )


def _read_wav_pcm(path: Path) -> bytes:
    """Read PCM frames from a WAV file produced by ffmpeg."""
    with wave.open(str(path), "rb") as wav:
        channels = wav.getnchannels()
        sample_width = wav.getsampwidth()
        sample_rate = wav.getframerate()
        if channels != 1 or sample_width != 2 or sample_rate != settings.audio.sample_rate:
            raise AudioPipelineError(
                f"Decoded WAV has unexpected format: channels={channels}, "
                f"sample_width={sample_width}, sample_rate={sample_rate}"
            )
        return wav.readframes(wav.getnframes())


def _run_offline_vad(pcm_bytes: bytes, recording_id: int) -> tuple[bytes, int]:
    """Run the existing VAD processor over the full decoded PCM stream."""
    vad = VADProcessor(recording_id=recording_id)
    speech_chunks: list[bytes] = []
    speech_segments = 0

    for segment in vad.process(pcm_bytes):
        speech_chunks.append(segment.pcm_bytes)
        speech_segments += 1

    trailing = vad.flush()
    if trailing:
        speech_chunks.append(trailing.pcm_bytes)
        speech_segments += 1

    return b"".join(speech_chunks), speech_segments


def _write_wav(path: Path, pcm_bytes: bytes) -> None:
    """Write PCM s16le bytes as a mono WAV file."""
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(settings.audio.sample_rate)
        wav.writeframes(pcm_bytes)
