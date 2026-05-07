/**
 * recorder.js — MediaRecorder + WebSocket wrapper.
 *
 * Captures microphone audio using the browser's MediaRecorder API and keeps
 * browser chunks locally. When the user stops, it returns one complete raw
 * session Blob; the backend then handles full-file decode and offline VAD.
 *
 * Usage:
 *   const recorder = new AudioRecorder();
 *   await recorder.start(recordingId, { onStatus, onError, onStopped });
 *   recorder.pause();
 *   recorder.resume();
 *   recorder.stop();
 */

/** Preferred MIME types, tried in order. */
const MIME_CANDIDATES = [
  "audio/webm;codecs=opus",
  "audio/webm",
  "audio/ogg;codecs=opus",
  "audio/ogg",
];

const DEFAULT_AUDIO_CONSTRAINTS = {
  channelCount: 2,
  sampleRate: 48000,   // hint only; browser may ignore
  echoCancellation: false,
  noiseSuppression: false,
};

function pickMimeType() {
  for (const mime of MIME_CANDIDATES) {
    if (MediaRecorder.isTypeSupported(mime)) return mime;
  }
  return "";   // let the browser pick
}

export class AudioRecorder {
  constructor() {
    /** @type {MediaStream|null} */
    this._stream = null;
    /** @type {MediaRecorder|null} */
    this._mediaRecorder = null;
    this._isRecording = false;
    this._isPaused = false;
    this._chunksSent = 0;
    this._bytesTotal = 0;
    this._activeStartedAt = null;
    this._elapsedActiveMs = 0;
    this._mimeType = "";
    this._recordedChunks = [];
    this._stopPromise = null;

    // Callbacks set in start()
    this._onStatus  = null;
    this._onError   = null;
    this._onStopped = null;
    this._onVolume  = null;

    // Web Audio API nodes for volume monitoring
    /** @type {AudioContext|null} */
    this._audioCtx = null;
    /** @type {AnalyserNode|null} */
    this._analyser = null;
    /** @type {number|null} RAF handle */
    this._volumeRafId = null;
  }

  // -------------------------------------------------------------------------
  // Public API
  // -------------------------------------------------------------------------

  get isRecording() { return this._isRecording; }

  get isPaused() { return this._isPaused; }

  /** Elapsed active recording time in seconds, excluding paused intervals. */
  get elapsedSeconds() {
    let elapsedMs = this._elapsedActiveMs;
    if (this._isRecording && !this._isPaused && this._activeStartedAt) {
      elapsedMs += Date.now() - this._activeStartedAt;
    }
    return elapsedMs / 1000;
  }

  /**
   * Request microphone access and start capturing one local browser blob.
   *
   * @param {number} recordingId  — DB recording id
   * @param {object} callbacks
   * @param {(msg: object) => void} callbacks.onStatus   — local capture status messages
   * @param {(err: Error|string) => void} callbacks.onError
   * @param {(msg: object) => void} callbacks.onStopped  — local stop status
   */
  /**
   * @param {number} recordingId
   * @param {object} callbacks
   * @param {(msg: object) => void}      [callbacks.onStatus]
   * @param {(err: Error|string) => void} [callbacks.onError]
   * @param {(msg: object) => void}      [callbacks.onStopped]
   * @param {(level: number) => void}    [callbacks.onVolume]  — 0..100 RMS level, ~30 fps
   * @param {MediaTrackConstraints}      [callbacks.audioConstraints] — browser mic constraints
   */
  async start(recordingId, { onStatus = null, onError = null, onStopped = null, onVolume = null, audioConstraints = null } = {}) {
    if (this._isRecording) throw new Error("Already recording.");

    this._onStatus  = onStatus;
    this._onError   = onError;
    this._onStopped = onStopped;
    this._onVolume  = onVolume;
    this._chunksSent = 0;
    this._bytesTotal = 0;
    this._stopPromise = null;
    this._isPaused = false;
    this._elapsedActiveMs = 0;
    this._activeStartedAt = null;

    // 1. Request microphone
    try {
      const constraints = {
        ...DEFAULT_AUDIO_CONSTRAINTS,
        ...(audioConstraints || {}),
      };
      this._stream = await navigator.mediaDevices.getUserMedia({
        audio: constraints,
        video: false,
      });

      const [audioTrack] = this._stream.getAudioTracks();
      if (audioTrack && typeof audioTrack.getSettings === "function") {
        console.info("[AudioRecorder] Requested mic constraints:", constraints);
        console.info("[AudioRecorder] Actual mic settings:", audioTrack.getSettings());
      }
    } catch (err) {
      const msg = err.name === "NotAllowedError"
        ? "Microphone access denied. Please allow microphone access and try again."
        : `Could not access microphone: ${err.message}`;
      this._emitError(msg);
      throw new Error(msg);
    }

    // 1b. Set up Web Audio AnalyserNode for volume monitoring
    this._setupAnalyser();

    // 2. Set up MediaRecorder
    const mimeType = pickMimeType();
    const options = mimeType ? { mimeType } : {};
    this._mimeType = mimeType;
    this._recordedChunks = [];

    try {
      this._mediaRecorder = new MediaRecorder(this._stream, options);
    } catch (err) {
      this._cleanup();
      const msg = `MediaRecorder init failed: ${err.message}`;
      this._emitError(msg);
      throw new Error(msg);
    }

    this._mediaRecorder.ondataavailable = (event) => {
      if (event.data.size > 0) {
        this._recordedChunks.push(event.data);
        this._chunksSent++;
        this._bytesTotal += event.data.size;
        if (typeof this._onStatus === "function") {
          this._onStatus({
            status: "chunk_buffered",
            chunks: this._chunksSent,
            bytes: this._bytesTotal,
          });
        }
      }
    };

    this._mediaRecorder.onerror = (event) => {
      this._emitError(`MediaRecorder error: ${event.error?.message || "unknown"}`);
    };

    // Emit chunks periodically into local memory. Nothing is uploaded until stop.
    this._mediaRecorder.start(1000);
    this._isRecording = true;
    this._activeStartedAt = Date.now();

    // Start volume polling loop
    this._startVolumeMonitor();
  }

  /**
   * Pause media capture. Browser MediaRecorder excludes paused intervals from
   * the final Blob and keeps appending resumed audio to the same session.
   */
  pause() {
    if (!this._isRecording || this._isPaused) return false;
    const recorder = this._mediaRecorder;
    if (!recorder || recorder.state !== "recording") return false;

    if (this._activeStartedAt) {
      this._elapsedActiveMs += Date.now() - this._activeStartedAt;
      this._activeStartedAt = null;
    }

    recorder.pause();
    this._isPaused = true;
    this._pauseVolumeMonitor();
    return true;
  }

  /** Resume media capture after pause. */
  resume() {
    if (!this._isRecording || !this._isPaused) return false;
    const recorder = this._mediaRecorder;
    if (!recorder || recorder.state !== "paused") return false;

    recorder.resume();
    this._isPaused = false;
    this._activeStartedAt = Date.now();
    this._startVolumeMonitor();
    return true;
  }

  /**
   * Stop recording and resolve to one complete raw audio Blob after the browser
   * has fired its final dataavailable event.
   */
  stop() {
    if (!this._isRecording) return Promise.resolve(null);
    if (this._stopPromise) return this._stopPromise;

    this._stopPromise = new Promise((resolve, reject) => {
      const recorder = this._mediaRecorder;
      if (!recorder || recorder.state === "inactive") {
        resolve(this._buildBlob());
        return;
      }

      recorder.onstop = () => {
        try {
          const blob = this._buildBlob();
          if (typeof this._onStopped === "function") {
            this._onStopped({
              status: "stopped",
              chunks: this._chunksSent,
              bytes: this._bytesTotal,
              mimeType: blob.type,
            });
          }
          resolve(blob);
        } catch (err) {
          reject(err);
        }
      };

      try {
        if (!this._isPaused && this._activeStartedAt) {
          this._elapsedActiveMs += Date.now() - this._activeStartedAt;
          this._activeStartedAt = null;
        }
        recorder.stop();
      } catch (err) {
        reject(err);
      }
    });

    this._isRecording = false;
    this._isPaused = false;
    this._stopVolumeMonitor();
    this._stopTracks();

    return this._stopPromise;
  }

  // -------------------------------------------------------------------------
  // Internal helpers
  // -------------------------------------------------------------------------

  _buildBlob() {
    if (!this._recordedChunks.length) {
      throw new Error("Browser produced no audio chunks. Please try recording again.");
    }
    return new Blob(this._recordedChunks, {
      type: this._mimeType || this._recordedChunks[0]?.type || "audio/webm",
    });
  }

  _stopTracks() {
    if (this._stream) {
      this._stream.getTracks().forEach((t) => t.stop());
      this._stream = null;
    }
  }

  // -------------------------------------------------------------------------
  // Volume monitoring (Web Audio API)
  // -------------------------------------------------------------------------

  /**
   * Create an AnalyserNode connected to the current MediaStream.
   * Must be called after this._stream is set.
   */
  _setupAnalyser() {
    if (!this._stream) return;
    try {
      const AudioCtx = window.AudioContext || window.webkitAudioContext;
      if (!AudioCtx) return;
      this._audioCtx = new AudioCtx();
      this._analyser = this._audioCtx.createAnalyser();
      this._analyser.fftSize = 256;
      this._analyser.smoothingTimeConstant = 0.75;
      const source = this._audioCtx.createMediaStreamSource(this._stream);
      source.connect(this._analyser);
      // Do NOT connect analyser → destination (avoids mic playback)
    } catch (err) {
      console.warn("[AudioRecorder] AnalyserNode setup failed:", err);
      this._analyser = null;
    }
  }

  /** Start the requestAnimationFrame loop that reads RMS volume and fires onVolume. */
  _startVolumeMonitor() {
    if (!this._analyser || typeof this._onVolume !== "function") return;
    if (this._volumeRafId !== null) return;

    const dataArray = new Uint8Array(this._analyser.frequencyBinCount);

    const loop = () => {
      if (!this._isRecording || this._isPaused || !this._analyser) {
        this._volumeRafId = null;
        return;
      }

      this._analyser.getByteTimeDomainData(dataArray);

      // Compute RMS of the time-domain signal, normalised to 0–1
      let sumSq = 0;
      for (let i = 0; i < dataArray.length; i++) {
        const v = (dataArray[i] - 128) / 128; // -1..1
        sumSq += v * v;
      }
      const rms = Math.sqrt(sumSq / dataArray.length);

      // Scale to 0–100; multiply by ~350 so normal speech hits 40–80
      const level = Math.min(100, Math.round(rms * 350));

      this._onVolume(level);
      this._volumeRafId = requestAnimationFrame(loop);
    };

    this._volumeRafId = requestAnimationFrame(loop);
  }

  /** Pause only the visual volume loop; keep AudioContext ready for resume. */
  _pauseVolumeMonitor() {
    if (this._volumeRafId !== null) {
      cancelAnimationFrame(this._volumeRafId);
      this._volumeRafId = null;
    }
    if (typeof this._onVolume === "function") {
      this._onVolume(0);
    }
  }

  /** Cancel the RAF loop and close the AudioContext. */
  _stopVolumeMonitor() {
    this._pauseVolumeMonitor();
    if (this._audioCtx) {
      this._audioCtx.close().catch(() => {});
      this._audioCtx = null;
      this._analyser = null;
    }
  }

  _cleanup() {
    this._stopVolumeMonitor();
    this._stopTracks();
    this._recordedChunks = [];
    this._stopPromise = null;
    this._isRecording = false;
    this._isPaused = false;
    this._activeStartedAt = null;
    this._elapsedActiveMs = 0;
  }

  _emitError(msg) {
    if (typeof this._onError === "function") {
      this._onError(msg);
    } else {
      console.error("[AudioRecorder]", msg);
    }
  }
}
