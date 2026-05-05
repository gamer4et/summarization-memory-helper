"""
Storage helpers for complete browser-recorded audio sessions.

The new recording flow intentionally stores the exact browser blob first
(WebM/Opus, Ogg/Opus, or browser-selected audio format). Offline decode and
VAD then run against this full file instead of trying to decode tiny live
chunks.
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import UploadFile

from backend.core.config import settings

logger = logging.getLogger(__name__)


_MIME_EXTENSIONS: dict[str, str] = {
    "audio/webm": ".webm",
    "audio/webm;codecs=opus": ".webm",
    "audio/ogg": ".ogg",
    "audio/ogg;codecs=opus": ".ogg",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/mpeg": ".mp3",
    "audio/mp4": ".m4a",
}


def extension_for_mime(content_type: str | None, filename: str | None = None) -> str:
    """Return a safe file extension for a browser audio upload."""
    normalized = (content_type or "").split(";")[0].strip().lower()
    if normalized in _MIME_EXTENSIONS:
        return _MIME_EXTENSIONS[normalized]

    if filename:
        suffix = Path(filename).suffix.lower()
        if suffix in {".webm", ".ogg", ".wav", ".mp3", ".m4a"}:
            return suffix

    # Chrome/Edge default for MediaRecorder with Opus.
    return ".webm"


async def save_raw_audio_upload(
    recording_id: int,
    upload: UploadFile,
    *,
    suffix: str = "",
) -> Path:
    """Persist a complete browser recording blob and return its path."""
    raw_dir = Path(settings.audio.raw_storage_dir)
    raw_dir.mkdir(parents=True, exist_ok=True)

    ext = extension_for_mime(upload.content_type, upload.filename)
    path = raw_dir / f"{recording_id}{suffix}{ext}"

    total_bytes = 0
    with path.open("wb") as out:
        while True:
            chunk = await upload.read(1024 * 1024)
            if not chunk:
                break
            total_bytes += len(chunk)
            out.write(chunk)

    logger.info(
        "Saved raw browser audio for recording_id=%d: %s (%d bytes, content_type=%s)",
        recording_id,
        path,
        total_bytes,
        upload.content_type,
    )

    return path
