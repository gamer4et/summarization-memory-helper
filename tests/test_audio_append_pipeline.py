from pathlib import Path
import wave

import pytest

from backend.services import audio_pipeline


def _write_wav(path: Path, pcm_bytes: bytes, sample_rate: int = 16_000) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(pcm_bytes)


def _read_wav(path: Path) -> bytes:
    with wave.open(str(path), "rb") as wav:
        return wav.readframes(wav.getnframes())


@pytest.mark.asyncio
async def test_append_raw_recording_concatenates_existing_decoded_audio(monkeypatch, tmp_path):
    decoded_dir = tmp_path / "decoded"
    vad_dir = tmp_path / "vad"
    raw_path = tmp_path / "raw" / "1-append.webm"
    raw_path.parent.mkdir(parents=True)
    raw_path.write_bytes(b"browser audio")

    existing_pcm = b"\x01\x00" * 160
    appended_pcm = b"\x02\x00" * 80
    decoded_path = decoded_dir / "1.wav"
    _write_wav(decoded_path, existing_pcm)

    monkeypatch.setattr(audio_pipeline.settings.audio, "decoded_storage_dir", str(decoded_dir))
    monkeypatch.setattr(audio_pipeline.settings.audio, "vad_storage_dir", str(vad_dir))
    monkeypatch.setattr(audio_pipeline.settings.audio, "sample_rate", 16_000)
    monkeypatch.setattr(audio_pipeline.settings.audio, "min_transcription_audio_bytes", 1)

    async def fake_decode_to_wav(source_path: Path, target_path: Path) -> None:
        assert source_path == raw_path
        _write_wav(target_path, appended_pcm)

    monkeypatch.setattr(audio_pipeline, "_decode_to_wav", fake_decode_to_wav)
    monkeypatch.setattr(audio_pipeline, "_run_offline_vad", lambda pcm, recording_id: [pcm])

    finalized = await audio_pipeline.append_raw_recording(raw_path, 1)

    combined_pcm = existing_pcm + appended_pcm
    assert _read_wav(decoded_path) == combined_pcm
    assert _read_wav(vad_dir / "1.wav") == combined_pcm
    assert finalized.vad_duration_seconds == pytest.approx(len(combined_pcm) / 2 / 16_000)
    assert finalized.speech_segments == 1
    assert not list(decoded_dir.glob("*.append-*.wav"))


@pytest.mark.asyncio
async def test_append_raw_recording_rejects_missing_existing_audio(monkeypatch, tmp_path):
    decoded_dir = tmp_path / "decoded"
    vad_dir = tmp_path / "vad"
    raw_path = tmp_path / "raw" / "1-append.webm"
    raw_path.parent.mkdir(parents=True)
    raw_path.write_bytes(b"browser audio")

    monkeypatch.setattr(audio_pipeline.settings.audio, "decoded_storage_dir", str(decoded_dir))
    monkeypatch.setattr(audio_pipeline.settings.audio, "vad_storage_dir", str(vad_dir))

    with pytest.raises(audio_pipeline.AudioPipelineError, match="no existing decoded/VAD audio"):
        await audio_pipeline.append_raw_recording(raw_path, 1)
