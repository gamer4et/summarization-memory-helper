/**
 * recordingPanel.js — Recording view for a selected book.
 *
 * Flow:
 *   1. POST /api/recordings to create a DB row → get recording_id, or reuse an existing recording id in continuation mode
 *   2. User selects a language from the dropdown (default: Russian)
 *   3. "Start Recording" → AudioRecorder.start(recording_id)
 *   4. Optional "Pause" excludes paused time from the final audio; "Resume" keeps appending to the same blob
 *   5. "Stop & Process" → AudioRecorder.stop() → upload one raw blob or append blob → offline VAD → POST process
 *   6. Navigate to chapter view on success
 *
 * Exported:
 *   renderRecordingPanel(container, { book, appendToRecordingId, onBack, onViewChapters })
 */

import { api, ApiError, appendRecordingAudio, processRecording, uploadRecordingAudio } from "../api.js";
import { AudioRecorder } from "../recorder.js";
import { showToast } from "../app.js";

/**
 * @param {HTMLElement} container
 * @param {object} opts
 * @param {object}   opts.book              — BookOut object
 * @param {number|null} [opts.appendToRecordingId] — existing Recording id to append to
 * @param {() => void} opts.onBack          — navigate back to book list
 * @param {(recordingId: number) => void} opts.onViewChapters
 */
export async function renderRecordingPanel(container, { book, appendToRecordingId = null, onBack, onViewChapters }) {
  const isAppendMode = Boolean(appendToRecordingId);
  container.innerHTML = buildPanelHTML(book, { isAppendMode, appendToRecordingId });

  const stateEl      = container.querySelector("#rec-status-text");
  const iconEl       = container.querySelector("#rec-icon");
  const startBtn     = container.querySelector("#btn-start");
  const pauseBtn     = container.querySelector("#btn-pause");
  const stopBtn      = container.querySelector("#btn-stop");
  const processEl    = container.querySelector("#processing-status");
  const elapsedEl    = container.querySelector("#elapsed-time");
  const chunksEl     = container.querySelector("#chunks-count");
  const languageSel  = container.querySelector("#language-select");
  const micControls  = Array.from(container.querySelectorAll("[data-mic-setting]"));
  const volMeterEl   = container.querySelector("#volume-meter");
  const volFillEl    = container.querySelector("#volume-fill");

  // Component state — language selection (default: Russian)
  let selectedLanguage = languageSel.value;

  languageSel.addEventListener("change", () => {
    selectedLanguage = languageSel.value;
  });

  // Create a new Recording row unless the panel was opened to continue one.
  let recording = isAppendMode ? { id: appendToRecordingId, book_id: book.id } : null;
  if (!recording) {
    try {
      recording = await api.post("/api/recordings", { book_id: book.id });
    } catch (err) {
      container.innerHTML = `
        <div class="card" style="color:var(--color-danger)">
          <strong>Failed to create recording:</strong> ${escHtml(err.detail || err.message)}
        </div>
        <div class="view-actions">
          <button class="btn-ghost" id="btn-back-err">← Back</button>
        </div>`;
      container.querySelector("#btn-back-err").addEventListener("click", onBack);
      return;
    }
  }

  const recorder = new AudioRecorder();

  // ── Elapsed-time ticker ───────────────────────────────────────────────────
  let tickInterval = null;

  function startTick() {
    tickInterval = setInterval(() => {
      const s = recorder.elapsedSeconds;
      elapsedEl.textContent = formatDuration(s);
    }, 500);
  }

  function stopTick() {
    clearInterval(tickInterval);
    tickInterval = null;
  }

  // ── Start button ──────────────────────────────────────────────────────────
  startBtn.addEventListener("click", async () => {
    startBtn.disabled = true;
    languageSel.disabled = true;   // lock language once recording starts
    setMicControlsDisabled(micControls, true);
    stateEl.textContent = "Requesting microphone access…";
    setProcessingStatus(processEl, "");

    try {
      await recorder.start(recording.id, {
        audioConstraints: readMicConstraints(container),
        onStatus(msg) {
          const n = msg.chunks || (parseInt(chunksEl.dataset.count || "0") + 1);
          chunksEl.dataset.count = n;
          chunksEl.textContent = n;
        },
        onError(errMsg) {
          showToast(errMsg, "error");
          setRecordingUI("idle", { iconEl, stateEl, startBtn, pauseBtn, stopBtn, isAppendMode });
          setVolumeMeter(volMeterEl, volFillEl, 0, false);
          stopTick();
        },
        onStopped(msg) {
          // Handled in stop flow below
          stopTick();
        },
        onVolume(level) {
          setVolumeMeter(volMeterEl, volFillEl, level, true);
        },
      });

      setRecordingUI("recording", { iconEl, stateEl, startBtn, pauseBtn, stopBtn, isAppendMode });
      setVolumeMeter(volMeterEl, volFillEl, 0, true);
      startTick();
    } catch (err) {
      showToast(err.message, "error");
      setRecordingUI("idle", { iconEl, stateEl, startBtn, pauseBtn, stopBtn, isAppendMode });
      setVolumeMeter(volMeterEl, volFillEl, 0, false);
      startBtn.disabled = false;
      languageSel.disabled = false;
      setMicControlsDisabled(micControls, false);
    }
  });

  // ── Pause / resume button ────────────────────────────────────────────────
  pauseBtn.addEventListener("click", () => {
    if (!recorder.isRecording) return;

    if (recorder.isPaused) {
      const resumed = recorder.resume();
      if (!resumed) return;
      setRecordingUI("recording", { iconEl, stateEl, startBtn, pauseBtn, stopBtn, isAppendMode });
      setVolumeMeter(volMeterEl, volFillEl, 0, true);
      return;
    }

    const paused = recorder.pause();
    if (!paused) return;
    setRecordingUI("paused", { iconEl, stateEl, startBtn, pauseBtn, stopBtn, isAppendMode });
    setVolumeMeter(volMeterEl, volFillEl, 0, false);
  });

  // ── Stop button ───────────────────────────────────────────────────────────
  stopBtn.addEventListener("click", async () => {
    setRecordingUI("stopping", { iconEl, stateEl, startBtn, pauseBtn, stopBtn, isAppendMode });
    setVolumeMeter(volMeterEl, volFillEl, 0, false);

    try {
      const audioBlob = await recorder.stop();
      stopTick();
      setRecordingUI("finalizing", { iconEl, stateEl, startBtn, pauseBtn, stopBtn, isAppendMode });
      setVolumeMeter(volMeterEl, volFillEl, 0, false);

      if (!audioBlob || audioBlob.size === 0) {
        throw new Error("Browser produced an empty audio blob. Please record again.");
      }

      setRecordingUI("finalizing", { iconEl, stateEl, startBtn, pauseBtn, stopBtn, isAppendMode });
      stateEl.textContent = isAppendMode
        ? "Uploading additional raw audio…"
        : "Uploading complete raw recording…";
      setProcessingStatus(
        processEl,
        "processing",
        isAppendMode
          ? "⬆️ Appending audio and rebuilding VAD-filtered recording…"
          : "⬆️ Uploading raw audio and running offline VAD…"
      );
      if (isAppendMode) {
        await appendRecordingAudio(recording.id, audioBlob);
      } else {
        await uploadRecordingAudio(recording.id, audioBlob);
      }

      stateEl.textContent = isAppendMode
        ? "Combined VAD-filtered audio is ready."
        : "VAD-filtered audio is ready.";

      // Trigger the transcription + summarisation pipeline with the selected language
      setRecordingUI("processing", { iconEl, stateEl, startBtn, pauseBtn, stopBtn, isAppendMode });
      await runProcessing(recording.id, selectedLanguage, processEl, onViewChapters);
      setRecordingUI("idle", { iconEl, stateEl, startBtn, pauseBtn, stopBtn, isAppendMode });
      languageSel.disabled = false;
      setMicControlsDisabled(micControls, false);
    } catch (err) {
      const detail = err instanceof ApiError ? err.detail : err.message;
      setProcessingStatus(processEl, "error", `❌ Recording finalization failed: ${escHtml(detail)}`);
      showToast("Recording finalization failed — see panel for details.", "error");
      setRecordingUI("idle", { iconEl, stateEl, startBtn, pauseBtn, stopBtn, isAppendMode });
      setVolumeMeter(volMeterEl, volFillEl, 0, false);
      startBtn.disabled = false;
      languageSel.disabled = false;
      setMicControlsDisabled(micControls, false);
    }
  });

  // ── Back button ───────────────────────────────────────────────────────────
  container.querySelector("#btn-back").addEventListener("click", () => {
    if (recorder.isRecording) {
      if (!confirm("A recording is in progress. Leave and discard it?")) return;
      recorder.stop();
      stopTick();
    }
    onBack();
  });
}

// ---------------------------------------------------------------------------
// Processing
// ---------------------------------------------------------------------------

async function runProcessing(recordingId, language, processEl, onViewChapters) {
  setProcessingStatus(processEl, "processing", "⏳ Transcribing audio…");

  try {
    const result = await processRecording(recordingId, language);
    setProcessingStatus(processEl, "success",
      `✅ Done! ${result.chapters?.length ?? 0} chapter(s) detected.`);
    showToast("Processing complete!", "success");

    // Brief pause so user can read the status, then navigate
    await sleep(900);
    onViewChapters(recordingId);
  } catch (err) {
    const detail = err instanceof ApiError ? err.detail : err.message;
    setProcessingStatus(processEl, "error", `❌ Processing failed: ${escHtml(detail)}`);
    showToast("Processing failed — see panel for details.", "error");
  }
}

// ---------------------------------------------------------------------------
// UI state helpers
// ---------------------------------------------------------------------------

/**
 * Show/hide and update the volume meter bar.
 * @param {HTMLElement} containerEl  — the .volume-meter wrapper
 * @param {HTMLElement} fillEl       — the .volume-meter-fill bar
 * @param {number}      level        — 0–100 RMS level
 * @param {boolean}     active       — whether recording is live
 */
function setVolumeMeter(containerEl, fillEl, level, active) {
  if (!containerEl || !fillEl) return;
  containerEl.classList.toggle("is-active", active);
  fillEl.style.width = `${level}%`;

  // Colour: green → amber → red based on level
  let color;
  if (level < 40)       color = "var(--color-success)";
  else if (level < 75)  color = "var(--color-warning)";
  else                  color = "var(--color-danger)";
  fillEl.style.backgroundColor = color;
}

function readMicConstraints(container) {
  const channelCount = Number(container.querySelector("#mic-channel-count")?.value || 2);
  const sampleRate = Number(container.querySelector("#mic-sample-rate")?.value || 48000);
  const echoCancellation = Boolean(container.querySelector("#mic-echo-cancellation")?.checked);
  const noiseSuppression = Boolean(container.querySelector("#mic-noise-suppression")?.checked);

  return {
    channelCount,
    sampleRate,
    echoCancellation,
    noiseSuppression,
  };
}

function setMicControlsDisabled(controls, disabled) {
  controls.forEach((control) => {
    control.disabled = disabled;
  });
}

function setRecordingUI(mode, { iconEl, stateEl, startBtn, pauseBtn, stopBtn, isAppendMode = false }) {
  const isRecording = mode === "recording";
  const isPaused    = mode === "paused";
  const isStopping  = mode === "stopping";
  const isFinalizing = mode === "finalizing";
  const isProcessing = mode === "processing";
  const isBusy      = isRecording || isPaused || isStopping || isFinalizing || isProcessing;

  iconEl.textContent = isRecording ? "🔴" : isPaused ? "⏸" : "🎙";

  if (isRecording) {
    stateEl.innerHTML = `<span class="pulse-ring"></span>Recording in progress… say <em>"new chapter"</em> to split chapters`;
  } else if (isPaused) {
    stateEl.innerHTML = `⏸ Recording paused. Paused time will not be included in the final audio.`;
  } else if (isStopping) {
    stateEl.textContent = "Stopping recording…";
  } else if (isFinalizing) {
    stateEl.textContent = "Preparing final audio…";
  } else if (isProcessing) {
    stateEl.textContent = "Transcribing and summarizing audio…";
  } else {
    stateEl.innerHTML = isAppendMode
      ? "Press <strong>Start Recording</strong> to append audio to the existing recording."
      : "Press <strong>Start Recording</strong> to begin.";
  }

  startBtn.disabled = isBusy;
  pauseBtn.disabled = !isRecording && !isPaused;
  pauseBtn.textContent = isPaused ? "▶ Resume" : "⏸ Pause";
  stopBtn.disabled  = !isRecording && !isPaused;

  const recCard = iconEl.closest(".recorder-card");
  if (recCard) {
    recCard.classList.toggle("is-recording", isRecording);
    recCard.classList.toggle("is-paused", isPaused);
  }
}

/** @param {'processing'|'success'|'error'|''} type */
function setProcessingStatus(el, type, text = "") {
  if (!el) return;
  el.className = "processing-status";
  el.hidden    = !text;
  el.textContent = text;
  if (type) el.classList.add(`is-${type}`);
}

// ---------------------------------------------------------------------------
// HTML template
// ---------------------------------------------------------------------------

function buildPanelHTML(book, { isAppendMode = false, appendToRecordingId = null } = {}) {
  return `
    <div class="recording-panel">
      <div class="recording-panel-header">
        <h2>${isAppendMode ? "Continue Recording" : escHtml(book.title)}</h2>
        ${book.author ? `<div class="author">by ${escHtml(book.author)}</div>` : ""}
        ${isAppendMode ? `<div class="book-detail-meta">Appending to recording #${escHtml(appendToRecordingId)}</div>` : ""}
      </div>

      <div class="recorder-card" id="recorder-card">
        <div id="rec-icon" class="rec-icon">🎙</div>
        <div id="rec-status-text" class="rec-status-text">
          ${isAppendMode
            ? `Press <strong>Start Recording</strong> to append audio to the existing recording.`
            : `Press <strong>Start Recording</strong> to begin.`}
        </div>

        <div class="rec-language">
          <label for="language-select">🌐 Language:</label>
          <select id="language-select" class="language-select">
            <option value="ru" selected>Russian (ru)</option>
            <option value="en">English (en)</option>
            <option value="auto">Auto-detect</option>
            <option value="de">German (de)</option>
            <option value="fr">French (fr)</option>
            <option value="es">Spanish (es)</option>
          </select>
        </div>

        <fieldset class="mic-settings" aria-label="Browser microphone constraints">
          <legend>🎚 Browser audio flags</legend>
          <label class="mic-setting">
            <span>Channels</span>
            <select id="mic-channel-count" data-mic-setting>
              <option value="1">1 / mono</option>
              <option value="2" selected>2 / stereo</option>
            </select>
          </label>
          <label class="mic-setting">
            <span>Sample rate hint</span>
            <select id="mic-sample-rate" data-mic-setting>
              <option value="16000">16 kHz</option>
              <option value="44100">44.1 kHz</option>
              <option value="48000" selected>48 kHz</option>
            </select>
          </label>
          <label class="mic-setting mic-setting-checkbox">
            <input id="mic-echo-cancellation" type="checkbox" data-mic-setting>
            <span>echoCancellation</span>
          </label>
          <label class="mic-setting mic-setting-checkbox">
            <input id="mic-noise-suppression" type="checkbox" data-mic-setting>
            <span>noiseSuppression</span>
          </label>
          <div class="mic-settings-note">
            Defaults favor clean USB microphones: 48 kHz stereo, browser echo/noise processing off.
          </div>
        </fieldset>

        <div class="volume-meter" id="volume-meter" aria-label="Microphone volume">
          <span class="volume-meter-label">🎤</span>
          <div class="volume-meter-track">
            <div class="volume-meter-fill" id="volume-fill"></div>
          </div>
        </div>

        <div class="rec-controls">
          <button class="btn-primary btn-lg" id="btn-start">▶ ${isAppendMode ? "Start Continuation" : "Start Recording"}</button>
          <button class="btn-warning btn-lg" id="btn-pause" disabled>⏸ Pause</button>
          <button class="btn-danger btn-lg" id="btn-stop" disabled>⏹ Stop &amp; Process</button>
        </div>

        <div class="live-stats">
          <div>
            Elapsed: <span class="stat-value" id="elapsed-time">0:00</span>
          </div>
          <div>
            Chunks buffered: <span class="stat-value" id="chunks-count" data-count="0">0</span>
          </div>
        </div>
      </div>

      <div id="processing-status" class="processing-status" hidden></div>

      <div class="view-actions">
        <button class="btn-ghost" id="btn-back">← Back to Book</button>
      </div>

      <div class="card mt-3" style="font-size:.88rem;color:var(--color-text-muted)">
        <strong>Tip:</strong> ${isAppendMode
          ? "After stopping, the new audio will be appended and the full recording will be processed again, replacing previous results."
          : "Select the spoken language before recording."}
        While recording, say phrases like
        <em>"new chapter"</em>, <em>"next chapter"</em>,
        <em>"chapter three"</em>, or <em>"глава один"</em>
        to create chapter divisions.
      </div>
    </div>
  `;
}

// ---------------------------------------------------------------------------
// Utilities
// ---------------------------------------------------------------------------

function escHtml(str) {
  return String(str ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function formatDuration(totalSeconds) {
  const s = Math.floor(totalSeconds % 60);
  const m = Math.floor(totalSeconds / 60);
  return `${m}:${String(s).padStart(2, "0")}`;
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}
