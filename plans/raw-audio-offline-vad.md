# Raw Audio Upload + Offline VAD Plan

## Why this change exists

The old flow streamed many small browser chunks over WebSocket and tried to run
decode + VAD live. The frontend volume meter could show activity because it was
driven locally by the microphone analyser, even when the backend ultimately
received no usable decoded/VAD audio.

The new flow makes recording deterministic:

1. The browser records locally while the user holds the recording session open.
2. When the user clicks stop, the browser waits for the final `MediaRecorder`
   data event and builds one complete raw audio blob.
3. The browser uploads that one raw blob to the backend.
4. The backend stores the raw blob, decodes the full file with `ffmpeg`, runs
   VAD offline, writes a separate VAD-filtered WAV, and only then starts
   transcription.

## Runtime directories

Configured in `config/settings.yaml`:

- `data/raw_audio/` — exact browser session upload, usually WebM/Opus.
- `data/decoded_audio/` — full decoded 16 kHz mono WAV for diagnostics.
- `data/vad_audio/` — VAD-filtered WAV used for Gemini/OpenRouter
  transcription and served at `/media/audio/{recording_id}.wav`.
- `data/audio/` — legacy assembled/live WAV directory retained for compatibility.

## Failure policy

Gemini/OpenRouter transcription uses only the VAD-filtered WAV from
`data/vad_audio/`. If VAD produces no speech or the output is too short, the
recording is marked `error` and the UI shows a clear message. There is no
fallback to raw or decoded audio.

## Relevant files

- `frontend/js/recorder.js` — captures one local raw session blob.
- `frontend/js/api.js` — uploads the raw blob through `POST /api/recordings/{id}/audio`.
- `frontend/js/components/recordingPanel.js` — waits for browser stop, upload,
  offline VAD, then processing.
- `backend/api/recordings.py` — accepts raw upload and updates recording status.
- `backend/services/raw_audio_storage.py` — stores browser raw blobs.
- `backend/services/audio_pipeline.py` — full decode + offline VAD + VAD WAV output.
- `backend/services/processor.py` — validates the VAD WAV before transcription.
