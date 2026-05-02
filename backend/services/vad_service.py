"""
Voice Activity Detection (VAD) processor.

Wraps ``webrtcvad`` with a ring-buffer approach to detect speech / silence
boundaries in a continuous PCM s16le byte stream.

Usage
-----
    vad = VADProcessor(recording_id=42)

    # Feed raw PCM chunks as they arrive; collect complete speech segments.
    for raw_pcm in stream:
        segments = vad.process(raw_pcm)
        for seg in segments:
            await writer.append(seg)

    # Flush any trailing speech when the stream ends.
    trailing = vad.flush()
    if trailing:
        await writer.append(trailing)
"""

import collections
import logging
from dataclasses import dataclass
from typing import Deque, List, Optional, Tuple

import webrtcvad

from backend.core.config import settings

logger = logging.getLogger(__name__)


@dataclass
class SpeechSegment:
    """A contiguous block of voiced audio in PCM s16le format."""

    pcm_bytes: bytes
    duration_ms: int


class VADProcessor:
    """
    Converts a raw PCM s16le stream into discrete :class:`SpeechSegment` objects.

    Algorithm
    ---------
    * Incoming bytes are split into fixed-size frames.
    * A ring-buffer of the last ``max_silence_ms / frame_duration_ms`` frames
      is maintained.
    * When > 90 % of ring-buffer frames are voiced → speech starts (``triggered``).
    * When > 90 % of ring-buffer frames are silent after ``triggered`` → speech
      ends; the accumulated frames are emitted as a :class:`SpeechSegment`.
    * Segments shorter than ``min_speech_duration_ms`` are discarded.
    """

    def __init__(self, recording_id: int) -> None:
        self.recording_id = recording_id
        self._vad = webrtcvad.Vad(settings.audio.vad_aggressiveness)
        self.sample_rate: int = settings.audio.sample_rate
        self.frame_duration_ms: int = settings.audio.frame_duration_ms

        # Number of bytes per frame: sample_rate * duration_s * 2 bytes/sample
        self.frame_bytes: int = (
            int(self.sample_rate * self.frame_duration_ms / 1000) * 2
        )

        # Ring buffer length = how many frames fit in max_silence_ms
        ring_len = max(1, settings.audio.max_silence_ms // self.frame_duration_ms)
        self._ring: Deque[Tuple[bytes, bool]] = collections.deque(maxlen=ring_len)

        self._triggered: bool = False
        self._buffered_frames: List[bytes] = []
        # Accumulate partial bytes that don't yet form a complete frame.
        self._leftover: bytes = b""

        logger.debug(
            "VADProcessor created: recording_id=%d frame_bytes=%d ring_len=%d",
            recording_id,
            self.frame_bytes,
            ring_len,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def process(self, pcm_bytes: bytes) -> List[SpeechSegment]:
        """
        Feed raw PCM s16le bytes; return any completed speech segments.

        The input does **not** need to be aligned to frame boundaries; leftover
        bytes are buffered internally and prepended to the next call.
        """
        data = self._leftover + pcm_bytes
        segments: List[SpeechSegment] = []
        offset = 0

        while offset + self.frame_bytes <= len(data):
            frame = data[offset : offset + self.frame_bytes]
            offset += self.frame_bytes
            seg = self._process_frame(frame)
            if seg is not None:
                segments.append(seg)

        self._leftover = data[offset:]
        return segments

    def flush(self) -> Optional[SpeechSegment]:
        """
        Emit a final :class:`SpeechSegment` for any trailing speech.

        Call this when the audio stream ends (WebSocket close / stop action).
        """
        if self._triggered and self._buffered_frames:
            segment_bytes = b"".join(self._buffered_frames)
            duration_ms = self._bytes_to_ms(len(segment_bytes))
            self._triggered = False
            self._buffered_frames.clear()
            self._ring.clear()
            if duration_ms >= settings.audio.min_speech_duration_ms:
                logger.debug(
                    "VAD flush: emitting %d ms segment (recording_id=%d)",
                    duration_ms,
                    self.recording_id,
                )
                return SpeechSegment(pcm_bytes=segment_bytes, duration_ms=duration_ms)
        return None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _process_frame(self, frame: bytes) -> Optional[SpeechSegment]:
        """Process a single frame; return a SpeechSegment if one is complete."""
        is_speech = self._vad.is_speech(frame, self.sample_rate)

        if not self._triggered:
            self._ring.append((frame, is_speech))
            num_voiced = sum(1 for _, s in self._ring if s)
            if num_voiced > 0.9 * self._ring.maxlen:
                # Speech onset detected — seed the buffer with ring contents.
                self._triggered = True
                self._buffered_frames = [f for f, _ in self._ring]
                self._ring.clear()
                logger.debug("VAD: speech onset (recording_id=%d)", self.recording_id)
        else:
            self._buffered_frames.append(frame)
            self._ring.append((frame, is_speech))
            num_silent = sum(1 for _, s in self._ring if not s)
            if num_silent > 0.9 * self._ring.maxlen:
                # Silence long enough → end of utterance.
                self._triggered = False
                segment_bytes = b"".join(self._buffered_frames)
                duration_ms = self._bytes_to_ms(len(segment_bytes))
                self._buffered_frames = []
                self._ring.clear()
                if duration_ms >= settings.audio.min_speech_duration_ms:
                    logger.debug(
                        "VAD: utterance end — %d ms (recording_id=%d)",
                        duration_ms,
                        self.recording_id,
                    )
                    return SpeechSegment(pcm_bytes=segment_bytes, duration_ms=duration_ms)
                else:
                    logger.debug(
                        "VAD: dropped short segment %d ms (recording_id=%d)",
                        duration_ms,
                        self.recording_id,
                    )
        return None

    def _bytes_to_ms(self, num_bytes: int) -> int:
        """Convert a PCM s16le byte count to milliseconds."""
        # Each sample is 2 bytes; sample_rate samples/sec → ms
        return num_bytes // 2 * 1000 // self.sample_rate
